"""Fake ACP agent (stdio JSON-RPC) for tests.

Env: FAKE_ACP_STATE (dir for persisted sessions), FAKE_ACP_CAPS (comma list of
resume,load,list,close). A prompt is a script, one command per line:
  say TEXT | msg TEXT | tool TITLE IN|OUT | think TEXT | recall | sleep S | hang S | perm KIND |
  escalate TITLE IN|OUT | write FILE TEXT
  big N | tool_big N | crash | fail | env NAME | model
"""
import asyncio
import json
import os
import sys
import uuid

STATE = os.environ["FAKE_ACP_STATE"]
CAPS = {c for c in os.environ.get("FAKE_ACP_CAPS", "resume,list,close").split(",") if c}
os.makedirs(STATE, exist_ok=True)
live: dict[str, dict] = {}
pending: dict[int, asyncio.Future] = {}
next_id = 1000
MODELS = [{"value": "m-small", "name": "Small"}, {"value": "m-big", "name": "Big"}]


def path(sid):
    return os.path.join(STATE, sid + ".json")


def load(sid):
    with open(path(sid)) as f:
        return json.load(f)


def save(sid, h):
    with open(path(sid), "w") as f:
        json.dump(h, f)


def config_options(h):
    return [{"id": "model", "name": "Model", "category": "model", "type": "select",
             "currentValue": h.get("model", "m-small"), "options": [{"group": "g", "name": "G", "options": MODELS}]}]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def update(sid, upd):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": upd}})


def say(sid, mid, text):
    update(sid, {"sessionUpdate": "agent_message_chunk", "messageId": mid, "content": {"type": "text", "text": text}})


async def client_request(method, params):
    global next_id
    next_id += 1
    fut = asyncio.get_running_loop().create_future()
    pending[next_id] = fut
    send({"jsonrpc": "2.0", "id": next_id, "method": method, "params": params})
    return await fut


class RpcError(Exception):
    pass


async def run_prompt(sid, text):
    s = live[sid]
    h = load(sid)
    mid = uuid.uuid4().hex
    s.setdefault("cancel", asyncio.Event())
    stop = "end_turn"
    for line in text.splitlines():
        cmd, _, rest = line.strip().partition(" ")
        if cmd == "say":
            say(sid, mid, rest)
        elif cmd == "msg":  # a new agent message (new messageId)
            mid = uuid.uuid4().hex
            say(sid, mid, rest)
        elif cmd == "tool":  # tool TITLE INPUT|OUTPUT
            title, _, io = rest.partition(" ")
            inp, _, outp = io.partition("|")
            tid = "tc-" + uuid.uuid4().hex[:6]
            update(sid, {"sessionUpdate": "tool_call", "toolCallId": tid, "title": title, "kind": "other",
                         "status": "in_progress", "rawInput": {"command": inp}})
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": "completed",
                         "content": [{"type": "content", "content": {"type": "text", "text": outp}}]})
        elif cmd == "think":
            update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": rest}})
        elif cmd == "recall":
            say(sid, mid, "history: " + json.dumps(h["prompts"]))
        elif cmd == "sleep":
            try:
                await asyncio.wait_for(s["cancel"].wait(), float(rest))
                stop = "cancelled"
                break
            except asyncio.TimeoutError:
                pass
        elif cmd == "hang":
            await asyncio.sleep(float(rest))
        elif cmd == "perm":
            tc = {"toolCallId": "tc-" + uuid.uuid4().hex[:6], "title": "do " + rest, "kind": rest}
            update(sid, {"sessionUpdate": "tool_call", **tc, "status": "pending"})
            r = await client_request("session/request_permission", {"sessionId": sid, "toolCall": tc, "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]})
            o = r.get("outcome", {})
            say(sid, mid, "permission: " + (o.get("optionId") if o.get("outcome") == "selected" else "cancelled"))
        elif cmd == "escalate":  # dsh-acp-style sandbox escalation ask: only a toolCallId, allow-once/reject-once
            title, _, io = rest.partition(" ")
            inp, _, outp = io.partition("|")
            tid = "tc-" + uuid.uuid4().hex[:6]
            update(sid, {"sessionUpdate": "tool_call", "toolCallId": tid, "title": title, "kind": "execute",
                         "status": "in_progress", "rawInput": {"command": inp}})
            r = await client_request("session/request_permission", {
                "sessionId": sid, "toolCall": {"toolCallId": tid},
                "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                            {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]})
            o = r.get("outcome", {})
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": "completed",
                         "content": [{"type": "content", "content": {"type": "text", "text": outp or "ok"}}]})
            say(sid, mid, "escalate: " + (o.get("optionId") if o.get("outcome") == "selected" else "cancelled"))
        elif cmd == "write":
            fn, _, t = rest.partition(" ")
            with open(os.path.join(s["cwd"], fn), "w") as f:
                f.write(t + "\n")
        elif cmd == "big":
            say(sid, mid, "x" * int(rest))
        elif cmd == "tool_big":
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "tc-big", "status": "completed",
                         "content": [{"type": "content", "content": {"type": "text", "text": "y" * int(rest)}}]})
        elif cmd == "crash":
            os._exit(3)
        elif cmd == "fail":
            raise RpcError("scripted failure")
        elif cmd == "env":
            say(sid, mid, f"{rest}={os.environ.get(rest, 'unset')}")
        elif cmd == "model":
            say(sid, mid, "model=" + h.get("model", "m-small"))
        if s["cancel"].is_set():
            stop = "cancelled"
            break
    h = load(sid)
    h["prompts"].append(text)
    save(sid, h)
    return {"stopReason": stop}


async def handle(msg):
    method, params, mid = msg["method"], msg.get("params") or {}, msg.get("id")
    try:
        if method == "initialize":
            res = {"protocolVersion": 1, "agentInfo": {"name": "fake-acp", "version": "0"},
                   "agentCapabilities": {"loadSession": "load" in CAPS,
                                         "sessionCapabilities": {k: {} for k in ("resume", "list", "close") if k in CAPS}}}
        elif method == "session/new":
            sid = str(uuid.uuid4())
            h = {"cwd": params["cwd"], "prompts": []}
            save(sid, h)
            live[sid] = {"cwd": params["cwd"]}
            update(sid, {"sessionUpdate": "available_commands_update", "availableCommands": []})
            res = {"sessionId": sid, "configOptions": config_options(h)}
        elif method in ("session/resume", "session/load"):
            sid = params["sessionId"]
            if not os.path.exists(path(sid)):
                raise RpcError("unknown session")
            h = load(sid)
            if h["cwd"] != params["cwd"]:
                raise RpcError("cwd mismatch")
            live[sid] = {"cwd": h["cwd"]}
            if method == "session/load":
                for p in h["prompts"]:
                    update(sid, {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": p}})
            res = {"configOptions": config_options(h)}
        elif method == "session/list":
            out = []
            for fn in sorted(os.listdir(STATE)):
                h = json.load(open(os.path.join(STATE, fn)))
                if params.get("cwd") in (None, h["cwd"]):
                    out.append({"sessionId": fn[:-5], "cwd": h["cwd"]})
            res = {"sessions": out}
        elif method == "session/close":
            live.pop(params["sessionId"], None)
            res = {}
        elif method == "session/set_config_option":
            sid = params["sessionId"]
            h = load(sid)
            h["model"] = params["value"]
            save(sid, h)
            res = {"configOptions": config_options(h)}
        elif method == "session/prompt":
            sid = params["sessionId"]
            if sid not in live:
                raise RpcError("session not loaded in this process")
            text = "".join(b.get("text", "") for b in params["prompt"])
            res = await run_prompt(sid, text)
        else:
            raise RpcError("method not found " + method)
        if mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "result": res})
    except RpcError as e:
        if mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)}})


async def main():
    print("[fake] booting: this line is not JSON-RPC", flush=True)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=2**24)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    tasks = set()
    while True:
        line = await reader.readline()
        if not line:
            break
        msg = json.loads(line)
        if "method" in msg and "id" in msg:
            if msg["method"] == "session/prompt" and msg["params"]["sessionId"] in live:
                live[msg["params"]["sessionId"]]["cancel"] = asyncio.Event()  # before a racing session/cancel
            t = asyncio.create_task(handle(msg))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
        elif "method" in msg:
            if msg["method"] == "session/cancel":
                s = live.get(msg["params"]["sessionId"])
                if s and s.get("cancel"):
                    s["cancel"].set()
        else:
            fut = pending.pop(msg["id"], None)
            if fut and not fut.done():
                fut.set_result(msg.get("result") or {})


asyncio.run(main())

"""Real end-to-end acceptance test with `dsh --profile acp` (costs a little DeepSeek credit).

    uv run python scripts/smoke_dsh.py

Starts its own gateway on a random port. Its state dir and the throwaway git repo live in a temp dir
under ~/Library/Application Support (like the real state dir, nothing under /tmp), deleted at the end.

Acceptance (session resumption):
  turn 1  create notes.md holding a random code name (default permissionPolicy read_only)
  turn 2  without tools: repeat the code name, and memorise a random secret word given only in chat
  stop    SIGTERM the gateway, kill any dsh it left, check no dsh of this run is alive
  start   a new gateway on the same state dir
  turn 3  same sessionId, without tools: name the file, the code name and the chat-only secret word;
          no tool calls allowed in this turn, so the answer can only come from resumed context
Extra:
  busy    a running `sleep` turn; a second prompt fails with SESSION_BUSY; cancel_prompt settles it
  crash   SIGKILL the gateway, restart, turn 4 still remembers the secret word
  close   close_session

Every tool call must return within 60 s. dsh's own copy of the session (~/.dsh/sessions/...) is
removed at the end too.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import secrets
import tempfile
import time
from pathlib import Path

import anyio
import httpx
from mcp import Client

ROOT = Path(__file__).resolve().parent.parent
TURN_TIMEOUT = 600
CODE = f"青松-{secrets.randbelow(9000) + 1000}"
WORD = f"琥珀{secrets.choice('甲乙丙丁戊己庚辛')}{secrets.randbelow(900) + 100}"
APP_SUPPORT = Path.home() / "Library" / "Application Support"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Gateway:
    def __init__(self, state: Path, port: int):
        self.state, self.port = state, port
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        log = open(self.state.parent / "gateway.log", "ab")
        self.proc = subprocess.Popen(
            ["uv", "run", "--quiet", "dot-gateway", "--port", str(self.port), "--state-dir", str(self.state)],
            cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{self.port}/", timeout=1)
                return
            except httpx.HTTPError:
                time.sleep(0.5)
        raise RuntimeError("gateway did not come up")

    def stop(self, sig=signal.SIGTERM) -> None:
        if self.proc and self.proc.poll() is None:
            # uv execs/forks the gateway in the same group; only the group leader tree is ours.
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()


def payload(r):
    if r.is_error:
        raise RuntimeError(r.content[0].text)
    sc = getattr(r, "structured_content", None)
    return sc if sc is not None else json.loads(r.content[0].text)


async def call(client, name, args, limit=60.0):
    t0 = time.monotonic()
    try:
        res = payload(await client.call_tool(name, args))
    finally:
        dt = time.monotonic() - t0
        assert dt < limit, f"{name} took {dt:.1f}s (limit {limit:.0f}s)"
    return res, dt


async def wait_turn(client, sid, cursor=0):
    """Long-poll get_events until the current turn is no longer running. Returns (prompt, events)."""
    events = []
    deadline = time.monotonic() + TURN_TIMEOUT
    while time.monotonic() < deadline:
        r, _ = await call(client, "get_events", {"sessionId": sid, "afterSequence": cursor, "waitMs": 25000,
                                                 "limit": 500}, limit=35)
        events += r["events"]
        cursor = r["nextSequence"]
        if r["prompt"] and r["prompt"]["state"] != "running" and not r["hasMore"]:
            return r["prompt"], events, cursor
    raise AssertionError(f"turn not done after {TURN_TIMEOUT}s")


def kinds(evs):
    return [e["kind"] for e in evs]


def pids_matching(pattern: str) -> list[str]:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout
    return out.split()


def make_repo(base: Path) -> Path:
    d = base / "repo"
    d.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "smoke", "GIT_AUTHOR_EMAIL": "smoke@local",
           "GIT_COMMITTER_NAME": "smoke", "GIT_COMMITTER_EMAIL": "smoke@local"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=d, check=True)
    (d / "README.md").write_text("# smoke\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=d, check=True, env=env)
    return d


def dsh_pids_in(cwd: Path) -> list[str]:
    """dsh ACP processes whose working directory is our temp repo (so other people's dsh are left alone)."""
    out = []
    for pid in pids_matching("dsh --profile acp"):
        r = subprocess.run(["lsof", "-a", "-d", "cwd", "-p", pid, "-Fn"], capture_output=True, text=True).stdout
        if any(l[1:] == str(cwd) or l[1:] == os.path.realpath(cwd) for l in r.splitlines() if l.startswith("n")):
            out.append(pid)
    return out


def show_turn(label, text, p, evs):
    tools = [e["update"].get("title") for e in evs if e["kind"] == "tool_call"]
    print(f"--- {label} [{p['promptId']}] state={p['state']} stop={p['stopReason']} tool_calls={tools}")
    print(f"    asked: {text}")
    print(f"    reply: {p['text'][:600]!r}")
    return tools


async def main() -> int:
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="dot-gateway-smoke-", dir=APP_SUPPORT))
    repo = make_repo(base)
    gw = Gateway(base / "state", free_port())
    checks: dict[str, bool] = {}
    print(f"code name={CODE} secret word={WORD} state dir={gw.state}")
    try:
        gw.start()
        async with Client(gw.url) as client:
            tools = sorted(t.name for t in (await client.list_tools()).tools)
            print("tools:", tools)

            # ---- turn 1
            t1 = (f"Create a file named notes.md in the current directory whose only content is the line "
                  f"`代号：{CODE}`. Do not create, change or commit anything else. Reply with one word: done.")
            s, dt = await call(client, "start_session", {"cwd": str(repo), "prompt": t1}, limit=30)
            sid = s["sessionId"]
            print(f"start_session(prompt) returned in {dt:.2f}s state={s.get('state')} sessionId={sid}")
            checks["start_session returns fast"] = dt < 30
            p1, ev1, cur = await wait_turn(client, sid)
            show_turn("turn 1", t1, p1, ev1)
            perms = [e for e in ev1 if e["kind"].startswith("permission")]
            notes = repo / "notes.md"
            print(f"    permission events={json.dumps(perms, ensure_ascii=False)[:800]} notes.md exists={notes.exists()}")
            if not notes.exists():
                t1b = "The write was refused before; it is allowed now. Create notes.md as asked, reply done."
                await call(client, "prompt", {"sessionId": sid, "permissionPolicy": "allow_all", "text": t1b})
                p1, ev1b, cur = await wait_turn(client, sid, cur)
                show_turn("turn 1b (allow_all)", t1b, p1, ev1b)
            content = notes.read_text() if notes.exists() else ""
            print(f"    notes.md content={content!r} gitSummary.statusShort={(p1.get('gitSummary') or {}).get('statusShort')!r}")
            checks["turn 1 wrote notes.md"] = CODE in content

            # ---- turn 2
            t2 = (f"Without using any tools: what code name did you write into the file? Also remember this "
                  f"secret word, which I only tell you here in chat and which is not in any file: {WORD}. "
                  f"Answer in one line with the code name, then 'OK'.")
            r, dt = await call(client, "prompt", {"sessionId": sid, "text": t2}, limit=10)
            print(f"prompt returned in {dt:.2f}s")
            p2, ev2, cur = await wait_turn(client, sid, cur)
            show_turn("turn 2", t2, p2, ev2)
            checks["turn 2 uses turn-1 context"] = CODE in p2["text"]
            old_pid = (await call(client, "get_session", {"sessionId": sid}))[0]["agentPid"]

        # ---- full stop
        gw.stop(signal.SIGTERM)
        print(f"gateway stopped (SIGTERM, exit={gw.proc.returncode}); dsh pid during turns 1-2 was {old_pid}")
        for pid in dsh_pids_in(repo):
            print("killing leftover dsh", pid)
            os.kill(int(pid), signal.SIGKILL)
        time.sleep(1)
        left = dsh_pids_in(repo)
        print("dsh processes of this run still alive:", left)
        checks["no dsh alive between gateways"] = not left
        rec = json.loads((gw.state / "sessions" / sid / "session.json").read_text())
        print("persisted mapping:", json.dumps({k: rec.get(k) for k in (
            "session_id", "acp_session_id", "agent_id", "cwd", "permission_mode", "permission_policy", "state")},
            ensure_ascii=False))

        # ---- restart, turn 3
        gw.start()
        async with Client(gw.url) as client:
            ls, _ = await call(client, "list_sessions", {})
            print("list_sessions after restart:", [(x["sessionId"], x["state"], x["live"]) for x in ls["sessions"]])
            t3 = ("Without using any tools, from our conversation only: which file did you create in the first "
                  "turn, what code name did it contain, and what secret word did I tell you in the second turn? "
                  "Answer in one line.")
            r, dt = await call(client, "prompt", {"sessionId": sid, "text": t3}, limit=60)
            print(f"prompt on the restarted gateway returned in {dt:.2f}s live={r.get('live')}")
            p3, ev3, cur = await wait_turn(client, sid, cur)
            t3_tools = show_turn("turn 3 (after restart)", t3, p3, ev3)
            att = [e for e in ev3 if e["kind"] in ("gateway_restarted", "session_attached")]
            info, _ = await call(client, "get_session", {"sessionId": sid})
            print(f"    attach events={json.dumps(att, ensure_ascii=False)} new dsh pid={info['agentPid']}")
            checks["turn 3 resumed via session/resume"] = any(
                e["kind"] == "session_attached" and e["update"].get("method") == "session/resume" for e in att)
            checks["turn 3 remembers turns 1-2 without tools"] = (
                "notes.md" in p3["text"] and CODE in p3["text"] and WORD in p3["text"] and not t3_tools)
            checks["turn 3 ran in a new dsh process"] = info["agentPid"] not in (None, old_pid)

            # ---- busy + cancel
            await call(client, "prompt", {"sessionId": sid, "permissionPolicy": "allow_all", "text":
                "Run the shell command `sleep 127` in the current directory and wait for it, then reply done."},
                limit=10)
            busy_err = ""
            try:
                await call(client, "prompt", {"sessionId": sid, "text": "say hi"}, limit=10)
            except RuntimeError as e:
                busy_err = str(e)
            print("second prompt while busy:", busy_err[:200])
            checks["SESSION_BUSY"] = "SESSION_BUSY" in busy_err
            t0 = time.monotonic()
            while time.monotonic() - t0 < 90 and not pids_matching("sleep 127"):
                await anyio.sleep(2)
            c, dt = await call(client, "cancel_prompt", {"sessionId": sid}, limit=15)
            if c["prompt"]["state"] == "running":
                c, _ = await call(client, "cancel_prompt", {"sessionId": sid, "force": True}, limit=20)
            pc, _, cur = await wait_turn(client, sid, cur)
            await anyio.sleep(2)
            print(f"cancel_prompt {dt:.2f}s -> {pc['state']} stop={pc['stopReason']} leftover sleep={pids_matching('sleep 127')}")
            checks["cancel settles"] = pc["state"] == "cancelled" and not pids_matching("sleep 127")

        # ---- crash, turn 4
        gw.stop(signal.SIGKILL)
        gw.start()
        async with Client(gw.url) as client:
            t4 = "Without using any tools: what was the secret word? Answer with the word only."
            await call(client, "prompt", {"sessionId": sid, "text": t4}, limit=60)
            p4, ev4, cur = await wait_turn(client, sid, cur)
            show_turn("turn 4 (after SIGKILL crash)", t4, p4, ev4)
            checks["turn 4 remembers after crash"] = WORD in p4["text"]
            cl, _ = await call(client, "close_session", {"sessionId": sid}, limit=60)
            checks["closed"] = cl.get("state") == "closed"
    finally:
        gw.stop()
        for pid in dsh_pids_in(repo):
            os.kill(int(pid), signal.SIGKILL)
        log = (base / "gateway.log").read_text(errors="replace") if (base / "gateway.log").exists() else ""
        print("gateway log (recover lines):", [l for l in log.splitlines() if "recovered" in l])
        shutil.rmtree(base, ignore_errors=True)
        print("temp dir removed:", not base.exists())
        # dsh keeps its own copy of the conversation in ~/.dsh/sessions/<encoded cwd>/; drop this run's.
        dsh_dirs = list((Path.home() / ".dsh" / "sessions").glob(f"*{base.name}*"))
        for d in dsh_dirs:
            shutil.rmtree(d, ignore_errors=True)
        print("dsh session store of this run removed:", [d.name for d in dsh_dirs])
    print("checks:", json.dumps(checks, ensure_ascii=False, indent=1))
    ok = bool(checks) and all(checks.values())
    print("SMOKE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(anyio.run(main))

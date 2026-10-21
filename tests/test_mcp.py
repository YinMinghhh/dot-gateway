"""MCP-level tests: in-process client and a real Streamable HTTP round trip, with the fake ACP agent."""

import json
import socket
import threading
import time

import anyio
import httpx
import pytest
import uvicorn
from mcp import Client

from dot_gateway.server import build_server, camelize, make_app

TOOLS = {"list_agents", "start_session", "prompt", "get_events", "list_sessions", "get_session", "load_session",
         "cancel_prompt", "set_config_option", "resolve_permission", "close_session", "get_tool_call"}


def payload(r):
    """Each result is exactly one compact JSON text block, without a duplicate structuredContent."""
    assert not r.is_error, r.content[0].text
    assert r.structured_content is None and len(r.content) == 1 and r.content[0].type == "text"
    text = r.content[0].text
    assert "\n" not in text and ": " not in text[:40]
    return json.loads(text)


async def exercise(client, cwd):
    assert {t.name for t in (await client.list_tools()).tools} == TOOLS
    agents = payload(await client.call_tool("list_agents", {}))
    assert "dsh" in [a["agentId"] for a in agents["agents"]]
    s = payload(await client.call_tool("start_session", {"cwd": cwd, "agentId": "fake",
                                                          "prompt": "say one\ntool bash echo|hi\nsleep 1"}))
    sid = s["sessionId"]
    busy = await client.call_tool("prompt", {"sessionId": sid, "text": "say two"})
    assert busy.is_error and "SESSION_BUSY" in busy.content[0].text
    cur = 0
    for _ in range(200):
        g = payload(await client.call_tool("get_events", {"sessionId": sid, "afterSequence": cur}))
        assert g["sessionId"] == sid
        cur = g["nextSequence"]
        if g["prompt"]["state"] != "running":
            break
        await anyio.sleep(0.05)
    assert g["prompt"]["state"] == "completed" and g["prompt"]["text"] == "one" and "events" not in g
    assert g["hasMore"] is False and g["prompt"]["permissionRequests"] == 0
    assert len(g["prompt"]["toolCallIds"]) == 1
    # includeToolOutput is forwarded through the MCP layer and camelized.
    ev = payload(await client.call_tool("get_events", {"sessionId": sid, "afterSequence": 0,
                                                       "includeText": True, "includeToolOutput": True}))
    assert ev["prompt"]["toolOutputs"][0]["output"] == "hi"
    again = payload(await client.call_tool("get_events", {"sessionId": sid, "afterSequence": cur}))
    assert "text" not in again["prompt"] and again["prompt"]["deliveredAtSequence"] == cur
    full = payload(await client.call_tool("get_session", {"sessionId": sid, "detail": "full"}))
    assert full["prompts"][-1]["text"] == "one" and full["configOptions"]
    msgs = payload(await client.call_tool("get_events", {"sessionId": sid, "view": "messages", "limit": 500}))
    assert any(e["kind"] == "message" for e in msgs["events"])
    p = payload(await client.call_tool("prompt", {"sessionId": sid, "text": "recall", "waitMs": 5000}))
    assert p["sessionId"] == sid and "say one" in p["prompt"]["text"]
    assert (p["permissionMode"], p["permissionPolicy"]) == ("low", "read_only") and "permissionNote" not in p
    lst = payload(await client.call_tool("list_sessions", {}))
    assert lst["sessions"][0]["sessionId"] == sid
    c = payload(await client.call_tool("close_session", {"sessionId": sid}))
    assert c == {"sessionId": sid, "state": "closed", "note": c["note"]}
    # operator: dsh-acp-style escalation asks carry only a toolCallId; the gateway enriches them and waits.
    op = payload(await client.call_tool("start_session", {"cwd": cwd, "agentId": "fake",
                                                          "permissionPolicy": "operator",
                                                          "prompt": "escalate bash echo|hi"}))
    assert op["permissionPolicy"] == "operator" and op["permissionNote"]
    pending = None
    for _ in range(200):
        g2 = payload(await client.call_tool("get_events", {"sessionId": op["sessionId"]}))
        pending = (g2.get("pendingPermissions") or [None])[0]
        if pending:
            break
        await anyio.sleep(0.05)
    assert pending and pending["toolCallId"].startswith("tc-")
    assert pending["tool"].startswith("bash: echo")
    payload(await client.call_tool("resolve_permission", {"sessionId": op["sessionId"],
                                                          "permissionId": pending["permissionId"],
                                                          "optionId": "allow-once"}))
    for _ in range(200):
        g3 = payload(await client.call_tool("get_events", {"sessionId": op["sessionId"]}))
        if g3["prompt"]["state"] != "running":
            break
        await anyio.sleep(0.05)
    assert g3["prompt"]["text"] == "escalate: allow-once"
    payload(await client.call_tool("close_session", {"sessionId": op["sessionId"]}))
    bad = await client.call_tool("get_events", {"sessionId": "nope"})
    assert bad.is_error and "SESSION_NOT_FOUND" in bad.content[0].text


@pytest.mark.anyio
async def test_in_process(manager, repo):
    async with Client(build_server(manager)) as client:
        await exercise(client, str(repo))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_streamable_http(make_manager, repo):
    m = make_manager()
    m.recover()
    m.config.port = _free_port()
    server = uvicorn.Server(uvicorn.Config(make_app(m), host="127.0.0.1", port=m.config.port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    try:
        async def run():
            async with Client(f"http://127.0.0.1:{m.config.port}/mcp") as client:
                await exercise(client, str(repo))
        anyio.run(run)
        base = f"http://127.0.0.1:{m.config.port}"
        for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp",
                     "/.well-known/oauth-authorization-server", "/.well-known/oauth-authorization-server/mcp",
                     "/.well-known/openid-configuration", "/", "/nope"):
            r = httpx.get(base + path, timeout=5)
            assert r.status_code == 404, path
            assert r.headers["content-type"].startswith("application/json"), path
            assert r.json() == {"error": "not_found"}, path
    finally:
        server.should_exit = True
        t.join(timeout=15)
    assert not t.is_alive() and not m._rt  # lifespan shutdown stopped agent processes


def test_camelize_keeps_acp_payloads():
    out = camelize({"session_id": "s", "events": [{"sequence": 1, "update": {"raw_input": {"file_path": "x"}}}],
                    "pending_permissions": [{"tool_call": {"a_b": 1}}], "git_summary": {"status_short": ""}})
    assert out == {"sessionId": "s", "events": [{"sequence": 1, "update": {"raw_input": {"file_path": "x"}}}],
                   "pendingPermissions": [{"toolCall": {"a_b": 1}}], "gitSummary": {"statusShort": ""}}


@pytest.mark.anyio
async def test_structured_output_switch(manager):
    """Default is text-only; the deployment switch adds the same object as structuredContent."""
    manager.config.structured_output = True
    try:
        async with Client(build_server(manager)) as client:
            r = await client.call_tool("list_agents", {})
            assert not r.is_error and r.structured_content is not None
            assert len(r.content) == 1 and r.content[0].type == "text"
            assert json.loads(r.content[0].text) == r.structured_content
    finally:
        manager.config.structured_output = False

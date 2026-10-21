import time
import json
import os
import signal

import anyio
import pytest

from conftest import alive, wait_done, wait_for
from dot_gateway.agents import AgentError, load_registry
from dot_gateway.config import Config
from dot_gateway.sessions import SessionError

pytestmark = pytest.mark.anyio


def kinds(events):
    return [e["kind"] for e in events]


async def all_events(m, sid, **kw):
    kw.setdefault("view", "full")
    out, cur = [], 0
    while True:
        r = await m.get_events(sid, after_sequence=cur, **kw)
        out += r["events"]
        if r["next_sequence"] == cur or not r["has_more"]:
            return out, r
        cur = r["next_sequence"]


async def test_two_turns_keep_context_and_journal(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", prompt="say hello\nwrite a.txt A")
    sid = s["session_id"]
    assert s["prompt_id"] and s["permission_mode"] == "low" and set(s) <= {
        "session_id", "agent_id", "cwd", "state", "live", "permission_mode", "permission_policy", "prompt_id"}
    p1 = await wait_done(manager, sid)
    assert p1["state"] == "completed" and p1["stop_reason"] == "end_turn" and p1["text"] == "hello"
    assert "?? a.txt" in p1["git_summary"]["status_short"]
    evs, last = await all_events(manager, sid)
    k = kinds(evs)
    assert k[0] == "session_started" and "available_commands_update" in k
    assert k.index("prompt_started") < k.index("agent_message_chunk") < k.index("prompt_finished")
    assert [e["sequence"] for e in evs] == list(range(1, len(evs) + 1))
    assert last["session_id"] == sid and last["state"] == "idle"
    # The non-JSON stdout line went to the agent log, not into the protocol.
    assert "not JSON-RPC" in (manager.root / sid / "agent-stderr.log").read_text()
    # Turn 2 sees turn 1.
    r = await manager.prompt(sid, "recall")
    assert r["session_id"] == sid and r["prompt"]["state"] == "running"
    p2 = await wait_done(manager, sid)
    assert "say hello" in p2["text"] and p2["prompt_id"] == r["prompt_id"]
    assert (await manager.get_session(sid))["prompt_count"] == 2


async def test_busy_cancel_and_continue(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", prompt="sleep 30")
    sid = s["session_id"]
    with pytest.raises(SessionError) as e:
        await manager.prompt(sid, "say again")
    assert e.value.code == "SESSION_BUSY" and s["prompt_id"] in str(e.value)
    r = await manager.cancel_prompt(sid)
    assert r["prompt"]["state"] == "cancelled" and r["prompt"]["stop_reason"] == "cancelled"
    assert r["state"] == "idle" and (await manager.get_session(sid))["live"]
    assert "no prompt is running" in (await manager.cancel_prompt(sid))["note"]
    await manager.prompt(sid, "say after")
    assert (await wait_done(manager, sid))["text"] == "after"


async def test_force_cancel_kills_agent_and_session_resumes(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", prompt="say first")
    sid = s["session_id"]
    await wait_done(manager, sid)
    await manager.prompt(sid, "hang 60")
    pid = (await manager.get_session(sid, detail="full"))["agent_pid"]
    r = await manager.cancel_prompt(sid, force=True)
    assert r["prompt"]["state"] == "cancelled" and not (await manager.get_session(sid))["live"]
    assert not alive(pid)
    r = await manager.prompt(sid, "recall")
    p = await wait_done(manager, sid)
    assert p["resumed"] and "say first" in p["text"]


@pytest.mark.parametrize("policy,kind,expect", [
    ("read_only", "read", "allow"), ("read_only", "edit", "reject"), ("read_only", "other", "reject"),
    ("allow_all", "execute", "allow"), ("deny_all", "read", "reject"),
])
async def test_permission_policies(manager, repo, policy, kind, expect):
    s = await manager.start_session(str(repo), agent_id="fake", permission_policy=policy, prompt=f"perm {kind}")
    p = await wait_done(manager, s["session_id"])
    assert p["text"] == f"permission: {expect}"
    evs, _ = await all_events(manager, s["session_id"])
    req = next(e for e in evs if e["kind"] == "permission_request")
    res = next(e for e in evs if e["kind"] == "permission_resolved")
    assert req["update"]["policy"] == policy and res["update"]["by"] == "policy"


async def test_permission_policy_override_per_prompt(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake")
    sid = s["session_id"]
    await manager.prompt(sid, "perm edit", permission_policy="allow_all")
    assert (await wait_done(manager, sid))["text"] == "permission: allow"
    await manager.prompt(sid, "perm edit")
    assert (await wait_done(manager, sid))["text"] == "permission: reject"


async def test_operator_permission_pending_then_resolved(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", permission_policy="operator", prompt="perm edit")
    sid = s["session_id"]

    async def pending():
        r = await manager.get_events(sid)
        return r.get("pending_permissions")

    pend = await wait_for(pending)
    perm = pend[0]
    assert "do edit" in perm["tool"] and {o["optionId"] for o in perm["options"]} == {"allow", "reject"}
    assert (await manager.get_session(sid))["pending_permissions"][0]["permission_id"] == perm["permission_id"]
    with pytest.raises(SessionError):
        manager.resolve_permission(sid, perm["permission_id"], "nope")
    r = manager.resolve_permission(sid, perm["permission_id"], "allow")
    assert r["resolved"]["option_id"] == "allow"
    p = await wait_done(manager, sid)
    assert p["text"] == "permission: allow"
    assert "pending_permissions" not in (await manager.get_events(sid))
    evs, _ = await all_events(manager, sid)
    assert next(e for e in evs if e["kind"] == "permission_resolved")["update"]["by"] == "caller"


async def test_operator_pending_cancelled_by_cancel_prompt(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", permission_policy="operator", prompt="perm edit\nsleep 30")
    sid = s["session_id"]
    await wait_for(lambda: _pending(manager, sid))
    r = await manager.cancel_prompt(sid)
    assert r["prompt"]["state"] == "cancelled" and "text" not in r["prompt"]


async def _pending(m, sid):
    return (await m.get_events(sid)).get("pending_permissions")


async def test_permission_mode_per_prompt_restarts_agent(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", prompt="env FAKE_MODE")
    sid = s["session_id"]
    assert (await wait_done(manager, sid))["text"] == "FAKE_MODE=low"
    await manager.prompt(sid, "env FAKE_MODE\nrecall", permission_mode="high")
    p = await wait_done(manager, sid)
    assert p["text"].startswith("FAKE_MODE=high") and "env FAKE_MODE" in p["text"] and p["resumed"]
    assert p["permission_mode"] == "high"
    await manager.prompt(sid, "env FAKE_MODE")
    assert (await wait_done(manager, sid))["text"] == "FAKE_MODE=low"
    evs, _ = await all_events(manager, sid)
    assert kinds(evs).count("agent_restart") == 2
    with pytest.raises(AgentError):
        await manager.prompt(sid, "x", permission_mode="root")


async def test_gateway_restart_recovers_sessions(make_manager, repo):
    a = make_manager()
    a.recover()
    s = await a.start_session(str(repo), agent_id="fake", prompt="say one")
    sid = s["session_id"]
    await wait_done(a, sid)
    await a.prompt(sid, "sleep 60")
    pid = (await a.get_session(sid, detail="full"))["agent_pid"]
    assert alive(pid)
    # Simulated crash: manager A stops writing state; its agent process is left behind.
    a._save = lambda rec: None
    a._emit = lambda *x, **k: None
    b = make_manager()
    counts = b.recover()
    assert counts == {"sessions": 1, "interrupted_prompts": 1, "orphans_killed": 1}
    assert not alive(pid)
    lst = await b.list_sessions()
    assert lst["sessions"][0]["session_id"] == sid and lst["sessions"][0]["state"] == "idle"
    assert lst["sessions"][0]["last_prompt"]["state"] == "failed"
    g = await b.get_session(sid)
    assert "interrupted" in g["prompts"][-1]["error"]["message"] and not g["live"]
    r = await b.prompt(sid, "recall")
    assert (await b.get_session(sid))["live"] and r["prompt"]["resumed"]
    p = await wait_done(b, sid)
    assert p["resumed"] and "say one" in p["text"]
    evs, _ = await all_events(b, sid)
    assert "gateway_restarted" in kinds(evs) and "session_attached" in kinds(evs)
    await b.shutdown()
    await a.shutdown()


async def test_close_and_load_with_resume(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", prompt="say x")
    sid = s["session_id"]
    await wait_done(manager, sid)
    pid = (await manager.get_session(sid, detail="full"))["agent_pid"]
    c = await manager.close_session(sid)
    assert c["state"] == "closed" and "load_session" in c["note"] and set(c) == {"session_id", "state", "note"}
    assert not (await manager.get_session(sid))["live"]
    assert not alive(pid)
    with pytest.raises(SessionError) as e:
        await manager.prompt(sid, "recall")
    assert e.value.code == "SESSION_CLOSED"
    r = await manager.load_session(sid)
    assert r["state"] == "idle" and r["live"]
    assert "already live" in (await manager.load_session(sid))["note"]
    await manager.prompt(sid, "recall")
    assert "say x" in (await wait_done(manager, sid))["text"]


async def test_load_falls_back_to_session_load_with_replay(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake_load", prompt="say x")
    sid = s["session_id"]
    await wait_done(manager, sid)
    await manager.close_session(sid)
    await manager.load_session(sid)
    evs, _ = await all_events(manager, sid)
    att = [e for e in evs if e["kind"] == "session_attached"][-1]
    assert att["update"]["method"] == "session/load"
    replay = [e for e in evs if e["kind"] == "user_message_chunk"]
    assert replay and replay[0]["update"]["replay"] is True


async def test_agent_without_resume_or_load(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake_none", prompt="say x")
    sid = s["session_id"]
    await wait_done(manager, sid)
    await manager.close_session(sid)
    with pytest.raises(SessionError) as e:
        await manager.load_session(sid)
    assert e.value.code == "NOT_RESUMABLE"


async def test_adopt_agent_session_listed_by_agent(make_manager, repo, tmp_path):
    a = make_manager()
    s = await a.start_session(str(repo), agent_id="fake", prompt="say adopted")
    sid = s["session_id"]
    await wait_done(a, sid)
    await a.close_session(sid)
    await a.shutdown()
    b = make_manager(tmp_path / "other-state")
    lst = await b.list_sessions(source="agent", agent_id="fake", cwd=str(repo))
    entry = next(x for x in lst["sessions"] if x["session_id"] == sid)
    assert entry["known_to_gateway"] is False
    with pytest.raises(SessionError):
        await b.load_session(sid)  # needs agent_id + cwd
    r = await b.load_session(sid, agent_id="fake", cwd=entry["cwd"])
    assert (await b.get_session(sid))["origin"] == "loaded" and r["live"]
    await b.prompt(sid, "recall")
    assert "say adopted" in (await wait_done(b, sid))["text"]
    with pytest.raises(SessionError) as e:
        await b.load_session("00000000-dead", agent_id="fake", cwd=str(repo))
    assert e.value.code == "ATTACH_FAILED"
    assert not (b.root / "00000000-dead").exists()
    await b.shutdown()


async def test_agent_crash_mid_prompt(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", prompt="say before\ncrash")
    sid = s["session_id"]
    p = await wait_done(manager, sid)
    assert p["state"] == "failed" and "exited" in p["error"]["message"]
    assert not (await manager.get_session(sid))["live"]
    evs, _ = await all_events(manager, sid)
    assert "agent_exited" in kinds(evs)
    await manager.prompt(sid, "recall")
    assert (await wait_done(manager, sid))["resumed"]


async def test_agent_error_response(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", prompt="fail")
    p = await wait_done(manager, s["session_id"])
    assert p["state"] == "failed" and "scripted failure" in p["error"]["message"]
    assert (await manager.get_session(s["session_id"]))["live"]


async def test_config_option_by_name_persists_across_reattach(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", config={"model": "Big"})
    sid = s["session_id"]
    await manager.prompt(sid, "model")
    assert (await wait_done(manager, sid))["text"] == "model=m-big"
    with pytest.raises(SessionError):
        await manager.set_config_option(sid, "model", "Huge")
    with pytest.raises(SessionError):
        await manager.set_config_option(sid, "nope", "x")
    r = await manager.set_config_option(sid, "model", "m-small")
    assert r["current_value"] == "m-small" and r["config_values"] == {"model": "m-small"}
    await manager.cancel_prompt(sid)
    await manager.prompt(sid, "model", permission_mode="high")  # restart + resume, value re-applied
    assert (await wait_done(manager, sid))["text"] == "model=m-small"


async def test_events_paging_and_size_caps(manager, repo):
    script = "\n".join([f"say line {i}" for i in range(300)] + ["think hidden", "big 300000", "tool_big 100000"])
    s = await manager.start_session(str(repo), agent_id="fake", prompt=script)
    sid = s["session_id"]
    await wait_done(manager, sid)
    total = (await manager.get_events(sid))["latest_sequence"]
    cur, seen = 0, []
    while True:
        r = await manager.get_events(sid, after_sequence=cur, limit=37, include_thoughts=True, view="full")
        seen += [e["sequence"] for e in r["events"]]
        assert len(r["events"]) <= 37
        if not r["has_more"]:
            break
        cur = r["next_sequence"]
    assert seen == list(range(1, total + 1))
    # Thoughts hidden by default, cursor still advances past them.
    evs, _ = await all_events(manager, sid)
    assert "agent_thought_chunk" not in kinds(evs) and len(evs) == total - 1
    # Clipping: every stored event <= 32 KiB; big strings marked truncated.
    big = [e for e in evs if e.get("truncated")]
    assert len(big) >= 2 and all(len(json.dumps(e)) <= 33 * 1024 for e in evs)
    # Byte cap: tiny max_bytes still returns at least one event.
    r = await manager.get_events(sid, max_bytes=1, limit=500, view="full")  # clamped to 1 KiB, never an empty page
    assert 1 <= len(r["events"]) and sum(len(json.dumps(e)) for e in r["events"][1:]) <= 1024
    r = await manager.get_events(sid, max_bytes=10**9, limit=10**6, reply_bytes=10**9, view="full", include_text=True)
    assert len(json.dumps(r).encode()) < 1.5 * 1024 * 1024
    assert r["prompt"]["text_truncated"] and r["prompt"]["text_bytes"] >= 256 * 1024  # stored reply cap
    r = await manager.get_events(sid, reply_bytes=1000, include_text=True)
    assert len(r["prompt"]["text"].encode()) <= 1000


async def test_get_events_wait(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", prompt="sleep 0.5\nsay late")
    sid = s["session_id"]
    cur = (await manager.get_events(sid, view="messages"))["next_sequence"]
    t0 = anyio.current_time()
    r = await manager.get_events(sid, after_sequence=cur, wait_ms=5000, view="messages")
    assert r["events"] and anyio.current_time() - t0 < 4


async def test_final_view_long_polls_until_turn_end(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", prompt="say a\nsleep 0.6\nsay b\nsleep 0.6\nsay c")
    sid = s["session_id"]
    t0 = anyio.current_time()
    r = await manager.get_events(sid, wait_ms=10000)
    assert r["prompt"]["state"] == "completed" and r["prompt"]["text"] == "abc"
    assert anyio.current_time() - t0 >= 1.0 and "events" not in r
    t0 = anyio.current_time()
    r = await manager.get_events(sid, wait_ms=300, after_sequence=0, limit=1)
    assert anyio.current_time() - t0 < 0.3  # nothing running: returns at once
    s2 = await manager.start_session(str(repo), agent_id="fake", prompt="sleep 5")
    t0 = anyio.current_time()
    r = await manager.get_events(s2["session_id"], wait_ms=400)
    assert r["prompt"]["state"] == "running" and 0.35 <= anyio.current_time() - t0 < 2
    assert set(r["prompt"]["progress"]) == {"tool_calls", "last_tool", "messages"}
    await manager.cancel_prompt(s2["session_id"])


async def test_validation(manager, repo):
    with pytest.raises(SessionError):
        await manager.start_session("relative", agent_id="fake")
    with pytest.raises(SessionError):
        await manager.start_session(str(repo / "missing"), agent_id="fake")
    with pytest.raises(AgentError):
        await manager.start_session(str(repo), agent_id="pi")
    with pytest.raises(SessionError):
        await manager.start_session(str(repo), agent_id="fake", permission_policy="yolo")
    with pytest.raises(SessionError) as e:
        await manager.get_events("../etc")
    assert e.value.code == "BAD_ARGUMENT"
    with pytest.raises(SessionError) as e:
        await manager.prompt("no-such-session", "hi")
    assert e.value.code == "SESSION_NOT_FOUND"
    s = await manager.start_session(str(repo), agent_id="fake")
    with pytest.raises(SessionError):
        await manager.prompt(s["session_id"], "   ")


async def test_shutdown_stops_agents_and_marks_running_prompt(make_manager, repo):
    m = make_manager()
    s = await m.start_session(str(repo), agent_id="fake", prompt="sleep 60")
    sid = s["session_id"]
    pid = (await m.get_session(sid, detail="full"))["agent_pid"]
    await m.shutdown()
    assert not alive(pid)
    rec = json.loads((m.root / sid / "session.json").read_text())
    assert rec["state"] == "idle" and rec["prompts"][-1]["state"] == "failed"
    assert "gateway stopped" in rec["prompts"][-1]["error"]["message"]


def test_registry_from_config_file(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "defaults": {"permission_policy": "allow_all", "dsh_permission_mode": "read-only"},
        "agents": {"pi": {"command": "/opt/homebrew/bin/pi-acp", "description": "pi via pi-acp"},
                   "dsh": {"env": {"X": "1"}}}}))
    c = Config(state_dir=tmp_path / "s", config_file=cfg)
    c.apply_file_defaults()
    assert c.default_permission_policy == "allow_all"
    reg = load_registry(c)
    assert reg.ids() == ["dsh", "pi"]
    dsh = reg.get("dsh")
    assert dsh.args == ["--profile", "acp"] and dsh.default_permission_mode == "read-only" and dsh.env == {"X": "1"}
    argv, env = dsh.launch("danger-full-access", {})
    assert argv == ["/opt/homebrew/bin/dsh", "--profile", "acp"] and env["DSH_PERMISSION_MODE"] == "danger-full-access"
    assert reg.get("pi").permission_modes == {} and reg.get("pi").resolve_mode(None) is None
    with pytest.raises(AgentError):
        reg.get("pi").resolve_mode("high")
    cfg.write_text(json.dumps({"agents": {"bad": {"args": []}}}))
    with pytest.raises(AgentError):
        load_registry(c)


def test_builtin_dsh_defaults(tmp_path):
    # Isolate from the host's real <state_dir>/config.json (deployments may override dsh).
    c = Config(state_dir=tmp_path / "state", config_file=tmp_path / "missing-config.json")
    d = load_registry(c).get("dsh")
    assert d.default_permission_mode == "workspace-write"
    assert sorted(d.permission_modes) == ["danger-full-access", "read-only", "workspace-write"]
    assert c.default_permission_policy == "read_only"


async def test_prompt_wait_returns_running_or_done(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake")
    sid = s["session_id"]
    t0 = time.monotonic()
    r = await manager.prompt(sid, "sleep 30", wait_ms=300)
    assert r["prompt"]["state"] == "running" and time.monotonic() - t0 < 3
    await manager.cancel_prompt(sid)
    r = await manager.prompt(sid, "say quick", wait_ms=5000)
    assert r["prompt"]["state"] == "completed" and r["prompt"]["text"] == "quick"


async def test_update_keeps_session_update_field(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", prompt="say a")
    await wait_done(manager, s["session_id"])
    evs, _ = await all_events(manager, s["session_id"])
    chunk = next(e for e in evs if e["kind"] == "agent_message_chunk")
    assert chunk["update"]["sessionUpdate"] == "agent_message_chunk" and chunk["promptId"]


async def test_resume_after_full_stop_like_reboot(make_manager, repo):
    """Acceptance shape: turn 1, turn 2, gateway fully stopped and no agent left, new gateway, turn 3 same id."""
    a = make_manager()
    a.recover()
    s = await a.start_session(str(repo), agent_id="fake", prompt="say codename blue-whale",
                              permission_policy="allow_all")
    sid = s["session_id"]
    assert (await wait_done(a, sid))["state"] == "completed"
    await a.prompt(sid, "recall")
    p2 = await wait_done(a, sid)
    assert "codename blue-whale" in p2["text"]
    pid = (await a.get_session(sid, detail="full"))["agent_pid"]
    await a.shutdown()
    assert not alive(pid) and not a._rt
    rec = json.loads((a.root / sid / "session.json").read_text())
    assert rec["acp_session_id"] == sid and rec["agent_id"] == "fake" and rec["cwd"] == os.path.realpath(repo)
    assert rec["permission_policy"] == "allow_all" and rec["state"] == "idle"
    b = make_manager()  # only the state dir and the agent's own store survive
    assert b.recover()["sessions"] == 1
    r = await b.prompt(sid, "recall")
    p3 = await wait_done(b, sid)
    assert p3["resumed"] and "codename blue-whale" in p3["text"] and "recall" in p3["text"]
    evs, _ = await all_events(b, sid)
    att = [e for e in evs if e["kind"] == "session_attached"]
    assert att[-1]["update"]["method"] == "session/resume"
    assert (await b.get_session(sid))["permission_policy"] == "allow_all"
    await b.shutdown()


async def test_final_text_is_last_message_and_delivered_once(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake",
                                    prompt="say I will look around first.\ntool bash ls -la|a.txt b.txt\nmsg Final: two files.")
    sid = s["session_id"]
    r = await manager.get_events(sid, wait_ms=10000)
    p = r["prompt"]
    assert p["state"] == "completed" and p["text"] == "Final: two files."  # not the in-between remark
    assert p["tool_calls"] == ["bash: ls -la → completed"] and "events" not in r
    assert len(p["tool_call_ids"]) == 1 and p["tool_call_ids"][0].startswith("tc-")
    assert p["permission_requests"] == 0
    end = r["next_sequence"]
    # Delivered once: a later poll from the new cursor gets no text, only its size and where it was delivered.
    r2 = await manager.get_events(sid, after_sequence=end)
    assert "text" not in r2["prompt"] and r2["prompt"]["text_bytes"] == len("Final: two files.")
    assert r2["prompt"]["delivered_at_sequence"] == end
    # 'final' never republishes on a repeated poll; recover it explicitly with includeText/get_session.
    assert "text" not in (await manager.get_events(sid, after_sequence=0))["prompt"]
    assert (await manager.get_events(sid, after_sequence=end, include_text=True))["prompt"]["text"] == "Final: two files."
    full = await manager.get_session(sid, detail="full")
    assert full["prompts"][-1]["text"] == "Final: two files." and full["prompts"][-1]["tool_calls"][0]["line"].startswith("bash")
    summ = await manager.get_session(sid)
    assert "text" not in summ["prompts"][-1] and "config_options" not in summ and "prompt" not in summ
    lst = await manager.list_sessions()
    assert "text" not in json.dumps(lst["sessions"][0]["last_prompt"])
    # Persisted across a gateway restart.
    rec = json.loads((manager.root / sid / "session.json").read_text())
    assert rec["prompts"][-1]["delivered_at_sequence"] == end and rec["prompts"][-1]["reply_text"] == "Final: two files."


async def test_prompt_wait_delivers_text_once(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake")
    sid = s["session_id"]
    r = await manager.prompt(sid, "say quick", wait_ms=5000)
    assert r["prompt"]["text"] == "quick"
    g = await manager.get_events(sid, after_sequence=(await manager.get_session(sid))["latest_sequence"])
    assert "text" not in g["prompt"] and g["prompt"]["delivered_at_sequence"]
    # prompt(waitMs) has no event cursor to retry: recover with includeText (or get_session detail=full)
    assert "text" not in (await manager.get_events(sid, after_sequence=0))["prompt"]
    assert (await manager.get_events(sid, include_text=True))["prompt"]["text"] == "quick"


async def test_messages_view_coalesces_and_summarizes_tools(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake",
                                    prompt="say part1 \nsay part2\nthink secret\ntool grep TODO src|x.py:1: TODO\nmsg done")
    sid = s["session_id"]
    await manager.get_events(sid, wait_ms=10000)  # final view: the text is delivered here
    r = await manager.get_events(sid, view="messages", limit=500)
    items = r["events"]
    msgs = [e for e in items if e["kind"] == "message"]
    assert msgs[0]["text"] == "part1part2"  # two chunks of one message merged
    assert not any(e["kind"] == "thought" for e in items)
    tools = [e for e in items if e["kind"] == "tool_call"]
    assert len(tools) == 1 and tools[0]["line"] == "grep: TODO src → completed" and "content" not in tools[0]
    assert not any(e["kind"] in ("usage_update", "available_commands_update") for e in items)
    # The page covers the end of the turn, so prompt.text carries the final message and it is not repeated.
    assert r["prompt"]["text"] == "done" and msgs[-1].get("textInPrompt") and "text" not in msgs[-1]
    started = next(e for e in items if e["kind"] == "prompt_started")
    assert started["textPreview"].startswith("say part1")
    r = await manager.get_events(sid, view="messages", include_thoughts=True, limit=500)
    assert any(e["kind"] == "thought" and e["text"] == "secret" for e in r["events"])
    # messages/full page events; final never does (nextSequence is just the latest journal position).
    a = await manager.get_events(sid, view="full", limit=3)
    b = await manager.get_events(sid, view="messages", limit=3)
    c = await manager.get_events(sid, view="final", limit=3)
    assert a["next_sequence"] == b["next_sequence"] and a["has_more"] == b["has_more"] is True
    assert c["next_sequence"] == c["latest_sequence"] and c["has_more"] is False and "events" not in c
    with pytest.raises(SessionError):
        await manager.get_events(sid, view="verbose")


async def test_prompt_started_stores_preview_only(manager, repo):
    long = "say x\n" + "y" * 5000
    s = await manager.start_session(str(repo), agent_id="fake", prompt=long)
    sid = s["session_id"]
    await wait_done(manager, sid)
    evs, _ = await all_events(manager, sid)
    st = next(e for e in evs if e["kind"] == "prompt_started")["update"]
    assert len(st["textPreview"]) == 200 and st["textChars"] == len(long) and "text" not in st
    rec = json.loads((manager.root / sid / "session.json").read_text())
    assert len(rec["prompts"][-1]["text_preview"]) == 200


async def test_get_tool_call_full_record_paged(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", prompt="tool_big 100000\nsay ok")
    sid = s["session_id"]
    await wait_done(manager, sid)
    evs, _ = await all_events(manager, sid)
    ev = next(e for e in evs if e["kind"] == "tool_call_update")
    assert ev.get("truncated")  # the journal is clipped at 8 KiB per string ...
    out, off = "", 0
    while True:  # ... but get_tool_call has the full output
        r = await manager.get_tool_call(sid, "tc-big", offset=off, max_bytes=30000)
        out += r["output"]
        off = r["next_offset"]
        if not r["has_more"]:
            break
    assert out == "y" * 100000 and r["output_bytes"] == 100000 and r["status"] == "completed"
    s2 = await manager.start_session(str(repo), agent_id="fake", prompt="tool bash cat README|hi")
    await wait_done(manager, s2["session_id"])
    tid = (await manager.get_session(s2["session_id"], detail="full"))["prompts"][-1]["tool_calls"][0]["tool_call_id"]
    r = await manager.get_tool_call(s2["session_id"], tid)
    assert r["title"] == "bash" and r["input"] == {"command": "cat README"} and r["output"] == "hi"
    with pytest.raises(SessionError) as e:
        await manager.get_tool_call(sid, "nope")
    assert e.value.code == "NOT_FOUND"


async def test_final_view_never_pages_and_delivers_text_once(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", prompt="msg one\ntool bash echo|hi\nmsg done")
    sid = s["session_id"]
    r = await manager.get_events(sid, wait_ms=10000)
    p = r["prompt"]
    assert p["state"] == "completed" and p["text"] == "done"
    assert r["has_more"] is False and r["next_sequence"] == r["latest_sequence"] and "events" not in r
    # Advancing the cursor must not repeat the reply, even with a tiny page limit.
    r2 = await manager.get_events(sid, after_sequence=r["next_sequence"], limit=1)
    assert "text" not in r2["prompt"] and r2["prompt"]["delivered_at_sequence"] == r["next_sequence"]
    assert r2["has_more"] is False
    # A repeated final poll (even with after_sequence=0) must not duplicate the reply, but evidence is
    # still available; includeText re-publishes it explicitly.
    r3 = await manager.get_events(sid, after_sequence=0, include_tool_output=True)
    assert "text" not in r3["prompt"] and r3["prompt"]["tool_outputs"][0]["output"] == "hi"
    p3 = (await manager.get_events(sid, after_sequence=0, include_text=True,
                                   include_tool_output=True))["prompt"]
    assert p3["text"] == "done" and p3["permission_requests"] == 0
    assert len(p3["tool_call_ids"]) == 1 and p3["tool_call_ids"][0].startswith("tc-")
    out = p3["tool_outputs"]
    assert out[0]["tool_call_id"] == p3["tool_call_ids"][0] and out[0]["output"] == "hi"
    assert out[0]["output_bytes"] == 2 and out[0]["truncated"] is False
    # includeText and get_session(detail="full") always recover it.
    assert (await manager.get_events(sid, after_sequence=r["next_sequence"],
                                     include_text=True))["prompt"]["text"] == "done"
    assert (await manager.get_session(sid, detail="full"))["prompts"][-1]["text"] == "done"


async def test_event_pages_deliver_text_only_on_the_page_covering_turn_end(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake",
                                    prompt="say a\nsay b\nsay c\ntool echo hi|hi\nmsg final")
    sid = s["session_id"]
    for _ in range(200):  # wait via get_session so the final view does not deliver the text
        lp = (await manager.get_session(sid))["prompts"][-1]
        if lp["state"] != "running":
            break
        await anyio.sleep(0.02)
    pages, cursors, cur = [], [], 0
    while True:
        cursors.append(cur)
        r = await manager.get_events(sid, after_sequence=cur, view="full", limit=2)
        pages.append(r)
        if not r["has_more"]:
            break
        cur = r["next_sequence"]
    idx = next(i for i, r in enumerate(pages) if any(e["kind"] == "prompt_finished" for e in r["events"]))
    assert pages[idx]["prompt"]["text"] == "final"
    assert sum("text" in r["prompt"] for r in pages) == 1  # only the page covering the turn end
    # Lost response: retrying the exact page that delivered it republishes the text (event views only).
    retry = await manager.get_events(sid, after_sequence=cursors[idx], view="full", limit=2)
    assert retry["prompt"]["text"] == "final"
    last = pages[-1]
    after = await manager.get_events(sid, after_sequence=last["next_sequence"], view="full")
    assert "text" not in after["prompt"] and after["prompt"]["delivered_at_sequence"] is not None


async def test_permission_requests_count_in_turn_view(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", prompt="perm edit",
                                    permission_policy="allow_all")
    r = await manager.get_events(s["session_id"], wait_ms=10000)
    assert r["prompt"]["state"] == "completed" and r["prompt"]["permission_requests"] == 1
    s2 = await manager.start_session(str(repo), agent_id="fake", prompt="say hi")
    r2 = await manager.get_events(s2["session_id"], wait_ms=10000)
    assert r2["prompt"]["permission_requests"] == 0


async def test_operator_note_and_escalation_request_enrichment(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake", permission_policy="operator",
                                    prompt="escalate bash touch /tmp/nope|ok")
    sid = s["session_id"]
    assert s["permission_policy"] == "operator" and "permission_note" in s
    pend = await wait_for(lambda: _pending(manager, sid))
    perm = pend[0]
    assert perm["tool_call_id"].startswith("tc-")
    assert perm["tool"].startswith("bash: touch /tmp/nope") and "→ in_progress" in perm["tool"]
    assert {o["optionId"] for o in perm["options"]} == {"allow-once", "reject-once"}
    r = manager.resolve_permission(sid, perm["permission_id"], "allow-once")
    assert r["resolved"]["option_id"] == "allow-once"
    p = await wait_done(manager, sid)
    assert p["text"] == "escalate: allow-once"
    evs, _ = await all_events(manager, sid)
    req = next(e for e in evs if e["kind"] == "permission_request")["update"]
    assert req["toolCall"]["title"] == "bash" and req["toolCall"]["rawInput"]["command"] == "touch /tmp/nope"


async def test_prompt_response_echoes_effective_permission_config(manager, repo):
    s = await manager.start_session(str(repo), agent_id="fake")
    sid = s["session_id"]
    assert "permission_note" not in s
    r = await manager.prompt(sid, "say run", permission_mode="high", permission_policy="deny_all")
    assert (r["permission_mode"], r["permission_policy"]) == ("high", "deny_all")
    assert "permission_note" not in r
    assert (await wait_done(manager, sid))["text"] == "run"
    r2 = await manager.prompt(sid, "say again", permission_policy="operator")
    assert r2["permission_policy"] == "operator" and "permission_note" in r2
    assert (await wait_done(manager, sid))["text"] == "again"

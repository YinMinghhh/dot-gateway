#!/usr/bin/env python3
"""Deterministic Auto review allow/ask/deny transport test (costs a few model turns).

Runs real dsh with the vendored acp-auto profile and DSH_AUTO_REVIEW_TEST=by-command,
so the reviewer decisions are scripted instead of model-generated:
  command contains AUTO_TEST_ASK  -> medium deny -> ACP ask -> gateway pendingPermissions
  command contains AUTO_TEST_DENY -> high deny  -> final denial
  otherwise                       -> low allow

The commands are harmless printf calls in a temporary directory. This validates the
whole path: Auto review audit -> gateway get_events/get_tool_call fields -> operator
resolve_permission. It does not touch real user files or the production gateway.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path("/tmp/dotgw-auto-review-verify")
CWD = ROOT / "repo"
STATE = ROOT / "state"
DSH_SESSIONS = Path.home() / ".dsh" / "sessions"
PROJCACHE = Path.home() / ".dsh" / "storages" / "session_projcache" / "sessions"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def cleanup() -> None:
    shutil.rmtree(ROOT, ignore_errors=True)
    for d in DSH_SESSIONS.glob("--private-tmp-dotgw-auto-review-verify--*"):
        for sub in d.iterdir():
            if sub.is_dir():
                (PROJCACHE / (sub.name + ".json")).unlink(missing_ok=True)
        shutil.rmtree(d, ignore_errors=True)


async def wait_turn(m: object, sid: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await m.get_events(sid, wait_ms=30000)
        if r.get("pending_permissions"):
            return r
        p = r.get("prompt") or {}
        if p.get("state") != "running":
            return r
    raise AssertionError("turn did not settle within timeout")


async def wait_finished(m: object, sid: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await m.get_events(sid, wait_ms=30000)
        p = r.get("prompt") or {}
        if p.get("state") != "running":
            return r
        await asyncio.sleep(0.1)
    raise AssertionError("turn did not finish within timeout")


async def main() -> None:
    cleanup()
    STATE.mkdir(parents=True)
    CWD.mkdir(parents=True)

    from dot_gateway.agents import AgentDefinition, load_registry
    from dot_gateway.config import Config
    from dot_gateway.sessions import SessionManager

    c = Config(state_dir=STATE, config_file=STATE / "config.json")
    reg = load_registry(c)
    auto_env = {"DSH_AUTO_REVIEW": "1", "DSH_PERMISSION_MODE": "workspace-write",
                "DSH_AUTO_REVIEW_TEST": "by-command"}
    reg.register(AgentDefinition(
        id="dsh-auto-test",
        command=c.dsh_bin,
        args=["--profile", "acp-auto"],
        env=dict(auto_env),
        permission_modes={"auto": dict(auto_env)},
        default_permission_mode="auto",
    ))
    m = SessionManager(c, reg)
    sid = None
    try:
        s = await m.start_session(str(CWD), agent_id="dsh-auto-test", permission_mode="auto",
                                  permission_policy="operator",
                                  prompt="Use bash to run exactly: printf AUTO_TEST_ALLOW\nReport its output. Do not retry on block.")
        sid = s["session_id"]
        await wait_turn(m, sid)
        full = await m.get_session(sid, detail="full")
        pr = full["prompts"][-1]
        tid = pr["tool_calls"][0]["tool_call_id"]
        allow_out = (await m.get_tool_call(sid, tid))["output"]
        allow_review = (await m.get_events(sid, include_text=True))["prompt"].get("tool_reviews") or []
        assert "AUTO_TEST_ALLOW" in allow_out, allow_out
        assert allow_review and allow_review[0]["decision"] == "allow", allow_review
        assert allow_review[0]["executedSandbox"] == "danger-full-access", allow_review
        print("PASS allow:", allow_review[0])

        await m.prompt(sid, "Use bash to run exactly: printf AUTO_TEST_ASK\nReport the result. Do not retry on block.",
                       permission_policy="operator")
        r = await wait_turn(m, sid)
        pend = r.get("pending_permissions") or []
        assert pend, "expected an Auto review ask"
        p = pend[0]
        assert p["approval_source"] == "auto-review", p
        assert p["review"]["decision"] == "ask", p
        assert "AUTO_TEST_ASK" in (p.get("review", {}).get("reason") or ""), p
        m.resolve_permission(sid, p["permission_id"], "reject-once")
        await wait_finished(m, sid)
        ask_pr = (await m.get_session(sid, detail="full"))["prompts"][-1]
        ask_tid = ask_pr["tool_calls"][0]["tool_call_id"]
        ask_out = (await m.get_tool_call(sid, ask_tid))["output"]
        ask_review = (await m.get_events(sid, include_text=True))["prompt"].get("tool_reviews") or []
        assert "AUTO_TEST_ASK" not in ask_out.replace("Auto review", ""), ask_out
        assert ask_review and ask_review[0]["decision"] == "ask", ask_review
        print("PASS ask:", {"source": p["approval_source"], "review": ask_review[0]})

        await m.prompt(sid, "Use bash to run exactly: printf AUTO_TEST_DENY\nReport the result. Never retry or work around a denial.",
                       permission_policy="operator")
        await wait_turn(m, sid)
        deny_pr = (await m.get_session(sid, detail="full"))["prompts"][-1]
        deny_tid = deny_pr["tool_calls"][0]["tool_call_id"]
        deny_out = (await m.get_tool_call(sid, deny_tid))["output"]
        deny_review = (await m.get_events(sid, include_text=True))["prompt"].get("tool_reviews") or []
        assert "AUTO_TEST_DENY" not in deny_out.replace("Auto review", ""), deny_out
        assert deny_review and deny_review[0]["decision"] == "deny", deny_review
        print("PASS deny:", deny_review[0])
        print("ALL AUTO REVIEW BRANCHES PASSED")
    finally:
        if sid is not None:
            try:
                await m.close_session(sid)
            except Exception:
                pass
        cleanup()


if __name__ == "__main__":
    asyncio.run(main())

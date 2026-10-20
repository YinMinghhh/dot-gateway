import os
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest

from dot_gateway.agents import AgentDefinition, load_registry
from dot_gateway.config import Config
from dot_gateway.sessions import SessionManager

FAKE = str(Path(__file__).with_name("fake_acp_agent.py"))
MODES = {"low": {"env": {"FAKE_MODE": "low"}}, "high": {"env": {"FAKE_MODE": "high"}}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_state(tmp_path):
    p = tmp_path / "fake-agent-state"
    p.mkdir()
    return p


def fake_agent(agent_id, fake_state, caps="resume,list,close"):
    return AgentDefinition(id=agent_id, command=sys.executable, args=[FAKE],
                           env={"FAKE_ACP_STATE": str(fake_state), "FAKE_ACP_CAPS": caps},
                           permission_modes=MODES, default_permission_mode="low")


@pytest.fixture
def make_manager(tmp_path, fake_state):
    def _make(state_dir=None):
        c = Config(state_dir=Path(state_dir or tmp_path / "state"))
        c.startup_timeout = 20
        reg = load_registry(c)
        reg.register(fake_agent("fake", fake_state))
        reg.register(fake_agent("fake_load", fake_state, "load,list,close"))
        reg.register(fake_agent("fake_none", fake_state, ""))
        return SessionManager(c, reg)

    return _make


@pytest.fixture
async def manager(make_manager):
    m = make_manager()
    m.recover()
    yield m
    await m.shutdown()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    (r / "README").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True, env=env)
    return r


async def wait_done(m, sid, timeout=15):
    """Wait for the current turn; return its full record (as get_session(detail='full') lists it)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await m.get_events(sid, after_sequence=0, limit=1)
        if r["prompt"] and r["prompt"]["state"] != "running":
            return (await m.get_session(sid, detail="full", prompts_limit=1))["prompts"][-1]
        await anyio.sleep(0.05)
    raise AssertionError("prompt did not finish")


async def wait_for(pred, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = await pred()
        if v:
            return v
        await anyio.sleep(0.05)
    raise AssertionError("condition not met")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")

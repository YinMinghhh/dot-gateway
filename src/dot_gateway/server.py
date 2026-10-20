"""MCP server: ACP session tools (named after kxress/acp-mcp) over stateless Streamable HTTP."""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import logging
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .acp import AcpError
from .agents import AgentError, load_registry
from .config import PERMISSION_POLICIES, Config
from .sessions import SessionError, SessionManager

log = logging.getLogger("dot_gateway")

INSTRUCTIONS = """\
Run coding agents (default: dsh, DeepSeek Harness) on this machine as multi-turn ACP sessions.
Flow: start_session(cwd) -> prompt(sessionId, text) -> get_events(sessionId, waitMs=25000) until
prompt.state is completed / failed / cancelled (the default view 'final' long-polls until the turn ends or a
permission question appears) -> read prompt.text (the agent's final reply, sent once) and prompt.toolCalls
(one line per tool call) -> prompt again to continue the same conversation -> close_session when done.
Every call returns within 60 s (the tunnel allows 120 s). One prompt at a time per session: prompting a busy
session fails with SESSION_BUSY. Keep the sessionId; list_sessions finds it again.
The final reply is delivered once; later responses show textBytes + deliveredAtSequence instead. If you lost
it, call get_events(includeText=true) or get_session(detail='full'), which always return it.
Details on demand: get_events(view='messages') = whole messages + one line per tool call;
view='full' (+ includeThoughts) = raw ACP updates; get_tool_call(toolCallId) = one call's full input/output.
Sessions survive gateway restarts and machine reboots (prompting re-attaches with session/resume).
Permission requests are answered by permissionPolicy (default read_only); with 'operator' they appear in
pendingPermissions until resolve_permission. If the machine is asleep or offline, calls fail at the tunnel.
"""

_NO_CAMEL = {"update", "tool_call", "options", "config_options", "capabilities", "agent_info", "error",
             "config_values", "git_summary", "git_start", "events", "input", "locations"}


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


def camelize(obj: Any) -> Any:
    """snake_case gateway fields -> camelCase like acp-mcp; ACP payloads are passed through untouched."""
    if isinstance(obj, list):
        return [camelize(v) for v in obj]
    if not isinstance(obj, dict):
        return obj
    out: dict[str, Any] = {}
    for k, v in obj.items():
        ck = _camel(k) if isinstance(k, str) else k
        if k in _NO_CAMEL:
            out[ck] = camelize(v) if k == "git_summary" else v
        else:
            out[ck] = camelize(v)
    return out


def compact(res: Any) -> str:
    """One compact JSON text block per tool result (no indentation, no duplicate structuredContent)."""
    return json.dumps(camelize(res), ensure_ascii=False, separators=(",", ":"))


async def _call(coro_or_value) -> str:
    try:
        res = await coro_or_value if hasattr(coro_or_value, "__await__") else coro_or_value
    except (SessionError, AgentError, AcpError) as e:
        raise ToolError(str(e)) from None
    return compact(res)


def _sync(fn, *a, **kw) -> str:
    try:
        return compact(fn(*a, **kw))
    except (SessionError, AgentError, AcpError) as e:
        raise ToolError(str(e)) from None


def build_server(manager: SessionManager) -> MCPServer:
    server = MCPServer(name="dot-gateway", title="Local coding agents (ACP)", version=__version__,
                       instructions=INSTRUCTIONS, log_level=manager.config.log_level)  # type: ignore[arg-type]
    policies = " | ".join(PERMISSION_POLICIES)
    tool = functools.partial(server.tool, structured_output=False)

    @tool(description="List configured ACP agents (dsh built in; more via the gateway config file), "
          "their permission modes, and the permission policies.")
    async def list_agents() -> str:
        return _sync(manager.list_agents)

    @tool(description=(
        "Start a new ACP session (session/new) for agent `agentId` (default dsh) in absolute directory `cwd`. "
        "Returns sessionId (and promptId if `prompt` was given) at once; the first turn runs in the background - "
        "poll get_events. permissionMode (dsh: read-only | workspace-write | danger-full-access; default "
        f"workspace-write) is the agent's own sandbox. permissionPolicy ({policies}; default read_only) decides "
        "how the gateway answers the agent's permission requests. `model` (e.g. deepseek-v4-pro) or `config` "
        "({configId: value}) set advertised ACP config options. Capabilities/config: get_session(detail='full')."))
    async def start_session(cwd: str, agentId: str = "dsh", prompt: str | None = None,
                            permissionMode: str | None = None, permissionPolicy: str | None = None,
                            model: str | None = None, config: dict[str, str] | None = None) -> str:
        cfg = dict(config or {})
        if model:
            cfg["model"] = model
        return await _call(manager.start_session(cwd=cwd, agent_id=agentId, prompt=prompt,
                                                 permission_mode=permissionMode,
                                                 permission_policy=permissionPolicy, config=cfg))

    @tool(description=(
        "Send the next turn to a session (session/prompt); the agent keeps the conversation's context. Returns "
        "promptId immediately and the turn runs in the background - poll get_events. Fails with SESSION_BUSY "
        "while a previous turn runs. permissionMode / permissionPolicy override the session defaults for this "
        "turn only (a different permissionMode restarts the agent and resumes the session). waitMs (0-60000, "
        "default 0) waits for a quick turn; if it finishes in time, prompt.text is the final reply (delivered "
        "here, so later get_events show deliveredAtSequence instead), otherwise state is running."))
    async def prompt(sessionId: str, text: str, permissionMode: str | None = None,
                     permissionPolicy: str | None = None, waitMs: int = 0) -> str:
        return await _call(manager.prompt(sessionId, text, permission_mode=permissionMode,
                                          permission_policy=permissionPolicy, wait_ms=waitMs))

    @tool(description=(
        "Follow a session's current/last turn. view='final' (default): no event list, just prompt = {state, "
        "stopReason, progress while running; when finished: text = the agent's final reply (its last message), "
        "toolCalls = one line per tool call, gitSummary}. The final text is sent once: later calls give "
        "textBytes + deliveredAtSequence; includeText=true (or re-polling from a cursor before the turn's end) "
        "returns it again, and get_session(detail='full') always has it. waitMs (<=30000) long-polls: in "
        "'final' until the turn ends or a permission question appears. view='messages': events as whole "
        "messages and one line per tool call (no usage noise, no thoughts). view='full': raw ACP session/update "
        "events (thoughts only with includeThoughts). Paging is the same in every view: pass nextSequence back "
        "as afterSequence; limit<=500 events, maxBytes<=1 MiB per page. pendingPermissions lists open questions. "
        "Full tool input/output: get_tool_call."))
    async def get_events(sessionId: str, afterSequence: int = 0, view: str = "final", waitMs: int = 0,
                         includeText: bool = False, limit: int = 100, maxBytes: int = 200_000,
                         includeThoughts: bool = False, replyBytes: int = 65_536) -> str:
        return await _call(manager.get_events(sessionId, after_sequence=afterSequence, limit=limit,
                                              max_bytes=maxBytes, include_thoughts=includeThoughts,
                                              wait_ms=waitMs, reply_bytes=replyBytes, view=view,
                                              include_text=includeText))

    @tool(description=(
        "One tool call's full record: title, kind, status, input (rawInput) and output text, paged by bytes "
        "(offset / nextOffset, maxBytes<=256 KiB). toolCallId comes from view='messages' or 'full' events, or "
        "get_session(detail='full')."))
    async def get_tool_call(sessionId: str, toolCallId: str, offset: int = 0, maxBytes: int = 32_768) -> str:
        return await _call(manager.get_tool_call(sessionId, toolCallId, offset=offset, max_bytes=maxBytes))

    @tool(description=(
        "List sessions, newest first. source='gateway' (default): sessions this gateway manages, with state "
        "(idle/busy/closed), live, and the last prompt's state and preview (no reply text). source='agent': ask "
        "agent `agentId` (session/list) for the sessions it persisted, optionally filtered by cwd; any of them "
        "can be attached with load_session."))
    async def list_sessions(cwd: str | None = None, state: str | None = None, limit: int = 20,
                            source: str = "gateway", agentId: str = "dsh", cursor: str | None = None) -> str:
        return await _call(manager.list_sessions(cwd=cwd, state=state, limit=limit, source=source,
                                                 agent_id=agentId, cursor=cursor))

    @tool(description=(
        "One session. detail='summary' (default): state, modes, recent prompts (state, preview, textBytes, "
        "deliveredAtSequence) without reply text. detail='full': also every listed prompt's final reply text "
        "(the reliable way to recover a reply), tool-call lines with toolCallIds, git summary, config options "
        "(models), capabilities and agent process info."))
    async def get_session(sessionId: str, detail: str = "summary", promptsLimit: int = 10,
                          replyBytes: int = 65_536) -> str:
        return await _call(manager.get_session(sessionId, prompts_limit=promptsLimit, detail=detail,
                                               reply_bytes=replyBytes))

    @tool(description=(
        "Attach a session to a live agent process: session/resume when the agent supports it (dsh), else "
        "session/load. Use it to reopen a closed session, after a gateway restart, or to adopt a session the "
        "agent persisted outside the gateway (then pass agentId and the session's cwd). prompt also re-attaches "
        "automatically, so this is optional for known sessions."))
    async def load_session(sessionId: str, agentId: str | None = None, cwd: str | None = None,
                           permissionMode: str | None = None, permissionPolicy: str | None = None) -> str:
        return await _call(manager.load_session(sessionId, agent_id=agentId, cwd=cwd,
                                                permission_mode=permissionMode, permission_policy=permissionPolicy))

    @tool(description=(
        "Cancel the running turn (ACP session/cancel); pending permission requests are answered 'cancelled'. "
        "Waits up to 5 s for the agent to settle. force=true also kills the agent process (the session stays "
        "resumable). The session remains usable for later prompts."))
    async def cancel_prompt(sessionId: str, promptId: str | None = None, force: bool = False) -> str:
        return await _call(manager.cancel_prompt(sessionId, prompt_id=promptId, force=force))

    @tool(description="Set an ACP config option the agent advertises (session/set_config_option), e.g. "
          "configId='model' value='deepseek-v4-pro', or 'reasoning_effort' 'max'. Value may be the option "
          "value or its display name. Kept and re-applied after re-attach. Options: get_session(detail='full').")
    async def set_config_option(sessionId: str, configId: str, value: str) -> str:
        return await _call(manager.set_config_option(sessionId, configId, value))

    @tool(description="Answer a pending permission request (policy 'operator') with one of its optionIds, "
          "as listed in pendingPermissions.")
    async def resolve_permission(sessionId: str, permissionId: str, optionId: str) -> str:
        return _sync(manager.resolve_permission, sessionId, permissionId, optionId)

    @tool(description="Close a session (cancels a running turn, ACP session/close, stops the agent process). "
          "History stays; load_session reopens it.")
    async def close_session(sessionId: str) -> str:
        return await _call(manager.close_session(sessionId))

    return server


def make_app(manager: SessionManager):
    c = manager.config
    app = build_server(manager).streamable_http_app(streamable_http_path=c.path, stateless_http=True,
                                                    json_response=True, host=c.host)
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        async with inner(a) as state:
            try:
                yield state
            finally:
                await manager.shutdown()

    app.router.lifespan_context = lifespan
    # Unknown paths (e.g. tunnel-client's OAuth discovery probes under /.well-known/) get a JSON 404 instead
    # of Starlette's plain-text "Not Found", so JSON-parsing clients see "no such resource", not a parse error.
    # No auth metadata is advertised. The MCP endpoint itself is untouched.
    app.add_exception_handler(404, _json_not_found)
    return app


async def _json_not_found(request, exc):
    from starlette.responses import JSONResponse

    return JSONResponse({"error": "not_found"}, status_code=404)


def parse_args(argv: list[str] | None = None) -> Config:
    c = Config.from_env()
    p = argparse.ArgumentParser(prog="dot-gateway", description="MCP gateway to local ACP coding agents")
    p.add_argument("--host", default=c.host)
    p.add_argument("--port", type=int, default=c.port)
    p.add_argument("--path", default=c.path, help="MCP endpoint path")
    p.add_argument("--state-dir", type=Path, default=None)
    p.add_argument("--config", type=Path, default=None, help="agents/defaults JSON file")
    p.add_argument("--log-level", default=c.log_level)
    a = p.parse_args(argv)
    if a.state_dir or a.config:  # re-read so the config file of that state dir applies, env still wins
        env = dict(os.environ)
        if a.state_dir:
            env["DOT_GATEWAY_STATE_DIR"] = str(a.state_dir.expanduser())
        if a.config:
            env["DOT_GATEWAY_CONFIG"] = str(a.config.expanduser())
        c = Config.from_env(env)
    c.host, c.port, c.path, c.log_level = a.host, a.port, a.path, a.log_level.upper()
    return c


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    config = parse_args(argv)
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    manager = SessionManager(config, load_registry(config))
    manager.recover()
    log.info("dot-gateway %s on http://%s:%s%s state=%s config=%s", __version__, config.host, config.port,
             config.path, config.state_dir, config.config_file)
    uvicorn.run(make_app(manager), host=config.host, port=config.port, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()

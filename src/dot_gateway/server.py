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
(one line per tool call; the matching ids are prompt.toolCallIds) -> prompt again to continue the same
conversation -> close_session when done.
Every call returns within 60 s (the tunnel allows 120 s). One prompt at a time per session: prompting a busy
session fails with SESSION_BUSY. Keep the sessionId; list_sessions finds it again.
get_events views: 'final' (default) returns prompt only and does not page events (nextSequence is always
latestSequence, hasMore is always false); 'messages' = whole messages + one line per tool call; 'full'
(+ includeThoughts) = raw ACP updates. Only messages/full page through events: pass nextSequence back as
afterSequence (nextSequence/hasMore, limit <= 500, maxBytes <= 1 MiB).
The final reply is delivered once. 'final' returns it in the first response that sees the turn finished and
never duplicates it on later polls, even if afterSequence does not move; 'messages'/'full' return it only on
the page that covers the turn's prompt_finished event, and retrying that same page's afterSequence republishes
it (lost response). After delivery you see textBytes, deliveredAtSequence and permissionRequests; recover it
with includeText=true or get_session(detail='full'), which always has every reply. A reply delivered by
prompt(waitMs) is recovered the same way (includeText or detail='full').
get_events(includeToolOutput=true) adds prompt.toolOutputs for a finished turn: bounded stdout/stderr excerpts
({toolCallId, title, status, output, outputBytes, truncated}; 2 KiB per call, 8 KiB per response). Raw tool
stdout/stderr and larger output: get_tool_call(toolCallId), which has the full input and output.
ACP has no exit-code field and dsh does not always print one: use status, the stdout/stderr text, and
get_tool_call as evidence, not an exit code.
Permissions have two layers. permissionMode is the agent's own sandbox (built-in dsh: read-only |
workspace-write | danger-full-access; a deployment may advertise extra modes such as 'auto', see
list_agents); it is not an approval switch. permissionPolicy (read_only | allow_all | deny_all |
operator; default read_only) only decides how the gateway answers permission requests the agent sends itself:
read_only auto-rejects non-read requests, allow_all auto-allows, deny_all auto-rejects, and operator holds a
request in pendingPermissions until resolve_permission. Work the sandbox already allows needs no approval, so
empty pendingPermissions and permissionRequests=0 are normal, not a broken setting.
dsh escalation: after a sandbox denial dsh may retry the same call once with sandbox_permissions (one step
strictly wider, e.g. workspace-write -> danger-full-access) plus a justification; it asks for approval before
that call (workspace-write/read-only presets ask, danger-full-access preset never) and dsh-acp bridges the ask
into ACP session/request_permission. Under operator such a request waits in pendingPermissions until
resolve_permission; an allow-once answer widens that one call only and does not change the session
permissionMode (change the session with prompt(permissionMode=...); the agent restarts and resumes).
Some deployments add an 'auto' permissionMode (dsh Auto review): Auto review checks every supported tool call
before it runs. Ordinary project-local work executes with full host access; medium/high-risk calls are denied
and then ask through the same session/request_permission path. Pair it with permissionPolicy=operator so those
questions reach you in pendingPermissions; read_only would answer them without asking. Auto review is a dsh
experimental feature, costs extra model calls, and is enabled by the gateway deployment's agent config, not by
an ACP call.
pendingPermissions entries are {permissionId, promptId, toolCallId, tool, options}: dsh-acp sends only the
toolCallId, and the gateway backfills title/kind/status/input from this session's tool records into `tool`, one
line like 'bash: rm -rf … → in_progress' ('tool' if nothing can be backfilled). Limit: dsh-acp does not forward
the requested mode or justification, so the gateway cannot show which wider mode was asked for, or why.
The prompt response echoes the turn's effective permissionMode and permissionPolicy and adds permissionNote
under operator; start_session/load_session/get_session summaries add permissionNote under operator as well.
Sessions survive gateway restarts and machine reboots (prompting re-attaches with session/resume). If the
machine is asleep or offline, calls fail at the tunnel.
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
    # Text-only contract: one compact JSON text block, no duplicate structuredContent. Keep it that way.
    tool = functools.partial(server.tool, structured_output=False)

    @tool(description="List configured ACP agents (dsh built in; more via the gateway config file), "
          "their permission modes, and the permission policies.")
    async def list_agents() -> str:
        return _sync(manager.list_agents)

    @tool(description=(
        "Start a new ACP session (session/new) for agent `agentId` (default dsh) in absolute directory `cwd`. "
        "Returns sessionId (and promptId if `prompt` was given) at once; the first turn runs in the background - "
        "poll get_events. permissionMode values and the default are listed by list_agents for the chosen agent "
        "(built-in dsh: read-only | workspace-write | danger-full-access; a deployment may add e.g. auto for "
        f"Auto review). permissionPolicy ({policies}; default read_only) only "
        "decides how the gateway answers permission requests the agent sends itself; sandbox-allowed work needs "
        "no approval, so an empty pendingPermissions is normal, and under 'operator' the returned summary "
        "includes permissionNote. `model` (e.g. deepseek-v4-pro) or `config` "
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
        "turn only (a different permissionMode restarts the agent and resumes the session); the response echoes "
        "the effective permissionMode/permissionPolicy and adds permissionNote under policy 'operator'. "
        "permissionPolicy only answers the agent's own permission requests - sandbox-allowed work needs none, "
        "and an allow-once answer to an escalation widens that one call only, never the session mode. "
        "waitMs (0-60000, default 0) waits for a quick turn; if it finishes in time, prompt.text is the final "
        "reply (delivered here, so later get_events show textBytes + deliveredAtSequence instead; recover it "
        "with get_events(includeText=true) or get_session(detail='full')), otherwise state is running."))
    async def prompt(sessionId: str, text: str, permissionMode: str | None = None,
                     permissionPolicy: str | None = None, waitMs: int = 0) -> str:
        return await _call(manager.prompt(sessionId, text, permission_mode=permissionMode,
                                          permission_policy=permissionPolicy, wait_ms=waitMs))

    @tool(description=(
        "Follow a session's current/last turn. view='final' (default): no event list, just prompt = {state, "
        "stopReason, permissionRequests, progress while running; when finished, permissionRequests stays and "
        "the response that delivers the reply also carries text = the agent's final reply (its last message), "
        "toolCalls = one line per tool call, toolCallIds = the matching ids, gitSummary; with "
        "includeToolOutput=true also toolOutputs = bounded tool output excerpts}. The 'final' view does "
        "not page events: nextSequence is always latestSequence and hasMore is always false. The final text is "
        "delivered once: 'final' returns it in the first response that sees the turn finished and never repeats "
        "it on later polls; 'messages'/'full' only on the page covering the turn's end, and retrying that page's "
        "afterSequence republishes it (lost response). Later calls give textBytes + deliveredAtSequence and "
        "permissionRequests; recover with includeText=true or get_session(detail='full'), which always has every "
        "reply; a reply delivered by prompt(waitMs) is recovered the same way. waitMs (<=30000) long-polls: in "
        "'final' until the turn "
        "ends or a permission question appears. view='messages': events as whole messages and one line per tool "
        "call (no usage noise, no thoughts). view='full': raw ACP session/update events (thoughts only with "
        "includeThoughts). Only messages/full page through events: pass nextSequence back as afterSequence; "
        "limit<=500 events, maxBytes<=1 MiB per page; text appears only on the page that covers the turn's end. "
        "includeToolOutput=true adds prompt.toolOutputs for a finished turn: {toolCallId, title, status, output, "
        "outputBytes, truncated}, 2 KiB per call and 8 KiB per response - the full record is get_tool_call. "
        "pendingPermissions lists open permission requests while the turn waits: {permissionId, promptId, "
        "toolCallId, tool, options}; tool is one line backfilled from this session's tool records (e.g. "
        "'bash: rm -rf … → in_progress'), or 'tool' when nothing can be backfilled. Under 'operator' an escalation "
        "to a wider sandbox waits there until resolve_permission; empty pendingPermissions and "
        "permissionRequests=0 are normal. ACP has no exit-code field and dsh does not always print one: use "
        "status, the stdout/stderr text, and get_tool_call as evidence, not an exit code."))
    async def get_events(sessionId: str, afterSequence: int = 0, view: str = "final", waitMs: int = 0,
                         includeText: bool = False, includeToolOutput: bool = False, limit: int = 100,
                         maxBytes: int = 200_000, includeThoughts: bool = False,
                         replyBytes: int = 65_536) -> str:
        return await _call(manager.get_events(sessionId, after_sequence=afterSequence, limit=limit,
                                              max_bytes=maxBytes, include_thoughts=includeThoughts,
                                              wait_ms=waitMs, reply_bytes=replyBytes, view=view,
                                              include_text=includeText,
                                              include_tool_output=includeToolOutput))

    @tool(description=(
        "One tool call's full record: title, kind, status, input (rawInput) and output text, paged by bytes "
        "(offset / nextOffset, maxBytes<=256 KiB). toolCallId comes from view='messages' or 'full' events, or "
        "get_session(detail='full'). Use the full stdout/stderr here plus status as evidence: ACP has no "
        "exit-code field and dsh does not always print one, so no tool promises an exit code."))
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
        "One session. detail='summary' (default): state, permissionMode/permissionPolicy (plus permissionNote "
        "under 'operator'), pendingPermissions ({permissionId, promptId, toolCallId, tool, options}) and recent "
        "prompts (state, preview, textBytes, deliveredAtSequence) without reply text. detail='full': also every "
        "listed prompt's final reply text (the reliable way to recover a reply), tool-call lines with toolCallIds, "
        "git summary, config options (models), capabilities and agent process info."))
    async def get_session(sessionId: str, detail: str = "summary", promptsLimit: int = 10,
                          replyBytes: int = 65_536) -> str:
        return await _call(manager.get_session(sessionId, prompts_limit=promptsLimit, detail=detail,
                                               reply_bytes=replyBytes))

    @tool(description=(
        "Attach a session to a live agent process: session/resume when the agent supports it (dsh), else "
        "session/load. Use it to reopen a closed session, after a gateway restart, or to adopt a session the "
        "agent persisted outside the gateway (then pass agentId and the session's cwd). prompt also re-attaches "
        "automatically, so this is optional for known sessions. Returns the session summary, which includes "
        "permissionNote under policy 'operator'."))
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

    @tool(description="Answer a pending permission request (policy 'operator') with one of its optionIds from "
          "the pendingPermissions entry (e.g. a sandbox escalation offering allow-once/reject-once). An "
          "allow-once answer widens that one call only and does not change the session permissionMode; to change "
          "the session use prompt(permissionMode=...).")
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

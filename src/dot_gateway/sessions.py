"""Session manager: maps ACP sessions to MCP tool calls, persists sessions and events.

Process model: one ACP agent process per live session, started in the
session's cwd (dsh uses the process cwd as its sandbox root and reads
DSH_PERMISSION_MODE at start). Agent processes are children of the gateway;
they end when the gateway stops. Session records, event journals and the
agent's own persisted conversation survive, and the next prompt/load_session
re-attaches with session/resume (or session/load when that is all the agent
offers).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, gitinfo
from .acp import PROTOCOL_VERSION, AcpConnection, AcpError, AgentExited
from .agents import AgentDefinition, AgentError, AgentRegistry
from .config import PERMISSION_POLICIES, Config
from .events import EventLog, atomic_write_json, clip, now_iso

log = logging.getLogger("dot_gateway.sessions")

MAX_REPLY_STORED = 256 * 1024
MAX_PROMPTS_KEPT = 100
DEFAULT_EVENT_BYTES = 200 * 1024
MAX_EVENT_BYTES = 1024 * 1024
DEFAULT_REPLY_BYTES = 64 * 1024
MAX_REPLY_BYTES = 256 * 1024
PREVIEW_CHARS = 200  # prompt previews (prompt_started event, prompt records, list_sessions)
MAX_TOOL_STORED = 1024 * 1024  # per string, in tool-calls/<id>.json (full detail for get_tool_call)
DEFAULT_TOOL_BYTES = 32 * 1024
MAX_TOOL_BYTES = 256 * 1024
MAX_TOOL_SUMMARIES = 200  # one-line tool summaries kept per prompt
VIEWS = ("final", "messages", "full")
_QUIET_KINDS = {"usage_update", "available_commands_update", "config_option_update", "current_mode_update"}
MAX_PROMPT_WAIT_MS = 60_000  # prompt(waitMs): returns 'running' after this
MAX_WAIT_MS = 30_000  # get_events(waitMs) long-poll
READ_KINDS = ("read", "search", "fetch", "think")
_SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class SessionError(ValueError):
    """Reported to the caller as a tool error. `code` is a stable machine-readable tag."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass
class Runtime:
    agent: AgentDefinition
    mode: str | None
    conn: AcpConnection | None = None
    sid: str | None = None
    caps: dict[str, Any] = field(default_factory=dict)
    agent_info: dict[str, Any] = field(default_factory=dict)
    policy: str = "read_only"
    prompt_id: str | None = None
    prompt_task: asyncio.Task | None = None
    cancel_requested: bool = False
    stopping: str | None = None  # reason when the gateway itself stops the process
    replaying: bool = False
    early: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    pending: dict[str, tuple[asyncio.Future, dict[str, Any]]] = field(default_factory=dict)
    replies: dict[str, dict[str, Any]] = field(default_factory=dict)
    tools: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)  # prompt_id -> toolCallId -> summary


def _validate_policy(policy: str | None, default: str) -> str:
    p = policy or default
    if p not in PERMISSION_POLICIES:
        raise SessionError("BAD_ARGUMENT", f"permission_policy must be one of {PERMISSION_POLICIES}")
    return p


def _flatten_options(opt: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for o in opt.get("options") or []:
        if isinstance(o, dict) and "options" in o and "value" not in o:
            out += [x for x in o["options"] if isinstance(x, dict)]
        elif isinstance(o, dict):
            out.append(o)
    return out


class SessionManager:
    def __init__(self, config: Config, registry: AgentRegistry):
        self.config = config
        self.registry = registry
        self.root = Path(config.state_dir) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, dict[str, Any]] = {}
        self._logs: dict[str, EventLog] = {}
        self._rt: dict[str, Runtime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._changed: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ storage
    def _dir(self, sid: str) -> Path:
        if not isinstance(sid, str) or not _SID_RE.match(sid) or sid in (".", ".."):
            raise SessionError("BAD_ARGUMENT", f"invalid session_id {sid!r}")
        return self.root / sid

    def _rec(self, sid: str) -> dict[str, Any]:
        d = self._dir(sid)
        rec = self._records.get(sid)
        if rec is None:
            p = d / "session.json"
            if not p.exists():
                raise SessionError(
                    "SESSION_NOT_FOUND",
                    f"no session {sid!r} in the gateway; use list_sessions, or load_session with agent_id and cwd "
                    "to attach a session the agent persisted",
                )
            rec = json.loads(p.read_text())
            self._records[sid] = rec
        return rec

    def _save(self, rec: dict[str, Any]) -> None:
        rec["updated_at"] = now_iso()
        d = self._dir(rec["session_id"])
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_json(d / "session.json", rec)

    def _log(self, sid: str) -> EventLog:
        lg = self._logs.get(sid)
        if lg is None:
            lg = self._logs[sid] = EventLog(self._dir(sid) / "events.jsonl")
        return lg

    def _emit(self, sid: str, kind: str, data: Any = None, prompt_id: str | None = None) -> None:
        self._log(sid).append(kind, data, prompt_id)
        ev = self._changed.pop(sid, None)
        if ev is not None:
            ev.set()

    def _lock(self, sid: str) -> asyncio.Lock:
        return self._locks.setdefault(sid, asyncio.Lock())

    def _spawn_task(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    # ------------------------------------------------------------------ recovery
    def recover(self) -> dict[str, int]:
        """Run once at startup, before serving. Agent processes never survive a gateway restart."""
        counts = {"sessions": 0, "interrupted_prompts": 0, "orphans_killed": 0}
        for d in sorted(self.root.iterdir()):
            p = d / "session.json"
            if not p.exists():
                continue
            try:
                rec = json.loads(p.read_text())
            except ValueError:
                log.warning("unreadable session record %s", p)
                continue
            sid = rec["session_id"]
            self._records[sid] = rec
            counts["sessions"] += 1
            if _kill_orphan(rec.get("agent_pid"), rec.get("agent_argv") or []):
                counts["orphans_killed"] += 1
            changed = False
            if rec.get("agent_pid"):
                rec["agent_pid"] = None
                changed = True
            if rec["state"] == "busy":
                pr = self._prompt(rec, rec.get("current_prompt_id"))
                if pr is not None and pr["state"] == "running":
                    pr.update(
                        state="failed",
                        finished_at=now_iso(),
                        error={
                            "message": "interrupted: the gateway restarted while this prompt was running and the "
                            "agent process ended with it. The agent keeps its own conversation history up to its "
                            "last checkpoint; prompt again to continue (the gateway re-attaches automatically)."
                        },
                    )
                    counts["interrupted_prompts"] += 1
                rec["state"] = "idle"
                rec["current_prompt_id"] = None
                changed = True
            if rec.get("pending_permissions"):
                rec["pending_permissions"] = []
                changed = True
            if changed:
                self._emit(sid, "gateway_restarted", {"note": "agent process gone; will re-attach on next use"})
                self._save(rec)
        log.info("recovered: %s", counts)
        return counts

    async def shutdown(self) -> None:
        for rt in list(self._rt.values()):
            rt.stopping = "interrupted: the gateway stopped while this prompt was running"
        await asyncio.gather(*(self._stop_runtime(rt, close_remote=False, grace=3) for rt in list(self._rt.values())),
                             return_exceptions=True)
        tasks = [t for t in self._tasks if not t.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=5)

    # ------------------------------------------------------------------ agent processes
    def _base_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = self.config.extra_path + ":" + env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        return env

    async def _spawn(self, agent: AgentDefinition, cwd: str, mode: str | None, stderr_path: Path) -> Runtime:
        rt = Runtime(agent=agent, mode=mode)
        argv, env = agent.launch(mode, self._base_env())
        rt.conn = AcpConnection(
            argv,
            cwd,
            env,
            str(stderr_path),
            on_notification=lambda m, p: self._on_notification(rt, m, p),
            on_request=lambda m, p: self._on_request(rt, m, p),
            on_exit=lambda code: self._on_exit(rt, code),
            on_noise=lambda line: _append_text(stderr_path, f"[agent stdout, not JSON-RPC] {line}\n"),
        )
        try:
            await rt.conn.start()
        except OSError as e:
            raise SessionError("AGENT_START_FAILED", f"could not start {argv[0]}: {e}") from None
        try:
            init = await rt.conn.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                    "clientInfo": {"name": "dot-gateway", "version": __version__},
                },
                timeout=self.config.startup_timeout,
            )
        except AcpError as e:
            await rt.conn.stop(grace=1)
            raise SessionError("AGENT_START_FAILED", f"initialize failed: {e}; see {stderr_path}") from None
        rt.caps = (init or {}).get("agentCapabilities") or {}
        rt.agent_info = (init or {}).get("agentInfo") or {}
        return rt

    async def _stop_runtime(self, rt: Runtime, close_remote: bool, grace: float = 5) -> None:
        if rt.sid and self._rt.get(rt.sid) is rt:
            del self._rt[rt.sid]
        if rt.conn is None:
            return
        if close_remote and rt.sid and "close" in (rt.caps.get("sessionCapabilities") or {}) and not rt.conn.exited.is_set():
            try:
                rec = self._records.get(rt.sid)
                await rt.conn.request("session/close", {"sessionId": _acp_id(rec) if rec else rt.sid}, timeout=10)
            except AcpError:
                pass
        rt.conn.closed = True
        await rt.conn.stop(grace=grace)
        if rt.sid and rt.sid in self._records:
            rec = self._records[rt.sid]
            if rec.get("agent_pid") == rt.conn.pid:
                rec["agent_pid"] = None
                self._save(rec)

    def _register(self, rt: Runtime, rec: dict[str, Any]) -> None:
        sid = rec["session_id"]
        rt.sid = sid
        self._rt[sid] = rt
        rec["agent_pid"] = rt.conn.pid if rt.conn else None
        rec["agent_argv"] = rt.conn.argv if rt.conn else []
        rec["agent_mode"] = rt.mode
        rec["agent_capabilities"] = rt.caps
        rec["agent_info"] = rt.agent_info

    def _flush_early(self, rt: Runtime) -> None:
        early, rt.early = rt.early, []
        for method, params in early:
            self._on_notification(rt, method, params)

    async def _attach(self, rec: dict[str, Any], mode: str | None) -> Runtime:
        """Start an agent process for an existing session: session/resume, else session/load."""
        sid = rec["session_id"]
        agent = self.registry.get(rec["agent_id"])
        if not os.path.isdir(rec["cwd"]):
            raise SessionError("CWD_MISSING", f"session cwd {rec['cwd']} no longer exists")
        rt = await self._spawn(agent, rec["cwd"], mode, self._dir(sid) / "agent-stderr.log")
        rt.policy = rec["permission_policy"]
        self._register(rt, rec)
        sc = rt.caps.get("sessionCapabilities") or {}
        params = {"sessionId": _acp_id(rec), "cwd": rec["cwd"], "mcpServers": []}
        try:
            if "resume" in sc:
                method = "session/resume"
                r = await rt.conn.request(method, params, timeout=self.config.startup_timeout)
            elif rt.caps.get("loadSession"):
                method = "session/load"
                rt.replaying = True
                try:
                    r = await rt.conn.request(method, params, timeout=self.config.startup_timeout)
                finally:
                    rt.replaying = False
            else:
                raise SessionError(
                    "NOT_RESUMABLE",
                    f"agent {agent.id!r} advertises neither session/resume nor session/load, so this session cannot "
                    "be continued after its agent process ended; start a new session",
                )
        except (AcpError, SessionError) as e:
            await self._stop_runtime(rt, close_remote=False, grace=1)
            if isinstance(e, SessionError):
                raise
            raise SessionError("ATTACH_FAILED", f"{method} failed: {e}") from None
        r = r or {}
        if r.get("configOptions") is not None:
            rec["config_options"] = r["configOptions"]
        self._emit(sid, "session_attached", {"method": method, "permissionMode": mode, "pid": rt.conn.pid})
        await self._reapply_config(rt, rec)
        self._save(rec)
        return rt

    async def _reapply_config(self, rt: Runtime, rec: dict[str, Any]) -> None:
        current = {o.get("id"): o.get("currentValue") for o in rec.get("config_options") or []}
        for cid, value in (rec.get("config_values") or {}).items():
            if current.get(cid) == value:
                continue
            try:
                r = await rt.conn.request(
                    "session/set_config_option", {"sessionId": _acp_id(rec), "configId": cid, "value": value}, timeout=30
                )
                if (r or {}).get("configOptions") is not None:
                    rec["config_options"] = r["configOptions"]
            except AcpError as e:
                self._emit(rec["session_id"], "config_reapply_failed", {"configId": cid, "error": str(e)})

    # ------------------------------------------------------------------ agent callbacks
    def _on_notification(self, rt: Runtime, method: str, params: dict[str, Any]) -> None:
        if rt.sid is None:
            rt.early.append((method, params))
            return
        sid = rt.sid
        if method != "session/update":
            self._emit(sid, f"notification:{method}", params, rt.prompt_id)
            return
        update = dict(params.get("update") or {})
        kind = update.get("sessionUpdate", "unknown")  # kept inside update too, like acp-mcp
        if rt.replaying:
            update["replay"] = True
        if kind == "agent_message_chunk" and rt.prompt_id and not rt.replaying:
            content = update.get("content") or {}
            if content.get("type") == "text":
                agg = rt.replies.setdefault(rt.prompt_id, {"order": [], "text": {}})
                mid = update.get("messageId") or "_"
                if mid not in agg["text"]:
                    agg["order"].append(mid)
                    agg["text"][mid] = ""
                agg["text"][mid] += content.get("text", "")
        if kind in ("tool_call", "tool_call_update") and not rt.replaying:
            self._track_tool(rt, sid, update)
        if kind == "config_option_update" and sid in self._records:
            opts = update.get("configOptions")
            if opts is not None:
                self._records[sid]["config_options"] = opts
        self._emit(sid, kind, update, rt.prompt_id)

    def _tool_path(self, sid: str, tool_call_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(tool_call_id))[:150] or "_"
        return self._dir(sid) / "tool-calls" / f"{safe}.json"

    def _track_tool(self, rt: Runtime, sid: str, update: dict[str, Any]) -> None:
        """Keep a one-line summary per tool call (for concise views) and its full record on disk (get_tool_call)."""
        tid = update.get("toolCallId")
        if not tid:
            return
        tid = str(tid)
        if rt.prompt_id:
            tools = rt.tools.setdefault(rt.prompt_id, {})
            summ = tools.get(tid)
            if summ is None and len(tools) < MAX_TOOL_SUMMARIES:
                summ = tools[tid] = {"tool_call_id": tid, "title": None, "kind": None, "status": None, "input": None}
            if summ is not None:
                for k in ("title", "kind", "status"):
                    if update.get(k) is not None:
                        summ[k] = update[k]
                if update.get("rawInput") is not None:
                    summ["input"] = _one_line(update["rawInput"])
        path = self._tool_path(sid, tid)
        full: dict[str, Any] = {"toolCallId": tid, "promptId": rt.prompt_id, "updates": 0}
        try:
            if path.exists():
                full = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        for k, v in update.items():
            if k != "sessionUpdate":
                full[k] = v
        full["updates"] = int(full.get("updates") or 0) + 1
        full, cut = clip(full, MAX_TOOL_STORED)
        if cut:
            full["truncated"] = True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(full, ensure_ascii=False))
        except OSError:  # pragma: no cover
            log.exception("could not store tool call %s", tid)

    async def _on_request(self, rt: Runtime, method: str, params: dict[str, Any]) -> Any:
        if method == "session/request_permission":
            return await self._on_permission(rt, params)
        raise AcpError(f"{method} is not supported by dot-gateway (client capabilities: no fs, no terminal)", -32601)

    def _on_exit(self, rt: Runtime, code: int | None) -> None:
        if rt.sid is None:
            return
        sid = rt.sid
        expected = rt.conn is not None and rt.conn.closed
        if self._rt.get(sid) is rt:
            del self._rt[sid]
            rec = self._records.get(sid)
            if rec is not None and rec.get("agent_pid") == (rt.conn.pid if rt.conn else None):
                rec["agent_pid"] = None
                self._save(rec)
        if not expected:
            self._emit(sid, "agent_exited", {"returncode": code, "note": "unexpected; next prompt re-attaches"},
                       rt.prompt_id)

    async def _on_permission(self, rt: Runtime, params: dict[str, Any]) -> dict[str, Any]:
        sid = rt.sid
        tool = params.get("toolCall") or {}
        options = params.get("options") or []
        policy = rt.policy
        pid = "perm-" + uuid.uuid4().hex[:12]
        info = {
            "permissionId": pid,
            "promptId": rt.prompt_id,
            "toolCall": clip(tool, 2048)[0],
            "options": options,
            "policy": policy,
            "requestedAt": now_iso(),
        }
        self._emit(sid, "permission_request", info, rt.prompt_id)
        by = "policy"
        if policy == "operator" and sid is not None:
            fut = asyncio.get_running_loop().create_future()
            rt.pending[pid] = (fut, info)
            self._sync_pending(rt)
            try:
                outcome, by = await fut
            finally:
                rt.pending.pop(pid, None)
                self._sync_pending(rt)
        else:
            kind = tool.get("kind")
            permit = policy == "allow_all" or (policy == "read_only" and kind in READ_KINDS)
            want = ("allow_once", "allow_always") if permit else ("reject_once", "reject_always")
            opt = next((o for k in want for o in options if o.get("kind") == k), None)
            outcome = {"outcome": "selected", "optionId": opt["optionId"]} if opt else {"outcome": "cancelled"}
        if sid is not None:
            self._emit(sid, "permission_resolved", {"permissionId": pid, "outcome": outcome, "by": by}, rt.prompt_id)
        return {"outcome": outcome}

    def _sync_pending(self, rt: Runtime) -> None:
        if rt.sid in self._records:
            rec = self._records[rt.sid]
            rec["pending_permissions"] = [info for _, info in rt.pending.values()]
            self._save(rec)

    def _resolve_all_pending(self, rt: Runtime, by: str) -> None:
        for fut, _ in list(rt.pending.values()):
            if not fut.done():
                fut.set_result(({"outcome": "cancelled"}, by))

    # ------------------------------------------------------------------ views
    @staticmethod
    def _prompt(rec: dict[str, Any], prompt_id: str | None) -> dict[str, Any] | None:
        if not prompt_id:
            return None
        return next((p for p in rec.get("prompts", []) if p["prompt_id"] == prompt_id), None)

    def _rt_for_prompt(self, prompt_id: str | None) -> Runtime | None:
        if not prompt_id:
            return None
        return next((r for r in self._rt.values() if r.prompt_id == prompt_id), None)

    def _tool_summaries(self, pr: dict[str, Any]) -> list[dict[str, Any]]:
        rt = self._rt_for_prompt(pr["prompt_id"]) if pr.get("state") == "running" else None
        if rt is not None:
            return list(rt.tools.get(pr["prompt_id"], {}).values())
        return pr.get("tool_calls") or []

    def _turn(
        self,
        rec: dict[str, Any],
        pr: dict[str, Any] | None,
        *,
        deliver: bool = False,
        include_text: bool = False,
        covered: bool = False,
        reply_bytes: int = DEFAULT_REPLY_BYTES,
    ) -> dict[str, Any] | None:
        """Concise view of one turn. The final reply `text` is sent once: on the first response that sees the
        turn finished (deliver=True marks it delivered), again when the page covers the turn's prompt_finished
        event (a re-poll from the same cursor), or on request (include_text)."""
        if pr is None:
            return None
        v: dict[str, Any] = {"prompt_id": pr["prompt_id"], "state": pr["state"], "stop_reason": pr.get("stop_reason")}
        if pr.get("error"):
            v["error"] = pr["error"]
        if pr["state"] == "running":
            rt = self._rt_for_prompt(pr["prompt_id"])
            tools = list(rt.tools.get(pr["prompt_id"], {}).values()) if rt else []
            agg = rt.replies.get(pr["prompt_id"]) if rt else None
            v["progress"] = {"tool_calls": len(tools), "last_tool": _tool_line(tools[-1]) if tools else None,
                             "messages": len(agg["order"]) if agg else 0}
            return v
        raw = (pr.get("reply_text") or "").encode()
        v["text_bytes"] = len(raw)
        delivered = pr.get("delivered_at_sequence")
        if include_text or delivered is None or covered:
            v["text"] = raw[:reply_bytes].decode("utf-8", errors="ignore")
            if len(raw) > reply_bytes or pr.get("reply_truncated"):
                v["text_truncated"] = True
            tools = pr.get("tool_calls") or []
            if tools:
                v["tool_calls"] = [_tool_line(t) for t in tools]
            if pr.get("git_summary"):
                v["git_summary"] = pr["git_summary"]
            if delivered is None and deliver:
                pr["delivered_at_sequence"] = pr.get("last_sequence") or self._log(rec["session_id"]).latest
                self._save(rec)
        else:
            v["delivered_at_sequence"] = delivered
        return v

    def _summary(self, rec: dict[str, Any]) -> dict[str, Any]:
        sid = rec["session_id"]
        return {
            "session_id": sid,
            "agent_id": rec["agent_id"],
            "cwd": rec["cwd"],
            "state": rec["state"],
            "live": sid in self._rt,
            "permission_mode": rec.get("permission_mode"),
            "permission_policy": rec.get("permission_policy"),
        }

    @staticmethod
    def _pending(rec: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for info in rec.get("pending_permissions") or []:
            tc = info.get("toolCall") or {}
            out.append({"permission_id": info.get("permissionId"), "prompt_id": info.get("promptId"),
                        "tool": _tool_line({"title": tc.get("title"), "kind": tc.get("kind"),
                                            "input": _one_line(tc.get("rawInput")) if tc.get("rawInput") else None}),
                        "options": [{"optionId": o.get("optionId"), "name": o.get("name"), "kind": o.get("kind")}
                                    for o in info.get("options") or []]})
        return out

    def _last_prompt(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        prompts = rec.get("prompts", [])
        return self._prompt(rec, rec.get("current_prompt_id")) or (prompts[-1] if prompts else None)

    # ------------------------------------------------------------------ operations
    def list_agents(self) -> dict[str, Any]:
        return {"agents": self.registry.infos(), "permission_policies": list(PERMISSION_POLICIES),
                "default_permission_policy": self.config.default_permission_policy}

    async def start_session(
        self,
        cwd: str,
        agent_id: str = "dsh",
        prompt: str | None = None,
        permission_mode: str | None = None,
        permission_policy: str | None = None,
        config: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        agent = self.registry.get(agent_id)
        mode = agent.resolve_mode(permission_mode)
        policy = _validate_policy(permission_policy, self.config.default_permission_policy)
        cwd_real = _check_cwd(cwd)
        tmp_err = self.root / f".starting-{uuid.uuid4().hex}.stderr.log"
        rt = await self._spawn(agent, cwd_real, mode, tmp_err)
        rt.policy = policy
        try:
            r = await rt.conn.request("session/new", {"cwd": cwd_real, "mcpServers": []},
                                      timeout=self.config.startup_timeout)
            sid = (r or {}).get("sessionId")
            if not sid or not _SID_RE.match(sid):
                raise SessionError("AGENT_PROTOCOL", f"agent returned an unusable sessionId {sid!r}")
            if sid in self._records or (self.root / sid).exists():
                raise SessionError("AGENT_PROTOCOL", f"agent reused an existing session id {sid!r}")
        except (AcpError, SessionError) as e:
            await rt.conn.stop(grace=1)
            _move(tmp_err, None)
            if isinstance(e, SessionError):
                raise
            raise SessionError("AGENT_START_FAILED", f"session/new failed: {e}") from None
        d = self._dir(sid)
        d.mkdir(parents=True)
        _move(tmp_err, d / "agent-stderr.log")
        rt.conn.stderr_path = str(d / "agent-stderr.log")
        rec: dict[str, Any] = {
            "session_id": sid,
            # The agent's own session id (dsh). Equal to session_id today; session/resume uses this one.
            "acp_session_id": sid,
            "agent_id": agent_id,
            "cwd": cwd_real,
            "origin": "started",
            "state": "idle",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "permission_mode": mode,
            "permission_policy": policy,
            "config_values": {},
            "config_options": (r or {}).get("configOptions") or [],
            "current_prompt_id": None,
            "prompts": [],
            "pending_permissions": [],
        }
        self._records[sid] = rec
        self._register(rt, rec)
        self._emit(sid, "session_started", {"agentId": agent_id, "cwd": cwd_real, "permissionMode": mode,
                                            "permissionPolicy": policy, "pid": rt.conn.pid})
        self._flush_early(rt)
        self._save(rec)
        notes = []
        for cid, value in (config or {}).items():
            try:
                await self._set_config(rec, rt, cid, value)
            except SessionError as e:
                notes.append(str(e))
        res: dict[str, Any] = {}
        if prompt:
            res = await self.prompt(sid, prompt)
        view = self._summary(rec)
        if notes:
            view["config_errors"] = notes
        if res:
            view["prompt_id"] = res["prompt_id"]
        return view

    async def load_session(
        self,
        session_id: str,
        agent_id: str | None = None,
        cwd: str | None = None,
        permission_mode: str | None = None,
        permission_policy: str | None = None,
    ) -> dict[str, Any]:
        self._dir(session_id)
        async with self._lock(session_id):
            known = session_id in self._records or (self.root / session_id / "session.json").exists()
            if known:
                rec = self._rec(session_id)
                agent = self.registry.get(rec["agent_id"])
                if agent_id and agent_id != rec["agent_id"]:
                    raise SessionError("BAD_ARGUMENT", f"session belongs to agent {rec['agent_id']!r}")
                if permission_mode:
                    rec["permission_mode"] = agent.resolve_mode(permission_mode)
                if permission_policy:
                    rec["permission_policy"] = _validate_policy(permission_policy, rec["permission_policy"])
                    if session_id in self._rt:
                        self._rt[session_id].policy = rec["permission_policy"]
                note = None
                if session_id in self._rt:
                    note = "already live; nothing to load"
                    if self._rt[session_id].mode != rec["permission_mode"]:
                        note += " (the new permission_mode applies from the next prompt; the agent restarts then)"
                else:
                    await self._attach(rec, rec["permission_mode"])
                if rec["state"] == "closed":
                    rec["state"] = "idle"
                self._save(rec)
            else:
                if not agent_id or not cwd:
                    raise SessionError(
                        "BAD_ARGUMENT",
                        "session unknown to the gateway: pass agent_id and the cwd the agent recorded for it "
                        "(see list_sessions(source='agent'))",
                    )
                agent = self.registry.get(agent_id)
                rec = {
                    "session_id": session_id,
                    "acp_session_id": session_id,
                    "agent_id": agent_id,
                    "cwd": _check_cwd(cwd),
                    "origin": "loaded",
                    "state": "idle",
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "permission_mode": agent.resolve_mode(permission_mode),
                    "permission_policy": _validate_policy(permission_policy, self.config.default_permission_policy),
                    "config_values": {},
                    "config_options": [],
                    "current_prompt_id": None,
                    "prompts": [],
                    "pending_permissions": [],
                }
                self._dir(session_id).mkdir(parents=True, exist_ok=True)
                self._records[session_id] = rec
                try:
                    await self._attach(rec, rec["permission_mode"])
                except SessionError:
                    self._records.pop(session_id, None)
                    self._logs.pop(session_id, None)
                    shutil.rmtree(self._dir(session_id), ignore_errors=True)
                    raise
                note = None
            view = self._summary(rec)
            if note:
                view["note"] = note
            return view

    async def prompt(
        self,
        session_id: str,
        text: str,
        permission_mode: str | None = None,
        permission_policy: str | None = None,
        wait_ms: int = 0,
    ) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise SessionError("BAD_ARGUMENT", "text must be a non-empty string")
        t0 = time.monotonic()
        rec = self._rec(session_id)
        async with self._lock(session_id):
            if rec["state"] == "closed":
                raise SessionError("SESSION_CLOSED", "session is closed; call load_session to reopen it first")
            if rec["state"] == "busy":
                raise SessionError(
                    "SESSION_BUSY",
                    f"prompt {rec['current_prompt_id']} is still running in this session (one turn at a time); "
                    "poll get_events until it finishes, or cancel_prompt",
                )
            agent = self.registry.get(rec["agent_id"])
            mode = agent.resolve_mode(permission_mode) if permission_mode else rec["permission_mode"]
            policy = _validate_policy(permission_policy, rec["permission_policy"])
            rt = self._rt.get(session_id)
            resumed = False
            if rt is not None and rt.mode != mode:
                self._emit(session_id, "agent_restart", {"reason": "permission_mode change", "from": rt.mode, "to": mode})
                await self._stop_runtime(rt, close_remote=True)
                rt = None
            if rt is None:
                rt = await self._attach(rec, mode)
                resumed = True
            prompt_id = uuid.uuid4().hex
            git_start = await asyncio.to_thread(gitinfo.snapshot, rec["cwd"])
            pr = {
                "prompt_id": prompt_id,
                "state": "running",
                "text_preview": text[:PREVIEW_CHARS],
                "started_at": now_iso(),
                "finished_at": None,
                "stop_reason": None,
                "error": None,
                "permission_mode": mode,
                "permission_policy": policy,
                "resumed": resumed,
                "first_sequence": self._log(session_id).latest + 1,
                "last_sequence": None,
                "git_start": git_start,
                "git_summary": None,
                "reply_text": "",
                "reply_truncated": False,
                "delivered_at_sequence": None,
                "tool_calls": [],
            }
            rec.setdefault("prompts", []).append(pr)
            del rec["prompts"][:-MAX_PROMPTS_KEPT]
            rec["state"] = "busy"
            rec["current_prompt_id"] = prompt_id
            rt.policy = policy
            rt.prompt_id = prompt_id
            rt.cancel_requested = False
            self._emit(session_id, "prompt_started", {"textPreview": text[:PREVIEW_CHARS], "textChars": len(text),
                                                      "permissionMode": mode, "permissionPolicy": policy,
                                                      "resumed": resumed}, prompt_id)
            self._save(rec)
            try:
                fut = await rt.conn.send_request(
                    "session/prompt", {"sessionId": _acp_id(rec), "prompt": [{"type": "text", "text": text}]})
            except AcpError as e:
                fut = asyncio.get_running_loop().create_future()
                fut.set_exception(e)
            rt.prompt_task = self._spawn_task(self._run_prompt(rec, rt, prompt_id, fut))
        if wait_ms:
            # Total call time (including any agent restart) stays under MAX_PROMPT_WAIT_MS.
            budget = min(max(0, int(wait_ms)), MAX_PROMPT_WAIT_MS) / 1000 - (time.monotonic() - t0)
            if budget > 0:
                await asyncio.wait({rt.prompt_task}, timeout=budget)
        turn = self._turn(rec, self._prompt(rec, prompt_id), deliver=True)
        if turn is not None and resumed:
            turn["resumed"] = True
        return {"session_id": session_id, "prompt_id": prompt_id, "prompt": turn}

    async def _run_prompt(self, rec: dict[str, Any], rt: Runtime, prompt_id: str, fut: asyncio.Future) -> None:
        sid = rec["session_id"]
        stop_reason = None
        error = None
        try:
            r = await fut
            stop_reason = (r or {}).get("stopReason")
            state = "cancelled" if stop_reason == "cancelled" or rt.cancel_requested else "completed"
        except AcpError as e:
            if rt.cancel_requested:
                state = "cancelled"
                if isinstance(e, AgentExited):
                    error = {"message": "agent process stopped by cancel_prompt(force=true)"}
            elif rt.stopping:
                state = "failed"
                error = {"message": rt.stopping}
            else:
                state = "failed"
                error = e.to_dict()
        except asyncio.CancelledError:  # pragma: no cover
            state, error = "failed", {"message": "gateway task cancelled"}
        self._resolve_all_pending(rt, "prompt_finished")
        summary = await asyncio.to_thread(gitinfo.summary, rec["cwd"], (self._prompt(rec, prompt_id) or {}).get("git_start"))
        reply, reply_mid, n_messages = _last_message(rt, prompt_id)
        rt.replies.pop(prompt_id, None)
        tools = list(rt.tools.pop(prompt_id, {}).values())
        raw = reply.encode()
        pr = self._prompt(rec, prompt_id)
        if pr is not None:
            pr.update(
                state=state,
                stop_reason=stop_reason,
                error=error,
                finished_at=now_iso(),
                git_summary=summary,
                reply_text=raw[:MAX_REPLY_STORED].decode("utf-8", errors="ignore"),
                reply_truncated=len(raw) > MAX_REPLY_STORED,
                reply_message_id=reply_mid,
                message_count=n_messages,
                tool_calls=tools,
            )
        if rt.prompt_id == prompt_id:
            rt.prompt_id = None
            rt.prompt_task = None
            rt.cancel_requested = False
        rec["state"] = "idle" if rec["state"] == "busy" else rec["state"]
        rec["current_prompt_id"] = None
        self._emit(sid, "prompt_finished", {"state": state, "stopReason": stop_reason, "error": error}, prompt_id)
        if pr is not None:
            pr["last_sequence"] = self._log(sid).latest
        self._save(rec)

    async def get_events(
        self,
        session_id: str,
        after_sequence: int = 0,
        limit: int = 100,
        max_bytes: int = DEFAULT_EVENT_BYTES,
        include_thoughts: bool = False,
        wait_ms: int = 0,
        reply_bytes: int = DEFAULT_REPLY_BYTES,
        view: str = "final",
        include_text: bool = False,
    ) -> dict[str, Any]:
        if view not in VIEWS:
            raise SessionError("BAD_ARGUMENT", f"view must be one of {VIEWS}")
        rec = self._rec(session_id)
        lg = self._log(session_id)
        after_sequence = max(0, int(after_sequence))
        wait_ms = min(max(0, int(wait_ms)), MAX_WAIT_MS)
        if wait_ms and rec["state"] == "busy":
            deadline = time.monotonic() + wait_ms / 1000
            while rec["state"] == "busy" and not rec.get("pending_permissions"):
                if view != "final" and lg.latest > after_sequence:
                    break  # messages/full: any news ends the wait; final: only the end of the turn or a question
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                ev = self._changed.setdefault(session_id, asyncio.Event())
                try:
                    await asyncio.wait_for(ev.wait(), left)
                except asyncio.TimeoutError:
                    break
        limit = max(1, min(int(limit), 500))
        max_bytes = max(1024, min(int(max_bytes), MAX_EVENT_BYTES))
        events, next_seq, has_more = lg.read(after_sequence, limit, max_bytes, include_thoughts)
        pr = self._last_prompt(rec)
        last = (pr or {}).get("last_sequence")
        covered = bool(last) and after_sequence < last <= next_seq
        out: dict[str, Any] = {
            "session_id": session_id,
            "state": rec["state"],
            "next_sequence": next_seq,
            "has_more": has_more,
            "latest_sequence": lg.latest,
            "prompt": self._turn(rec, pr, deliver=True, include_text=include_text, covered=covered,
                                 reply_bytes=max(0, min(int(reply_bytes), MAX_REPLY_BYTES))),
        }
        pending = self._pending(rec)
        if pending:
            out["pending_permissions"] = pending
        if view == "full":
            out["events"] = events
        elif view == "messages":
            sent_text = (out["prompt"] or {}).get("text") is not None
            final_mid = (pr or {}).get("reply_message_id") if sent_text else None
            out["events"] = self._coalesce(rec, events, final_mid)
        return out

    def _coalesce(self, rec: dict[str, Any], events: list[dict[str, Any]], final_mid: str | None) -> list[dict[str, Any]]:
        """messages view: chunks merged into whole messages, one line per tool call, noise dropped."""
        out: list[dict[str, Any]] = []
        tools: dict[str, dict[str, Any]] = {}
        known = {}
        for p in rec.get("prompts", []):
            for t in p.get("tool_calls") or []:
                known[t["tool_call_id"]] = t
        for rt in self._rt.values():
            for d in rt.tools.values():
                known.update(d)
        for e in events:
            k, u = e["kind"], e.get("update") or {}
            base = {"sequence": e["sequence"], "kind": k}
            if e.get("promptId"):
                base["promptId"] = e["promptId"]
            if k in ("agent_message_chunk", "agent_thought_chunk"):
                kind = "message" if k == "agent_message_chunk" else "thought"
                mid = u.get("messageId")
                content = u.get("content") or {}
                txt = content.get("text", "") if content.get("type") == "text" else f"[{content.get('type')}]"
                prev = out[-1] if out else None
                if prev is not None and prev["kind"] == kind and mid and prev.get("messageId") == mid:
                    prev["text"] += txt
                    prev["sequence"] = e["sequence"]
                else:
                    out.append({**base, "kind": kind, "messageId": mid, "text": txt})
            elif k in ("tool_call", "tool_call_update"):
                tid = str(u.get("toolCallId") or "")
                item = tools.get(tid)
                summ = dict(known.get(tid) or {"title": None, "kind": None, "status": None, "input": None})
                if item is None:
                    item = tools[tid] = {**base, "kind": "tool_call", "toolCallId": tid}
                    out.append(item)
                    item["_s"] = summ
                for f in ("title", "kind", "status"):
                    if u.get(f) is not None:
                        item["_s"][f] = u[f]
                if u.get("rawInput") is not None:
                    item["_s"]["input"] = _one_line(u["rawInput"])
                item["sequence"] = e["sequence"]
            elif k in _QUIET_KINDS:
                continue
            elif k == "plan":
                out.append({**base, "entries": [f"[{x.get('status')}] {x.get('content')}" for x in u.get("entries") or []]})
            elif k == "prompt_started":
                out.append({**base, "textPreview": u.get("textPreview") or str(u.get("text", ""))[:PREVIEW_CHARS]})
            elif k == "permission_request":
                tc = u.get("toolCall") or {}
                out.append({**base, "permissionId": u.get("permissionId"), "policy": u.get("policy"),
                            "tool": _tool_line({"title": tc.get("title"), "kind": tc.get("kind"),
                                                "input": _one_line(tc.get("rawInput")) if tc.get("rawInput") else None})})
            else:
                small = json.dumps(u, ensure_ascii=False)
                out.append({**base, "update": u} if len(small) <= 500 else {**base, "update": {"preview": small[:500]}})
        for item in out:
            s = item.pop("_s", None)
            if s is not None:
                item["line"] = _tool_line(s)
                item["status"] = s.get("status")
            if final_mid and item["kind"] == "message" and item.get("messageId") == final_mid:
                item.pop("text")
                item["textInPrompt"] = True  # the final reply is prompt.text in this same response
        return out

    async def get_tool_call(self, session_id: str, tool_call_id: str, offset: int = 0,
                            max_bytes: int = DEFAULT_TOOL_BYTES) -> dict[str, Any]:
        self._rec(session_id)
        path = self._tool_path(session_id, tool_call_id)
        if not path.exists():
            raise SessionError("NOT_FOUND", f"no tool call {tool_call_id!r} recorded in this session")
        full = json.loads(path.read_text())
        output = _tool_output_text(full).encode()
        offset = max(0, min(int(offset), len(output)))
        max_bytes = max(1024, min(int(max_bytes), MAX_TOOL_BYTES))
        chunk = output[offset:offset + max_bytes].decode("utf-8", errors="ignore")
        end = offset + len(chunk.encode())
        return {
            "session_id": session_id,
            "tool_call_id": full.get("toolCallId", tool_call_id),
            "prompt_id": full.get("promptId"),
            "title": full.get("title"),
            "kind": full.get("kind"),
            "status": full.get("status"),
            "input": clip(full.get("rawInput"), 16 * 1024)[0],
            "locations": full.get("locations"),
            "output": chunk,
            "output_bytes": len(output),
            "offset": offset,
            "next_offset": end,
            "has_more": end < len(output),
            "truncated_on_disk": bool(full.get("truncated")),
        }

    async def cancel_prompt(self, session_id: str, prompt_id: str | None = None, force: bool = False) -> dict[str, Any]:
        rec = self._rec(session_id)
        rt = self._rt.get(session_id)
        current = rec.get("current_prompt_id")
        if rec["state"] != "busy" or rt is None or rt.prompt_task is None:
            return {"session_id": session_id, "state": rec["state"], "note": "no prompt is running in this session"}
        if prompt_id and prompt_id != current:
            raise SessionError("BAD_ARGUMENT", f"prompt {prompt_id} is not the running prompt ({current})")
        rt.cancel_requested = True
        self._emit(session_id, "cancel_requested", {"force": force}, current)
        self._resolve_all_pending(rt, "cancel_prompt")
        try:
            await rt.conn.notify("session/cancel", {"sessionId": _acp_id(rec)})
        except AcpError:
            pass
        if force:
            rt.conn.kill()
        task = rt.prompt_task
        if task is not None:
            await asyncio.wait({task}, timeout=5)
        pr = self._prompt(rec, current) or {}
        view = {"session_id": session_id, "state": rec["state"],
                "prompt": {"prompt_id": current, "state": pr.get("state"), "stop_reason": pr.get("stop_reason")}}
        if rec["state"] == "busy":
            view["note"] = "cancel sent; the agent has not settled yet. Poll get_events, or call again with force=true"
        return view

    async def close_session(self, session_id: str) -> dict[str, Any]:
        rec = self._rec(session_id)
        if rec["state"] == "busy":
            await self.cancel_prompt(session_id)
            rt = self._rt.get(session_id)
            if rec["state"] == "busy" and rt is not None:
                await self.cancel_prompt(session_id, force=True)
        async with self._lock(session_id):
            rt = self._rt.get(session_id)
            if rt is not None:
                await self._stop_runtime(rt, close_remote=True)
            rec["state"] = "closed"
            rec["current_prompt_id"] = None
            self._emit(session_id, "session_closed", {})
            self._save(rec)
            view = {"session_id": session_id, "state": "closed"}
            view["note"] = ("closed; the agent keeps its persisted conversation, load_session reopens it"
                            if "resume" in ((rec.get("agent_capabilities") or {}).get("sessionCapabilities") or {})
                            or (rec.get("agent_capabilities") or {}).get("loadSession") else "closed")
            return view

    async def set_config_option(self, session_id: str, config_id: str, value: str) -> dict[str, Any]:
        rec = self._rec(session_id)
        async with self._lock(session_id):
            if rec["state"] == "closed":
                raise SessionError("SESSION_CLOSED", "session is closed; call load_session first")
            rt = self._rt.get(session_id) or await self._attach(rec, rec["permission_mode"])
            await self._set_config(rec, rt, config_id, value)
            self._save(rec)
            opt = next((o for o in rec.get("config_options") or [] if o.get("id") == config_id), None)
            return {"session_id": session_id, "config_id": config_id,
                    "current_value": (opt or {}).get("currentValue", rec["config_values"].get(config_id)),
                    "config_values": rec.get("config_values")}

    async def _set_config(self, rec: dict[str, Any], rt: Runtime, config_id: str, value: str) -> None:
        opt = next((o for o in rec.get("config_options") or [] if o.get("id") == config_id), None)
        if opt is None:
            raise SessionError("BAD_ARGUMENT", f"agent advertises no config option {config_id!r}; "
                               f"available: {[o.get('id') for o in rec.get('config_options') or []]}")
        choices = _flatten_options(opt)
        if choices:
            match = next((c for c in choices if c.get("value") == value), None) or next(
                (c for c in choices if str(c.get("name", "")).lower() == str(value).lower()), None)
            if match is None:
                raise SessionError("BAD_ARGUMENT", f"{value!r} is not an advertised value for {config_id!r}; "
                                   f"choices: {[c.get('name') for c in choices]}")
            value = match["value"]
        try:
            r = await rt.conn.request("session/set_config_option",
                                      {"sessionId": _acp_id(rec), "configId": config_id, "value": value}, timeout=30)
        except AcpError as e:
            raise SessionError("AGENT_ERROR", f"session/set_config_option failed: {e}") from None
        if (r or {}).get("configOptions") is not None:
            rec["config_options"] = r["configOptions"]
        rec.setdefault("config_values", {})[config_id] = value
        self._emit(rec["session_id"], "config_set", {"configId": config_id, "value": value})

    def resolve_permission(self, session_id: str, permission_id: str, option_id: str) -> dict[str, Any]:
        rec = self._rec(session_id)
        rt = self._rt.get(session_id)
        entry = rt.pending.get(permission_id) if rt else None
        if entry is None:
            raise SessionError("BAD_ARGUMENT", f"no pending permission request {permission_id!r}")
        fut, info = entry
        if not any(o.get("optionId") == option_id for o in info["options"]):
            raise SessionError("BAD_ARGUMENT", f"option {option_id!r} not offered; options: "
                               f"{[o.get('optionId') for o in info['options']]}")
        if not fut.done():
            fut.set_result(({"outcome": "selected", "optionId": option_id}, "caller"))
        return {"session_id": session_id, "state": rec["state"],
                "resolved": {"permission_id": permission_id, "option_id": option_id}}

    async def get_session(self, session_id: str, prompts_limit: int = 10, detail: str = "summary",
                          reply_bytes: int = DEFAULT_REPLY_BYTES) -> dict[str, Any]:
        """summary: state and compact prompt list, no reply text. full: every prompt's reply text (always
        recoverable here), tool lines, git summary, config options, capabilities, process info."""
        if detail not in ("summary", "full"):
            raise SessionError("BAD_ARGUMENT", "detail must be 'summary' or 'full'")
        rec = self._rec(session_id)
        view = self._summary(rec)
        prompts = rec.get("prompts", [])
        n = max(1, min(int(prompts_limit), MAX_PROMPTS_KEPT))
        view.update(origin=rec.get("origin"), created_at=rec.get("created_at"), updated_at=rec.get("updated_at"),
                    prompt_count=len(prompts), latest_sequence=self._log(session_id).latest)
        pending = self._pending(rec)
        if pending:
            view["pending_permissions"] = pending
        items = []
        rb = max(0, min(int(reply_bytes), MAX_REPLY_BYTES))
        for p in prompts[-n:]:
            it = {"prompt_id": p["prompt_id"], "state": p["state"], "stop_reason": p.get("stop_reason"),
                  "started_at": p.get("started_at"), "finished_at": p.get("finished_at"),
                  "prompt_preview": (p.get("text_preview") or "")[:PREVIEW_CHARS],
                  "text_bytes": len((p.get("reply_text") or "").encode()),
                  "delivered_at_sequence": p.get("delivered_at_sequence"),
                  "tool_call_count": len(self._tool_summaries(p))}
            if p.get("error"):
                it["error"] = p["error"]
            if detail == "full":
                raw = (p.get("reply_text") or "").encode()
                it.update(text=raw[:rb].decode("utf-8", errors="ignore"),
                          tool_calls=[{"tool_call_id": t["tool_call_id"], "line": _tool_line(t)}
                                      for t in self._tool_summaries(p)],
                          git_summary=p.get("git_summary"), permission_mode=p.get("permission_mode"),
                          permission_policy=p.get("permission_policy"), resumed=p.get("resumed"),
                          first_sequence=p.get("first_sequence"), last_sequence=p.get("last_sequence"))
                if len(raw) > rb or p.get("reply_truncated"):
                    it["text_truncated"] = True
            items.append(it)
        view["prompts"] = items
        if detail == "full":
            rt = self._rt.get(session_id)
            view.update(acp_session_id=_acp_id(rec), config_options=rec.get("config_options"),
                        config_values=rec.get("config_values"),
                        capabilities=rt.caps if rt else rec.get("agent_capabilities"),
                        agent_info=rt.agent_info if rt else rec.get("agent_info"),
                        agent_pid=rt.conn.pid if rt and rt.conn else None,
                        live_permission_mode=rt.mode if rt else None,
                        available_permission_modes=sorted(self.registry.get(rec["agent_id"]).permission_modes))
        return view

    async def list_sessions(
        self,
        cwd: str | None = None,
        state: str | None = None,
        limit: int = 20,
        source: str = "gateway",
        agent_id: str = "dsh",
        cursor: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        if source == "agent":
            return await self._list_agent_sessions(agent_id, cwd, cursor, limit)
        if source != "gateway":
            raise SessionError("BAD_ARGUMENT", "source must be 'gateway' or 'agent'")
        if state is not None and state not in ("idle", "busy", "closed"):
            raise SessionError("BAD_ARGUMENT", "state must be idle, busy or closed")
        want_cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else None
        recs = [r for r in self._records.values()
                if (state is None or r["state"] == state) and (want_cwd is None or r["cwd"] == want_cwd)]
        recs.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        items = []
        for r in recs[:limit]:
            v = self._summary(r)
            v["updated_at"] = r.get("updated_at")
            v["prompt_count"] = len(r.get("prompts", []))
            lp = self._last_prompt(r)
            v["last_prompt"] = None if lp is None else {
                "prompt_id": lp["prompt_id"], "state": lp["state"], "finished_at": lp.get("finished_at"),
                "prompt_preview": (lp.get("text_preview") or "")[:PREVIEW_CHARS]}
            items.append(v)
        return {"source": "gateway", "total_matching": len(recs), "returned": len(items), "sessions": items}

    async def _list_agent_sessions(self, agent_id: str, cwd: str | None, cursor: str | None, limit: int) -> dict[str, Any]:
        agent = self.registry.get(agent_id)
        where = _check_cwd(cwd) if cwd else str(Path.home())
        err = Path(self.config.state_dir) / f".list-{uuid.uuid4().hex}.stderr.log"
        rt = await self._spawn(agent, where, agent.default_permission_mode, err)
        try:
            if "list" not in (rt.caps.get("sessionCapabilities") or {}):
                raise SessionError("NOT_SUPPORTED", f"agent {agent_id!r} does not advertise session/list")
            params: dict[str, Any] = {}
            if cwd:
                params["cwd"] = where
            if cursor:
                params["cursor"] = cursor
            r = await rt.conn.request("session/list", params, timeout=self.config.startup_timeout) or {}
        except AcpError as e:
            raise SessionError("AGENT_ERROR", f"session/list failed: {e}") from None
        finally:
            rt.conn.closed = True
            await rt.conn.stop(grace=2)
            _move(err, None)
        sessions = []
        for s in (r.get("sessions") or [])[:limit]:
            sid = s.get("sessionId")
            known = self._records.get(sid)
            sessions.append({**s, "session_id": sid, "known_to_gateway": known is not None,
                             "gateway_state": known["state"] if known else None})
        return {"source": "agent", "agent_id": agent_id, "returned": len(sessions),
                "next_cursor": r.get("nextCursor"), "sessions": sessions}


# ---------------------------------------------------------------------- helpers
def _last_message(rt: Runtime, prompt_id: str) -> tuple[str, str | None, int]:
    """(text of the turn's last non-empty agent message, its messageId, number of messages)."""
    agg = rt.replies.get(prompt_id)
    if not agg:
        return "", None, 0
    for m in reversed(agg["order"]):
        if agg["text"][m].strip():
            return agg["text"][m], (None if m == "_" else m), len(agg["order"])
    return "", None, len(agg["order"])


_INPUT_KEYS = ("command", "cmd", "path", "file_path", "filePath", "pattern", "query", "url", "glob")


def _one_line(raw: Any, limit: int = 120) -> str:
    """Short single-line rendering of a tool's input (command / path / pattern ...)."""
    if isinstance(raw, dict):
        parts = [str(raw[k]) for k in _INPUT_KEYS if raw.get(k) not in (None, "")]
        text = " ".join(parts) if parts else json.dumps(raw, ensure_ascii=False)
    elif isinstance(raw, str):
        text = raw
    else:
        text = json.dumps(raw, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_line(t: dict[str, Any]) -> str:
    name = t.get("title") or t.get("kind") or "tool"
    line = f"{name}: {t['input']}" if t.get("input") else str(name)
    return f"{line} → {t['status']}" if t.get("status") else line


def _tool_output_text(full: dict[str, Any]) -> str:
    parts = []
    for c in full.get("content") or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "content":
            inner = c.get("content") or {}
            parts.append(inner.get("text") if inner.get("type") == "text" else json.dumps(inner, ensure_ascii=False))
        elif c.get("type") == "diff":
            parts.append(f"--- diff {c.get('path')}\n{c.get('newText') or ''}")
        elif c.get("type") == "terminal":
            parts.append(f"[terminal {c.get('terminalId')}]")
        else:
            parts.append(json.dumps(c, ensure_ascii=False))
    if full.get("rawOutput") is not None:
        ro = full["rawOutput"]
        parts.append(ro if isinstance(ro, str) else json.dumps(ro, ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def _check_cwd(cwd: str | None) -> str:
    if not cwd or not isinstance(cwd, str):
        raise SessionError("BAD_ARGUMENT", "cwd is required (absolute path of an existing directory)")
    p = os.path.expanduser(cwd)
    if not os.path.isabs(p):
        raise SessionError("BAD_ARGUMENT", "cwd must be an absolute path")
    if not os.path.isdir(p):
        raise SessionError("BAD_ARGUMENT", f"cwd does not exist or is not a directory: {p}")
    return os.path.realpath(p)


def _append_text(path: Path, text: str) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _move(src: Path, dst: Path | None) -> None:
    try:
        if dst is None:
            src.unlink()
        else:
            os.replace(src, dst)
    except OSError:
        pass


def _acp_id(rec: dict[str, Any]) -> str:
    """Gateway session id -> agent (ACP) session id, as persisted in session.json."""
    return rec.get("acp_session_id") or rec["session_id"]


def _kill_orphan(pid: int | None, argv: list[str]) -> bool:
    """Kill an agent process left from a previous gateway run (only if it is clearly ours)."""
    if not pid or not argv:
        return False
    try:
        out = subprocess.run(["ps", "-o", "pgid=,command=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    if not out:
        return False
    pgid, _, command = out.partition(" ")
    command = command.strip()
    if pgid.strip() != str(pid) or os.path.basename(argv[0]) not in command or " ".join(argv[1:]) not in command:
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return True
        for _ in range(20):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
    return True

"""Minimal ACP (Agent Client Protocol) client over stdio: newline-delimited JSON-RPC 2.0.

One AcpConnection = one agent process. The agent runs in its own process group
so it can be stopped as a unit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Any, Awaitable, Callable

log = logging.getLogger("dot_gateway.acp")

PROTOCOL_VERSION = 1
STREAM_LIMIT = 64 * 1024 * 1024  # max bytes of one JSON-RPC line from the agent


class AcpError(Exception):
    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        return {"message": str(self), "code": self.code, "data": self.data}


class AgentExited(AcpError):
    pass


RequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]
NotificationHandler = Callable[[str, dict[str, Any]], None]
ExitHandler = Callable[[int | None], None]


class AcpConnection:
    def __init__(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str],
        stderr_path: str,
        on_notification: NotificationHandler,
        on_request: RequestHandler,
        on_exit: ExitHandler,
        on_noise: Callable[[str], None] | None = None,
    ):
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.stderr_path = stderr_path
        self.on_notification = on_notification
        self.on_request = on_request
        self.on_exit = on_exit
        self.on_noise = on_noise or (lambda line: None)
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._reader: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self.closed = False
        self.returncode: int | None = None
        self.exited = asyncio.Event()

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    async def start(self) -> None:
        with open(self.stderr_path, "ab") as err:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=self.cwd,
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=err,
                start_new_session=True,
                limit=STREAM_LIMIT,
            )
        self._reader = asyncio.create_task(self._read_loop(), name=f"acp-reader-{self.proc.pid}")

    async def _send(self, msg: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None or self.exited.is_set():
            raise AgentExited("agent process is not running")
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode()
        async with self._write_lock:
            try:
                self.proc.stdin.write(data)
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                raise AgentExited(f"agent process stdin closed: {e}") from None

    async def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if timeout is None:
                return await fut
            try:
                return await asyncio.wait_for(asyncio.shield(fut), timeout)
            except asyncio.TimeoutError:
                raise AcpError(f"{method}: no answer from agent within {timeout:.0f}s") from None
        finally:
            self._pending.pop(rid, None)

    async def send_request(self, method: str, params: dict[str, Any]) -> asyncio.Future:
        """Send a request now and return the future of its result (so later messages are ordered after it)."""
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        fut.add_done_callback(lambda _f: self._pending.pop(rid, None))
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except AcpError:
            self._pending.pop(rid, None)
            raise
        return fut

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        stdout = self.proc.stdout
        try:
            while True:
                try:
                    line = await stdout.readline()
                except ValueError:  # line longer than STREAM_LIMIT
                    self.on_noise("[dot-gateway] dropped an over-long line from agent stdout")
                    continue
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    self.on_noise(text)  # e.g. dsh prints a model-listing log line on stdout
                    continue
                if not isinstance(msg, dict):
                    self.on_noise(text[:500])
                    continue
                self._dispatch(msg)
        finally:
            self.returncode = await self.proc.wait()
            self.exited.set()
            err = AgentExited(f"agent process exited (code {self.returncode})")
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(err)
            try:
                self.on_exit(self.returncode)
            except Exception:  # pragma: no cover
                log.exception("on_exit handler failed")

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" in msg and "id" in msg:
            t = asyncio.create_task(self._handle_request(msg))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        elif "method" in msg:
            try:
                self.on_notification(msg["method"], msg.get("params") or {})
            except Exception:  # pragma: no cover
                log.exception("notification handler failed")
        elif "id" in msg:
            fut = self._pending.get(msg["id"])
            if fut is None or fut.done():
                return
            if msg.get("error") is not None:
                e = msg["error"] or {}
                fut.set_exception(AcpError(str(e.get("message", "agent error")), e.get("code"), e.get("data")))
            else:
                fut.set_result(msg.get("result"))

    async def _handle_request(self, msg: dict[str, Any]) -> None:
        try:
            result = await self.on_request(msg["method"], msg.get("params") or {})
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except AcpError as e:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": e.code or -32603, "message": str(e)}}
        except Exception as e:  # pragma: no cover
            log.exception("request handler failed")
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}}
        try:
            await self._send(reply)
        except AgentExited:
            pass

    async def stop(self, grace: float = 5.0) -> None:
        """Close stdin (ACP agents serve until the client disconnects), then TERM, then KILL the group."""
        self.closed = True
        if self.proc is None:
            return
        if not self.exited.is_set():
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.exited.wait(), grace)
            except asyncio.TimeoutError:
                self._signal_group(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.exited.wait(), grace)
                except asyncio.TimeoutError:
                    self._signal_group(signal.SIGKILL)
                    await self.exited.wait()
        self._signal_group(signal.SIGKILL)  # leftover descendants in the agent's group

    def kill(self) -> None:
        self._signal_group(signal.SIGKILL)

    def _signal_group(self, sig: int) -> None:
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

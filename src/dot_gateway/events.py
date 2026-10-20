"""Append-only, file-backed event journal for one session (events.jsonl).

Sequences are dense integers starting at 1. Events are clipped when written
(8 KiB per string, 32 KiB per event) so one page stays small.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_STR = 8 * 1024
MAX_EVENT = 32 * 1024
THOUGHT_KINDS = {"agent_thought_chunk"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def clip(obj: Any, max_str: int = MAX_STR) -> tuple[Any, bool]:
    """Return (clipped copy, whether anything was cut)."""
    cut = False

    def walk(o: Any) -> Any:
        nonlocal cut
        if isinstance(o, str):
            if len(o) > max_str:
                cut = True
                return o[:max_str] + f"…[truncated {len(o) - max_str} chars]"
            return o
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o

    return walk(obj), cut


class EventLog:
    def __init__(self, path: Path):
        self.path = path
        self._offsets: list[int] | None = None

    def _index(self) -> list[int]:
        if self._offsets is None:
            offsets: list[int] = []
            if self.path.exists():
                with open(self.path, "rb") as f:
                    pos = 0
                    for line in f:
                        if not line.endswith(b"\n"):  # torn write from a crash: drop it
                            with open(self.path, "r+b") as g:
                                g.truncate(pos)
                            break
                        offsets.append(pos)
                        pos += len(line)
            self._offsets = offsets
        return self._offsets

    @property
    def latest(self) -> int:
        return len(self._index())

    def append(self, kind: str, data: Any = None, prompt_id: str | None = None) -> dict[str, Any]:
        idx = self._index()
        data, cut = clip(data)
        ev: dict[str, Any] = {"sequence": len(idx) + 1, "timestamp": now_iso(), "kind": kind}
        if prompt_id:
            ev["promptId"] = prompt_id
        ev["update"] = data
        if cut:
            ev["truncated"] = True
        line = json.dumps(ev, ensure_ascii=False)
        if len(line.encode()) > MAX_EVENT:
            ev["update"] = {"preview": json.dumps(data, ensure_ascii=False)[:MAX_STR]}
            ev["truncated"] = True
            line = json.dumps(ev, ensure_ascii=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "ab") as f:
            pos = f.tell()
            f.write(line.encode() + b"\n")
        idx.append(pos)
        return ev

    def read(
        self, after: int, limit: int, max_bytes: int, include_thoughts: bool
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Events with seq > after. Returns (events, next_sequence, has_more).

        `limit` counts scanned events (hidden thoughts included), like acp-mcp;
        pass next_sequence back as after_sequence to continue.
        """
        idx = self._index()
        total = len(idx)
        after = max(0, min(after, total))
        events: list[dict[str, Any]] = []
        size = 0
        next_seq = after
        if after < total:
            with open(self.path, "rb") as f:
                f.seek(idx[after])
                for _ in range(limit):
                    line = f.readline()
                    if not line:
                        break
                    ev = json.loads(line)
                    if not include_thoughts and ev["kind"] in THOUGHT_KINDS:
                        next_seq = ev["sequence"]
                        continue
                    if events and size + len(line) > max_bytes:
                        break
                    events.append(ev)
                    size += len(line)
                    next_seq = ev["sequence"]
        return events, next_seq, next_seq < total


def atomic_write_json(path: Path, data: Any) -> None:
    """Write-then-rename with fsync, so the file is intact after a crash or power loss (machine reboot)."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass

"""ACP agent registry.

dsh is built in. Any stdio ACP agent can be added (or dsh overridden) in the
gateway config file, e.g. pi through pi-acp:

    {"agents": {"pi": {"command": "pi-acp", "args": [], "description": "pi via pi-acp"}}}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config

DSH_MODES = ("read-only", "workspace-write", "danger-full-access")


class AgentError(ValueError):
    pass


@dataclass
class AgentDefinition:
    id: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""
    # Launch variants selected by start_session/prompt(permission_mode=...):
    # each maps to extra env and args for the agent process.
    permission_modes: dict[str, dict[str, Any]] = field(default_factory=dict)
    default_permission_mode: str | None = None

    def launch(self, mode: str | None, base_env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        argv = [self.command, *self.args]
        env = dict(base_env)
        env.update(self.env)
        if mode is not None:
            m = self.permission_modes[mode]
            argv += list(m.get("args") or [])
            env.update({str(k): str(v) for k, v in (m.get("env") or {}).items()})
        return argv, env

    def resolve_mode(self, mode: str | None) -> str | None:
        if mode is None:
            return self.default_permission_mode
        if mode not in self.permission_modes:
            raise AgentError(
                f"permission_mode must be one of {sorted(self.permission_modes)} for agent {self.id!r}"
                if self.permission_modes
                else f"agent {self.id!r} defines no permission modes"
            )
        return mode

    def info(self) -> dict[str, Any]:
        return {
            "agent_id": self.id,
            "command": [self.command, *self.args],
            "description": self.description,
            "permission_modes": sorted(self.permission_modes),
            "default_permission_mode": self.default_permission_mode,
            "env_keys": sorted(self.env),
        }


def builtin_dsh(config: Config) -> AgentDefinition:
    return AgentDefinition(
        id="dsh",
        command=config.dsh_bin,
        args=["--profile", "acp"],
        description="DeepSeek Harness over ACP (dsh --profile acp). Supports session list/resume/close, not session/load.",
        permission_modes={m: {"env": {"DSH_PERMISSION_MODE": m}} for m in DSH_MODES},
        default_permission_mode=config.dsh_permission_mode or "workspace-write",
    )


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}

    def register(self, agent: AgentDefinition) -> None:
        self._agents[agent.id] = agent

    def get(self, agent_id: str) -> AgentDefinition:
        try:
            return self._agents[agent_id]
        except KeyError:
            raise AgentError(f"unknown agent {agent_id!r}; available: {sorted(self._agents)}") from None

    def ids(self) -> list[str]:
        return sorted(self._agents)

    def infos(self) -> list[dict[str, Any]]:
        return [self._agents[i].info() for i in self.ids()]


def _from_json(agent_id: str, spec: dict[str, Any], base: AgentDefinition | None) -> AgentDefinition:
    if not isinstance(spec, dict):
        raise AgentError(f"agent {agent_id!r}: definition must be an object")
    if base is None and not spec.get("command"):
        raise AgentError(f"agent {agent_id!r}: 'command' is required")
    d = AgentDefinition(
        id=agent_id,
        command=spec.get("command", base.command if base else ""),
        args=list(spec.get("args", base.args if base else [])),
        env={str(k): str(v) for k, v in spec.get("env", base.env if base else {}).items()},
        description=spec.get("description", base.description if base else ""),
        permission_modes=dict(spec.get("permission_modes", base.permission_modes if base else {})),
        default_permission_mode=spec.get("default_permission_mode", base.default_permission_mode if base else None),
    )
    if d.default_permission_mode is not None and d.default_permission_mode not in d.permission_modes:
        raise AgentError(f"agent {agent_id!r}: default_permission_mode not in permission_modes")
    return d


def load_registry(config: Config) -> AgentRegistry:
    reg = AgentRegistry()
    reg.register(builtin_dsh(config))
    path = Path(config.config_file)
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except ValueError as e:
            raise AgentError(f"config file {path} is not valid JSON: {e}") from None
        for agent_id, spec in (data.get("agents") or {}).items():
            reg.register(_from_json(agent_id, spec, reg._agents.get(agent_id)))
    extra = os.environ.get("DOT_GATEWAY_AGENTS_JSON")
    if extra:
        for agent_id, spec in json.loads(extra).items():
            reg.register(_from_json(agent_id, spec, reg._agents.get(agent_id)))
    return reg

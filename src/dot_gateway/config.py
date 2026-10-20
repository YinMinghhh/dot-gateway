"""Gateway configuration (env vars, CLI flags, optional JSON file). None of these is a security limit."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PERMISSION_POLICIES = ("read_only", "operator", "allow_all", "deny_all")


def _default_state_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "dot-gateway"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/mcp"
    state_dir: Path = field(default_factory=_default_state_dir)
    # JSON file with extra/overridden agents and defaults; default <state_dir>/config.json
    config_file: Path | None = None
    dsh_bin: str = "/opt/homebrew/bin/dsh"
    # Default dsh permission mode for new sessions (empty = workspace-write, dsh's own default).
    dsh_permission_mode: str = ""
    # How the gateway answers ACP session/request_permission unless a call overrides it.
    default_permission_policy: str = "read_only"
    # Prepended to PATH for agent processes (launchd's PATH is bare; dsh needs `env node`).
    extra_path: str = "/opt/homebrew/bin:/usr/local/bin"
    # Per-step limit (initialize, session/new|resume|list): calls that start an agent stay well under ~60 s.
    startup_timeout: float = 25.0
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.config_file is None:
            self.config_file = Path(self.state_dir) / "config.json"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        state = Path(env["DOT_GATEWAY_STATE_DIR"]).expanduser() if env.get("DOT_GATEWAY_STATE_DIR") else _default_state_dir()
        c = cls(state_dir=state)
        if env.get("DOT_GATEWAY_CONFIG"):
            c.config_file = Path(env["DOT_GATEWAY_CONFIG"]).expanduser()
        c.apply_file_defaults()
        c.host = env.get("DOT_GATEWAY_HOST", c.host)
        c.port = int(env.get("DOT_GATEWAY_PORT", c.port))
        c.path = env.get("DOT_GATEWAY_PATH", c.path)
        c.dsh_bin = env.get("DOT_GATEWAY_DSH_BIN", c.dsh_bin)
        c.dsh_permission_mode = env.get("DOT_GATEWAY_DSH_PERMISSION_MODE", c.dsh_permission_mode)
        c.default_permission_policy = env.get("DOT_GATEWAY_PERMISSION_POLICY", c.default_permission_policy)
        c.extra_path = env.get("DOT_GATEWAY_EXTRA_PATH", c.extra_path)
        c.startup_timeout = float(env.get("DOT_GATEWAY_STARTUP_TIMEOUT", c.startup_timeout))
        c.log_level = env.get("DOT_GATEWAY_LOG_LEVEL", c.log_level).upper()
        return c

    def apply_file_defaults(self) -> None:
        """Optional "defaults" block in the config file; env vars and flags override it."""
        p = Path(self.config_file)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text())
        except ValueError:
            return
        d = data.get("defaults") or {}
        self.default_permission_policy = d.get("permission_policy", self.default_permission_policy)
        self.dsh_permission_mode = d.get("dsh_permission_mode", self.dsh_permission_mode)
        self.dsh_bin = d.get("dsh_bin", self.dsh_bin)

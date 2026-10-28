"""Runtime dependencies passed to every node."""
from __future__ import annotations

import base64
import os
import pathlib
from dataclasses import dataclass, field

from factory.breaker import Breaker
from factory.config import env, secret


@dataclass
class Deps:
    dsn: str
    workspace_root: pathlib.Path
    broker_url: str
    _breakers: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Deps":
        return cls(dsn=env("FACTORY_DB_DSN"),
                   workspace_root=pathlib.Path(os.environ.get("WORKSPACE_ROOT", "/workspace")),
                   broker_url=env("BROKER_URL", "http://sandbox-broker:8080"))

    def breaker_for(self, run_id: str) -> Breaker:
        if run_id not in self._breakers:
            self._breakers[run_id] = Breaker(self.dsn, run_id)
        return self._breakers[run_id]

    def gateway_for(self, state):
        from factory.gateway import TriageGateway
        return TriageGateway(self.breaker_for(state["run_id"]), dict(state.get("model_pins") or {}))

    def run_dir(self, state) -> pathlib.Path:
        return self.workspace_root / "runs" / state["run_id"]

    def repo_path(self, state) -> pathlib.Path:
        return self.run_dir(state) / "repo"

    def git_auth_env(self) -> dict[str, str]:
        """GitHub auth for git via env-scoped config, so the token never lands in a remote URL or argv."""
        token = secret("github_token")
        if not token:
            return {}
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}", "GIT_TERMINAL_PROMPT": "0"}

    def github_token(self) -> str | None:
        return secret("github_token")

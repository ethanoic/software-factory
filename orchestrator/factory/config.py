"""Environment and policy loading. Everything is read lazily so modules import cleanly in tests."""
from __future__ import annotations

import functools
import os
import pathlib

import yaml


@functools.lru_cache(maxsize=1)
def policy() -> dict:
    """pool.yaml: roles, routing and budgets. Contains no model IDs except optional manual pins."""
    path = os.environ.get("POOL_FILE", "/app/pool.yaml")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    _validate(data)
    return data


def _validate(p: dict) -> None:
    required = ["discovery", "providers", "eligibility", "roles", "qualification", "local_mode", "anthropic_route"]
    missing = [k for k in required if k not in p]
    if missing:
        raise SystemExit(f"pool.yaml is missing keys: {missing}")
    if p["local_mode"] not in ("off", "assist", "generate"):
        raise SystemExit("pool.yaml: local_mode must be off | assist | generate")
    if p["anthropic_route"] not in ("subscription", "api_key"):
        raise SystemExit("pool.yaml: anthropic_route must be subscription | api_key")
    for role, cfg in p["roles"].items():
        if cfg.get("tier") not in ("t2", "t3"):
            raise SystemExit(f"pool.yaml: role {role} needs tier t2 or t3")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"missing environment variable {name}")
    return value


def secrets_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("SECRETS_DIR", "/run/secrets"))


def secret(name: str) -> str | None:
    """Docker secrets are files under /run/secrets; fall back to an env var of the same name.
    An empty file (created by `make init` as a placeholder) counts as unset."""
    p = secrets_dir() / name
    if p.is_file() and p.read_text().strip():
        return p.read_text().strip()
    return os.environ.get(name.upper())


CLAUDE_TOKEN_LIFETIME_DAYS = 365          # `claude setup-token` issues a one-year token


def claude_oauth_token() -> tuple[str | None, int | None]:
    """(token, created_epoch) for the Claude subscription route.

    Source order: the `claude_oauth_token` Docker secret written by scripts/claude-token.sh (JSON with a
    creation time, or a bare token), then the CLAUDE_CODE_OAUTH_TOKEN variable from older setups."""
    p = secrets_dir() / "claude_oauth_token"
    raw = p.read_text().strip() if p.is_file() else ""
    if raw.startswith("{"):
        import json
        try:
            data = json.loads(raw)
        except ValueError:
            raise SystemExit("secrets/claude_oauth_token is not valid JSON: run `make claude-token` again")
        created = data.get("created_epoch")
        return (data.get("token") or None), (int(created) if created else None)
    if raw:
        return raw, None
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or None, None


def claude_token_warning(now: float | None = None, warn_days: int = 30) -> str | None:
    """A message when the Claude plan token is missing or close to expiry; None when all is well,
    when the factory is not on the subscription route, or when the token's age is unknown."""
    import time
    try:
        if policy().get("anthropic_route") != "subscription":
            return None
    except SystemExit:
        return None
    token, created = claude_oauth_token()
    if not token:
        return "No Claude plan token is stored. On the Mac, run: make claude-token"
    if created is None:
        return None
    left = CLAUDE_TOKEN_LIFETIME_DAYS - int(((now or time.time()) - created) // 86400)
    if left <= 0:
        return "The Claude plan token has expired. On the Mac, run: make claude-token"
    if left <= warn_days:
        return f"The Claude plan token expires in about {left} days. On the Mac, run: make claude-token"
    return None


RUN_CAP_USD = float(os.environ.get("RUN_CAP_USD", "2.00"))
DAY_CAP_USD = float(os.environ.get("DAY_CAP_USD", "10.00"))
MAX_REPAIRS = 3
RUN_WALL_S = 45 * 60
LOCAL_CTX_BUDGET = 24_000

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


def secret(name: str) -> str | None:
    """Docker secrets are files under /run/secrets; fall back to an env var of the same name."""
    p = pathlib.Path("/run/secrets") / name
    if p.exists():
        return p.read_text().strip()
    return os.environ.get(name.upper())


RUN_CAP_USD = float(os.environ.get("RUN_CAP_USD", "2.00"))
DAY_CAP_USD = float(os.environ.get("DAY_CAP_USD", "10.00"))
MAX_REPAIRS = 3
RUN_WALL_S = 45 * 60
LOCAL_CTX_BUDGET = 24_000

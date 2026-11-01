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


# ------------------------------------------------------------------ redaction and self-protection
import base64 as _b64
import re as _re

_SECRET_PATTERNS = [
    _re.compile(r"(?i)(api[_-]?key|token|secret|password)(\s*[=:]\s*)\S+"),             # KEY=value
    _re.compile(r"(?i)(\"[A-Za-z_]*(?:token|secret|key|password)[A-Za-z_]*\"\s*:\s*)\"[^\"]*\""),  # JSON
    _re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}"),                       # auth headers
    _re.compile(r"\b(sk-ant-|sk-or-|ghp_|gho_|ghs_|ghu_|github_pat_)[A-Za-z0-9_-]{6,}"),     # known prefixes
]
_SECRET_ENV = _re.compile(r"(?i)(key|token|secret|password|dsn)$")


def _secret_values() -> list[str]:
    vals = [secret("github_token"), claude_oauth_token()[0]]
    vals += [v for k, v in os.environ.items() if _SECRET_ENV.search(k)]
    out = set()
    for v in vals:
        if v and len(v) >= 8:
            out.add(v)
            out.add(_b64.b64encode(v.encode()).decode())
            out.add(_b64.b64encode(f"x-access-token:{v}".encode()).decode())
    return sorted(out, key=len, reverse=True)


def redact(text: str) -> str:
    """Remove secrets from text bound for a prompt, trace, comment or PR: first the literal values the
    factory holds (and their base64 forms), then common secret shapes as a second pass."""
    if not text:
        return text
    try:
        values = _secret_values()
    except SystemExit:                     # a malformed token file must not stop redaction
        values = []
    for v in values:
        text = text.replace(v, "<redacted>")
    for i, pat in enumerate(_SECRET_PATTERNS):
        text = pat.sub((lambda m: f"{m.group(1)}{m.group(2)}<redacted>") if i in (0, 2)
                       else (lambda m: f"{m.group(1)}\"<redacted>\"") if i == 1
                       else (lambda m: f"{m.group(1)}<redacted>"), text)
    return text


def factory_repos() -> set[str]:
    """The factory's own GitHub repo(s). Runs against these are refused: the factory is changed by a
    reviewed pull request and `make up`, never by itself."""
    raw = os.environ.get("FACTORY_SELF_REPOS", "ethanoic/software-factory")
    return {r.strip().lower() for r in raw.split(",") if r.strip()}


def is_factory_repo(repo: str) -> bool:
    return repo.strip().lower() in factory_repos()

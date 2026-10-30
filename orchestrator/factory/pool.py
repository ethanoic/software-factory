"""Model pool: discovery, selection and provider clients (spec §5.2).

No model ID is hardcoded. The nightly worker discovers models, qualification scores them per role,
and select() picks the best qualified, available model for a role at run time.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import pathlib
import re
import tempfile
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import psycopg

from factory.config import env, policy

HOSTSTAT = pathlib.Path(os.environ.get("HOSTSTAT_FILE", "/hoststat/hoststat.json"))
SUITE_ROOT = pathlib.Path(os.environ.get("QUALIFICATION_DIR", "/app/qualification"))


class NoQualifiedModel(Exception):
    """No qualified, available model for a role. The breaker turns this into a halt + alert."""


class QuotaExhausted(Exception):
    """Claude plan usage limit reached. Not a breaker trip: the run parks at its checkpoint."""


@dataclass(frozen=True)
class ModelSpec:
    model_id: str              # canonical catalog id, e.g. "anthropic/<model>" or "local/<alias>"
    api_id: str                # id sent on the chosen route
    route: str                 # "omlx" | "anthropic" | "anthropic_subscription" | "google" | "openai" | "openrouter"
    tier: str
    in_per_m: float
    out_per_m: float
    ctx: int
    bills_reasoning_separately: bool = True
    billed: bool = True        # False = covered by a flat subscription or local: $0 marginal

    @property
    def langfuse_name(self) -> str:
        return self.model_id

    @property
    def backend(self) -> str:
        return self.route

    def list_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.in_per_m + output_tokens * self.out_per_m) / 1_000_000

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return self.list_cost(input_tokens, output_tokens) if self.billed else 0.0


# ---------------------------------------------------------------- id matching
def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def match_id(short: str, ids: dict[str, str]) -> str | None:
    """Catalog ids omit release dates ("claude-x-4.5" -> "claude-x-4-5"); provider ids may add one
    ("claude-x-4-5-20251001"). Accept exact, or exact + "-YYYYMMDD"; prefer exact, then newest date."""
    hits = [n for n in ids
            if n == short or (n.startswith(short) and re.fullmatch(r"-\d{8}", n[len(short):]))]
    hits.sort(key=lambda n: (n != short, -int(n[len(short) + 1:] or 0)))
    return ids[hits[0]] if hits else None


SUITE_FOR = {"plan": "code", "generate_remote": "code", "repair": "code", "security_fix": "code",
             "generate": "code", "classify": "triage", "spillover": "triage"}


def suite_dir(role: str) -> pathlib.Path:
    return SUITE_ROOT / SUITE_FOR.get(role, role)


def suite_sha(role: str) -> str:
    """Hash of the qualification suite for a role. A changed suite invalidates old scores."""
    root = suite_dir(role)
    h = hashlib.sha256(role.encode())
    if root.exists():
        for p in sorted(root.rglob("*")):
            if p.is_file():
                h.update(p.relative_to(root).as_posix().encode())
                h.update(p.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------- provider adapters
class AnthropicChatAdapter:
    """Native Anthropic Messages API (Console API key) behind the small OpenAI-style surface the
    gateway uses. Native, not the OpenAI-compatible endpoint, so prompt caching is reported."""
    CACHE_WRITE, CACHE_READ = 1.25, 0.10       # multipliers on the input price (5-minute cache)

    def __init__(self, api_key: str):
        import anthropic
        self._anthropic = anthropic
        self._c = anthropic.Anthropic(api_key=api_key, timeout=120, max_retries=2)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=lambda: list(self._c.models.list()))

    def _create(self, *, model, messages, max_tokens, **_ignored):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
        if turns:   # cache breakpoint on the stable prefix: system + first user turn (spec, frozen tests)
            turns[0]["content"] = [{"type": "text", "text": turns[0]["content"],
                                    "cache_control": {"type": "ephemeral"}}]
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": turns}
        if system:
            kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        r = self._c.messages.create(**kwargs)
        u = r.usage
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        effective_in = round(u.input_tokens + self.CACHE_WRITE * cw + self.CACHE_READ * cr)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="".join(b.text for b in r.content if b.type == "text")))],
            usage=SimpleNamespace(prompt_tokens=effective_in,     # price-weighted; raw counts below
                                  completion_tokens=u.output_tokens, completion_tokens_details=None,
                                  raw={"input": u.input_tokens, "cache_write": cw, "cache_read": cr}))


class SubscriptionAdapter:
    """Claude Pro/Max through the Claude Agent SDK, signed in with the plan
    (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`). Single-shot completion with every tool
    disabled, so the gateway's request -> diff contract is unchanged. Usage draws from the plan's
    limits, not API billing. Verify option names against the pinned claude-agent-sdk version."""
    NO_TOOLS = ["Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep",
                "LS", "WebFetch", "WebSearch", "Task", "NotebookEdit", "TodoWrite"]

    def __init__(self):
        if os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is set; it would take precedence over the plan login")
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            raise SystemExit("CLAUDE_CODE_OAUTH_TOKEN missing: run `claude setup-token` on the Mac")
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=lambda: [])    # no list endpoint; qualification probes instead

    def _create(self, *, model, messages, max_tokens, **_ignored):
        return asyncio.run(self._run(model, messages))

    async def _run(self, model, messages):
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        prompt = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
        opts = ClaudeAgentOptions(model=model, system_prompt=system, max_turns=1,
                                  tools=[],                              # disable every built-in tool
                                  allowed_tools=[], disallowed_tools=self.NO_TOOLS,   # belt and braces
                                  setting_sources=[], cwd=tempfile.mkdtemp(prefix="sdk-"))
        text, result = [], None
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                text += [b.text for b in msg.content if isinstance(b, TextBlock)]
            elif isinstance(msg, ResultMessage):
                result = msg
        if result is None or result.is_error:
            detail = str(getattr(result, "result", "") or "no result")
            if re.search(r"(?i)usage limit|rate limit|limit reached", detail):
                raise QuotaExhausted(detail)
            raise RuntimeError(f"agent sdk error: {detail}")
        u = result.usage or {}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="".join(text)))],
            usage=SimpleNamespace(
                prompt_tokens=u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                + u.get("cache_read_input_tokens", 0),
                completion_tokens=u.get("output_tokens", 0), completion_tokens_details=None,
                raw={**u, "api_equivalent_usd": getattr(result, "total_cost_usd", None)}))


# ---------------------------------------------------------------- pool
class ModelPool:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._clients: dict = {}
        self._direct_cache: dict[str, dict[str, str]] | None = None

    # -------- discovery (nightly, pool worker) --------
    def refresh(self) -> int:
        P = policy()
        elig = P["eligibility"]
        cat = httpx.get(P["discovery"]["remote_catalog"], timeout=30).json()["data"]
        self._direct_cache = None
        direct = self.direct_ids()
        rows = []
        for m in cat:
            if "/" not in m.get("id", ""):
                continue
            lab = m["id"].split("/")[0]
            if lab not in elig["remote"]["labs"] or (m.get("context_length") or 0) < elig["remote"]["min_context"]:
                continue
            if any(re.search(p, m["id"]) for p in elig["remote"]["exclude_id_patterns"]):
                continue
            try:
                p_in = float(m["pricing"]["prompt"]) * 1e6        # catalog quotes USD per token
                p_out = float(m["pricing"]["completion"]) * 1e6
            except (KeyError, TypeError, ValueError):
                continue                                          # unpriced: never selectable
            if p_out <= 0 or p_out > elig["remote"]["max_out_price_per_m"]:
                continue                                          # $0 remote = free tier: rate-limited, may train on data
            short = norm(m["id"].split("/", 1)[1])
            route = next((prov for prov, ids in direct.items() if match_id(short, ids)), None)
            if lab == "anthropic" and P["anthropic_route"] == "subscription":
                route = "anthropic_subscription"
            rows.append((m["id"], lab, "t3", p_in, p_out, m["context_length"], None, route))
        rows += self._local_rows(elig["local"]["max_resident_gb"])
        with psycopg.connect(self.dsn) as c, c.transaction():
            c.execute("UPDATE model_catalog SET retired = true")          # absent tonight = retired
            c.cursor().executemany("""INSERT INTO model_catalog
                (model_id, lab, tier, in_per_m, out_per_m, ctx, resident_gb, direct_route, last_seen, retired)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now(), false)
                ON CONFLICT (model_id) DO UPDATE SET in_per_m = EXCLUDED.in_per_m,
                  out_per_m = EXCLUDED.out_per_m, ctx = EXCLUDED.ctx, direct_route = EXCLUDED.direct_route,
                  resident_gb = EXCLUDED.resident_gb, last_seen = now(), retired = false""", rows)
        return len(rows)

    def direct_ids(self) -> dict[str, dict[str, str]]:
        if self._direct_cache is not None:
            return self._direct_cache
        out: dict[str, dict[str, str]] = {}
        for prov, cfg in policy()["providers"].items():
            if prov in ("openrouter", "anthropic_subscription") or not os.environ.get(cfg["env"]):
                continue
            if prov == "anthropic" and policy()["anthropic_route"] != "api_key":
                continue
            try:
                ids = [getattr(m, "id", str(m)).removeprefix("models/") for m in self.client(prov).models.list()]
                out[prov] = {norm(i): i for i in ids}
            except Exception:
                out[prov] = {}                   # provider down tonight: fall back to the aggregator
        self._direct_cache = out
        return out

    def _local_rows(self, max_gb: float) -> list[tuple]:
        try:
            sizes = json.loads(HOSTSTAT.read_text()).get("local_models", {})   # alias -> GB (hoststat.sh)
            ids = [m.id for m in self.client("omlx").models.list()]
        except Exception:
            return []
        return [(f"local/{i}", "local", "t2", 0, 0, 32768, sizes.get(i), "omlx")
                for i in ids if sizes.get(i) is not None and sizes[i] <= max_gb]

    # -------- selection (per run) --------
    def select(self, role: str, *, pinned: dict[str, str] | None = None,
               exclude: tuple[str, ...] = ()) -> ModelSpec:
        if pinned and role in pinned and pinned[role] not in exclude:
            spec = self.spec(pinned[role])
            if spec:
                return spec                      # same model for every attempt in this run
        cfg = policy()["roles"][role]
        manual = policy().get("pins", {}).get(role)
        labs = cfg.get("labs") or []
        order = {"quality": "q.pass_at_1 DESC",
                 "quality_per_dollar": "q.pass_at_1 / GREATEST(q.usd_per_task, 0.0001) DESC",
                 "cost": "q.usd_per_task ASC",
                 "latency": "q.p50_latency_s ASC"}[cfg["rank"]]
        with psycopg.connect(self.dsn) as c:
            rows = c.execute(f"""
                SELECT m.model_id FROM model_catalog m
                JOIN model_qualification q ON q.model_id = m.model_id AND q.role = %s
                WHERE m.tier = %s AND NOT m.retired AND m.in_per_m IS NOT NULL
                  AND q.status IN ('qualified','promoted') AND q.pass_at_1 >= %s
                  AND q.suite_sha256 = %s AND NOT (m.model_id = ANY(%s))
                  AND (cardinality(%s::text[]) = 0 OR m.lab = ANY(%s::text[]))
                ORDER BY (m.model_id = %s) DESC, (q.status = 'promoted') DESC, {order}
                LIMIT 1""",
                (role, cfg["tier"], cfg["min_score"], suite_sha(role), list(exclude), labs, labs, manual)).fetchall()
        if not rows:
            raise NoQualifiedModel(role)
        spec = self.spec(rows[0][0])
        if spec is None:
            raise NoQualifiedModel(role)
        return spec

    def shadow_reference(self) -> ModelSpec | None:
        try:
            return self.select("repair")         # counterfactual list price for the savings metric
        except NoQualifiedModel:
            return None

    def spec(self, model_id: str) -> ModelSpec | None:
        with psycopg.connect(self.dsn) as c:
            r = c.execute("""SELECT model_id, tier, in_per_m, out_per_m, ctx, direct_route
                             FROM model_catalog WHERE model_id = %s AND NOT retired""", (model_id,)).fetchone()
        if r is None:
            return None
        mid, tier, pin, pout, ctx, direct = r
        return self.spec_from_row(mid, tier, pin, pout, ctx, direct)

    def spec_from_row(self, mid, tier, pin, pout, ctx, direct) -> ModelSpec | None:
        if tier == "t2":
            return ModelSpec(mid, mid.removeprefix("local/"), "omlx", tier, 0.0, 0.0, ctx, False, billed=False)
        short = norm(mid.split("/", 1)[1])
        if direct == "anthropic_subscription":
            return ModelSpec(mid, short, direct, tier, float(pin), float(pout), ctx, True, billed=False)
        if direct:
            api_id = match_id(short, self.direct_ids().get(direct, {}))
            if api_id:
                return ModelSpec(mid, api_id, direct, tier, float(pin), float(pout), ctx)
        if not policy().get("route_via_aggregator", False):
            return None
        return ModelSpec(mid, mid, "openrouter", tier, float(pin), float(pout), ctx)

    # -------- clients --------
    def client_for(self, spec: ModelSpec):
        return self.client(spec.route)

    def client(self, route: str):
        if route not in self._clients:
            from openai import OpenAI
            if route == "omlx":
                self._clients[route] = OpenAI(base_url=env("OMLX_BASE_URL"),
                                              api_key=os.environ.get("OMLX_API_KEY", "local"),
                                              timeout=240, max_retries=0)
            elif route == "anthropic_subscription":
                self._clients[route] = SubscriptionAdapter()
            elif route == "anthropic":
                self._clients[route] = AnthropicChatAdapter(env(policy()["providers"]["anthropic"]["env"]))
            else:
                cfg = policy()["providers"][route]
                self._clients[route] = OpenAI(base_url=cfg["base_url"], api_key=env(cfg["env"]),
                                              timeout=120, max_retries=2)
        return self._clients[route]

    # -------- qualification records --------
    def record(self, model_id: str, role: str, score: float, usd: float, latency: float, status: str) -> None:
        """Store a MEASURED result. It replaces a manual trust for the same suite; a promoted model that
        re-qualifies keeps its promotion (only a failed re-measurement demotes it)."""
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("""INSERT INTO model_qualification
                (model_id, role, suite_sha256, pass_at_1, usd_per_task, p50_latency_s, status, manual)
                VALUES (%s,%s,%s,%s,%s,%s,%s, false)
                ON CONFLICT (model_id, role, suite_sha256) DO UPDATE SET pass_at_1 = EXCLUDED.pass_at_1,
                  usd_per_task = EXCLUDED.usd_per_task, p50_latency_s = EXCLUDED.p50_latency_s,
                  status = CASE WHEN model_qualification.status = 'promoted' AND EXCLUDED.status = 'qualified'
                                THEN 'promoted' ELSE EXCLUDED.status END,
                  manual = false, qualified_at = now()""",
                      (model_id, role, suite_sha(role), score, usd, latency, status))

    def promote(self, model_id: str, role: str) -> bool:
        """Human approval of a qualified challenger. False if it has no qualification for the current suite."""
        sha = suite_sha(role)
        with psycopg.connect(self.dsn) as c, c.transaction():
            exists = c.execute("""SELECT 1 FROM model_qualification WHERE role = %s AND model_id = %s
                                  AND suite_sha256 = %s AND status IN ('qualified','promoted')""",
                               (role, model_id, sha)).fetchone()
            if not exists:
                return False
            c.execute("""UPDATE model_qualification SET status = 'qualified'
                         WHERE role = %s AND suite_sha256 = %s AND status = 'promoted'""", (role, sha))
            c.execute("""UPDATE model_qualification SET status = 'promoted'
                         WHERE role = %s AND model_id = %s AND suite_sha256 = %s""", (role, model_id, sha))
        return True

    def trust_roles(self, model_id: str) -> list[str] | None:
        """Roles a catalog model may fill: same tier, and its lab allowed by the role's `labs`.
        None when the model is not in the current catalog."""
        with psycopg.connect(self.dsn) as c:
            r = c.execute("SELECT tier, lab FROM model_catalog WHERE model_id = %s AND NOT retired",
                          (model_id,)).fetchone()
        if r is None:
            return None
        tier, lab = r
        return [role for role, cfg in policy()["roles"].items()
                if cfg["tier"] == tier and (not cfg.get("labs") or lab in cfg["labs"])]

    def trust(self, model_id: str, roles: list[str] | None = None) -> list[str]:
        """Manual approval WITHOUT measurement, for getting started. Recorded with manual=true and
        pass_at_1 = the role's min_score (not a measured number). Any later measurement replaces it,
        and changing a role's qualification suite invalidates it."""
        allowed = self.trust_roles(model_id)
        if allowed is None:
            raise ValueError(f"{model_id} is not in the current catalog; run `pool_worker refresh` "
                             "and pick an id from `pool_worker catalog`")
        roles = roles or allowed
        bad = [r for r in roles if r not in allowed]
        if bad:
            raise ValueError(f"{model_id} cannot fill {bad} (wrong tier or lab); allowed: {allowed}")
        with psycopg.connect(self.dsn) as c, c.transaction():
            for role in roles:
                sha = suite_sha(role)
                c.execute("""UPDATE model_qualification SET status = 'qualified'
                             WHERE role = %s AND suite_sha256 = %s AND status = 'promoted'""", (role, sha))
                c.execute("""INSERT INTO model_qualification
                    (model_id, role, suite_sha256, pass_at_1, usd_per_task, p50_latency_s, status, manual)
                    VALUES (%s,%s,%s,%s,0,0,'promoted',true)
                    ON CONFLICT (model_id, role, suite_sha256) DO UPDATE SET status = 'promoted',
                      manual = true, pass_at_1 = EXCLUDED.pass_at_1, qualified_at = now()""",
                          (model_id, role, sha, policy()["roles"][role]["min_score"]))
        return roles

    def catalog(self) -> list[tuple]:
        with psycopg.connect(self.dsn) as c:
            return c.execute("""SELECT model_id, tier, coalesce(direct_route, '-'), in_per_m, out_per_m, ctx
                                FROM model_catalog WHERE NOT retired ORDER BY tier DESC, lab, model_id""").fetchall()

    def promoted(self, role: str):
        with psycopg.connect(self.dsn) as c:
            return c.execute("""SELECT model_id, pass_at_1 FROM model_qualification
                                WHERE role = %s AND status = 'promoted' AND suite_sha256 = %s""",
                             (role, suite_sha(role))).fetchone()

    def unscored_pairs(self) -> list[tuple[str, str]]:
        P = policy()
        days = int(P["qualification"]["requalify_days"])
        pairs = []
        with psycopg.connect(self.dsn) as c:
            for role, cfg in P["roles"].items():
                if not any(suite_dir(role).glob("*.json")):      # no suite, or an empty one: nothing to measure
                    continue
                labs = cfg.get("labs") or []
                rows = c.execute("""
                    SELECT m.model_id FROM model_catalog m
                    WHERE m.tier = %s AND NOT m.retired
                      AND (cardinality(%s::text[]) = 0 OR m.lab = ANY(%s::text[]))
                      AND NOT EXISTS (SELECT 1 FROM model_qualification q
                                      WHERE q.model_id = m.model_id AND q.role = %s AND q.suite_sha256 = %s
                                        AND NOT q.manual          -- trusted models still get measured
                                        AND q.qualified_at > now() - make_interval(days => %s))
                    ORDER BY m.last_seen DESC""",
                                 (cfg["tier"], labs, labs, role, suite_sha(role), days)).fetchall()
                pairs += [(r[0], role) for r in rows]
        return pairs


@functools.lru_cache(maxsize=1)
def get_pool() -> ModelPool:
    return ModelPool(env("FACTORY_DB_DSN"))

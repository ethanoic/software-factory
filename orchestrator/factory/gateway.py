"""Dynamic Triage Gateway (spec §2). route() is pure and returns a ROLE; the pool resolves the model."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum

from factory.breaker import Breaker, BreakerTripped
from factory.config import LOCAL_CTX_BUDGET, policy
from factory.obs import record_generation
from factory.pool import ModelSpec, NoQualifiedModel, get_pool
from factory.state import Tier

T1_KINDS = {"format", "lint_autofix", "import_sort", "rename_symbol"}
RESERVE_FACTOR = 1.25        # worst case: every input token is a cache write


class TaskKind(StrEnum):
    FORMAT = "format"; LINT_AUTOFIX = "lint_autofix"; IMPORT_SORT = "import_sort"
    RENAME_SYMBOL = "rename_symbol"; PLAN = "plan"; GENERATE = "generate"
    REPAIR = "repair"; SECURITY_FIX = "security_fix"; TRIAGE_CLASSIFY = "triage_classify"
    WRITE_TESTS = "write_tests"; REVIEW_CODE = "review_code"


# Roles whose model wrote the code. The test writer and the reviewer are kept away from these models
# whenever another qualified model exists (pool.yaml `review.independence`).
AUTHOR_ROLES = ("generate_remote", "generate", "repair", "security_fix")


@dataclass
class Task:
    kind: TaskKind
    messages: list[dict]
    target_files: list[str] = field(default_factory=list)
    complexity: str = "routine"
    escalation_required: bool = False
    allow_spillover: bool = False
    max_output_tokens: int = 4_096
    node: str = ""


@dataclass
class Decision:
    tier: Tier
    role: str | None
    reason: str
    model: ModelSpec | None = None


def est_tokens(messages: list[dict]) -> int:
    return int(sum(len(m["content"]) for m in messages) / 3.2) + 16 * len(messages)


def host_memory_free_pct() -> float:
    from factory.pool import HOSTSTAT
    try:
        stat = json.loads(HOSTSTAT.read_text())
        return 0.0 if time.time() - stat["ts"] > 30 else float(stat["free_pct"])
    except (OSError, ValueError, KeyError):
        return 0.0


def route(t: Task) -> Decision:
    """First match wins (spec §2.1). Pure: depends only on the task and pool.yaml."""
    if t.kind in T1_KINDS:
        return Decision(Tier.T1_DETERMINISTIC, None, f"kind={t.kind}")
    if t.kind is TaskKind.PLAN:
        return Decision(Tier.T3_REMOTE, "plan", "kind=plan")
    if t.kind is TaskKind.WRITE_TESTS:
        return Decision(Tier.T3_REMOTE, "write_tests", "kind=write_tests")
    if t.kind is TaskKind.REVIEW_CODE:
        return Decision(Tier.T3_REMOTE, "review_code", "kind=review_code")
    if t.kind is TaskKind.SECURITY_FIX:
        return Decision(Tier.T3_REMOTE, "security_fix", "kind=security_fix")
    if t.escalation_required:
        return Decision(Tier.T3_REMOTE, "repair", "escalation circuit")
    remote_role = "repair" if t.kind is TaskKind.REPAIR else "generate_remote"
    n = est_tokens(t.messages)
    mode = policy().get("local_mode", "assist")
    if t.kind is TaskKind.TRIAGE_CLASSIFY:
        return (Decision(Tier.T2_LOCAL, "classify", "local triage") if mode != "off"
                else Decision(Tier.T3_REMOTE, "spillover", "local_mode=off: cheapest qualified Claude"))
    if n > LOCAL_CTX_BUDGET:
        return Decision(Tier.T3_REMOTE, remote_role, f"ctx={n}")
    if len(t.target_files) > 3 or t.complexity == "complex":
        return Decision(Tier.T3_REMOTE, remote_role, f"files={len(t.target_files)} complexity={t.complexity}")
    if mode == "generate" and t.kind in (TaskKind.GENERATE, TaskKind.REPAIR):
        # REPAIR reaches here only for the single cheap syntax retry (escalation_required=False)
        return Decision(Tier.T2_LOCAL, "generate", f"routine {t.kind}; local_mode=generate")
    return Decision(Tier.T3_REMOTE, remote_role, f"routine; local_mode={mode}")


class TriageGateway:
    def __init__(self, breaker: Breaker, pins: dict[str, str], pool=None):
        self.breaker = breaker
        self.pins = pins             # role -> model_id chosen earlier in THIS run (persisted in state)
        self.pool = pool or get_pool()
        self.independence: dict[str, str] = {}   # role -> "independent" | "same model as author"

    def route(self, t: Task) -> Decision:
        return route(t)

    def resolve(self, d: Decision, exclude: tuple[str, ...] = ()) -> Decision:
        spec = self.pool.select(d.role, pinned=self.pins, exclude=exclude)     # raises NoQualifiedModel
        self.pins.setdefault(d.role, spec.model_id)                         # pin for the rest of the run
        return Decision(d.tier, d.role, d.reason, spec)

    def preselect(self, role: str) -> str | None:
        """Pin the model a role will use later in this run, without calling it. None if none qualifies."""
        try:
            return self.resolve(Decision(Tier.T3_REMOTE, role, "preselect")).model.model_id
        except NoQualifiedModel:
            return None

    def _resolve_independent(self, d: Decision, independent_of: tuple[str, ...]) -> Decision:
        from factory.review import settings
        authors = tuple({self.pins[r] for r in independent_of if r in self.pins})
        if not authors:
            return self.resolve(d)
        if d.role in self.pins and self.pins[d.role] not in authors:
            self.independence[d.role] = "independent"
            return self.resolve(d)
        try:
            self.pins.pop(d.role, None)              # an earlier same-model pin must not stick
            out = self.resolve(d, exclude=authors)
            self.independence[d.role] = "independent"
            return out
        except NoQualifiedModel:
            if settings()["independence"] == "require":
                raise BreakerTripped(f"no independent model for role '{d.role}': every qualified model also "
                                     "wrote the code. Trust a second Claude model, or set review.independence: prefer")
            self.independence[d.role] = "same model as author"
            return self.resolve(d)

    def complete(self, t: Task, *, run_id: str, independent_of: tuple[str, ...] = ()) -> tuple[str, Decision]:
        d = self.route(t)
        if d.tier is Tier.T1_DETERMINISTIC:
            raise ValueError("T1 tasks run through the sandbox broker, not an LLM")
        if d.tier is Tier.T2_LOCAL and not self._local_ready(d.role):
            d = self._wait_or_spill(t, d)
        try:
            d = self._resolve_independent(d, independent_of) if independent_of else self.resolve(d)
        except NoQualifiedModel as e:
            raise BreakerTripped(f"no qualified model for role '{e.args[0]}'") from e
        return self._call(t, d, run_id, tried=()), d

    def _call(self, t: Task, d: Decision, run_id: str, tried: tuple[str, ...]) -> str:
        if d.model.ctx < est_tokens(t.messages) + t.max_output_tokens:
            raise BreakerTripped(f"{d.model.model_id} context too small for {t.node}")
        reservation = self.breaker.reserve(
            d.model.cost(int(est_tokens(t.messages) * RESERVE_FACTOR), t.max_output_tokens), node=t.node)
        started = time.time()
        try:
            resp = self.pool.client_for(d.model).chat.completions.create(
                model=d.model.api_id, messages=t.messages, max_tokens=t.max_output_tokens)
        except Exception as e:                       # 404 / retired / outage; QuotaExhausted re-raised
            self.breaker.settle(reservation, 0.0)
            if d.tier is Tier.T3_REMOTE and not tried and _is_unavailable(e):
                nxt = self.resolve(Decision(d.tier, d.role, "failover"), exclude=(d.model.model_id,))
                self.pins[d.role] = nxt.model.model_id                    # re-pin, visible in the trace
                return self._call(t, nxt, run_id, (d.model.model_id,))
            raise
        u = resp.usage
        self.breaker.settle(reservation, d.model.cost(u.prompt_tokens, u.completion_tokens))
        text = resp.choices[0].message.content or ""
        record_generation(run_id=run_id, node=t.node, decision=d, messages=t.messages, output=text,
                          usage=u, latency_s=time.time() - started,
                          shadow_spec=self.pool.shadow_reference() if not d.model.billed else None)
        return text

    def _local_ready(self, role: str) -> bool:
        if host_memory_free_pct() < 15:
            return False
        try:
            self.pool.select(role)
            return True
        except NoQualifiedModel:
            return False

    def _wait_or_spill(self, t: Task, d: Decision) -> Decision:
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(15)
            if self._local_ready(d.role):
                return d
        if t.allow_spillover or t.kind is TaskKind.TRIAGE_CLASSIFY:
            return Decision(Tier.T3_REMOTE, "spillover", "local unavailable; spillover")
        raise BreakerTripped("local backend unavailable for 10 min and spillover disabled")


def _is_unavailable(e: Exception) -> bool:
    code = getattr(e, "status_code", None)
    msg = str(e).lower()
    return code in (404, 410, 503) or ("model" in msg and "not found" in msg)

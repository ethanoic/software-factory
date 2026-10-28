"""Model qualification (spec §5.2). Runs as part of the pool worker, never inside a ticket run.

Suites live in qualification/:
  code/*.json    {"ticket": {...TicketSpec...}}                    -> plan, generate_remote, repair, security_fix, generate
  triage/*.json  {"ticket": {...}, "expected_accept": true|false}  -> classify, spillover
A code task passes when the run reaches compile_pr (dry run) with zero repair cycles.
"""
from __future__ import annotations

import json
import statistics
import time
import uuid

import psycopg

from factory.breaker import Breaker, BreakerTripped, alert
from factory.config import policy
from factory.pool import SUITE_FOR, ModelPool, NoQualifiedModel, QuotaExhausted, suite_dir
from factory.state import TicketSpec, Tier

CODE_ROLES = ("plan", "generate_remote", "repair", "security_fix")


def month_eval_spend(dsn: str) -> float:
    with psycopg.connect(dsn) as c:
        return float(c.execute("""SELECT coalesce(sum(spent_usd), 0) FROM run_ledger WHERE run_id LIKE 'qual-%%'
                                  AND started_at >= date_trunc('month', now())""").fetchone()[0])


def _tasks(role: str) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(suite_dir(role).glob("*.json"))]


def probe(pool: ModelPool, model_id: str) -> bool:
    """One-line call; used for subscription models, which have no model-list endpoint."""
    spec = pool.spec(model_id)
    if spec is None:
        return False
    try:
        r = pool.client_for(spec).chat.completions.create(
            model=spec.api_id, max_tokens=8, messages=[{"role": "user", "content": "Reply with: ok"}])
        return bool(r.choices[0].message.content)
    except QuotaExhausted:
        raise
    except Exception:
        return False


def qualify_triage(pool: ModelPool, deps, model_id: str, role: str) -> tuple[float, float, float]:
    from factory import prompts
    from factory.gateway import Decision, Task, TaskKind, TriageGateway
    from factory.nodes import _json
    run_id = f"qual-{uuid.uuid4().hex[:12]}"
    gw = TriageGateway(Breaker(deps.dsn, run_id), {role: model_id}, pool)
    spec = pool.spec(model_id)
    ok, lat = 0, []
    tasks = _tasks(role)
    for task in tasks:
        t = TicketSpec(**task["ticket"])
        started = time.time()
        d = Decision(Tier.T2_LOCAL if spec.tier == "t2" else Tier.T3_REMOTE, role, "qualification", spec)
        raw = gw._call(Task(TaskKind.TRIAGE_CLASSIFY, prompts.triage(t), max_output_tokens=200, node="qual"),
                       d, run_id, tried=())
        lat.append(time.time() - started)
        try:
            ok += bool(_json(raw).get("accept")) == bool(task["expected_accept"])
        except Exception:
            pass
    spent = gw.breaker.spent()
    return ok / max(len(tasks), 1), spent / max(len(tasks), 1), statistics.median(lat or [0])


def qualify_code(pool: ModelPool, deps, model_id: str, role: str, budget: float) -> tuple[float, float, float]:
    from langgraph.checkpoint.memory import MemorySaver

    from factory.graph import compile_app, config_for
    roles = [role] if role == "generate" else list(CODE_ROLES)
    passed, costs, lat = 0, [], []
    tasks = _tasks(role)
    app = compile_app(deps, MemorySaver(), human_gate=False)
    for task in tasks:
        if sum(costs) >= budget:
            break
        run_id = f"qual-{uuid.uuid4().hex[:12]}"
        started = time.time()
        try:
            final = app.invoke({"run_id": run_id, "ticket": TicketSpec(**task["ticket"]), "repair_attempts": 0,
                                "model_pins": {r: model_id for r in roles}, "attempts": [],
                                "dry_run": True, "qualification": True}, config_for(run_id))
            passed += final.get("pr_url") == "dry-run" and final.get("repair_attempts", 0) == 0
        except (BreakerTripped, NoQualifiedModel):
            pass
        lat.append(time.time() - started)
        costs.append(Breaker(deps.dsn, run_id).spent())
    return passed / max(len(tasks), 1), statistics.mean(costs or [0]), statistics.median(lat or [0])


def qualify_candidates(pool: ModelPool, deps) -> None:
    P = policy()["qualification"]
    monthly_cap = float(P["max_monthly_eval_spend_usd"])
    per_candidate = float(P["max_eval_spend_usd"])
    done_code: dict[str, tuple] = {}
    for model_id, role in pool.unscored_pairs():
        if month_eval_spend(deps.dsn) >= monthly_cap:
            print(f"[qualify] monthly eval cap ${monthly_cap:.2f} reached; remaining candidates wait", flush=True)
            return
        spec = pool.spec(model_id)
        if spec is None:
            continue
        try:
            if spec.route == "anthropic_subscription" and not probe(pool, model_id):
                pool.record(model_id, role, 0.0, 0.0, 0.0, "rejected")
                continue
            if SUITE_FOR.get(role) == "triage":
                score, usd, latency = qualify_triage(pool, deps, model_id, role)
            elif role in CODE_ROLES and model_id in done_code:     # one code run scores all code roles
                score, usd, latency = done_code[model_id]
            else:
                score, usd, latency = qualify_code(pool, deps, model_id, role, per_candidate)
                if role in CODE_ROLES:
                    done_code[model_id] = (score, usd, latency)
        except QuotaExhausted:
            print("[qualify] Claude plan limit reached; qualification resumes next cycle", flush=True)
            return
        cfg = policy()["roles"][role]
        status = "qualified" if score >= cfg["min_score"] else "rejected"
        pool.record(model_id, role, score, usd, latency, status)
        print(f"[qualify] {model_id} {role}: pass@1={score:.2f} ${usd:.4f}/task -> {status}", flush=True)
        incumbent = pool.promoted(role)
        if status == "qualified" and (incumbent is None or score >= float(incumbent[1]) + float(P["promote_margin"])):
            _request_promotion(pool, model_id, role, score, incumbent)


def _request_promotion(pool: ModelPool, model_id: str, role: str, score: float, incumbent) -> None:
    if not policy()["qualification"].get("promotions_require_approval", True):
        pool.promote(model_id, role)
        return
    inc = f"{incumbent[0]} ({float(incumbent[1]):.2f})" if incumbent else "none"
    alert("qualification", "model-pool",
          f"{model_id} qualified for {role} at {score:.2f} (incumbent: {inc}). "
          f"Approve: python -m factory.pool_worker promote {model_id} {role}")

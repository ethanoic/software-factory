"""Circuit breaker (spec §4.2): repair cap, per-run and daily cost caps, wall clock.

Cost is reserved BEFORE each billed call, in Postgres, so the cap survives crashes and cannot be raced.
"""
from __future__ import annotations

import functools
import os
import uuid

import httpx
import psycopg

from factory.config import DAY_CAP_USD, MAX_REPAIRS, RUN_CAP_USD, RUN_WALL_S
from factory.state import FailureClass, SandboxResult


class BreakerTripped(Exception):
    pass


class PatchRejected(Exception):
    pass


class Breaker:
    def __init__(self, dsn: str, run_id: str):
        self.dsn, self.run_id = dsn, run_id
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("INSERT INTO run_ledger (run_id) VALUES (%s) ON CONFLICT DO NOTHING", (run_id,))

    def reserve(self, worst_case_usd: float, *, node: str) -> tuple[str, float]:
        if worst_case_usd == 0:
            return ("free", 0.0)                 # local and subscription calls never block on money
        with psycopg.connect(self.dsn) as c, c.transaction():
            day = c.execute("""SELECT coalesce(sum(spent_usd + reserved_usd), 0) FROM run_ledger
                               WHERE started_at > now() - interval '24 hours'""").fetchone()[0]
            if float(day) + worst_case_usd > DAY_CAP_USD:
                self._trip(c, f"daily cap ${DAY_CAP_USD:.2f} would be exceeded at {node}")
            row = c.execute("""UPDATE run_ledger SET reserved_usd = reserved_usd + %s
                               WHERE run_id = %s AND NOT tripped
                                 AND spent_usd + reserved_usd + %s <= %s
                                 AND started_at > now() - make_interval(secs => %s)
                               RETURNING spent_usd""",
                            (worst_case_usd, self.run_id, worst_case_usd, RUN_CAP_USD, RUN_WALL_S)).fetchone()
            if row is None:
                self._trip(c, f"run cap ${RUN_CAP_USD:.2f} or wall clock exceeded at {node} "
                              f"(next call worst case ${worst_case_usd:.4f})")
        return (str(uuid.uuid4()), worst_case_usd)

    def settle(self, reservation: tuple[str, float], actual_usd: float) -> None:
        _, held = reservation
        if held == 0 and actual_usd == 0:
            return
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("""UPDATE run_ledger SET reserved_usd = GREATEST(reserved_usd - %s, 0),
                                             spent_usd = spent_usd + %s WHERE run_id = %s""",
                      (held, actual_usd, self.run_id))

    def check_repairs(self, attempts: int) -> None:
        if attempts >= MAX_REPAIRS:
            with psycopg.connect(self.dsn) as c, c.transaction():
                self._trip(c, f"repair limit: {attempts} cycles completed, 4th refused")

    def check_wall_clock(self) -> None:
        with psycopg.connect(self.dsn) as c, c.transaction():
            over = c.execute("""SELECT started_at < now() - make_interval(secs => %s) FROM run_ledger
                                WHERE run_id = %s""", (RUN_WALL_S, self.run_id)).fetchone()
            if over and over[0]:
                self._trip(c, f"wall clock {RUN_WALL_S // 60} min exceeded")

    def spent(self) -> float:
        with psycopg.connect(self.dsn) as c:
            r = c.execute("SELECT spent_usd FROM run_ledger WHERE run_id = %s", (self.run_id,)).fetchone()
        return float(r[0]) if r else 0.0

    def _trip(self, _conn, reason: str):
        # Separate autocommit connection: the caller's transaction rolls back when we raise,
        # and the trip must persist regardless.
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("UPDATE run_ledger SET tripped = true, trip_reason = %s WHERE run_id = %s",
                      (reason, self.run_id))
        raise BreakerTripped(reason)


def alert(run_id: str, ticket_id: str, reason: str) -> None:
    url = os.environ.get("ALERT_WEBHOOK_URL")          # Slack incoming webhook or ntfy topic URL
    from factory.obs import trace_url
    text = f"Factory halted {ticket_id}: {reason}\n{trace_url(run_id)}"
    print(f"[alert] {text}", flush=True)
    if url:
        try:
            httpx.post(url, json={"text": text}, timeout=10)
        except httpx.HTTPError as e:
            print(f"[alert] webhook failed: {e}", flush=True)


def guarded(fn, deps, *, node_name: str):
    """Wrap a node: trace span, breaker checks, and conversion of known failures into state."""
    from factory.obs import node_span

    @functools.wraps(fn)
    def wrapper(state):
        breaker = deps.breaker_for(state["run_id"])
        with node_span(state, node_name) as span:
            try:
                breaker.check_wall_clock()
                if node_name == "repair":
                    breaker.check_repairs(state.get("repair_attempts", 0))
                update = fn(state, deps)
            except BreakerTripped as e:
                span.update(level="ERROR", status_message=str(e))
                alert(state["run_id"], state["ticket"].ticket_id, str(e))
                return {"halt_reason": f"breaker: {e}"}
            except PatchRejected as e:
                span.update(level="WARNING", status_message=str(e))
                return {"repair_attempts": state.get("repair_attempts", 0) + 1,
                        "patch_rejected": True,
                        "sandbox": SandboxResult(exit_code=1, passed=0, failed=0, lint_errors=1,
                                                 failure_class=FailureClass.SYNTAX,
                                                 stack_excerpt=str(e)[:8000])}
            span.update(output={k: str(v)[:500] for k, v in update.items() if k != "patch"})
            return {**update, "patch_rejected": False}
    return wrapper

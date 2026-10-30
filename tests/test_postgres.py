"""Integration tests against a real Postgres. Skipped unless FACTORY_TEST_DSN points at an empty database:
   FACTORY_TEST_DSN=postgresql://user@host:5432/factory_test make test
"""
import os

import psycopg
import pytest

DSN = os.environ.get("FACTORY_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set FACTORY_TEST_DSN to run Postgres integration tests")


@pytest.fixture()
def db():
    from factory.runner import migrate
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS model_qualification, model_catalog, run_ledger CASCADE")
    migrate(DSN)
    migrate(DSN)                                   # idempotent
    return DSN


# ------------------------------------------------------------------ breaker (D7)
def test_reservation_refuses_call_that_would_cross_run_cap(db):
    from factory.breaker import Breaker, BreakerTripped
    b = Breaker(db, "run-cap-0001")
    r = b.reserve(1.90, node="plan")
    b.settle(r, 1.90)
    with pytest.raises(BreakerTripped, match="run cap"):
        b.reserve(0.126, node="repair#2")
    with psycopg.connect(db) as c:
        tripped, spent, reserved = c.execute(
            "SELECT tripped, spent_usd, reserved_usd FROM run_ledger WHERE run_id='run-cap-0001'").fetchone()
    assert tripped and float(spent) == pytest.approx(1.90) and float(reserved) == 0


def test_tripped_run_refuses_everything_after(db):
    from factory.breaker import Breaker, BreakerTripped
    b = Breaker(db, "run-trip-0001")
    with pytest.raises(BreakerTripped):
        b.reserve(5.0, node="plan")
    with pytest.raises(BreakerTripped):
        b.reserve(0.01, node="generate")


def test_free_calls_never_touch_the_ledger(db):
    from factory.breaker import Breaker
    b = Breaker(db, "run-free-0001")
    assert b.reserve(0.0, node="triage") == ("free", 0.0)
    b.settle(("free", 0.0), 0.0)
    assert b.spent() == 0.0


def test_daily_cap_spans_runs(db):
    from factory.breaker import Breaker, BreakerTripped
    for i in range(5):
        b = Breaker(db, f"run-day-{i:04d}")
        b.settle(b.reserve(1.95, node="plan"), 1.95)          # 5 × 1.95 = 9.75
    with pytest.raises(BreakerTripped, match="daily cap"):
        Breaker(db, "run-day-0099").reserve(0.50, node="plan")


def test_repair_cap(db):
    from factory.breaker import Breaker, BreakerTripped
    b = Breaker(db, "run-rep-0001")
    b.check_repairs(2)
    with pytest.raises(BreakerTripped, match="repair limit"):
        b.check_repairs(3)


# ------------------------------------------------------------------ pool selection (D2, D3)
def seed(db, rows, quals):
    from factory.pool import suite_sha
    with psycopg.connect(db, autocommit=True) as c:
        c.cursor().executemany("""INSERT INTO model_catalog (model_id, lab, tier, in_per_m, out_per_m, ctx, direct_route, retired)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        c.cursor().executemany("""INSERT INTO model_qualification (model_id, role, suite_sha256, pass_at_1, usd_per_task,
                         p50_latency_s, status) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                      [(m, r, suite_sha(r), s, u, 1.0, st) for m, r, s, u, st in quals])


def test_select_respects_labs_score_retirement_and_promotion(db):
    from factory.pool import ModelPool, NoQualifiedModel
    seed(db,
         [("anthropic/a-big", "anthropic", "t3", 5, 25, 200000, "anthropic_subscription", False),
          ("anthropic/a-mid", "anthropic", "t3", 3, 15, 200000, "anthropic_subscription", False),
          ("anthropic/a-old", "anthropic", "t3", 3, 15, 200000, "anthropic_subscription", True),
          ("google/g-pro", "google", "t3", 2, 12, 200000, None, False)],
         [("anthropic/a-big", "repair", 0.80, 0.10, "qualified"),
          ("anthropic/a-mid", "repair", 0.75, 0.05, "promoted"),
          ("anthropic/a-old", "repair", 0.99, 0.01, "qualified"),     # retired: ignored
          ("google/g-pro", "repair", 0.95, 0.02, "qualified")])       # wrong lab for coding roles
    pool = ModelPool(db)
    spec = pool.select("repair")
    assert spec.model_id == "anthropic/a-mid"                          # promoted beats higher-scoring qualified
    assert spec.route == "anthropic_subscription" and spec.billed is False and spec.cost(10**6, 10**6) == 0
    assert pool.select("repair", exclude=("anthropic/a-mid",)).model_id == "anthropic/a-big"
    assert pool.select("repair", pinned={"repair": "anthropic/a-big"}).model_id == "anthropic/a-big"
    with pytest.raises(NoQualifiedModel):
        pool.select("plan")                                            # nothing qualified for plan


def test_promote_swaps_incumbent(db):
    from factory.pool import ModelPool
    seed(db, [("anthropic/x", "anthropic", "t3", 3, 15, 200000, "anthropic_subscription", False),
              ("anthropic/y", "anthropic", "t3", 3, 15, 200000, "anthropic_subscription", False)],
         [("anthropic/x", "plan", 0.8, 0, "promoted"), ("anthropic/y", "plan", 0.9, 0, "qualified")])
    pool = ModelPool(db)
    pool.promote("anthropic/y", "plan")
    assert pool.promoted("plan")[0] == "anthropic/y"
    assert pool.select("plan").model_id == "anthropic/y"


def test_runs_today_ignores_qualification_runs(db):
    from factory.breaker import Breaker
    from factory.runner import runs_today
    Breaker(db, "app-1-aaaaaaaa")
    Breaker(db, "qual-bbbbbbbbbbbb")
    assert runs_today(db) == 1


def test_refresh_filters_catalog_and_routes_claude_to_the_plan(db, monkeypatch):
    from types import SimpleNamespace

    from factory.pool import ModelPool
    catalog = {"data": [
        {"id": "anthropic/claude-x-4.5", "context_length": 200000, "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
        {"id": "anthropic/claude-x:free", "context_length": 200000, "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "google/gemini-z-pro", "context_length": 1000000, "pricing": {"prompt": "0.000002", "completion": "0.000012"}},
        {"id": "meta/llama-q", "context_length": 128000, "pricing": {"prompt": "0.0000002", "completion": "0.0000006"}},
        {"id": "openai/gpt-tiny", "context_length": 16000, "pricing": {"prompt": "0.0000001", "completion": "0.0000004"}},
        {"id": "openai/gpt-image-1", "context_length": 200000, "pricing": {"prompt": "0.000005", "completion": "0.00004"}},
    ]}
    monkeypatch.setattr("factory.pool.httpx.get", lambda *a, **k: SimpleNamespace(json=lambda: catalog))
    pool = ModelPool(db)
    monkeypatch.setattr(pool, "direct_ids", lambda: {})
    monkeypatch.setattr(pool, "_local_rows", lambda gb: [("local/qwen-small", "local", "t2", 0, 0, 32768, 5.0, "omlx")])
    assert pool.refresh() == 3
    with psycopg.connect(db) as c:
        rows = dict(c.execute("SELECT model_id, direct_route FROM model_catalog WHERE NOT retired").fetchall())
    assert rows == {"anthropic/claude-x-4.5": "anthropic_subscription", "google/gemini-z-pro": None,
                    "local/qwen-small": "omlx"}
    assert pool.spec("anthropic/claude-x-4.5").api_id == "claude-x-4-5"
    assert pool.spec("google/gemini-z-pro") is None          # no direct key, aggregator disabled (D5)


# ------------------------------------------------------------------ parking on plan limits (D3)
def test_quota_exhausted_parks_and_resumes_from_checkpoint(db, monkeypatch):
    from langgraph.checkpoint.postgres import PostgresSaver

    from factory import graph, nodes
    from factory.pool import QuotaExhausted
    from factory.state import FailureClass, Patch, Plan, QAResult, SandboxResult, Tier, TicketSpec

    class StubDeps:
        def breaker_for(self, run_id):
            from factory.breaker import Breaker
            return Breaker(db, run_id)

    calls = {"plan": 0, "generate": 0}
    plan = Plan(summary="s", target_files=["a.py"], acceptance_criteria=["c"], test_files=[], complexity="routine")

    def plan_node(s, d):
        calls["plan"] += 1
        return {"plan": plan}

    def generate(s, d):
        calls["generate"] += 1
        if calls["generate"] == 1:
            raise QuotaExhausted("usage limit reached")
        return {"patch": Patch(unified_diff="d", produced_by=Tier.T3_REMOTE, model="m"), "qa": None}

    monkeypatch.setattr(nodes, "ingest", lambda s, d: {"base_sha": "abc"})
    monkeypatch.setattr(nodes, "triage", lambda s, d: {"accepted": True})
    monkeypatch.setattr(nodes, "plan", plan_node)
    monkeypatch.setattr(nodes, "freeze_tests", lambda s, d: {"tests_sha256": "h"})
    monkeypatch.setattr(nodes, "generate", generate)
    monkeypatch.setattr(nodes, "sandbox", lambda s, d: {"sandbox": SandboxResult(
        exit_code=0, passed=1, failed=0, lint_errors=0, failure_class=FailureClass.NONE)})
    monkeypatch.setattr(nodes, "qa", lambda s, d: {"qa": QAResult(high_findings=0, medium_findings=0)})

    ticket = TicketSpec(ticket_id="T-9", title="t", body="b", repo="o/r", language="python")
    cfg = graph.config_for("park-test-0001")
    with PostgresSaver.from_conn_string(db) as saver:
        saver.setup()
        app = graph.compile_app(StubDeps(), saver)
        with pytest.raises(QuotaExhausted):
            app.invoke({"run_id": "park-test-0001", "ticket": ticket, "repair_attempts": 0,
                        "model_pins": {}, "attempts": []}, cfg)
    with PostgresSaver.from_conn_string(db) as saver:                   # new process, same checkpoint
        app = graph.compile_app(StubDeps(), saver)
        app.invoke(None, cfg)
        assert app.get_state(cfg).next == ("compile_pr",)
    assert calls == {"plan": 1, "generate": 2}                          # plan was NOT re-run


# ------------------------------------------------------------------ manual trust (hello-world quick start)
def test_trust_lets_runs_start_without_a_suite_and_is_marked_manual(db):
    from factory.pool import ModelPool, NoQualifiedModel
    seed(db, [("anthropic/a-x", "anthropic", "t3", 3, 15, 200000, "anthropic_subscription", False),
              ("local/small", "local", "t2", 0, 0, 32768, "omlx", False),
              ("google/g-x", "google", "t3", 2, 12, 200000, None, False)], [])
    pool = ModelPool(db)
    with pytest.raises(NoQualifiedModel):
        pool.select("plan")
    roles = pool.trust("anthropic/a-x")
    assert set(roles) == {"plan", "generate_remote", "repair", "security_fix", "spillover"}
    assert pool.select("plan").model_id == "anthropic/a-x"
    assert pool.trust("local/small") == ["generate", "classify"]
    with pytest.raises(ValueError, match="cannot fill"):
        pool.trust("google/g-x", ["plan"])                    # coding roles are anthropic-only (D2)
    with pytest.raises(ValueError, match="not in the current catalog"):
        pool.trust("anthropic/does-not-exist")
    with psycopg.connect(db) as c:
        manual, score = c.execute("SELECT manual, pass_at_1 FROM model_qualification "
                                  "WHERE model_id='anthropic/a-x' AND role='plan'").fetchone()
    assert manual and float(score) == 0.70                    # the role's min_score, not a fake measurement


def test_trusted_models_are_still_measured_and_measurement_wins(db, monkeypatch, tmp_path):
    from factory import pool as poolmod
    from factory.pool import ModelPool
    (tmp_path / "triage").mkdir()
    (tmp_path / "triage" / "t.json").write_text("{}")
    monkeypatch.setattr(poolmod, "SUITE_ROOT", tmp_path)
    seed(db, [("anthropic/a-x", "anthropic", "t3", 3, 15, 200000, "anthropic_subscription", False)], [])
    pool = ModelPool(db)
    pool.trust("anthropic/a-x", ["spillover"])
    assert ("anthropic/a-x", "spillover") in pool.unscored_pairs()     # trust does not skip measurement
    pool.record("anthropic/a-x", "spillover", 0.5, 0.0, 1.0, "rejected")
    with psycopg.connect(db) as c:
        status, manual = c.execute("SELECT status, manual FROM model_qualification "
                                   "WHERE model_id='anthropic/a-x' AND role='spillover'").fetchone()
    assert (status, manual) == ("rejected", False)


def test_requalified_incumbent_keeps_promotion_and_promote_needs_a_row(db):
    from factory.pool import ModelPool
    seed(db, [("anthropic/x", "anthropic", "t3", 3, 15, 200000, "anthropic_subscription", False),
              ("anthropic/y", "anthropic", "t3", 3, 15, 200000, "anthropic_subscription", False)],
         [("anthropic/x", "repair", 0.8, 0, "promoted")])
    pool = ModelPool(db)
    pool.record("anthropic/x", "repair", 0.82, 0.01, 2.0, "qualified")   # monthly re-measurement passes
    assert pool.promoted("repair")[0] == "anthropic/x"
    assert pool.promote("anthropic/y", "repair") is False                # never qualified: refused, not silent
    assert pool.promoted("repair")[0] == "anthropic/x"


def test_empty_suite_folder_never_revokes_trust(db, monkeypatch, tmp_path):
    """`make init` creates an empty qualification/code/: nightly qualification must skip it."""
    from factory import pool as poolmod
    from factory.pool import ModelPool
    (tmp_path / "code").mkdir()
    (tmp_path / "triage").mkdir()
    monkeypatch.setattr(poolmod, "SUITE_ROOT", tmp_path)
    seed(db, [("anthropic/a-x", "anthropic", "t3", 3, 15, 200000, "anthropic_subscription", False)], [])
    pool = ModelPool(db)
    pool.trust("anthropic/a-x")
    assert pool.unscored_pairs() == []
    assert pool.select("plan").model_id == "anthropic/a-x"

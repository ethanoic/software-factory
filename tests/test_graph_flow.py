"""Drives the real LangGraph wiring with stubbed nodes: checks loops, the repair cap and the human gate."""
import pytest
from langgraph.checkpoint.memory import MemorySaver

from factory import graph, nodes
from factory.breaker import BreakerTripped
from factory.state import FailureClass, Patch, Plan, QAResult, SandboxResult, Tier, TicketSpec


class StubBreaker:
    def check_wall_clock(self):
        pass

    def check_repairs(self, attempts):
        from factory.config import MAX_REPAIRS
        if attempts >= MAX_REPAIRS:
            raise BreakerTripped("repair limit")


class StubDeps:
    def breaker_for(self, run_id):
        return StubBreaker()


TICKET = TicketSpec(ticket_id="T-1", title="t", body="b", repo="o/r", language="python")
PLAN = Plan(summary="s", target_files=["app.py"], acceptance_criteria=["c"], test_files=[], complexity="routine")


def sandbox_result(fc):
    return SandboxResult(exit_code=0 if fc is FailureClass.NONE else 1, passed=1, failed=0 if fc is FailureClass.NONE else 1,
                         lint_errors=0, failure_class=fc)


@pytest.fixture()
def stub_nodes(monkeypatch):
    calls = {"sandbox": 0, "repair": 0}
    outcomes = []

    monkeypatch.setattr(nodes, "ingest", lambda s, d: {"base_sha": "abc"})
    monkeypatch.setattr(nodes, "triage", lambda s, d: {"accepted": True})
    monkeypatch.setattr(nodes, "plan", lambda s, d: {"plan": PLAN})
    monkeypatch.setattr(nodes, "freeze_tests", lambda s, d: {"tests_sha256": "h"})
    monkeypatch.setattr(nodes, "generate", lambda s, d: {
        "patch": Patch(unified_diff="d", produced_by=Tier.T3_REMOTE, model="m"), "qa": None})

    def sandbox(s, d):
        calls["sandbox"] += 1
        return {"sandbox": sandbox_result(outcomes.pop(0) if outcomes else FailureClass.ASSERTION)}

    def repair(s, d):
        calls["repair"] += 1
        return {"patch": Patch(unified_diff="d2", produced_by=Tier.T3_REMOTE, model="m"),
                "repair_attempts": s.get("repair_attempts", 0) + 1, "qa": None}

    monkeypatch.setattr(nodes, "sandbox", sandbox)
    monkeypatch.setattr(nodes, "repair", repair)
    monkeypatch.setattr(nodes, "qa", lambda s, d: {"qa": QAResult(high_findings=0, medium_findings=0)})
    monkeypatch.setattr(nodes, "compile_pr", lambda s, d: {"pr_url": "https://example/pr/1"})
    monkeypatch.setattr("factory.breaker.alert", lambda *a: None)
    monkeypatch.setattr(nodes, "alert", lambda *a: None)
    return calls, outcomes


def start(app, run_id="run-test-0001"):
    cfg = graph.config_for(run_id)
    return app, cfg, app.invoke({"run_id": run_id, "ticket": TICKET, "repair_attempts": 0, "model_pins": {},
                                 "attempts": []}, cfg)


def test_two_failures_then_green_stops_at_human_gate(stub_nodes):
    calls, outcomes = stub_nodes
    outcomes += [FailureClass.ASSERTION, FailureClass.ASSERTION, FailureClass.NONE]
    app, cfg, _ = start(graph.compile_app(StubDeps(), MemorySaver()))
    snap = app.get_state(cfg)
    assert snap.next == ("compile_pr",)                 # waiting for approval, nothing pushed
    assert calls == {"sandbox": 3, "repair": 2}
    final = app.invoke(None, cfg)                       # human approves
    assert final["pr_url"] == "https://example/pr/1"


def test_always_failing_halts_after_three_repairs(stub_nodes):
    calls, _ = stub_nodes
    app, cfg, final = start(graph.compile_app(StubDeps(), MemorySaver()))
    assert calls["repair"] == 3 and calls["sandbox"] == 4
    assert "repair limit" in final["halt_reason"]
    assert app.get_state(cfg).next == ()


def test_tamper_halts_without_repair(stub_nodes):
    calls, outcomes = stub_nodes
    outcomes.append(FailureClass.TAMPER)
    _, _, final = start(graph.compile_app(StubDeps(), MemorySaver()))
    assert calls["repair"] == 0 and final["halt_reason"].startswith("sandbox tamper")


def test_triage_reject_halts_quietly(stub_nodes, monkeypatch):
    monkeypatch.setattr(nodes, "triage", lambda s, d: {"accepted": False, "triage_reason": "infra"})
    _, _, final = start(graph.compile_app(StubDeps(), MemorySaver()))
    assert final["halt_reason"] == "triage rejected: infra"


def test_runner_outcome_maps_every_end_state(stub_nodes, monkeypatch):
    from factory.runner import outcome
    calls, outcomes = stub_nodes
    outcomes.append(FailureClass.NONE)
    app, cfg, _ = start(graph.compile_app(StubDeps(), MemorySaver()), "run-out-0001")
    assert outcome(app, cfg) == ("awaiting_approval", "")
    app.invoke(None, cfg)
    assert outcome(app, cfg) == ("pr_open", "https://example/pr/1")
    monkeypatch.setattr(nodes, "triage", lambda s, d: {"accepted": False, "triage_reason": "infra"})
    app, cfg, _ = start(graph.compile_app(StubDeps(), MemorySaver()), "run-out-0002")
    assert outcome(app, cfg) == ("rejected", "infra")
    monkeypatch.setattr(nodes, "triage", lambda s, d: {"accepted": True})
    app, cfg, _ = start(graph.compile_app(StubDeps(), MemorySaver()), "run-out-0003")
    st, detail = outcome(app, cfg)
    assert st == "halted" and "repair limit" in detail

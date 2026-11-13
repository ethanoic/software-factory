"""Gate 1: a person owns the acceptance criteria. Parser, plan node, graph routing and approval."""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from factory import criteria, graph, nodes, runner
from factory.state import FailureClass, Plan, TicketSpec
from test_graph_flow import PLAN, StubDeps, stub_nodes  # noqa: F401  (fixture)


# ------------------------------------------------------------------ parser
def test_parses_bullets_under_the_heading_until_the_next_heading():
    body = ("Intro text\n\n### Acceptance Criteria\n"
            "- Given an empty name, when greet() runs, then it returns \"Hello, World!\"\n"
            "* [ ] Given \"Ada\", when greet() runs,\n"
            "  then it returns \"Hello, Ada!\"\n"
            "1. numbered items count too\n"
            "- feel: the greeting reads as friendly\n"
            "- Manual: works on a phone-sized window\n"
            "\n## Notes\n- not a criterion\n")
    c = criteria.parse(body)
    assert c.testable == ['Given an empty name, when greet() runs, then it returns "Hello, World!"',
                          'Given "Ada", when greet() runs, then it returns "Hello, Ada!"',
                          "numbered items count too"]
    assert c.manual == ["the greeting reads as friendly", "works on a phone-sized window"]


@pytest.mark.parametrize("body", ["", "no section at all", "## Acceptance criteria\n\nprose, no bullets\n",
                                  "## Acceptance criteria that are not a heading match\n- x"])
def test_no_section_or_no_bullets_means_no_criteria(body):
    assert not criteria.parse(body)


def test_last_section_wins_and_limits_hold():
    body = "## Acceptance criteria\n- old\n\n## Acceptance criteria\n" + "".join(f"- c{i} " + "x" * 500 + "\n"
                                                                               for i in range(30))
    c = criteria.parse(body)
    assert "old" not in c.testable and len(c.testable) == criteria.MAX_ITEMS
    assert all(len(x) <= criteria.MAX_CHARS for x in c.testable)


def test_markdown_round_trips():
    md = criteria.as_markdown(["a returns b", "c"], ["feels fast"])
    c = criteria.parse(md)
    assert (c.testable, c.manual) == (["a returns b", "c"], ["feels fast"])


# ------------------------------------------------------------------ plan node: a person's criteria win
class Gw:
    pins: dict = {}

    def __init__(self, raw):
        self.raw = raw

    def complete(self, task, run_id):
        self.task = task
        return self.raw, None


class PlanDeps:
    def __init__(self, raw, tmp_path):
        import subprocess
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        self.gw, self.tmp = Gw(raw), tmp_path

    def repo_path(self, s):
        return self.tmp

    def gateway_for(self, s):
        return self.gw


MODEL_PLAN = ('{"summary": "s", "target_files": ["app.py"], "acceptance_criteria": ["the model\'s own idea"], '
              '"complexity": "trivial"}')


def test_issue_criteria_replace_whatever_the_planner_wrote(tmp_path):
    t = TicketSpec(ticket_id="T", title="t", body="b", repo="o/r", language="python",
                   criteria=["add(1, 2) returns 3"])
    deps = PlanDeps(MODEL_PLAN, tmp_path)
    out = nodes.plan({"ticket": t, "run_id": "r"}, deps)
    assert out["plan"].acceptance_criteria == ["add(1, 2) returns 3"] and out["criteria_source"] == "issue"
    assert "add(1, 2) returns 3" in deps.gw.task.messages[1]["content"]          # the planner is told, too


def test_without_issue_criteria_the_plan_is_only_a_proposal(tmp_path):
    t = TicketSpec(ticket_id="T", title="t", body="b", repo="o/r", language="python")
    out = nodes.plan({"ticket": t, "run_id": "r"}, PlanDeps(MODEL_PLAN, tmp_path))
    assert out["plan"].acceptance_criteria == ["the model's own idea"] and out["criteria_source"] == "proposed"


def test_criteria_gate_never_passes_without_a_person():
    s = {"plan": PLAN, "criteria_source": "proposed"}
    assert "not approved by a person" in nodes.criteria_gate(s, None)["halt_reason"]
    assert nodes.criteria_gate({**s, "criteria_approved": True}, None) == {"criteria_source": "approved"}


# ------------------------------------------------------------------ the graph
def _proposing(monkeypatch):
    monkeypatch.setattr(nodes, "plan", lambda s, d: {"plan": PLAN, "criteria_source": "proposed"})


def _start(app, run_id, **extra):
    cfg = graph.config_for(run_id)
    t = TicketSpec(ticket_id="T-1", title="t", body="b", repo="o/r", language="python")
    app.invoke({"run_id": run_id, "ticket": t, "repair_attempts": 0, "model_pins": {}, "attempts": [], **extra}, cfg)
    return cfg


def test_proposed_criteria_stop_before_any_test_is_written(stub_nodes, monkeypatch):
    calls, outcomes = stub_nodes
    _proposing(monkeypatch)
    app = graph.compile_app(StubDeps(), MemorySaver())
    cfg = _start(app, "run-crit-0001")
    assert app.get_state(cfg).next == ("criteria_gate",)
    assert calls == {"sandbox": 0, "repair": 0}
    assert runner.outcome(app, cfg) == ("awaiting_criteria", "")


def test_resuming_without_approval_halts(stub_nodes, monkeypatch):
    _proposing(monkeypatch)
    app = graph.compile_app(StubDeps(), MemorySaver())
    cfg = _start(app, "run-crit-0002")
    final = app.invoke(None, cfg)                                     # what `make resume` would do
    assert "not approved by a person" in final["halt_reason"]


def test_approval_with_edited_criteria_replaces_the_proposal_and_runs_on(stub_nodes, monkeypatch):
    calls, outcomes = stub_nodes
    outcomes.append(FailureClass.NONE)
    _proposing(monkeypatch)
    seen = {}
    monkeypatch.setattr(nodes, "write_tests", lambda s, d: seen.setdefault("criteria", s["plan"].acceptance_criteria)
                        and {"plan": s["plan"], "tests_feedback": None})
    saver = MemorySaver()
    app = graph.compile_app(StubDeps(), saver)
    cfg = _start(app, "run-crit-0003")
    edited = criteria.Criteria(testable=["shout('ada') returns 'ADA'"], manual=["sounds loud"])
    monkeypatch.setattr(runner, "compile_app", lambda deps, s: app)
    runner.approve_criteria(StubDeps(), saver, "run-crit-0003", edited)
    app.invoke(None, cfg)
    snap = app.get_state(cfg)
    assert snap.next == ("compile_pr",)                               # on to the push gate, not past it
    assert seen["criteria"] == ["shout('ada') returns 'ADA'"]
    assert snap.values["criteria_source"] == "approved"
    assert snap.values["ticket"].manual_checks == ["sounds loud"]
    note = runner.approval_note(snap.values)
    assert "approved by a person" in note and "sounds loud" in note


def test_approving_a_run_that_is_not_waiting_is_refused(stub_nodes, monkeypatch):
    app = graph.compile_app(StubDeps(), MemorySaver())
    _start(app, "run-crit-0004")                                      # issue criteria: no gate
    monkeypatch.setattr(runner, "compile_app", lambda deps, s: app)
    with pytest.raises(SystemExit, match="not waiting"):
        runner.approve_criteria(StubDeps(), None, "run-crit-0004")


def test_issue_criteria_and_qualification_skip_the_gate(stub_nodes, monkeypatch):
    _, outcomes = stub_nodes
    outcomes += [FailureClass.NONE, FailureClass.NONE]
    app = graph.compile_app(StubDeps(), MemorySaver())
    assert app.get_state(_start(app, "run-crit-0005")).next == ("compile_pr",)
    _proposing(monkeypatch)
    app = graph.compile_app(StubDeps(), MemorySaver())
    assert app.get_state(_start(app, "run-crit-0006", qualification=True)).next == ("compile_pr",)


def test_criteria_note_is_the_section_to_paste():
    t = TicketSpec(ticket_id="T", title="t", body="b", repo="o/r", language="python", manual_checks=["feels ok"])
    note = runner.criteria_note({"plan": PLAN, "ticket": t})
    assert criteria.parse(note).testable == list(PLAN.acceptance_criteria)
    assert criteria.parse(note).manual == ["feels ok"]


def test_human_gates_are_exactly_criteria_and_push():
    assert graph.HUMAN_GATES == ["criteria_gate", "compile_pr"]
    assert graph.route_after_plan({"criteria_source": "issue"}) == "write_tests"
    assert graph.route_after_plan({"criteria_source": "proposed"}) == "criteria_gate"
    assert graph.route_after_plan({}) == "criteria_gate"             # unknown source: ask a person
    assert graph.route_after_plan({"halt_reason": "x"}) == "halt"


def test_board_puts_waiting_runs_at_your_criteria_with_both_ways_to_approve():
    from factory.board import NODE_STATION, card_action
    assert NODE_STATION["criteria_gate"] == "criteria"
    card = {"status": "awaiting_criteria", "ref": "o/r#4", "id": "run-x", "issue_url": "https://github.com/o/r/issues/4",
            "number": 4, "detail": "## Acceptance criteria\n- a returns b"}
    a = card_action(card)
    text = " ".join(s.get("text", "") + s.get("command", "") for s in a["steps"])
    assert a["tone"] == "yours" and "a returns b" in text and "make criteria-ok RUN=run-x" in text
    assert "factory:criteria-ok" in text


def test_board_client_and_station_list_know_the_new_state():
    import pathlib
    js = (pathlib.Path(__file__).resolve().parent.parent / "orchestrator" / "factory" / "board_static" / "app.js").read_text()
    assert "awaiting_criteria" in js and "criteria_gate" in js
    from factory.board import STATIONS
    ids = [sid for sid, _, _ in STATIONS]
    assert ids.index("plan") < ids.index("criteria") < ids.index("tests")

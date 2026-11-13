"""Rework (a person sends a change back) and retry memory (a new run knows how the last one ended)."""
from __future__ import annotations

import json

import pytest

from factory import nodes, prompts, runner
from factory.state import History, Patch, Plan, Tier, TicketSpec

T = TicketSpec(ticket_id="o/r#7", title="Add shout()", body="b", repo="o/r", language="python", issue_number=7,
               criteria=["shout('a') returns 'A'"])
PLAN = Plan(summary="s", target_files=["app.py"], acceptance_criteria=["shout('a') returns 'A'"], complexity="trivial")


class Gw:
    pins: dict = {}

    def __init__(self, raw="{}"):
        self.raw, self.calls = raw, 0

    def complete(self, task, run_id):
        self.calls += 1
        self.task = task
        return self.raw, None


class D:
    def __init__(self, gw):
        self.gw = gw

    def gateway_for(self, s):
        return self.gw


# ------------------------------------------------------------------ triage of a rework
@pytest.mark.parametrize("reason,accepted", [("bug: empty name crashes", True), ("BUG : wrong case", True),
                                             ("spec: I want it purple", False), ("Spec:redo", False)])
def test_the_person_can_decide_with_a_prefix_and_no_model_is_asked(reason, accepted):
    gw = Gw()
    out = nodes.triage({"ticket": T.model_copy(update={"rework": reason}), "run_id": "r"}, D(gw))
    assert out["accepted"] is accepted and gw.calls == 0
    if not accepted:
        assert "Acceptance criteria" in out["triage_reason"]


@pytest.mark.parametrize("raw,accepted", [('{"kind": "bug", "reason": "code misses criterion 1"}', True),
                                          ('{"kind": "spec", "reason": "new requirement"}', False),
                                          ("not json at all", False),               # unclear -> the person
                                          ('{"kind": "maybe"}', False)])
def test_otherwise_the_triage_model_decides_and_doubt_goes_to_the_person(raw, accepted):
    gw = Gw(raw)
    out = nodes.triage({"ticket": T.model_copy(update={"rework": "the greeting is wrong for blank names"}),
                        "run_id": "r"}, D(gw))
    assert out["accepted"] is accepted and gw.calls == 1
    assert "the greeting is wrong for blank names" in gw.task.messages[1]["content"]


def test_rework_skips_the_scope_triage_it_already_passed():
    gw = Gw('{"accept": false, "reason": "would reject"}')
    out = nodes.triage({"ticket": T.model_copy(update={"rework": "bug: x"}), "run_id": "r"}, D(gw))
    assert out["accepted"] is True


# ------------------------------------------------------------------ prompts
def test_plan_sees_how_the_previous_run_ended():
    h = History(previous_run="run-1", status="halted", reason="repair limit reached (3 cycles)",
                attempts=[{"attempt": 3, "failure_class": "assertion", "diff_stat": "app.py | 2"}])
    content = prompts.plan(T, "map", h)[1]["content"]
    assert "<history>" in content and "repair limit reached" in content and "assertion" in content
    assert "<history>" not in prompts.plan(T, "map")[1]["content"]       # a first run has none


def test_rework_reason_and_previous_diff_reach_the_planner_and_the_coder():
    t = T.model_copy(update={"rework": "bug: shout('') returns '!'"})
    h = History(previous_run="run-1", status="pr_open", previous_diff="--- a/app.py\n+++ b/app.py\n@@\n+x\n")
    assert "shout('') returns '!'" in prompts.plan(t, "map", h)[1]["content"]
    gen = prompts.generate({"ticket": t, "plan": PLAN, "history": h}, {"app.py": "src"})
    assert "<rework>" in gen[1]["content"] and "+++ b/app.py" in gen[1]["content"]
    plain = prompts.generate({"ticket": T, "plan": PLAN, "history": h}, {"app.py": "src"})
    assert "<rework>" not in plain[1]["content"] and "<diff>" not in plain[1]["content"]


def test_rework_and_history_are_marked_untrusted():
    for tag in ("<rework>", "<history>"):
        assert tag in prompts.UNTRUSTED


# ------------------------------------------------------------------ previous_run and make rework
class Snap:
    def __init__(self, values):
        self.values, self.next = values, ()


class App:
    def __init__(self, values):
        self._v = values

    def get_state(self, cfg):
        return Snap(self._v)


@pytest.fixture()
def db():
    import os
    import psycopg
    dsn = os.environ.get("FACTORY_TEST_DSN")
    if not dsn:
        pytest.skip("set FACTORY_TEST_DSN")
    runner.migrate(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("TRUNCATE runs")
    return dsn


def test_previous_run_carries_halt_reason_attempts_and_for_rework_the_diff(db, monkeypatch):
    from factory.board_state import record_run, ticket_fields
    assert runner.previous_run(db, None, None, T) is None                # first run
    record_run(db, "run-old-0001", **ticket_fields(T), status="halted", detail="repair limit reached token=abc")
    values = {"attempts": [{"attempt": i, "failure_class": "assertion", "diff_stat": "x", "model": "m"}
                           for i in (1, 2, 3, 4)],
              "patch": Patch(unified_diff="--- a/app.py\n+++ b/app.py\n", produced_by=Tier.T3_REMOTE, model="m")}
    monkeypatch.setattr(runner, "compile_app", lambda deps, saver: App(values))
    h = runner.previous_run(db, None, None, T)
    assert (h.previous_run, h.status) == ("run-old-0001", "halted")
    assert "repair limit" in h.reason and "abc" not in h.reason
    assert [a["attempt"] for a in h.attempts] == [2, 3, 4] and "model" not in h.attempts[0]
    assert h.previous_diff == ""                                          # a retry gets no diff
    h = runner.previous_run(db, None, None, T.model_copy(update={"rework": "bug: x"}))
    assert "+++ b/app.py" in h.previous_diff                              # a rework does


def test_make_rework_queues_the_ticket_with_the_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "compile_app", lambda deps, saver: App({"ticket": T}))

    class Deps:
        workspace_root = tmp_path
    (tmp_path / "inbox").mkdir()
    f = runner.rework_ticket(Deps(), None, "run-x", "bug: blank names crash password=hunter2")
    t = TicketSpec(**json.loads(f.read_text()))
    assert t.ticket_id == T.ticket_id and t.rework.startswith("bug: blank names crash")
    assert "hunter2" not in t.rework
    with pytest.raises(SystemExit, match="say what is wrong"):
        runner.rework_ticket(Deps(), None, "run-x", "   ")


def test_a_rework_supersedes_the_change_that_was_waiting(db, monkeypatch):
    import psycopg
    from factory.board_state import record_run, ticket_fields
    record_run(db, "run-wait-0001", **ticket_fields(T), status="awaiting_approval", detail="ok")
    monkeypatch.setattr(runner, "compile_app", lambda deps, saver: App({}))
    monkeypatch.setattr(runner, "run_or_park", lambda deps, saver, run_id, first, intake=None: "running")

    class Deps:
        dsn = db
    run_id, _ = runner.start_ticket(Deps(), None, T.model_copy(update={"rework": "bug: x"}))
    with psycopg.connect(db) as c:
        status, detail = c.execute("SELECT status, detail FROM runs WHERE run_id='run-wait-0001'").fetchone()
    assert status == "superseded" and run_id in detail
    assert "run-wait-0001" not in runner.KEEP_STATUSES                    # its clone can now be cleaned

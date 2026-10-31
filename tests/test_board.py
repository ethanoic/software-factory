"""The factory board: run recording, the board model and its instructions, and the read-only server.
Rule tests need nothing; the rest need FACTORY_TEST_DSN (see test_postgres.py)."""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import psycopg
import pytest

from factory import board
from factory.board_state import heartbeat, record_run, ticket_fields
from factory.state import TicketSpec

DSN = os.environ.get("FACTORY_TEST_DSN")
needs_db = pytest.mark.skipif(not DSN, reason="set FACTORY_TEST_DSN to run Postgres integration tests")
R = "ethanoic/factory-hello-node"


def card(**kw):
    base = {"id": "run-1", "ticket_id": f"{R}#1", "ref": "#1", "repo": R, "number": 1,
            "issue_url": board.issue_url(R, 1), "status": "halted", "detail": "", "repair_attempts": 0,
            "pr_url": None, "trace_url": "http://localhost:3000/trace/x"}
    return {**base, **kw}


# ------------------------------------------------------------------ instructions (no database)
@pytest.mark.parametrize("detail,expect", [
    ("breaker: no qualified model for role 'plan'", "trust"),
    ("breaker: run cap $2.00 or wall clock exceeded at repair#2", "RUN_CAP_USD"),
    ("repair limit reached (3 cycles)", "acceptance criteria"),
    ("sandbox environment: needs a human", "make runner-ts"),
    ("sandbox tamper: needs a human", "trace"),
    ("clone failed: fatal: repository not found", "github.yaml"),
    ("PR creation failed: HTTP 403", "Pull requests"),
    ("something new", "Read the reason"),
])
def test_halt_instructions_match_the_reason(detail, expect):
    a = board.card_action(card(status="halted", detail=detail))
    text = json.dumps(a["steps"])
    assert a["tone"] == "stopped" and expect in text
    assert "factory:ready" in text and a["steps"][0]["url_label"] == "Open trace"   # see, fix, retry


def test_approval_instructions_for_github_and_inbox_tickets():
    gh = board.card_action(card(status="awaiting_approval", repair_attempts=1))
    assert gh["title"] == "Approve #1" and "after 1 repair" in gh["why"]
    labels = [s.get("url_label") for s in gh["steps"]]
    assert "Open issue #1" in labels and any("factory:approve" in s["text"] for s in gh["steps"])
    assert gh["steps"][-1]["command"] == "make approve RUN=run-1"
    inbox = board.card_action(card(status="awaiting_approval", number=None, issue_url=None, ref="HELLO-1"))
    assert [s.get("command") for s in inbox["steps"]][-1] == "make approve RUN=run-1"
    assert not any("factory:approve" in s["text"] for s in inbox["steps"])


def test_pr_review_and_quiet_states():
    pr = board.card_action(card(status="pr_open", pr_url="https://github.com/x/y/pull/6"))
    assert pr["steps"][0]["url"].endswith("/pull/6") and "closes issue #1" in pr["steps"][1]["text"]
    for st in ("running", "parked", "queued", "done", "rejected"):
        assert board.card_action(card(status=st)) is None                       # nothing for you to do


def test_system_actions():
    assert board.system_actions(None, None)[0]["title"] == "Start the factory"
    assert board.system_actions({}, 999)[0]["steps"][0]["command"] == "make up"
    acts = board.system_actions({"missing_roles": ["plan"], "token_warning":
                                 "The Claude plan token has expired. On the Mac, run: make claude-token"}, 10)
    assert [a["title"] for a in acts] == ["Approve a model", "Renew the Claude token"]
    assert all(a["tone"] == "stopped" for a in acts)
    assert board.system_actions({"missing_roles": []}, 10) == []


def test_only_github_links_are_trusted():
    assert board.safe_github_url("https://github.com/a/b/pull/3") == "https://github.com/a/b/pull/3"
    for bad in ("javascript:alert(1)", "https://evil.example/pull/3", "https://github.com.evil.io/x", None):
        assert board.safe_github_url(bad) is None
    assert board.issue_url("../../x", 1) is None and board.issue_url(R, None) is None


# ------------------------------------------------------------------ recording + board model (Postgres)
@pytest.fixture()
def db(tmp_path):
    if not DSN:
        pytest.skip("set FACTORY_TEST_DSN")
    from factory.runner import migrate
    migrate(DSN)
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("TRUNCATE runs, factory_health")
        c.execute("DELETE FROM github_issues")
    inbox = tmp_path / "inbox"
    (inbox / "parked").mkdir(parents=True)
    return inbox


def ticket(n, title="t"):
    return TicketSpec(ticket_id=f"{R}#{n}", title=title, body="b", repo=R, language="javascript", issue_number=n)


@needs_db
def test_runs_move_along_the_line(db):
    record_run(DSN, "run-a", **ticket_fields(ticket(1, "Trim names")), status="running", stage="ingest")
    record_run(DSN, "run-a", status="running", stage="generate")
    b = board.build_board(DSN, db)
    (c,) = b["cards"]
    assert (c["station"], c["ref"], c["title"]) == ("code", "#1", "Trim names")
    record_run(DSN, "run-a", status="awaiting_approval", stage="compile_pr", repair_attempts=1)
    b = board.build_board(DSN, db)
    assert b["cards"][0]["station"] == "approval" and b["actions"][-1]["title"] == "Approve #1"
    record_run(DSN, "run-a", status="pr_open", pr_url="https://github.com/x/y/pull/9")
    assert board.build_board(DSN, db)["cards"][0]["station"] == "review"
    record_run(DSN, "run-a", status="done")
    b = board.build_board(DSN, db)
    assert b["cards"] == [] and b["finished"][0]["status"] == "done"


@needs_db
def test_a_retry_replaces_the_stopped_run_and_qualification_runs_are_hidden(db):
    record_run(DSN, "run-old", **ticket_fields(ticket(4)), status="halted", detail="repair limit reached (3 cycles)")
    time.sleep(0.01)
    record_run(DSN, "run-new", **ticket_fields(ticket(4)), status="running", stage="plan")
    record_run(DSN, "qual-abc", **ticket_fields(ticket(99)), status="running", stage="plan")
    b = board.build_board(DSN, db)
    assert [c["id"] for c in b["cards"]] == ["run-new"]
    assert not any(a["tone"] == "stopped" and "#4" in a["title"] for a in b["actions"])


@needs_db
def test_queue_parked_health_and_unauthorized(db):
    (db / "a.json").write_text(json.dumps({"ticket_id": f"{R}#7", "title": "Seven", "repo": R, "issue_number": 7}))
    (db / "broken.json").write_text("{not json")
    record_run(DSN, "run-p", **ticket_fields(ticket(5)), status="parked", stage="generate")
    (db / "parked" / "run-p.json").write_text(json.dumps({"run_id": "run-p", "parked_at": time.time(), "retries": 2}))
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("INSERT INTO github_issues (repo, number, status) VALUES (%s, 9, 'unauthorized')", (R,))
    heartbeat(DSN, {"runs_today": 3, "daily_limit": 4, "missing_roles": [], "intake_repos": [R]})
    b = board.build_board(DSN, db)
    queued = [c for c in b["cards"] if c["station"] == "queued"]
    parked = next(c for c in b["cards"] if c["status"] == "parked")
    assert [c["ref"] for c in queued] == ["#7"] and parked["station"] == "code" and parked["retries"] == "2 of 6"
    assert b["health"]["online"] and b["health"]["runs_left"] == 1
    assert [a["title"] for a in b["actions"]] == ["Decide on #9"]


@needs_db
def test_stale_heartbeat_puts_start_the_factory_first(db):
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("INSERT INTO factory_health (id, last_seen, data) VALUES (1, now() - interval '10 minutes', '{}')")
    record_run(DSN, "run-h", **ticket_fields(ticket(4)), status="halted", detail="repair limit reached")
    b = board.build_board(DSN, db)
    assert not b["health"]["online"] and b["actions"][0]["title"] == "Start the factory"


@needs_db
def test_merged_pr_closes_the_card(db):
    import httpx

    from factory.github import GitHub, Intake, validate
    record_run(DSN, "run-m", **ticket_fields(ticket(6)), status="pr_open")
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("INSERT INTO github_issues (repo, number, status, run_id) VALUES (%s, 6, 'pr_open', 'run-m')", (R,))
    gh = GitHub("t", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"state": "closed"})))
    intake = Intake(gh, validate({"repos": [{"repo": R}]}), DSN, db)
    assert intake.sync_closed() == 1
    assert board.build_board(DSN, db)["finished"][0]["status"] == "done"


@needs_db
def test_pipeline_steps_record_their_stage(db):
    from factory.breaker import guarded

    class Deps:
        dsn = DSN

        def breaker_for(self, run_id):
            class B:
                def check_wall_clock(self): pass
                def check_repairs(self, n): pass
            return B()
    record_run(DSN, "run-g", **ticket_fields(ticket(2)), status="running", stage="ingest")
    seen = {}

    def node(state, deps):
        with psycopg.connect(DSN) as c:
            seen["stage"] = c.execute("SELECT stage FROM runs WHERE run_id='run-g'").fetchone()[0]
        return {"model_pins": {"repair": "anthropic/x"}}
    guarded(node, Deps(), node_name="repair")({"run_id": "run-g", "ticket": ticket(2), "repair_attempts": 1})
    with psycopg.connect(DSN) as c:
        stage, models, reps = c.execute("SELECT stage, models, repair_attempts FROM runs WHERE run_id='run-g'").fetchone()
    assert seen["stage"] == "repair" and stage == "repair" and models == {"repair": "anthropic/x"} and reps == 1


def test_recording_never_breaks_a_run():
    record_run("postgresql://nobody@127.0.0.1:1/none", "run-x", status="running")       # logged, not raised
    record_run(None, "run-x", status="running")


# ------------------------------------------------------------------ server (Postgres)
@pytest.fixture()
def server(db):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), board.make_handler(DSN, db, {"localhost:8090"}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(url, host="localhost:8090", method="GET"):
    req = urllib.request.Request(url, method=method, headers={"Host": host})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@needs_db
def test_server_serves_the_board_safely(server):
    code, headers, body = get(f"{server}/")
    assert code == 200 and b"Factory board" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"] and headers["X-Content-Type-Options"] == "nosniff"
    code, headers, body = get(f"{server}/api/board")
    assert code == 200 and "actions" in json.loads(body) and headers["Cache-Control"] == "no-store"
    assert get(f"{server}/app.js")[0] == 200 and get(f"{server}/fonts/barlow-latin-400-normal.woff2")[0] == 200


@needs_db
def test_server_refuses_writes_rebinding_and_traversal(server):
    assert get(f"{server}/api/board", method="POST")[0] == 405
    assert get(f"{server}/api/board", host="evil.example:8090")[0] == 421       # DNS rebinding
    assert get(f"{server}/../board.py")[0] == 404
    assert get(f"{server}/%2e%2e/board.py")[0] == 404

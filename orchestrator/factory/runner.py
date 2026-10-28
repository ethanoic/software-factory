"""Entry point.

  python -m factory.runner serve              watch workspace/inbox, run tickets one at a time
  python -m factory.runner run  <ticket.json> run one ticket now (counts toward the daily limit)
  python -m factory.runner approve <run_id>   human gate: open the PR for a run waiting at compile_pr
  python -m factory.runner resume  <run_id>   continue a parked run from its last checkpoint
  python -m factory.runner status             list recent runs
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import sys
import time
import uuid

import psycopg

from factory.breaker import alert
from factory.config import policy
from factory.deps import Deps
from factory.graph import compile_app, config_for
from factory.pool import QuotaExhausted
from factory.state import TicketSpec

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"
POLL_S = 30
PARK_RETRY_S = 60 * 60
PARK_MAX = 6


def migrate(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as c:
        for f in sorted(MIGRATIONS.glob("*.sql")):
            c.execute(f.read_text())


def new_run_id(ticket_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", ticket_id.lower()).strip("-")[:40] or "ticket"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def runs_today(dsn: str) -> int:
    with psycopg.connect(dsn) as c:
        return c.execute("""SELECT count(*) FROM run_ledger WHERE run_id NOT LIKE 'qual-%%'
                            AND started_at > now() - interval '24 hours'""").fetchone()[0]


def daily_limit() -> int | None:
    P = policy()
    if P["anthropic_route"] == "subscription":
        return int(P.get("subscription", {}).get("max_factory_runs_per_day", 4))
    return None


def outcome(app, cfg) -> tuple[str, str]:
    """(status, detail) of a run after invoke returns: awaiting_approval | pr_open | rejected | halted."""
    snap = app.get_state(cfg)
    if snap.next and "compile_pr" in snap.next:
        return "awaiting_approval", ""
    v = snap.values
    if v.get("pr_url"):
        return "pr_open", v["pr_url"]
    if v.get("accepted") is False:
        return "rejected", v.get("triage_reason", "")
    return "halted", v.get("halt_reason") or "halted"


def run_or_park(deps: Deps, saver, run_id: str, first_input, intake=None) -> str:
    app = compile_app(deps, saver)
    cfg = config_for(run_id)
    ticket = (first_input or app.get_state(cfg).values)["ticket"]
    try:
        app.invoke(first_input, cfg)
    except QuotaExhausted as e:
        alert(run_id, ticket.ticket_id, f"Claude plan limit reached; run parked at its last checkpoint ({e})")
        if intake:
            intake.report(ticket, run_id, "parked")
        return "parked"
    st, detail = outcome(app, cfg)
    if st == "awaiting_approval":
        how = "add the approve label on the issue, or " if ticket.issue_number else ""
        alert(run_id, ticket.ticket_id, f"{ticket.ticket_id} passed tests and QA. To open the PR, {how}"
                                        f"run: python -m factory.runner approve {run_id}")
    elif st == "pr_open":
        print(f"[done] {ticket.ticket_id}: {detail}", flush=True)
    if intake:
        intake.report(ticket, run_id, st, detail)
    return st


def start_ticket(deps: Deps, saver, ticket: TicketSpec, intake=None) -> tuple[str, str]:
    run_id = new_run_id(ticket.ticket_id)
    if intake:
        intake.report(ticket, run_id, "running")
    status = run_or_park(deps, saver, run_id, {"run_id": run_id, "ticket": ticket, "repair_attempts": 0,
                                               "model_pins": {}, "attempts": []}, intake)
    return run_id, status


def serve(deps: Deps) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    from factory.github import intake_from_env
    inbox = deps.workspace_root / "inbox"
    for sub in ("processing", "done", "parked", "rejected"):
        (inbox / sub).mkdir(parents=True, exist_ok=True)
    intake = intake_from_env(deps.dsn, inbox)
    with PostgresSaver.from_conn_string(deps.dsn) as saver:
        saver.setup()
        repos = ", ".join(r["repo"] for r in intake.cfg["repos"]) if intake else "none (inbox only)"
        print(f"[serve] watching {inbox}; GitHub intake: {repos}", flush=True)
        while True:
            if intake and intake.due():
                intake.poll()
                for run_id in intake.approvals():
                    run_or_park(deps, saver, run_id, None, intake)
            _retry_parked(deps, saver, inbox, intake)
            limit = daily_limit()
            tickets = sorted(inbox.glob("*.json"), key=lambda p: p.stat().st_mtime)
            if tickets and (limit is None or runs_today(deps.dsn) < limit):
                f = tickets[0]
                moved = inbox / "processing" / f.name
                shutil.move(f, moved)
                try:
                    ticket = TicketSpec(**json.loads(moved.read_text()))
                except Exception as e:
                    print(f"[serve] bad ticket {f.name}: {e}", flush=True)
                    shutil.move(moved, inbox / "rejected" / f.name)
                    continue
                run_id, status = start_ticket(deps, saver, ticket, intake)
                dest = "parked" if status == "parked" else "done"
                meta = {"run_id": run_id, "status": status, "parked_at": time.time(), "retries": 0}
                (inbox / dest / f"{run_id}.json").write_text(json.dumps(meta))
                moved.unlink(missing_ok=True)
            time.sleep(POLL_S)


def _retry_parked(deps: Deps, saver, inbox: pathlib.Path, intake=None) -> None:
    for f in sorted((inbox / "parked").glob("*.json")):
        meta = json.loads(f.read_text())
        if time.time() - meta["parked_at"] < PARK_RETRY_S:
            continue
        status = run_or_park(deps, saver, meta["run_id"], None, intake)
        if status == "parked":
            meta.update(parked_at=time.time(), retries=meta["retries"] + 1)
            if meta["retries"] >= PARK_MAX:
                alert(meta["run_id"], meta["run_id"], f"still parked after {PARK_MAX} retries; resume manually")
                shutil.move(f, inbox / "done" / f.name)
                continue
            f.write_text(json.dumps(meta))
        else:
            shutil.move(f, inbox / "done" / f.name)


def status(dsn: str) -> None:
    with psycopg.connect(dsn) as c:
        rows = c.execute("""SELECT run_id, started_at, spent_usd, tripped, coalesce(trip_reason, '')
                            FROM run_ledger ORDER BY started_at DESC LIMIT 20""").fetchall()
    for r in rows:
        print(f"{r[0]:<52} {r[1]:%Y-%m-%d %H:%M}  ${float(r[2]):.4f}  {'TRIPPED ' + r[4] if r[3] else ''}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="factory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("status")
    sub.add_parser("migrate")
    p_run = sub.add_parser("run"); p_run.add_argument("ticket_file")
    for name in ("approve", "resume"):
        sub.add_parser(name).add_argument("run_id")
    a = ap.parse_args(argv)

    deps = Deps.from_env()
    migrate(deps.dsn)
    if a.cmd == "migrate":
        return
    if a.cmd == "serve":
        return serve(deps)
    if a.cmd == "status":
        return status(deps.dsn)

    from langgraph.checkpoint.postgres import PostgresSaver

    from factory.github import intake_from_env
    intake = intake_from_env(deps.dsn, deps.workspace_root / "inbox")
    with PostgresSaver.from_conn_string(deps.dsn) as saver:
        saver.setup()
        if a.cmd == "run":
            limit = daily_limit()
            if limit is not None and runs_today(deps.dsn) >= limit:
                sys.exit(f"daily limit of {limit} factory runs reached (protects your Claude plan limits)")
            ticket = TicketSpec(**json.loads(pathlib.Path(a.ticket_file).read_text()))
            run_id, st = start_ticket(deps, saver, ticket, intake)
            print(json.dumps({"run_id": run_id, "status": st}))
        else:                                         # approve and resume both continue from the checkpoint
            print(json.dumps({"run_id": a.run_id, "status": run_or_park(deps, saver, a.run_id, None, intake)}))


if __name__ == "__main__":
    main()

"""Entry point.

  python -m factory.runner serve              watch workspace/inbox, run tickets one at a time
  python -m factory.runner run  <ticket.json> run one ticket now (counts toward the daily limit)
  python -m factory.runner approve <run_id>   human gate: open the PR for a run waiting at compile_pr
  python -m factory.runner resume  <run_id>   continue a parked run from its last checkpoint
  python -m factory.runner status             list recent runs
  python -m factory.runner clean [--days N]   delete finished runs' clones from the runs volume
  python -m factory.runner diff <run_id>      print the change a run made (read it before approving)
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
from factory.board_state import heartbeat, record_run, ticket_fields
from factory.config import claude_token_warning, policy
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


def missing_roles() -> list[str]:
    """Roles a run needs right now that have no qualified or trusted model."""
    from factory.pool import NoQualifiedModel, get_pool
    mode = policy().get("local_mode", "assist")
    needed = ["plan", "write_tests", "generate_remote", "review_code", "repair", "security_fix"]
    needed += ["spillover"] if mode == "off" else ["classify"]
    needed += ["generate"] if mode == "generate" else []
    out = []
    for role in needed:
        try:
            get_pool().select(role)
        except NoQualifiedModel:
            out.append(role)
        except Exception as e:                      # database hiccup: report nothing rather than guess
            print(f"[serve] role check failed: {e}", flush=True)
            return []
    return out


def runs_today(dsn: str) -> int:
    with psycopg.connect(dsn) as c:
        return c.execute("""SELECT count(*) FROM run_ledger WHERE run_id NOT LIKE 'qual-%%'
                            AND started_at > now() - interval '24 hours'""").fetchone()[0]


def runs_today_by_repo(dsn: str) -> dict[str, int]:
    with psycopg.connect(dsn) as c:
        rows = c.execute("""SELECT lower(repo), count(*) FROM runs WHERE run_id NOT LIKE 'qual-%%'
                            AND started_at > now() - interval '24 hours' GROUP BY lower(repo)""").fetchall()
    return {r: int(n) for r, n in rows}


def pick_next(waiting: list[tuple[pathlib.Path, str, float]], used: dict[str, int],
              caps: dict[str, int | None]) -> pathlib.Path | None:
    """Fair share across projects. `waiting` = (ticket file, repo, mtime) for tickets whose project can run
    now. Projects at their own daily cap wait; among the rest, the project with the fewest runs in the
    last 24 hours goes next (ties: the oldest ticket), and its oldest ticket is taken."""
    by_repo: dict[str, list[tuple[float, pathlib.Path]]] = {}
    for path, repo, mtime in waiting:
        cap = caps.get(repo)
        if cap is not None and used.get(repo, 0) >= cap:
            continue
        by_repo.setdefault(repo, []).append((mtime, path))
    if not by_repo:
        return None
    repo = min(by_repo, key=lambda r: (used.get(r, 0), min(by_repo[r])[0], r))
    return min(by_repo[repo])[1]


def daily_limit() -> int | None:
    P = policy()
    if P["anthropic_route"] == "subscription":
        return int(P.get("subscription", {}).get("max_factory_runs_per_day", 4))
    return None


def outcome(app, cfg) -> tuple[str, str]:
    """(status, detail) of a run after invoke returns:
    awaiting_criteria | awaiting_arch_approval | awaiting_approval | pr_open | rejected | halted."""
    snap = app.get_state(cfg)
    if snap.next and "criteria_gate" in snap.next:
        return "awaiting_criteria", ""
    if snap.next and "compile_pr" in snap.next:
        if snap.values.get("arch_triggers") and not snap.values.get("arch_approved"):
            return "awaiting_arch_approval", ""
        return "awaiting_approval", ""
    v = snap.values
    if v.get("pr_url"):
        return "pr_open", v["pr_url"]
    if v.get("accepted") is False:
        return "rejected", v.get("triage_reason", "")
    return "halted", v.get("halt_reason") or "halted"


CRITERIA_SOURCE = {"issue": "written in the issue", "approved": "proposed by the planner, approved by a person",
                   "proposed": "proposed by the planner, NOT approved"}


def approval_note(snap: dict) -> str:
    """What the checks found, for the approval comment, the board and `make diff`."""
    from factory.review import summary
    rc = snap.get("red_check")
    ind = (snap.get("independence") or {}).get("write_tests", "independence unknown")
    manual = list(getattr(snap.get("ticket"), "manual_checks", None) or [])
    return (f"Acceptance criteria: {CRITERIA_SOURCE.get(snap.get('criteria_source') or '', 'unknown')}.\n"
            f"Tests: {rc.detail if rc else 'not checked'} before the change (written by "
            f"{(snap.get('model_pins') or {}).get('write_tests', '?')}, {ind}).\n{summary(snap.get('review'))}"
            + (f"\nCheck by hand (feel:): " + "; ".join(manual) if manual else "")
            + ("\n" + arch_note(snap) if snap.get("arch_triggers") else ""))


def criteria_note(snap: dict) -> str:
    """The proposed criteria, as the section a person can paste into the issue and edit."""
    from factory import criteria
    p, t = snap.get("plan"), snap.get("ticket")
    return criteria.as_markdown(list(p.acceptance_criteria) if p else [],
                                list(getattr(t, "manual_checks", None) or []))


def arch_note(snap: dict) -> str:
    from factory.archgate import CHECKLIST
    return ("Architecture gate: this change touches\n" + "\n".join(f"- {x}" for x in snap.get("arch_triggers") or [])
            + f"\n{CHECKLIST}")


def approve_arch(deps: Deps, saver, run_id: str) -> None:
    """A person approved an architecture-changing patch (Gate 3). This is also the push approval."""
    app, cfg = compile_app(deps, saver), config_for(run_id)
    snap = app.get_state(cfg)
    if not snap.next or "compile_pr" not in snap.next or not snap.values.get("arch_triggers"):
        raise SystemExit(f"{run_id} is not waiting for an architecture approval")
    app.update_state(cfg, {"arch_approved": True})


def approve_criteria(deps: Deps, saver, run_id: str, override=None) -> None:
    """A person approved the criteria of a run waiting at criteria_gate. `override` (criteria.Criteria),
    when it has testable items, replaces the planner's proposal: that is the person editing them."""
    app, cfg = compile_app(deps, saver), config_for(run_id)
    snap = app.get_state(cfg)
    if not snap.next or "criteria_gate" not in snap.next:
        raise SystemExit(f"{run_id} is not waiting for criteria approval")
    update: dict = {"criteria_approved": True}
    if override is not None and override.testable:
        update["plan"] = snap.values["plan"].model_copy(update={"acceptance_criteria": list(override.testable)})
        update["ticket"] = snap.values["ticket"].model_copy(update={"criteria": list(override.testable),
                                                                    "manual_checks": list(override.manual)})
    app.update_state(cfg, update)


def run_or_park(deps: Deps, saver, run_id: str, first_input, intake=None) -> str:
    app = compile_app(deps, saver)
    cfg = config_for(run_id)
    ticket = (first_input or app.get_state(cfg).values)["ticket"]
    try:
        app.invoke(first_input, cfg)
    except QuotaExhausted as e:
        alert(run_id, ticket.ticket_id, f"Claude plan limit reached; run parked at its last checkpoint ({e})")
        record_run(deps.dsn, run_id, status="parked", detail=str(e))
        if intake:
            intake.report(ticket, run_id, "parked")
        return "parked"
    st, detail = outcome(app, cfg)
    snap = app.get_state(cfg).values
    if st == "awaiting_approval":
        detail = approval_note(snap)
    elif st == "awaiting_arch_approval":
        detail = approval_note(snap)
    elif st == "awaiting_criteria":
        detail = criteria_note(snap)
    record_run(deps.dsn, run_id, status=st, detail=detail,
               pr_url=detail if st == "pr_open" else None,
               stage={"awaiting_approval": "compile_pr", "pr_open": "compile_pr", "awaiting_arch_approval": "compile_pr",
                      "awaiting_criteria": "criteria_gate"}.get(st),
               repair_attempts=snap.get("repair_attempts", 0), models=snap.get("model_pins") or None)
    if st == "awaiting_approval":
        how = "add the approve label on the issue, or " if ticket.issue_number else ""
        alert(run_id, ticket.ticket_id, f"{ticket.ticket_id} passed tests and QA. Read the change: make diff "
                                        f"RUN={run_id}. To open the PR, {how}run: make approve RUN={run_id}")
    elif st == "awaiting_arch_approval":
        how = "add the factory:arch-approve label on the issue, or " if ticket.issue_number else ""
        alert(run_id, ticket.ticket_id, f"{ticket.ticket_id} passed every check but changes the architecture "
                                        f"({len(snap.get('arch_triggers') or [])} reason(s)). Read: make diff RUN={run_id}. "
                                        f"To push it, {how}run: make arch-ok RUN={run_id}")
    elif st == "awaiting_criteria":
        how = "add the factory:criteria-ok label on the issue (edit its Acceptance criteria section first if " \
              "they are wrong), or " if ticket.issue_number else ""
        alert(run_id, ticket.ticket_id, f"{ticket.ticket_id} needs its acceptance criteria approved before tests "
                                        f"are written. To approve, {how}run: make criteria-ok RUN={run_id}")
    elif st == "pr_open":
        print(f"[done] {ticket.ticket_id}: {detail}", flush=True)
    if intake:
        intake.report(ticket, run_id, st, detail)
    return st


WAITING = ("awaiting_criteria", "awaiting_arch_approval", "awaiting_approval")


def previous_run(dsn: str, saver, deps: Deps, ticket: TicketSpec):
    """The most recent earlier run on this ticket, as a History, or None. Its halt reason and repair attempts
    let a retry start informed; its diff lets a rework start from what the person saw."""
    from factory.config import redact
    from factory.state import History
    try:
        with psycopg.connect(dsn) as c:
            row = c.execute("SELECT run_id, status, coalesce(detail, '') FROM runs WHERE ticket_id = %s "
                            "AND run_id NOT LIKE 'qual-%%' ORDER BY started_at DESC LIMIT 1",
                            (ticket.ticket_id,)).fetchone()
    except psycopg.Error:
        return None
    if row is None:
        return None
    prev, status, detail = row
    values = compile_app(deps, saver).get_state(config_for(prev)).values or {}
    patch = values.get("patch")
    keep = ("attempt", "failure_class", "diff_stat")
    return History(previous_run=prev, status=status,
                   reason=redact(detail)[:1000] if status in ("halted", "rejected") else "",
                   attempts=[{k: a.get(k) for k in keep} for a in (values.get("attempts") or [])][-3:],
                   previous_diff=redact(patch.unified_diff)[:12_000] if (patch and ticket.rework) else "")


def start_ticket(deps: Deps, saver, ticket: TicketSpec, intake=None) -> tuple[str, str]:
    history = previous_run(deps.dsn, saver, deps, ticket)
    run_id = new_run_id(ticket.ticket_id)
    if history is not None and history.status in WAITING:      # a rework replaces a change nobody pushed
        record_run(deps.dsn, history.previous_run, status="superseded", detail=f"replaced by {run_id}")
    record_run(deps.dsn, run_id, **ticket_fields(ticket), status="running", stage="ingest")
    if intake:
        intake.report(ticket, run_id, "running")
    status = run_or_park(deps, saver, run_id, {"run_id": run_id, "ticket": ticket, "repair_attempts": 0,
                                               "model_pins": {}, "attempts": [], "history": history}, intake)
    return run_id, status


def rework_ticket(deps: Deps, saver, run_id: str, reason: str) -> pathlib.Path:
    """`make rework`: send a run's change back with a reason. Queues a new run of the same ticket."""
    from factory.config import redact
    snap = compile_app(deps, saver).get_state(config_for(run_id)).values
    if not snap.get("ticket"):
        raise SystemExit(f"unknown run {run_id}")
    if not reason.strip():
        raise SystemExit("say what is wrong: make rework RUN=<run_id> REASON='bug: ...' (or 'spec: ...')")
    t = snap["ticket"].model_copy(update={"rework": redact(reason)[:4000]})
    inbox = deps.workspace_root / "inbox"
    f = inbox / f"rework__{re.sub(r'[^A-Za-z0-9_-]+', '_', t.ticket_id)}__{int(time.time())}.json"
    f.write_text(t.model_dump_json())
    return f


def serve(deps: Deps) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    from factory.github import intake_from_env
    inbox = deps.workspace_root / "inbox"
    for sub in ("processing", "done", "parked", "rejected"):
        (inbox / sub).mkdir(parents=True, exist_ok=True)
    intake = intake_from_env(deps.dsn, inbox)
    from factory import projects
    from factory.github import config as github_config
    moved = projects.import_from_yaml(deps.dsn, github_config())
    if moved:
        print(f"[serve] moved {', '.join(moved)} from github.yaml into projects; build their runner images "
              f"with make project-rebuild REPO=...", flush=True)
    with PostgresSaver.from_conn_string(deps.dsn) as saver:
        saver.setup()
        ready = [p.repo for p in projects.ready(deps.dsn)]
        print(f"[serve] watching {inbox}; ready projects: {', '.join(ready) or 'none'}; "
              f"GitHub intake: {'on' if intake else 'off (inbox only)'}", flush=True)
        last_token_check = last_roles_check = 0.0
        token_warning, missing = None, []
        while True:
            if time.time() - last_token_check >= 86_400:      # daily: missing or expiring Claude token
                last_token_check = time.time()
                token_warning = claude_token_warning()
                if token_warning:
                    alert("claude-token", "factory", token_warning)
            if time.time() - last_roles_check >= 300:         # roles that would halt a run right now
                last_roles_check = time.time()
                missing = missing_roles()
            limit = daily_limit()
            heartbeat(deps.dsn, {"runs_today": runs_today(deps.dsn), "daily_limit": limit,
                                 "token_warning": token_warning, "missing_roles": missing,
                                 "local_mode": policy().get("local_mode"),
                                 "intake_repos": [p.repo for p in projects.ready(deps.dsn)] if intake else []})
            if intake and intake.due():
                intake.poll()
                for run_id in intake.approvals():
                    run_or_park(deps, saver, run_id, None, intake)
                intake.reworks()
                for run_id in intake.arch_approvals():
                    approve_arch(deps, saver, run_id)
                    run_or_park(deps, saver, run_id, None, intake)
                for run_id, override in intake.criteria_approvals():
                    approve_criteria(deps, saver, run_id, override)
                    run_or_park(deps, saver, run_id, None, intake)
            _retry_parked(deps, saver, inbox, intake)
            f = None
            if limit is None or runs_today(deps.dsn) < limit:
                f = _next_ticket(deps, inbox, intake)
            if f is not None:
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


def _next_ticket(deps: Deps, inbox: pathlib.Path, intake=None) -> pathlib.Path | None:
    """The inbox ticket to run next, or None. Tickets for unregistered repos are rejected here, before a
    run starts; tickets for paused projects, or projects without a runner image yet, wait in the inbox."""
    from factory import projects
    known = {p.repo: p for p in projects.list_all(deps.dsn)}
    waiting = []
    for f in inbox.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            repo = str(data.get("repo", "")).lower()
        except (OSError, ValueError, AttributeError):
            return f                                   # malformed: the caller moves it to rejected/
        p = known.get(repo)
        if p is None:
            reason = f"{repo or 'no repo'} is not a registered project (make project-add REPO=owner/name LANGUAGE=...)"
            print(f"[serve] rejected {f.name}: {reason}", flush=True)
            shutil.move(f, inbox / "rejected" / f.name)
            if intake and data.get("issue_number"):
                try:
                    intake.report(TicketSpec(**data), "-", "rejected", reason)
                except Exception as e:                  # never let reporting stop the loop
                    print(f"[serve] could not report rejection: {e}", flush=True)
            continue
        if p.ready:
            waiting.append((f, repo, f.stat().st_mtime))
    return pick_next(waiting, runs_today_by_repo(deps.dsn), {r: p.max_runs_per_day for r, p in known.items()})


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


KEEP_STATUSES = ("running", "parked", "awaiting_criteria", "awaiting_arch_approval", "awaiting_approval")


def clean(dsn: str, runs_root: pathlib.Path, days: int) -> list[str]:
    """Delete run folders (clone, frozen tests, results) older than `days` whose run no longer needs
    them. Runs that are running, parked or waiting for approval are always kept."""
    with psycopg.connect(dsn) as c:
        keep = {r[0] for r in c.execute("SELECT run_id FROM runs WHERE status = ANY(%s)",
                                        (list(KEEP_STATUSES),)).fetchall()}
    cutoff, removed = time.time() - days * 86_400, []
    for d in sorted(runs_root.iterdir()) if runs_root.is_dir() else []:
        if d.is_dir() and not d.is_symlink() and d.name not in keep and d.stat().st_mtime < cutoff:
            for p in d.rglob("*"):                  # frozen tests are read-only on disk
                if not p.is_symlink():
                    p.chmod(0o700 if p.is_dir() else 0o600)
            shutil.rmtree(d)
            removed.append(d.name)
    return removed


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
    sub.add_parser("clean").add_argument("--days", type=int, default=14)
    p_run = sub.add_parser("run"); p_run.add_argument("ticket_file")
    for name in ("approve", "resume", "diff", "criteria-ok", "arch-ok"):
        sub.add_parser(name).add_argument("run_id")
    p_rw = sub.add_parser("rework"); p_rw.add_argument("run_id"); p_rw.add_argument("reason")
    a = ap.parse_args(argv)

    deps = Deps.from_env()
    migrate(deps.dsn)
    if a.cmd == "migrate":
        return
    if a.cmd == "serve":
        return serve(deps)
    if a.cmd == "status":
        return status(deps.dsn)
    if a.cmd == "clean":
        gone = clean(deps.dsn, deps.runs_root, a.days)
        print(f"removed {len(gone)} finished run folder(s) older than {a.days} days" + (": " + ", ".join(gone) if gone else ""))
        return

    from langgraph.checkpoint.postgres import PostgresSaver

    from factory.github import intake_from_env
    intake = intake_from_env(deps.dsn, deps.workspace_root / "inbox")
    with PostgresSaver.from_conn_string(deps.dsn) as saver:
        saver.setup()
        if a.cmd == "diff":
            snap = compile_app(deps, saver).get_state(config_for(a.run_id)).values
            if not snap.get("patch"):
                sys.exit(f"no change recorded for {a.run_id} (unknown run, or it stopped before writing code)")
            t = snap["ticket"]
            print(f"# {a.run_id} · {t.repo} · {t.ticket_id}: {t.title}\n"
                  f"# repairs: {snap.get('repair_attempts', 0)} · tests passed: "
                  f"{getattr(snap.get('sandbox'), 'passed', '?')}")
            print("\n".join("# " + line for line in approval_note(snap).splitlines()) + "\n")
            print(snap["patch"].unified_diff, end="")
            return
        if a.cmd == "run":
            limit = daily_limit()
            if limit is not None and runs_today(deps.dsn) >= limit:
                sys.exit(f"daily limit of {limit} factory runs reached (protects your Claude plan limits)")
            ticket = TicketSpec(**json.loads(pathlib.Path(a.ticket_file).read_text()))
            run_id, st = start_ticket(deps, saver, ticket, intake)
            print(json.dumps({"run_id": run_id, "status": st}))
        elif a.cmd == "rework":
            f = rework_ticket(deps, saver, a.run_id, a.reason)
            print(json.dumps({"run_id": a.run_id, "queued": f.name}))
        elif a.cmd == "arch-ok":
            approve_arch(deps, saver, a.run_id)
            print(json.dumps({"run_id": a.run_id, "status": run_or_park(deps, saver, a.run_id, None, intake)}))
        elif a.cmd == "criteria-ok":
            approve_criteria(deps, saver, a.run_id)
            print(json.dumps({"run_id": a.run_id, "status": run_or_park(deps, saver, a.run_id, None, intake)}))
        else:                                         # approve and resume both continue from the checkpoint
            snap = compile_app(deps, saver).get_state(config_for(a.run_id))
            nxt = snap.next or ()
            if a.cmd == "approve" and "compile_pr" in nxt and snap.values.get("arch_triggers"):
                sys.exit(f"{a.run_id} changes the architecture:\n{arch_note(snap.values)}\n"
                         f"Read make diff RUN={a.run_id}, then push it with: make arch-ok RUN={a.run_id}")
            if "criteria_gate" in nxt:
                sys.exit(f"{a.run_id} is waiting for its acceptance criteria to be approved, not for a push. "
                         f"Read them on the board, then: make criteria-ok RUN={a.run_id}")
            print(json.dumps({"run_id": a.run_id, "status": run_or_park(deps, saver, a.run_id, None, intake)}))


if __name__ == "__main__":
    main()

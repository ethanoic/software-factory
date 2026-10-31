"""Writes what the factory board shows: one row per run, plus a heartbeat with health facts.

Best effort by design: a board write that fails is logged and ignored, so the board can never stop or
slow a run. Qualification runs (run ids starting with "qual-") are not recorded.
"""
from __future__ import annotations

import json

import psycopg

COLUMNS = ("ticket_id", "title", "repo", "issue_number", "source", "status", "stage", "detail",
           "repair_attempts", "models", "pr_url")


def ticket_fields(ticket) -> dict:
    return {"ticket_id": ticket.ticket_id, "title": ticket.title[:300], "repo": ticket.repo,
            "issue_number": ticket.issue_number, "source": "github" if ticket.issue_number else "inbox"}


def record_run(dsn: str | None, run_id: str, **fields) -> None:
    if not dsn or not run_id or run_id.startswith("qual-"):
        return
    fields = {k: v for k, v in fields.items() if k in COLUMNS and v is not None}
    if "models" in fields:
        fields["models"] = json.dumps(fields["models"])
    if "detail" in fields:
        fields["detail"] = str(fields["detail"])[:1000]
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            if "ticket_id" in fields:                         # first write for this run: full insert
                cols = ["run_id", *fields]
                c.execute(
                    f"INSERT INTO runs ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                    f"ON CONFLICT (run_id) DO UPDATE SET "
                    + ", ".join(f"{k} = EXCLUDED.{k}" for k in fields) + ", updated_at = now()",
                    [run_id, *fields.values()])
            elif fields:
                c.execute(f"UPDATE runs SET {', '.join(f'{k} = %s' for k in fields)}, updated_at = now() "
                          "WHERE run_id = %s", [*fields.values(), run_id])
    except psycopg.Error as e:
        print(f"[board] could not record {run_id}: {e}", flush=True)


def heartbeat(dsn: str, data: dict) -> None:
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("""INSERT INTO factory_health (id, last_seen, data) VALUES (1, now(), %s)
                         ON CONFLICT (id) DO UPDATE SET last_seen = now(), data = EXCLUDED.data""",
                      (json.dumps(data),))
    except psycopg.Error as e:
        print(f"[board] heartbeat failed: {e}", flush=True)

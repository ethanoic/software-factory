-- Per-run cost and breaker ledger (spec §4.2). Idempotent.
CREATE TABLE IF NOT EXISTS run_ledger (
    run_id       text PRIMARY KEY,
    spent_usd    numeric(10,6) NOT NULL DEFAULT 0,
    reserved_usd numeric(10,6) NOT NULL DEFAULT 0,
    repairs      int NOT NULL DEFAULT 0,
    started_at   timestamptz NOT NULL DEFAULT now(),
    tripped      boolean NOT NULL DEFAULT false,
    trip_reason  text
);
CREATE INDEX IF NOT EXISTS run_ledger_day ON run_ledger (started_at);

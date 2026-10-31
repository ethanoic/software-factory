-- State for the factory board (docs: README "Factory board"). Written by the orchestrator, read by the
-- board through a read-only role. Idempotent.
CREATE TABLE IF NOT EXISTS runs (
    run_id          text PRIMARY KEY,
    ticket_id       text NOT NULL,
    title           text NOT NULL DEFAULT '',
    repo            text NOT NULL,
    issue_number    int,
    source          text NOT NULL DEFAULT 'inbox',      -- github | inbox
    status          text NOT NULL,                      -- running | parked | awaiting_approval | halted
                                                        -- | pr_open | done | rejected
    stage           text,                               -- current or last FSM node
    detail          text,                               -- halt reason, PR link, triage reason
    repair_attempts int NOT NULL DEFAULT 0,
    models          jsonb NOT NULL DEFAULT '{}',
    pr_url          text,
    started_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status, updated_at);

-- One row: the orchestrator's heartbeat and the health facts the board shows.
CREATE TABLE IF NOT EXISTS factory_health (
    id         int PRIMARY KEY CHECK (id = 1),
    last_seen  timestamptz NOT NULL,
    data       jsonb NOT NULL DEFAULT '{}'
);

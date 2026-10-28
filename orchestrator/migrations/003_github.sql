-- GitHub intake state (spec §5.6): one row per issue, so re-polls never double-queue. Idempotent.
CREATE TABLE IF NOT EXISTS github_issues (
    repo        text NOT NULL,
    number      int  NOT NULL,
    status      text NOT NULL,       -- queued | running | awaiting_approval | parked | pr_open | halted | rejected | unauthorized
    run_id      text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, number)
);
CREATE INDEX IF NOT EXISTS github_issues_status ON github_issues (status);

-- Projects: the repos this factory works on (README "Projects"). One row each; idempotent.
CREATE TABLE IF NOT EXISTS projects (
    repo             text PRIMARY KEY,                      -- owner/name, lower case
    base_branch      text NOT NULL DEFAULT 'main',
    language         text NOT NULL CHECK (language IN ('python', 'javascript', 'typescript')),
    status           text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused')),
    labelers         jsonb NOT NULL DEFAULT '[]',           -- extra logins allowed to label, on top of github.yaml
    max_runs_per_day int CHECK (max_runs_per_day IS NULL OR max_runs_per_day >= 0),   -- NULL = no own cap
    runner_image     text,                                  -- factory-runner-<slug>:<deps hash>; NULL = not built
    deps_hash        text,                                  -- hash of the lockfiles the image was built from
    image_built_at   timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS runs_repo_started ON runs (repo, started_at);

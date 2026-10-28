-- Model catalog and qualification results (spec §5.2). Idempotent.
CREATE TABLE IF NOT EXISTS model_catalog (
    model_id      text PRIMARY KEY,          -- "anthropic/<model>", "google/<model>", "local/<alias>"
    lab           text NOT NULL,
    tier          text NOT NULL CHECK (tier IN ('t2','t3')),
    in_per_m      numeric(10,4),             -- NULL = unpriced = never selectable
    out_per_m     numeric(10,4),
    ctx           int NOT NULL,
    resident_gb   numeric(6,2),              -- local only
    direct_route  text,                      -- provider route, e.g. anthropic_subscription, google, omlx
    last_seen     timestamptz NOT NULL DEFAULT now(),
    retired       boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS model_qualification (
    model_id      text REFERENCES model_catalog(model_id),
    role          text NOT NULL,
    suite_sha256  text NOT NULL,             -- a changed suite invalidates old scores
    pass_at_1     numeric(5,4) NOT NULL,
    usd_per_task  numeric(10,6) NOT NULL,
    p50_latency_s numeric(8,2) NOT NULL,
    status        text NOT NULL CHECK (status IN ('qualified','promoted','rejected')),
    qualified_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model_id, role, suite_sha256)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_promoted_per_role
    ON model_qualification (role, suite_sha256) WHERE status = 'promoted';

CREATE TABLE effects (
    operation_id uuid PRIMARY KEY, job_id uuid NOT NULL, owner_id text NOT NULL,
    attempt_fence bigint NOT NULL, epoch uuid NOT NULL, kind text NOT NULL,
    fingerprint text NOT NULL, state text NOT NULL DEFAULT 'prepared'
        CHECK(state IN ('prepared','confirmed_succeeded','confirmed_no_effect','uncertain')),
    receipt jsonb, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), recorded_at timestamptz,
    UNIQUE(owner_id,operation_id), FOREIGN KEY(owner_id,job_id) REFERENCES jobs(owner_id,id),
    FOREIGN KEY(job_id,attempt_fence) REFERENCES attempts(job_id,fence)
);
CREATE TABLE effect_receipts (
    operation_id uuid NOT NULL, owner_id text NOT NULL, receipt_hash text NOT NULL,
    outcome text NOT NULL CHECK(outcome IN ('confirmed_succeeded','confirmed_no_effect','uncertain')),
    receipt jsonb NOT NULL, recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(operation_id,receipt_hash),
    FOREIGN KEY(owner_id,operation_id) REFERENCES effects(owner_id,operation_id)
);
CREATE TABLE reconciliations (
    id uuid PRIMARY KEY, job_id uuid NOT NULL, owner_id text NOT NULL,
    actor text NOT NULL, decision text NOT NULL CHECK(decision IN ('confirmed_succeeded','closed_without_retry')),
    reason text NOT NULL CHECK(length(btrim(reason))>0), created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(owner_id,job_id) REFERENCES jobs(owner_id,id)
);

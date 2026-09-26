CREATE TABLE attempts (
    job_id uuid NOT NULL, fence bigint NOT NULL CHECK(fence>0),
    owner_id text NOT NULL, epoch uuid NOT NULL, worker_id text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_until timestamptz NOT NULL, finished_at timestamptz,
    outcome text, safe_reason text,
    PRIMARY KEY(job_id,fence),
    FOREIGN KEY(owner_id,job_id) REFERENCES jobs(owner_id,id)
);

CREATE TABLE mailbox_checkpoints (
    connection_id uuid PRIMARY KEY, owner_id text NOT NULL, generation integer NOT NULL,
    fence bigint NOT NULL DEFAULT 0, epoch uuid NOT NULL, chain_id uuid NOT NULL,
    lease_until timestamptz, next_page_key text, cursor text, baseline_cursor text,
    mode text NOT NULL DEFAULT 'backfill' CHECK(mode IN ('backfill','history')),
    backfill_since timestamptz, resync_required boolean NOT NULL DEFAULT false,
    ingestion_error text, updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(owner_id,connection_id) REFERENCES connections(owner_id,id)
);
CREATE TABLE ingested_events (
    id uuid PRIMARY KEY, owner_id text NOT NULL, connection_id uuid NOT NULL,
    identity text NOT NULL, payload jsonb NOT NULL, job_id uuid,
    decision text NOT NULL CHECK(decision IN ('generation_queued','no_generation_required')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(owner_id,connection_id,identity),
    FOREIGN KEY(owner_id,connection_id) REFERENCES connections(owner_id,id),
    FOREIGN KEY(owner_id,job_id) REFERENCES jobs(owner_id,id)
);
CREATE TABLE page_receipts (
    owner_id text NOT NULL, connection_id uuid NOT NULL, chain_id uuid NOT NULL,
    page_key text NOT NULL, payload_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(owner_id,connection_id,chain_id,page_key),
    FOREIGN KEY(owner_id,connection_id) REFERENCES connections(owner_id,id)
);
CREATE UNIQUE INDEX generation_request ON jobs(owner_id,connection_id,kind,request_key) WHERE kind='gmail_generation';
ALTER TABLE todos ADD COLUMN connection_id uuid;
ALTER TABLE todos ADD COLUMN message_id text;
ALTER TABLE todos ADD COLUMN thread_id text;
ALTER TABLE todos ADD COLUMN importance text CHECK(importance IN ('low','medium','high'));
ALTER TABLE todos ADD COLUMN suggested_action text;
ALTER TABLE todos ADD COLUMN reasoning text;
ALTER TABLE todos ADD COLUMN due_date date;
ALTER TABLE todos ADD COLUMN source_meta jsonb;
ALTER TABLE todos DROP CONSTRAINT todos_status_check;
ALTER TABLE todos ADD CONSTRAINT todos_status_check CHECK(status IN ('open','ongoing','closed'));
ALTER TABLE todos ADD CONSTRAINT todo_connection_owner FOREIGN KEY(owner_id,connection_id) REFERENCES connections(owner_id,id);
CREATE TABLE generation_decisions (
    job_id uuid PRIMARY KEY, owner_id text NOT NULL, decision text NOT NULL CHECK(decision IN ('created','duplicate','skipped')),
    todo_id uuid, safe_reason text NOT NULL, request_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(owner_id,job_id) REFERENCES jobs(owner_id,id), FOREIGN KEY(owner_id,todo_id) REFERENCES todos(owner_id,id)
);

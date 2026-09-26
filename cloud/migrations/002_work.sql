CREATE TABLE todos (
    id uuid PRIMARY KEY, owner_id text NOT NULL REFERENCES owners,
    title text NOT NULL CHECK(length(btrim(title))>0),
    source text NOT NULL CHECK(source IN ('gmail','user')),
    status text NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    dedup_key text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(owner_id,id), UNIQUE(owner_id,dedup_key)
);
CREATE TABLE conversations (
    id uuid PRIMARY KEY, owner_id text NOT NULL REFERENCES owners,
    kind text NOT NULL CHECK(kind IN ('chat','todo')), todo_id uuid,
    generation integer NOT NULL DEFAULT 1 CHECK(generation>0),
    next_message_seq bigint NOT NULL DEFAULT 1 CHECK(next_message_seq>0),
    next_job_order bigint NOT NULL DEFAULT 1 CHECK(next_job_order>0),
    reconciliation_hold boolean NOT NULL DEFAULT false,
    UNIQUE(owner_id,id), FOREIGN KEY(owner_id,todo_id) REFERENCES todos(owner_id,id),
    CHECK((kind='chat' AND todo_id IS NULL) OR (kind='todo' AND todo_id IS NOT NULL))
);
CREATE TABLE jobs (
    id uuid PRIMARY KEY, owner_id text NOT NULL REFERENCES owners,
    conversation_id uuid, generation integer, job_order bigint,
    kind text NOT NULL CHECK(kind IN ('chat','todo','gmail_generation')),
    request_key uuid NOT NULL, input_hash text NOT NULL, input jsonb NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK(state IN
        ('queued','running','retry_pending','succeeded','failed','cancelled','expired','needs_reconciliation')),
    reason text, attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
    fence bigint NOT NULL DEFAULT 0 CHECK(fence>=0), epoch uuid NOT NULL,
    due_at timestamptz NOT NULL DEFAULT clock_timestamp(), expires_at timestamptz NOT NULL,
    lease_until timestamptz, worker_id text, cancel_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), finished_at timestamptz,
    UNIQUE(owner_id,id), UNIQUE(owner_id,conversation_id,generation,request_key),
    UNIQUE(owner_id,conversation_id,generation,job_order),
    UNIQUE(owner_id,conversation_id,generation,id),
    FOREIGN KEY(owner_id,conversation_id) REFERENCES conversations(owner_id,id),
    CHECK((kind='gmail_generation' AND conversation_id IS NULL AND generation IS NULL AND job_order IS NULL)
       OR (kind IN ('chat','todo') AND conversation_id IS NOT NULL AND generation>0 AND job_order>0))
);
CREATE INDEX jobs_pending ON jobs(state,due_at);
CREATE TABLE messages (
    id uuid PRIMARY KEY, owner_id text NOT NULL REFERENCES owners,
    conversation_id uuid NOT NULL, generation integer NOT NULL CHECK(generation>0),
    sequence bigint NOT NULL CHECK(sequence>0),
    role text NOT NULL CHECK(role IN ('user','assistant','notice')),
    content text NOT NULL, origin text NOT NULL, job_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(owner_id,conversation_id,generation,sequence),
    UNIQUE(owner_id,conversation_id,generation,origin),
    FOREIGN KEY(owner_id,conversation_id) REFERENCES conversations(owner_id,id),
    FOREIGN KEY(owner_id,conversation_id,generation,job_id) REFERENCES jobs(owner_id,conversation_id,generation,id)
);
CREATE TABLE outbox (
    id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES jobs,
    epoch uuid NOT NULL, version integer NOT NULL DEFAULT 1,
    due_at timestamptz NOT NULL DEFAULT clock_timestamp(), published_at timestamptz,
    lease_until timestamptz, fence bigint NOT NULL DEFAULT 0 CHECK(fence>=0),
    quarantined boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX outbox_pending ON outbox(due_at) WHERE published_at IS NULL AND NOT quarantined;

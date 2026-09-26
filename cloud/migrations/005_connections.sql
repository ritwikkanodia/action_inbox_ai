CREATE TABLE connections (
    id uuid PRIMARY KEY, owner_id text NOT NULL REFERENCES owners, account text NOT NULL,
    generation integer NOT NULL DEFAULT 1 CHECK(generation>0), active boolean NOT NULL DEFAULT true,
    credential_ref text, granted_scopes text[] NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(owner_id,id), UNIQUE(owner_id,account)
);
ALTER TABLE jobs ADD COLUMN connection_id uuid;
ALTER TABLE jobs ADD COLUMN connection_generation integer;
ALTER TABLE jobs ADD CONSTRAINT jobs_connection_owner FOREIGN KEY(owner_id,connection_id) REFERENCES connections(owner_id,id);
ALTER TABLE jobs ADD CONSTRAINT jobs_connection_pair CHECK(
    (connection_id IS NULL AND connection_generation IS NULL) OR
    (connection_id IS NOT NULL AND connection_generation IS NOT NULL AND connection_generation>0));
CREATE TABLE capabilities (
    token_hash text PRIMARY KEY, owner_id text NOT NULL, job_id uuid NOT NULL,
    fence bigint NOT NULL, epoch uuid NOT NULL, connection_id uuid NOT NULL,
    connection_generation integer NOT NULL CHECK(connection_generation>0),
    scopes text[] NOT NULL CHECK(cardinality(scopes)>0), expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    FOREIGN KEY(owner_id,job_id) REFERENCES jobs(owner_id,id),
    FOREIGN KEY(owner_id,connection_id) REFERENCES connections(owner_id,id),
    FOREIGN KEY(job_id,fence) REFERENCES attempts(job_id,fence)
);

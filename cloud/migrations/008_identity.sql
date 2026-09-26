CREATE TABLE auth_admission (
    singleton boolean PRIMARY KEY CHECK(singleton),
    seat_limit integer NOT NULL CHECK(seat_limit=50)
);
INSERT INTO auth_admission VALUES (true,50);
CREATE TABLE auth_identities (
    owner_id text PRIMARY KEY REFERENCES owners,
    provider text NOT NULL CHECK(provider='google'),
    subject text NOT NULL CHECK(length(subject) BETWEEN 1 AND 255),
    email text NOT NULL CHECK(length(email) BETWEEN 3 AND 320),
    display_name text NOT NULL CHECK(length(display_name)<=200),
    disabled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(provider,subject)
);
CREATE TABLE auth_invites (
    id uuid PRIMARY KEY, email text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL, revoked_at timestamptz,
    redeemed_owner_id text UNIQUE REFERENCES owners,
    CHECK(email=lower(btrim(email))), CHECK(expires_at>created_at)
);
CREATE TABLE auth_sessions (
    token_hash text PRIMARY KEY CHECK(length(token_hash)=64),
    owner_id text NOT NULL REFERENCES auth_identities(owner_id),
    epoch uuid NOT NULL, context_id uuid NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL, revoked_at timestamptz,
    CHECK(expires_at>created_at)
);
CREATE INDEX auth_sessions_owner ON auth_sessions(owner_id);
CREATE INDEX auth_sessions_expiry ON auth_sessions(expires_at);
CREATE TABLE auth_flows (
    state_hash text PRIMARY KEY CHECK(length(state_hash)=64),
    browser_hash text NOT NULL UNIQUE CHECK(length(browser_hash)=64),
    nonce_hash text NOT NULL CHECK(length(nonce_hash)=64),
    verifier_cipher bytea, epoch uuid NOT NULL,
    status text NOT NULL CHECK(status IN ('pending','claimed','finished','failed')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), expires_at timestamptz NOT NULL,
    CHECK((status='pending')=(verifier_cipher IS NOT NULL))
);
CREATE INDEX auth_flows_expiry ON auth_flows(expires_at);
CREATE INDEX auth_invites_expiry ON auth_invites(expires_at);
CREATE TABLE auth_limits (
    key text NOT NULL, bucket_start timestamptz NOT NULL,
    attempts integer NOT NULL CHECK(attempts>0), PRIMARY KEY(key,bucket_start)
);
CREATE INDEX auth_limits_bucket ON auth_limits(bucket_start);
CREATE TABLE auth_audit (
    id uuid PRIMARY KEY, action text NOT NULL, owner_id text,
    actor text NOT NULL, reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE cloud_conversation_bindings (
    owner_id text NOT NULL REFERENCES owners, slot text NOT NULL,
    conversation_id uuid NOT NULL,
    PRIMARY KEY(owner_id,slot), UNIQUE(owner_id,conversation_id),
    FOREIGN KEY(owner_id,conversation_id) REFERENCES conversations(owner_id,id)
);


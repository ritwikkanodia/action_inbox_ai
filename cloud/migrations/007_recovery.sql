CREATE TABLE recovery_checks (
    epoch uuid NOT NULL, check_name text NOT NULL CHECK(check_name IN
        ('deletions_revocations','source_checkpoints','uncertain_effects','credential_invalidation')),
    reviewer text, reason text, reviewed_at timestamptz,
    PRIMARY KEY(epoch,check_name)
);
CREATE TABLE recovery_audit (
    id uuid PRIMARY KEY, epoch uuid NOT NULL, action text NOT NULL,
    reviewer text, reason text, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

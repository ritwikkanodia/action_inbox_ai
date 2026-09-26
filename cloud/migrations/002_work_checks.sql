-- SQL CHECK accepts UNKNOWN: explicitly reject NULL ordering for conversation jobs.
ALTER TABLE jobs ADD CONSTRAINT conversation_job_has_order
    CHECK(kind='gmail_generation' OR (generation IS NOT NULL AND job_order IS NOT NULL));

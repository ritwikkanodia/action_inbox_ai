CREATE TABLE owners (
    owner_id text PRIMARY KEY,
    enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE runtime (
    singleton boolean PRIMARY KEY CHECK(singleton),
    epoch uuid NOT NULL,
    enabled boolean NOT NULL DEFAULT false
);
INSERT INTO runtime(singleton,epoch) VALUES (true,gen_random_uuid());

ALTER TABLE drafts ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE drafts ADD COLUMN repost_of TEXT REFERENCES jobs(id);
ALTER TABLE jobs ADD COLUMN plan_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE jobs ADD COLUMN repost_of TEXT REFERENCES jobs(id);
ALTER TABLE jobs ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN retry_at REAL;
ALTER TABLE jobs ADD COLUMN error_code TEXT;
ALTER TABLE jobs ADD COLUMN error_message TEXT;
ALTER TABLE jobs ADD COLUMN completed_at REAL;
ALTER TABLE jobs ADD COLUMN review_json TEXT NOT NULL DEFAULT '{}';
DROP INDEX prevent_accidental_repost;
CREATE UNIQUE INDEX prevent_accidental_repost ON jobs(owner_id,gid,channel_id)
WHERE repost_of IS NULL AND (state NOT IN ('failed','cancelled') OR published_pages>0);

CREATE TABLE queue_control (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1))
);
INSERT INTO queue_control(singleton) VALUES (1);

CREATE TABLE artifacts (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    page_index INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('source','photo')),
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    transforms_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(job_id,page_index,kind)
);
CREATE TABLE publication_parts (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    part_index INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sending','sent')),
    attempt_id TEXT,
    attempted_at TEXT,
    result_json TEXT,
    evidence TEXT,
    PRIMARY KEY(job_id,part_index)
);
CREATE TABLE batches (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    confirmed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE batch_items (
    batch_id TEXT NOT NULL REFERENCES batches(id),
    position INTEGER NOT NULL,
    draft_id TEXT NOT NULL UNIQUE REFERENCES drafts(id),
    PRIMARY KEY(batch_id,position)
);
CREATE TABLE bot_inbox (
    update_id INTEGER PRIMARY KEY,
    payload_json TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE bot_outbox (
    id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sending','sent','uncertain')),
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0
);

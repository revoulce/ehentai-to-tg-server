CREATE TABLE drafts (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    gid INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('analyzing', 'ready', 'error', 'confirmed')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    selection_json TEXT NOT NULL,
    snapshot_id TEXT REFERENCES gallery_snapshots(id),
    created_at TEXT NOT NULL
);

CREATE TABLE gallery_snapshots (
    id TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER immutable_snapshot_update BEFORE UPDATE ON gallery_snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE TRIGGER immutable_snapshot_delete BEFORE DELETE ON gallery_snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;

CREATE TABLE jobs (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    draft_id TEXT NOT NULL UNIQUE REFERENCES drafts(id),
    owner_id TEXT NOT NULL,
    gid INTEGER NOT NULL,
    channel_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES gallery_snapshots(id),
    selection_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN (
        'queued', 'downloading', 'preparing', 'publishing', 'retry_wait',
        'paused', 'needs_review', 'completed', 'failed', 'cancelled'
    )),
    published_pages INTEGER NOT NULL DEFAULT 0 CHECK (published_pages >= 0),
    published_parts INTEGER NOT NULL DEFAULT 0 CHECK (published_parts >= 0),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX prevent_accidental_repost ON jobs(owner_id, gid, channel_id)
WHERE state NOT IN ('failed', 'cancelled') OR published_pages > 0;

CREATE TABLE job_pages (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    source_page_index INTEGER NOT NULL CHECK (source_page_index > 0),
    page_identity TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'verified', 'prepared')),
    PRIMARY KEY (job_id, source_page_index)
);

CREATE TABLE idempotency (
    owner_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner_id, operation, key)
);

CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    request_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE draft_analysis (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL UNIQUE REFERENCES drafts(id),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN (
        'pending', 'running', 'retry_wait', 'paused', 'succeeded', 'failed', 'cancelled'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    lease_owner TEXT,
    lease_until REAL,
    retry_at REAL,
    error_code TEXT,
    error_message TEXT,
    retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0,1)),
    CHECK ((state='running' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR (state<>'running' AND lease_owner IS NULL AND lease_until IS NULL))
);

INSERT INTO draft_analysis(draft_id)
SELECT id FROM drafts WHERE state='analyzing' ORDER BY created_at,id;

CREATE TABLE source_control (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
    reason_code TEXT,
    next_request_at REAL NOT NULL DEFAULT 0
);
INSERT INTO source_control(singleton) VALUES (1);

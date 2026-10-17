CREATE TABLE bot_drafts (
    draft_id TEXT PRIMARY KEY REFERENCES drafts(id),
    shown_revision INTEGER NOT NULL DEFAULT 0
);

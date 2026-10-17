ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
CREATE TABLE bot_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    next_offset INTEGER NOT NULL DEFAULT 0
);
INSERT INTO bot_state(singleton) VALUES (1);

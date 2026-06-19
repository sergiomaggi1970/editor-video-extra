CREATE TABLE IF NOT EXISTS timeline_clips (
    id               SERIAL PRIMARY KEY,
    timeline_id      UUID        NOT NULL,
    position         INTEGER     NOT NULL,
    original_filename TEXT,
    local_path       TEXT,
    status           TEXT        DEFAULT 'uploaded',
    output_format    TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_timeline_clips_timeline_id
    ON timeline_clips (timeline_id);

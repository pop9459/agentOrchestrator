-- Proposed Linear writes. Nothing reaches Linear unless a row is applied (KAP-95).

CREATE TABLE linear_changes (
    id                INTEGER PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('create_issue', 'update_issue', 'comment')),
    target_identifier TEXT,
    project_id        TEXT,
    payload           TEXT NOT NULL CHECK (json_valid(payload)),
    summary           TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'applied', 'rejected', 'failed')),
    created_by        TEXT NOT NULL DEFAULT 'user',
    task_id           INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    run_id            INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at        TEXT,
    result            TEXT CHECK (result IS NULL OR json_valid(result)),
    error             TEXT
);
CREATE INDEX idx_linear_changes_status ON linear_changes(status);

-- Read-only mirror of Linear issues plus team metadata (states, labels, projects, sync time).

CREATE TABLE linear_issues (
    id                TEXT PRIMARY KEY,
    identifier        TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL,
    description       TEXT,
    state_name        TEXT,
    state_type        TEXT,
    priority          INTEGER,
    labels            TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(labels)),
    project_id        TEXT,
    project_name      TEXT,
    milestone_name    TEXT,
    assignee          TEXT,
    parent_identifier TEXT,
    url               TEXT,
    updated_at        TEXT NOT NULL,
    synced_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX idx_linear_issues_project ON linear_issues(project_name);
CREATE INDEX idx_linear_issues_state ON linear_issues(state_type);

CREATE TABLE linear_meta (
    key   TEXT PRIMARY KEY,
    data  TEXT NOT NULL CHECK (json_valid(data))
);

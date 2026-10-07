-- Task results, retry/loop bookkeeping, delegation depth and attached files.

ALTER TABLE tasks ADD COLUMN result TEXT;
ALTER TABLE tasks ADD COLUMN error TEXT;
ALTER TABLE tasks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0);
ALTER TABLE tasks ADD COLUMN depth INTEGER NOT NULL DEFAULT 0 CHECK (depth >= 0);
ALTER TABLE tasks ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(attachments));

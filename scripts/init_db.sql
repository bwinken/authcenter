-- Auth Center Local Database (SQLite) Schema

CREATE TABLE IF NOT EXISTS user_accounts (
    staff_id    VARCHAR(50)  PRIMARY KEY,
    password_hash VARCHAR(255) NOT NULL,
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME     DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_access_rules (
    id            INTEGER      PRIMARY KEY AUTOINCREMENT,
    app_id        VARCHAR(100) NOT NULL UNIQUE,
    allowed_depts TEXT         DEFAULT '[]',   -- JSON array of dept_code strings, [] means all allowed
    min_level     INTEGER      DEFAULT 1       -- minimum staff level required (1, 2, or 3)
);

-- Example access rules
INSERT OR IGNORE INTO app_access_rules (app_id, allowed_depts, min_level) VALUES
    ('ai_chat_app',   '[]', 1),          -- all departments, level 1+
    ('ai_report_app', '["IT","FIN"]', 2), -- IT & FIN only, level 2+
    ('test_app',      '[]', 1);           -- all departments, level 1+

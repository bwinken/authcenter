-- Auth Center Local Database (SQLite) Schema

CREATE TABLE IF NOT EXISTS user_accounts (
    employee_name VARCHAR(50)  PRIMARY KEY,
    password_hash VARCHAR(255) NOT NULL,
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME     DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS auth_codes (
    code          VARCHAR(64)  PRIMARY KEY,
    employee_name VARCHAR(50)  NOT NULL,
    app_id        VARCHAR(100) NOT NULL,
    expires_at    REAL         NOT NULL
);

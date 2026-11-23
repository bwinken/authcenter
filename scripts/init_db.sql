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

CREATE TABLE IF NOT EXISTS registration_tokens (
    token         VARCHAR(64)  PRIMARY KEY,
    employee_name VARCHAR(50)  NOT NULL,
    app_id        VARCHAR(100) DEFAULT '',
    redirect_uri  TEXT         DEFAULT '',
    expires_at    REAL         NOT NULL,
    -- request：登入頁產生，只能送出註冊申請；register：寄到信箱或管理員產生的連結，才能設定密碼
    purpose       VARCHAR(16)  NOT NULL DEFAULT 'request'
);

CREATE TABLE IF NOT EXISTS user_app_permissions (
    employee_name VARCHAR(50)  NOT NULL,
    app_id        VARCHAR(100) NOT NULL,
    level         INTEGER      NOT NULL DEFAULT 1,
    granted_by    VARCHAR(50)  NOT NULL DEFAULT '',
    granted_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (employee_name, app_id)
);

CREATE TABLE IF NOT EXISTS app_admins (
    employee_name VARCHAR(50)  NOT NULL,
    app_id        VARCHAR(100) NOT NULL,
    assigned_by   VARCHAR(50)  NOT NULL DEFAULT '',
    assigned_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (employee_name, app_id)
);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id            INTEGER      PRIMARY KEY AUTOINCREMENT,
    admin_name    VARCHAR(50)  NOT NULL,
    action        VARCHAR(100) NOT NULL,
    target        TEXT         DEFAULT '',
    details       TEXT         DEFAULT '',
    ip_address    VARCHAR(45)  DEFAULT '',
    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP
);

-- Keycloak 登入的一次性 state（auth_provider 為 keycloak / both 的 App 使用）
CREATE TABLE IF NOT EXISTS keycloak_login_states (
    state         VARCHAR(64)  PRIMARY KEY,
    binding_hash  VARCHAR(64)  NOT NULL,
    nonce         VARCHAR(64)  NOT NULL,
    code_verifier VARCHAR(128) NOT NULL,
    app_id        VARCHAR(100) NOT NULL,
    redirect_uri  TEXT         NOT NULL,
    flow          VARCHAR(8)   NOT NULL DEFAULT 'auth',
    oidc_state    TEXT         DEFAULT '',
    oidc_nonce    TEXT         DEFAULT '',
    oidc_scope    TEXT         DEFAULT '',
    expires_at    REAL         NOT NULL
);

-- Indexes for efficient expiry cleanup
CREATE INDEX IF NOT EXISTS idx_auth_codes_expires_at ON auth_codes(expires_at);
CREATE INDEX IF NOT EXISTS idx_reg_tokens_expires_at ON registration_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_kc_states_expires_at ON keycloak_login_states(expires_at);

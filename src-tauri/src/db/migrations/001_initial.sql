-- launcher.db — initial schema (migration 001)
-- See docs/LAUNCHER_BACKEND_API.md §3 in the Claude Orchestrator repo for rationale.

-- Projects registered in the launcher. A project is a folder on disk
-- assigned to ONE host (base orchestrator OR MAO, never both).
CREATE TABLE projects (
    id              TEXT PRIMARY KEY,          -- UUID v4
    name            TEXT NOT NULL,
    folder_path     TEXT NOT NULL UNIQUE,      -- absolute path
    host            TEXT NOT NULL              -- "base" | "mao"
                    CHECK (host IN ('base','mao')),
    created_at      INTEGER NOT NULL,          -- unix ms
    updated_at      INTEGER NOT NULL
);

CREATE INDEX idx_projects_host ON projects(host);

-- Modules installed for a given project.
CREATE TABLE module_installs (
    id              TEXT PRIMARY KEY,          -- UUID
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    module_id       TEXT NOT NULL,             -- manifest.id, e.g. "vct-coordination"
    module_version  TEXT NOT NULL,             -- manifest.version at install time
    install_path    TEXT NOT NULL,             -- resolved {install_dir}
    status          TEXT NOT NULL              -- installing | installed | running | stopped | error
                    CHECK (status IN ('installing','installed','running','stopped','error')),
    enabled         INTEGER NOT NULL DEFAULT 1,
    installed_at    INTEGER NOT NULL,
    last_started_at INTEGER,
    last_error      TEXT,
    UNIQUE(project_id, module_id)
);

CREATE INDEX idx_mi_project ON module_installs(project_id);
CREATE INDEX idx_mi_status ON module_installs(status);

-- Per-project MODULE settings (non-secret). Secrets live in OS keychain.
-- Values are JSON-encoded so arrays / ints / strings can all be stored.
CREATE TABLE module_settings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    module_id       TEXT NOT NULL,
    setting_key     TEXT NOT NULL,
    setting_value   TEXT NOT NULL,             -- JSON
    UNIQUE(project_id, module_id, setting_key)
);

CREATE INDEX idx_ms_project ON module_settings(project_id);

-- Codegraph access matrix: which projects can READ which other projects'
-- code graphs. Default is no access (not listing a row == no access).
CREATE TABLE codegraph_access (
    grantor_project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    grantee_project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    access_level         TEXT NOT NULL
                         CHECK (access_level IN ('read','none')),
    granted_at           INTEGER NOT NULL,
    PRIMARY KEY (grantor_project_id, grantee_project_id)
);

-- KG collection access matrix: each project declares which collections
-- (named Weaviate collections) it has read/write access to.
CREATE TABLE kg_collection_access (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    collection_name TEXT NOT NULL,
    access_level    TEXT NOT NULL
                    CHECK (access_level IN ('read','write','none')),
    PRIMARY KEY (project_id, collection_name)
);

-- License tier cache. Exactly one row; refreshed from the validate-tier
-- edge function. On cache miss / network failure within the 3-day grace
-- period, the cached row remains authoritative.
CREATE TABLE tier_cache (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    orchestrator_tier TEXT NOT NULL
                      CHECK (orchestrator_tier IN ('free','pro','mao','enterprise')),
    module_licenses   TEXT NOT NULL,            -- JSON: {module_id: {tier, expires_at}}
    last_validated    INTEGER NOT NULL,
    last_error        TEXT
);

-- Seed the tier cache with a default free row so reads never return empty.
INSERT INTO tier_cache (id, orchestrator_tier, module_licenses, last_validated, last_error)
    VALUES (1, 'free', '{}', 0, NULL);

-- Audit log for privileged operations. Never logs values, only operation
-- metadata: which project, which module, which secret key (not value).
CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    operation       TEXT NOT NULL,              -- "secret_set", "project_host_switch", etc.
    project_id      TEXT,
    module_id       TEXT,
    detail          TEXT NOT NULL,              -- JSON summary (no values)
    created_at      INTEGER NOT NULL
);

CREATE INDEX idx_audit_created ON audit_log(created_at);

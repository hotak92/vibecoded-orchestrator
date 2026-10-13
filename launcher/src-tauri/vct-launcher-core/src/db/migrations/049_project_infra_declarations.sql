-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
-- Migration 049 (v0.2.102, W5-A / M3): per-project infrastructure declarations.
--
-- WHAT IT ANSWERS: "which pieces of infrastructure on this machine belong to a
-- project, not to VCO?". A project that runs its own Weaviate / Ollama for its
-- own runtime (next to the dev KG VCO keeps for it) declares them here, and
-- every VCO surface that could otherwise mistake them for VCO's leaves them
-- alone. Three kinds, one row per declared value:
--
--   external_collection — a Weaviate class name the project owns. The doctor's
--                         unclaimed-class reading, the binding-evidence heal
--                         and the fixture-shaped listing skip it (and say so).
--   unmanaged_container — a container name VCO must never heal, start,
--                         re-create, label or adopt: the hub watchdog, the
--                         compose-recovery heals and the service detection
--                         that feeds adoption all honour it.
--   local_port          — a loopback port one of the project's own services
--                         listens on. Projected into the project's env as
--                         VCT_SSRF_EXTRA_LOCAL_PORTS (the WebFetch SSRF guard
--                         allows it for THIS project only) and never probed
--                         or adopted by VCO's service detection.
--
-- Machine-wide effect: a container or a collection is ONE object on the
-- machine, so a declaration by any project protects it everywhere. The SSRF
-- allowance is the exception — it is per-project, through that project's env.
--
-- Additive: a new table, no existing row touched. ON DELETE CASCADE with the
-- project, like every other per-project table (026, 044).

CREATE TABLE IF NOT EXISTS project_infra_declarations (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL
                CHECK (kind IN ('external_collection', 'unmanaged_container', 'local_port')),
    value       TEXT NOT NULL CHECK (length(trim(value)) > 0),
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (project_id, kind, value)
);

CREATE INDEX IF NOT EXISTS idx_project_infra_declarations_kind
    ON project_infra_declarations(kind, value);

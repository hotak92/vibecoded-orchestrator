-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
-- Migration 047 (v0.2.97): service_endpoints — where VCO's three core
-- services are reached, as ONE row per service.
--
-- WHAT IT ANSWERS: "where is Weaviate / Ollama / code-embed on this machine,
-- and who owns the container behind it?". Before this table the answer was
-- spread over `~/.vct/services.toml` adoption rows, three app_state
-- `*.port_override` keys nothing ever wrote, the `weaviate_url` of a
-- `vct-config.toml` sitting next to whichever binary read it, and the
-- endpoint env vars of whatever process asked. Each reader picked a different
-- subset, so the hub, the projection and the MCP registration could name
-- three different Weaviates.
--
-- READERS (machine-scoped processes only — the hub, the launcher, the env
-- projection, the MCP registration):
--   * Rust: `vct_launcher_core::db::service_endpoints` (row read) +
--     `services::service_endpoints` (row -> URL/port render -> compiled
--     default when the row is absent).
--   * Python: `vco_lib.service_endpoints` (the same render, pinned by
--     `tests/fixtures/service_endpoint_parity.json`).
-- Project-scoped clients (MCPs, hooks, scripts) never read this table; they
-- read the transport the projection renders from it (WEAVIATE_URL, …).
--
-- WRITER: `vco_lib.service_endpoints` (Python) and nothing else. It enforces
-- every CHECK below before it writes, and its tests load THIS file, so the
-- two cannot drift.
--
-- NO SEED ROWS. A row states a fact about this machine (a detected or chosen
-- endpoint); a default written here would be indistinguishable from a
-- verified one. An absent row means "not yet reconciled" and every resolver
-- answers the compiled default for it (8081/50052, 11435, 11440).
--
-- MODES:
--   vco_managed        VCO's own compose owns the container (host must be
--                      local: the compose file publishes on this machine).
--   adopted_container  someone else's container, started/stopped BY NAME
--                      only, never recreated (container_name required).
--   adopted_external   a URL — a native process or a remote host; VCO has
--                      no lifecycle over it.
-- code_embed is always vco_managed (its cache identity lives in
-- data_mount_json so a recreate reuses it).
--
-- Plain CREATE TABLE IF NOT EXISTS — idempotent by construction AND by the
-- runner's version check; not self-transactional (rides the runner's outer
-- transaction). No FK: the table is machine-global, not per project.
-- LAUNCHER_DB_TABLE_SET_VERSION bumps 46->47 atomically with this migration
-- (B-2 discipline).

CREATE TABLE IF NOT EXISTS service_endpoints (
  service           TEXT PRIMARY KEY CHECK (service IN ('weaviate','ollama','code_embed')),
  mode              TEXT NOT NULL CHECK (mode IN ('vco_managed','adopted_container','adopted_external')),
  scheme            TEXT NOT NULL DEFAULT 'http' CHECK (scheme IN ('http','https')),
  host              TEXT NOT NULL DEFAULT 'localhost',
  port              INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
  grpc_port         INTEGER CHECK (grpc_port IS NULL OR grpc_port BETWEEN 1 AND 65535),
  -- vco_managed: the compose container_name; adopted_container: the pinned name
  container_name    TEXT,
  -- observed com.docker.compose.project label (diagnostics + guards)
  compose_project   TEXT,
  -- observed {"kind":"bind|volume","source":…,"destination":…}
  data_mount_json   TEXT,
  -- code_embed = 0 on CPU hosts (the gpu profile is not run)
  enabled           INTEGER NOT NULL DEFAULT 1,
  -- adopted_container: start by name when VCO needs it
  autostart         INTEGER NOT NULL DEFAULT 1,
  -- install_probe | user_gui | user_cli | migrated:<store> | live_reconcile
  source            TEXT NOT NULL,
  confirmed_by_user INTEGER NOT NULL DEFAULT 0,
  -- last successful health probe (unix ms)
  verified_at       INTEGER,
  updated_at        INTEGER NOT NULL,
  CHECK (mode <> 'adopted_container' OR container_name IS NOT NULL),
  CHECK (mode <> 'vco_managed' OR host IN ('localhost','127.0.0.1')),
  CHECK (service <> 'weaviate' OR grpc_port IS NOT NULL),
  CHECK (service <> 'code_embed' OR mode = 'vco_managed')
);

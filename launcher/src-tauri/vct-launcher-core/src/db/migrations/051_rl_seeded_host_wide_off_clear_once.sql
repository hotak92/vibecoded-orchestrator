-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (c) 2026 VibeCoded Tools
-- Migration 051 (v0.2.106, owner ruling 2026-11-12 "clear once"): remove the
-- host-wide RL scoring "off" row the old installers SEEDED, once.
--
-- BACKGROUND
-- ----------
-- From v0.2.52 (V52-AD) to v0.2.105, every `install.py` run wrote a
-- host-wide (project_id IS NULL) `vct-rl-reranker` / `enabled_for_project`
-- row with the value `false` whenever none existed (fewer than 500 RL events
-- recorded). v0.2.106 retired that seed: with no choice made, RL scoring now
-- follows the bundled weights (`vco_lib/rl_scoring_default.py` — ON only when
-- a trained model ships for the active embedding profile). A seeded row is
-- not a choice, but the enable cascade cannot tell it from one: left in
-- place it pins every existing install OFF even after a trained model ships.
--
-- WHAT IT DELETES
-- ---------------
-- Exactly that row — host-wide, RL reranker, `enabled_for_project`, value
-- `false` — unless the USER chose it: the launcher audits every host-wide
-- write a user makes (`module_global_enabled_changed`, from the Modules
-- panel and the dashboard's RL switch; the seed was never audited). When the
-- LATEST such audit row for the RL reranker records `"enabled": false`, the
-- current `false` row is the user's (the seed never overwrote an existing
-- row) and stays. A latest audit of `true` or of a clear (`null`) means the
-- user's choice was later replaced by the seed, so the row goes.
--
-- NOT TOUCHED: every per-project row (project_id IS NOT NULL, either value),
-- a host-wide `true`, every other module and key.
--
-- ONCE: the schema-migration runner applies each version exactly once and
-- records it in `_schema_migrations` (that record is the stamp), so a
-- host-wide choice the user makes AFTER this ran — `false` included — is
-- never removed again. It runs wherever the new launcher or hub first opens
-- launcher.db, which every existing install does on its v0.2.106 update; a
-- fresh install has no such row, and this is a no-op.
--
-- NOT self-transactional: a plain DELETE, rides the runner's outer
-- transaction. LAUNCHER_DB_TABLE_SET_VERSION bumps 50 -> 51 atomically with
-- this migration's Rust registration.

DELETE FROM module_settings
WHERE project_id IS NULL
  AND module_id = 'vct-rl-reranker'
  AND setting_key = 'enabled_for_project'
  AND setting_value = 'false'
  AND COALESCE((
      SELECT json_extract(a.detail, '$.enabled')
      FROM audit_log AS a
      WHERE a.operation = 'module_global_enabled_changed'
        AND a.module_id = 'vct-rl-reranker'
        AND json_valid(a.detail)
      ORDER BY a.created_at DESC, a.id DESC
      LIMIT 1
  ), -1) IS NOT 0;

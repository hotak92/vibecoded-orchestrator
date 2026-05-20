-- Generic key-value table for launcher application state that needs to
-- survive across launches AND respect VCT_STATE_DIR isolation (Bug 14).
--
-- Background: the onboarding-completion flag used to live in the WebView
-- localStorage at `vct.onboarding_complete`. localStorage is keyed by the
-- WebView's origin + the app identifier (`com.vibecodedtools.launcher`),
-- so a dev launcher run with `VCT_STATE_DIR=$HOME/.vct-dev/` and a
-- production launcher (default `~/.vct/`) shared the SAME localStorage —
-- meaning a user who completed onboarding on prod would never see the
-- wizard on dev, and vice versa. That broke the dev/prod isolation that
-- VCT_STATE_DIR was supposed to enable.
--
-- Fix: move launcher-state flags out of localStorage into launcher.db
-- (which IS isolated by VCT_STATE_DIR via crate::paths::vct_root_dir).
-- Frontend reads/writes go through Tauri commands instead of direct
-- localStorage access.
--
-- Why a key-value table instead of a dedicated `onboarding_state` table:
-- we expect more flags to migrate over time (e.g. `last_seen_release`,
-- `dismissed_callout_X`, `accepted_telemetry_terms`). A single typed
-- key-value sink avoids one migration per flag.
--
-- Migration semantics for existing users: on first read after this
-- migration ships, if the launcher.db has no row AND the WebView's
-- localStorage has the legacy `vct.onboarding_complete=true` key, the
-- frontend's onboarding read-path performs a one-shot copy localStorage
-- → DB and then deletes the localStorage key. Existing users do NOT
-- see the wizard re-fire. This logic lives in the layout's preflight,
-- gated by the `is_set` field of the new `get_app_state_value` Tauri
-- command's response so we can tell "row absent" from "row present and
-- false".

CREATE TABLE IF NOT EXISTS app_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL  -- unix millis
);

-- No initial seed rows: presence of a row signals "the user has set
-- this", absence signals "default behaviour" (e.g. onboarding flag
-- absent = wizard should fire).

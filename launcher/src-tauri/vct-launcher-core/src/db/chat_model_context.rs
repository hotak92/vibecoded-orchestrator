// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! The version-keyed CHAT-model context table (v0.2.92, WP-11, migration 043).
//!
//! `launcher.db` is the canonical home; the model gateway
//! (`claude_mcp_servers/model_router/`) consumes an EXPORTED copy of it as a
//! JSON file, because the gateway is deliberately hub-independent and must
//! keep working when neither the launcher nor `vct-hub` is running.
//!
//! What the table decides: whether the gateway advertises a model id to
//! Claude Code as `<id>[1m]`. The client sizes its context indicator and its
//! `/compact` thresholds from the window it believes the selected model has,
//! and for an unknown id it assumes a conservative default — so a 1M-context
//! model reads as far fuller than it is and compaction fires early.
//!
//! ## EXACT full-model-id keys — the whole reason this table exists
//!
//! `glm-5.2` has a 1M window; `glm-5.1` has 200K. A `glm-5*` family rule
//! would overstate the smaller one by 5x, and the user would discover it only
//! when a long session silently truncated. So: no prefix match, no wildcard,
//! no "nearest version". The primary key is the full id, verbatim.
//!
//! ## This is NOT `MODEL_TOKEN_LIMITS`
//!
//! `claude_mcp_servers/weaviate_mcp/chunking.py::MODEL_TOKEN_LIMITS` answers a
//! question phrased with the same words and means something else. It covers
//! EMBEDDING models, sets Ollama's `num_ctx` for the chunker, matches
//! PARTIALLY on purpose (an Ollama tag varies by quantisation while the
//! architectural limit does not), and is a WIRE-FORMAT input to stored
//! embeddings guarded by the chunker-revision sentinel. This table covers CHAT
//! models, drives only what the gateway ADVERTISES, and changing it re-embeds
//! nothing. Do not merge them; do not teach either to read the other. Full
//! ruling: `PLAN-v0292-EXTENSION-2026-09-02.md` §3.19.
//!
//! ## Citation is enforced, not requested
//!
//! Every row carries the official vendor page it was read from. A blank
//! `source` is refused by a SQL `CHECK` *and* by [`ChatModelContextInput::
//! validated`], because guessing a window is exactly what a version-keyed
//! table exists to prevent. The gateway's reader independently ignores an
//! uncited row and warns; this side makes sure our own writer never produces
//! one for it to ignore.
//!
//! ## Layering
//!
//! This module is pure DB + the export DOCUMENT builder. It does no file I/O
//! and reads no environment: the seed is loaded, and the export written, one
//! layer up in `commands::chat_model_context` (launcher crate), which owns
//! path resolution and atomic writes. The document builder lives HERE rather
//! than there because `vct-hub` serves the same shape over
//! `GET /api/v1/chat-model-context` and both crates depend on this one — one
//! shape, one home, no second serialiser to drift.

use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};

use super::Db;

/// Schema version of the exported document. The gateway's reader
/// (`model_router/context_table.py::SUPPORTED_SCHEMA_VERSIONS`) accepts
/// exactly this set; a bump here without a bump there makes the gateway fall
/// back to its shipped seed and say so in a warning — a named, visible
/// degradation rather than a silent one.
pub const EXPORT_SCHEMA_VERSION: u64 = 1;

/// Value of the export's `source` field. Distinguishes a launcher-written
/// table from the seed the gateway ships inside its own wheel.
pub const EXPORT_SOURCE_LAUNCHER_DB: &str = "launcher.db";

// ─── Row types ────────────────────────────────────────────────────────────

/// One persisted row. Serialised over Tauri IPC as-is; the Preferences pane
/// renders every field.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatModelContextRow {
    /// FULL vendor model id, verbatim. Primary key. Never a family prefix.
    pub model_id: String,
    /// Vendor registry id (`zai`, ...). Rendered in the pane; carried in the
    /// export so a consumer can group without a second source of truth.
    pub vendor: String,
    /// Total context tokens (the vendor's own decimal-K figure).
    pub context_window: i64,
    /// Max output tokens. `0` = UNSTATED: the vendor publishes no figure
    /// (cross-language contract — the Python gateway folds 0 to None via
    /// `catalog::_positive`). A consumer must never render or apply 0 as a
    /// token count. Negative is impossible (SQL CHECK, migration 046).
    pub max_output: i64,
    /// `true` → the gateway advertises this id as `<id>[1m]`.
    pub window_1m: bool,
    /// `true` → the vendor's page states this model takes TEXT-ONLY input:
    /// it cannot see an image block. The gateway replaces image blocks
    /// routed to a flagged model with a short text note instead of letting
    /// the request silently degrade (v0.2.101 parity gap 7); the pane badges
    /// it "text only" so the routing choice is visible BEFORE a chat fails.
    /// Conservative default `false` — the gateway reader's own direction for
    /// an absent key: an unflagged model keeps its image blocks untouched.
    pub text_only: bool,
    /// The citation: the official vendor page these numbers were read from.
    /// Never empty (SQL `CHECK` + [`ChatModelContextInput::validated`]).
    pub source: String,
    /// Optional caveat travelling with the citation. Load-bearing for two
    /// shipped rows — see the seed file — and preserved through the DB so a
    /// future editor cannot "correct" a row backwards for lack of context.
    pub source_note: String,
    /// `true` = a human edited this row from the launcher. The reseed guard.
    pub user_edited: bool,
    /// ISO-8601 UTC, e.g. `2026-09-02T18:04:11Z`.
    pub updated_at: String,
}

/// The caller-supplied half of a row: everything except the two fields the
/// STORE owns (`user_edited`, which is decided by which code path is
/// writing, and `updated_at`, which is the clock).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatModelContextInput {
    pub model_id: String,
    pub vendor: String,
    pub context_window: i64,
    /// Max output tokens; `0` = UNSTATED (see [`ChatModelContextRow`]).
    /// Negative is refused by [`validated`](Self::validated).
    pub max_output: i64,
    pub window_1m: bool,
    /// `#[serde(default)]` (false) so a GUI payload that predates the field
    /// is accepted rather than failing deserialisation — the same tolerance
    /// the gateway's reader gives an absent key.
    #[serde(default)]
    pub text_only: bool,
    pub source: String,
    /// Optional; `""` when absent. `#[serde(default)]` so a GUI payload that
    /// omits it is accepted rather than failing deserialisation.
    #[serde(default)]
    pub source_note: String,
}

impl ChatModelContextInput {
    /// Trim, then refuse anything the table must never hold.
    ///
    /// Front-of-house validation, in FRONT of the SQL `CHECK`s that back it
    /// up: the constraint gives a `SqliteFailure` a user cannot act on, this
    /// gives a sentence naming the field and why. Both exist because either
    /// alone is a single point of failure — the CHECK cannot be bypassed but
    /// cannot explain, and this can explain but could be bypassed by a future
    /// caller that forgets it.
    ///
    /// The `source` rule is "non-empty after trimming" and NOT "must look
    /// like a URL", deliberately: the gateway's reader applies exactly that
    /// rule, and a validator stricter than the file format would refuse rows
    /// the format accepts (a vendor PDF, an internal doc reference). The
    /// *shipped seed* is held to the stronger `https://docs.z.ai/` bar by its
    /// own test in `tests/test_model_router_context_table.py`; a row a user
    /// adds by hand only has to be cited, not cited with a URL.
    pub fn validated(self) -> Result<Self, String> {
        let model_id = self.model_id.trim().to_string();
        if model_id.is_empty() {
            return Err("model id is required (the full vendor id, e.g. `glm-5.2`)".into());
        }
        let vendor = self.vendor.trim().to_string();
        if vendor.is_empty() {
            return Err(format!("vendor is required for `{}`", model_id));
        }
        let source = self.source.trim().to_string();
        if source.is_empty() {
            return Err(format!(
                "a source citation is required for `{}` — an uncited context \
                 window is a guess, and the client would act on it",
                model_id
            ));
        }
        if self.context_window <= 0 {
            return Err(format!(
                "context window for `{}` must be a positive number of tokens \
                 (got {})",
                model_id, self.context_window
            ));
        }
        // `max_output == 0` is VALID and means UNSTATED — the vendor's docs
        // publish no per-model figure (the shipped qwen Token-Plan rows carry
        // 0). Inventing a positive figure the citation does not state is
        // exactly what the R10 citation rule forbids, so 0 is the only honest
        // marker.
        //
        // CROSS-LANGUAGE CONTRACT — one rule, FOUR homes. MUST MATCH all of:
        //   1. SQL  — `CHECK (max_output >= 0)` in
        //      `launcher/src-tauri/vct-launcher-core/src/db/migrations/
        //      046_chat_model_context_max_output_unstated.sql` (the backstop;
        //      nothing at all can write a negative, not even a hand `UPDATE`).
        //   2. Rust — HERE (`ChatModelContextInput::validated`): the gate
        //      every launcher write passes through.
        //   3. TS   — `parseMaxOutputTokens` in
        //      `launcher/src/lib/api/chat_model_context.ts`: blank or `0`
        //      means UNSTATED, so the pane never makes the user invent a
        //      figure, and a stored 0 renders as blank rather than as "0".
        //   4. Python — `model_router/catalog.py::_positive` in the gateway's
        //      reader: folds 0 to None, so an unstated row never publishes a
        //      token count downstream.
        // The rule in all four: `0` = UNSTATED (valid), `> 0` = the cited
        // vendor figure, `< 0` = impossible. Change the rule in one home and
        // it MUST change in the other three — this is a C-tier mirror (there
        // is no shared runtime between SQLite, Rust, TS and Python here), so
        // the naming is what keeps the four honest. v0.2.96 D-3: pre-fix only
        // the TS and SQL homes named the others.
        if self.max_output < 0 {
            return Err(format!(
                "max output for `{}` cannot be negative (got {}) — use 0 \
                 when the vendor does not publish a figure",
                model_id, self.max_output
            ));
        }
        Ok(Self {
            model_id,
            vendor,
            context_window: self.context_window,
            max_output: self.max_output,
            window_1m: self.window_1m,
            text_only: self.text_only,
            source,
            source_note: self.source_note.trim().to_string(),
        })
    }

    /// Whether an existing row already carries exactly these values (ignoring
    /// `user_edited` / `updated_at`). Lets reseed skip a no-op UPDATE, which
    /// keeps `updated_at` honest: it means "when this row last CHANGED", not
    /// "when reseed last ran".
    fn matches(&self, row: &ChatModelContextRow) -> bool {
        self.vendor == row.vendor
            && self.context_window == row.context_window
            && self.max_output == row.max_output
            && self.window_1m == row.window_1m
            && self.text_only == row.text_only
            && self.source == row.source
            && self.source_note == row.source_note
    }
}

/// What a reseed actually did. Four counters rather than a bool because the
/// pane reports it to the user, and "3 updated, 2 of your edits preserved" is
/// the sentence that makes the `user_edited` guard visible instead of
/// folklore.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReseedOutcome {
    /// Shipped rows that were absent (a new vendor model, or a row the user
    /// had deleted — "Reseed from shipped defaults" restores both).
    pub inserted: usize,
    /// Rows with `user_edited = 0` whose values differed and were refreshed.
    pub updated: usize,
    /// Rows with `user_edited = 0` that already matched: not written at all.
    pub unchanged: usize,
    /// Rows with `user_edited = 1`: LEFT ALONE, byte-identical.
    pub preserved_user_edits: usize,
    /// Machine-seeded rows (`user_edited = 0`) the converged seed no longer
    /// ships, retired by the converge (v0.2.101, NB-02). The instance that
    /// motivated the counter: `glm-5.2`, owner-retired in v0.2.100 — the
    /// seed stopped listing it, but the seed converge only ever merged, so
    /// every existing install kept a dead row the model-context pane and the
    /// exported JSON still listed while the gateway refused the id.
    pub retired: usize,
    /// Per-row record of every row this pass WROTE — `(model_id, action)`
    /// with action `"inserted"` / `"updated"` (v0.2.101, Q6/G1: the
    /// model-picker provenance log). Unwritten rows are NOT listed: a
    /// steady-state boot converges an unchanged table and records nothing,
    /// so the log lines the commands layer derives from this stay quiet
    /// until a row actually appears or changes — which is exactly the
    /// moment the next duplicate picker row needs to be traceable from.
    pub written: Vec<(String, String)>,
}

// ─── Timestamps ───────────────────────────────────────────────────────────

/// `updated_at` / `generated_at` format: RFC 3339 in UTC, second precision,
/// `Z` suffix (`2026-09-02T18:04:11Z`).
///
/// TEXT rather than the epoch-millis convention the rest of `launcher.db`
/// uses, because this value is rendered verbatim in the pane and compared by
/// eye against the export's `generated_at` — which is ISO-8601 by the
/// gateway's file contract, not ours to choose.
pub fn now_iso8601_utc() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

// ─── CRUD ─────────────────────────────────────────────────────────────────

fn row_from_sql(r: &rusqlite::Row<'_>) -> rusqlite::Result<ChatModelContextRow> {
    Ok(ChatModelContextRow {
        model_id: r.get(0)?,
        vendor: r.get(1)?,
        context_window: r.get(2)?,
        max_output: r.get(3)?,
        window_1m: r.get::<_, i64>(4)? != 0,
        source: r.get(5)?,
        source_note: r.get(6)?,
        user_edited: r.get::<_, i64>(7)? != 0,
        updated_at: r.get(8)?,
        text_only: r.get::<_, i64>(9)? != 0,
    })
}

const SELECT_COLUMNS: &str = "model_id, vendor, context_window, max_output, \
                              window_1m, source, source_note, user_edited, \
                              updated_at, text_only";

impl Db {
    /// Every row, ordered by `model_id`. That order is the export's order
    /// too, so the exported file is stable across runs.
    pub fn list_chat_model_context(&self) -> Result<Vec<ChatModelContextRow>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(&format!(
                "SELECT {SELECT_COLUMNS} FROM chat_model_context ORDER BY model_id ASC"
            ))
            .map_err(|e| format!("prepare list_chat_model_context: {}", e))?;
        let rows = stmt
            .query_map([], row_from_sql)
            .map_err(|e| format!("query list_chat_model_context: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list_chat_model_context: {}", e))
    }

    /// One row by exact id. EXACT — never a prefix or family match.
    pub fn get_chat_model_context(
        &self,
        model_id: &str,
    ) -> Result<Option<ChatModelContextRow>, String> {
        let guard = self.lock();
        guard
            .query_row(
                &format!("SELECT {SELECT_COLUMNS} FROM chat_model_context WHERE model_id = ?1"),
                params![model_id],
                row_from_sql,
            )
            .optional()
            .map_err(|e| format!("get_chat_model_context: {}", e))
    }

    /// Insert or replace one row.
    ///
    /// `user_edited` is the CALLER's statement about which path this write
    /// came from: `true` from the GUI, `false` from the seeder/reseeder. It
    /// is not inferred, because inference would be wrong in exactly the case
    /// that matters — a user re-typing the shipped value is still a user
    /// edit, and reseed must not silently take that row back.
    pub fn upsert_chat_model_context(
        &self,
        input: ChatModelContextInput,
        user_edited: bool,
    ) -> Result<ChatModelContextRow, String> {
        let input = input.validated()?;
        let now = now_iso8601_utc();
        let row = ChatModelContextRow {
            model_id: input.model_id,
            vendor: input.vendor,
            context_window: input.context_window,
            max_output: input.max_output,
            window_1m: input.window_1m,
            text_only: input.text_only,
            source: input.source,
            source_note: input.source_note,
            user_edited,
            updated_at: now,
        };
        {
            let mut guard = self.lock();
            let tx = guard
                .transaction()
                .map_err(|e| format!("upsert_chat_model_context begin: {}", e))?;
            // Review R2-9: a row the user deletes and then re-adds by hand
            // must not keep its tombstone — the next boot's seed would read
            // it as "the user does not want this id" while the pane shows the
            // row they just typed. One transaction with the write, for the
            // same reason as the delete above.
            tx.execute(
                "DELETE FROM chat_model_context_tombstone WHERE model_id = ?1",
                params![row.model_id],
            )
            .map_err(|e| format!("upsert_chat_model_context tombstone: {}", e))?;
            tx
                .execute(
                    "INSERT INTO chat_model_context
                        (model_id, vendor, context_window, max_output, window_1m,
                         source, source_note, user_edited, updated_at, text_only)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
                     ON CONFLICT(model_id) DO UPDATE SET
                        vendor         = excluded.vendor,
                        context_window = excluded.context_window,
                        max_output     = excluded.max_output,
                        window_1m      = excluded.window_1m,
                        text_only      = excluded.text_only,
                        source         = excluded.source,
                        source_note    = excluded.source_note,
                        user_edited    = excluded.user_edited,
                        updated_at     = excluded.updated_at",
                    params![
                        row.model_id,
                        row.vendor,
                        row.context_window,
                        row.max_output,
                        i64::from(row.window_1m),
                        row.source,
                        row.source_note,
                        i64::from(row.user_edited),
                        row.updated_at,
                        i64::from(row.text_only),
                    ],
                )
                .map_err(|e| format!("upsert_chat_model_context: {}", e))?;
            tx.commit()
                .map_err(|e| format!("upsert_chat_model_context commit: {}", e))?;
        }
        Ok(row)
    }

    /// Delete one row AND remember that it was deleted (migration 045).
    ///
    /// `Ok(false)` when nothing matched — an absent row is not an error, and
    /// reporting it as one would make a double-click on the pane's delete
    /// button look like a failure.
    ///
    /// The tombstone is what keeps the boot seed from reinstating this row on
    /// the next launch, now that the seed is per-row rather than
    /// first-boot-only. It is written even when no row matched: a user who
    /// deletes an id that is not there yet has still said "I do not want
    /// this one", and the shipped seed may add it tomorrow.
    pub fn delete_chat_model_context(&self, model_id: &str) -> Result<bool, String> {
        let now = now_iso8601_utc();
        let mut guard = self.lock();
        // ONE transaction (review R2-4). Split, a failed tombstone INSERT
        // leaves the row deleted with nothing remembering it — which is
        // precisely the state the next boot's seed would silently undo, and
        // the caller would have been told the delete failed.
        let tx = guard
            .transaction()
            .map_err(|e| format!("delete_chat_model_context begin: {}", e))?;
        let affected = tx
            .execute(
                "DELETE FROM chat_model_context WHERE model_id = ?1",
                params![model_id],
            )
            .map_err(|e| format!("delete_chat_model_context: {}", e))?;
        tx.execute(
            "INSERT INTO chat_model_context_tombstone (model_id, deleted_at)
             VALUES (?1, ?2)
             ON CONFLICT(model_id) DO UPDATE SET deleted_at = excluded.deleted_at",
            params![model_id, now],
        )
        .map_err(|e| format!("delete_chat_model_context tombstone: {}", e))?;
        tx.commit()
            .map_err(|e| format!("delete_chat_model_context commit: {}", e))?;
        Ok(affected > 0)
    }

    /// Every id the user has deleted, for tests and for the pane.
    pub fn list_chat_model_context_tombstones(&self) -> Result<Vec<String>, String> {
        let guard = self.lock();
        let mut stmt = guard
            .prepare(
                "SELECT model_id FROM chat_model_context_tombstone ORDER BY model_id ASC",
            )
            .map_err(|e| format!("prepare list tombstones: {}", e))?;
        let rows = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(|e| format!("query list tombstones: {}", e))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("collect list tombstones: {}", e))
    }

    /// Boot converge: bring the table in step with the shipped seed, row by
    /// row — NEVER over a user edit and NEVER undoing a delete.
    ///
    /// Per row of the shipped seed (the same per-row rule
    /// `reseed_chat_model_context` applies, minus the one thing only a
    /// deliberate click may do):
    ///
    ///   * absent and not tombstoned → INSERT. Per-row absence, not
    ///     emptiness: the emptiness gate this path replaced
    ///     (`seed_chat_model_context_if_empty`, v0.2.92) meant an UPGRADED
    ///     install never saw a newly shipped model — 0.2.93 added four Claude
    ///     5 rows and every table that already held the ten GLM rows stayed
    ///     at ten. A first-boot-only seed is a seed that only ever works on
    ///     machines that did not need it.
    ///   * present, `user_edited = 0`, values differ → UPDATE to the shipped
    ///     values. The row itself carries the answer the old "this path
    ///     cannot tell" argument said was missing: `user_edited` is 0
    ///     exactly when no human hand has written the row since it was
    ///     seeded, so refreshing it cannot clobber anyone's edit. This is
    ///     the case an insert-if-absent-only seed could never reach — the
    ///     v0.2.98 qwen correction (200K → the vendor's per-model 1M) landed
    ///     in the seed and reached no existing table.
    ///   * present, `user_edited = 0`, values identical → written NOT AT
    ///     ALL, so `updated_at` keeps meaning "when this row last changed",
    ///     not "when the launcher last booted".
    ///   * present, `user_edited = 1` → LEFT ALONE ENTIRELY, every column
    ///     byte-identical.
    ///   * tombstoned id (migration 045) → SKIPPED, even when absent: an
    ///     automatic path must never undo a human's delete. Only the
    ///     explicit "Reseed from shipped defaults" clears tombstones, and a
    ///     deliberate click may.
    ///
    /// Rows in the table that the shipped seed does not mention are retired
    /// when — and only when — no human hand wrote them (`user_edited = 0`):
    /// converge adds, refreshes and retires stale seeded rows, but never
    /// prunes a user's own (see [`retire_rows_absent_from_seed`] for the
    /// exact predicate).
    pub fn converge_chat_model_context_seed(
        &self,
        rows: &[ChatModelContextInput],
    ) -> Result<ReseedOutcome, String> {
        // Validate the WHOLE batch before writing any of it: a shipped seed
        // with one bad row is a broken build, and a half-seeded table would
        // hide that behind a table that looks populated.
        let validated: Vec<ChatModelContextInput> = rows
            .iter()
            .cloned()
            .map(|r| r.validated())
            .collect::<Result<_, _>>()?;

        let now = now_iso8601_utc();
        let mut guard = self.lock();
        let tx = guard
            .transaction()
            .map_err(|e| format!("converge_chat_model_context_seed begin: {}", e))?;
        let outcome = converge_rows(&tx, &validated, &now, false)?;
        tx.commit()
            .map_err(|e| format!("converge_chat_model_context_seed commit: {}", e))?;
        Ok(outcome)
    }

    /// Re-apply the shipped rows, NEVER over a user edit.
    ///
    /// The per-row rule is [`converge_chat_model_context_seed`]'s — the two
    /// share one implementation (`converge_rows`) — plus exactly one power
    /// the automatic boot path must never have: it CLEARS the tombstone of
    /// every id it re-applies (migration 045). "Restore the shipped
    /// defaults" is a deliberate click that means exactly that, and leaving
    /// a tombstone behind would let the next boot's converge disagree with
    /// the row this click just restored.
    pub fn reseed_chat_model_context(
        &self,
        rows: &[ChatModelContextInput],
    ) -> Result<ReseedOutcome, String> {
        let validated: Vec<ChatModelContextInput> = rows
            .iter()
            .cloned()
            .map(|r| r.validated())
            .collect::<Result<_, _>>()?;

        let now = now_iso8601_utc();
        let mut guard = self.lock();
        let tx = guard
            .transaction()
            .map_err(|e| format!("reseed_chat_model_context begin: {}", e))?;
        let outcome = converge_rows(&tx, &validated, &now, true)?;
        tx.commit()
            .map_err(|e| format!("reseed_chat_model_context commit: {}", e))?;
        Ok(outcome)
    }
}

/// The ONE per-row rule both the boot converge and the explicit reseed apply
/// (one concern, one home — the boot path was taught this rule in v0.2.98,
/// and the rule lives here rather than twice).
///
/// Per row: absent → insert; present with `user_edited = 0` and different
/// values → update; present with `user_edited = 0` and identical values →
/// written not at all (so `updated_at` keeps meaning "when this row last
/// changed"); present with `user_edited = 1` → LEFT ALONE ENTIRELY.
///
/// After the per-row merge, rows the seed does NOT mention are retired when
/// they are machine-written — [`retire_rows_absent_from_seed`], shared by
/// both callers for the same reason this whole function is.
///
/// `restore_deleted` is the ONLY difference between the two callers: when it
/// is `true` (the reseed button — a deliberate click) an absent row is
/// inserted and its delete tombstone cleared; when it is `false` (the
/// automatic boot path) a tombstoned id is SKIPPED entirely, even when
/// absent — an automatic path must never undo a human's delete.
fn converge_rows(
    tx: &rusqlite::Transaction<'_>,
    rows: &[ChatModelContextInput],
    now: &str,
    restore_deleted: bool,
) -> Result<ReseedOutcome, String> {
    let mut outcome = ReseedOutcome::default();
    for row in rows {
        if !restore_deleted {
            let tombstoned = tx
                .query_row(
                    "SELECT 1 FROM chat_model_context_tombstone WHERE model_id = ?1",
                    params![row.model_id],
                    |_| Ok(()),
                )
                .optional()
                .map_err(|e| format!("converge tombstone check {}: {}", row.model_id, e))?
                .is_some();
            if tombstoned {
                // Counted nowhere on purpose: the outcome's four counters
                // describe writes and preservations, and "the user deleted
                // this id" is neither.
                continue;
            }
        }
        let existing: Option<ChatModelContextRow> = tx
            .query_row(
                &format!("SELECT {SELECT_COLUMNS} FROM chat_model_context WHERE model_id = ?1"),
                params![row.model_id],
                row_from_sql,
            )
            .optional()
            .map_err(|e| format!("converge lookup {}: {}", row.model_id, e))?;

        match existing {
            Some(current) if current.user_edited => {
                outcome.preserved_user_edits += 1;
            }
            Some(current) if row.matches(&current) => {
                outcome.unchanged += 1;
            }
            Some(_) => {
                tx.execute(
                    "UPDATE chat_model_context SET
                        vendor         = ?2,
                        context_window = ?3,
                        max_output     = ?4,
                        window_1m      = ?5,
                        source         = ?6,
                        source_note    = ?7,
                        user_edited    = 0,
                        updated_at     = ?8,
                        text_only      = ?9
                     WHERE model_id = ?1",
                    params![
                        row.model_id,
                        row.vendor,
                        row.context_window,
                        row.max_output,
                        i64::from(row.window_1m),
                        row.source,
                        row.source_note,
                        now,
                        i64::from(row.text_only),
                    ],
                )
                .map_err(|e| format!("converge update {}: {}", row.model_id, e))?;
                outcome.updated += 1;
                outcome
                    .written
                    .push((row.model_id.clone(), "updated".to_string()));
            }
            None => {
                if restore_deleted {
                    tx.execute(
                        "DELETE FROM chat_model_context_tombstone WHERE model_id = ?1",
                        params![row.model_id],
                    )
                    .map_err(|e| format!("converge clear tombstone {}: {}", row.model_id, e))?;
                }
                tx.execute(
                    "INSERT INTO chat_model_context
                        (model_id, vendor, context_window, max_output, window_1m,
                         source, source_note, user_edited, updated_at, text_only)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 0, ?8, ?9)",
                    params![
                        row.model_id,
                        row.vendor,
                        row.context_window,
                        row.max_output,
                        i64::from(row.window_1m),
                        row.source,
                        row.source_note,
                        now,
                        i64::from(row.text_only),
                    ],
                )
                .map_err(|e| format!("converge insert {}: {}", row.model_id, e))?;
                outcome.inserted += 1;
                outcome
                    .written
                    .push((row.model_id.clone(), "inserted".to_string()));
            }
        }
    }
    retire_rows_absent_from_seed(tx, rows, &mut outcome)?;
    Ok(outcome)
}

/// Retire rows the converged seed no longer ships (v0.2.101, NB-02).
///
/// PREDICATE — a row is RETIRED exactly when BOTH hold:
///
///   * `user_edited = 0`. A human edit is never deleted by any automatic
///     path, per row, ever — the same guard the merge half applies. The only
///     writer of a `user_edited = 0` row is a previous seed converge, so
///     this arm names rows that are machine-written and nothing else.
///   * the id is absent from the seed being converged — the one shipped
///     catalog surface this layer holds. The gateway's other catalog
///     surfaces (`model_router/vendors.py`: `static_ids`, `verified_ids`,
///     `retired_ids`) are Python-side data with no Rust reader, and this
///     module deliberately does no file I/O, so a seed-absent machine row is
///     the strongest "appears in no shipped catalog surface" test available
///     here — and it is exactly right: such a row was written by an OLDER
///     seed and dropped from the shipped set, which is a model the vendor
///     no longer serves.
///
/// There is deliberately NO second hardcoded tombstone list here. The
/// gateway's tombstone set for retired ids lives in
/// `claude_mcp_servers/model_router/vendors.py` (`retired_ids`, today
/// `("glm-5.2",)` on both routes); duplicating those ids in Rust would be a
/// (C)-tier mirror without the parity test that would keep it honest — the
/// seed-absent predicate reaches the same rows from data this layer already
/// holds. When a model is retired from the shipped seed, this pass retires
/// it from every existing table on the next boot; if a future release
/// re-ships an id, the merge half re-inserts it (a retire writes NO
/// `chat_model_context_tombstone` row — that table is the user's delete
/// marker and must stay exactly that).
///
/// One `tracing::info!` line per retired row. The retire COUNT also
/// surfaces elsewhere — the boot summary line
/// (`commands::chat_model_context::converge_summary_line`) and the reseed
/// toast after "Reseed from shipped defaults" both carry it — but neither
/// names the retired IDS; this per-row line is where a user learns which
/// row went, which no pane shows (a removed row simply disappears).
fn retire_rows_absent_from_seed(
    tx: &rusqlite::Transaction<'_>,
    seed: &[ChatModelContextInput],
    outcome: &mut ReseedOutcome,
) -> Result<(), String> {
    let mut stmt = tx
        .prepare("SELECT model_id FROM chat_model_context WHERE user_edited = 0")
        .map_err(|e| format!("retire scan prepare: {}", e))?;
    let machine_rows: Vec<String> = stmt
        .query_map([], |r| r.get::<_, String>(0))
        .map_err(|e| format!("retire scan: {}", e))?
        .collect::<Result<_, _>>()
        .map_err(|e| format!("retire scan collect: {}", e))?;
    drop(stmt);
    for model_id in machine_rows {
        if seed.iter().any(|row| row.model_id == model_id) {
            continue;
        }
        // The `user_edited = 0` re-check is defensive: the scan and the
        // delete share one transaction, but the guard costs nothing and a
        // future edit that moves this off the scanned set must not be able
        // to turn it into a delete of a user's row.
        let deleted = tx
            .execute(
                "DELETE FROM chat_model_context WHERE model_id = ?1 AND user_edited = 0",
                params![model_id],
            )
            .map_err(|e| format!("retire delete {}: {}", model_id, e))?;
        if deleted > 0 {
            outcome.retired += 1;
            tracing::info!(
                "[vct] chat-model context: retired `{}` — machine-seeded and \
                 absent from the shipped seed, so the gateway refuses it on \
                 every route. Re-add it in the model-context pane if you \
                 still want it listed (a hand-added row is never retired).",
                model_id
            );
        }
    }
    Ok(())
}

// ─── Export document ──────────────────────────────────────────────────────

/// Build the document the gateway reads.
///
/// The shape is NOT ours to choose — it is the contract stated in
/// `claude_mcp_servers/model_router/context_table.py`'s module docstring and
/// parsed by its `_parse`:
///
/// ```json
/// {"schema_version": 1,
///  "generated_at": "<ISO-8601 UTC>",
///  "source": "launcher.db",
///  "models": {"<full-model-id>": {"vendor": "...", "context_window": 0,
///                                 "max_output": 0, "window_1m": false,
///                                 "text_only": false,
///                                 "source": "...", "source_note": "..."}}}
/// ```
///
/// `source_note` is omitted when empty, matching the shipped seed's own
/// shape; the reader coalesces an absent note to `""`.
///
/// KEY ORDER is meaningful and is why this builds an ordered document
/// deliberately: every launcher crate enables `serde_json`'s `preserve_order`
/// (v0.2.92 WP-19), so `Value::Object` is an `IndexMap` and serialisation
/// follows INSERTION order rather than alphabetical order. Models are
/// inserted in `model_id` order so a re-export with no data change produces
/// byte-identical output — which is what lets the gateway's `(mtime_ns, size)`
/// change-detector stay quiet and what makes a diff of this file readable.
pub fn export_document(
    rows: &[ChatModelContextRow],
    tombstones: &[String],
    generated_at: &str,
) -> serde_json::Value {
    let mut models = serde_json::Map::new();
    let mut ordered: Vec<&ChatModelContextRow> = rows.iter().collect();
    ordered.sort_by(|a, b| a.model_id.cmp(&b.model_id));
    for row in ordered {
        let mut entry = serde_json::Map::new();
        entry.insert("vendor".into(), serde_json::Value::from(row.vendor.clone()));
        entry.insert(
            "context_window".into(),
            serde_json::Value::from(row.context_window),
        );
        entry.insert("max_output".into(), serde_json::Value::from(row.max_output));
        entry.insert("window_1m".into(), serde_json::Value::from(row.window_1m));
        // The image-capability flag always travels (never omitted, even for
        // false): the gateway's reader defaults an ABSENT key to false too,
        // but an explicit value is what makes this export a complete answer
        // to "can this model see images?" — same treatment as `window_1m`.
        entry.insert("text_only".into(), serde_json::Value::from(row.text_only));
        entry.insert("source".into(), serde_json::Value::from(row.source.clone()));
        if !row.source_note.is_empty() {
            entry.insert(
                "source_note".into(),
                serde_json::Value::from(row.source_note.clone()),
            );
        }
        models.insert(row.model_id.clone(), serde_json::Value::Object(entry));
    }

    // CROSS-LANE CONTRACT (v0.2.94): `tombstones` is the sorted list of model
    // ids the user has DELETED. The gateway reads it so its own shipped-seed
    // fallback never advertises a `[1m]` companion for a row this machine
    // removed — without it, "deleted" holds in launcher.db and in the export's
    // `models` map while the seed inside the gateway's wheel still knows the
    // id. Always present, possibly empty: an absent key and an empty list must
    // not mean different things to the reader.
    let mut deleted: Vec<&String> = tombstones.iter().collect();
    deleted.sort();
    deleted.dedup();

    let mut doc = serde_json::Map::new();
    doc.insert(
        "schema_version".into(),
        serde_json::Value::from(EXPORT_SCHEMA_VERSION),
    );
    doc.insert(
        "generated_at".into(),
        serde_json::Value::from(generated_at.to_string()),
    );
    doc.insert(
        "source".into(),
        serde_json::Value::from(EXPORT_SOURCE_LAUNCHER_DB),
    );
    doc.insert("models".into(), serde_json::Value::Object(models));
    doc.insert(
        "tombstones".into(),
        serde_json::Value::Array(
            deleted
                .into_iter()
                .map(|id| serde_json::Value::from(id.clone()))
                .collect(),
        ),
    );
    serde_json::Value::Object(doc)
}

/// Parse a document in the export shape back into rows.
///
/// The inverse of [`export_document`], and the loader for the shipped seed
/// (`claude_mcp_servers/model_router/chat_model_context.seed.json` — the same
/// file the gateway carries as its own fallback, read here rather than copied
/// so the shipped data has exactly one home).
///
/// STRICTER than the gateway's reader, deliberately. The reader is tolerant —
/// it drops an uncited or malformed row, warns, and serves the rest — because
/// it may be handed a file a user edited by hand and must never crash the
/// daemon over it. This parser is only ever pointed at a file that ships WITH
/// the code, so a bad row there is a broken build, and the useful behaviour
/// is to say so loudly instead of silently seeding a table with a hole in it.
///
/// Top-level keys beginning with `_` (the seed's `_comment`) and model ids
/// beginning with `_` are skipped, matching the reader.
pub fn parse_document(payload: &serde_json::Value) -> Result<Vec<ChatModelContextInput>, String> {
    let obj = payload
        .as_object()
        .ok_or_else(|| "top level is not a JSON object".to_string())?;

    let version = obj
        .get("schema_version")
        .and_then(|v| v.as_u64())
        .ok_or_else(|| "`schema_version` is missing or not a number".to_string())?;
    if version != EXPORT_SCHEMA_VERSION {
        return Err(format!(
            "schema_version {version} is not supported (this launcher understands \
             {EXPORT_SCHEMA_VERSION})"
        ));
    }

    let models = obj
        .get("models")
        .and_then(|v| v.as_object())
        .ok_or_else(|| "`models` is missing or not an object".to_string())?;

    let mut out = Vec::with_capacity(models.len());
    for (model_id, raw) in models {
        if model_id.starts_with('_') {
            continue;
        }
        let entry = raw
            .as_object()
            .ok_or_else(|| format!("model `{model_id}` is not an object"))?;
        let num = |key: &str| -> Result<i64, String> {
            entry
                .get(key)
                .and_then(|v| v.as_i64())
                .ok_or_else(|| format!("model `{model_id}` has no numeric `{key}`"))
        };
        let text = |key: &str| -> String {
            entry
                .get(key)
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string()
        };
        let input = ChatModelContextInput {
            model_id: model_id.clone(),
            vendor: text("vendor"),
            context_window: num("context_window")?,
            max_output: num("max_output")?,
            window_1m: entry
                .get("window_1m")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            // The image-capability flag (v0.2.101). Absent → false, the SAME
            // direction the gateway's reader defaults a missing key
            // (context_table.py: `bool(raw.get("text_only"))`): the shipped
            // seed states it only on the rows the vendor page decides, and
            // an unflagged model keeps its image blocks untouched.
            text_only: entry
                .get("text_only")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            source: text("source"),
            source_note: text("source_note"),
        };
        out.push(input.validated()?);
    }
    out.sort_by(|a, b| a.model_id.cmp(&b.model_id));
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;
    use std::sync::Mutex;

    /// In-memory DB. NEVER touches `~/.vct/launcher.db`: no `Db::open()`, no
    /// `VCT_STATE_DIR`, no filesystem at all.
    fn make_db() -> Db {
        let conn = Connection::open_in_memory().unwrap();
        conn.pragma_update(None, "foreign_keys", "ON").unwrap();
        crate::db::migrations::apply(&conn).unwrap();
        Db(Mutex::new(conn))
    }

    fn input(model_id: &str) -> ChatModelContextInput {
        ChatModelContextInput {
            model_id: model_id.to_string(),
            vendor: "zai".into(),
            context_window: 200_000,
            max_output: 128_000,
            window_1m: false,
            text_only: false,
            source: "https://docs.z.ai/guides/llm/glm-5.1".into(),
            source_note: String::new(),
        }
    }

    fn one_m(model_id: &str) -> ChatModelContextInput {
        ChatModelContextInput {
            context_window: 1_000_000,
            window_1m: true,
            source: "https://docs.z.ai/guides/llm/glm-5.2".into(),
            ..input(model_id)
        }
    }

    // ── validation ───────────────────────────────────────────────────────

    #[test]
    fn an_uncited_row_is_refused_and_the_message_says_why() {
        for blank in ["", "   ", "\n\t"] {
            let err = ChatModelContextInput {
                source: blank.into(),
                ..input("glm-5.1")
            }
            .validated()
            .expect_err("an uncited row must be refused");
            assert!(
                err.contains("source citation is required"),
                "message must name the rule, got: {}",
                err
            );
        }
        // ACT half — a cited row passes, so the rule discriminates.
        assert!(input("glm-5.1").validated().is_ok());
    }

    #[test]
    fn the_store_refuses_an_uncited_row_too_not_only_the_validator() {
        let db = make_db();
        let err = db
            .upsert_chat_model_context(
                ChatModelContextInput {
                    source: "  ".into(),
                    ..input("glm-5.1")
                },
                true,
            )
            .expect_err("uncited upsert must fail");
        assert!(err.contains("source citation is required"), "got: {}", err);
        assert!(
            db.list_chat_model_context().unwrap().is_empty(),
            "a refused upsert must write nothing"
        );
    }

    #[test]
    fn validation_refuses_impossible_windows_and_blank_identity() {
        let cases: Vec<(ChatModelContextInput, &str)> = vec![
            (
                ChatModelContextInput { model_id: "  ".into(), ..input("x") },
                "model id is required",
            ),
            (
                ChatModelContextInput { vendor: "".into(), ..input("x") },
                "vendor is required",
            ),
            (
                ChatModelContextInput { context_window: 0, ..input("x") },
                "context window",
            ),
            (
                ChatModelContextInput { context_window: -1, ..input("x") },
                "context window",
            ),
            (
                ChatModelContextInput { max_output: -1, ..input("x") },
                "max output",
            ),
        ];
        for (case, needle) in cases {
            let err = case.validated().expect_err("must be refused");
            assert!(err.contains(needle), "expected {:?} in {:?}", needle, err);
        }
    }

    #[test]
    fn validation_trims_rather_than_storing_padded_ids() {
        let v = ChatModelContextInput {
            model_id: "  glm-5.2  ".into(),
            source: "  https://docs.z.ai/guides/llm/glm-5.2 ".into(),
            source_note: "  noted  ".into(),
            ..input("ignored")
        }
        .validated()
        .unwrap();
        assert_eq!(v.model_id, "glm-5.2");
        assert_eq!(v.source, "https://docs.z.ai/guides/llm/glm-5.2");
        assert_eq!(v.source_note, "noted");
    }

    /// v0.2.96 cross-language contract: `max_output == 0` means UNSTATED and
    /// is VALID — the vendor's docs publish no per-model figure (the shipped
    /// qwen Token-Plan rows carry 0) and inventing one is forbidden. Negative
    /// stays invalid. Mirrors the Python mapping (`catalog::_positive` folds
    /// 0 to None so nothing downstream publishes it as a count).
    #[test]
    fn zero_max_output_validates_as_unstated_and_negative_does_not() {
        let unstated = ChatModelContextInput { max_output: 0, ..input("qwen3.8-max") }
            .validated()
            .expect("0 = unstated must validate");
        assert_eq!(
            unstated.max_output, 0,
            "the unstated marker survives validation verbatim — never \
             coerced to an invented figure"
        );

        let err = ChatModelContextInput { max_output: -1, ..input("qwen3.8-max") }
            .validated()
            .expect_err("a negative max_output must be refused");
        assert!(err.contains("cannot be negative"), "got: {}", err);

        // Leave-alone: a cited positive figure still validates.
        assert!(input("glm-5.2").validated().is_ok());
    }

    /// The unstated marker STORES: upsert and the boot seed both write a
    /// `max_output = 0` row through the SQL CHECK (migration 046 relaxed it
    /// from > 0 to >= 0 for exactly this), and it lists back verbatim — never
    /// NULL, never replaced by a guess.
    #[test]
    fn an_unstated_max_output_row_stores_and_lists_verbatim() {
        let db = make_db();
        let created = db
            .upsert_chat_model_context(
                ChatModelContextInput { max_output: 0, ..input("qwen3.8-max") },
                false,
            )
            .expect("the relaxed CHECK must accept the unstated marker");
        assert_eq!(created.max_output, 0);

        assert_eq!(
            db.converge_chat_model_context_seed(&[
                // The pre-upserted row travels IN the seed so this test stays
                // about the unstated marker, not the retire pass: a
                // machine-written seed-absent row would (correctly, v0.2.101)
                // be retired here and shrink the listing below.
                ChatModelContextInput { max_output: 0, ..input("qwen3.8-max") },
                ChatModelContextInput { max_output: 0, ..input("deepseek-v4-pro") },
            ])
            .unwrap()
            .inserted,
            1,
            "the boot seed writes unstated rows too"
        );

        let listed = db.list_chat_model_context().unwrap();
        assert_eq!(listed.len(), 2);
        assert!(
            listed.iter().all(|r| r.max_output == 0),
            "0 round-trips through the DB verbatim"
        );

        // And a negative is refused — with the stored row left untouched.
        let err = db
            .upsert_chat_model_context(
                ChatModelContextInput { max_output: -1, ..input("qwen3.8-max") },
                true,
            )
            .expect_err("a negative max_output must be refused");
        assert!(err.contains("cannot be negative"), "got: {}", err);
        assert_eq!(
            db.get_chat_model_context("qwen3.8-max").unwrap().unwrap().max_output,
            0,
            "a refused upsert leaves the stored unstated row untouched"
        );
    }

    // ── CRUD ─────────────────────────────────────────────────────────────

    #[test]
    fn upsert_inserts_then_updates_the_same_key_and_marks_the_edit() {
        let db = make_db();
        let created = db
            .upsert_chat_model_context(input("glm-5.1"), false)
            .unwrap();
        assert!(!created.user_edited);
        assert_eq!(created.context_window, 200_000);

        let edited = db
            .upsert_chat_model_context(
                ChatModelContextInput { context_window: 250_000, ..input("glm-5.1") },
                true,
            )
            .unwrap();
        assert!(edited.user_edited, "a GUI write marks the row user-edited");

        let all = db.list_chat_model_context().unwrap();
        assert_eq!(all.len(), 1, "upsert must not duplicate the primary key");
        assert_eq!(all[0].context_window, 250_000);
        assert!(all[0].user_edited);
    }

    #[test]
    fn lookup_is_exact_never_a_family_prefix() {
        let db = make_db();
        db.upsert_chat_model_context(one_m("glm-5.2"), false).unwrap();
        assert!(db.get_chat_model_context("glm-5.2").unwrap().is_some());
        // The 5x-lie cases: neither a longer id nor a shorter family stem
        // resolves to the 1M row.
        assert!(db.get_chat_model_context("glm-5.2-flash").unwrap().is_none());
        assert!(db.get_chat_model_context("glm-5").unwrap().is_none());
        assert!(db.get_chat_model_context("glm").unwrap().is_none());
    }

    #[test]
    fn list_is_ordered_by_model_id_so_the_export_is_stable() {
        let db = make_db();
        for id in ["glm-5.2", "glm-4.5", "glm-5.1"] {
            db.upsert_chat_model_context(input(id), false).unwrap();
        }
        let ids: Vec<String> = db
            .list_chat_model_context()
            .unwrap()
            .into_iter()
            .map(|r| r.model_id)
            .collect();
        assert_eq!(ids, vec!["glm-4.5", "glm-5.1", "glm-5.2"]);
    }

    #[test]
    fn delete_reports_whether_anything_matched() {
        let db = make_db();
        db.upsert_chat_model_context(input("glm-5.1"), true).unwrap();
        assert!(db.delete_chat_model_context("glm-5.1").unwrap());
        // Leave-alone: deleting again is not an error and changes nothing.
        assert!(!db.delete_chat_model_context("glm-5.1").unwrap());
        assert!(db.list_chat_model_context().unwrap().is_empty());
    }

    // ── seed ─────────────────────────────────────────────────────────────

    #[test]
    fn seed_populates_an_empty_table_once_and_is_a_no_op_after() {
        let db = make_db();
        let seed = vec![input("glm-5.1"), one_m("glm-5.2")];

        assert_eq!(db.converge_chat_model_context_seed(&seed).unwrap().inserted, 2);
        // Second boot: every shipped row is present and matches, so nothing
        // is written — not even a same-value UPDATE.
        let again = db.converge_chat_model_context_seed(&seed).unwrap();
        assert_eq!(
            (again.inserted, again.updated, again.unchanged),
            (0, 0, 2),
            "a converged table is a no-op on the next boot"
        );
        assert_eq!(db.list_chat_model_context().unwrap().len(), 2);
        assert!(
            db.list_chat_model_context()
                .unwrap()
                .iter()
                .all(|r| !r.user_edited),
            "seeded rows are not user edits"
        );
    }

    #[test]
    fn seed_inserts_newly_shipped_rows_into_an_upgraded_table() {
        // The 0.2.93 shape exactly: ten GLM rows already in the table, four
        // Claude rows added to the shipped seed. Under the old emptiness
        // gate this inserted NOTHING and the export stayed at ten — the
        // reason a whole release's context rows never reached any upgraded
        // install.
        let db = make_db();
        let old_seed: Vec<ChatModelContextInput> =
            (0..10).map(|i| input(&format!("glm-{}", i))).collect();
        assert_eq!(
            db.converge_chat_model_context_seed(&old_seed).unwrap().inserted,
            10
        );

        let mut new_seed = old_seed.clone();
        for id in ["claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-fable-5-1"] {
            new_seed.push(one_m(id));
        }
        let outcome = db.converge_chat_model_context_seed(&new_seed).unwrap();
        assert_eq!(
            (outcome.inserted, outcome.updated, outcome.unchanged),
            (4, 0, 10),
            "only the absent rows are inserted; the present ones already match"
        );
        assert_eq!(db.list_chat_model_context().unwrap().len(), 14);
        assert!(db.get_chat_model_context("claude-opus-5").unwrap().unwrap().window_1m);
    }

    #[test]
    fn converge_never_overwrites_a_user_edited_row() {
        // The guard, not the absence: since v0.2.98 the boot path refreshes
        // untouched rows, and what makes that safe is `user_edited` — the
        // row carries the answer the old "cannot tell" argument said was
        // missing. A USER-EDITED row is left entirely alone; refreshing an
        // untouched row is covered by its own test below.
        let db = make_db();
        let edited = db
            .upsert_chat_model_context(
                ChatModelContextInput {
                    context_window: 111_111,
                    source: "my own measurement".into(),
                    ..input("glm-5.1")
                },
                true,
            )
            .unwrap();

        let outcome = db
            .converge_chat_model_context_seed(&[one_m("glm-5.1"), input("glm-5.2")])
            .unwrap();
        assert_eq!(
            (outcome.inserted, outcome.preserved_user_edits),
            (1, 1),
            "the absent row arrives; the user-edited one is preserved"
        );
        assert_eq!(
            db.get_chat_model_context("glm-5.1").unwrap().unwrap(),
            edited,
            "every field of the existing row survives, updated_at included"
        );
    }

    #[test]
    fn converge_refreshes_an_untouched_row_to_the_shipped_values() {
        // The v0.2.98 delivery defect, in miniature: the table already holds
        // the OLD figure an earlier boot seeded (200K), the shipped seed now
        // carries the vendor's per-model correction (1M), and the row has
        // never been user-edited. Insert-if-absent-only could never reach
        // this row; converge must.
        let db = make_db();
        db.upsert_chat_model_context(input("glm-5.1"), false).unwrap();

        let outcome = db.converge_chat_model_context_seed(&[one_m("glm-5.1")]).unwrap();
        assert_eq!(
            outcome,
            ReseedOutcome { inserted: 0, updated: 1, unchanged: 0, preserved_user_edits: 0, retired: 0,
                written: vec![("glm-5.1".into(), "updated".into())] },
            "an untouched row with stale values is REFRESHED, not skipped"
        );
        let row = db.get_chat_model_context("glm-5.1").unwrap().unwrap();
        assert_eq!(row.context_window, 1_000_000);
        assert!(row.window_1m);
        assert!(!row.user_edited, "a converged row is still not a user edit");
    }

    #[test]
    fn converge_leaves_a_user_edited_row_byte_identical() {
        // The leave-alone half of the same rule: `user_edited = 1` means a
        // human hand wrote this row, and no automatic path may touch it.
        let db = make_db();
        let edited = db
            .upsert_chat_model_context(
                ChatModelContextInput {
                    context_window: 111_111,
                    max_output: 999,
                    source: "my own measurement".into(),
                    source_note: "measured locally".into(),
                    ..input("glm-5.1")
                },
                true,
            )
            .unwrap();

        let outcome = db.converge_chat_model_context_seed(&[one_m("glm-5.1")]).unwrap();
        assert_eq!(
            outcome,
            ReseedOutcome { inserted: 0, updated: 0, unchanged: 0, preserved_user_edits: 1, retired: 0,
                written: Vec::new() }
        );

        // BYTE-IDENTICAL: context_window, max_output, window_1m, source,
        // source_note AND updated_at — struct equality covers every column.
        let after = db.get_chat_model_context("glm-5.1").unwrap().unwrap();
        assert_eq!(
            after, edited,
            "every column of a user-edited row survives converge unchanged, \
             updated_at included"
        );
        assert_eq!(after.context_window, 111_111);
        assert_eq!(after.max_output, 999);
        assert!(!after.window_1m);
        assert_eq!(after.source, "my own measurement");
        assert_eq!(after.source_note, "measured locally");
    }

    #[test]
    fn converge_does_not_rewrite_a_row_that_already_matches() {
        // `updated_at` must mean "when this row last changed", not "when the
        // launcher last booted" — the same rule the reseed path documents.
        let db = make_db();
        db.converge_chat_model_context_seed(&[input("glm-5.1")]).unwrap();
        let first = db.get_chat_model_context("glm-5.1").unwrap().unwrap();

        // The timestamps are second-precision: cross a second boundary so a
        // rewrite could not hide behind truncation.
        std::thread::sleep(std::time::Duration::from_secs(1));

        let outcome = db.converge_chat_model_context_seed(&[input("glm-5.1")]).unwrap();
        assert_eq!(
            outcome,
            ReseedOutcome { inserted: 0, updated: 0, unchanged: 1, preserved_user_edits: 0, retired: 0,
                written: Vec::new() },
            "an identical row is counted, not written"
        );
        assert_eq!(
            db.get_chat_model_context("glm-5.1").unwrap().unwrap(),
            first,
            "updated_at is untouched — nothing was written for this row"
        );
    }

    #[test]
    fn converge_never_reinserts_a_tombstoned_id_even_when_absent() {
        // A tombstone for an id that is not in the table either: the user
        // deleted a model the shipped seed did not carry YET. When the seed
        // gains it, the automatic path still may not add it back.
        let db = make_db();
        db.delete_chat_model_context("glm-5.9").unwrap();
        assert_eq!(
            db.list_chat_model_context_tombstones().unwrap(),
            vec!["glm-5.9"]
        );

        let outcome = db
            .converge_chat_model_context_seed(&[one_m("glm-5.9"), input("glm-5.2")])
            .unwrap();
        assert_eq!(
            (outcome.inserted, outcome.updated, outcome.unchanged, outcome.preserved_user_edits),
            (1, 0, 0, 0),
            "only the non-tombstoned absent row is written; the tombstoned \
             id lands in NO counter"
        );
        assert!(
            db.get_chat_model_context("glm-5.9").unwrap().is_none(),
            "deleted means deleted, even for a newly shipped id"
        );
        assert!(db.get_chat_model_context("glm-5.2").unwrap().is_some());
    }

    #[test]
    fn a_deleted_shipped_row_stays_deleted_across_boots() {
        // Review R1-6. Per-row seeding without a tombstone would resurrect
        // this row on the next launch: an absent row is absent whether the
        // user deleted it or the vendor is new, and only the tombstone tells
        // those apart.
        let db = make_db();
        let seed = vec![input("glm-5.1"), one_m("glm-5.2")];
        db.converge_chat_model_context_seed(&seed).unwrap();
        db.delete_chat_model_context("glm-5.1").unwrap();
        assert_eq!(db.list_chat_model_context_tombstones().unwrap(), vec!["glm-5.1"]);

        for _boot in 0..3 {
            assert_eq!(db.converge_chat_model_context_seed(&seed).unwrap().inserted, 0);
        }
        let ids: Vec<String> = db
            .list_chat_model_context()
            .unwrap()
            .into_iter()
            .map(|r| r.model_id)
            .collect();
        assert_eq!(ids, vec!["glm-5.2"], "deleted means deleted");
    }

    #[test]
    fn an_explicit_reseed_restores_a_deleted_row_and_clears_its_tombstone() {
        // The one path allowed to undo a delete, because the user asked for
        // it by name — and it must leave no tombstone behind, or the next
        // boot's seed would disagree with the row it just restored.
        let db = make_db();
        let seed = vec![input("glm-5.1")];
        db.converge_chat_model_context_seed(&seed).unwrap();
        db.delete_chat_model_context("glm-5.1").unwrap();

        assert_eq!(db.reseed_chat_model_context(&seed).unwrap().inserted, 1);
        assert!(db.list_chat_model_context_tombstones().unwrap().is_empty());
        assert_eq!(db.converge_chat_model_context_seed(&seed).unwrap().inserted, 0);
        assert!(db.get_chat_model_context("glm-5.1").unwrap().is_some());
    }

    #[test]
    fn a_failed_tombstone_write_rolls_the_delete_back() {
        // Review R2-4: split across two statements, a failing tombstone
        // INSERT left the row deleted with nothing remembering it — the exact
        // state the next boot's seed silently undoes, reported to the caller
        // as a failure.
        let db = make_db();
        db.converge_chat_model_context_seed(&[input("glm-5.1")]).unwrap();
        // Make the tombstone INSERT fail: the CHECK refuses a blank id, and a
        // blank id is what an empty model_id delete would write.
        assert!(db.delete_chat_model_context("").is_err());

        // Now the real proof: a delete whose tombstone cannot be written must
        // not leave the table row gone. Drop the tombstone table to force the
        // INSERT to fail, then delete a real row.
        {
            let guard = db.lock();
            guard
                .execute("DROP TABLE chat_model_context_tombstone", [])
                .unwrap();
        }
        assert!(
            db.delete_chat_model_context("glm-5.1").is_err(),
            "a delete that cannot be remembered must report failure"
        );
        assert!(
            db.get_chat_model_context("glm-5.1").unwrap().is_some(),
            "and must leave the row in place — one transaction, both writes"
        );
    }

    #[test]
    fn re_adding_a_deleted_row_by_hand_clears_its_tombstone() {
        // Review R2-9: otherwise the pane shows the row the user just typed
        // while the next boot's seed still reads "they do not want this id".
        let db = make_db();
        db.converge_chat_model_context_seed(&[input("glm-5.1")]).unwrap();
        db.delete_chat_model_context("glm-5.1").unwrap();
        assert_eq!(db.list_chat_model_context_tombstones().unwrap(), vec!["glm-5.1"]);

        db.upsert_chat_model_context(input("glm-5.1"), true).unwrap();
        assert!(
            db.list_chat_model_context_tombstones().unwrap().is_empty(),
            "the id is wanted again — the memory of the delete goes with it"
        );
        // And the boot converge leaves the re-added row exactly as typed.
        assert_eq!(
            db.converge_chat_model_context_seed(&[one_m("glm-5.1")])
                .unwrap()
                .preserved_user_edits,
            1
        );
        assert!(db.get_chat_model_context("glm-5.1").unwrap().unwrap().user_edited);
    }

    #[test]
    fn a_tombstone_never_blocks_a_different_model() {
        // LEAVE-ALONE half: deleting one row must not make the table
        // un-seedable for the rest.
        let db = make_db();
        db.converge_chat_model_context_seed(&[input("glm-5.1")]).unwrap();
        db.delete_chat_model_context("glm-5.1").unwrap();
        assert_eq!(
            db.converge_chat_model_context_seed(&[input("glm-5.1"), one_m("claude-opus-5")])
                .unwrap()
                .inserted,
            1,
            "the new model arrives; the deleted one does not come back"
        );
        assert!(db.get_chat_model_context("claude-opus-5").unwrap().is_some());
        assert!(db.get_chat_model_context("glm-5.1").unwrap().is_none());
    }

    #[test]
    fn a_seed_batch_with_one_bad_row_writes_nothing() {
        let db = make_db();
        let seed = vec![
            input("glm-5.1"),
            ChatModelContextInput { source: "".into(), ..input("glm-5.2") },
        ];
        assert!(db.converge_chat_model_context_seed(&seed).is_err());
        assert!(
            db.list_chat_model_context().unwrap().is_empty(),
            "a half-seeded table would hide a broken build behind a populated look"
        );
    }

    // ── reseed: the user_edited guard, act + leave-alone ──────────────────

    #[test]
    fn reseed_refreshes_an_untouched_row() {
        let db = make_db();
        db.converge_chat_model_context_seed(&[input("glm-5.1")])
            .unwrap();

        // The vendor published a correction; the shipped seed now says 1M.
        let outcome = db.reseed_chat_model_context(&[one_m("glm-5.1")]).unwrap();
        assert_eq!(
            outcome,
            ReseedOutcome { inserted: 0, updated: 1, unchanged: 0, preserved_user_edits: 0, retired: 0,
                written: vec![("glm-5.1".into(), "updated".into())] }
        );
        let row = db.get_chat_model_context("glm-5.1").unwrap().unwrap();
        assert_eq!(row.context_window, 1_000_000);
        assert!(row.window_1m);
        assert!(!row.user_edited);
    }

    #[test]
    fn reseed_leaves_a_user_edited_row_byte_identical() {
        let db = make_db();
        db.converge_chat_model_context_seed(&[input("glm-5.1")])
            .unwrap();
        let edited = db
            .upsert_chat_model_context(
                ChatModelContextInput {
                    context_window: 111_111,
                    source: "my own measurement".into(),
                    ..input("glm-5.1")
                },
                true,
            )
            .unwrap();

        let outcome = db.reseed_chat_model_context(&[one_m("glm-5.1")]).unwrap();
        assert_eq!(
            outcome,
            ReseedOutcome { inserted: 0, updated: 0, unchanged: 0, preserved_user_edits: 1, retired: 0,
                written: Vec::new() }
        );

        let after = db.get_chat_model_context("glm-5.1").unwrap().unwrap();
        assert_eq!(
            after, edited,
            "every field of a user-edited row survives reseed unchanged, \
             including updated_at"
        );
    }

    #[test]
    fn reseed_inserts_a_newly_shipped_model_and_skips_an_identical_row() {
        let db = make_db();
        db.converge_chat_model_context_seed(&[input("glm-5.1")])
            .unwrap();

        let outcome = db
            .reseed_chat_model_context(&[input("glm-5.1"), one_m("glm-5.3")])
            .unwrap();
        assert_eq!(
            outcome,
            ReseedOutcome { inserted: 1, updated: 0, unchanged: 1, preserved_user_edits: 0, retired: 0,
                written: vec![("glm-5.3".into(), "inserted".into())] },
            "a new vendor model arrives; the identical row is not rewritten"
        );
    }

    /// v0.2.101 (Q6/G1): the provenance record. A converge that inserts,
    /// updates and leaves alone must record EXACTLY the two rows it wrote
    /// with their actions — the unchanged row must NOT appear (a
    /// steady-state boot stays quiet) and the record is what the commands
    /// layer turns into the model-picker provenance log lines.
    /// (Red-proof mutation: drop either `written.push` in `converge_rows`
    /// and this fails.)
    #[test]
    fn converge_records_exactly_the_rows_it_wrote() {
        let db = make_db();
        db.upsert_chat_model_context(input("glm-5.1"), false).unwrap();

        let outcome = db
            .converge_chat_model_context_seed(&[
                one_m("glm-5.1"),      // present, stale → updated
                input("glm-5.3"),      // absent → inserted
                input("glm-5.2"),      // absent → inserted
            ])
            .unwrap();

        assert_eq!(
            outcome.written,
            vec![
                ("glm-5.1".to_string(), "updated".to_string()),
                ("glm-5.3".to_string(), "inserted".to_string()),
                ("glm-5.2".to_string(), "inserted".to_string()),
            ],
            "written records each row that landed, in seed order, with its action"
        );

        // Steady state: converging the SAME seed again writes nothing and
        // records nothing — the provenance log stays silent on a boot that
        // changed no row.
        let again = db
            .converge_chat_model_context_seed(&[
                one_m("glm-5.1"),
                input("glm-5.3"),
                input("glm-5.2"),
            ])
            .unwrap();
        assert!(again.written.is_empty(), "an unchanged table records nothing");
    }

    #[test]
    fn reseed_never_prunes_a_user_edited_row_the_seed_does_not_mention() {
        let db = make_db();
        db.upsert_chat_model_context(
            ChatModelContextInput {
                vendor: "kimi".into(),
                source: "https://platform.moonshot.ai/docs".into(),
                ..input("kimi-k2")
            },
            true,
        )
        .unwrap();

        db.reseed_chat_model_context(&[input("glm-5.1")]).unwrap();

        assert!(
            db.get_chat_model_context("kimi-k2").unwrap().is_some(),
            "reseed adds, refreshes and retires stale SEEDED rows; a row a \
             human wrote is never one of them"
        );
    }

    // ── retire: stale seeded rows (v0.2.101, NB-02) ───────────────────────
    //
    // The defect this section pins: the seed converge only ever MERGED, so a
    // model retired from the shipped seed (glm-5.2, owner ruling v0.2.100)
    // stayed in every existing table — a row the pane listed and the export
    // served while the gateway refused the id on every route.

    /// The NB-02 shape exactly: an existing table holding the retired row a
    /// pre-v0.2.100 seed wrote (plain, `user_edited = 0`), the same retired
    /// id re-added BY HAND (`user_edited = 1`), and a current-seed row.
    /// After the converge: the machine-written retired row is GONE, the
    /// hand-added one is KEPT, the seed row is kept, and the outcome counts
    /// exactly one retirement.
    #[test]
    fn converge_retires_a_stale_seeded_row_but_never_a_user_edited_one() {
        let db = make_db();
        // The retired row exactly as an earlier boot seeded it.
        db.upsert_chat_model_context(one_m("glm-5.2"), false).unwrap();
        // The user's OWN glm-5.2 row (they re-added it by hand): a human
        // hand wrote it, so no automatic path may remove it.
        db.upsert_chat_model_context(
            ChatModelContextInput {
                context_window: 555_555,
                source: "my own measurement".into(),
                ..one_m("glm-5.2")
            },
            true,
        )
        .unwrap();
        // A row the current seed still ships.
        db.upsert_chat_model_context(input("glm-5.1"), false).unwrap();
        let seed = vec![input("glm-5.1"), one_m("glm-5.3")];

        let outcome = db.converge_chat_model_context_seed(&seed).unwrap();

        assert_eq!(outcome.retired, 0, "glm-5.2 is user_edited now, not stale");
        assert!(
            db.get_chat_model_context("glm-5.2")
                .unwrap()
                .expect("the user's row must survive every automatic path")
                .user_edited,
            "the hand-re-added row keeps its user-edited guard"
        );

        // The actual stale shape: a SECOND table (an install where nobody
        // touched the row) holding the machine-written glm-5.2.
        let db2 = make_db();
        db2.upsert_chat_model_context(one_m("glm-5.2"), false).unwrap();
        db2.upsert_chat_model_context(input("glm-5.1"), false).unwrap();

        let outcome2 = db2.converge_chat_model_context_seed(&seed).unwrap();

        assert_eq!(outcome2.retired, 1, "exactly the stale seeded row");
        assert!(
            db2.get_chat_model_context("glm-5.2").unwrap().is_none(),
            "the machine-written glm-5.2 row is retired, not refreshed"
        );
        assert!(
            db2.get_chat_model_context("glm-5.1").unwrap().is_some(),
            "a row the seed still ships is kept"
        );
        assert!(
            db2.get_chat_model_context("glm-5.3").unwrap().is_some(),
            "the newly shipped row landed"
        );
        assert_eq!(db2.list_chat_model_context().unwrap().len(), 2);

        // The next boot is a no-op retire: nothing left to remove, no
        // counter creep.
        let again = db2.converge_chat_model_context_seed(&seed).unwrap();
        assert_eq!(again.retired, 0, "a retired row does not linger anywhere");
    }

    /// The retire writes NO delete tombstone — that table is the user's
    /// delete marker ("do not reseed this id"), and a model retired from the
    /// seed must come BACK automatically if a future release re-ships it.
    #[test]
    fn a_retired_stale_row_leaves_no_delete_tombstone_behind() {
        let db = make_db();
        db.upsert_chat_model_context(one_m("glm-5.2"), false).unwrap();

        db.converge_chat_model_context_seed(&[input("glm-5.1")])
            .unwrap();

        assert!(
            db.list_chat_model_context_tombstones()
                .unwrap()
                .is_empty(),
            "the retire is not the user's delete; it must not block a future \
             seed that re-ships the id"
        );

        // And indeed: a future seed re-shipping the id re-inserts it through
        // the ordinary boot converge, no button press needed.
        let outcome = db
            .converge_chat_model_context_seed(&[input("glm-5.1"), one_m("glm-5.2")])
            .unwrap();
        assert_eq!(outcome.inserted, 1);
        assert!(db.get_chat_model_context("glm-5.2").unwrap().is_some());
    }

    /// "Reseed from shipped defaults" applies the same retire: the button's
    /// label promises the shipped set, and a stale machine row is not part
    /// of it. (A user's own row is exempt — pinned by the test above.)
    #[test]
    fn reseed_retires_a_stale_seeded_row_too() {
        let db = make_db();
        db.upsert_chat_model_context(one_m("glm-5.2"), false).unwrap();

        let outcome = db.reseed_chat_model_context(&[input("glm-5.1")]).unwrap();

        assert_eq!(outcome.retired, 1);
        assert!(db.get_chat_model_context("glm-5.2").unwrap().is_none());
    }

    #[test]
    fn reseed_restores_a_deleted_shipped_row() {
        let db = make_db();
        db.converge_chat_model_context_seed(&[input("glm-5.1")])
            .unwrap();
        db.delete_chat_model_context("glm-5.1").unwrap();

        let outcome = db.reseed_chat_model_context(&[input("glm-5.1")]).unwrap();
        assert_eq!(outcome.inserted, 1, "\"reseed from shipped defaults\" restores it");
    }

    // ── export document ──────────────────────────────────────────────────

    fn row(model_id: &str, window_1m: bool, note: &str) -> ChatModelContextRow {
        ChatModelContextRow {
            model_id: model_id.into(),
            vendor: "zai".into(),
            context_window: if window_1m { 1_000_000 } else { 200_000 },
            max_output: 128_000,
            window_1m,
            text_only: false,
            source: "https://docs.z.ai/guides/llm/x".into(),
            source_note: note.into(),
            user_edited: false,
            updated_at: "2026-09-02T00:00:00Z".into(),
        }
    }

    #[test]
    fn export_document_matches_the_gateway_readers_contract() {
        let doc = export_document(
            &[row("glm-5.2", true, ""), row("glm-4.5-air", false, "cited from the glm-4.5 card")],
            &[],
            "2026-09-02T18:04:11Z",
        );

        assert_eq!(doc["schema_version"], serde_json::json!(1));
        assert_eq!(doc["generated_at"], serde_json::json!("2026-09-02T18:04:11Z"));
        assert_eq!(doc["source"], serde_json::json!("launcher.db"));

        let models = doc["models"].as_object().expect("models is an object");
        assert_eq!(models.len(), 2);
        let air = &models["glm-4.5-air"];
        assert_eq!(air["vendor"], serde_json::json!("zai"));
        assert_eq!(air["context_window"], serde_json::json!(200_000));
        assert_eq!(air["max_output"], serde_json::json!(128_000));
        assert_eq!(air["window_1m"], serde_json::json!(false));
        assert_eq!(air["source"], serde_json::json!("https://docs.z.ai/guides/llm/x"));
        assert_eq!(
            air["source_note"],
            serde_json::json!("cited from the glm-4.5 card"),
            "the caveat must survive the round trip into the export"
        );
        // window_1m must be a JSON BOOLEAN, not the DB's 0/1 integer: the
        // reader coerces with bool(), so an integer would work by accident
        // while making the file wrong for a human and for jq.
        assert!(air["window_1m"].is_boolean());

        // CROSS-LANE CONTRACT: the key is always present, even when empty —
        // the gateway reads exactly `tombstones`, and an absent key would
        // have to be guessed at.
        assert_eq!(doc["tombstones"], serde_json::json!([]));
        assert!(doc.as_object().unwrap().contains_key("tombstones"));
    }

    #[test]
    fn the_export_carries_the_tombstones_sorted_deduped_and_named_exactly() {
        // The gateway's seed fallback still knows every shipped id, so a row
        // deleted here has to travel to it as a deletion — otherwise it keeps
        // advertising the `[1m]` companion for a model this machine removed.
        let doc = export_document(
            &[row("glm-5.2", true, "")],
            &[
                "glm-5.1".to_string(),
                "claude-opus-5".to_string(),
                "glm-5.1".to_string(),
            ],
            "t",
        );
        assert_eq!(
            doc["tombstones"],
            serde_json::json!(["claude-opus-5", "glm-5.1"]),
            "sorted and de-duplicated, so a re-export is byte-identical"
        );
        // The models map is untouched by it.
        assert!(doc["models"].as_object().unwrap().contains_key("glm-5.2"));
    }

    #[test]
    fn a_tombstoned_id_is_not_also_a_model_row() {
        // Belt and braces on the contract's meaning: the two lists cannot
        // disagree, because a deleted row is gone from the table.
        let db = make_db();
        db.converge_chat_model_context_seed(&[input("glm-5.1"), one_m("glm-5.2")])
            .unwrap();
        db.delete_chat_model_context("glm-5.1").unwrap();
        let doc = export_document(
            &db.list_chat_model_context().unwrap(),
            &db.list_chat_model_context_tombstones().unwrap(),
            "t",
        );
        assert_eq!(doc["tombstones"], serde_json::json!(["glm-5.1"]));
        assert!(!doc["models"].as_object().unwrap().contains_key("glm-5.1"));
    }

    #[test]
    fn export_document_omits_an_empty_source_note() {
        let doc = export_document(&[row("glm-5.2", true, "")], &[], "t");
        let entry = doc["models"]["glm-5.2"].as_object().unwrap();
        assert!(
            !entry.contains_key("source_note"),
            "an empty note is omitted, matching the shipped seed's shape"
        );
    }

    #[test]
    fn export_document_orders_models_by_id_regardless_of_input_order() {
        // preserve_order is on, so serialisation follows INSERTION order —
        // this test is what makes that order deterministic.
        let doc = export_document(
            &[row("glm-5.2", true, ""), row("glm-4.5", false, ""), row("glm-5.1", false, "")],
            &[],
            "t",
        );
        let keys: Vec<&String> = doc["models"].as_object().unwrap().keys().collect();
        assert_eq!(keys, vec!["glm-4.5", "glm-5.1", "glm-5.2"]);

        // Top-level keys are in declaration order, not alphabetical (which
        // would put `generated_at` first).
        let top: Vec<&String> = doc.as_object().unwrap().keys().collect();
        assert_eq!(
            top,
            vec!["schema_version", "generated_at", "source", "models", "tombstones"],
            "`tombstones` goes LAST — the gateway's reader keys off names, but \
             a stable key order is what keeps a re-export byte-identical"
        );
    }

    #[test]
    fn export_document_of_an_empty_table_is_a_valid_document_not_a_null() {
        let doc = export_document(&[], &[], "t");
        assert_eq!(doc["schema_version"], serde_json::json!(1));
        assert!(doc["models"].as_object().unwrap().is_empty());
    }

    #[test]
    fn re_exporting_unchanged_rows_is_byte_identical() {
        let rows = vec![row("glm-5.2", true, ""), row("glm-4.5", false, "n")];
        let a = serde_json::to_string_pretty(&export_document(&rows, &[], "t")).unwrap();
        let b = serde_json::to_string_pretty(&export_document(&rows, &[], "t")).unwrap();
        assert_eq!(a, b, "the gateway's (mtime_ns,size) detector depends on this");
    }

    // ── parse_document (the seed loader / export inverse) ────────────────

    #[test]
    fn parse_document_round_trips_export_document() {
        let rows = vec![
            row("glm-5.2", true, ""),
            row("glm-4.5-air", false, "cited from the glm-4.5 card"),
        ];
        let parsed = parse_document(&export_document(&rows, &[], "t")).unwrap();
        assert_eq!(parsed.len(), 2);
        // Sorted by id, and every carried field survives the round trip.
        assert_eq!(parsed[0].model_id, "glm-4.5-air");
        assert_eq!(parsed[0].source_note, "cited from the glm-4.5 card");
        assert_eq!(parsed[1].model_id, "glm-5.2");
        assert!(parsed[1].window_1m);
        assert_eq!(parsed[1].context_window, 1_000_000);
        assert_eq!(parsed[1].max_output, 128_000);
        assert_eq!(parsed[1].vendor, "zai");
    }

    /// The wire shape of an UNSTATED row: the export carries `max_output: 0`
    /// VERBATIM — 0 IS the marker, and the gateway's reader folds it to "no
    /// information" (`context_table.py` coalesces missing-or-zero with
    /// `or 0`; `catalog::_positive` maps 0 → None so no description ever
    /// publishes it as a count). The export must therefore never OMIT the key
    /// and never substitute a figure; `parse_document` accepts it back.
    #[test]
    fn export_carries_an_unstated_max_output_as_zero_verbatim() {
        let unstated = ChatModelContextRow {
            max_output: 0,
            ..row("qwen3.8-max", false, "vendor page states no max-output figure")
        };
        let doc = export_document(&[unstated], &[], "t");
        let entry = &doc["models"]["qwen3.8-max"];
        assert_eq!(
            entry["max_output"],
            serde_json::json!(0),
            "the unstated marker travels as an explicit 0, the shape the \
             shipped seed and the gateway reader both carry"
        );
        assert!(
            entry.as_object().unwrap().contains_key("max_output"),
            "the key is present — an absent key and 0 must not diverge in \
             meaning across the wire"
        );
        assert_eq!(entry["context_window"], serde_json::json!(200_000));

        let parsed = parse_document(&doc).expect("the unstated row must parse back");
        assert_eq!(parsed.len(), 1);
        assert_eq!(
            parsed[0].max_output, 0,
            "0 round-trips through export → parse; nothing invents a figure"
        );
    }

    // ── text_only (v0.2.101 — the image-capability flag) ────────────────
    //
    // Pre-fix, the launcher's seed mirror silently DROPPED the field: the
    // pane could not show it, a reseed could not carry it, and the export
    // could not publish it. These three pin the whole chain — parse reads
    // it, converge carries it (including over rows that predate the column),
    // export ships it.

    #[test]
    fn export_carries_the_image_capability_flag_on_every_row() {
        let text_only_model = ChatModelContextRow {
            text_only: true,
            ..row("glm-5.3", false, "text_only=true: vendor page states verbatim")
        };
        let doc = export_document(&[text_only_model, row("glm-5.3-flash", false, "")], &[], "t");
        assert_eq!(
            doc["models"]["glm-5.3"]["text_only"],
            serde_json::json!(true),
            "a flagged row exports the flag"
        );
        // Always present, even when false — same treatment as `window_1m`,
        // so the export is a complete answer to "can this model see images?"
        assert_eq!(
            doc["models"]["glm-5.3-flash"]["text_only"],
            serde_json::json!(false),
            "an unflagged row carries an explicit false, never an absent key"
        );
    }

    #[test]
    fn parse_document_reads_the_image_capability_flag_and_defaults_absent_to_false() {
        // The shipped seed states `text_only` only on the rows the vendor
        // page decides; absent must mean false — the SAME direction the
        // gateway's reader defaults a missing key (`bool(raw.get(...))`).
        let doc = serde_json::json!({
            "schema_version": 1,
            "models": {
                "glm-5.3": {
                    "vendor": "zai", "context_window": 200000, "max_output": 128000,
                    "window_1m": false, "text_only": true,
                    "source": "https://docs.z.ai/guides/llm/glm-5.3"
                },
                "glm-4.5-air": {
                    "vendor": "zai", "context_window": 200000, "max_output": 128000,
                    "window_1m": false,
                    "source": "https://docs.z.ai/guides/llm/glm-4.5-air"
                }
            }
        });
        let parsed = parse_document(&doc).unwrap();
        assert_eq!(parsed.len(), 2);
        assert_eq!(
            parsed.iter().find(|r| r.model_id == "glm-5.3").unwrap().text_only,
            true,
            "a stated flag is READ, not dropped on the floor"
        );
        assert_eq!(
            parsed
                .iter()
                .find(|r| r.model_id == "glm-4.5-air")
                .unwrap()
                .text_only,
            false,
            "an absent flag defaults to false (the gateway reader's direction)"
        );
    }

    #[test]
    fn converge_carries_text_only_and_pre_column_rows_converge_without_loss() {
        // The upgrade shape: a table whose rows predate the `text_only`
        // column (migration 048 backfilled them to 0 = false) converges
        // against a seed that now states the flag — the pre-fix mirror
        // dropped the field, so this exact converge was a silent no-op.
        let db = make_db();
        // Row 1: shipped-shaped, pre-column (text_only = false by backfill).
        db.upsert_chat_model_context(input("glm-5.3"), false).unwrap();
        // Row 2: a user edit — must stay untouched, flag included.
        let mut edited = input("glm-5.2");
        edited.source = "internal wiki".into();
        db.upsert_chat_model_context(edited.clone(), true).unwrap();

        // The shipped seed now states text_only on glm-5.3; every OTHER
        // field of that row is unchanged.
        let mut flagged = input("glm-5.3");
        flagged.text_only = true;
        let mut flagged_edit = edited.clone();
        flagged_edit.text_only = true;

        let outcome = db
            .converge_chat_model_context_seed(&[flagged, flagged_edit])
            .unwrap();
        assert_eq!(
            (outcome.inserted, outcome.updated, outcome.preserved_user_edits),
            (0, 1, 1),
            "the flagged row is an UPDATE (only the flag changed), the user \
             edit is preserved"
        );
        let rows = db.list_chat_model_context().unwrap();
        let glm53 = rows.iter().find(|r| r.model_id == "glm-5.3").unwrap();
        assert!(glm53.text_only, "the converge WROTE the flag");
        let glm52 = rows.iter().find(|r| r.model_id == "glm-5.2").unwrap();
        assert!(
            !glm52.text_only && glm52.user_edited && glm52.source == "internal wiki",
            "the user-edited row is byte-identical to what they wrote"
        );
    }

    #[test]
    fn parse_document_skips_comment_keys_the_seed_carries() {
        let doc = serde_json::json!({
            "schema_version": 1,
            "_comment": "seed prose the gateway reader also skips",
            "models": {
                "_note": {"vendor": "x"},
                "glm-5.1": {
                    "vendor": "zai", "context_window": 200000, "max_output": 128000,
                    "window_1m": false, "source": "https://docs.z.ai/guides/llm/glm-5.1"
                }
            }
        });
        let parsed = parse_document(&doc).unwrap();
        assert_eq!(parsed.len(), 1);
        assert_eq!(parsed[0].model_id, "glm-5.1");
    }

    #[test]
    fn parse_document_refuses_an_unsupported_schema_version_by_name() {
        let doc = serde_json::json!({"schema_version": 99, "models": {}});
        let err = parse_document(&doc).expect_err("must refuse");
        assert!(err.contains("schema_version 99"), "got: {}", err);
    }

    #[test]
    fn parse_document_is_strict_where_the_gateway_reader_is_tolerant() {
        // The reader DROPS an uncited row and warns, because it may be
        // handed a hand-edited file. This parser only ever reads a file
        // that ships with the code, so it fails the load instead.
        let doc = serde_json::json!({
            "schema_version": 1,
            "models": {"glm-5.1": {
                "vendor": "zai", "context_window": 200000, "max_output": 128000,
                "window_1m": false, "source": ""
            }}
        });
        let err = parse_document(&doc).expect_err("uncited seed row must fail the load");
        assert!(err.contains("source citation is required"), "got: {}", err);

        // Missing numbers are named rather than defaulted to zero.
        let doc = serde_json::json!({
            "schema_version": 1,
            "models": {"glm-5.1": {"vendor": "zai", "source": "https://x.invalid"}}
        });
        let err = parse_document(&doc).expect_err("missing window must fail");
        assert!(err.contains("context_window"), "got: {}", err);

        // A NEGATIVE max_output fails the load (v0.2.96: 0 = unstated is the
        // only non-positive value with a meaning; a missing max_output is
        // still named rather than defaulted, as asserted above).
        let doc = serde_json::json!({
            "schema_version": 1,
            "models": {"glm-5.1": {
                "vendor": "zai", "context_window": 200000, "max_output": -1,
                "window_1m": false, "source": "https://x.invalid"
            }}
        });
        let err = parse_document(&doc).expect_err("negative max_output must fail the load");
        assert!(err.contains("cannot be negative"), "got: {}", err);
    }

    #[test]
    fn parse_document_refuses_a_document_that_is_not_the_contract() {
        assert!(parse_document(&serde_json::json!([])).is_err());
        assert!(parse_document(&serde_json::json!({"models": {}})).is_err());
        assert!(parse_document(&serde_json::json!({"schema_version": 1})).is_err());
    }
}

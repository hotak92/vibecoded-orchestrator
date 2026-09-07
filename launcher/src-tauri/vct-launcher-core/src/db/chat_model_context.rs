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
    /// Max output tokens.
    pub max_output: i64,
    /// `true` → the gateway advertises this id as `<id>[1m]`.
    pub window_1m: bool,
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
    pub max_output: i64,
    pub window_1m: bool,
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
        if self.max_output <= 0 {
            return Err(format!(
                "max output for `{}` must be a positive number of tokens \
                 (got {})",
                model_id, self.max_output
            ));
        }
        Ok(Self {
            model_id,
            vendor,
            context_window: self.context_window,
            max_output: self.max_output,
            window_1m: self.window_1m,
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
            && self.source == row.source
            && self.source_note == row.source_note
    }
}

/// What a reseed actually did. Four counters rather than a bool because the
/// pane reports it to the user, and "3 updated, 2 of your edits preserved" is
/// the sentence that makes the `user_edited` guard visible instead of
/// folklore.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
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
    })
}

const SELECT_COLUMNS: &str = "model_id, vendor, context_window, max_output, \
                              window_1m, source, source_note, user_edited, \
                              updated_at";

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
            source: input.source,
            source_note: input.source_note,
            user_edited,
            updated_at: now,
        };
        {
            let guard = self.lock();
            guard
                .execute(
                    "INSERT INTO chat_model_context
                        (model_id, vendor, context_window, max_output, window_1m,
                         source, source_note, user_edited, updated_at)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
                     ON CONFLICT(model_id) DO UPDATE SET
                        vendor         = excluded.vendor,
                        context_window = excluded.context_window,
                        max_output     = excluded.max_output,
                        window_1m      = excluded.window_1m,
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
                    ],
                )
                .map_err(|e| format!("upsert_chat_model_context: {}", e))?;
        }
        Ok(row)
    }

    /// Delete one row. `Ok(false)` when nothing matched — an absent row is
    /// not an error, and reporting it as one would make a double-click on the
    /// pane's delete button look like a failure.
    pub fn delete_chat_model_context(&self, model_id: &str) -> Result<bool, String> {
        let guard = self.lock();
        let affected = guard
            .execute(
                "DELETE FROM chat_model_context WHERE model_id = ?1",
                params![model_id],
            )
            .map_err(|e| format!("delete_chat_model_context: {}", e))?;
        Ok(affected > 0)
    }

    /// First-boot seed: insert every shipped row IF AND ONLY IF the table is
    /// empty. Returns the number inserted (0 when the table already had rows).
    ///
    /// Emptiness — not per-row absence — is the gate on purpose. A user who
    /// deletes a row they do not want must not have it silently reinstated on
    /// the next boot; restoring shipped rows is what the explicit "Reseed"
    /// button is for.
    pub fn seed_chat_model_context_if_empty(
        &self,
        rows: &[ChatModelContextInput],
    ) -> Result<usize, String> {
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
            .map_err(|e| format!("seed_chat_model_context_if_empty begin: {}", e))?;
        let existing: i64 = tx
            .query_row("SELECT COUNT(*) FROM chat_model_context", [], |r| r.get(0))
            .map_err(|e| format!("seed_chat_model_context_if_empty count: {}", e))?;
        if existing > 0 {
            return Ok(0);
        }
        let mut inserted = 0usize;
        for row in &validated {
            tx.execute(
                "INSERT INTO chat_model_context
                    (model_id, vendor, context_window, max_output, window_1m,
                     source, source_note, user_edited, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 0, ?8)",
                params![
                    row.model_id,
                    row.vendor,
                    row.context_window,
                    row.max_output,
                    i64::from(row.window_1m),
                    row.source,
                    row.source_note,
                    now,
                ],
            )
            .map_err(|e| format!("seed row {}: {}", row.model_id, e))?;
            inserted += 1;
        }
        tx.commit()
            .map_err(|e| format!("seed_chat_model_context_if_empty commit: {}", e))?;
        Ok(inserted)
    }

    /// Re-apply the shipped rows, NEVER over a user edit.
    ///
    /// Per row: absent → insert; present with `user_edited = 0` and different
    /// values → update; present with `user_edited = 0` and identical values →
    /// left untouched (so `updated_at` keeps meaning "when this row last
    /// changed"); present with `user_edited = 1` → LEFT ALONE ENTIRELY.
    ///
    /// Rows in the table that the shipped seed does not mention are never
    /// removed: reseed adds and refreshes, it does not prune. A user's own
    /// Kimi/Qwen rows survive it.
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
        let mut outcome = ReseedOutcome::default();
        let mut guard = self.lock();
        let tx = guard
            .transaction()
            .map_err(|e| format!("reseed_chat_model_context begin: {}", e))?;
        for row in &validated {
            let existing: Option<ChatModelContextRow> = tx
                .query_row(
                    &format!(
                        "SELECT {SELECT_COLUMNS} FROM chat_model_context WHERE model_id = ?1"
                    ),
                    params![row.model_id],
                    row_from_sql,
                )
                .optional()
                .map_err(|e| format!("reseed lookup {}: {}", row.model_id, e))?;

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
                            updated_at     = ?8
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
                        ],
                    )
                    .map_err(|e| format!("reseed update {}: {}", row.model_id, e))?;
                    outcome.updated += 1;
                }
                None => {
                    tx.execute(
                        "INSERT INTO chat_model_context
                            (model_id, vendor, context_window, max_output, window_1m,
                             source, source_note, user_edited, updated_at)
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 0, ?8)",
                        params![
                            row.model_id,
                            row.vendor,
                            row.context_window,
                            row.max_output,
                            i64::from(row.window_1m),
                            row.source,
                            row.source_note,
                            now,
                        ],
                    )
                    .map_err(|e| format!("reseed insert {}: {}", row.model_id, e))?;
                    outcome.inserted += 1;
                }
            }
        }
        tx.commit()
            .map_err(|e| format!("reseed_chat_model_context commit: {}", e))?;
        Ok(outcome)
    }
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
pub fn export_document(rows: &[ChatModelContextRow], generated_at: &str) -> serde_json::Value {
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
        entry.insert("source".into(), serde_json::Value::from(row.source.clone()));
        if !row.source_note.is_empty() {
            entry.insert(
                "source_note".into(),
                serde_json::Value::from(row.source_note.clone()),
            );
        }
        models.insert(row.model_id.clone(), serde_json::Value::Object(entry));
    }

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
                ChatModelContextInput { max_output: 0, ..input("x") },
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
    fn seed_populates_an_empty_table_once_and_never_again() {
        let db = make_db();
        let seed = vec![input("glm-5.1"), one_m("glm-5.2")];

        assert_eq!(db.seed_chat_model_context_if_empty(&seed).unwrap(), 2);
        // Second boot: the table is not empty, so the seeder does nothing.
        assert_eq!(db.seed_chat_model_context_if_empty(&seed).unwrap(), 0);
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
    fn seed_does_not_reinstate_a_row_the_user_deleted() {
        let db = make_db();
        let seed = vec![input("glm-5.1"), one_m("glm-5.2")];
        db.seed_chat_model_context_if_empty(&seed).unwrap();
        db.delete_chat_model_context("glm-5.1").unwrap();

        // Next boot.
        assert_eq!(db.seed_chat_model_context_if_empty(&seed).unwrap(), 0);
        let ids: Vec<String> = db
            .list_chat_model_context()
            .unwrap()
            .into_iter()
            .map(|r| r.model_id)
            .collect();
        assert_eq!(
            ids,
            vec!["glm-5.2"],
            "an emptiness gate, not a per-row gate — the deleted row stays gone"
        );
    }

    #[test]
    fn a_seed_batch_with_one_bad_row_writes_nothing() {
        let db = make_db();
        let seed = vec![
            input("glm-5.1"),
            ChatModelContextInput { source: "".into(), ..input("glm-5.2") },
        ];
        assert!(db.seed_chat_model_context_if_empty(&seed).is_err());
        assert!(
            db.list_chat_model_context().unwrap().is_empty(),
            "a half-seeded table would hide a broken build behind a populated look"
        );
    }

    // ── reseed: the user_edited guard, act + leave-alone ──────────────────

    #[test]
    fn reseed_refreshes_an_untouched_row() {
        let db = make_db();
        db.seed_chat_model_context_if_empty(&[input("glm-5.1")])
            .unwrap();

        // The vendor published a correction; the shipped seed now says 1M.
        let outcome = db.reseed_chat_model_context(&[one_m("glm-5.1")]).unwrap();
        assert_eq!(
            outcome,
            ReseedOutcome { inserted: 0, updated: 1, unchanged: 0, preserved_user_edits: 0 }
        );
        let row = db.get_chat_model_context("glm-5.1").unwrap().unwrap();
        assert_eq!(row.context_window, 1_000_000);
        assert!(row.window_1m);
        assert!(!row.user_edited);
    }

    #[test]
    fn reseed_leaves_a_user_edited_row_byte_identical() {
        let db = make_db();
        db.seed_chat_model_context_if_empty(&[input("glm-5.1")])
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
            ReseedOutcome { inserted: 0, updated: 0, unchanged: 0, preserved_user_edits: 1 }
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
        db.seed_chat_model_context_if_empty(&[input("glm-5.1")])
            .unwrap();

        let outcome = db
            .reseed_chat_model_context(&[input("glm-5.1"), one_m("glm-5.3")])
            .unwrap();
        assert_eq!(
            outcome,
            ReseedOutcome { inserted: 1, updated: 0, unchanged: 1, preserved_user_edits: 0 },
            "a new vendor model arrives; the identical row is not rewritten"
        );
    }

    #[test]
    fn reseed_never_prunes_a_row_the_shipped_seed_does_not_mention() {
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
            "reseed adds and refreshes; it does not prune the user's own rows"
        );
    }

    #[test]
    fn reseed_restores_a_deleted_shipped_row() {
        let db = make_db();
        db.seed_chat_model_context_if_empty(&[input("glm-5.1")])
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
    }

    #[test]
    fn export_document_omits_an_empty_source_note() {
        let doc = export_document(&[row("glm-5.2", true, "")], "t");
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
            "t",
        );
        let keys: Vec<&String> = doc["models"].as_object().unwrap().keys().collect();
        assert_eq!(keys, vec!["glm-4.5", "glm-5.1", "glm-5.2"]);

        // Top-level keys are in declaration order, not alphabetical (which
        // would put `generated_at` first).
        let top: Vec<&String> = doc.as_object().unwrap().keys().collect();
        assert_eq!(top, vec!["schema_version", "generated_at", "source", "models"]);
    }

    #[test]
    fn export_document_of_an_empty_table_is_a_valid_document_not_a_null() {
        let doc = export_document(&[], "t");
        assert_eq!(doc["schema_version"], serde_json::json!(1));
        assert!(doc["models"].as_object().unwrap().is_empty());
    }

    #[test]
    fn re_exporting_unchanged_rows_is_byte_identical() {
        let rows = vec![row("glm-5.2", true, ""), row("glm-4.5", false, "n")];
        let a = serde_json::to_string_pretty(&export_document(&rows, "t")).unwrap();
        let b = serde_json::to_string_pretty(&export_document(&rows, "t")).unwrap();
        assert_eq!(a, b, "the gateway's (mtime_ns,size) detector depends on this");
    }

    // ── parse_document (the seed loader / export inverse) ────────────────

    #[test]
    fn parse_document_round_trips_export_document() {
        let rows = vec![
            row("glm-5.2", true, ""),
            row("glm-4.5-air", false, "cited from the glm-4.5 card"),
        ];
        let parsed = parse_document(&export_document(&rows, "t")).unwrap();
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
    }

    #[test]
    fn parse_document_refuses_a_document_that_is_not_the_contract() {
        assert!(parse_document(&serde_json::json!([])).is_err());
        assert!(parse_document(&serde_json::json!({"models": {}})).is_err());
        assert!(parse_document(&serde_json::json!({"schema_version": 1})).is_err());
    }
}

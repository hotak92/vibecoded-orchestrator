// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! RL telemetry events — queryable replacement for the JSONL corpus.
//!
//! Migration 025 (v0.2.47) ships one table:
//!
//!   * `rl_events` — append-only `{retrieval, citation}` events written by
//!     the MCP-side telemetry writer (`claude_mcp_servers/rl_client/`)
//!     via the hub's `POST /api/v1/rl/events` route. The full event JSON
//!     lives in `payload_json TEXT`; the indexed columns (event_type,
//!     ts, project_id, task_id, embedding_source) are denormalized copies
//!     of fields inside the payload kept SQL-queryable.
//!
//! WRITER MODEL: this module exposes `insert_rl_event` only. The hub
//! handler (`vct-hub/src/rl_events_api.rs`) parses the incoming JSON,
//! pulls the indexed fields, and calls this helper with the raw payload.
//! Python clients NEVER open launcher.db directly — preserves the
//! single-writer architectural rule documented at
//! `vco_lib/config_projection.py:488-491`.
//!
//! READ MODEL: `list_rl_events` for dashboard widgets and `count_rl_events`
//! for the per-project counter the launcher Identity tab shows. The
//! offline trainer's read path uses the hub's GET route directly to
//! avoid in-process SQLite coupling.

use std::io::Write as _;
use std::path::{Path, PathBuf};

use rusqlite::{params, OptionalExtension};

use super::Db;

/// One RL event row. Matches the migration-025 schema (+ the migration-039
/// quarantine columns, v0.2.75 RL-14).
///
/// `payload_json` carries the full v3 event JSON verbatim. The indexed
/// columns above are denormalized for SQL queries; callers that need
/// non-indexed fields (e.g. per-node `n_emb`, `linked_embs`, `cosine_sims`)
/// must parse `payload_json` themselves.
///
/// `quarantined_at` (unix-ms) + `quarantine_reason` mark POISONED rows —
/// e.g. the historical out-of-range-score class that pre-dates the v0.2.70
/// F-E writer clamp. Marked rows stay on disk (query-distribution signal)
/// but are excluded from training-data reads by default.
#[derive(Debug, Clone)]
pub struct RlEvent {
    pub id: i64,
    pub event_type: String,
    pub schema_version: i64,
    /// Unix epoch millis.
    pub ts_ms: i64,
    pub project_id: Option<String>,
    pub project_name: Option<String>,
    pub task_id: String,
    pub task_type: Option<String>,
    pub embedding_source: Option<String>,
    pub embedding_dim: Option<i64>,
    pub embedding_model: Option<String>,
    pub payload_json: String,
    /// RL-14: unix-ms when the row was quarantined; NULL = clean.
    pub quarantined_at: Option<i64>,
    /// RL-14: stable machine tag (e.g. `score_out_of_range`).
    pub quarantine_reason: Option<String>,
}

/// RL-14: the app_state key guarding the one-time historical marking pass.
/// Present (any value) ⇒ the backfill already ran on this launcher.db.
pub const QUARANTINE_BACKFILL_STATE_KEY: &str = "rl_events.quarantine_backfill_v1";

/// RL-14: stable reason tag for the historical out-of-range-score class.
pub const QUARANTINE_REASON_SCORE_OUT_OF_RANGE: &str = "score_out_of_range";

// ─── R1 (v0.2.91): retention ARCHIVE — archive-then-delete ──────────────────
//
// WHY THIS EXISTS. Before v0.2.91 `prune_rl_events` was a bare
// `DELETE FROM rl_events WHERE ts < ?` with no export path. With the shipped
// 90-day default (`rl_client/rl_retention.py::_DEFAULT_MAX_AGE_DAYS`) that
// silently destroyed the oldest slice of the RL training corpus — one hourly
// prune at a time — starting the day the corpus crossed 90 days. Every deleted
// retrieval/citation pair is an irreplaceable training example: it carries a
// query embedding, per-node embeddings, `linked_embs`, `answer_chunk_embs`, and
// a cosine-derived label that cannot be recomputed from anything else.
//
// THE RULE (from the 2026-08-27 telemetry verification, §6): **archive = move,
// never drop**. This module makes that structural, not a convention:
// `prune_rl_events` REQUIRES an archive directory (there is no no-archive code
// path), and a failed archive ABORTS the prune with `Err` — nothing is deleted.
//
// FORMAT: gzip-compressed JSONL, one hub-ROW object per line, field-for-field
// identical to `vct-hub`'s `RlEventOut` (the JSON the offline trainer already
// consumes through `GET /api/v1/rl/events`). That makes the sidecar readable by
// `hub_event_loader._reshape_hub_row` with no new parser — the "ship the
// consumer with the signal" rule. `payload_json` is carried VERBATIM as a
// string, byte-for-byte, so no embedding is lost to re-serialization.
// `vco_lib/rl_archive.py` is the shared reader (and its `python -m` CLI).
//
// CRASH ORDERING: the archive is written to `<name>.jsonl.gz.pending`, fsynced,
// and VERIFIED (re-read + line count) BEFORE any DELETE. It is renamed to its
// final `.jsonl.gz` name only AFTER the DELETE succeeds. So a crash between the
// two leaves a `.pending` file the reader ignores while the rows are still in
// the DB — no loss, and no double-count. A crash after the DELETE but before
// the rename leaves rows that live ONLY in that `.pending` file.
//
// PENDING RECONCILE: [`Db::reconcile_pending_archives`] runs at the start of
// every prune and closes that window — for each stale `.pending` it asks the DB
// which of its row-ids survive: NONE ⇒ the crash was after the DELETE, so the
// file is the only copy and gets PROMOTED; ALL ⇒ the crash was before it, so the
// rows will be archived again and the orphan is dropped. A MIXED answer is not a
// crash shape this module can produce — the DELETE is ONE transaction (see
// [`Db::delete_rl_events_by_id`]) — so it is reported as the bug it is and the
// prune ABORTS rather than guessing which half to lose.

/// Env override for the retention archive directory (read in the process that
/// owns launcher.db — the hub). Unset → `<VCT_STATE_DIR or ~/.vct>/rl_archive`.
pub const RL_ARCHIVE_DIR_ENV: &str = "RL_EVENTS_ARCHIVE_DIR";

/// Suffix of a COMPLETE archive sidecar (readable by the loader).
pub const RL_ARCHIVE_SUFFIX: &str = ".jsonl.gz";

/// Suffix of an IN-FLIGHT archive sidecar. A file with this suffix means the
/// prune did not finish; its rows are still in the DB and the reader MUST skip
/// it (reading it would double-count those events).
pub const RL_ARCHIVE_PENDING_SUFFIX: &str = ".jsonl.gz.pending";

/// Resolve the retention archive directory: `$RL_EVENTS_ARCHIVE_DIR`, else
/// `<VCT_STATE_DIR or ~/.vct>/rl_archive` (sibling of `launcher.db`).
///
/// Callers pass the result into [`Db::prune_rl_events`]; the DB method takes
/// the directory as a REQUIRED argument so no caller can reach the DELETE
/// without naming an archive destination.
pub fn rl_archive_dir() -> PathBuf {
    if let Ok(custom) = std::env::var(RL_ARCHIVE_DIR_ENV) {
        let trimmed = custom.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    crate::paths::vct_root_dir().join("rl_archive")
}

/// One archived row, serialized exactly like `vct-hub`'s `RlEventOut`.
fn archive_line(e: &RlEvent) -> Result<String, String> {
    // `payload_json` stays a STRING (verbatim), matching the hub's GET shape
    // that `hub_event_loader._reshape_hub_row` parses with `json.loads`.
    let v = serde_json::json!({
        "id": e.id,
        "event_type": e.event_type,
        "schema_version": e.schema_version,
        "ts_ms": e.ts_ms,
        "project_id": e.project_id,
        "project_name": e.project_name,
        "task_id": e.task_id,
        "task_type": e.task_type,
        "embedding_source": e.embedding_source,
        "embedding_dim": e.embedding_dim,
        "embedding_model": e.embedding_model,
        "payload_json": e.payload_json,
        "quarantined_at": e.quarantined_at,
        "quarantine_reason": e.quarantine_reason,
    });
    serde_json::to_string(&v).map_err(|err| format!("rl archive serialize: {}", err))
}

/// Write `rows` to `<dir>/<stem><RL_ARCHIVE_PENDING_SUFFIX>`, fsync it, then
/// VERIFY by re-reading + counting decompressed lines. Returns the pending
/// path on success. Any failure returns `Err` (and removes the partial file);
/// the caller MUST then delete nothing.
fn write_pending_archive(dir: &Path, stem: &str, rows: &[RlEvent]) -> Result<PathBuf, String> {
    std::fs::create_dir_all(dir)
        .map_err(|e| format!("rl archive: create {}: {}", dir.display(), e))?;

    let pending = dir.join(format!("{}{}", stem, RL_ARCHIVE_PENDING_SUFFIX));
    let cleanup = |p: &Path| {
        let _ = std::fs::remove_file(p);
    };

    // Build the gzip stream.
    let file = std::fs::File::create(&pending)
        .map_err(|e| format!("rl archive: create {}: {}", pending.display(), e))?;
    let mut enc = flate2::write::GzEncoder::new(file, flate2::Compression::default());
    for row in rows {
        let line = match archive_line(row) {
            Ok(l) => l,
            Err(e) => {
                let _ = enc.finish();
                cleanup(&pending);
                return Err(e);
            }
        };
        if let Err(e) = enc.write_all(line.as_bytes()).and_then(|_| enc.write_all(b"\n")) {
            let _ = enc.finish();
            cleanup(&pending);
            return Err(format!("rl archive: write {}: {}", pending.display(), e));
        }
    }
    // finish() flushes the deflate trailer; sync_all() puts the bytes on the
    // platter BEFORE we are allowed to delete the rows they represent.
    let file = match enc.finish() {
        Ok(f) => f,
        Err(e) => {
            cleanup(&pending);
            return Err(format!("rl archive: finish {}: {}", pending.display(), e));
        }
    };
    if let Err(e) = file.sync_all() {
        cleanup(&pending);
        return Err(format!("rl archive: fsync {}: {}", pending.display(), e));
    }
    drop(file);

    // VERIFY: re-open, decompress, count lines. An archive we cannot read back
    // is a delete with extra steps — refuse to proceed on any mismatch.
    match verify_archive_line_count(&pending) {
        Ok(n) if n == rows.len() => Ok(pending),
        Ok(n) => {
            cleanup(&pending);
            Err(format!(
                "rl archive: verify {} — {} lines readable, {} expected",
                pending.display(),
                n,
                rows.len()
            ))
        }
        Err(e) => {
            cleanup(&pending);
            Err(e)
        }
    }
}

/// Decompress `path` and count newline-terminated records. Used as the
/// post-write verification gate (and by the archive tests).
fn verify_archive_line_count(path: &Path) -> Result<usize, String> {
    use std::io::BufRead;
    let f = std::fs::File::open(path)
        .map_err(|e| format!("rl archive: reopen {}: {}", path.display(), e))?;
    let dec = flate2::read::GzDecoder::new(f);
    let reader = std::io::BufReader::new(dec);
    let mut n = 0usize;
    for line in reader.lines() {
        let line = line.map_err(|e| format!("rl archive: decode {}: {}", path.display(), e))?;
        if !line.trim().is_empty() {
            n += 1;
        }
    }
    Ok(n)
}

/// Row ids recorded in an archive sidecar, in file order.
///
/// Used by the `.pending` reconcile to ask the DB which crash it is looking at.
/// STRICT on purpose: a line that is not JSON, or carries no integer `id`, makes
/// the whole file unreadable rather than yielding a partial id set — a partial
/// set would answer "mixed" for a file that is merely damaged, and mixed is the
/// arm that aborts retention.
fn archive_row_ids(path: &Path) -> Result<Vec<i64>, String> {
    use std::io::BufRead;
    let f = std::fs::File::open(path)
        .map_err(|e| format!("rl archive: open {}: {}", path.display(), e))?;
    let dec = flate2::read::GzDecoder::new(f);
    let reader = std::io::BufReader::new(dec);
    let mut ids = Vec::new();
    for line in reader.lines() {
        let line = line.map_err(|e| format!("rl archive: decode {}: {}", path.display(), e))?;
        if line.trim().is_empty() {
            continue;
        }
        let v: serde_json::Value = serde_json::from_str(&line)
            .map_err(|e| format!("rl archive: parse {}: {}", path.display(), e))?;
        let id = v
            .get("id")
            .and_then(|i| i.as_i64())
            .ok_or_else(|| format!("rl archive: {} has a row with no id", path.display()))?;
        ids.push(id);
    }
    Ok(ids)
}

/// The published path a `.pending` sidecar is promoted to: same stem, final
/// suffix. Built by SWAPPING the suffixes explicitly rather than trimming
/// `".pending"`, so the two constants stay the only definition of either name.
fn published_path_for_pending(pending: &Path) -> Result<PathBuf, String> {
    let name = pending
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .ok_or_else(|| format!("rl archive: {} has no file name", pending.display()))?;
    let stem = name.strip_suffix(RL_ARCHIVE_PENDING_SUFFIX).ok_or_else(|| {
        format!("rl archive: {} is not a pending sidecar", pending.display())
    })?;
    let dir = pending
        .parent()
        .ok_or_else(|| format!("rl archive: {} has no parent dir", pending.display()))?;
    Ok(dir.join(format!("{}{}", stem, RL_ARCHIVE_SUFFIX)))
}

/// Archive-file stem: sortable UTC timestamp + row count + a project tag.
/// Collision-safe within a second via the row-id range suffix.
fn archive_stem(project_id: Option<&str>, rows: &[RlEvent]) -> String {
    let ts = chrono::Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
    let scope = match project_id {
        Some(p) if !p.is_empty() => {
            let safe: String = p
                .chars()
                .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
                .collect();
            safe
        }
        _ => "all".to_string(),
    };
    let lo = rows.iter().map(|r| r.id).min().unwrap_or(0);
    let hi = rows.iter().map(|r| r.id).max().unwrap_or(0);
    format!("rl_events-{}-{}-{}-{}-{}rows", ts, scope, lo, hi, rows.len())
}

impl Db {
    /// Insert one RL event. Always appends; rl_events is never updated in
    /// place. Returns the new row's `id`.
    ///
    /// Soft-fail discipline: any DB error returns `Err(String)` to the
    /// caller. The hub handler logs + responds 5xx; the Python writer
    /// treats non-2xx as data loss (per the locked decision — no retry
    /// queue, no JSONL fallback).
    ///
    /// `ts_ms` is supplied by the caller (NOT server-side now()) because
    /// the writer captured the wall-clock at event-construction time and
    /// the hub may be reached after a buffering delay; the auth-time
    /// timestamp is what's training-relevant.
    #[allow(clippy::too_many_arguments)]
    pub fn insert_rl_event(
        &self,
        event_type: &str,
        schema_version: i64,
        ts_ms: i64,
        project_id: Option<&str>,
        project_name: Option<&str>,
        task_id: &str,
        task_type: Option<&str>,
        embedding_source: Option<&str>,
        embedding_dim: Option<i64>,
        embedding_model: Option<&str>,
        payload_json: &str,
    ) -> Result<i64, String> {
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO rl_events
                    (event_type, schema_version, ts, project_id, project_name,
                     task_id, task_type, embedding_source, embedding_dim,
                     embedding_model, payload_json)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
                params![
                    event_type,
                    schema_version,
                    ts_ms,
                    project_id,
                    project_name,
                    task_id,
                    task_type,
                    embedding_source,
                    embedding_dim,
                    embedding_model,
                    payload_json,
                ],
            )
            .map_err(|e| format!("insert rl_event: {}", e))?;
        Ok(guard.last_insert_rowid())
    }

    /// List rl_events for a project / event-type / time-range. Returns rows
    /// newest-first. Used by the launcher GUI's per-project event-rate
    /// dashboard and the offline trainer's resume-from-cursor read.
    ///
    /// All filters are optional; passing all `None` returns the most-recent
    /// `limit` rows across the whole table (use with care — the table grows
    /// linearly with retrieval traffic).
    ///
    /// RL-14 (v0.2.75): `include_quarantined = false` (the default every
    /// training-data read passes) excludes rows a marking pass flagged as
    /// poisoned (`quarantined_at IS NOT NULL`). Pass `true` only for
    /// inspection surfaces that deliberately want the full corpus.
    pub fn list_rl_events(
        &self,
        project_id: Option<&str>,
        event_type: Option<&str>,
        since_ms: Option<i64>,
        until_ms: Option<i64>,
        limit: u32,
        include_quarantined: bool,
    ) -> Result<Vec<RlEvent>, String> {
        // Build the WHERE clause + params iteratively to keep the prepared
        // statement cache-friendly across common filter combinations.
        let mut sql = String::from(
            "SELECT id, event_type, schema_version, ts, project_id, project_name,
                    task_id, task_type, embedding_source, embedding_dim,
                    embedding_model, payload_json, quarantined_at, quarantine_reason
               FROM rl_events
              WHERE 1=1",
        );
        let mut params_vec: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
        if let Some(p) = project_id {
            sql.push_str(" AND project_id = ?");
            params_vec.push(Box::new(p.to_string()));
        }
        if let Some(et) = event_type {
            sql.push_str(" AND event_type = ?");
            params_vec.push(Box::new(et.to_string()));
        }
        if let Some(s) = since_ms {
            sql.push_str(" AND ts >= ?");
            params_vec.push(Box::new(s));
        }
        if let Some(u) = until_ms {
            sql.push_str(" AND ts <= ?");
            params_vec.push(Box::new(u));
        }
        if !include_quarantined {
            sql.push_str(" AND quarantined_at IS NULL");
        }
        sql.push_str(" ORDER BY ts DESC, id DESC LIMIT ?");
        params_vec.push(Box::new(limit as i64));

        let guard = self.lock();
        let mut stmt = guard
            .prepare(&sql)
            .map_err(|e| format!("prepare list_rl_events: {}", e))?;
        let param_refs: Vec<&dyn rusqlite::ToSql> =
            params_vec.iter().map(|b| b.as_ref()).collect();
        let rows = stmt
            .query_map(param_refs.as_slice(), |row| {
                Ok(RlEvent {
                    id: row.get(0)?,
                    event_type: row.get(1)?,
                    schema_version: row.get(2)?,
                    ts_ms: row.get(3)?,
                    project_id: row.get(4)?,
                    project_name: row.get(5)?,
                    task_id: row.get(6)?,
                    task_type: row.get(7)?,
                    embedding_source: row.get(8)?,
                    embedding_dim: row.get(9)?,
                    embedding_model: row.get(10)?,
                    payload_json: row.get(11)?,
                    quarantined_at: row.get(12)?,
                    quarantine_reason: row.get(13)?,
                })
            })
            .map_err(|e| format!("query list_rl_events: {}", e))?;

        let mut out = Vec::new();
        for r in rows {
            out.push(r.map_err(|e| format!("read rl_event row: {}", e))?);
        }
        Ok(out)
    }

    /// Count rl_events for a project / event-type / time-range.
    /// Used by the launcher Identity-tab event-rate badge.
    ///
    /// RL-14 (v0.2.75): `quarantined = None` counts ALL rows (the badge's
    /// pre-RL-14 semantics, unchanged); `Some(true)` counts only quarantined
    /// rows (rl-doctor's report); `Some(false)` only clean rows.
    pub fn count_rl_events(
        &self,
        project_id: Option<&str>,
        event_type: Option<&str>,
        since_ms: Option<i64>,
        quarantined: Option<bool>,
    ) -> Result<i64, String> {
        let mut sql = String::from("SELECT COUNT(*) FROM rl_events WHERE 1=1");
        let mut params_vec: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
        if let Some(p) = project_id {
            sql.push_str(" AND project_id = ?");
            params_vec.push(Box::new(p.to_string()));
        }
        if let Some(et) = event_type {
            sql.push_str(" AND event_type = ?");
            params_vec.push(Box::new(et.to_string()));
        }
        if let Some(s) = since_ms {
            sql.push_str(" AND ts >= ?");
            params_vec.push(Box::new(s));
        }
        match quarantined {
            Some(true) => sql.push_str(" AND quarantined_at IS NOT NULL"),
            Some(false) => sql.push_str(" AND quarantined_at IS NULL"),
            None => {}
        }

        let guard = self.lock();
        let param_refs: Vec<&dyn rusqlite::ToSql> =
            params_vec.iter().map(|b| b.as_ref()).collect();
        let row: Option<i64> = guard
            .query_row(&sql, param_refs.as_slice(), |row| row.get(0))
            .optional()
            .map_err(|e| format!("count_rl_events: {}", e))?;
        Ok(row.unwrap_or(0))
    }

    /// Prune rl_events by age and/or row-cap (RL-5 retention, v0.2.73).
    ///
    /// Drives the hub's `POST /api/v1/rl/events/prune` route, which the
    /// Python retention driver (`rl_client/hub_writer.py::post_rl_prune`)
    /// calls to keep the corpus bounded. Two independent bounds, applied in
    /// a single logical pass; returns the TOTAL rows deleted across both.
    ///
    ///   * `cutoff_ms` (Some): delete rows with `ts < cutoff_ms` (age bound).
    ///   * `max_rows`  (Some): keep only the newest `max_rows` rows (by
    ///     `ts DESC, id DESC`), delete the rest (row-cap bound).
    ///   * `project_id` (Some): scope BOTH bounds to that project. A
    ///     `project_id = ?` predicate naturally excludes `project_id IS NULL`
    ///     (free-tier) rows — desired. `None` spans ALL projects (global),
    ///     which is the documented contract for an unscoped retention run.
    ///
    /// SAFETY (hard requirement): if BOTH bounds are `None` this is a no-op —
    /// it deletes nothing and returns `Ok(0)`. A prune with no age bound and
    /// no row cap must NEVER delete rows (that would wipe the corpus). The
    /// guard below returns early before any DELETE is prepared.
    ///
    /// The victim selection runs under the SAME lock guard as the DELETE so a
    /// concurrent writer cannot interleave a row between the age-selection and
    /// the row-cap-selection.
    ///
    /// R1 (v0.2.91) — **ARCHIVE-THEN-DELETE**. `archive_dir` is REQUIRED: there
    /// is deliberately no code path that deletes without naming an archive
    /// destination (`rl_archive_dir()` resolves the production default). The
    /// selected rows are written to a gzip JSONL sidecar in hub-row shape,
    /// fsynced, and read back for verification BEFORE the first DELETE runs. If
    /// ANY of that fails the method returns `Err` and **deletes nothing** — a
    /// broken archive must never become silent data loss. See the module-level
    /// "retention ARCHIVE" block for the format + crash ordering.
    ///
    /// R1 pair-integrity: a retrieval event and its citation join on `task_id`
    /// and the citation can land hours later, so selecting strictly by `ts`
    /// would split a training pair across the boundary (archive keeps one half,
    /// the DB keeps the other, and NEITHER source can produce a training
    /// sample). The victim set is therefore COMPLETED BY `task_id` GROUP: once
    /// any row of a task is selected, every row of that task — inside the same
    /// project scope — moves with it.
    ///
    /// R1 incrementality: one pass moves at most
    /// [`RL_PRUNE_MAX_TASKS_PER_PASS_DEFAULT`] task groups, OLDEST FIRST, so the
    /// archive's in-memory row set stays bounded even on the first prune of a
    /// long-neglected corpus. The hourly cadence drains the rest.
    ///
    /// Wave-4 additions, both about a pass that is not alone in the world: every
    /// pass first reconciles stale `.pending` sidecars left by a crashed
    /// predecessor (see [`Db::reconcile_pending_archives`]), and a pass whose
    /// DELETE removed NOTHING while it had victims discards its own sidecar
    /// instead of publishing a duplicate of the winner's (step 3b).
    pub fn prune_rl_events(
        &self,
        cutoff_ms: Option<i64>,
        max_rows: Option<i64>,
        project_id: Option<&str>,
        archive_dir: &Path,
    ) -> Result<u64, String> {
        // Empty/degenerate-bounds no-op guard: never delete-all. A row-cap of
        // Some(0) (or negative) is NOT a valid keep-zero-delete-all request —
        // `LIMIT 0` yields an empty keep-set so `id NOT IN ()` would wipe the
        // ENTIRE corpus (the invariant the doc above forbids). `_DEFAULT_MAX_ROWS
        // = 0` on the Python driver means "row-cap disabled"; the driver coerces
        // 0 -> None, but this method is the deletion AUTHORITY and must not
        // depend on a caller's coercion (a manual curl / rl-doctor / future
        // driver edit could pass 0). Treat max_rows <= 0 as "no row-cap bound".
        let rowcap_active = matches!(max_rows, Some(n) if n > 0);
        if cutoff_ms.is_none() && !rowcap_active {
            return Ok(0);
        }

        // ── 0. RECONCILE stale `.pending` sidecars ─────────────────────────
        //
        // Before selecting anything new, close the DELETE→rename crash window
        // of a PREVIOUS pass: promote a sidecar whose rows are gone (it is
        // their only copy), drop one whose rows are all still here, abort on a
        // mix. Runs first so a promoted file is published before this pass can
        // write a second sidecar into the same directory.
        self.reconcile_pending_archives(archive_dir)?;

        // ── 1. SELECT the victim set (both bounds) under one guard ─────────
        //
        // Both bounds resolve to a set of `task_id`s; the row set is then every
        // row of those tasks (pair-integrity — see the doc comment). Selecting
        // the ROWS (not just ids) here is what makes the archive possible: the
        // full payload_json is captured before anything is deleted.
        let cap = prune_max_tasks_per_pass();
        let cap_i = cap as i64;
        let victims: Vec<RlEvent> = {
            let guard = self.lock();

            // Both bound queries select TASK GROUPS, ordered OLDEST-FIRST by the
            // group's earliest row, and LIMITed to the per-pass cap — so a pass
            // is bounded in memory and always drains the oldest backlog first.
            let mut task_ids: Vec<String> = Vec::new();
            let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();

            // Age bound → tasks with any row older than the cutoff.
            if let Some(cutoff) = cutoff_ms {
                let (sql, p): (&str, Vec<Box<dyn rusqlite::ToSql>>) = match project_id {
                    Some(pid) => (
                        "SELECT task_id FROM rl_events
                          WHERE ts < ?1 AND project_id = ?2
                          GROUP BY task_id ORDER BY MIN(ts) ASC LIMIT ?3",
                        vec![Box::new(cutoff), Box::new(pid.to_string()), Box::new(cap_i)],
                    ),
                    None => (
                        "SELECT task_id FROM rl_events
                          WHERE ts < ?1
                          GROUP BY task_id ORDER BY MIN(ts) ASC LIMIT ?2",
                        vec![Box::new(cutoff), Box::new(cap_i)],
                    ),
                };
                collect_task_ids(&guard, sql, &p, "cutoff", &mut task_ids, &mut seen)?;
            }

            // Row-cap bound → tasks with any row outside the newest `keep`.
            // Defense-in-depth (matches the guard above): keep <= 0 is NOT a
            // keep-zero-delete-all — a LIMIT 0 keep-set would wipe the corpus.
            // Only a positive keep is a real row-cap; 0/negative = disabled.
            if let Some(keep) = max_rows.filter(|&k| k > 0) {
                let (sql, p): (&str, Vec<Box<dyn rusqlite::ToSql>>) = match project_id {
                    Some(pid) => (
                        "SELECT task_id FROM rl_events
                          WHERE project_id = ?1
                            AND id NOT IN (
                                SELECT id FROM rl_events
                                 WHERE project_id = ?1
                                 ORDER BY ts DESC, id DESC
                                 LIMIT ?2
                            )
                          GROUP BY task_id ORDER BY MIN(ts) ASC LIMIT ?3",
                        vec![Box::new(pid.to_string()), Box::new(keep), Box::new(cap_i)],
                    ),
                    None => (
                        "SELECT task_id FROM rl_events
                          WHERE id NOT IN (
                                SELECT id FROM rl_events
                                 ORDER BY ts DESC, id DESC
                                 LIMIT ?1
                            )
                          GROUP BY task_id ORDER BY MIN(ts) ASC LIMIT ?2",
                        vec![Box::new(keep), Box::new(cap_i)],
                    ),
                };
                collect_task_ids(&guard, sql, &p, "max_rows", &mut task_ids, &mut seen)?;
            }

            if task_ids.is_empty() {
                return Ok(0);
            }
            task_ids.truncate(cap);
            select_rows_for_tasks(&guard, &task_ids, project_id)?
        };

        if victims.is_empty() {
            return Ok(0);
        }

        // ── 2. ARCHIVE first — durable + verified, or abort ─────────────────
        //
        // Deliberately OUTSIDE the DB lock: gzip + fsync of a multi-MB sidecar
        // must not block the launcher's other DB users for its whole duration.
        // Correctness is unaffected because step 3 deletes by explicit row id —
        // a row inserted meanwhile is neither archived nor deleted.
        let stem = archive_stem(project_id, &victims);
        let pending = write_pending_archive(archive_dir, &stem, &victims)?;

        // ── 3. DELETE exactly the archived ids ─────────────────────────────
        let ids: Vec<i64> = victims.iter().map(|r| r.id).collect();
        let deleted = match self.delete_rl_events_by_id(&ids) {
            Ok(n) => n,
            Err(e) => {
                // Nothing (or only part) was deleted — drop the pending file so
                // a later successful prune re-archives the same rows rather
                // than leaving a half-published sidecar behind.
                let _ = std::fs::remove_file(&pending);
                return Err(e);
            }
        };

        // ── 3b. LOST-THE-RACE guard ─────────────────────────────────────────
        //
        // The DB lock is deliberately dropped for the gzip+fsync, so two prune
        // passes (two writer processes each past their own per-process hourly
        // throttle) can select the SAME victims. The loser's delete-by-id then
        // removes 0 rows — no error, because deleting an absent id is not one —
        // and publishing here would put a byte-identical second copy of every
        // one of those rows in front of the trainer. Drop our sidecar and report
        // 0: nothing is lost, the winner published exactly once.
        //
        // A PARTIAL overlap (0 < deleted < ids.len()) still publishes: the rows
        // WE deleted now exist only in this file, and losing them outright is
        // strictly worse than the raced minority being counted twice.
        if deleted == 0 {
            let _ = std::fs::remove_file(&pending);
            return Ok(0);
        }

        // ── 4. PUBLISH the archive (rename off `.pending`) ──────────────────
        // Only now are the rows gone from the DB, so only now may the reader
        // see this file (reading it earlier would double-count those events).
        let final_path = archive_dir.join(format!("{}{}", stem, RL_ARCHIVE_SUFFIX));
        std::fs::rename(&pending, &final_path).map_err(|e| {
            format!(
                "rl archive: publish {} -> {}: {}",
                pending.display(),
                final_path.display(),
                e
            )
        })?;
        // Durability of the rename itself (POSIX: fsync the directory).
        #[cfg(unix)]
        if let Ok(dir_handle) = std::fs::File::open(archive_dir) {
            let _ = dir_handle.sync_all();
        }

        Ok(deleted)
    }

    /// Delete rl_events rows by explicit id, in bounded chunks (SQLite's
    /// variable limit), inside ONE transaction. Returns the rows removed.
    ///
    /// ATOMICITY IS LOAD-BEARING here, not tidiness. [`Db::prune_rl_events`]
    /// removes the fsynced, VERIFIED `.pending` archive when this method returns
    /// `Err`, which is sound only if `Err` means NOTHING was deleted. Separately
    /// autocommitted chunks broke that: with a victim set past 400 rows (routine
    /// — the pass cap is 500 task GROUPS) chunk 1 commits, chunk 2 hits
    /// `SQLITE_BUSY` past the busy_timeout (launcher.db is shared cross-process
    /// with the launcher, which checkpoints on its own schedule), `SQLITE_FULL`
    /// — disk-full being adjacent to retention pruning — or an IO error, and the
    /// caller then destroys the only copy of the 400 rows chunk 1 already
    /// deleted. One transaction makes the delete all-or-nothing, so the caller's
    /// error arm can never remove an archive whose rows have left the table.
    ///
    /// `BEGIN IMMEDIATE` takes the write lock UP FRONT rather than upgrading a
    /// deferred transaction at the first DELETE: a busy database then fails
    /// before any chunk runs, instead of failing an upgrade mid-sequence.
    fn delete_rl_events_by_id(&self, ids: &[i64]) -> Result<u64, String> {
        if ids.is_empty() {
            return Ok(0);
        }
        let mut guard = self.lock();
        let tx = guard
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|e| format!("prune_rl_events (delete txn): {}", e))?;
        let mut deleted: u64 = 0;
        for chunk in ids.chunks(400) {
            let placeholders = std::iter::repeat("?")
                .take(chunk.len())
                .collect::<Vec<_>>()
                .join(",");
            let sql = format!("DELETE FROM rl_events WHERE id IN ({})", placeholders);
            let p: Vec<&dyn rusqlite::ToSql> =
                chunk.iter().map(|i| i as &dyn rusqlite::ToSql).collect();
            // `?` drops `tx` on the error path → rusqlite ROLLBACKs, so an
            // earlier chunk's deletions are undone before the caller sees Err.
            let n = tx
                .execute(&sql, p.as_slice())
                .map_err(|e| format!("prune_rl_events (delete): {}", e))?;
            deleted += n as u64;
        }
        tx.commit()
            .map_err(|e| format!("prune_rl_events (delete commit): {}", e))?;
        Ok(deleted)
    }

    /// How many of `ids` are still rows in `rl_events`. Chunked like the delete
    /// (SQLite's variable limit). Used by the `.pending` reconcile to tell a
    /// crash-before-DELETE from a crash-after-DELETE.
    fn count_rl_event_ids_present(&self, ids: &[i64]) -> Result<usize, String> {
        if ids.is_empty() {
            return Ok(0);
        }
        let guard = self.lock();
        let mut present = 0usize;
        for chunk in ids.chunks(400) {
            let placeholders = std::iter::repeat("?")
                .take(chunk.len())
                .collect::<Vec<_>>()
                .join(",");
            let sql = format!(
                "SELECT COUNT(*) FROM rl_events WHERE id IN ({})",
                placeholders
            );
            let p: Vec<&dyn rusqlite::ToSql> =
                chunk.iter().map(|i| i as &dyn rusqlite::ToSql).collect();
            let n: i64 = guard
                .query_row(&sql, p.as_slice(), |row| row.get(0))
                .map_err(|e| format!("rl archive: reconcile count: {}", e))?;
            present += n.max(0) as usize;
        }
        Ok(present)
    }

    /// Reconcile stale `.pending` archive sidecars before a prune selects new
    /// victims (crash-recovery for the DELETE→rename window).
    ///
    /// A `.pending` file is an archive whose publish never happened. The DB
    /// answers which crash it was, per file:
    ///
    ///   * NONE of its ids remain ⇒ the crash landed AFTER the DELETE. That file
    ///     is the only surviving copy of those training rows → PROMOTE it
    ///     (rename to `.jsonl.gz`) so the loader stops skipping it.
    ///   * ALL of its ids remain ⇒ the crash landed BEFORE the DELETE. The rows
    ///     are still in the corpus and a later pass re-archives them → DROP the
    ///     orphan (keeping it would double-count on any future promote).
    ///   * MIXED ⇒ impossible from this module (the DELETE is one transaction),
    ///     so it is a BUG, not a crash shape. Abort the prune loudly and name
    ///     the file: promoting would double-count the deleted half and dropping
    ///     would lose it, and only a human can tell which happened.
    ///
    /// An UNREADABLE pending file is left exactly where it is (neither promoted
    /// nor dropped) with a log line, and the prune continues: we cannot confirm
    /// what it holds, and both guesses can lose data — while blocking retention
    /// forever on one corrupt file would be its own outage.
    fn reconcile_pending_archives(&self, archive_dir: &Path) -> Result<(), String> {
        let entries = match std::fs::read_dir(archive_dir) {
            Ok(e) => e,
            // No archive dir yet (first prune) or unreadable — nothing to
            // reconcile. The archive WRITE below reports a real dir problem.
            Err(_) => return Ok(()),
        };
        let mut pendings: Vec<PathBuf> = entries
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                p.file_name()
                    .map(|n| n.to_string_lossy().ends_with(RL_ARCHIVE_PENDING_SUFFIX))
                    .unwrap_or(false)
            })
            .collect();
        // Deterministic order so a multi-file reconcile logs (and fails) the
        // same way twice.
        pendings.sort();

        for pending in pendings {
            let ids = match archive_row_ids(&pending) {
                Ok(ids) => ids,
                Err(e) => {
                    tracing::warn!(
                        error = %e,
                        "[vct] rl archive: leaving unreadable pending sidecar in place"
                    );
                    continue;
                }
            };
            if ids.is_empty() {
                // Carries no rows: nothing to recover, nothing to lose.
                let _ = std::fs::remove_file(&pending);
                continue;
            }
            let present = self.count_rl_event_ids_present(&ids)?;
            if present == 0 {
                let final_path = published_path_for_pending(&pending)?;
                std::fs::rename(&pending, &final_path).map_err(|e| {
                    format!(
                        "rl archive: promote {} -> {}: {}",
                        pending.display(),
                        final_path.display(),
                        e
                    )
                })?;
                #[cfg(unix)]
                if let Ok(dir_handle) = std::fs::File::open(archive_dir) {
                    let _ = dir_handle.sync_all();
                }
                tracing::info!(
                    sidecar = %final_path.display(),
                    rows = ids.len(),
                    "[vct] rl archive: promoted stale sidecar; rows recovered \
                     (their DELETE had committed before the crash)"
                );
            } else if present == ids.len() {
                std::fs::remove_file(&pending).map_err(|e| {
                    format!("rl archive: drop orphan {}: {}", pending.display(), e)
                })?;
            } else {
                return Err(format!(
                    "rl archive: {} is MIXED — {} of {} archived rows are still \
                     in rl_events. Refusing to prune: promoting it would \
                     double-count those rows in training and dropping it would \
                     lose the others. The file is readable gzip JSONL of hub \
                     rows; resolve it by hand.",
                    pending.display(),
                    present,
                    ids.len(),
                ));
            }
        }
        Ok(())
    }

    /// RL-14 (v0.2.75): one-time marking pass for the HISTORICAL poisoned
    /// class — retrieval events whose payload carries any node `score > 1.0`
    /// (unbounded hybrid-fusion scores that pre-date the v0.2.70 F-E writer
    /// clamp; `compute_unified_targets` clamped them to 1.0, silently
    /// mis-marking those nodes as max-cited in every training pass).
    ///
    /// Marks rows (`quarantined_at = now_ms`, reason
    /// `score_out_of_range`) — never deletes. IDEMPOTENT by construction:
    /// only rows with `quarantined_at IS NULL` are examined, so a re-run
    /// touches nothing already marked (and never rewrites a timestamp).
    ///
    /// Runs in Rust, not migration SQL: `payload_json` is writer-supplied
    /// TEXT the hub never JSON-validates, so a SQL `json_each` pass would
    /// hard-error the whole migration on one malformed row. Here a row that
    /// fails to parse is SKIPPED (left clean — conservative leave-alone: we
    /// only quarantine rows we can positively convict).
    ///
    /// Returns the number of rows marked.
    pub fn backfill_quarantine_out_of_range(&self, now_ms: i64) -> Result<u64, String> {
        // Collect candidate ids under one lock, then mark under another —
        // the table is append-only + the NULL filter makes the two-step
        // safe against concurrent writers (new rows are clamped at the
        // writer boundary and can't join the historical class).
        let candidates: Vec<i64> = {
            let guard = self.lock();
            let mut stmt = guard
                .prepare(
                    "SELECT id, payload_json FROM rl_events
                      WHERE event_type = 'retrieval' AND quarantined_at IS NULL",
                )
                .map_err(|e| format!("prepare quarantine scan: {}", e))?;
            let rows = stmt
                .query_map([], |row| {
                    Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
                })
                .map_err(|e| format!("query quarantine scan: {}", e))?;

            let mut ids = Vec::new();
            for r in rows {
                let (id, payload) = r.map_err(|e| format!("read quarantine row: {}", e))?;
                if payload_has_out_of_range_score(&payload) {
                    ids.push(id);
                }
            }
            ids
        };

        let mut marked: u64 = 0;
        let guard = self.lock();
        for id in candidates {
            let n = guard
                .execute(
                    "UPDATE rl_events
                        SET quarantined_at = ?1, quarantine_reason = ?2
                      WHERE id = ?3 AND quarantined_at IS NULL",
                    params![now_ms, QUARANTINE_REASON_SCORE_OUT_OF_RANGE, id],
                )
                .map_err(|e| format!("mark quarantine row {}: {}", id, e))?;
            marked += n as u64;
        }
        Ok(marked)
    }

    /// RL-14: run [`Self::backfill_quarantine_out_of_range`] exactly once
    /// per launcher.db, guarded by the `rl_events.quarantine_backfill_v1`
    /// app_state key. Soft-fail: any error logs + leaves the guard UNSET so
    /// the next open retries (the pass is idempotent either way). Called
    /// from `Db::open` (both the launcher and the hub route through it).
    pub fn run_quarantine_backfill_once(&self) {
        match self.app_state_get(QUARANTINE_BACKFILL_STATE_KEY) {
            Ok(Some(_)) => return, // already ran on this DB
            Ok(None) => {}
            Err(e) => {
                tracing::warn!(error = %e, "[launcher-db] quarantine backfill guard read failed");
                return; // no positive confirmation → do nothing (conservative)
            }
        }
        let now_ms = chrono::Utc::now().timestamp_millis();
        match self.backfill_quarantine_out_of_range(now_ms) {
            Ok(marked) => {
                if marked > 0 {
                    tracing::info!(
                        marked,
                        "[launcher-db] RL-14 quarantine backfill: marked historical \
                         out-of-range rl_events row(s)"
                    );
                }
                if let Err(e) = self.app_state_set(QUARANTINE_BACKFILL_STATE_KEY, "done") {
                    tracing::warn!(error = %e, "[launcher-db] quarantine backfill guard write failed");
                }
            }
            Err(e) => {
                tracing::warn!(
                    error = %e,
                    "[launcher-db] RL-14 quarantine backfill failed (will retry next open)"
                );
            }
        }
    }
}

/// R1: how many task GROUPS one prune pass may move, oldest-first.
///
/// The archive holds every victim row in memory before it is written, so an
/// unbounded pass on a long-neglected corpus (the first prune after months of
/// accumulation) would materialize gigabytes of embeddings at once. Bounding it
/// makes the prune INCREMENTAL instead: each hourly pass drains the oldest N
/// task groups and the next one continues. Correctness is unaffected — a prune
/// that moves a subset is a valid prune, and the remainder is simply still in
/// the table. Bounding by TASK GROUP (not rows) is what keeps retrieval↔citation
/// pairs whole. `RL_EVENTS_PRUNE_MAX_TASKS_PER_PASS` overrides for an operator
/// who wants a backlog drained faster.
const RL_PRUNE_MAX_TASKS_PER_PASS_DEFAULT: usize = 500;
pub const RL_PRUNE_MAX_TASKS_ENV: &str = "RL_EVENTS_PRUNE_MAX_TASKS_PER_PASS";

fn prune_max_tasks_per_pass() -> usize {
    std::env::var(RL_PRUNE_MAX_TASKS_ENV)
        .ok()
        .and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|n| *n > 0)
        .unwrap_or(RL_PRUNE_MAX_TASKS_PER_PASS_DEFAULT)
}

/// R1 helper: run a task-selection bound query (already ordered oldest-first and
/// LIMITed) and append its task_ids to `out`, skipping duplicates. `tag` names
/// the bound for the error message.
fn collect_task_ids(
    guard: &rusqlite::Connection,
    sql: &str,
    p: &[Box<dyn rusqlite::ToSql>],
    tag: &str,
    out: &mut Vec<String>,
    seen: &mut std::collections::HashSet<String>,
) -> Result<(), String> {
    let mut stmt = guard
        .prepare(sql)
        .map_err(|e| format!("prune_rl_events ({} select): {}", tag, e))?;
    let refs: Vec<&dyn rusqlite::ToSql> = p.iter().map(|b| b.as_ref()).collect();
    let rows = stmt
        .query_map(refs.as_slice(), |row| row.get::<_, String>(0))
        .map_err(|e| format!("prune_rl_events ({} select): {}", tag, e))?;
    for r in rows {
        let t = r.map_err(|e| format!("prune_rl_events ({} row): {}", tag, e))?;
        if seen.insert(t.clone()) {
            out.push(t);
        }
    }
    Ok(())
}

/// R1 helper: materialize EVERY row belonging to `task_ids` (within the given
/// project scope) so the archive carries whole retrieval↔citation pairs.
fn select_rows_for_tasks(
    guard: &rusqlite::Connection,
    task_ids: &[String],
    project_id: Option<&str>,
) -> Result<Vec<RlEvent>, String> {
    let mut out: Vec<RlEvent> = Vec::new();
    let all: Vec<&String> = task_ids.iter().collect();
    for chunk in all.chunks(400) {
        let placeholders = std::iter::repeat("?")
            .take(chunk.len())
            .collect::<Vec<_>>()
            .join(",");
        let mut sql = format!(
            "SELECT id, event_type, schema_version, ts, project_id, project_name,
                    task_id, task_type, embedding_source, embedding_dim,
                    embedding_model, payload_json, quarantined_at, quarantine_reason
               FROM rl_events
              WHERE task_id IN ({})",
            placeholders
        );
        let mut params_vec: Vec<Box<dyn rusqlite::ToSql>> = chunk
            .iter()
            .map(|t| Box::new((*t).clone()) as Box<dyn rusqlite::ToSql>)
            .collect();
        if let Some(pid) = project_id {
            sql.push_str(" AND project_id = ?");
            params_vec.push(Box::new(pid.to_string()));
        }
        sql.push_str(" ORDER BY id ASC");
        let mut stmt = guard
            .prepare(&sql)
            .map_err(|e| format!("prune_rl_events (victim select): {}", e))?;
        let refs: Vec<&dyn rusqlite::ToSql> = params_vec.iter().map(|b| b.as_ref()).collect();
        let rows = stmt
            .query_map(refs.as_slice(), |row| {
                Ok(RlEvent {
                    id: row.get(0)?,
                    event_type: row.get(1)?,
                    schema_version: row.get(2)?,
                    ts_ms: row.get(3)?,
                    project_id: row.get(4)?,
                    project_name: row.get(5)?,
                    task_id: row.get(6)?,
                    task_type: row.get(7)?,
                    embedding_source: row.get(8)?,
                    embedding_dim: row.get(9)?,
                    embedding_model: row.get(10)?,
                    payload_json: row.get(11)?,
                    quarantined_at: row.get(12)?,
                    quarantine_reason: row.get(13)?,
                })
            })
            .map_err(|e| format!("prune_rl_events (victim select): {}", e))?;
        for r in rows {
            out.push(r.map_err(|e| format!("prune_rl_events (victim row): {}", e))?);
        }
    }
    Ok(out)
}

/// RL-14: does this payload carry any node with `score > 1.0`?
///
/// Pure function over the raw payload text. Unparseable JSON, a missing /
/// non-array `nodes`, or non-numeric scores all return `false` — we only
/// convict on positive evidence. Scores exactly 1.0 are IN range (the F-E
/// clamp emits 1.0 legitimately).
fn payload_has_out_of_range_score(payload_json: &str) -> bool {
    let parsed: serde_json::Value = match serde_json::from_str(payload_json) {
        Ok(v) => v,
        Err(_) => return false,
    };
    let nodes = match parsed.get("nodes").and_then(|n| n.as_array()) {
        Some(a) => a,
        None => return false,
    };
    nodes.iter().any(|n| {
        n.get("score")
            .and_then(|s| s.as_f64())
            .map(|s| s > 1.0)
            .unwrap_or(false)
    })
}

#[cfg(test)]
mod tests {
    use super::super::Db;
    use super::{QUARANTINE_BACKFILL_STATE_KEY, QUARANTINE_REASON_SCORE_OUT_OF_RANGE};

    fn fresh_db() -> Db {
        // In-memory DB with all migrations applied (including migration 025
        // which creates `rl_events`).
        Db::open_in_memory().expect("in-memory db")
    }

    #[test]
    fn insert_returns_rowid() {
        let db = fresh_db();
        let id = db
            .insert_rl_event(
                "retrieval",
                3,
                1_700_000_000_000,
                None,
                Some("VCO_dev"),
                "task-abc",
                Some("mcp_interactive"),
                Some("qwen3"),
                Some(1024),
                Some("qwen3-embedding:0.6b"),
                r#"{"event":"retrieval","schema_version":3}"#,
            )
            .expect("insert");
        assert_eq!(id, 1);
    }

    #[test]
    fn list_returns_inserted_rows_newest_first() {
        let db = fresh_db();
        for i in 0..3 {
            db.insert_rl_event(
                "retrieval",
                3,
                1_700_000_000_000 + i,
                None,
                Some("VCO_dev"),
                &format!("task-{}", i),
                Some("mcp_interactive"),
                Some("qwen3"),
                Some(1024),
                Some("qwen3-embedding:0.6b"),
                r#"{"event":"retrieval"}"#,
            )
            .unwrap();
        }
        let rows = db.list_rl_events(None, None, None, None, 10, false).unwrap();
        assert_eq!(rows.len(), 3);
        // Newest-first ordering.
        assert_eq!(rows[0].task_id, "task-2");
        assert_eq!(rows[2].task_id, "task-0");
    }

    #[test]
    fn filter_by_event_type() {
        let db = fresh_db();
        db.insert_rl_event(
            "retrieval", 3, 1, None, None, "t1", None, None, None, None, "{}",
        )
        .unwrap();
        db.insert_rl_event(
            "citation", 3, 2, None, None, "t1", None, None, None, None, "{}",
        )
        .unwrap();
        let cit = db
            .list_rl_events(None, Some("citation"), None, None, 10, false)
            .unwrap();
        assert_eq!(cit.len(), 1);
        assert_eq!(cit[0].event_type, "citation");
    }

    #[test]
    fn count_matches_filter() {
        let db = fresh_db();
        for i in 0..5 {
            db.insert_rl_event(
                if i % 2 == 0 { "retrieval" } else { "citation" },
                3,
                1_000 + i,
                None,
                None,
                &format!("task-{}", i),
                None,
                None,
                None,
                None,
                "{}",
            )
            .unwrap();
        }
        assert_eq!(db.count_rl_events(None, None, None, None).unwrap(), 5);
        assert_eq!(
            db.count_rl_events(None, Some("retrieval"), None, None).unwrap(),
            3
        );
        assert_eq!(
            db.count_rl_events(None, Some("citation"), None, None).unwrap(),
            2
        );
    }

    #[test]
    fn since_ms_filter_excludes_older_rows() {
        let db = fresh_db();
        db.insert_rl_event(
            "retrieval", 3, 100, None, None, "t1", None, None, None, None, "{}",
        )
        .unwrap();
        db.insert_rl_event(
            "retrieval", 3, 200, None, None, "t2", None, None, None, None, "{}",
        )
        .unwrap();
        db.insert_rl_event(
            "retrieval", 3, 300, None, None, "t3", None, None, None, None, "{}",
        )
        .unwrap();
        let recent = db
            .list_rl_events(None, None, Some(150), None, 10, false)
            .unwrap();
        assert_eq!(recent.len(), 2);
        let count = db.count_rl_events(None, None, Some(150), None).unwrap();
        assert_eq!(count, 2);
    }

    /// Helper: insert one event with explicit ts + optional project_id.
    fn insert_at(db: &Db, ts: i64, project_id: Option<&str>, task: &str) -> i64 {
        db.insert_rl_event(
            "retrieval", 3, ts, project_id, None, task, None, None, None, None, "{}",
        )
        .unwrap()
    }

    /// Helper: seed a `projects` row so an rl_events insert with that
    /// `project_id` satisfies the FK constraint (project_id → projects.id).
    fn seed_project(db: &Db, id: &str) {
        use crate::db::models::ProjectHost;
        db.insert_project(
            id,
            &format!("Project {id}"),
            &format!("/tmp/project-{id}"),
            ProjectHost::Base,
            &format!("project-{id}"),
        )
        .expect("insert project");
    }

    /// R1 (v0.2.91): every prune test writes its archive into a fresh temp
    /// dir. NEVER the resolved production `rl_archive` dir — a test run must
    /// not deposit sidecars into the user's real `~/.vct`.
    fn tmp_archive() -> tempfile::TempDir {
        tempfile::tempdir().expect("temp archive dir")
    }

    /// Count the rows the archive sidecars in `dir` hold (published files only;
    /// `.pending` files are deliberately skipped — see RL_ARCHIVE_PENDING_SUFFIX).
    fn archived_rows(dir: &std::path::Path) -> Vec<serde_json::Value> {
        use std::io::BufRead;
        let mut out = Vec::new();
        let entries = match std::fs::read_dir(dir) {
            Ok(e) => e,
            Err(_) => return out,
        };
        for e in entries.flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            if !name.ends_with(super::RL_ARCHIVE_SUFFIX) {
                continue;
            }
            let f = std::fs::File::open(e.path()).expect("open archive");
            let dec = flate2::read::GzDecoder::new(f);
            for line in std::io::BufReader::new(dec).lines() {
                let line = line.expect("decode archive line");
                if line.trim().is_empty() {
                    continue;
                }
                out.push(serde_json::from_str(&line).expect("archive line is JSON"));
            }
        }
        out
    }

    #[test]
    fn prune_cutoff_deletes_older_keeps_newer() {
        let db = fresh_db();
        insert_at(&db, 100, None, "old-1");
        insert_at(&db, 150, None, "old-2");
        insert_at(&db, 200, None, "new-1");
        insert_at(&db, 300, None, "new-2");
        // Cutoff 200 → delete ts < 200 (the two ts=100,150 rows).
        let arch = tmp_archive();
        let deleted = db.prune_rl_events(Some(200), None, None, arch.path()).unwrap();
        assert_eq!(deleted, 2);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 2);
        // Boundary row ts==200 is retained (strict <).
        assert!(rows.iter().all(|r| r.ts_ms >= 200));
    }

    #[test]
    fn prune_max_rows_keeps_newest_globally() {
        let db = fresh_db();
        for i in 0..5 {
            insert_at(&db, 1_000 + i, None, &format!("t-{}", i));
        }
        // Keep the newest 2 rows → delete the other 3.
        let arch = tmp_archive();
        let deleted = db.prune_rl_events(None, Some(2), None, arch.path()).unwrap();
        assert_eq!(deleted, 3);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 2);
        // Newest kept: ts 1004 and 1003.
        assert_eq!(rows[0].task_id, "t-4");
        assert_eq!(rows[1].task_id, "t-3");
    }

    #[test]
    fn prune_project_scoping_leaves_other_projects_untouched() {
        let db = fresh_db();
        seed_project(&db, "proj-a");
        seed_project(&db, "proj-b");
        insert_at(&db, 100, Some("proj-a"), "a-old");
        insert_at(&db, 300, Some("proj-a"), "a-new");
        insert_at(&db, 100, Some("proj-b"), "b-old");
        insert_at(&db, 300, Some("proj-b"), "b-new");
        // Prune proj-a older-than-200 only.
        let arch = tmp_archive();
        let deleted = db.prune_rl_events(Some(200), None, Some("proj-a"), arch.path()).unwrap();
        assert_eq!(deleted, 1);
        // proj-a lost its old row; proj-b fully intact.
        let a = db
            .list_rl_events(Some("proj-a"), None, None, None, 100, false)
            .unwrap();
        assert_eq!(a.len(), 1);
        assert_eq!(a[0].task_id, "a-new");
        let b = db
            .list_rl_events(Some("proj-b"), None, None, None, 100, false)
            .unwrap();
        assert_eq!(b.len(), 2);
    }

    #[test]
    fn prune_project_scoping_excludes_null_project_rows() {
        let db = fresh_db();
        seed_project(&db, "proj-a");
        insert_at(&db, 100, Some("proj-a"), "a-old");
        insert_at(&db, 100, None, "null-old");
        // Scoped prune of proj-a must NOT touch the NULL-project row.
        let arch = tmp_archive();
        let deleted = db.prune_rl_events(Some(200), None, Some("proj-a"), arch.path()).unwrap();
        assert_eq!(deleted, 1);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].task_id, "null-old");
        assert!(rows[0].project_id.is_none());
    }

    #[test]
    fn prune_both_bounds_together() {
        let db = fresh_db();
        // ts: 100,150 (old), 200..=204 (newer). Cutoff 200 removes 2 old.
        insert_at(&db, 100, None, "old-1");
        insert_at(&db, 150, None, "old-2");
        for i in 0..5 {
            insert_at(&db, 200 + i, None, &format!("keep-{}", i));
        }
        // Cutoff 200 deletes the 2 old rows; then max_rows=3 keeps newest 3
        // of the surviving 5 → deletes 2 more. Total 4.
        let arch = tmp_archive();
        let deleted = db.prune_rl_events(Some(200), Some(3), None, arch.path()).unwrap();
        assert_eq!(deleted, 4);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 3);
        assert_eq!(rows[0].task_id, "keep-4");
        assert_eq!(rows[2].task_id, "keep-2");
    }

    #[test]
    fn prune_both_none_is_noop_returns_zero() {
        // The critical safety test: no cutoff + no max_rows must delete NOTHING.
        let db = fresh_db();
        for i in 0..4 {
            insert_at(&db, 1_000 + i, None, &format!("t-{}", i));
        }
        let arch = tmp_archive();
        let deleted = db.prune_rl_events(None, None, None, arch.path()).unwrap();
        assert_eq!(deleted, 0);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 4, "no-op prune must leave all rows intact");
    }

    #[test]
    fn prune_max_rows_zero_is_noop_not_corpus_wipe() {
        // v0.2.73 Stage-1 correctness SEV-2 #1: max_rows=Some(0) must be a NO-OP,
        // NOT a "keep zero, delete all". LIMIT 0 -> empty keep-set -> id NOT IN ()
        // would wipe the ENTIRE corpus. _DEFAULT_MAX_ROWS=0 ("disabled") makes 0
        // a live expected value; the deletion authority must not delete-all on it.
        let db = fresh_db();
        for i in 0..5 {
            insert_at(&db, 2_000 + i, None, &format!("z-{}", i));
        }
        let arch = tmp_archive();
        let deleted = db.prune_rl_events(None, Some(0), None, arch.path()).unwrap();
        assert_eq!(deleted, 0, "max_rows=0 must delete NOTHING (row-cap disabled)");
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 5, "max_rows=0 must leave the whole corpus intact");
    }

    #[test]
    fn prune_max_rows_negative_is_noop() {
        // Same guard, negative row-cap: never delete-all.
        let db = fresh_db();
        for i in 0..3 {
            insert_at(&db, 3_000 + i, None, &format!("n-{}", i));
        }
        let arch = tmp_archive();
        let deleted = db.prune_rl_events(None, Some(-1), None, arch.path()).unwrap();
        assert_eq!(deleted, 0);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 3);
    }

    #[test]
    fn prune_max_rows_larger_than_count_deletes_nothing() {
        let db = fresh_db();
        for i in 0..3 {
            insert_at(&db, 1_000 + i, None, &format!("t-{}", i));
        }
        // Keep 100 but only 3 exist → nothing to delete.
        let arch = tmp_archive();
        let deleted = db.prune_rl_events(None, Some(100), None, arch.path()).unwrap();
        assert_eq!(deleted, 0);
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 3);
    }

    // ── R1 (v0.2.91): archive-then-delete — BOTH SIDES ─────────────────────

    /// SIDE A (the act): a prune that deletes rows must FIRST have written a
    /// published archive sidecar containing exactly those rows, with
    /// `payload_json` carried VERBATIM. RED-PROOF: fails on the pre-R1 source,
    /// which ran a bare DELETE and produced no file at all.
    #[test]
    fn prune_archives_rows_before_deleting_them() {
        let db = fresh_db();
        let arch = tmp_archive();
        // A payload with real training content — the archive must carry it
        // byte-for-byte (embeddings are unrecoverable if re-serialized lossily).
        let payload = r#"{"event":"retrieval","schema_version":3,"query_emb":[0.5,-0.25],"nodes":[{"title":"N","emb":[1.0,2.0]}]}"#;
        db.insert_rl_event(
            "retrieval", 3, 100, None, Some("VCO_dev"), "old-task",
            Some("pre_edit_kg_search"), Some("qwen3"), Some(1024),
            Some("qwen3-embedding:0.6b"), payload,
        )
        .unwrap();
        insert_at(&db, 500, None, "fresh-task");

        let deleted = db.prune_rl_events(Some(200), None, None, arch.path()).unwrap();
        assert_eq!(deleted, 1);

        let archived = archived_rows(arch.path());
        assert_eq!(archived.len(), 1, "the deleted row must be in the archive");
        let row = &archived[0];
        // Hub-row shape (what hub_event_loader._reshape_hub_row consumes).
        assert_eq!(row["event_type"], "retrieval");
        assert_eq!(row["ts_ms"], 100);
        assert_eq!(row["task_id"], "old-task");
        assert_eq!(row["task_type"], "pre_edit_kg_search");
        assert_eq!(row["embedding_source"], "qwen3");
        assert_eq!(row["embedding_dim"], 1024);
        assert_eq!(row["embedding_model"], "qwen3-embedding:0.6b");
        assert_eq!(row["schema_version"], 3);
        assert!(row["id"].is_i64(), "archive must carry the row id");
        // payload_json VERBATIM (string, not re-encoded object).
        assert_eq!(
            row["payload_json"].as_str().expect("payload_json is a string"),
            payload,
            "payload_json must be archived byte-for-byte"
        );

        // The un-pruned row is still in the table and NOT in the archive.
        let left = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(left.len(), 1);
        assert_eq!(left[0].task_id, "fresh-task");
    }

    /// SIDE B (the leave-alone): if the archive cannot be written, the prune
    /// ABORTS — it returns Err and the corpus is left completely intact. The
    /// unwritable destination here is a PLAIN FILE where the archive dir should
    /// be, so `create_dir_all` fails.
    #[test]
    fn failed_archive_aborts_prune_and_deletes_nothing() {
        let db = fresh_db();
        for i in 0..4 {
            insert_at(&db, 100 + i, None, &format!("old-{}", i));
        }
        let holder = tmp_archive();
        let blocked = holder.path().join("not-a-dir");
        std::fs::write(&blocked, b"i am a file, not a directory").unwrap();

        let res = db.prune_rl_events(Some(1_000), None, None, &blocked);
        assert!(res.is_err(), "a failed archive must abort the prune: {:?}", res);
        let msg = res.unwrap_err();
        assert!(
            msg.contains("rl archive"),
            "error must name the archive stage, got: {msg}"
        );

        // The whole corpus survives — this is the invariant the pre-R1 bare
        // DELETE could not offer.
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(rows.len(), 4, "a failed archive must delete NOTHING");
    }

    /// Archive sidecars in `dir` whose name ends with `suffix`, sorted. The two
    /// suffixes never collide: `.jsonl.gz.pending` does not end in `.jsonl.gz`.
    fn files_with_suffix(dir: &std::path::Path, suffix: &str) -> Vec<std::path::PathBuf> {
        let mut out: Vec<std::path::PathBuf> = std::fs::read_dir(dir)
            .map(|entries| {
                entries
                    .flatten()
                    .map(|e| e.path())
                    .filter(|p| {
                        p.file_name()
                            .map(|n| n.to_string_lossy().ends_with(suffix))
                            .unwrap_or(false)
                    })
                    .collect()
            })
            .unwrap_or_default();
        out.sort();
        out
    }

    fn published_files(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
        files_with_suffix(dir, super::RL_ARCHIVE_SUFFIX)
    }

    fn pending_files(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
        files_with_suffix(dir, super::RL_ARCHIVE_PENDING_SUFFIX)
    }

    /// Seed `n` rows in `n` distinct task groups (ids 1..=n), so a prune's
    /// delete spans more than one 400-id chunk while staying under the
    /// 500-group per-pass cap.
    fn seed_two_chunks(db: &Db) -> i64 {
        for i in 0..450 {
            insert_at(db, 100 + i, None, &format!("task-{}", i));
        }
        450
    }

    /// MAJOR-1 (wave-4). A DELETE that fails PART-WAY must roll the whole delete
    /// back, because `prune_rl_events`'s error arm removes the fsynced, VERIFIED
    /// `.pending` archive. With separately autocommitted chunks that arm
    /// destroyed the only copy of every row an earlier chunk had already
    /// deleted — permanent loss of exactly the class R1 exists to protect, on
    /// R1's own error path.
    ///
    /// The mid-sequence failure is injected with a BEFORE-DELETE trigger that
    /// `RAISE(ABORT)`s on one id in the SECOND chunk: chunk 1 (400 rows) runs,
    /// chunk 2 aborts. That is the same shape as SQLITE_BUSY past the
    /// busy_timeout / SQLITE_FULL / an IO error, made deterministic.
    ///
    /// RED-PROOF: against the pre-fix autocommitted body chunk 1's 400 deletions
    /// commit, only 50 rows are left, and the count assertion below fails.
    #[test]
    fn delete_failing_on_a_later_chunk_rolls_the_whole_delete_back() {
        let db = fresh_db();
        let arch = tmp_archive();
        let total = seed_two_chunks(&db);
        db.lock()
            .execute_batch(
                "CREATE TRIGGER rl_events_chunk2_boom BEFORE DELETE ON rl_events
                     WHEN OLD.id = 420
                   BEGIN
                     SELECT RAISE(ABORT, 'simulated chunk-2 delete failure');
                   END;",
            )
            .expect("install the mid-sequence failure trigger");

        let res = db.prune_rl_events(Some(1_000_000), None, None, arch.path());
        assert!(res.is_err(), "a failing chunk must fail the prune: {:?}", res);

        assert_eq!(
            db.count_rl_events(None, None, None, None).unwrap(),
            total,
            "a partial delete must roll back: rows removed by an earlier chunk \
             exist NOWHERE once the caller drops the pending archive",
        );
        // Nothing published either — no reader may see a half-told story.
        assert!(
            published_files(arch.path()).is_empty(),
            "a failed prune must publish no sidecar, found {:?}",
            published_files(arch.path()),
        );
    }

    /// SIDE B of MAJOR-1: the same >400-row delete with nothing failing removes
    /// EVERY row and publishes ONE sidecar carrying all of them. This pins the
    /// COMMIT — a transaction opened and never committed would roll back here
    /// and silently leave the corpus untouched while reporting success.
    #[test]
    fn multi_chunk_delete_commits_every_chunk() {
        let db = fresh_db();
        let arch = tmp_archive();
        let total = seed_two_chunks(&db);

        let deleted = db
            .prune_rl_events(Some(1_000_000), None, None, arch.path())
            .unwrap();
        assert_eq!(deleted as i64, total, "both chunks must delete");
        assert_eq!(
            db.count_rl_events(None, None, None, None).unwrap(),
            0,
            "the reported count must match what actually left the table",
        );
        assert_eq!(archived_rows(arch.path()).len() as i64, total);
        assert_eq!(published_files(arch.path()).len(), 1);
    }

    /// MINOR-2 (wave-4). A pass whose DELETE removed NOTHING while it HAD
    /// victims lost a race: another prune already archived and deleted exactly
    /// these rows (the DB lock is deliberately dropped for the gzip+fsync).
    /// Publishing our sidecar too would hand the trainer a byte-identical second
    /// copy of every row in it.
    ///
    /// The race is simulated deterministically with a BEFORE-DELETE trigger that
    /// `RAISE(IGNORE)`s: SQLite abandons the row silently, so the DELETE
    /// succeeds and reports 0 changes — precisely what the loser observes.
    ///
    /// RED-PROOF: pre-fix this published a full sidecar and returned its row
    /// count, so both the `published_files` and the `Ok(0)` assertions fail.
    #[test]
    fn losing_the_prune_race_drops_the_sidecar_instead_of_publishing() {
        let db = fresh_db();
        let arch = tmp_archive();
        for i in 0..4 {
            insert_at(&db, 100 + i, None, &format!("raced-{}", i));
        }
        db.lock()
            .execute_batch(
                "CREATE TRIGGER rl_events_race BEFORE DELETE ON rl_events
                   BEGIN SELECT RAISE(IGNORE); END;",
            )
            .expect("install the lost-race trigger");

        let deleted = db.prune_rl_events(Some(1_000), None, None, arch.path()).unwrap();
        assert_eq!(deleted, 0, "the loser deletes nothing");
        assert!(
            published_files(arch.path()).is_empty(),
            "the loser must NOT publish — the winner already archived these rows",
        );
        assert!(
            pending_files(arch.path()).is_empty(),
            "and it must not leave its own `.pending` behind either",
        );

        // ACT side: with the race gone, the very same pass publishes normally.
        db.lock()
            .execute_batch("DROP TRIGGER rl_events_race")
            .expect("drop the trigger");
        let deleted = db.prune_rl_events(Some(1_000), None, None, arch.path()).unwrap();
        assert_eq!(deleted, 4);
        assert_eq!(published_files(arch.path()).len(), 1);
        assert_eq!(archived_rows(arch.path()).len(), 4);
    }

    /// MINOR-1 arm 1 (wave-4). A crash AFTER the DELETE committed but BEFORE the
    /// rename leaves rows that live only in a `.pending` file the reader skips
    /// forever. The next prune's reconcile finds none of its ids in the table
    /// and PROMOTES it — the recovery that was documented but not implemented.
    ///
    /// RED-PROOF: without the reconcile the file stays `.pending` forever and
    /// both assertions below fail.
    #[test]
    fn stale_pending_whose_rows_are_gone_is_promoted() {
        let db = fresh_db();
        let arch = tmp_archive();
        for i in 0..3 {
            insert_at(&db, 100 + i, None, &format!("gone-{}", i));
        }
        // Produce a REAL archive of rows that no longer exist, then rename it
        // back to `.pending` — byte-for-byte the state the crash window leaves.
        assert_eq!(
            db.prune_rl_events(Some(1_000), None, None, arch.path()).unwrap(),
            3
        );
        let published = published_files(arch.path());
        assert_eq!(published.len(), 1);
        let stem = published[0]
            .file_name()
            .unwrap()
            .to_string_lossy()
            .strip_suffix(super::RL_ARCHIVE_SUFFIX)
            .expect("published name ends with the archive suffix")
            .to_string();
        let crashed = arch
            .path()
            .join(format!("{}{}", stem, super::RL_ARCHIVE_PENDING_SUFFIX));
        std::fs::rename(&published[0], &crashed).unwrap();
        assert!(published_files(arch.path()).is_empty());

        // Any later prune reconciles first — even one that selects no victims.
        insert_at(&db, 9_000, None, "fresh");
        assert_eq!(
            db.prune_rl_events(Some(200), None, None, arch.path()).unwrap(),
            0
        );

        assert!(
            pending_files(arch.path()).is_empty(),
            "the stale sidecar must be promoted, not left for the reader to skip",
        );
        assert_eq!(
            archived_rows(arch.path()).len(),
            3,
            "and its rows must now be readable by the loader",
        );
    }

    /// MINOR-1 arm 2. A crash BEFORE the DELETE leaves a `.pending` whose rows
    /// are all still in the corpus. Promoting it would double-count them the
    /// next time they are archived for real, so the orphan is DROPPED and the
    /// rows are left alone.
    #[test]
    fn stale_pending_whose_rows_all_remain_is_dropped() {
        let db = fresh_db();
        let arch = tmp_archive();
        for i in 0..3 {
            insert_at(&db, 100 + i, None, &format!("live-{}", i));
        }
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        let pending =
            super::write_pending_archive(arch.path(), "rl_events-crashed-early", &rows)
                .expect("write the stale pending");
        assert!(pending.exists());

        assert_eq!(
            db.prune_rl_events(Some(50), None, None, arch.path()).unwrap(),
            0
        );

        assert!(!pending.exists(), "an orphan whose rows all remain must be dropped");
        assert!(
            published_files(arch.path()).is_empty(),
            "and never promoted — that is the double-count",
        );
        assert_eq!(
            db.count_rl_events(None, None, None, None).unwrap(),
            3,
            "reconcile never touches rows",
        );
    }

    /// MINOR-1 arm 3. A MIXED pending (some ids gone, some present) is not a
    /// crash shape this module can produce — the DELETE is one transaction — so
    /// it is a BUG. Abort the prune, name the file, and touch NOTHING: promoting
    /// double-counts the deleted half, dropping loses it, and only a human can
    /// tell which happened.
    #[test]
    fn mixed_pending_aborts_the_prune_and_is_left_untouched() {
        let db = fresh_db();
        let arch = tmp_archive();
        for i in 0..4 {
            insert_at(&db, 100 + i, None, &format!("mix-{}", i));
        }
        let rows = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        let pending = super::write_pending_archive(arch.path(), "rl_events-mixed", &rows)
            .expect("write the stale pending");
        // Half of the archived rows leave the table — the state only a partial
        // (pre-MAJOR-1) delete could produce.
        db.lock()
            .execute("DELETE FROM rl_events WHERE id IN (1,2)", [])
            .unwrap();

        let msg = db
            .prune_rl_events(Some(1_000), None, None, arch.path())
            .expect_err("a mixed pending must abort the prune");
        assert!(msg.contains("MIXED"), "error must name the class, got: {msg}");
        assert!(
            msg.contains("rl_events-mixed"),
            "error must name the file, got: {msg}"
        );

        assert!(pending.exists(), "left for a human: neither promoted nor dropped");
        assert!(published_files(arch.path()).is_empty());
        assert_eq!(
            db.count_rl_events(None, None, None, None).unwrap(),
            2,
            "the abort happens BEFORE any new deletion",
        );
    }

    /// MINOR-1, the unreadable arm. A corrupt `.pending` cannot be classified,
    /// and both guesses can lose data — so it is left exactly where it is. It
    /// must NOT block retention forever either; that would be its own outage.
    #[test]
    fn unreadable_pending_is_left_alone_and_does_not_block_retention() {
        let db = fresh_db();
        let arch = tmp_archive();
        let junk = arch
            .path()
            .join(format!("rl_events-corrupt{}", super::RL_ARCHIVE_PENDING_SUFFIX));
        std::fs::write(&junk, b"not gzip at all").unwrap();
        for i in 0..2 {
            insert_at(&db, 100 + i, None, &format!("t-{}", i));
        }

        let deleted = db.prune_rl_events(Some(1_000), None, None, arch.path()).unwrap();
        assert_eq!(deleted, 2, "one corrupt sidecar must not stop retention");
        assert!(
            junk.exists(),
            "an unreadable pending is never guessed at — it stays put",
        );
        assert_eq!(archived_rows(arch.path()).len(), 2);
    }

    /// A retrieval and its citation join on `task_id` and the citation can land
    /// hours after the retrieval. Selecting strictly by `ts` would archive the
    /// retrieval and leave the citation in the DB — breaking the pair in BOTH
    /// places. The victim set is completed by task_id group.
    #[test]
    fn prune_keeps_retrieval_citation_pairs_together() {
        let db = fresh_db();
        let arch = tmp_archive();
        // Retrieval at ts=100 (past the cutoff), its citation at ts=900 (inside
        // the retained window) — same task_id.
        db.insert_rl_event(
            "retrieval", 3, 100, None, None, "pair-1", None, None, None, None, "{}",
        )
        .unwrap();
        db.insert_rl_event(
            "citation", 3, 900, None, None, "pair-1", None, None, None, None, "{}",
        )
        .unwrap();
        // An unrelated recent task that must NOT move.
        insert_at(&db, 950, None, "recent-task");

        let deleted = db.prune_rl_events(Some(500), None, None, arch.path()).unwrap();
        assert_eq!(deleted, 2, "both halves of the pair must be pruned together");

        let archived = archived_rows(arch.path());
        assert_eq!(archived.len(), 2);
        assert!(archived.iter().all(|r| r["task_id"] == "pair-1"));
        assert!(archived.iter().any(|r| r["event_type"] == "retrieval"));
        assert!(archived.iter().any(|r| r["event_type"] == "citation"));

        let left = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(left.len(), 1);
        assert_eq!(left[0].task_id, "recent-task");
    }

    /// A no-op prune (both bounds absent / degenerate) must not create a file.
    /// Otherwise the hourly cadence would litter the archive dir with empty
    /// sidecars forever.
    #[test]
    fn noop_prune_writes_no_archive_file() {
        let db = fresh_db();
        for i in 0..3 {
            insert_at(&db, 1_000 + i, None, &format!("t-{}", i));
        }
        let arch = tmp_archive();
        assert_eq!(db.prune_rl_events(None, None, None, arch.path()).unwrap(), 0);
        assert_eq!(db.prune_rl_events(None, Some(0), None, arch.path()).unwrap(), 0);
        // Nothing matched → nothing written (not even a `.pending`).
        let entries: Vec<_> = std::fs::read_dir(arch.path())
            .map(|e| e.flatten().map(|x| x.path()).collect())
            .unwrap_or_default();
        assert!(entries.is_empty(), "no-op prune wrote {:?}", entries);
    }

    /// The SERVING path is untouched by R1: reads/counts/inserts keep their
    /// exact pre-R1 behaviour, and an archive sidecar never leaks into them.
    #[test]
    fn serving_path_unchanged_by_archive() {
        let db = fresh_db();
        let arch = tmp_archive();
        for i in 0..5 {
            insert_at(&db, 100 + i, None, &format!("t-{}", i));
        }
        let before_list = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        let before_count = db.count_rl_events(None, None, None, None).unwrap();
        assert_eq!(before_list.len(), 5);
        assert_eq!(before_count, 5);

        // Prune the two oldest, then re-read: list/count reflect ONLY the
        // deletion — the archive is not a second source the readers can see.
        let deleted = db.prune_rl_events(Some(102), None, None, arch.path()).unwrap();
        assert_eq!(deleted, 2);
        assert_eq!(db.count_rl_events(None, None, None, None).unwrap(), 3);
        let after = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(after.len(), 3);
        assert_eq!(after[0].task_id, "t-4", "newest-first ordering preserved");
        // Inserts still append normally after a prune.
        insert_at(&db, 500, None, "post-prune");
        assert_eq!(db.count_rl_events(None, None, None, None).unwrap(), 4);
    }

    /// One pass moves at most `RL_PRUNE_MAX_TASKS_PER_PASS` task groups, OLDEST
    /// FIRST, so the archive's in-memory row set stays bounded on the first
    /// prune of a long-neglected corpus. Successive passes drain the rest.
    #[test]
    fn prune_is_incremental_and_drains_oldest_first() {
        let db = fresh_db();
        let arch = tmp_archive();
        let key = super::RL_PRUNE_MAX_TASKS_ENV;
        let prev = std::env::var(key).ok();
        std::env::set_var(key, "2");

        for i in 0..5 {
            insert_at(&db, 100 + i, None, &format!("task-{}", i));
        }
        // Cutoff past every row: without the cap this would take all 5 at once.
        let first = db.prune_rl_events(Some(1_000), None, None, arch.path()).unwrap();
        assert_eq!(first, 2, "one pass must move at most the capped task count");
        let left = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        let names: Vec<&str> = left.iter().map(|r| r.task_id.as_str()).collect();
        assert!(
            !names.contains(&"task-0") && !names.contains(&"task-1"),
            "the OLDEST groups must go first, left={names:?}"
        );

        // Successive passes drain the remainder.
        let mut total = first;
        for _ in 0..4 {
            total += db.prune_rl_events(Some(1_000), None, None, arch.path()).unwrap();
        }
        assert_eq!(total, 5);
        assert!(db.list_rl_events(None, None, None, None, 100, false).unwrap().is_empty());
        // Every row landed in an archive across the passes.
        assert_eq!(archived_rows(arch.path()).len(), 5);

        match prev {
            Some(v) => std::env::set_var(key, v),
            None => std::env::remove_var(key),
        }
    }

    /// `rl_archive_dir()` honours the env override and otherwise sits beside
    /// launcher.db in the resolved state dir.
    #[test]
    fn archive_dir_env_override_and_default() {
        let key = super::RL_ARCHIVE_DIR_ENV;
        let prev = std::env::var(key).ok();
        std::env::set_var(key, "/tmp/vco-test-archive-dir");
        assert_eq!(
            super::rl_archive_dir(),
            std::path::PathBuf::from("/tmp/vco-test-archive-dir")
        );
        // Empty/whitespace is NOT a valid override — fall back to the default.
        std::env::set_var(key, "   ");
        assert_eq!(
            super::rl_archive_dir(),
            crate::paths::vct_root_dir().join("rl_archive")
        );
        std::env::remove_var(key);
        assert_eq!(
            super::rl_archive_dir(),
            crate::paths::vct_root_dir().join("rl_archive"),
            "default archive dir is a sibling of launcher.db"
        );
        if let Some(v) = prev {
            std::env::set_var(key, v);
        }
    }

    #[test]
    fn nullable_project_id_round_trips() {
        let db = fresh_db();
        let id = db
            .insert_rl_event(
                "retrieval",
                3,
                1,
                None, // free-tier: no project_id
                Some("workspace-slug"),
                "task-free",
                None,
                None,
                None,
                None,
                "{}",
            )
            .unwrap();
        let rows = db.list_rl_events(None, None, None, None, 10, false).unwrap();
        assert_eq!(rows[0].id, id);
        assert!(rows[0].project_id.is_none());
        assert_eq!(rows[0].project_name.as_deref(), Some("workspace-slug"));
    }

    // ─── RL-14 (v0.2.75): quarantine marker ─────────────────────────────

    /// Helper: insert one event with an explicit payload.
    fn insert_payload(db: &Db, event_type: &str, task: &str, payload: &str) -> i64 {
        db.insert_rl_event(
            event_type, 3, 1_000, None, None, task, None, None, None, None, payload,
        )
        .unwrap()
    }

    const POISONED: &str = r#"{"nodes":[{"title":"A","score":10.37},{"title":"B","score":0.4}]}"#;
    const CLEAN: &str = r#"{"nodes":[{"title":"A","score":0.91},{"title":"B","score":1.0}]}"#;

    #[test]
    fn backfill_marks_out_of_range_and_leaves_in_range_alone() {
        let db = fresh_db();
        insert_payload(&db, "retrieval", "poisoned", POISONED);
        insert_payload(&db, "retrieval", "clean", CLEAN);
        // score exactly 1.0 is IN range (the F-E clamp emits it legitimately).
        insert_payload(&db, "retrieval", "boundary", r#"{"nodes":[{"score":1.0}]}"#);
        // Malformed payload: never convicted (skip softly).
        insert_payload(&db, "retrieval", "malformed", "not json {");
        // Citation events carry no nodes[].score contract → never scanned.
        insert_payload(&db, "citation", "citation", POISONED);

        let marked = db.backfill_quarantine_out_of_range(9_999).unwrap();
        assert_eq!(marked, 1, "exactly the poisoned retrieval row is marked");

        let all = db.list_rl_events(None, None, None, None, 100, true).unwrap();
        let poisoned = all.iter().find(|r| r.task_id == "poisoned").unwrap();
        assert_eq!(poisoned.quarantined_at, Some(9_999));
        assert_eq!(
            poisoned.quarantine_reason.as_deref(),
            Some(QUARANTINE_REASON_SCORE_OUT_OF_RANGE)
        );
        for task in ["clean", "boundary", "malformed", "citation"] {
            let row = all.iter().find(|r| r.task_id == task).unwrap();
            assert!(
                row.quarantined_at.is_none(),
                "{} must be left alone",
                task
            );
        }
    }

    #[test]
    fn backfill_is_idempotent() {
        let db = fresh_db();
        insert_payload(&db, "retrieval", "poisoned", POISONED);
        assert_eq!(db.backfill_quarantine_out_of_range(1_111).unwrap(), 1);
        // Second pass: nothing new to mark, timestamp NOT rewritten.
        assert_eq!(db.backfill_quarantine_out_of_range(2_222).unwrap(), 0);
        let rows = db.list_rl_events(None, None, None, None, 10, true).unwrap();
        assert_eq!(rows[0].quarantined_at, Some(1_111), "first mark timestamp survives");
    }

    #[test]
    fn quarantined_rows_excluded_from_corpus_reads_by_default() {
        let db = fresh_db();
        insert_payload(&db, "retrieval", "poisoned", POISONED);
        insert_payload(&db, "retrieval", "clean", CLEAN);
        db.backfill_quarantine_out_of_range(5_000).unwrap();

        // Default (training) read: poisoned row invisible.
        let corpus = db.list_rl_events(None, None, None, None, 100, false).unwrap();
        assert_eq!(corpus.len(), 1);
        assert_eq!(corpus[0].task_id, "clean");

        // Inspection read: both visible.
        let full = db.list_rl_events(None, None, None, None, 100, true).unwrap();
        assert_eq!(full.len(), 2);

        // Count filters: None = all (badge unchanged), Some(true) = doctor's.
        assert_eq!(db.count_rl_events(None, None, None, None).unwrap(), 2);
        assert_eq!(db.count_rl_events(None, None, None, Some(true)).unwrap(), 1);
        assert_eq!(db.count_rl_events(None, None, None, Some(false)).unwrap(), 1);
    }

    #[test]
    fn run_once_guard_prevents_second_pass() {
        let db = fresh_db();
        insert_payload(&db, "retrieval", "poisoned-1", POISONED);
        db.run_quarantine_backfill_once();
        assert_eq!(
            db.count_rl_events(None, None, None, Some(true)).unwrap(),
            1,
            "first run marks the historical row"
        );
        // A NEW poisoned row after the one-time pass (can't happen in
        // production — the writer clamp blocks it — but pins the guard).
        insert_payload(&db, "retrieval", "poisoned-2", POISONED);
        db.run_quarantine_backfill_once();
        assert_eq!(
            db.count_rl_events(None, None, None, Some(true)).unwrap(),
            1,
            "guarded second run must not scan again"
        );
        assert!(db
            .app_state_get(QUARANTINE_BACKFILL_STATE_KEY)
            .unwrap()
            .is_some());
    }

    #[test]
    fn payload_scanner_only_convicts_on_positive_evidence() {
        assert!(super::payload_has_out_of_range_score(POISONED));
        assert!(!super::payload_has_out_of_range_score(CLEAN));
        assert!(!super::payload_has_out_of_range_score("not json {"));
        assert!(!super::payload_has_out_of_range_score("{}"));
        assert!(!super::payload_has_out_of_range_score(r#"{"nodes":"oops"}"#));
        assert!(!super::payload_has_out_of_range_score(r#"{"nodes":[{"score":"high"}]}"#));
    }
}

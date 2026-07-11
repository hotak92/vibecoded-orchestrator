//! Bundle-update change detectors (kg/docs re-embed gate).
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the content-change detectors that
//! decide whether an install-bundle update actually mutated a `knowledge/` or
//! `docs/` file (and therefore whether a kg-sync re-embed must run):
//! `BUNDLE_CONTENT_CHANGING_BUCKETS`, `is_kg_or_docs_rel_path`,
//! `envelope_kg_or_docs_content_changed`. These previously lived inline in
//! `projects_v2.rs`; behaviour is unchanged; the facade re-exports every symbol.
//! The const is used only by these detectors, so it travels with them.

/// v0.2.71 Piece 5b — relative-path buckets whose membership means the bundle
/// actually CHANGED a file's on-disk bytes (vs left it untouched). Only these
/// gate the kg-sync re-embed; `noop` / `preserve` / `keep-regenerated` /
/// `skip-*` / `orphan-preserved` all leave the on-disk content as-is, so they
/// must NOT trigger a re-embed.
pub(crate) const BUNDLE_CONTENT_CHANGING_BUCKETS: [&str; 4] =
    ["create", "overwrite", "always-overwrite", "orphan-deleted"];

/// True iff a relative bundle path lives under `knowledge/` or `docs/` — the
/// two trees `sync_knowledge_graph.py --all` walks. Normalises Windows
/// backslashes so the same envelope path matches cross-OS.
pub(crate) fn is_kg_or_docs_rel_path(rel: &str) -> bool {
    let norm = rel.replace('\\', "/");
    let norm = norm.trim_start_matches("./");
    norm.starts_with("knowledge/") || norm.starts_with("docs/")
}

/// v0.2.71 Piece 5b — pure inspector over the install-bundle `--json` envelope.
///
/// Returns `true` iff at least one path in a content-CHANGING bucket
/// (`BUNDLE_CONTENT_CHANGING_BUCKETS`) lives under `knowledge/**` or `docs/**`.
/// That is the ONLY condition under which a fresh `kg-sync --all` re-embed can
/// surface new/changed content into Weaviate on an UPDATE.
///
/// Conservative on ambiguity: if `actions` is missing or not the expected
/// object-of-arrays shape, returns `true` (assume something changed → spawn the
/// sync). Better to pay an unnecessary all-skip re-validation (bounded now by
/// the Piece-5a semaphore) than to silently skip a sync that WAS needed.
pub(crate) fn envelope_kg_or_docs_content_changed(v: &serde_json::Value) -> bool {
    let Some(actions) = v.get("actions").and_then(|a| a.as_object()) else {
        // Unparseable / unexpected shape → assume changed (spawn, safe-but-slow).
        return true;
    };
    for bucket in BUNDLE_CONTENT_CHANGING_BUCKETS {
        let Some(arr) = actions.get(bucket).and_then(|x| x.as_array()) else {
            continue;
        };
        for entry in arr {
            if let Some(rel) = entry.as_str() {
                if is_kg_or_docs_rel_path(rel) {
                    return true;
                }
            }
        }
    }
    false
}

/// v0.2.71 Piece 5b — pure decision predicate for the kg-sync spawn gate.
/// Unit-testable without spawning anything.
///
/// On CREATE (`is_initial_create=true`) ALWAYS spawn: a fresh project's
/// pre-existing `knowledge/**`/`docs/**` must be indexed for the first time
/// (the original 2026-05-12 KG-auto-sync purpose). On UPDATE, spawn ONLY when
/// the bundle actually changed KG/docs content — otherwise the `--all` would
/// re-walk byte-identical content and (per the audit) merely multiply
/// Weaviate fetch round-trips under contention, plus risk a full arctic-CPU
/// re-seed of the curated/shared nodes on any slot/hash miss.
///
/// NOTE the deliberate scope: this gates ONLY the content-change axis. A
/// genuine embedding-MODEL or COLLECTION switch is handled by the dedicated
/// re-embed / migration flow (the regenerate-embeddings modal + migration
/// runner), NOT by a bundle update — a bundle update never changes the active
/// embedding model. So content-change is the correct and sufficient gate here.
pub(crate) fn should_spawn_kg_sync_on_bundle(
    is_initial_create: bool,
    kg_or_docs_content_changed: bool,
) -> bool {
    is_initial_create || kg_or_docs_content_changed
}


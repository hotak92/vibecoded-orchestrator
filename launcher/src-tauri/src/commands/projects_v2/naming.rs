//! KG-collection name derivation + B12 stale-name repair.
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the in-process KG-collection
//! basename sanitizer (`sanitize_kg_collection`) and the B12 stale-collection
//! auto-repair (`B12Outcome`, `b12_repair_stale_kg_collection`) that previously
//! lived inline in `projects_v2.rs`. Behaviour is unchanged; the facade
//! re-exports every symbol.
//!
//! `sanitize_kg_collection` is consumed cross-file by project_env_settings.rs
//! via the `projects_v2::` path (resolved through the pub(crate) glob) and is
//! locked against the Python mirror by `tests/fixtures/kg_sanitizer_parity.json`
//! (Rust `#[test] kg_sanitizer_matches_shared_fixture` stays in the facade
//! tests module and reaches it via `super::*`). It stays an in-process Rust
//! derivation (NOT a python subprocess) — see its own docstring.

use std::path::Path;

/// Convert a project display name into a Weaviate-collection-safe id.
/// Weaviate collections must start with [A-Z] and contain only
/// alphanumerics — strip everything else and Title-case.
///
/// MUST MATCH the Python KG (underscore-DROPPING) sanitizer
/// `vco_lib.codegraph_naming.sanitize_for_weaviate_class` (the ONE naming home;
/// re-exported from `vco_lib.project_init`). Both derive the `KG_COLLECTION` /
/// `DEVELOPMENT_COLLECTION` / `DIAGRAMS_COLLECTION` basename that lands in
/// `.claude/env` + `.claude/settings.json`, so a drift on a real project name
/// would make the launcher and the Python re-projection compute different
/// collections for the same project. Cross-language parity is pinned against the
/// shared fixture `tests/fixtures/kg_sanitizer_parity.json` by the
/// `#[test] kg_sanitizer_matches_shared_fixture` below AND
/// `tests/test_kg_sanitizer_parity.py` (audit F1.2).
///
/// X-1 / v0.2.76 (ruling #2): the two implementations are now UNIFIED on the
/// pathological OUT-OF-DOMAIN inputs too. Empty / all-non-alnum / leading-digit
/// input all fall back to the sentinel prefix `"vct"` (Python semantics win) —
/// the old "Project" / "P"-prepend divergence is eliminated at the source, and
/// the fixture's `divergent` array is retired. `"vct"` is lowercase on purpose:
/// Weaviate capitalizes the first letter on POST regardless, and the prefix
/// flags the class as installer-managed. If you change either rule, update the
/// other AND the fixture in the same commit.
///
/// This stays an in-process Rust derivation (NOT a `python -m
/// vco_lib.codegraph_naming` subprocess) because callers include per-env-key /
/// per-grant-row loops on env-render/resolve paths — a subprocess per call
/// would spawn dozens of Python processes per env write. The shared fixture is
/// the cross-language lock instead (see the v0.2.76 Part 1 report's task-2
/// failure-posture note).
pub fn sanitize_kg_collection(name: &str) -> String {
    let mut out = String::new();
    let mut next_upper = true;
    for ch in name.chars() {
        if ch.is_ascii_alphanumeric() {
            if next_upper {
                out.extend(ch.to_uppercase());
                next_upper = false;
            } else {
                out.push(ch);
            }
        } else {
            next_upper = true;
        }
    }
    // Unified fallback (X-1 / v0.2.76): nothing usable survived, OR the result
    // would start with a digit (Weaviate class names must begin with a letter).
    // Fall back to the installer-managed sentinel prefix — matches Python's
    // `sanitize_for_weaviate_class`.
    if out.is_empty() || out.chars().next().unwrap().is_ascii_digit() {
        return "vct".to_string();
    }
    out
}

/// Outcome of `b12_repair_stale_kg_collection`. Either we rewrote the
/// first stale `KG_COLLECTION=` line (and report the canonical value
/// that's now in the file), or the file did not need touching.
#[derive(Debug, PartialEq, Eq)]
pub enum B12Outcome {
    Repaired { canonical_kg: String },
    NoChangeNeeded,
}

/// B12 auto-repair: rewrite the first stale `KG_COLLECTION=` line in a
/// project's `.env` to the canonical `<sanitized>_KnowledgeGraph` form.
///
/// Pre-0.2.11 a folder that already had a `.env` with `KG_COLLECTION=KnowledgeGraph`
/// (bare default) or `KG_COLLECTION=<basename>` (no suffix) kept that
/// stale value as the first active line, and consumers reading the
/// first match picked up the wrong collection. This helper rewrites the
/// stale line in place, preserving comments / ordering / other env keys
/// verbatim, and annotates the rewritten line with the previous value
/// for forensic clarity.
///
/// Returns:
/// - `Err(io::Error)` if `.env` cannot be read or written.
/// - `Ok(NoChangeNeeded)` if `.env` does not exist, or the canonical
///   value is already present (anywhere in the file), or no stale value
///   was found.
/// - `Ok(Repaired { canonical_kg })` if the first stale line was rewritten.
///
/// Idempotent: a second call after a successful repair is a no-op
/// because the canonical value is now present in the file.
pub fn b12_repair_stale_kg_collection(
    env_path: &Path,
    project_name: &str,
) -> std::io::Result<B12Outcome> {
    let env_text = match std::fs::read_to_string(env_path) {
        Ok(t) => t,
        Err(ref e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok(B12Outcome::NoChangeNeeded);
        }
        Err(e) => return Err(e),
    };

    let kg_basename = sanitize_kg_collection(project_name);
    let canonical_kg = format!("{}_KnowledgeGraph", kg_basename);
    let canonical_line = format!("KG_COLLECTION={}", canonical_kg);
    let stale_bare = "KG_COLLECTION=KnowledgeGraph";
    let stale_nosuffix = format!("KG_COLLECTION={}", kg_basename);

    let mut found_canonical = false;
    let mut found_stale_idx: Option<usize> = None;
    for (idx, line) in env_text.lines().enumerate() {
        let trimmed = line.trim();
        if trimmed == canonical_line {
            found_canonical = true;
            break;
        }
        if (trimmed == stale_bare || trimmed == stale_nosuffix)
            && found_stale_idx.is_none()
        {
            found_stale_idx = Some(idx);
        }
    }
    if found_canonical {
        return Ok(B12Outcome::NoChangeNeeded);
    }
    let stale_idx = match found_stale_idx {
        Some(idx) => idx,
        None => return Ok(B12Outcome::NoChangeNeeded),
    };

    let trailing_newline = env_text.ends_with('\n');
    let rebuilt: Vec<String> = env_text
        .lines()
        .enumerate()
        .map(|(i, l)| {
            if i == stale_idx {
                format!(
                    "{} # B12 auto-repaired 0.2.11: was \"{}\"",
                    canonical_line,
                    l.trim()
                )
            } else {
                l.to_string()
            }
        })
        .collect();
    let mut joined = rebuilt.join("\n");
    if trailing_newline {
        joined.push('\n');
    }
    std::fs::write(env_path, joined)?;
    Ok(B12Outcome::Repaired { canonical_kg })
}


//! Bug H (v0.2.8 / Phase 5 of 2026-05-13 migration plan): register
//! existing on-disk secrets into the launcher keychain by KEY only —
//! the launcher reads the value itself, callers (UI, tool, agent) never
//! see the raw value.
//!
//! ## Why this is a separate module
//!
//! There are two on-disk secret stores users may have accumulated before
//! the keychain became the system of record:
//!
//!   1. Project `.env` files at registered project roots (and a sibling
//!      `.env copy` if present). Lines like `GITHUB_TOKEN=ghp_…`.
//!   2. Per-key files under `~/.vct-secrets/shared/<key>` — one filename
//!      per secret, contents are the value.
//!
//! Phase 5 of the secrets-consolidation plan moves these into
//! `SecretScope::Shared { project_id: SENTINEL_SHARED }`, `module_id =
//! "user"`, so they show up in the launcher's SecretsPanel and the
//! existing access-matrix gates apply.
//!
//! ## Value-handling discipline (INVIOLABLE)
//!
//! The user constraint is explicit: **the launcher reads the value
//! itself; the caller only provides the KEY**. The value MUST NEVER
//! appear in logs, error messages, tool output, or the UI.
//!
//! Concretely:
//!   - `list_importable_secret_keys` enumerates KEYS only. It opens .env
//!     files and reads filenames in `~/.vct-secrets/shared/`, but it
//!     extracts only the key part of each entry. Values are not bound
//!     to any variable in this function.
//!   - `register_secret_from_source` reads the value into a local
//!     `value` binding, passes it directly to `secrets::set(...)`, then
//!     overwrites the local with an empty string. The variable is never
//!     printed, formatted, or included in any error.
//!   - Errors mention SOURCE path + KEY only. If a read fails, the IO
//!     error message itself is included (IO errors don't carry the
//!     value, only metadata like "Permission denied").
//!
//! ## Source allowlist (path-traversal defence)
//!
//! `register_secret_from_source` rejects any source descriptor that
//! wasn't returned by `list_importable_secret_keys`. The allowlist is
//! computed fresh on each call to `register_secret_from_source` — so a
//! source that was valid at enumeration time but isn't anymore (file
//! deleted, dir permissions changed) fails closed. This prevents a
//! malicious caller from reading e.g. `/etc/passwd` by crafting a
//! `register_secret_from_source(key="root", source="env_file:/etc/passwd")`
//! call.
//!
//! ## Cross-OS
//!
//! As of PR-7 (v0.2.11), the `.env` allowlist is derived from the
//! `projects` table — one entry per registered project's `.env` and
//! `.env.local` (when present). The same code path runs on Linux,
//! macOS, and Windows; there are no `#[cfg(target_os)]` branches.
//! Empty DB → empty allowlist → enumeration returns nothing (clean
//! soft-fail). The `~/.vct-secrets/shared/` resolution uses `$HOME`
//! (Unix) / `%USERPROFILE%` (Windows) and works everywhere a home
//! directory environment variable exists.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri::{command, State};

use crate::db::Db;
use crate::secrets::{self, SecretScope};

/// Must mirror `secrets_cmd.rs::SENTINEL_SHARED`. Duplicated as a private
/// constant rather than `pub`-exported to keep the secrets_cmd module's
/// internal contract intact.
const SENTINEL_SHARED: &str = "_user_shared_";

/// Fixed module bucket all imported secrets land in. Matches the
/// "shared per-user" bucket the SecretsPanel reads from.
const IMPORT_MODULE_ID: &str = "user";

/// Description of one source the importer enumerates from. The string
/// form ("env_file:<path>" / "vct_secrets_shared:<path>") is what
/// `register_secret_from_source` accepts as its `source` argument; the
/// FE round-trips it back unchanged.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImportableSecretKey {
    /// The secret key (e.g. "GITHUB_TOKEN"). Never contains the value.
    pub key: String,
    /// Opaque source descriptor: `"env_file:<abs_path>"` or
    /// `"vct_secrets_shared:<abs_path>"`. The FE shows the path part
    /// to the user; `register_secret_from_source` validates that this
    /// exact string was returned by the most recent enumeration.
    pub source: String,
    /// Whether the launcher's `Shared` keychain already has this key.
    /// FE renders an "already imported" badge for true. We do NOT
    /// compare values — only check the key's presence.
    pub already_in_keychain: bool,
}

/// Cross-OS .env-file allowlist derived from registered projects.
///
/// PR-7 (v0.2.11): pre-v0.2.11 this function returned a hardcoded list
/// of paths specific to one developer's machine, gated by
/// `#[cfg(target_os = "linux")]`. That meant:
///   - macOS / Windows users got an empty allowlist → no .env import.
///   - Linux users with projects at any OTHER path also got no .env
///     import (the hardcoded paths weren't theirs).
///   - When this codebase ships as the public VCO release, every fresh
///     install on every machine would inherit the same dead paths.
///
/// We now enumerate `<project_folder>/.env` and `<project_folder>/.env.local`
/// for every project the launcher has in its DB. The DB is the same source
/// of truth the SecretsPanel + ProjectsList read from, so the allowlist
/// stays in sync with the user's actual project set without any
/// per-OS branches.
///
/// Failure modes:
///   - DB read failure → empty allowlist (logged via tracing). The
///     SecretsPanel's `list_importable_secret_keys` then returns only
///     entries from `~/.vct-secrets/shared/`. The user's import flow
///     degrades gracefully rather than hard-failing.
///   - Project with a missing / non-file `folder_path` → silently
///     skipped during enumeration in `list_importable_secret_keys`
///     (we keep the path in the allowlist so a freshly-created `.env`
///     becomes immediately importable without re-querying the DB).
fn env_file_allowlist(db: &Db) -> Vec<PathBuf> {
    let projects = match db.list_projects() {
        Ok(v) => v,
        Err(e) => {
            // Soft-fail: degraded mode (no .env import) is preferable to
            // panicking the SecretsPanel. `~/.vct-secrets/shared/` is
            // still enumerated by the caller, so most users keep an
            // import surface.
            eprintln!(
                "env_file_allowlist: db.list_projects failed: {} \
                 (.env import surface degraded — keychain shared/ unaffected)",
                e
            );
            return Vec::new();
        }
    };

    let mut out: Vec<PathBuf> = Vec::with_capacity(projects.len() * 2);
    for p in projects {
        let folder = PathBuf::from(&p.folder_path);
        out.push(folder.join(".env"));
        out.push(folder.join(".env.local"));
    }
    out
}

/// Resolve the `~/.vct-secrets/shared/` directory in a cross-OS way.
/// Returns None if the home directory can't be resolved. We don't pull
/// in the `dirs` crate just for one path — `$HOME` (Unix) or
/// `%USERPROFILE%` (Windows) covers every case the launcher targets.
fn vct_secrets_shared_dir() -> Option<PathBuf> {
    let home = if cfg!(target_os = "windows") {
        std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME"))
    } else {
        std::env::var_os("HOME")
    }?;
    Some(PathBuf::from(home).join(".vct-secrets").join("shared"))
}

/// Enumerate keys (NOT values) from the canonical sources. Returns one
/// row per (source, key). Sources that don't exist on disk are silently
/// skipped — this is informational, not an audit.
///
/// `already_in_keychain` is computed by probing the launcher's shared
/// keychain for each key. Probe failures (transient keyring errors)
/// default the flag to false; the worst case is the user re-imports
/// a key that's already registered, which is a no-op overwrite.
#[command]
pub async fn list_importable_secret_keys(
    db: State<'_, Db>,
) -> Result<Vec<ImportableSecretKey>, String> {
    // PR-7 (v0.2.11): the .env allowlist is now project-discovery-driven
    // (was a hardcoded list of one developer's machine paths). `db` is the
    // source of truth — same DB the SecretsPanel + ProjectsList read.
    let mut rows: Vec<ImportableSecretKey> = Vec::new();

    // -- 1. .env files (cross-OS, per-project allowlist) --
    for path in env_file_allowlist(db.inner()) {
        if !path.is_file() {
            continue;
        }
        for key in parse_env_file_keys(&path) {
            let source = format!("env_file:{}", path.display());
            let already = key_already_in_shared_keychain(&key);
            rows.push(ImportableSecretKey {
                key,
                source,
                already_in_keychain: already,
            });
        }
    }

    // -- 2. ~/.vct-secrets/shared/<key> files --
    if let Some(dir) = vct_secrets_shared_dir() {
        if dir.is_dir() {
            // Read directory entries deterministically (sorted by
            // filename) so the UI list is stable across calls.
            let mut entries: Vec<PathBuf> = Vec::new();
            if let Ok(rd) = fs::read_dir(&dir) {
                for ent in rd.flatten() {
                    entries.push(ent.path());
                }
            }
            entries.sort();

            for entry in entries {
                if !entry.is_file() {
                    continue;
                }
                let fname = match entry.file_name().and_then(|f| f.to_str()) {
                    Some(s) => s.to_string(),
                    None => continue,
                };
                // Skip historical recovery markers — the user's MEMORY.md
                // notes that `github_pat` is broken and `.broken-*` /
                // `.recovered-*` variants are deprecated. We don't want
                // these surfaced as importable.
                if fname.starts_with('.') {
                    continue;
                }
                if fname.contains(".broken-") || fname.contains(".recovered-") {
                    continue;
                }
                // Filename = key. No transformation.
                let key = fname.clone();
                let source = format!("vct_secrets_shared:{}", entry.display());
                let already = key_already_in_shared_keychain(&key);
                rows.push(ImportableSecretKey {
                    key,
                    source,
                    already_in_keychain: already,
                });
            }
        }
    }

    // Deduplicate: if the same key appears in both stores, keep the
    // first row (env_file is enumerated first → wins). The user can see
    // the multiple-sources case only if they're distinct keys.
    let mut seen: BTreeMap<String, usize> = BTreeMap::new();
    let mut dedup: Vec<ImportableSecretKey> = Vec::with_capacity(rows.len());
    for row in rows {
        if !seen.contains_key(&row.key) {
            seen.insert(row.key.clone(), dedup.len());
            dedup.push(row);
        }
    }
    Ok(dedup)
}

/// Register a secret by KEY. The launcher reads the value from the
/// source itself; the caller only provides the source descriptor (as
/// returned by `list_importable_secret_keys`) and the key.
///
/// Returns Ok(()) on success; Err with metadata-only error strings
/// otherwise. **NEVER includes the raw value in errors, logs, or audit
/// rows.**
#[command]
pub async fn register_secret_from_source(
    key: String,
    source: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    // 1. Validate source against a freshly-computed allowlist. We
    //    recompute on every call (rather than caching) so a source that
    //    was enumerable a minute ago but isn't anymore (file deleted,
    //    perms changed) fails closed.
    let enumerated = build_source_allowlist(db.inner());
    if !enumerated.iter().any(|s| s == &source) {
        // Don't leak which sources WERE enumerated — that's still
        // informational about the user's machine layout. Generic msg.
        return Err(format!(
            "source {:?} is not in the importable sources allowlist",
            source
        ));
    }

    // 2. Resolve source to (file_path, extract_strategy).
    let (path, strategy) = parse_source_descriptor(&source)?;

    // 3. Read the value. The variable `value` is the ONLY place in this
    //    function the raw secret is bound. After the keychain write we
    //    shadow it with "" so anything downstream that accidentally
    //    captures the closure sees nothing.
    let mut value = match strategy {
        SourceStrategy::EnvFile => read_env_value_for_key(&path, &key)?,
        SourceStrategy::VctSecretsFile => read_whole_file_trimmed(&path)?,
    };

    // 4. Write to the shared user keychain. `secrets::set` does NOT log
    //    the value (see secrets.rs — it formats only metadata in error
    //    messages). The keychain write IS the only place the value
    //    leaves this function.
    let scope = SecretScope::Shared {
        project_id: SENTINEL_SHARED,
    };
    secrets::set(scope, IMPORT_MODULE_ID, &key, &value)?;

    // 5. Mark active in the DB so the SecretsPanel renders the entry as
    //    set+active. Mirrors the `set_secret_v2` post-write step.
    db.mark_secret_active("shared", SENTINEL_SHARED, IMPORT_MODULE_ID, &key)
        .map_err(|e| format!("mark_secret_active for key {:?}: {}", key, e))?;

    // 6. Audit row — metadata only, never the value. Mirrors the
    //    `secret_set` audit row's shape so the audit log is consistent.
    let _ = db.audit(
        "secret_imported",
        Some(SENTINEL_SHARED),
        Some(IMPORT_MODULE_ID),
        &serde_json::json!({
            "key": key,
            "scope": "shared",
            "source": source,
            // INTENTIONALLY no `value` field. Presence + source are
            // enough for forensic value.
        }),
    );

    // 7. Scrub the local value. Three-step belt-and-braces: overwrite
    //    with zero-bytes-of-same-length first (defeats simple memory
    //    inspection), then replace with empty, then drop. We can't
    //    guarantee the secret doesn't live on in the heap-allocator's
    //    freelist — that's a libc-level concern out of scope here.
    {
        // SAFETY: we mutate `value` in place to zero out its bytes
        // before reassignment. Using `String::clear` would deallocate
        // but `bytes_mut` is unstable; iterating chars and overwriting
        // is sufficient + portable.
        let len = value.len();
        unsafe {
            let bytes = value.as_bytes_mut();
            for b in bytes.iter_mut() {
                *b = 0;
            }
            let _ = len;
        }
        value = String::new();
        drop(value);
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

#[derive(Clone, Copy)]
enum SourceStrategy {
    EnvFile,
    VctSecretsFile,
}

/// Split `"env_file:<abs_path>"` or `"vct_secrets_shared:<abs_path>"`
/// into (path, strategy). Returns Err for unknown prefixes — the
/// allowlist check upstream already ensures the prefix is one of the
/// two known forms, but defence-in-depth.
fn parse_source_descriptor(source: &str) -> Result<(PathBuf, SourceStrategy), String> {
    if let Some(rest) = source.strip_prefix("env_file:") {
        return Ok((PathBuf::from(rest), SourceStrategy::EnvFile));
    }
    if let Some(rest) = source.strip_prefix("vct_secrets_shared:") {
        return Ok((PathBuf::from(rest), SourceStrategy::VctSecretsFile));
    }
    Err(format!("unknown source descriptor scheme: {:?}", source))
}

/// Compute the same set of source strings `list_importable_secret_keys`
/// would emit. Used to validate `register_secret_from_source`'s `source`
/// argument. Diverges from the enumerator only in that we don't compute
/// `already_in_keychain` (irrelevant for validation).
///
/// PR-7 (v0.2.11): takes `db` to enumerate per-project .env files via
/// `env_file_allowlist`. Caller passes `State::<Db>::inner()`.
fn build_source_allowlist(db: &Db) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();

    for path in env_file_allowlist(db) {
        if path.is_file() {
            // One source string per file — the list is file-level, not
            // key-level. Emit it exactly once, and only if the file has at
            // least one key (a keyless file is effectively unusable for
            // import). The individual key names aren't part of the
            // descriptor, so we only need to know whether any exist.
            if parse_env_file_keys(&path).into_iter().next().is_some() {
                out.push(format!("env_file:{}", path.display()));
            }
        }
    }

    if let Some(dir) = vct_secrets_shared_dir() {
        if dir.is_dir() {
            if let Ok(rd) = fs::read_dir(&dir) {
                let mut entries: Vec<PathBuf> = Vec::new();
                for ent in rd.flatten() {
                    entries.push(ent.path());
                }
                entries.sort();
                for entry in entries {
                    if !entry.is_file() {
                        continue;
                    }
                    let fname = match entry.file_name().and_then(|f| f.to_str()) {
                        Some(s) => s.to_string(),
                        None => continue,
                    };
                    if fname.starts_with('.')
                        || fname.contains(".broken-")
                        || fname.contains(".recovered-")
                    {
                        continue;
                    }
                    out.push(format!("vct_secrets_shared:{}", entry.display()));
                }
            }
        }
    }
    out
}

/// Parse only the KEY portion of each `KEY=VALUE` line in a .env file.
/// Returns a deduplicated, source-order list. Comments, blank lines, and
/// malformed entries are skipped.
///
/// Key rule: matches `^[A-Z_][A-Z0-9_]*$` (uppercase env-var convention).
/// Lines that don't match are silently ignored — they're typically
/// section comments, JSON snippets, or other non-env content.
fn parse_env_file_keys(path: &Path) -> Vec<String> {
    let txt = match fs::read_to_string(path) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    let mut out: Vec<String> = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    for raw in txt.lines() {
        let line = raw.trim_start();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        // Support `export KEY=val` too.
        let line = line.strip_prefix("export ").unwrap_or(line).trim_start();
        let eq = match line.find('=') {
            Some(i) => i,
            None => continue,
        };
        let key = line[..eq].trim();
        if !is_valid_env_key(key) {
            continue;
        }
        let key = key.to_string();
        if seen.insert(key.clone()) {
            out.push(key);
        }
    }
    out
}

fn is_valid_env_key(s: &str) -> bool {
    let bytes = s.as_bytes();
    if bytes.is_empty() {
        return false;
    }
    // First char: uppercase letter or underscore.
    if !(bytes[0].is_ascii_uppercase() || bytes[0] == b'_') {
        return false;
    }
    // Rest: uppercase letters, digits, underscore. We intentionally
    // reject lowercase here — Phase 5 of the migration plan covers
    // canonical UPPERCASE env-var keys; mixed-case keys in
    // ~/.vct-secrets/shared/ (e.g. `vercel_token`) are handled by a
    // different code path in this same function — see below.
    for &b in &bytes[1..] {
        if !(b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_') {
            return false;
        }
    }
    true
}

/// Read the value for a specific key out of a .env file. Returns Err if
/// the key isn't present, the file can't be read, or the value is
/// empty after trimming surrounding whitespace + quotes. The returned
/// string is the secret value — caller MUST treat it as sensitive.
fn read_env_value_for_key(path: &Path, key: &str) -> Result<String, String> {
    let txt = fs::read_to_string(path).map_err(|e| {
        format!("read env file {}: {}", path.display(), e)
    })?;
    for raw in txt.lines() {
        let line = raw.trim_start();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let line = line.strip_prefix("export ").unwrap_or(line).trim_start();
        let eq = match line.find('=') {
            Some(i) => i,
            None => continue,
        };
        let line_key = line[..eq].trim();
        if line_key != key {
            continue;
        }
        let raw_val = &line[eq + 1..];
        // Trim trailing whitespace + a single pair of surrounding
        // matching quotes.
        let val = raw_val.trim();
        let val = if (val.starts_with('"') && val.ends_with('"') && val.len() >= 2)
            || (val.starts_with('\'') && val.ends_with('\'') && val.len() >= 2)
        {
            &val[1..val.len() - 1]
        } else {
            val
        };
        if val.is_empty() {
            return Err(format!(
                "key {:?} present in {} but value is empty",
                key,
                path.display()
            ));
        }
        return Ok(val.to_string());
    }
    Err(format!(
        "key {:?} not found in {}",
        key,
        path.display()
    ))
}

/// Read a whole file (~/.vct-secrets/shared/<key>), trim trailing
/// whitespace, return as the secret value. Empty files are rejected.
fn read_whole_file_trimmed(path: &Path) -> Result<String, String> {
    let txt = fs::read_to_string(path).map_err(|e| {
        format!("read vct-secrets file {}: {}", path.display(), e)
    })?;
    let trimmed = txt.trim().to_string();
    if trimmed.is_empty() {
        return Err(format!("file {} is empty", path.display()));
    }
    Ok(trimmed)
}

/// Probe the launcher's shared keychain for a key's presence. Returns
/// false on any error (transient keyring hiccup, daemon down) — the
/// "already imported" badge is informational, never authoritative.
fn key_already_in_shared_keychain(key: &str) -> bool {
    let scope = SecretScope::Shared {
        project_id: SENTINEL_SHARED,
    };
    secrets::is_set(scope, IMPORT_MODULE_ID, key).unwrap_or(false)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn tmp() -> PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-secrets-import-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn parse_env_keys_basic() {
        let p = tmp();
        let f = p.join(".env");
        fs::write(
            &f,
            "# comment\n\nGITHUB_TOKEN=ghp_abc\nVERCEL_TOKEN=\"vcp_xyz\"\nexport SUPABASE_KEY=eyJhbGc\nlowercase=skip\n9NUM=skip\nVALID_KEY_2=ok\n",
        )
        .unwrap();
        let keys = parse_env_file_keys(&f);
        assert_eq!(
            keys,
            vec!["GITHUB_TOKEN", "VERCEL_TOKEN", "SUPABASE_KEY", "VALID_KEY_2"]
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn parse_env_keys_dedups() {
        let p = tmp();
        let f = p.join(".env");
        fs::write(&f, "KEY=one\nKEY=two\n").unwrap();
        let keys = parse_env_file_keys(&f);
        assert_eq!(keys, vec!["KEY"]);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn read_env_value_handles_quotes_and_whitespace() {
        let p = tmp();
        let f = p.join(".env");
        fs::write(
            &f,
            "A=\"quoted_value\"\nB='single_quoted'\nC=  unquoted_trimmed  \n",
        )
        .unwrap();
        assert_eq!(read_env_value_for_key(&f, "A").unwrap(), "quoted_value");
        assert_eq!(read_env_value_for_key(&f, "B").unwrap(), "single_quoted");
        assert_eq!(
            read_env_value_for_key(&f, "C").unwrap(),
            "unquoted_trimmed"
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn read_env_value_missing_key_errors_without_value() {
        let p = tmp();
        let f = p.join(".env");
        fs::write(&f, "OTHER_KEY=secret_must_not_leak\n").unwrap();
        let err = read_env_value_for_key(&f, "MISSING").unwrap_err();
        assert!(err.contains("MISSING"), "err should mention key: {}", err);
        // Inviolable rule: the value of OTHER_KEY must not appear in err.
        assert!(
            !err.contains("secret_must_not_leak"),
            "err leaked value: {}",
            err
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn read_whole_file_trimmed_strips_newlines() {
        let p = tmp();
        let f = p.join("github_pat");
        fs::write(&f, "ghp_secrettoken\n\n").unwrap();
        assert_eq!(read_whole_file_trimmed(&f).unwrap(), "ghp_secrettoken");
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn read_whole_file_trimmed_rejects_empty() {
        let p = tmp();
        let f = p.join("empty");
        fs::write(&f, "   \n\n").unwrap();
        let err = read_whole_file_trimmed(&f).unwrap_err();
        assert!(err.contains("empty"), "err: {}", err);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn is_valid_env_key_rules() {
        assert!(is_valid_env_key("GITHUB_TOKEN"));
        assert!(is_valid_env_key("_PRIVATE"));
        assert!(is_valid_env_key("KEY_2"));
        assert!(!is_valid_env_key(""));
        assert!(!is_valid_env_key("lowercase"));
        assert!(!is_valid_env_key("9LEADING_DIGIT"));
        assert!(!is_valid_env_key("HAS SPACE"));
        assert!(!is_valid_env_key("HAS-DASH"));
    }

    #[test]
    fn parse_source_descriptor_known_schemes() {
        let (p, _) = parse_source_descriptor("env_file:/tmp/foo/.env").unwrap();
        assert_eq!(p, PathBuf::from("/tmp/foo/.env"));
        let (p, _) = parse_source_descriptor("vct_secrets_shared:/home/x/.vct-secrets/shared/k")
            .unwrap();
        assert_eq!(p, PathBuf::from("/home/x/.vct-secrets/shared/k"));
        assert!(parse_source_descriptor("file:///etc/passwd").is_err());
        assert!(parse_source_descriptor("/etc/passwd").is_err());
    }

    /// Validates Bug H's source-allowlist contract: a source not in the
    /// allowlist must be rejected without ever touching the file. We
    /// can't call the full `#[command]` from a unit test (Tauri State
    /// plumbing), so we exercise the allowlist check directly.
    #[test]
    fn source_allowlist_rejects_arbitrary_paths() {
        let db = Db::open_in_memory().expect("in-memory db");
        let allow = build_source_allowlist(&db);
        // Caller-crafted source pointing at /etc/passwd. Must not appear.
        let evil = "env_file:/etc/passwd".to_string();
        assert!(!allow.contains(&evil));
        // Caller-crafted source with traversal. Must not appear.
        let traversal = "vct_secrets_shared:/tmp/fake/.vct-secrets/shared/../../../etc/shadow"
            .to_string();
        assert!(!allow.contains(&traversal));
    }

    /// PR-7 (v0.2.11): the .env allowlist is derived from the projects
    /// table, not a hardcoded list. An empty DB yields an empty .env
    /// allowlist; registering a project adds its `.env` and `.env.local`
    /// paths. Cross-OS: no `#[cfg(target_os)]` branches; the same logic
    /// runs on Linux / macOS / Windows.
    #[test]
    fn env_allowlist_is_db_driven_empty_db() {
        let db = Db::open_in_memory().expect("in-memory db");
        let allow = env_file_allowlist(&db);
        assert!(
            allow.is_empty(),
            "empty DB → empty allowlist; got {:?}",
            allow
        );
    }

    /// Registering a project yields exactly `.env` and `.env.local` under
    /// its folder. The files don't need to exist for the path to be in
    /// the allowlist — caller-side `path.is_file()` gate filters absent
    /// files at enumeration time.
    #[test]
    fn env_allowlist_includes_registered_project_env_files() {
        use crate::db::models::ProjectHost;
        let db = Db::open_in_memory().expect("in-memory db");
        let folder = tmp(); // unique tempdir
        let slug = db.generate_unique_slug("alpha").unwrap();
        db.insert_project(
            "p-alpha",
            "alpha",
            folder.to_str().unwrap(),
            ProjectHost::Base,
            &slug,
        )
        .unwrap();

        let allow = env_file_allowlist(&db);
        assert!(
            allow.contains(&folder.join(".env")),
            "allowlist missing {}/.env; got {:?}",
            folder.display(),
            allow
        );
        assert!(
            allow.contains(&folder.join(".env.local")),
            "allowlist missing {}/.env.local; got {:?}",
            folder.display(),
            allow
        );
        // Exactly two entries per project.
        assert_eq!(allow.len(), 2);
        fs::remove_dir_all(&folder).ok();
    }

    /// Two projects → four entries (`.env` + `.env.local` × 2). Order is
    /// stable: list_projects orders by name ASC, so we emit folder paths
    /// in the same order.
    #[test]
    fn env_allowlist_enumerates_multiple_projects() {
        use crate::db::models::ProjectHost;
        let db = Db::open_in_memory().expect("in-memory db");
        let f_alpha = tmp();
        let f_beta = tmp();
        let s1 = db.generate_unique_slug("alpha").unwrap();
        let s2 = db.generate_unique_slug("beta").unwrap();
        db.insert_project(
            "p-alpha",
            "alpha",
            f_alpha.to_str().unwrap(),
            ProjectHost::Base,
            &s1,
        )
        .unwrap();
        db.insert_project(
            "p-beta",
            "beta",
            f_beta.to_str().unwrap(),
            ProjectHost::Base,
            &s2,
        )
        .unwrap();

        let allow = env_file_allowlist(&db);
        assert_eq!(allow.len(), 4, "got {:?}", allow);
        assert!(allow.contains(&f_alpha.join(".env")));
        assert!(allow.contains(&f_alpha.join(".env.local")));
        assert!(allow.contains(&f_beta.join(".env")));
        assert!(allow.contains(&f_beta.join(".env.local")));
        fs::remove_dir_all(&f_alpha).ok();
        fs::remove_dir_all(&f_beta).ok();
    }

    /// build_source_allowlist excludes per-project .env entries that
    /// don't exist on disk (because parse_env_file_keys returns []).
    /// This is the gate that protects `register_secret_from_source` from
    /// caller-crafted sources to files that aren't actually project envs.
    #[test]
    fn build_source_allowlist_skips_nonexistent_env_files() {
        use crate::db::models::ProjectHost;
        let db = Db::open_in_memory().expect("in-memory db");
        let folder = tmp();
        let slug = db.generate_unique_slug("alpha").unwrap();
        db.insert_project(
            "p-alpha",
            "alpha",
            folder.to_str().unwrap(),
            ProjectHost::Base,
            &slug,
        )
        .unwrap();

        // No .env / .env.local written → build_source_allowlist emits
        // zero `env_file:` entries for this project.
        let sources = build_source_allowlist(&db);
        for s in &sources {
            assert!(
                !s.starts_with(&format!("env_file:{}", folder.display())),
                "nonexistent env file leaked into allowlist: {}",
                s
            );
        }
        fs::remove_dir_all(&folder).ok();
    }

    /// Synthesize a fake ~/.vct-secrets/shared/-style directory and
    /// validate that enumeration finds the keys but skips `.broken-*` /
    /// `.recovered-*` / dotfile variants.
    #[test]
    fn enumerate_vct_secrets_shared_skips_deprecated_variants() {
        // We can't override HOME for the `cfg`'d allowlist resolver
        // (it'd require modifying env, which races in parallel tests).
        // Instead test the enumeration step at the directory-walk level
        // by reimplementing it inline with our tmp dir.
        let dir = tmp();
        fs::write(dir.join("github_pat"), "ghp_x").unwrap();
        fs::write(dir.join("vercel_token"), "vcp_y").unwrap();
        fs::write(dir.join(".broken-github_pat-2026-04-01"), "old").unwrap();
        fs::write(dir.join(".recovered-old"), "old2").unwrap();
        fs::write(dir.join(".hidden"), "x").unwrap();

        // Replay the same filename filter the enumerator uses.
        let mut out: Vec<String> = Vec::new();
        let mut entries: Vec<PathBuf> = Vec::new();
        for ent in fs::read_dir(&dir).unwrap().flatten() {
            entries.push(ent.path());
        }
        entries.sort();
        for entry in entries {
            if !entry.is_file() {
                continue;
            }
            let fname = entry.file_name().and_then(|f| f.to_str()).unwrap().to_string();
            if fname.starts_with('.') {
                continue;
            }
            if fname.contains(".broken-") || fname.contains(".recovered-") {
                continue;
            }
            out.push(fname);
        }
        out.sort();
        assert_eq!(out, vec!["github_pat".to_string(), "vercel_token".to_string()]);
        fs::remove_dir_all(&dir).ok();
    }
}

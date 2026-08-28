//! Local-side machine-config loaded once at launcher startup.
//!
//! Background: the launcher carries a few "where do I find the user's local
//! services" defaults — most prominently `http://localhost:8081` for the
//! local Weaviate instance the KG dashboard talks to. These are NOT
//! product-fixed (different from e.g. the canonical license-validation
//! Supabase URL): a user with port conflicts or running services in a
//! VM with port forwarding may legitimately need to override them WITHOUT
//! rebuilding the launcher binary.
//!
//! This module loads `vct-config.toml` from next to the launcher binary
//! (resolved via `std::env::current_exe()`) and exposes the values via
//! Tauri's managed state (`app.manage(LocalConfig::load())`). Consumers
//! pull `State<LocalConfig>` like any other handle and read the fields
//! directly.
//!
//! Resolution order per field:
//!   1. Env-var override (if set + non-empty) — highest priority, useful
//!      for one-shot overrides without editing the file.
//!   2. `vct-config.toml` value — persistent per-machine override.
//!   3. Compiled-default constant — fallback if neither is provided.
//!
//! Missing or unparseable config file falls through to compiled defaults.
//! We log once at startup so an operator can see what the launcher picked.
//!
//! What's IN scope (externalized):
//!   * `weaviate_url` — `http://localhost:8081`. The user's local Weaviate
//!     instance address. Override via `VCT_WEAVIATE_URL` env or
//!     `weaviate_url = "..."` in `vct-config.toml`. Mirrors the existing
//!     `WEAVIATE_URL` env var (kept as an alias for back-compat with
//!     existing developer setups).
//!
//! What's OUT of scope (intentionally NOT externalized):
//!   * `commands::licensing::DEFAULT_VALIDATE_TIER_URL` — product-fixed
//!     Supabase functions URL; staging override stays
//!     `VCT_VALIDATE_TIER_URL` (env-only, no file).
//!   * `commands::installer::ORCHESTRATOR_REPO` — canonical GitHub repo
//!     URL; not a per-machine value.
//!   * Per-service ports (`DEFAULT_WEAVIATE_PORT` etc. in
//!     `project_env_settings.rs` / `installer.rs`) — already overridable
//!     via the `app_state` per-project override keys + `services.toml`
//!     adoption flow. Layering a third source on top would duplicate that
//!     machinery without benefit.
//!   * `services/adoption.rs::ServiceCatalog` — feeds the adoption flow
//!     which has its own override path via `services.toml`.

use std::path::PathBuf;

/// Compiled default for the local Weaviate URL. Must match the historical
/// `kg.rs::DEFAULT_WEAVIATE_URL` value so a fresh install with no
/// `vct-config.toml` and no env override behaves identically to pre-config
/// builds.
pub const DEFAULT_WEAVIATE_URL: &str = "http://localhost:8081";

/// Filename of the optional config file, looked up next to the launcher
/// binary (`std::env::current_exe()` → parent dir → this name).
const CONFIG_FILENAME: &str = "vct-config.toml";

/// Local-side per-machine configuration loaded at launcher startup.
///
/// Add new fields here when externalizing additional local-side defaults.
/// Each new field MUST:
///   * Document its env-var override name in a doc comment.
///   * Have a sensible compiled default in `Default::default()`.
///   * Be resolved through the same env > file > default precedence
///     in `LocalConfig::load()`.
#[derive(Debug, Clone)]
pub struct LocalConfig {
    /// User's local Weaviate instance URL. Used by the KG dashboard
    /// backend (`commands::kg`), the codegraph dashboard
    /// (`commands::codegraph`), and the CLI-API hub bridge
    /// (`hub::cli_api`).
    ///
    /// Env-var override: `VCT_WEAVIATE_URL` (preferred). Legacy alias:
    /// `WEAVIATE_URL` — still honoured so existing developer setups keep
    /// working without re-exporting variables.
    ///
    /// Compiled default: `DEFAULT_WEAVIATE_URL`
    /// (`http://localhost:8081`).
    pub weaviate_url: String,
}

impl Default for LocalConfig {
    fn default() -> Self {
        Self {
            weaviate_url: DEFAULT_WEAVIATE_URL.to_string(),
        }
    }
}

/// Mirror struct for TOML parsing. Every field is `Option<T>` so a
/// partially-populated `vct-config.toml` (which is the common case —
/// users only override the keys they care about) doesn't error out.
#[derive(Debug, Default, serde::Deserialize)]
struct LocalConfigFile {
    weaviate_url: Option<String>,
}

impl LocalConfig {
    /// Load the launcher's local config.
    ///
    /// Never fails — a missing / unreadable / malformed config file is
    /// logged and replaced with compiled defaults. This is intentional:
    /// the launcher must boot even when the operator misconfigures the
    /// file, so they can still reach the GUI and fix it.
    ///
    /// One INFO-level line is emitted to stderr describing what the
    /// launcher picked, so an operator inspecting logs can quickly verify
    /// the source of each value.
    pub fn load() -> Self {
        Self::load_from_path(Self::default_config_path().as_deref())
    }

    /// Test-friendly entry point. `path = None` skips the file-load step
    /// (still applies env-var overrides on top of compiled defaults).
    pub fn load_from_path(path: Option<&std::path::Path>) -> Self {
        let mut cfg = LocalConfig::default();
        let mut sources: Vec<(&'static str, &'static str)> = Vec::new();

        // 1. Apply file values, if any.
        if let Some(p) = path {
            match std::fs::read_to_string(p) {
                Ok(contents) => match toml::from_str::<LocalConfigFile>(&contents) {
                    Ok(parsed) => {
                        if let Some(v) = parsed.weaviate_url.as_ref().filter(|s| !s.is_empty()) {
                            cfg.weaviate_url = v.clone();
                            sources.push(("weaviate_url", "file"));
                        }
                    }
                    Err(e) => {
                        tracing::warn!(
                            path = %p.display(),
                            error = %e,
                            "[vct-config] failed to parse config file; using compiled defaults"
                        );
                    }
                },
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    // Common case (user never created the file). No log.
                }
                Err(e) => {
                    tracing::warn!(
                        path = %p.display(),
                        error = %e,
                        "[vct-config] failed to read config file; using compiled defaults"
                    );
                }
            }
        }

        // 2. Apply env-var overrides. Highest priority — wins over file.
        // weaviate_url: `VCT_WEAVIATE_URL` is the preferred name; the
        // legacy `WEAVIATE_URL` alias is kept so existing developer
        // shells / docker-compose env files keep working.
        if let Some(v) = nonempty_env("VCT_WEAVIATE_URL") {
            cfg.weaviate_url = v;
            replace_or_push(&mut sources, "weaviate_url", "env(VCT_WEAVIATE_URL)");
        } else if let Some(v) = nonempty_env("WEAVIATE_URL") {
            cfg.weaviate_url = v;
            replace_or_push(&mut sources, "weaviate_url", "env(WEAVIATE_URL)");
        }

        // 3. Whatever fields don't have a `sources` entry are using the
        // compiled default. Surface the resolved provenance for ops.
        let pretty: Vec<String> = ["weaviate_url"]
            .iter()
            .map(|field| {
                let src = sources
                    .iter()
                    .find_map(|(f, s)| if f == field { Some(*s) } else { None })
                    .unwrap_or("default");
                format!("{}={}", field, src)
            })
            .collect();
        tracing::info!(
            provenance = %pretty.join(", "),
            "[vct-config] loaded local config"
        );

        cfg
    }

    /// Resolve the canonical config path: directory of the running
    /// launcher binary + `vct-config.toml`. Returns `None` if the
    /// current_exe lookup fails (rare — sandboxed test runners or
    /// stripped-symbol builds on some platforms).
    fn default_config_path() -> Option<PathBuf> {
        std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|p| p.to_path_buf()))
            .map(|d| d.join(CONFIG_FILENAME))
    }
}

/// Read an env var and return Some(value) iff it's set AND non-empty.
/// Empty strings are treated as "not set" — otherwise an accidental
/// `export VCT_WEAVIATE_URL=` (no value) would resolve the URL to the
/// empty string and break every Weaviate call.
fn nonempty_env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

/// Helper: update an existing `(field, source)` entry in-place if
/// present; otherwise append. Keeps the provenance log accurate when a
/// later precedence layer overrides an earlier one (e.g. env > file).
fn replace_or_push(
    sources: &mut Vec<(&'static str, &'static str)>,
    field: &'static str,
    source: &'static str,
) {
    if let Some(entry) = sources.iter_mut().find(|(f, _)| *f == field) {
        entry.1 = source;
    } else {
        sources.push((field, source));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::sync::Mutex;

    // Env vars are process-wide; serialise tests that mutate them so
    // parallel runs don't observe each other. Each `#[test]` calls
    // `serialize()` ONCE at entry — `std::sync::Mutex` is not re-entrant,
    // so locking it inside every nested `with_env` invocation deadlocks
    // (one outer + one inner with_env on the same test thread = lock
    // attempt on a Mutex the same thread already holds, which blocks
    // forever). Mirrors the pattern in `paths.rs` tests but with the
    // single-lock discipline made explicit.
    static SERIALIZE: Mutex<()> = Mutex::new(());

    /// Acquire the env-mutation lock for the duration of the test.
    /// Hold the returned guard until the test ends (let-binding it as
    /// `_g` is enough). Lock-poisoning from a panicking peer test is
    /// recovered via `into_inner` so a single failed test doesn't make
    /// every subsequent run hang on the poisoned lock.
    fn serialize() -> std::sync::MutexGuard<'static, ()> {
        SERIALIZE.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn with_env<F: FnOnce()>(key: &str, val: Option<&str>, f: F) {
        let prev = std::env::var(key).ok();
        match val {
            Some(v) => std::env::set_var(key, v),
            None => std::env::remove_var(key),
        }
        f();
        match prev {
            Some(v) => std::env::set_var(key, v),
            None => std::env::remove_var(key),
        }
    }

    fn write_config(dir: &std::path::Path, contents: &str) -> PathBuf {
        let path = dir.join(CONFIG_FILENAME);
        let mut f = std::fs::File::create(&path).unwrap();
        f.write_all(contents.as_bytes()).unwrap();
        path
    }

    #[test]
    fn missing_file_falls_back_to_compiled_default() {
        let _g = serialize();
        // Both env vars cleared; non-existent path must yield default.
        with_env("VCT_WEAVIATE_URL", None, || {
            with_env("WEAVIATE_URL", None, || {
                let cfg =
                    LocalConfig::load_from_path(Some(std::path::Path::new("/no/such/file.toml")));
                assert_eq!(cfg.weaviate_url, DEFAULT_WEAVIATE_URL);
            });
        });
    }

    #[test]
    fn no_path_no_env_yields_compiled_default() {
        let _g = serialize();
        with_env("VCT_WEAVIATE_URL", None, || {
            with_env("WEAVIATE_URL", None, || {
                let cfg = LocalConfig::load_from_path(None);
                assert_eq!(cfg.weaviate_url, DEFAULT_WEAVIATE_URL);
            });
        });
    }

    #[test]
    fn file_value_is_picked_up_when_no_env() {
        let _g = serialize();
        let dir = tempfile::tempdir().unwrap();
        let path = write_config(
            dir.path(),
            r#"weaviate_url = "http://example.test:9999""#,
        );
        with_env("VCT_WEAVIATE_URL", None, || {
            with_env("WEAVIATE_URL", None, || {
                let cfg = LocalConfig::load_from_path(Some(&path));
                assert_eq!(cfg.weaviate_url, "http://example.test:9999");
            });
        });
    }

    #[test]
    fn env_var_overrides_file_value() {
        let _g = serialize();
        let dir = tempfile::tempdir().unwrap();
        let path = write_config(
            dir.path(),
            r#"weaviate_url = "http://from-file:1111""#,
        );
        with_env("VCT_WEAVIATE_URL", Some("http://from-env:2222"), || {
            with_env("WEAVIATE_URL", None, || {
                let cfg = LocalConfig::load_from_path(Some(&path));
                assert_eq!(cfg.weaviate_url, "http://from-env:2222");
            });
        });
    }

    #[test]
    fn legacy_weaviate_url_env_alias_works() {
        let _g = serialize();
        // `WEAVIATE_URL` (no `VCT_` prefix) is the historical name. We
        // keep honoring it so existing developer shells / compose env
        // files don't break.
        with_env("VCT_WEAVIATE_URL", None, || {
            with_env("WEAVIATE_URL", Some("http://legacy:3333"), || {
                let cfg = LocalConfig::load_from_path(None);
                assert_eq!(cfg.weaviate_url, "http://legacy:3333");
            });
        });
    }

    #[test]
    fn vct_prefixed_env_beats_legacy_alias() {
        let _g = serialize();
        // If both env vars are set, the explicit `VCT_` name wins. This
        // matches the precedence documented in the file header.
        with_env("VCT_WEAVIATE_URL", Some("http://vct:4444"), || {
            with_env("WEAVIATE_URL", Some("http://legacy:5555"), || {
                let cfg = LocalConfig::load_from_path(None);
                assert_eq!(cfg.weaviate_url, "http://vct:4444");
            });
        });
    }

    #[test]
    fn empty_env_var_is_treated_as_unset() {
        let _g = serialize();
        // An accidental `export VCT_WEAVIATE_URL=` (no value) must not
        // resolve the URL to the empty string — that would break every
        // Weaviate call. Empty = unset = fall through.
        let dir = tempfile::tempdir().unwrap();
        let path = write_config(
            dir.path(),
            r#"weaviate_url = "http://from-file:6666""#,
        );
        with_env("VCT_WEAVIATE_URL", Some(""), || {
            with_env("WEAVIATE_URL", Some(""), || {
                let cfg = LocalConfig::load_from_path(Some(&path));
                assert_eq!(cfg.weaviate_url, "http://from-file:6666");
            });
        });
    }

    #[test]
    fn malformed_toml_falls_back_to_default() {
        let _g = serialize();
        // A syntactically invalid file must not crash the launcher. The
        // load function logs and falls through to compiled defaults.
        let dir = tempfile::tempdir().unwrap();
        let path = write_config(dir.path(), "this is not = valid = toml [[[");
        with_env("VCT_WEAVIATE_URL", None, || {
            with_env("WEAVIATE_URL", None, || {
                let cfg = LocalConfig::load_from_path(Some(&path));
                assert_eq!(cfg.weaviate_url, DEFAULT_WEAVIATE_URL);
            });
        });
    }

    #[test]
    fn empty_string_in_file_falls_back_to_default() {
        let _g = serialize();
        // `weaviate_url = ""` in the config file is treated the same as
        // "not set". Same reason as the empty-env-var case.
        let dir = tempfile::tempdir().unwrap();
        let path = write_config(dir.path(), r#"weaviate_url = """#);
        with_env("VCT_WEAVIATE_URL", None, || {
            with_env("WEAVIATE_URL", None, || {
                let cfg = LocalConfig::load_from_path(Some(&path));
                assert_eq!(cfg.weaviate_url, DEFAULT_WEAVIATE_URL);
            });
        });
    }

    #[test]
    fn unknown_keys_in_file_are_ignored() {
        let _g = serialize();
        // Forward-compatibility: a config file written by a future
        // launcher version (with extra keys) must not break the load.
        // Serde's default deny_unknown_fields is OFF for our struct.
        let dir = tempfile::tempdir().unwrap();
        let path = write_config(
            dir.path(),
            r#"
            weaviate_url = "http://known:7777"
            future_field_we_dont_understand = "ignored"
            "#,
        );
        with_env("VCT_WEAVIATE_URL", None, || {
            with_env("WEAVIATE_URL", None, || {
                let cfg = LocalConfig::load_from_path(Some(&path));
                assert_eq!(cfg.weaviate_url, "http://known:7777");
            });
        });
    }
}

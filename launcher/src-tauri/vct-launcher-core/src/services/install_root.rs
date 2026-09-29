// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! The ONE install-root resolver (v0.2.100 WP-02, plan AD-2; review
//! findings L2-F06 / L2-F07, incident U10 / I-04).
//!
//! Before this module the launcher answered "where is the orchestrator
//! clone this binary belongs to?" in four places with four different rules
//! (`installer::resolve_orchestrator_root`, `installer::get_known_install_path`,
//! `installer::walk_for_orchestrator_root` / `find_local_repo_root`,
//! `self_update::find_launcher_repo_root`), plus the hub's
//! `infra_watchdog::infrastructure_dir`. They disagreed in two ways that
//! mattered:
//!
//! * a binary launched from OUTSIDE its clone (copied to `/tmp`, a PATH
//!   directory, `/Applications`) found no root on the walk-only paths and
//!   degraded silently ("no orchestrator root resolved", "vct-hub binary not
//!   found", "self-update disabled");
//! * the unbounded first-`.git` walk accepted ANY git work tree above the
//!   exe — an unrelated repository could be taken for the clone.
//!
//! And during `install.py --update` the launcher's managed `launcher.db`
//! connection is a schema-less stand-in (`Db::close_for_update`), so the
//! DB-cache resolvers logged `no such table: app_state` and tried to WRITE
//! into it.
//!
//! The contract here:
//!
//! 1. [`resolve`] — the cached root first (launcher.db `launcher.install_path`,
//!    or the process-level copy of it while the DB is closed), re-checked
//!    with the same identity rule as the walk (W1R-06); then a walk up from the exe bounded to
//!    [`MAX_WALK_LEVELS`] ancestors that accepts a directory ONLY when it is
//!    identity-confirmed by [`is_orchestrator_clone`]. No hit is a typed
//!    [`RootError::NotFound`]; never a guess.
//! 2. [`InstallRoot::require_exe_inside`] — the self-update precondition. A
//!    binary outside the resolved clone gets [`RootError::ExeOutsideClone`]
//!    naming the clone and the directory to relaunch from, instead of a
//!    silent "self-update disabled".
//! 3. [`resolve_with_store`] — the DB-aware entry point. In update standby it
//!    reads ONLY the process cache and performs NO store write; outside
//!    standby it reads the DB cache under [`crate::db::Db::lock_live`], sets
//!    the process cache after every good resolution, and writes the sticky
//!    DB cache only for a root that came from the identity-checked walk.
//!
//! Cross-language: the identity rule is a rule-C mirror. The shared case
//! table is `tests/fixtures/install_root_cases.json` and the Python lock is
//! `tests/test_v02100_install_root_identity.py` — MUST match both. The Rust
//! boot path cannot shell out to `python -m vco_lib` (no venv is guaranteed
//! at launcher boot), which is why this is not rule A.

use std::path::{Path, PathBuf};
use std::sync::{OnceLock, RwLock};

/// `app_state` key holding the cached install root. The launcher's
/// `commands::installer::APP_STATE_KEY_INSTALL_PATH` re-exports this.
pub const APP_STATE_KEY_INSTALL_PATH: &str = "launcher.install_path";

/// `vct-module.json` `id` of the orchestrator core. A manifest with any
/// other id belongs to some other VCT module and never identifies the clone.
pub const ORCHESTRATOR_MODULE_ID: &str = "orchestrator";

/// Ancestor levels the exe walk inspects, starting at the exe's parent.
/// Covers the shipped dist layout (`<root>/launcher/dist/<arch>/exe`, level
/// 3), the cargo layout (`<root>/launcher/src-tauri/target/<profile>/exe`,
/// level 4) and a macOS `.app` inside dist (level 6) with headroom.
pub const MAX_WALK_LEVELS: usize = 8;

// ─── identity ────────────────────────────────────────────────────────────

/// Structural marker check: `vct-module.json`, OR `install.py` +
/// `CLAUDE.md`. Necessary but NOT sufficient for identity (a user project
/// may carry an `install.py` and a `CLAUDE.md`); see [`is_orchestrator_clone`].
pub fn looks_like_orchestrator_root(dir: &Path) -> bool {
    dir.join("vct-module.json").is_file()
        || (dir.join("install.py").is_file() && dir.join("CLAUDE.md").is_file())
}

/// The `id` of `<dir>/vct-module.json`, or `None` when the file is absent,
/// unreadable, unparseable, or has no string `id`.
pub fn manifest_id(dir: &Path) -> Option<String> {
    let raw = std::fs::read_to_string(dir.join("vct-module.json")).ok()?;
    let v: serde_json::Value = serde_json::from_str(&raw).ok()?;
    v.get("id")?.as_str().map(str::to_string)
}

/// Identity rule: `dir` IS an orchestrator clone. Structural markers AND a
/// `vct-module.json` whose `id` is exactly [`ORCHESTRATOR_MODULE_ID`].
///
/// MUST match `tests/fixtures/install_root_cases.json` `identity` rows and
/// the Python statement of the rule in
/// `tests/test_v02100_install_root_identity.py`.
pub fn is_orchestrator_clone(dir: &Path) -> bool {
    looks_like_orchestrator_root(dir)
        && manifest_id(dir).as_deref() == Some(ORCHESTRATOR_MODULE_ID)
}

/// Bounded, identity-checked walk from `exe`'s parent. Returns the NEAREST
/// ancestor (at most [`MAX_WALK_LEVELS`] levels) that [`is_orchestrator_clone`].
pub fn walk_from_exe(exe: &Path) -> Option<PathBuf> {
    let mut current = exe.parent()?.to_path_buf();
    for _ in 0..MAX_WALK_LEVELS {
        if is_orchestrator_clone(&current) {
            return Some(current);
        }
        if !current.pop() {
            break;
        }
    }
    None
}

// ─── path containment (Windows verbatim-aware) ───────────────────────────

/// Strip Windows' `\\?\` verbatim prefix (and rebuild the `\\?\UNC\` form
/// as `\\server\share`). No-op on any other input.
///
/// NOTE: `launcher::commands::orchestrator_root` holds a private copy of
/// this (`strip_windows_verbatim_prefix`) that predates this module; it
/// should delegate here (not in WP-02's file set — reported).
pub fn strip_windows_verbatim_prefix(s: &str) -> String {
    if let Some(rest) = s.strip_prefix(r"\\?\UNC\") {
        format!(r"\\{}", rest)
    } else if let Some(rest) = s.strip_prefix(r"\\?\") {
        rest.to_string()
    } else {
        s.to_string()
    }
}

/// Normalise a path string for containment: verbatim prefix stripped, `\`
/// folded to `/`, trailing separators dropped; Windows-shaped paths (drive
/// letter or UNC) are also ASCII-lowercased because Win32 paths are
/// case-insensitive. POSIX paths keep their case (conservative: a mismatch
/// reads as "outside", which refuses a self-update rather than risking one).
fn containment_key(p: &str) -> String {
    let stripped = strip_windows_verbatim_prefix(p);
    let b = stripped.as_bytes();
    let windows_shaped = (b.len() >= 2 && b[0].is_ascii_alphabetic() && b[1] == b':')
        || stripped.starts_with(r"\\");
    let mut s = stripped.replace('\\', "/");
    while s.len() > 1 && s.ends_with('/') {
        s.pop();
    }
    if windows_shaped {
        s.make_ascii_lowercase();
    }
    s
}

/// Is `exe` located under `root` (or equal to it)? String-level so the
/// Windows verbatim / UNC shapes are decidable on every host. MUST match the
/// `containment` rows of `tests/fixtures/install_root_cases.json`.
pub fn exe_is_inside(root: &Path, exe: &Path) -> bool {
    let r = containment_key(&root.to_string_lossy());
    let e = containment_key(&exe.to_string_lossy());
    if r.is_empty() {
        return false;
    }
    e == r || e.starts_with(&format!("{}/", r.trim_end_matches('/')))
}

// ─── resolver ────────────────────────────────────────────────────────────

/// Where a resolved root came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RootSource {
    /// The cached value (launcher.db, or its process-level copy in standby).
    DbCache,
    /// The identity-checked walk up from the running exe.
    ExeWalk,
}

/// A resolved install root plus the exe it was resolved for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstallRoot {
    pub path: PathBuf,
    pub source: RootSource,
    pub exe: PathBuf,
}

impl InstallRoot {
    /// True when the running binary lives inside the resolved clone.
    pub fn exe_inside(&self) -> bool {
        exe_is_inside(&self.path, &self.exe)
    }

    /// The self-update precondition: the running binary must belong to the
    /// clone it would update. Otherwise a typed refusal naming the clone and
    /// the directory to relaunch from.
    pub fn require_exe_inside(&self) -> Result<&Path, RootError> {
        if self.exe_inside() {
            Ok(&self.path)
        } else {
            Err(RootError::ExeOutsideClone {
                clone: self.path.clone(),
                exe: self.exe.clone(),
            })
        }
    }
}

/// Typed resolver failure. Never degraded to a silent `None` inside this
/// module; the thin launcher shims decide what `None` means for them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RootError {
    /// No usable cache and no identity-confirmed clone above the exe.
    NotFound { exe: PathBuf },
    /// A clone was resolved, but the running binary is not inside it.
    ExeOutsideClone { clone: PathBuf, exe: PathBuf },
    /// The launcher.db cache read failed (not standby — a real error) and
    /// the walk found nothing either.
    Db(String),
}

impl RootError {
    /// The directory the user should relaunch the launcher from, when known.
    pub fn relaunch_dir(&self) -> Option<PathBuf> {
        match self {
            RootError::ExeOutsideClone { clone, .. } => Some(dist_dir(clone)),
            _ => None,
        }
    }
}

/// `<root>/launcher/dist` — where the shipped launcher binaries live (one
/// per-platform subdirectory below it).
pub fn dist_dir(root: &Path) -> PathBuf {
    root.join("launcher").join("dist")
}

impl std::fmt::Display for RootError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RootError::NotFound { exe } => write!(
                f,
                "no orchestrator clone found: launcher.db has no valid install path and no \
                 ancestor (within {} levels) of {} is an orchestrator clone (vct-module.json \
                 with id \"{}\")",
                MAX_WALK_LEVELS,
                exe.display(),
                ORCHESTRATOR_MODULE_ID
            ),
            RootError::ExeOutsideClone { clone, exe } => write!(
                f,
                "this launcher binary ({}) is not inside the orchestrator clone it belongs to \
                 ({}); relaunch the launcher from {} so it can update itself",
                exe.display(),
                clone.display(),
                dist_dir(clone).display()
            ),
            RootError::Db(e) => write!(f, "launcher.db install-path read failed: {}", e),
        }
    }
}

impl std::error::Error for RootError {}

/// Pure resolver: cached root first, then the bounded identity-checked walk.
///
/// The cache is held to the SAME identity rule as the walk
/// ([`is_orchestrator_clone`] — structural markers AND a `vct-module.json`
/// whose id is `orchestrator`). It used to be accepted on the structural
/// markers alone (v0.2.100 wave-1 review W1R-06), on the argument that it was
/// written by install.py or an identity-checked walk. Neither holds for two
/// real sources: a `launcher.install_path` written by a pre-0.2.100 launcher
/// from the OLD structural walk, and the hub's `VCT_ORCHESTRATOR_ROOT` /
/// `VCT_INSTALL_ROOT` / project-row candidates — a user project carrying
/// `install.py` + `CLAUDE.md` (and a bundled compose copy) passed the
/// structural check and stuck as "the clone" forever. A cache that fails
/// identity is logged and falls through to the walk; it is never rewritten
/// here (the DB-aware entry point writes back only a walk hit).
///
/// The launcher layers its finished-install check on top before passing the
/// cache in.
pub fn resolve(db_cached: Option<PathBuf>, exe: &Path) -> Result<InstallRoot, RootError> {
    if let Some(cached) = db_cached {
        if !cached.as_os_str().is_empty() {
            if is_orchestrator_clone(&cached) {
                return Ok(InstallRoot {
                    path: cached,
                    source: RootSource::DbCache,
                    exe: exe.to_path_buf(),
                });
            }
            tracing::warn!(
                "[vct] install_root: cached root {} is not an orchestrator clone \
                 (needs vct-module.json with id \"{}\"); ignoring it and walking from {}",
                cached.display(),
                ORCHESTRATOR_MODULE_ID,
                exe.display()
            );
        }
    }
    match walk_from_exe(exe) {
        Some(path) => Ok(InstallRoot {
            path,
            source: RootSource::ExeWalk,
            exe: exe.to_path_buf(),
        }),
        None => Err(RootError::NotFound {
            exe: exe.to_path_buf(),
        }),
    }
}

/// Resolver for callers with no DB handle: the exe walk first (a binary
/// inside a clone belongs to THAT clone), then the process cache (so a
/// binary outside its clone still finds the root the DB named at boot).
pub fn resolve_without_db(exe: &Path) -> Result<InstallRoot, RootError> {
    match resolve(None, exe) {
        Ok(r) => Ok(r),
        Err(e) => match process_root() {
            Some(cached) => resolve(Some(cached), exe).map_err(|_| e),
            None => Err(e),
        },
    }
}

/// [`resolve_without_db`] for the running binary.
pub fn resolve_current_exe_without_db() -> Result<InstallRoot, RootError> {
    let exe = std::env::current_exe().map_err(|e| RootError::NotFound {
        exe: PathBuf::from(format!("<current_exe unavailable: {}>", e)),
    })?;
    resolve_without_db(&exe)
}

// ─── process-level cache ─────────────────────────────────────────────────

static PROCESS_ROOT: OnceLock<RwLock<Option<PathBuf>>> = OnceLock::new();

fn process_cell() -> &'static RwLock<Option<PathBuf>> {
    PROCESS_ROOT.get_or_init(|| RwLock::new(None))
}

/// The root last resolved from a good launcher.db read (or a walk), if any.
/// This is what the resolvers answer from while launcher.db is closed.
pub fn process_root() -> Option<PathBuf> {
    process_cell()
        .read()
        .unwrap_or_else(|p| p.into_inner())
        .clone()
}

/// Record `root` as the process-level cache. Memory only; never touches
/// launcher.db.
pub fn set_process_root(root: &Path) {
    *process_cell().write().unwrap_or_else(|p| p.into_inner()) = Some(root.to_path_buf());
}

// ─── DB-aware entry point ────────────────────────────────────────────────

/// The persistence the DB-aware resolver needs. `Db` implements it; tests
/// substitute a fake that fails on any write it did not expect.
pub trait RootStore {
    /// Is the managed connection the update-window stand-in?
    fn is_update_standby(&self) -> bool;
    /// Read the cached install path. `Err` for a real DB failure.
    fn read_cached_root(&self) -> Result<Option<String>, String>;
    /// Write the cached install path (sticky cache).
    fn write_cached_root(&self, value: &str) -> Result<(), String>;
}

impl RootStore for crate::db::Db {
    fn is_update_standby(&self) -> bool {
        crate::db::Db::is_update_standby(self)
    }

    // The statements below are `Db::app_state_get` / `Db::app_state_set`
    // (db/app_state.rs) executed under `lock_live`, so the standby check and
    // the statement share ONE guard: a `close_for_update` cannot slip in
    // between them and turn the read or the write into `no such table`.
    fn read_cached_root(&self) -> Result<Option<String>, String> {
        let guard = self.lock_live().map_err(|e| e.to_string())?;
        match guard.query_row(
            "SELECT value FROM app_state WHERE key = ?1",
            rusqlite::params![APP_STATE_KEY_INSTALL_PATH],
            |r| r.get::<_, String>(0),
        ) {
            Ok(v) => Ok(Some(v)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(format!("app_state_get({}): {}", APP_STATE_KEY_INSTALL_PATH, e)),
        }
    }

    fn write_cached_root(&self, value: &str) -> Result<(), String> {
        let now = chrono::Utc::now().timestamp_millis();
        let guard = self.lock_live().map_err(|e| e.to_string())?;
        guard
            .execute(
                "INSERT INTO app_state (key, value, updated_at)
                 VALUES (?1, ?2, ?3)
                 ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at",
                rusqlite::params![APP_STATE_KEY_INSTALL_PATH, value, now],
            )
            .map_err(|e| format!("app_state_set({}): {}", APP_STATE_KEY_INSTALL_PATH, e))?;
        Ok(())
    }
}

/// DB-aware resolution — the body of every launcher resolver.
///
/// * Standby (`install.py` holds launcher.db): the cache is the PROCESS
///   cache; the store is neither read nor written.
/// * Otherwise: the DB cache (kept only when `cache_ok` accepts it — the
///   launcher passes its finished-install check), then the walk. Every good
///   resolution refreshes the process cache; a walk hit is also written back
///   as the sticky DB cache (a write failure is logged, never fatal).
/// * A real DB read error with no walk hit is [`RootError::Db`].
pub fn resolve_with_store<S: RootStore + ?Sized>(
    store: &S,
    exe: &Path,
    cache_ok: impl Fn(&Path) -> bool,
) -> Result<InstallRoot, RootError> {
    if store.is_update_standby() {
        let resolved = resolve(process_root(), exe)?;
        if resolved.source == RootSource::ExeWalk {
            set_process_root(&resolved.path);
        }
        return Ok(resolved);
    }

    let (db_cached, read_err) = match store.read_cached_root() {
        Ok(Some(s)) if !s.is_empty() && cache_ok(Path::new(&s)) => (Some(PathBuf::from(s)), None),
        Ok(_) => (None, None),
        Err(e) => (None, Some(e)),
    };

    let resolved = match resolve(db_cached, exe) {
        Ok(r) => r,
        Err(nf) => {
            return Err(match read_err {
                Some(e) => RootError::Db(e),
                None => nf,
            })
        }
    };
    set_process_root(&resolved.path);
    if resolved.source == RootSource::ExeWalk {
        if let Err(e) = store.write_cached_root(&resolved.path.to_string_lossy()) {
            tracing::warn!(
                "[vct] install_root: failed to cache install_path {}: {}",
                resolved.path.display(),
                e
            );
        }
    }
    Ok(resolved)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};

    fn fixture() -> serde_json::Value {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../tests/fixtures/install_root_cases.json");
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        serde_json::from_str(&text).expect("fixture parses")
    }

    fn rel(base: &Path, p: &str) -> PathBuf {
        p.split('/').fold(base.to_path_buf(), |acc, c| acc.join(c))
    }

    fn plant(base: &Path, files: &serde_json::Value) {
        for (p, content) in files.as_object().expect("files object") {
            let target = rel(base, p);
            std::fs::create_dir_all(target.parent().unwrap()).unwrap();
            let body = match content.as_str().unwrap() {
                "@orchestrator" => r#"{"id": "orchestrator", "version": "0.0.0"}"#,
                other => other,
            };
            std::fs::write(&target, body).unwrap();
        }
    }

    #[test]
    fn identity_rows_match_the_shared_fixture() {
        let fx = fixture();
        let rows = fx["identity"].as_array().expect("identity rows");
        assert!(rows.len() >= 10, "identity corpus shrank");
        for row in rows {
            let tmp = tempfile::tempdir().unwrap();
            plant(tmp.path(), &row["files"]);
            let dir = rel(tmp.path(), row["dir"].as_str().unwrap());
            assert_eq!(
                is_orchestrator_clone(&dir),
                row["expect"].as_bool().unwrap(),
                "identity row: {}",
                row["name"]
            );
        }
    }

    #[test]
    fn resolve_rows_match_the_shared_fixture() {
        let fx = fixture();
        let rows = fx["resolve"].as_array().expect("resolve rows");
        assert!(rows.len() >= 14, "resolve corpus shrank");
        for row in rows {
            let name = row["name"].as_str().unwrap();
            let tmp = tempfile::tempdir().unwrap();
            plant(tmp.path(), &row["files"]);
            let exe = rel(tmp.path(), row["exe"].as_str().unwrap());
            let cached = row["db_cached"].as_str().map(|p| rel(tmp.path(), p));
            let got = resolve(cached, &exe);
            let want = &row["expect"];
            match want["kind"].as_str().unwrap() {
                "ok" => {
                    let r = got.unwrap_or_else(|e| panic!("{}: expected ok, got {:?}", name, e));
                    assert_eq!(r.path, rel(tmp.path(), want["root"].as_str().unwrap()), "{}", name);
                    let src = match r.source {
                        RootSource::DbCache => "db_cache",
                        RootSource::ExeWalk => "exe_walk",
                    };
                    assert_eq!(src, want["source"].as_str().unwrap(), "{}", name);
                    let inside = want["exe_inside"].as_bool().unwrap();
                    assert_eq!(r.exe_inside(), inside, "{}", name);
                    match r.require_exe_inside() {
                        Ok(p) => {
                            assert!(inside, "{}", name);
                            assert_eq!(p, r.path.as_path());
                        }
                        Err(e) => {
                            assert!(!inside, "{}", name);
                            assert_eq!(
                                e,
                                RootError::ExeOutsideClone { clone: r.path.clone(), exe: exe.clone() },
                                "{}",
                                name
                            );
                            assert_eq!(e.relaunch_dir(), Some(r.path.join("launcher").join("dist")));
                            let msg = e.to_string();
                            assert!(msg.contains(&r.path.join("launcher").join("dist").display().to_string()), "{}", msg);
                        }
                    }
                }
                "not_found" => {
                    assert_eq!(got, Err(RootError::NotFound { exe: exe.clone() }), "{}", name);
                }
                k => panic!("unknown expect.kind {}", k),
            }
        }
    }

    #[test]
    fn containment_rows_match_the_shared_fixture() {
        let fx = fixture();
        let rows = fx["containment"].as_array().expect("containment rows");
        assert!(rows.len() >= 10, "containment corpus shrank");
        for row in rows {
            assert_eq!(
                exe_is_inside(
                    Path::new(row["root"].as_str().unwrap()),
                    Path::new(row["exe"].as_str().unwrap())
                ),
                row["inside"].as_bool().unwrap(),
                "containment row: {}",
                row["name"]
            );
        }
    }

    #[test]
    fn strip_verbatim_forms() {
        assert_eq!(strip_windows_verbatim_prefix(r"\\?\C:\vco"), r"C:\vco");
        assert_eq!(strip_windows_verbatim_prefix(r"\\?\UNC\srv\share"), r"\\srv\share");
        assert_eq!(strip_windows_verbatim_prefix("/opt/vco"), "/opt/vco");
    }

    // ── store-level behaviour (standby / cache write) ──────────────────

    /// Fake store. `standby` flips the standby probe; `cached` is the DB row;
    /// `forbid_io` makes ANY read or write fail the test (the standby proof).
    struct FakeStore {
        standby: bool,
        cached: RefCell<Option<String>>,
        forbid_io: bool,
        writes: Cell<usize>,
        read_error: Option<String>,
    }

    impl FakeStore {
        fn new(standby: bool, cached: Option<&str>) -> Self {
            FakeStore {
                standby,
                cached: RefCell::new(cached.map(str::to_string)),
                forbid_io: standby,
                writes: Cell::new(0),
                read_error: None,
            }
        }
    }

    impl RootStore for FakeStore {
        fn is_update_standby(&self) -> bool {
            self.standby
        }
        fn read_cached_root(&self) -> Result<Option<String>, String> {
            assert!(!self.forbid_io, "resolver READ launcher.db while it was closed for an update");
            if let Some(e) = &self.read_error {
                return Err(e.clone());
            }
            Ok(self.cached.borrow().clone())
        }
        fn write_cached_root(&self, value: &str) -> Result<(), String> {
            assert!(!self.forbid_io, "resolver WROTE launcher.db while it was closed for an update");
            self.writes.set(self.writes.get() + 1);
            *self.cached.borrow_mut() = Some(value.to_string());
            Ok(())
        }
    }

    fn clone_tree() -> (tempfile::TempDir, PathBuf) {
        let tmp = tempfile::tempdir().unwrap();
        let clone = tmp.path().join("clone");
        std::fs::create_dir_all(&clone).unwrap();
        std::fs::write(clone.join("vct-module.json"), r#"{"id": "orchestrator"}"#).unwrap();
        (tmp, clone)
    }

    /// The process cache is global; every test that touches it serialises.
    static PROCESS_CACHE_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    #[test]
    fn standby_returns_the_process_cached_root_and_touches_no_store() {
        let _g = PROCESS_CACHE_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let (tmp, clone) = clone_tree();
        set_process_root(&clone);
        let store = FakeStore::new(true, None);
        // Exe OUTSIDE the clone: only the process cache can answer.
        let exe = tmp.path().join("tmpbin").join("vct-launcher");
        let r = resolve_with_store(&store, &exe, |_| true).expect("cached root in standby");
        assert_eq!(r.path, clone);
        assert_eq!(r.source, RootSource::DbCache);
        assert_eq!(store.writes.get(), 0);
    }

    #[test]
    fn standby_with_an_empty_process_cache_is_not_found_and_still_writes_nothing() {
        let _g = PROCESS_CACHE_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        *process_cell().write().unwrap() = None;
        let tmp = tempfile::tempdir().unwrap();
        let store = FakeStore::new(true, None);
        let exe = tmp.path().join("tmpbin").join("vct-launcher");
        assert!(matches!(
            resolve_with_store(&store, &exe, |_| true),
            Err(RootError::NotFound { .. })
        ));
        assert_eq!(store.writes.get(), 0);
    }

    #[test]
    fn standby_with_a_real_closed_db_returns_the_cached_root() {
        let _g = PROCESS_CACHE_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let (tmp, clone) = clone_tree();
        set_process_root(&clone);
        let db = crate::db::Db::open_in_memory().unwrap();
        assert!(!db.is_update_standby(), "a migrated in-memory test Db is live");
        db.close_for_update().unwrap();
        assert!(db.is_update_standby());
        let exe = tmp.path().join("tmpbin").join("vct-launcher");
        let r = resolve_with_store(&db, &exe, |_| true).expect("standby answers from the process cache");
        assert_eq!(r.path, clone);
        // And the typed guard is what a direct write attempt now meets.
        assert!(db.lock_live().is_err());
    }

    #[test]
    fn walk_hit_is_written_back_act() {
        let _g = PROCESS_CACHE_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let (_tmp, clone) = clone_tree();
        let store = FakeStore::new(false, None);
        let exe = clone.join("launcher").join("dist").join("linux-x64").join("vct-launcher");
        let r = resolve_with_store(&store, &exe, |_| true).unwrap();
        assert_eq!(r.source, RootSource::ExeWalk);
        assert_eq!(store.writes.get(), 1, "a walk hit becomes the sticky cache");
        assert_eq!(store.cached.borrow().as_deref(), Some(clone.to_string_lossy().as_ref()));
        assert_eq!(process_root(), Some(clone));
    }

    #[test]
    fn valid_db_cache_is_not_rewritten_leave_alone() {
        let _g = PROCESS_CACHE_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let (tmp, clone) = clone_tree();
        let store = FakeStore::new(false, Some(&clone.to_string_lossy()));
        let exe = tmp.path().join("tmpbin").join("vct-launcher");
        let r = resolve_with_store(&store, &exe, |_| true).unwrap();
        assert_eq!(r.source, RootSource::DbCache);
        assert_eq!(store.writes.get(), 0, "a cache hit is never rewritten");
        assert_eq!(process_root(), Some(clone), "a good DB read refreshes the process cache");
    }

    #[test]
    fn not_found_writes_nothing() {
        let _g = PROCESS_CACHE_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let tmp = tempfile::tempdir().unwrap();
        let store = FakeStore::new(false, None);
        let exe = tmp.path().join("tmpbin").join("vct-launcher");
        assert!(matches!(
            resolve_with_store(&store, &exe, |_| true),
            Err(RootError::NotFound { .. })
        ));
        assert_eq!(store.writes.get(), 0);
    }

    #[test]
    fn cache_rejected_by_the_caller_check_falls_through_to_the_walk() {
        let _g = PROCESS_CACHE_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let (_tmp, clone) = clone_tree();
        let store = FakeStore::new(false, Some(&clone.to_string_lossy()));
        let exe = clone.join("launcher").join("dist").join("linux-x64").join("vct-launcher");
        let r = resolve_with_store(&store, &exe, |_| false).unwrap();
        assert_eq!(r.source, RootSource::ExeWalk);
    }

    #[test]
    fn db_read_error_without_a_walk_hit_is_typed() {
        let _g = PROCESS_CACHE_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let tmp = tempfile::tempdir().unwrap();
        let mut store = FakeStore::new(false, None);
        store.read_error = Some("disk I/O error".into());
        let exe = tmp.path().join("tmpbin").join("vct-launcher");
        assert_eq!(
            resolve_with_store(&store, &exe, |_| true),
            Err(RootError::Db("disk I/O error".into()))
        );
    }

    // ── W1R-06: the cache is held to the identity rule ────────────────

    /// A pre-0.2.100 launcher wrote `launcher.install_path` from the OLD
    /// structural walk: a user project with `install.py` + `CLAUDE.md` (and a
    /// bundled compose copy) could be cached as "the clone". Act: the walk
    /// finds the real clone, and its hit REPLACES the stale cache.
    #[test]
    fn stale_structural_only_db_cache_falls_through_and_is_replaced_act() {
        let _g = PROCESS_CACHE_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let (tmp, clone) = clone_tree();
        let proj = tmp.path().join("proj");
        std::fs::create_dir_all(proj.join("infrastructure")).unwrap();
        std::fs::write(proj.join("install.py"), "").unwrap();
        std::fs::write(proj.join("CLAUDE.md"), "").unwrap();
        std::fs::write(proj.join("infrastructure").join("docker-compose.yml"), "services: {}\n").unwrap();
        assert!(looks_like_orchestrator_root(&proj), "the fixture must pass the OLD structural rule");
        let store = FakeStore::new(false, Some(&proj.to_string_lossy()));
        let exe = clone.join("launcher").join("dist").join("linux-x64").join("vct-launcher");
        let r = resolve_with_store(&store, &exe, |_| true).unwrap();
        assert_eq!(r.path, clone);
        assert_eq!(r.source, RootSource::ExeWalk);
        assert_eq!(store.writes.get(), 1, "the identity-checked walk hit replaces the stale cache");
        assert_eq!(store.cached.borrow().as_deref(), Some(clone.to_string_lossy().as_ref()));
        assert_eq!(process_root(), Some(clone));
    }

    /// Leave-alone: a stale structural-only cache and an exe outside every
    /// clone is NotFound — the project is never returned, and nothing is
    /// written (the stale row is not "confirmed" by a rewrite).
    #[test]
    fn stale_structural_only_db_cache_without_a_walk_hit_is_not_found_leave_alone() {
        let _g = PROCESS_CACHE_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let tmp = tempfile::tempdir().unwrap();
        let proj = tmp.path().join("proj");
        std::fs::create_dir_all(&proj).unwrap();
        std::fs::write(proj.join("install.py"), "").unwrap();
        std::fs::write(proj.join("CLAUDE.md"), "").unwrap();
        let store = FakeStore::new(false, Some(&proj.to_string_lossy()));
        let exe = tmp.path().join("tmpbin").join("vct-launcher");
        assert_eq!(
            resolve_with_store(&store, &exe, |_| true),
            Err(RootError::NotFound { exe: exe.clone() })
        );
        assert_eq!(store.writes.get(), 0);
    }

    /// The standby path answers from the PROCESS cache — it gets the same
    /// identity check (a non-clone process cache is never handed out).
    #[test]
    fn standby_process_cache_failing_identity_is_not_returned() {
        let _g = PROCESS_CACHE_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let tmp = tempfile::tempdir().unwrap();
        let proj = tmp.path().join("proj");
        std::fs::create_dir_all(&proj).unwrap();
        std::fs::write(proj.join("vct-module.json"), r#"{"id": "dotfiles"}"#).unwrap();
        set_process_root(&proj);
        let store = FakeStore::new(true, None);
        let exe = tmp.path().join("tmpbin").join("vct-launcher");
        assert!(matches!(
            resolve_with_store(&store, &exe, |_| true),
            Err(RootError::NotFound { .. })
        ));
        *process_cell().write().unwrap() = None;
    }

    // ── W1R-16: the update stand-in must stay schema-less ─────────────
    //
    // `is_update_standby` is "in-memory AND empty schema". It is correct only
    // while NO `Db` method issues DDL after `open`: one lazy
    // `CREATE TABLE IF NOT EXISTS` on the managed connection would flip the
    // stand-in to "live" mid-window — `lock_live` would hand it out, writes
    // would land in it and vanish, and `resolve_with_store` would read
    // `app_state` from it (`no such table`).

    /// The lever is real: DDL on a stand-in DOES flip the probe. (This is
    /// why the census below exists.)
    #[test]
    fn ddl_on_the_standin_flips_the_standby_probe() {
        let db = crate::db::Db::open_in_memory().unwrap();
        db.close_for_update().unwrap();
        assert!(db.is_update_standby());
        db.ensure_change_log().unwrap();
        assert!(!db.is_update_standby(), "lazy DDL turned the stand-in into a 'live' DB");
    }

    /// Behaviour: the public readers/writers the update window can reach
    /// all fail on the stand-in and leave it schema-less.
    #[test]
    fn standin_stays_schema_less_through_db_methods() {
        let db = crate::db::Db::open_in_memory().unwrap();
        db.close_for_update().unwrap();
        let _ = db.app_state_get(APP_STATE_KEY_INSTALL_PATH);
        let _ = db.app_state_set(APP_STATE_KEY_INSTALL_PATH, "/x");
        let _ = db.app_state_set_nonpanicking("k", "v");
        let _ = db.app_state_get_bool("k");
        let _ = db.app_state_delete_like("k%");
        let _ = db.get_orchestrator_root_kg_collection();
        let _ = db.list_projects();
        let _ = db.list_projects_nonpanicking();
        let _ = db.get_project("p");
        let _ = db.list_project_folder_paths();
        let _ = db.ensure_live();
        let _ = RootStore::read_cached_root(&db);
        let _ = RootStore::write_cached_root(&db, "/x");
        assert!(db.is_update_standby(), "a Db method issued DDL on the update stand-in");
    }

    /// Census (the red-proof lever the review accepted): every production
    /// DDL / `execute_batch` site on the launcher.db side is on this
    /// allowlist, with the reason it cannot reach the stand-in. A new lazy
    /// `CREATE` anywhere in the launcher crates fails here until it is
    /// either moved into `migrations` or justified below.
    #[test]
    fn no_lazy_ddl_outside_migrations_census() {
        // file (relative to launcher/src-tauri) -> why it cannot reach the stand-in
        let allow: &[(&str, &str)] = &[
            (
                "vct-launcher-core/src/db/change_log.rs",
                "ensure_change_log: called only from Db::open / open_in_memory (asserted below)",
            ),
            (
                "vct-launcher-core/src/db/module_db_migrations.rs",
                "module DDL runs after a module_db_migrations ledger read that fails on the \
                 stand-in; the other hits are error-message strings",
            ),
            (
                "vct-launcher-core/src/db/project_state.rs",
                "BEGIN IMMEDIATE / COMMIT / ROLLBACK — transaction control, not DDL",
            ),
        ];
        let base = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
        let needles = [
            "create table",
            "create index",
            "create unique index",
            "create trigger",
            "create view",
            "create virtual",
            "alter table",
            "drop table",
            "execute_batch(",
        ];
        let mut hits: std::collections::BTreeSet<String> = Default::default();
        let mut stack = vec![base.join("vct-launcher-core/src"), base.join("src")];
        while let Some(dir) = stack.pop() {
            for entry in std::fs::read_dir(&dir).unwrap().flatten() {
                let p = entry.path();
                let name = p.file_name().unwrap().to_string_lossy().to_string();
                if p.is_dir() {
                    if name != "migrations" {
                        stack.push(p);
                    }
                    continue;
                }
                if !name.ends_with(".rs") || name == "migrations.rs" {
                    continue;
                }
                let text = std::fs::read_to_string(&p).unwrap();
                let rel_path = p
                    .strip_prefix(&base)
                    .unwrap()
                    .to_string_lossy()
                    .replace('\\', "/");
                for line in text.lines() {
                    if line.starts_with("#[cfg(test)]") {
                        break; // production code only
                    }
                    let t = line.trim_start();
                    if t.starts_with("//") {
                        continue;
                    }
                    let norm = t.to_ascii_lowercase().split_whitespace().collect::<Vec<_>>().join(" ");
                    if needles.iter().any(|n| norm.contains(n)) {
                        hits.insert(rel_path.clone());
                    }
                }
            }
        }
        let allowed: std::collections::BTreeSet<String> =
            allow.iter().map(|(f, _)| f.to_string()).collect();
        let unexpected: Vec<_> = hits.difference(&allowed).collect();
        assert!(
            unexpected.is_empty(),
            "DDL / execute_batch outside migrations in {:?} — a lazy DDL on the managed \
             connection flips the update stand-in to 'live' (W1R-16)",
            unexpected
        );
        // The change_log allowance holds only while its callers are the two
        // open paths.
        let mod_rs = std::fs::read_to_string(base.join("vct-launcher-core/src/db/mod.rs")).unwrap();
        let prod = mod_rs.split("\n#[cfg(test)]").next().unwrap();
        assert_eq!(prod.matches("ensure_change_log()").count(), 2);
        let mut callers = 0usize;
        let mut stack = vec![base.join("vct-launcher-core/src"), base.join("src")];
        while let Some(dir) = stack.pop() {
            for entry in std::fs::read_dir(&dir).unwrap().flatten() {
                let p = entry.path();
                if p.is_dir() {
                    stack.push(p);
                } else if p.extension().map(|e| e == "rs").unwrap_or(false) {
                    let text = std::fs::read_to_string(&p).unwrap();
                    let prod = text.split("\n#[cfg(test)]").next().unwrap();
                    callers += prod
                        .lines()
                        .filter(|l| !l.trim_start().starts_with("//"))
                        .filter(|l| l.contains(".ensure_change_log()"))
                        .count();
                }
            }
        }
        assert_eq!(callers, 2, "ensure_change_log gained a caller outside Db::open / open_in_memory");
    }

    #[test]
    fn resolve_without_db_falls_back_to_the_process_cache_only_after_the_walk() {
        let _g = PROCESS_CACHE_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let (tmp, clone) = clone_tree();
        let (_tmp2, other) = clone_tree();
        set_process_root(&other);
        // Inside `clone`: the walk wins over the process cache.
        let inside = clone.join("launcher").join("dist").join("linux-x64").join("vct-launcher");
        assert_eq!(resolve_without_db(&inside).unwrap().path, clone);
        // Outside any clone: the process cache answers (I-04).
        let outside = tmp.path().join("tmpbin").join("vct-launcher");
        let r = resolve_without_db(&outside).unwrap();
        assert_eq!(r.path, other);
        assert!(r.require_exe_inside().is_err());
    }
}

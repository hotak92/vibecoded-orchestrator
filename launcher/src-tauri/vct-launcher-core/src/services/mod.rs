//! Container-runtime infrastructure shared between launcher GUI + vct-hub.
//!
//! v0.2.21 split out of the launcher's `services/` directory. Only the
//! runtime-agnostic / Tauri-free helpers live here; the launcher's
//! `services/settings_json_watcher.rs` and `services/watcher.rs` (the
//! GUI-side supervisor) remain in the launcher crate.

pub mod picker;
pub mod runtime;

// v0.2.83 WP-B6: cross-writer file lock for the `UPDATE_DEFERRED.{md,json}`
// read-modify-write cycle. The Python emitter (`vco_lib.deferral_emit`) holds an
// exclusive `flock` on `<folder>/.claude/context/.update-deferred.lock`; the
// launcher's DIRECT `std::fs` deferral writers (which run mid-update when Python
// can't be assumed) acquire the SAME lock via `lock_folder` so the two languages
// serialize instead of clobbering each other. POSIX `flock`, best-effort no-lock
// on Windows (symmetric with the Python side). `LOCK_REL` is string-pinned to the
// Python constant by `tests/test_deferral_lock_parity.py`.
pub mod deferral_lock;

// v0.2.62: per-service adoption state (`<vct_root_dir>/services.toml`).
// MOVED here from `launcher/src-tauri/src/services/adoption.rs` so the
// hub-side infra watchdog (`vct-hub::infra_watchdog`) can read the same
// adopt/parallel/refuse decisions the launcher GUI persists, WITHOUT a
// second copy of the schema. The module is pure (serde + toml + the
// shared `crate::paths::vct_root_dir()` lookup) — it never depended on
// Tauri, only on its file location. The launcher's
// `src/services/adoption.rs` is now a thin `pub use` re-export so its
// many call-sites compile unchanged. The watchdog NEVER touches a
// service whose adoption mode is Adopt / Parallel / Refuse.
pub mod adoption;

// v0.2.47: shared per-paid-module container helpers. Previously two
// near-identical copies lived in launcher/src/commands/module_service.rs
// and vct-hub/src/module_supervisor.rs; the drift between them caused
// the supervisor-image-resolution-variant-gap bug fixed in this release.
// See knowledge/concepts/supervisor-image-resolution-variant-gap-2026-06-04.md.
pub mod container_runtime;
pub mod gpu_mode;

// v0.2.54 Track I: per-boot bearer-token primitives (generate /
// persist-0o600 / constant-time-compare / Bearer parse). Extracted
// from vct-hub's auth.rs so the launcher's diagrams local server
// (diagrams.token) reuses the same implementation as hub.token.
pub mod boot_token;

// v0.2.62: shared pause-marker mechanism for the hub-side infra watchdog.
// The CONSUMER (vct-hub::infra_watchdog) and the PRODUCER (the launcher's
// service_stop / services_stop_all commands) are SEPARATE processes; both
// must resolve the SAME `<vct_root>/state/watchdog-paused/<service>` path.
// Keeping the path logic here (not duplicated as a string in each crate)
// is what makes the deliberate-stop signal actually reach the watchdog —
// the BLOCKER-1 remediation (marker had a consumer but no producer).
pub mod watchdog_pause;

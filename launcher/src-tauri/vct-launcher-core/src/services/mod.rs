//! Container-runtime infrastructure shared between launcher GUI + vct-hub.
//!
//! v0.2.21 split out of the launcher's `services/` directory. Only the
//! runtime-agnostic / Tauri-free helpers live here; the launcher's
//! `services/settings_json_watcher.rs` and `services/watcher.rs` (the
//! GUI-side supervisor) remain in the launcher crate.

// v0.2.97: `picker` (the v0.2.7 container picker) is RETIRED. Candidate
// detection — containers, native processes, upstream-default ports, VCO-data
// fingerprints probed on each candidate's OWN port — is one Python detector,
// `vco_lib/service_detection.py`, which the launcher calls through
// `vco_lib_bridge` (`python -m vco_lib.service_endpoints candidates --json`).
pub mod runtime;

// v0.2.97 R12 (owner ruling "Consolidate now"): the ONE Rust client for
// `python -m vco_lib.runtime_reconcile decide --json` — every post-install
// Rust surface asks it for the container-runtime verdict instead of
// mirroring the reconcile rules. The pre-install onboarding wizard keeps a
// minimal version probe (see `commands::installer::detect_runtime_version`).
pub mod runtime_verdict;

// v0.2.83 WP-B6: cross-writer file lock for the `UPDATE_DEFERRED.{md,json}`
// read-modify-write cycle. The Python emitter (`vco_lib.deferral_emit`) holds an
// exclusive `flock` on `<folder>/.claude/context/.update-deferred.lock`; the
// launcher's DIRECT `std::fs` deferral writers (which run mid-update when Python
// can't be assumed) acquire the SAME lock via `lock_folder` so the two languages
// serialize instead of clobbering each other. POSIX `flock`, best-effort no-lock
// on Windows (symmetric with the Python side). `LOCK_REL` is string-pinned to the
// Python constant by `tests/test_deferral_lock_parity.py`.
pub mod deferral_lock;

// v0.2.97: `adoption` (the `<vct_root_dir>/services.toml` reader/writer) is
// RETIRED. What a service is — VCO-managed, an adopted container, an adopted
// URL — is its launcher.db `service_endpoints` row (`crate::db::service_endpoints`,
// written only by `vco_lib.service_endpoints`). The v0.2.97 update imports a
// `services.toml` once (`vco_lib.service_reconcile`) and renames it; no Rust
// code reads it any more.

// v0.2.97 (lane W): where the three core services are reached — the ONE
// resolver behind the hub's `/config` and the launcher's project env
// projection (they had computed the Weaviate URL two different ways). Mirrored
// by `vco_lib/service_endpoints.py`; both run
// `tests/fixtures/service_endpoint_parity.json`.
pub mod service_endpoints;

// v0.2.97 (service endpoints SE-4): the services snapshot — ONE wire shape
// for the launcher's `services_status` and the hub's `/services/status`
// (the hub used to hand-mirror the launcher's structs), built from the rows.
pub mod service_status;

// v0.2.97 (R7a F7): the ONE service-probe HTTP client rule — no redirect is
// followed, only a 2xx is "answers" (mirror of vco_lib/service_probe_http.py).
pub mod probe_http;

// v0.2.97 (R7b F19): the ONE client for a request to this machine — the hub,
// the gateway, a module's loopback port, VCO's own services: no proxy, no
// redirects, the caller's timeout.
pub mod loopback_http;

// v0.2.97 (SE-4 × SE-3): the `compose up` argv for an explicit service list —
// the ONE rule is Python's (`vco_lib.service_lifecycle.compose_up_args`:
// `--no-deps`, `--profile gpu` for code_embed, nothing for an empty list);
// the launcher and the hub watchdog call it, never hand-build `up -d <svc>`.
pub mod compose_args;

// v0.2.47: shared per-paid-module container helpers. Previously two
// near-identical copies lived in launcher/src/commands/module_service.rs
// and vct-hub/src/module_supervisor.rs; the drift between them caused
// the supervisor-image-resolution-variant-gap bug fixed in this release.
// See knowledge/concepts/supervisor-image-resolution-variant-gap-2026-06-04.md.
// v0.2.97 R12: the WHICH-RUNTIME decision moved to Python
// (`runtime_verdict::decide`); what stays here is the module plane's
// container plumbing (run args, image refs, pulls, the reaper) and the
// ownership guard.
pub mod container_runtime;
pub mod gpu_mode;

// v0.2.95: the model gateway's port/file/service constants and the resolution
// chain over them. Extracted from THREE Rust copies — the launcher's Services
// card, the hub's gateway supervisor, and the hub's `/services/status`
// skeleton, which had simply hard-coded 11436 and therefore reported a health
// URL nothing served on any machine whose gateway had fallen back. The module
// is pure (env + two small file reads via `crate::paths::vct_root_dir`) and
// carries the parity test against the daemon's own `model_router/config.py`.
pub mod model_gateway_port;

// v0.2.97 (lane T): where the running vct-hub listens (`hub.port` →
// `$VCT_HUB_PORT` → 7700). Moved out of `vct_hub::module_supervisor` so the
// manifest placeholder `{hub_port}` resolves through the same ladder that
// builds a container's `VCT_HUB_BASE_URL`, instead of a second copy.
pub mod hub_port;

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

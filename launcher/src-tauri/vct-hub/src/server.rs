//! Hub HTTP server — starts alongside Tauri on port 7700.
//!
//! The server runs in a background tokio task. It exposes a REST API
//! that any local app/service can call to register, send messages,
//! query data, etc.
//!
//! ─── Authentication (H5, 2026-05-08) ─────────────────────────────
//!
//! Every request to `/api/v1/*` (except `/health`) requires
//! `Authorization: Bearer <token>` where `<token>` is the value the
//! hub wrote to `<vct_root_dir>/hub.token` on startup. Same threat
//! model as `~/.vct/hub.port` — same-user-only by file mode (0o600 on
//! Unix, default ACL on Windows). See `hub::auth` for the full
//! rationale and exempt-paths discussion.

use std::net::SocketAddr;
use std::sync::Arc;
use tower_http::cors::{Any, CorsLayer};

use super::{
    api, auth, cli_api, config_api, db, infra_watchdog, lifecycle_api, mcp_tool_grants_api,
    module_db_api, module_supervisor, modules_api, project_state_api, rl_events_api, secrets_api,
    weaviate_probe,
};

const DEFAULT_PORT: u16 = 7700;

/// Start the Hub API server on a background task.
/// Returns the port it's listening on.
pub async fn start_hub_server() -> Result<u16, String> {
    let port = std::env::var("VCT_HUB_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(DEFAULT_PORT);

    let database = db::open_db().map_err(|e| format!("Failed to open hub database: {}", e))?;

    // Open a second connection to launcher.db for the module/project routes.
    // WAL mode lets this coexist with the Tauri-side Db handle.
    let launcher_db = vct_launcher_core::db::Db::open()
        .map_err(|e| format!("Failed to open launcher.db: {}", e))?;
    let launcher_state = modules_api::LauncherDbHandle(Arc::new(launcher_db));

    // v0.2.21 Step 21: hub-startup Weaviate class existence check.
    // Spawn detached — the probe issues per-class HTTP HEADs and we
    // don't want server startup blocked on Weaviate's response time
    // (a slow probe could push us past the 30s install.py /health
    // deadline). The probe writes a sidecar JSONL on completion;
    // future surfaces (resolver 503 emission, install.py post-install
    // check, GUI status banner) read it. We re-open Db here because
    // the launcher_state handle wraps it in Arc<Mutex>; the probe
    // module wants an owned Db (its own connection, WAL-safe).
    // Soft-fail: if Db::open fails here, log and skip — the server
    // still boots and the rest of the routes serve normally.
    match vct_launcher_core::db::Db::open() {
        Ok(probe_db) => {
            let local_config = vct_launcher_core::config::LocalConfig::load();
            weaviate_probe::spawn_startup_probe(probe_db, &local_config);
        }
        Err(e) => {
            eprintln!(
                "[vct-hub] weaviate_probe: cannot open launcher.db for class check ({}); skipping",
                e
            );
        }
    }

    // ── Bind FIRST (F-6, v0.2.73) ────────────────────────────────
    // The listener bind is the point of no return for "this process IS
    // the hub" — so it must precede EVERY discovery-file write. Pre-fix,
    // hub.token was written BEFORE the bind and hub.port AFTER it: two
    // interleaving starters could publish a hub.port that paired with
    // the OTHER hub's hub.token, 401-ing every resolver until a restart.
    // With the bind first, a starter that fails to bind writes NOTHING —
    // a pre-existing healthy hub's token/port files stay untouched.
    //
    // See the bind-address rationale below for the loopback-default /
    // opt-in-all-interfaces posture (E-2, v0.2.73) and the module-
    // conditional widen (v0.2.75 P1a — `resolve_hub_bind_ip`). F-6
    // ORDERING PRESERVED: the bind still happens FIRST, before any
    // discovery-file write — only the bind ADDRESS is conditional
    // (loopback default; 0.0.0.0 on user env opt-in OR while a
    // hub-consuming module is installed), never the bind-first
    // sequencing.
    let bind_ip = resolve_hub_bind_ip(&launcher_state.0);
    let addr = SocketAddr::from((bind_ip, port));
    let listener = try_bind(addr, 5).await?;
    let actual_port = listener.local_addr().unwrap().port();

    // ── Auth token (H5) ──────────────────────────────────────────
    // Generate a fresh token on every startup and persist before we
    // accept any connections (the bind above creates the socket but
    // axum::serve below is what starts accepting). If either step fails
    // we refuse to start the server: serving secrets without auth would
    // be strictly worse than the launcher being temporarily down.
    let auth_token = auth::generate_token()
        .map_err(|e| format!("Failed to generate hub auth token: {}", e))?;
    auth::write_token_file(&auth_token)
        .map_err(|e| format!("Failed to write hub.token: {}", e))?;
    let auth_state = auth::AuthState::new(auth_token);

    // Write port file so other apps can discover us. Token first, port
    // second: resolvers discover the port and then read the token, so
    // publishing in this order guarantees the (token, port) pair they
    // assemble came from the SAME process.
    write_port_file(actual_port).await;

    // v0.2.75 P1a: record the ACTUAL bind IP alongside hub.port so
    // out-of-process checks (the launcher's module-start widen check,
    // the supervisor's container→hub reachability probe) can positively
    // confirm whether the RUNNING hub is loopback-bound without probing
    // the network. Absence of this file means a pre-v0.2.75 hub —
    // readers must treat that as loopback (the conservative-era default).
    write_bind_file(bind_ip).await;

    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        // `Any` for headers wouldn't include `Authorization` in some
        // browsers' interpretations of the spec; spell it out so a
        // future browser-side client can't be tripped up by a CORS
        // preflight that strips Authorization from the allowlist.
        .allow_headers([
            axum::http::header::AUTHORIZATION,
            axum::http::header::CONTENT_TYPE,
        ]);

    // Layer order (axum applies layers in reverse-of-declaration on
    // the way IN to a request, so the LAST layer added runs FIRST):
    //
    //   request → cors → require_auth → routes → response
    //
    // Why this order:
    //   * `cors` must wrap the auth check so OPTIONS preflights get
    //     CORS headers attached even if they would otherwise 401
    //     (the auth middleware does pass OPTIONS through, but having
    //     cors as the outermost layer means the response always
    //     carries the right Access-Control-Allow-* headers).
    //   * `require_auth` must wrap the route handlers so an
    //     unauthenticated request never even reaches the secret-
    //     serving logic. The `Extension` carries `AuthState` into
    //     the middleware closure.
    let app = axum::Router::new()
        .nest("/api/v1", api::router(database))
        .nest("/api/v1", modules_api::router().with_state(launcher_state.clone()))
        .nest(
            "/api/v1",
            project_state_api::router().with_state(launcher_state.clone()),
        )
        .nest(
            "/api/v1",
            config_api::router().with_state(launcher_state.clone()),
        )
        .nest(
            "/api/v1",
            lifecycle_api::router().with_state(launcher_state.clone()),
        )
        .nest(
            "/api/v1",
            cli_api::router().with_state(launcher_state.clone()),
        )
        // Phase 1.2: per-project MCP tool-grant resolver. Mounted
        // INSIDE the hub-wide auth layer (the wrappers send the
        // standard hub.token bearer). Read-only today; Phase 1.1
        // sibling adds the write path via its own Tauri command
        // (set_project_mcp_tool_enabled).
        .nest(
            "/api/v1",
            mcp_tool_grants_api::router().with_state(launcher_state.clone()),
        )
        // v0.2.46 V47-C (Gap C): secret-migration endpoint. Mounted INSIDE
        // the hub-wide auth layer (every caller — install.py, the GUI's
        // future Secrets tab — sends the standard hub.token bearer). The
        // endpoint writes to the OS keychain via vct_launcher_core::
        // secrets::set; same threat model as the rest of /api/v1.
        .nest(
            "/api/v1",
            secrets_api::router().with_state(launcher_state.clone()),
        )
        // v0.2.47 RL-4: RL telemetry events queryable store (migration 025).
        // POST /rl/events accepts the Python writer's v3 events, INSERTs
        // into launcher.db::rl_events. GET routes serve dashboards +
        // offline_trainer. Standard hub.token bearer auth applies (same
        // layer order as the sibling routes above).
        .nest(
            "/api/v1",
            rl_events_api::router().with_state(launcher_state.clone()),
        )
        // v0.2.31: module-owned DB rows. Uses its OWN bearer-scope
        // middleware (require_module_scope) — token is the per-(module,
        // project) shared secret stored in launcher.db's
        // `module_access_tokens` table, NOT the hub-wide hub.token.
        // We mount it OUTSIDE the hub-wide auth::require_auth layer
        // (see comment chain in module_db_api::require_module_scope).
        .nest(
            "/api/v1",
            module_db_api::router().with_state(launcher_state.clone()),
        )
        // Inject the launcher_state as a request extension so the
        // module_db_api middleware can pull it without going through
        // axum's State<>-typed router. Clone here so the v0.2.49
        // resume-on-boot task below (which spawns AFTER router build)
        // can still own its own handle.
        .layer(axum::Extension(launcher_state.clone()))
        .layer(axum::middleware::from_fn(auth::require_auth))
        .layer(axum::Extension(auth_state))
        .layer(cors);

    // Bind-address rationale — 127.0.0.1 DEFAULT, 0.0.0.0 OPT-IN (E-2,
    // v0.2.73; reverses the v0.2.61 Option-H "0.0.0.0 always" default).
    //
    // The hub CAN be reached from a global module's container network
    // namespace (the RL container reads its training corpus via
    // `GET <VCT_HUB_BASE_URL>/api/v1/modules/{id}/projects/{pid}/rl/events`).
    // `host.containers.internal` resolves to different host addresses per
    // container runtime/backend (bridge gateway on rootful podman/docker,
    // the host LAN IP on rootless pasta, etc.), so the only runtime-
    // agnostic way to be reachable from containers is to listen on all
    // interfaces. BUT most installs never run a container that needs the
    // hub — for them, binding all interfaces needlessly exposes a secret-
    // serving endpoint on the LAN.
    //
    // SECURITY POSTURE: every `/api/v1/*` route is gated by
    // `auth::require_auth` against a 256-bit CSPRNG `hub.token` (and module
    // routes by the per-module ephemeral token), so the token — not the
    // bind — is the real access boundary; a peer that reaches the port
    // still gets 401 without the token. HOWEVER (E-2 finding): if
    // `hub.token` ever leaks off-host (a loose-perms backup, a CI artifact,
    // an `scp` of `$HOME`), a 0.0.0.0 bind lets a same-LAN peer siphon
    // secrets over the NETWORK with no local code execution. Loopback-only
    // removes that LAN dimension entirely while keeping the token as
    // defense-in-depth — the conservative default.
    //
    // v0.2.75 P1a: the widen is now CONDITIONAL, not manual-only. While
    // ≥1 global (hub-consuming) module is installed, the hub widens to
    // 0.0.0.0 automatically (that module's container is unreachable
    // otherwise on every OS); the state is derived from launcher.db's
    // global install rows so install/uninstall set/clear it and every
    // start path honours it. An explicit user `VCT_HUB_BIND_ALL` env
    // (either direction) always wins. See `resolve_hub_bind_ip`.
    //
    // Port-ladder note (F-6, v0.2.73): `try_bind(addr, 5)` walks to
    // port+1..+5 when the base port is occupied. Pre-fix this DEFEATED
    // the lockfile's "EADDRINUSE reveals the duplicate" assumption — a
    // racing duplicate hub silently bound port+1 and ran. The lockfile
    // claim is now genuinely atomic (`lockfile::ClaimGuard`), so a
    // contested starter exits in `main.rs` BEFORE ever reaching this
    // bind; the ladder's only remaining consumer is a genuinely foreign
    // occupant of the base port, which is the feature it was meant for
    // (discovery goes through hub.port either way).

    tokio::spawn(async move {
        if let Err(e) = axum::serve(listener, app).await {
            eprintln!("[vct-hub] Server error: {}", e);
        }
    });

    // v0.2.49 Phase 3 (hub-side supervisor auth port): the resume sweep
    // is now PRODUCTION-WIRED. The pre-v0.2.49 comment here said "Phase
    // 3+ would wire a hub-side catalog resolver and cut the launcher
    // hook over to a no-op; until that work lands the stub here
    // masqueraded as live coverage" — this is that work.
    //
    // What changed vs v0.2.40 F4:
    //   * The hardcoded `Box::new(|_id| None)` resolver is gone.
    //     `module_supervisor::real_manifest_resolver()` reads the
    //     on-disk catalog (`<vct_root_dir>/modules/<id>/vct-module.json`
    //     + `<vct_root_dir>/bundled_manifests/*.json`) and returns the
    //     first match for a requested module_id.
    //   * Both `resume_containers_on_startup` and `lifecycle_api::
    //     module_start` are now live — the supervisor is a self-
    //     sufficient code path for both boot-time and on-demand
    //     container starts.
    //
    // Precedence with the launcher-side resume:
    //   The launcher-side `commands::module_service::resume_containers_
    //   on_startup` (invoked from `lib.rs::setup()`) is preserved as a
    //   FALLBACK. Both layers are idempotent — they check `is_container_
    //   running` before starting — so a double-resume on a host where
    //   both the launcher and the hub boot in quick succession is a
    //   no-op for the second runner. The launcher-side path covers the
    //   edge case where the hub isn't running yet at launcher boot
    //   (rare on the same machine since the launcher itself spawns
    //   `vct-hub`, but possible during upgrade flows).
    //
    // Spawned as a detached task so server boot doesn't block on the
    // sweep (which shells out to podman/docker per row). The launcher_db
    // handle is cloned (cheap Arc) so the task owns its reference.
    let resume_db = launcher_state.clone();
    tokio::spawn(async move {
        module_supervisor::resume_containers_on_startup(
            &resume_db.0,
            module_supervisor::real_manifest_resolver(),
        )
        .await;
    });

    // v0.2.62: continuous infra-container watchdog. Distinct from the
    // module supervisor above (paid modules) — this one keeps the shared
    // infra stack (vco_weaviate / vco_ollama / vco_code_embed) alive when
    // a container dies or is stopped mid-session, which previously went
    // unhealed until the launcher was restarted. Self-spawns a detached
    // task (or logs + no-ops when disabled via VCT_HUB_INFRA_WATCHDOG=0),
    // soft-fails every tick, and never restarts a service the user
    // adopted / runs in parallel / refused / paused. The launcher's own
    // boot-time `services_start_all` + the SessionStart hook remain the
    // cold-start path; the watchdog is the always-on safety net.
    infra_watchdog::spawn_infra_watchdog(launcher_state.clone());

    // E-2: log the ACTUAL bind host, not a hardcoded "127.0.0.1" (the prior
    // string drifted from the real 0.0.0.0 bind). Loopback is always reachable
    // as 127.0.0.1 regardless of the bind IP, so print that for the all-
    // interfaces case too, but annotate the exposure.
    let bind_note = if bind_ip == std::net::Ipv4Addr::LOCALHOST {
        "loopback-only"
    } else {
        "all interfaces (VCT_HUB_BIND_ALL opt-in or hub-consuming module installed)"
    };
    println!(
        "[vct-hub] API server running on http://127.0.0.1:{} (bound {}: {})",
        actual_port, bind_ip, bind_note
    );
    Ok(actual_port)
}

/// Why a bind was chosen — logged at startup + used for the loud
/// module-widen banner (v0.2.75 P1a).
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub(crate) enum BindReason {
    /// `VCT_HUB_BIND_ALL` recognised-truthy — user opted in to 0.0.0.0.
    UserEnvOptIn,
    /// `VCT_HUB_BIND_ALL` set to any other value — user explicitly keeps
    /// (or forces back) loopback; overrides the module-derived widen.
    UserEnvOptOut,
    /// No env; ≥1 global (hub-consuming) module install row exists in
    /// launcher.db — supervisor-managed widen to 0.0.0.0.
    ModuleWiden,
    /// No env, no hub-consuming module — the conservative loopback
    /// default (E-2, v0.2.73).
    LoopbackDefault,
}

/// Pure bind decision (v0.2.75 P1a) — testable without env/DB plumbing.
///
/// Precedence:
///   1. `env_value` SET + recognised-truthy (`1`/`true`/`TRUE`/`yes`) →
///      `0.0.0.0` (user opt-in).
///   2. `env_value` SET to anything else → `127.0.0.1` (user opt-OUT —
///      an explicit user setting wins over the supervisor-managed widen
///      in BOTH directions; we never override the user's env).
///   3. env UNSET + a hub-consuming module installed → `0.0.0.0`
///      (supervisor-managed widen; see `resolve_hub_bind_ip` for the
///      rationale + the loud exposure log).
///   4. otherwise → `127.0.0.1` (conservative default).
///
/// MUST MATCH `vct_launcher_core::db::Db::has_global_module_install`'s
/// doc contract: the module-derived widen state is the presence of a
/// global install row; only the env can override it.
pub(crate) fn decide_hub_bind_ip(
    env_value: Option<&str>,
    hub_consuming_module_installed: bool,
) -> (std::net::Ipv4Addr, BindReason) {
    match env_value {
        Some("1") | Some("true") | Some("TRUE") | Some("yes") => {
            (std::net::Ipv4Addr::UNSPECIFIED, BindReason::UserEnvOptIn)
        }
        Some(_) => (std::net::Ipv4Addr::LOCALHOST, BindReason::UserEnvOptOut),
        None if hub_consuming_module_installed => {
            (std::net::Ipv4Addr::UNSPECIFIED, BindReason::ModuleWiden)
        }
        None => (std::net::Ipv4Addr::LOCALHOST, BindReason::LoopbackDefault),
    }
}

/// Resolve the hub's bind IP (E-2 v0.2.73; module-conditional widen
/// v0.2.75 P1a).
///
/// Default `127.0.0.1` (loopback-only) so a leaked `hub.token` cannot be
/// used to siphon secrets over the LAN. Two ways the bind widens to
/// `0.0.0.0`:
///
///   * `VCT_HUB_BIND_ALL=1` (or `true`) — explicit user opt-in. Any
///     OTHER explicit value keeps/forces loopback and also suppresses
///     the module-derived widen below (user env wins both directions).
///   * ≥1 GLOBAL module install row in launcher.db (v0.2.75 P1a) — a
///     hub-consuming module's container must reach the hub across its
///     network namespace via `host.containers.internal`, which NEVER
///     resolves to the host's own 127.0.0.1: on Linux (native podman)
///     it maps to a bridge/host-gateway IP; on macOS/Windows the
///     container runtime runs inside a VM whose "host" view is the VM
///     boundary. Loopback-only is therefore unreachable from containers
///     on ALL THREE OSes; 0.0.0.0 covers all of them. The widen state
///     lives in the install rows themselves (set by install, dropped by
///     the last uninstall, honoured by EVERY start path — install.py,
///     the SessionStart hook, the launcher GUI, and the CLI all just
///     start this binary, which re-derives the bind here).
///
/// SECURITY POSTURE when widened: every `/api/v1/*` route stays gated by
/// `auth::require_auth` against the 256-bit `hub.token` bearer (module
/// routes by the per-module ephemeral token); a LAN peer that reaches
/// the port without the token gets 401. The widen is logged LOUDLY.
///
/// Conservative on uncertainty: a launcher.db read error resolves to
/// loopback (do NOT widen on a guess) with a warning.
fn resolve_hub_bind_ip(db: &vct_launcher_core::db::Db) -> std::net::Ipv4Addr {
    let env_value = std::env::var("VCT_HUB_BIND_ALL").ok();
    let modules_present = match db.has_global_module_install() {
        Ok(v) => v,
        Err(e) => {
            eprintln!(
                "[vct-hub] WARNING: could not read module installs for the bind \
                 decision ({}); keeping the conservative loopback bind. If a \
                 hub-consuming module's container cannot reach the hub, restart \
                 the hub once launcher.db is readable.",
                e
            );
            false
        }
    };
    let (ip, reason) = decide_hub_bind_ip(env_value.as_deref(), modules_present);
    match reason {
        BindReason::ModuleWiden => {
            eprintln!(
                "[vct-hub] NOTICE: binding 0.0.0.0 (all interfaces) because a \
                 hub-consuming module is installed — its container reaches the \
                 hub via host.containers.internal, which loopback cannot serve \
                 on any OS. LAN exposure: peers on your network can REACH the \
                 port, but every /api/v1 route remains bearer-token gated \
                 (hub.token, 0600) — requests without the token get 401. The \
                 bind narrows back to 127.0.0.1 automatically on the first \
                 hub start after the last hub-consuming module is uninstalled. \
                 Set VCT_HUB_BIND_ALL=0 to force loopback anyway (the module's \
                 container will NOT be able to read its data from the hub)."
            );
        }
        BindReason::UserEnvOptOut if modules_present => {
            eprintln!(
                "[vct-hub] WARNING: VCT_HUB_BIND_ALL={} forces a loopback-only \
                 bind while a hub-consuming module is installed — its container \
                 CANNOT reach the hub (host.containers.internal never resolves \
                 to the host's 127.0.0.1). Unset VCT_HUB_BIND_ALL (or set it to \
                 1) and restart the hub to restore module data reads.",
                env_value.as_deref().unwrap_or("")
            );
        }
        _ => {}
    }
    ip
}

async fn try_bind(base_addr: SocketAddr, retries: u16) -> Result<tokio::net::TcpListener, String> {
    for offset in 0..=retries {
        let addr = SocketAddr::from((base_addr.ip(), base_addr.port() + offset));
        match tokio::net::TcpListener::bind(addr).await {
            Ok(listener) => return Ok(listener),
            Err(_) if offset < retries => continue,
            Err(e) => return Err(format!(
                "Cannot bind to ports {}-{}: {}",
                base_addr.port(),
                base_addr.port() + retries,
                e
            )),
        }
    }
    unreachable!()
}

/// Write port to `<VCT_STATE_DIR or ~/.vct>/hub.port` so apps can discover the hub.
///
/// v0.2.61 (Option H C-PORT): write atomically via a temp file + rename.
/// A plain truncating write can be observed mid-write (empty / partial) by a
/// concurrent reader (`module_service::hub_port_for_proxy`,
/// `module_supervisor::resolve_hub_base_port`) whose `parse::<u16>()` then
/// fails → wrong VCT_HUB_BASE_URL / a failed readiness probe on a healthy hub.
/// A same-directory rename is atomic on POSIX and on Windows ReplaceFile
/// semantics, so a reader sees either the old value or the new one, never a
/// torn one.
/// Discovery file recording the hub's ACTUAL bind IP (v0.2.75 P1a).
/// Sibling of `hub.port`. Readers: the launcher's module-start widen
/// check (`module_service::widen_restart_action`) and the supervisor's
/// container→hub reachability probe. Absent ⇒ pre-v0.2.75 hub ⇒ treat
/// as loopback.
pub const BIND_FILE: &str = "hub.bind";

/// Persist the bound IP to `<vct_root_dir>/hub.bind`. Same atomic
/// pid-suffixed-temp + rename discipline as `write_port_file`.
async fn write_bind_file(bind_ip: std::net::Ipv4Addr) {
    let path = vct_launcher_core::paths::vct_root_dir().join(BIND_FILE);
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await.ok();
    }
    let tmp = path.with_extension(format!("bind.tmp.{}", std::process::id()));
    if tokio::fs::write(&tmp, bind_ip.to_string()).await.is_ok() {
        if tokio::fs::rename(&tmp, &path).await.is_err() {
            tokio::fs::write(&path, bind_ip.to_string()).await.ok();
            tokio::fs::remove_file(&tmp).await.ok();
        }
    }
}

async fn write_port_file(port: u16) {
    let path = vct_launcher_core::paths::vct_root_dir().join("hub.port");

    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await.ok();
    }
    // Temp name is pid-suffixed so two hub processes racing a write don't
    // clobber each other's temp file before their respective renames.
    let tmp = path.with_extension(format!("port.tmp.{}", std::process::id()));
    if tokio::fs::write(&tmp, port.to_string()).await.is_ok() {
        if tokio::fs::rename(&tmp, &path).await.is_err() {
            // Rename failed (e.g. cross-device, shouldn't happen same-dir) —
            // fall back to a direct write so the port is at least discoverable.
            tokio::fs::write(&path, port.to_string()).await.ok();
            tokio::fs::remove_file(&tmp).await.ok();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::Ipv4Addr;

    // ── v0.2.75 P1a: bind decision matrix ─────────────────────────────

    /// User env opt-in wins regardless of module state.
    #[test]
    fn bind_env_truthy_widens_with_or_without_modules() {
        for v in ["1", "true", "TRUE", "yes"] {
            assert_eq!(
                decide_hub_bind_ip(Some(v), false),
                (Ipv4Addr::UNSPECIFIED, BindReason::UserEnvOptIn)
            );
            assert_eq!(
                decide_hub_bind_ip(Some(v), true),
                (Ipv4Addr::UNSPECIFIED, BindReason::UserEnvOptIn)
            );
        }
    }

    /// User env set to anything else forces loopback EVEN when a
    /// hub-consuming module is installed — the supervisor-managed widen
    /// never overrides an explicit user setting (leave-alone).
    #[test]
    fn bind_env_optout_forces_loopback_even_with_modules() {
        for v in ["0", "false", "no", "banana"] {
            assert_eq!(
                decide_hub_bind_ip(Some(v), true),
                (Ipv4Addr::LOCALHOST, BindReason::UserEnvOptOut)
            );
        }
    }

    /// No env: the module-derived widen state decides.
    #[test]
    fn bind_module_widen_applies_only_without_user_env() {
        assert_eq!(
            decide_hub_bind_ip(None, true),
            (Ipv4Addr::UNSPECIFIED, BindReason::ModuleWiden)
        );
        assert_eq!(
            decide_hub_bind_ip(None, false),
            (Ipv4Addr::LOCALHOST, BindReason::LoopbackDefault)
        );
    }

    /// End-to-end through the persisted state: installing a global
    /// (hub-consuming) module SETS the widen; the last uninstall CLEARS
    /// it. This is the "supervisor sets/clears the widen state" contract
    /// — the state IS the global install row, so every future hub start
    /// (install.py post-step, SessionStart hook, launcher GUI, CLI) sees
    /// it via this same resolution.
    #[test]
    fn bind_widen_state_follows_global_install_rows() {
        let db = vct_launcher_core::db::Db::open_in_memory().unwrap();

        let installed = db.has_global_module_install().unwrap();
        assert!(!installed);
        assert_eq!(
            decide_hub_bind_ip(None, installed).0,
            Ipv4Addr::LOCALHOST,
            "no module → loopback"
        );

        db.insert_global_module_install("i-g", "vct-rl-reranker", "0.2.10", "/g")
            .unwrap();
        let installed = db.has_global_module_install().unwrap();
        assert!(installed, "install sets the widen state");
        assert_eq!(decide_hub_bind_ip(None, installed).0, Ipv4Addr::UNSPECIFIED);

        db.delete_global_module_install("vct-rl-reranker").unwrap();
        let installed = db.has_global_module_install().unwrap();
        assert!(!installed, "last uninstall clears the widen state");
        assert_eq!(decide_hub_bind_ip(None, installed).0, Ipv4Addr::LOCALHOST);
    }

    /// Comment-drift gate (v0.2.75 P1a task 1a): the module_supervisor's
    /// container-env comment must describe the REAL bind contract — the
    /// pre-fix text claimed the hub unconditionally binds 0.0.0.0, which
    /// E-2 (v0.2.73) had already reversed.
    #[test]
    fn module_supervisor_comment_reflects_conditional_bind_contract() {
        let src = include_str!("module_supervisor.rs");
        assert!(
            !src.contains("combined with\n    // the hub binding 0.0.0.0 (server.rs)")
                && !src.contains("combined with the hub binding 0.0.0.0"),
            "stale unconditional-0.0.0.0 comment must not return to module_supervisor.rs"
        );
        assert!(
            src.contains("resolve_hub_bind_ip"),
            "module_supervisor's env-injection comment must reference the \
             conditional bind resolution (resolve_hub_bind_ip)"
        );
    }
}

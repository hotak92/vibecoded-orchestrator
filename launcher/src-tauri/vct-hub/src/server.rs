//! Hub HTTP server — the vct-hub binary's API, on port 7700 unless configured
//! (see `resolve_bind_port`).
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
    api, auth, chat_model_context_api, cli_api, config_api, db, gateway_watchdog, infra_watchdog,
    lifecycle_api, mcp_tool_grants_api, module_db_api, module_supervisor, modules_api,
    project_state_api, project_tokens, rl_events_api, secrets_api, weaviate_probe,
};

/// The one default, shared with every Rust reader of `hub.port`.
const DEFAULT_PORT: u16 = vct_launcher_core::services::hub_port::DEFAULT_HUB_PORT;

/// The module whose GLOBAL setting [`HUB_PORT_KEY`] configures the port the
/// hub binds — `launcher/bundled_manifests/vct-hub-api.json` declares it.
pub(crate) const HUB_MODULE_ID: &str = "vct-hub-api";
/// The setting key — the SAME name as the env var that overrides it, so the
/// manifest names the one knob there is.
pub(crate) const HUB_PORT_KEY: &str = vct_launcher_core::services::hub_port::HUB_PORT_ENV;

/// The port the hub binds, in precedence order:
///
/// 1. `VCT_HUB_PORT` in the hub process's own environment — the explicit
///    override (scripts, tests, a user's shell); any value that parses as a
///    port wins, as it always has;
/// 2. the `vct-hub-api` module's GLOBAL `VCT_HUB_PORT` setting (launcher.db,
///    `project_id IS NULL` — one hub per machine, so never a project's row),
///    when it is a whole number in 1024..=65535 (the manifest's bounds);
///    anything else is ignored with a warning;
/// 3. 7700.
///
/// Chicken-and-egg, and why it does not bite: the hub cannot be ASKED for
/// its own port, so it reads the setting from launcher.db (a file) before it
/// binds. Clients never read the setting — they find the running hub through
/// `<vct root>/hub.port`, which the hub writes after binding (and a client
/// whose own environment pins `VCT_HUB_PORT` uses that, as it always did).
/// The hub reads the setting itself rather than each spawner (launcher,
/// hooks, boot units) passing it in, so the port never depends on who
/// started it.
fn resolve_bind_port(env_value: Option<&str>, setting: Option<&serde_json::Value>) -> u16 {
    // The ONE hub-port value rule (`services::hub_port::parse_hub_port`, the
    // shared table `tests/fixtures/hub_port_cases.json`): no sign (`+7822`),
    // no `0`, no `_`, no non-ASCII numerals, no internal whitespace.
    let parse = vct_launcher_core::services::hub_port::parse_hub_port;
    if let Some(port) = env_value.and_then(parse) {
        return port;
    }
    let Some(value) = setting else {
        return DEFAULT_PORT;
    };
    let parsed = match value {
        serde_json::Value::Number(n) => n.as_u64(),
        serde_json::Value::String(s) => parse(s).map(u64::from),
        _ => None,
    };
    match parsed {
        Some(p) if (1024..=65535).contains(&p) => p as u16,
        _ => {
            tracing::warn!(
                "[vct-hub] ignoring the {HUB_MODULE_ID} {HUB_PORT_KEY} setting {value} — \
                 not a port in 1024..=65535; binding the default {DEFAULT_PORT}"
            );
            DEFAULT_PORT
        }
    }
}

/// [`resolve_bind_port`] over this process's environment and `launcher_db`.
/// An unreadable setting row is logged and treated as unset.
fn bind_port(launcher_db: &vct_launcher_core::db::Db) -> u16 {
    let setting = launcher_db
        .get_global_setting(HUB_MODULE_ID, HUB_PORT_KEY)
        .unwrap_or_else(|e| {
            tracing::warn!("[vct-hub] cannot read the hub port setting ({e}); treating it as unset");
            None
        });
    resolve_bind_port(std::env::var(HUB_PORT_KEY).ok().as_deref(), setting.as_ref())
}

/// Start the Hub API server on a background task.
/// Returns the port it's listening on.
pub async fn start_hub_server() -> Result<u16, String> {
    // v0.2.97 (owner ruling 2026-09-24): VCT_HUB_LEGACY_GLOBAL_ENV was
    // removed — one clear startup line when it is still set, never a
    // silent accept or a silent ignore.
    auth::warn_removed_legacy_global_env();

    let database = db::open_db().map_err(|e| format!("Failed to open hub database: {}", e))?;

    // v0.2.97: the bundled core-module manifests are materialized here (and at
    // launcher start) — the documented "copied to ~/.vct/bundled_manifests/"
    // that nothing did, so `/env` never saw a bundled module's settings.
    // Soft-fail: every file is attempted; a failed one keeps its previous copy
    // and all failures are logged together in one warning.
    let vct_root = vct_launcher_core::paths::vct_root_dir();
    let sync = vct_launcher_core::bundled_manifests::sync_bundled_manifests(&vct_root);
    if !sync.written.is_empty() {
        tracing::info!("[vct-hub] bundled manifests refreshed: {}", sync.written.join(", "));
    }
    if !sync.reaped.is_empty() {
        tracing::info!("[vct-hub] removed orphaned bundled-manifest temp files: {}", sync.reaped.join(", "));
    }
    if let Some(warning) = sync.warning() {
        tracing::warn!("[vct-hub] could not materialize bundled manifests: {}", warning);
    }

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
            weaviate_probe::spawn_startup_probe(probe_db);
        }
        Err(e) => {
            tracing::warn!(
                error = %e,
                "[vct-hub] weaviate_probe: cannot open launcher.db for class check; skipping"
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
    let port = bind_port(&launcher_state.0);
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

    // v0.2.76 Part 4 — per-project resolver tokens. Minted AFTER the
    // global hub.token so its 0o600 write discipline is already proven on
    // this state dir. Reads the launcher.db project registry, writes one
    // `hub.token.<project_id>` per project, cleans up stale files for
    // deleted projects, and returns the in-memory registry the auth
    // middleware consults to accept a project-scoped bearer on the
    // `/env` + `/config` routes (with the global token still accepted for
    // the one-release compat window). Soft-fail throughout — an empty
    // registry just means every resolver falls back to the global token.
    let project_token_registry = project_tokens::mint_project_tokens(&launcher_state.0);

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
    let routes = axum::Router::new()
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
        // v0.2.92 WP-11: read-only chat-model context table. Ordinary
        // global-hub.token route (it is not per-project — a model's context
        // window is a property of the model), mounted INSIDE the hub-wide
        // auth layer like its siblings above. No write path here by design:
        // the launcher is the single writer for launcher.db and owns the
        // export-on-every-mutation invariant the gateway depends on.
        .nest(
            "/api/v1",
            chat_model_context_api::router().with_state(launcher_state.clone()),
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
        );

    // v0.2.77 F1: apply the auth stack via the shared SSOT so the layer
    // order (launcher_db + registry + auth_state ALL declared after
    // require_auth → all in request extensions when the middleware runs)
    // is identical in prod and in the auth tests. The `module_db_api`
    // middleware still pulls the launcher_state extension (outer-inserted
    // → visible to inner consumers); the v0.2.49 resume-on-boot task
    // below owns its own `launcher_state.clone()`.
    let app = apply_auth_layers(
        routes,
        launcher_state.clone(),
        auth_state,
        project_token_registry,
        cors,
    );

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
            tracing::error!(error = %e, "[vct-hub] server error");
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

    // v0.2.95 (R5c): supervision for the model GATEWAY — a process, not a
    // container, so it gets its own task rather than a row in the watchdog
    // above (see that module's `CANONICAL_INFRA_SERVICES` doc). The hub is
    // the always-on service (ruling R20), so it is where "restart it if it
    // crashes, and log the reason when it cannot be restarted" belongs. The
    // SessionStart hook remains the cold-start path; this is the always-on
    // one, and it heals through the SAME `vco_lib.gateway_ensure` entry
    // point rather than a second copy of the start logic.
    gateway_watchdog::spawn_gateway_watchdog(launcher_state.clone());

    // v0.2.97 (lane V): poll every active module's `runtime.health_check`
    // (loopback only, bounded, one task per probe) so the module tiles can
    // show up / down / unknown. Results are served on /modules/catalog and
    // /modules/{id}/status. `VCT_HUB_MODULE_HEALTH=0` disables it.
    super::module_health::spawn_module_health_poller(launcher_state.clone());

    // E-2: log the ACTUAL bind host, not a hardcoded "127.0.0.1" (the prior
    // string drifted from the real 0.0.0.0 bind). Loopback is always reachable
    // as 127.0.0.1 regardless of the bind IP, so print that for the all-
    // interfaces case too, but annotate the exposure.
    let bind_note = if bind_ip == std::net::Ipv4Addr::LOCALHOST {
        "loopback-only"
    } else {
        "all interfaces (VCT_HUB_BIND_ALL opt-in or hub-consuming module installed)"
    };
    // v0.2.91: this was the hub's ONE diagnostic on stdout — the stream
    // the lifecycle CLI's machine contract owns (`--status`,
    // `--boot-status`). Nothing ever parsed it, so moving it to the
    // diagnostics channel with the rest costs nothing and stops a daemon
    // log line from interleaving into a parsed stream.
    tracing::info!(
        port = actual_port,
        bind = %bind_ip,
        exposure = bind_note,
        "[vct-hub] API server running on http://127.0.0.1:{}",
        actual_port
    );
    Ok(actual_port)
}

/// Apply the hub-wide auth middleware stack to an already-assembled
/// router of `/api/v1/*` routes.
///
/// SSOT for the layer order (v0.2.77 F1 fix). Axum applies layers in
/// reverse-of-declaration on the way IN, so the LAST layer added runs
/// FIRST. For `auth::require_auth` to be able to pull `AuthState`,
/// `ProjectTokenRegistry`, AND `LauncherDbHandle` from the request
/// extensions, ALL THREE extension layers must be declared AFTER
/// `require_auth` (= run before it). Pre-fix, `Extension(launcher_state)`
/// was declared BEFORE `require_auth` (inner) so it was inserted only
/// AFTER the middleware ran — leaving the lazy-mint (Task 4a) and
/// slug-canonicalization (Task 4d) helpers, which read the DB handle
/// from request extensions, to always fail closed (hard 403) in prod.
/// The unit/integration tests passed because their bespoke test router
/// declared `Extension(db)` LAST (outermost) — masking the bug. Both the
/// production `start_hub_server` AND the auth tests now go through THIS
/// function so that masking is structurally impossible.
///
/// Resulting request-time order (outermost → innermost):
///   cors → Extension(launcher_db) → Extension(project_token_registry)
///        → Extension(auth_state) → require_auth → routes
///
/// The outermost `Extension(launcher_db)` also reaches the inner
/// `module_db_api::require_module_scope` middleware (an outer-inserted
/// extension is visible to every inner consumer), which is why a single
/// layer suffices for both consumers.
pub(crate) fn apply_auth_layers(
    router: axum::Router,
    launcher_state: modules_api::LauncherDbHandle,
    auth_state: auth::AuthState,
    project_token_registry: project_tokens::ProjectTokenRegistry,
    cors: CorsLayer,
) -> axum::Router {
    router
        // Declared FIRST → innermost → runs LAST (closest to the routes).
        // require_auth needs the three extensions below to already be in
        // request extensions, so they are declared AFTER it.
        .layer(axum::middleware::from_fn(auth::require_auth))
        .layer(axum::Extension(auth_state))
        // v0.2.76 Part 4 — the per-project token registry, read by
        // `auth::require_auth` to accept a project-scoped bearer on the
        // `/env` + `/config` routes.
        .layer(axum::Extension(project_token_registry))
        // v0.2.77 F1 — the launcher.db handle, read by `require_auth`'s
        // lazy-mint (4a) + slug-canonicalization (4d) helpers AND by the
        // inner `module_db_api` middleware. Declared LAST among the
        // extensions (outermost) so it is inserted before `require_auth`
        // runs and reaches every inner consumer.
        .layer(axum::Extension(launcher_state))
        .layer(cors)
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
            tracing::warn!(
                error = %e,
                "[vct-hub] could not read module installs for the bind \
                 decision; keeping the conservative loopback bind. If a \
                 hub-consuming module's container cannot reach the hub, restart \
                 the hub once launcher.db is readable."
            );
            false
        }
    };
    let (ip, reason) = decide_hub_bind_ip(env_value.as_deref(), modules_present);
    match reason {
        BindReason::ModuleWiden => {
            tracing::info!(
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
            tracing::warn!(
                "[vct-hub] VCT_HUB_BIND_ALL={} forces a loopback-only \
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

/// The ports the bind ladder tries: `base`, then up to `retries` more, never
/// past 65535 (v0.2.97 R7b F10: `base + offset` overflowed `u16` — a panic in
/// a debug build, a wrap to a low port in release, which the hub would then
/// have written to `hub.port`). Pure.
fn bind_ladder(base: u16, retries: u16) -> Vec<u16> {
    (0..=retries).map_while(|offset| base.checked_add(offset)).collect()
}

async fn try_bind(base_addr: SocketAddr, retries: u16) -> Result<tokio::net::TcpListener, String> {
    let ports = bind_ladder(base_addr.port(), retries);
    let last = ports.last().copied().unwrap_or(base_addr.port());
    let mut last_err = None;
    for port in ports {
        match tokio::net::TcpListener::bind(SocketAddr::from((base_addr.ip(), port))).await {
            Ok(listener) => return Ok(listener),
            Err(e) => last_err = Some(e),
        }
    }
    Err(format!(
        "Cannot bind to ports {}-{}: {}",
        base_addr.port(),
        last,
        last_err.map(|e| e.to_string()).unwrap_or_else(|| "no port tried".into())
    ))
}

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

/// Write port to `<VCT_STATE_DIR or ~/.vct>/hub.port` so apps can discover the hub.
///
/// v0.2.61 (Option H C-PORT): write atomically via a temp file + rename.
/// A plain truncating write can be observed mid-write (empty / partial) by a
/// concurrent reader (`module_service::hub_port_for_proxy`,
/// `vct_launcher_core::services::hub_port::resolve_hub_port`, which the
/// supervisor and the manifest placeholder `{hub_port}` both use) whose
/// `parse::<u16>()` then fails → wrong VCT_HUB_BASE_URL / a failed readiness
/// probe on a healthy hub.
/// A same-directory rename is atomic on POSIX and on Windows ReplaceFile
/// semantics, so a reader sees either the old value or the new one, never a
/// torn one.
async fn write_port_file(port: u16) {
    let path = vct_launcher_core::services::hub_port::hub_port_file();

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

    // ── v0.2.97 R7b F10: the bind ladder stops at 65535 ──────────────

    #[test]
    fn bind_ladder_never_steps_past_the_last_port() {
        assert_eq!(bind_ladder(7700, 5), vec![7700, 7701, 7702, 7703, 7704, 7705]);
        assert_eq!(bind_ladder(65534, 5), vec![65534, 65535]);
        assert_eq!(bind_ladder(65535, 5), vec![65535]);
    }

    // ── v0.2.97 review R6: the hub port setting is a real knob ───────

    #[test]
    fn bind_port_env_override_wins_over_the_setting() {
        let setting = serde_json::json!(8800);
        assert_eq!(resolve_bind_port(Some("9"), Some(&setting)), 9);
        assert_eq!(resolve_bind_port(Some(" 7711 "), None), 7711);
    }

    #[test]
    fn bind_port_uses_the_setting_when_the_env_is_absent_or_not_a_port() {
        let number = serde_json::json!(8800);
        let text = serde_json::json!("8801");
        assert_eq!(resolve_bind_port(None, Some(&number)), 8800);
        assert_eq!(resolve_bind_port(None, Some(&text)), 8801);
        assert_eq!(resolve_bind_port(Some("not-a-port"), Some(&number)), 8800);
    }

    /// R7b F9 / review round 7: the hub's own bind-port parse is the one
    /// hub-port value rule. Runs the env rows of the shared table
    /// `tests/fixtures/hub_port_cases.json`: a valid `VCT_HUB_PORT` is bound,
    /// an invalid one (`+7822`, `0`, `7_700`, non-ASCII numerals, `78 11`)
    /// falls through — for the hub to its setting, else the default (the hub
    /// never reads `hub.port`: it WRITES it). Red when the env is parsed with
    /// `str::parse::<u16>`, which accepts `+7822` and `0`.
    #[test]
    fn bind_port_env_follows_the_shared_hub_port_table() {
        let table: serde_json::Value =
            serde_json::from_str(include_str!("../../../../tests/fixtures/hub_port_cases.json")).unwrap();
        let setting = serde_json::json!(8800);
        let mut checked = 0;
        for case in table["cases"].as_array().unwrap() {
            let Some(env) = case["env_port"].as_str() else { continue };
            let name = case["name"].as_str().unwrap();
            // The client ladder used the env pin iff its answer is not what
            // the file alone gives (every row's env and file ports differ).
            let from_file = case["expect_file"].as_u64().unwrap_or(u64::from(DEFAULT_PORT));
            let expect = case["expect"].as_u64().unwrap();
            let env_port = (expect != from_file).then_some(expect as u16);
            assert_eq!(resolve_bind_port(Some(env), None), env_port.unwrap_or(DEFAULT_PORT), "case `{name}`");
            assert_eq!(
                resolve_bind_port(Some(env), Some(&setting)),
                env_port.unwrap_or(8800),
                "case `{name}` + setting"
            );
            checked += 1;
        }
        assert!(checked >= 8, "the table's env rows ran ({checked})");
        for bad in ["+7822", "0", "7_700", "78 11"] {
            assert_eq!(resolve_bind_port(Some(bad), None), DEFAULT_PORT, "{bad:?}");
        }
        // A string setting follows the same rule (and the 1024 floor).
        for bad in ["+8800", "8_800", "88 00", "0"] {
            assert_eq!(resolve_bind_port(None, Some(&serde_json::json!(bad))), DEFAULT_PORT, "{bad:?}");
        }
        assert_eq!(resolve_bind_port(None, Some(&serde_json::json!(" 8801\n"))), 8801);
    }

    #[test]
    fn bind_port_defaults_without_a_usable_setting() {
        assert_eq!(resolve_bind_port(None, None), DEFAULT_PORT);
        for bad in [
            serde_json::json!(80),
            serde_json::json!(70000),
            serde_json::json!(-1),
            serde_json::json!(8800.5),
            serde_json::json!("eighty"),
            serde_json::json!(true),
        ] {
            assert_eq!(resolve_bind_port(None, Some(&bad)), DEFAULT_PORT, "{bad}");
        }
    }

    /// The setting the hub reads is the one its bundled manifest declares,
    /// stored as a GLOBAL row — a project's row is not the hub's port.
    #[test]
    fn bind_port_reads_the_global_row_the_manifest_declares() {
        let manifest: serde_json::Value =
            serde_json::from_str(include_str!("../../../bundled_manifests/vct-hub-api.json")).unwrap();
        assert_eq!(manifest["id"], HUB_MODULE_ID);
        let keys: Vec<&str> = manifest["settings"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|s| s["key"].as_str())
            .collect();
        assert_eq!(keys, [HUB_PORT_KEY], "the manifest declares exactly the key the hub reads");
        assert_eq!(manifest["settings"][0]["default"], serde_json::json!(DEFAULT_PORT));

        let db = vct_launcher_core::db::Db::open_in_memory().unwrap();
        assert_eq!(db.get_global_setting(HUB_MODULE_ID, HUB_PORT_KEY).unwrap(), None);
        db.set_global_setting(HUB_MODULE_ID, HUB_PORT_KEY, &serde_json::json!(8802)).unwrap();
        let setting = db.get_global_setting(HUB_MODULE_ID, HUB_PORT_KEY).unwrap();
        assert_eq!(resolve_bind_port(None, setting.as_ref()), 8802);
        // Rewriting replaces the one global row.
        db.set_global_setting(HUB_MODULE_ID, HUB_PORT_KEY, &serde_json::json!(8803)).unwrap();
        let setting = db.get_global_setting(HUB_MODULE_ID, HUB_PORT_KEY).unwrap();
        assert_eq!(resolve_bind_port(None, setting.as_ref()), 8803);
    }

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

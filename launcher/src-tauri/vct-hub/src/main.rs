//! vct-hub — detached HTTP server for VCT (binary entry point).
//!
//! v0.2.21 Step 5: lifecycle CLI dispatch.
//!
//! The default (no-args / `--foreground`) path:
//!   1. Claim the lockfile (refuse to start if another live hub holds it).
//!   2. Start the HTTP server (server::start_hub_server — already
//!      ported in Step 4).
//!   3. Park the main task on SIGTERM/SIGINT (Unix) or Ctrl-C (Windows).
//!   4. On shutdown signal, release the lockfile + exit 0.
//!
//! Subcommand paths delegate to `vct_hub::lifecycle::*` and translate
//! their `LifecycleResult` into a process exit code.
//!
//! ## Diagnostics (v0.2.91, #21)
//!
//! Every hub diagnostic goes through `tracing` at an honest level, gated
//! by `VCO_LOG_LEVEL` > launcher.db `app_state['logging.level']` > INFO
//! (resolved by `vct_launcher_core::logging`). The subscriber writes to
//! stderr, which the detached daemon's callers redirect to a log file or
//! to `/dev/null`; stdout is left clean because the lifecycle CLI parses
//! it (`--status`, `--boot-status`).

use std::process;

use vct_hub::boot;
use vct_hub::cli::{self, Command};
use vct_hub::lifecycle::{self, LifecycleResult};
use vct_hub::lockfile;
use vct_hub::server;
use vct_launcher_core::logging;

#[tokio::main]
async fn main() {
    // FIRST statement: install diagnostics before anything can want to
    // emit one. `resolve_process_log_level` reads VCO_LOG_LEVEL and
    // best-effort probes launcher.db for the stored preference — see
    // below for why the hub can afford the DB read here and the launcher
    // cannot.
    //
    // Every failure mode of that probe (no launcher.db yet, schema
    // predating `app_state`, DB locked by a busy launcher) resolves to
    // the default level rather than an error: logging setup must never be
    // a reason the hub fails to start.
    logging::init_tracing(logging::resolve_process_log_level());

    let cmd = cli::parse_env_args();

    match cmd {
        Command::Help => {
            // [vct-print-contract] `--help` writes the usage text to
            // stdout, verbatim and unprefixed. A CLI's help output is
            // read by humans and redirected by scripts; routing it
            // through the log subscriber would stamp every line with a
            // timestamp and hide it whenever the level is raised.
            println!("{}", cli::usage());
            process::exit(0);
        }
        Command::Usage => {
            // [vct-print-contract] Same text on the bad-invocation path,
            // to stderr with EX_USAGE. Must appear whatever the log level
            // says — it is the response to the command, not commentary
            // about it.
            eprintln!("{}", cli::usage());
            process::exit(64); // EX_USAGE per BSD sysexits
        }
        Command::Status => exit_with(lifecycle::status()),
        Command::Stop => exit_with(lifecycle::stop()),
        Command::StartIfNotRunning => exit_with(lifecycle::start_if_not_running()),
        Command::RegisterBoot => exit_with(boot::run_register_boot()),
        Command::UnregisterBoot => exit_with(boot::run_unregister_boot()),
        Command::BootStatus => exit_with(boot::run_boot_status()),
        Command::Foreground => run_foreground().await,
    }
}

/// The hub-server lifecycle. Owns lockfile + server task + signal
/// handler. On any failure path, we release the lockfile before
/// exiting so a clean stop never leaves stale state.
async fn run_foreground() {
    // Acquire lockfile first. Reject duplicate-start cleanly.
    if let Err(err) = lifecycle::try_acquire_or_exit() {
        exit_with(err);
    }

    // v0.2.91: the version in this line used to be the hardcoded literal
    // "v0.2.21" — the release it was written in — so every build since
    // has announced the wrong version to the one log a support session
    // reads first. Same source as /health now (`CARGO_PKG_VERSION`,
    // inherited from the workspace).
    tracing::info!(
        version = env!("CARGO_PKG_VERSION"),
        pid = std::process::id(),
        "[vct-hub] starting"
    );

    let bind_result = server::start_hub_server().await;
    let port = match bind_result {
        Ok(p) => p,
        Err(e) => {
            // Lockfile was claimed but server failed to bind — release
            // so the next start_if_not_running doesn't see a fake
            // live hub (our PID would be alive, but no port was
            // ever bound). F-6 (v0.2.73): pid-OWNED release — by this
            // point the lockfile could record a different, healthy hub
            // (e.g. after a stale-version takeover raced us);
            // unconditional removal would delete the winner's claim.
            let _ = lockfile::release_owned();
            tracing::error!(error = %e, "[vct-hub] failed to start server");
            process::exit(1);
        }
    };
    tracing::info!(port, "[vct-hub] listening on http://127.0.0.1:{}", port);

    // Park until SIGTERM / SIGINT (Unix) or Ctrl-C (Windows).
    wait_for_shutdown_signal().await;

    tracing::info!("[vct-hub] shutting down");
    // v0.3.0 (WP-K): best-effort graceful close of the persistent keychain
    // Secret-Service connection (Linux). One clean client disconnect at a
    // controlled moment instead of an abrupt teardown mid-op — a mitigation of
    // the gnome-keyring disconnect-mid-dispatch fragility. BOUNDED: the drain
    // uses a `try_lock` with a ≤250ms deadline, so a keychain worker mid-op
    // (e.g. parked on a user unlock prompt) is left to the abrupt process
    // teardown rather than stalling hub shutdown. No-op on Windows / macOS.
    vct_launcher_core::secrets::shutdown_keychain_connection();
    // F-6 (v0.2.73): pid-owned release — never remove a lockfile that a
    // NEWER hub has since claimed (stale-version takeover while we were
    // being signalled).
    if let Err(e) = lockfile::release_owned() {
        tracing::warn!(error = %e, "[vct-hub] lockfile release failed");
    }
    process::exit(0);
}

/// Map a LifecycleResult to a process exit. Centralised so every
/// CLI dispatch arm above translates exit codes consistently.
fn exit_with(r: LifecycleResult) -> ! {
    match r {
        LifecycleResult::Ok => process::exit(0),
        LifecycleResult::OkExit(code) => process::exit(code),
        LifecycleResult::Err(msg) => {
            // ERROR, so it survives every level the preference can select
            // (the resolver's floor is ERROR — no setting can hide this).
            tracing::error!("[vct-hub] {}", msg);
            process::exit(1);
        }
    }
}

#[cfg(unix)]
async fn wait_for_shutdown_signal() {
    use tokio::signal::unix::{signal, SignalKind};
    let mut sigterm = match signal(SignalKind::terminate()) {
        Ok(s) => s,
        Err(e) => {
            tracing::error!(error = %e, "[vct-hub] cannot install SIGTERM handler");
            process::exit(1);
        }
    };
    let mut sigint = match signal(SignalKind::interrupt()) {
        Ok(s) => s,
        Err(e) => {
            tracing::error!(error = %e, "[vct-hub] cannot install SIGINT handler");
            process::exit(1);
        }
    };
    tokio::select! {
        _ = sigterm.recv() => tracing::info!("[vct-hub] SIGTERM received"),
        _ = sigint.recv() => tracing::info!("[vct-hub] SIGINT received"),
    }
}

#[cfg(windows)]
async fn wait_for_shutdown_signal() {
    if let Err(e) = tokio::signal::ctrl_c().await {
        tracing::error!(error = %e, "[vct-hub] ctrl_c handler error");
        process::exit(1);
    }
    tracing::info!("[vct-hub] Ctrl-C received");
}

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

use std::process;

use vct_hub::boot;
use vct_hub::cli::{self, Command};
use vct_hub::lifecycle::{self, LifecycleResult};
use vct_hub::lockfile;
use vct_hub::server;

#[tokio::main]
async fn main() {
    let cmd = cli::parse_env_args();

    match cmd {
        Command::Help => {
            println!("{}", cli::usage());
            process::exit(0);
        }
        Command::Usage => {
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

    eprintln!("[vct-hub] v0.2.21 starting (pid {})", std::process::id());

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
            eprintln!("[vct-hub] failed to start server: {}", e);
            process::exit(1);
        }
    };
    eprintln!("[vct-hub] listening on http://127.0.0.1:{}", port);

    // Park until SIGTERM / SIGINT (Unix) or Ctrl-C (Windows).
    wait_for_shutdown_signal().await;

    eprintln!("[vct-hub] shutting down");
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
        eprintln!("[vct-hub] warning: lockfile release failed: {}", e);
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
            eprintln!("[vct-hub] {}", msg);
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
            eprintln!("[vct-hub] cannot install SIGTERM handler: {}", e);
            process::exit(1);
        }
    };
    let mut sigint = match signal(SignalKind::interrupt()) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("[vct-hub] cannot install SIGINT handler: {}", e);
            process::exit(1);
        }
    };
    tokio::select! {
        _ = sigterm.recv() => eprintln!("[vct-hub] SIGTERM received"),
        _ = sigint.recv() => eprintln!("[vct-hub] SIGINT received"),
    }
}

#[cfg(windows)]
async fn wait_for_shutdown_signal() {
    if let Err(e) = tokio::signal::ctrl_c().await {
        eprintln!("[vct-hub] ctrl_c handler error: {}", e);
        process::exit(1);
    }
    eprintln!("[vct-hub] Ctrl-C received");
}

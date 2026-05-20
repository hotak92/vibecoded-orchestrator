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
        Command::RegisterBoot | Command::UnregisterBoot | Command::BootStatus => {
            // Step 11 placeholders. install.py probes these via exit
            // code 64 (EX_USAGE) to decide whether the installed
            // vct-hub binary supports boot integration — until Step 11
            // lands they all 64.
            eprintln!(
                "[vct-hub] boot-time auto-start commands not implemented yet \
                 (Step 11 of v0.2.21)"
            );
            process::exit(64);
        }
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
            // ever bound).
            let _ = lockfile::release();
            eprintln!("[vct-hub] failed to start server: {}", e);
            process::exit(1);
        }
    };
    eprintln!("[vct-hub] listening on http://127.0.0.1:{}", port);

    // Park until SIGTERM / SIGINT (Unix) or Ctrl-C (Windows).
    wait_for_shutdown_signal().await;

    eprintln!("[vct-hub] shutting down");
    if let Err(e) = lockfile::release() {
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

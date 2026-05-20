//! vct-hub — detached HTTP server for VCT (binary entry point).
//!
//! v0.2.21 Step 4 ports `hub/*` out of the launcher into this binary.
//! The lifecycle/single-instance machinery lands in Step 5
//! (`vct-hub --start-if-not-running`, `--stop`, `--status`).
//!
//! For Step 4 this binary just spins up `vct_hub::server::start_hub_server`
//! and parks on a `Notify` until SIGINT/SIGTERM (Unix) or Ctrl-C
//! (Windows). No CLI flags yet; Step 5 wires `clap`.
//!
//! The launcher GUI still owns the in-process hub for one more release
//! (Step 6 flips that over). During v0.2.21, this binary exists but is
//! not started by the launcher's setup; install.py is what brings it
//! up. See plan §"Step 5/6" for the takeover sequence.

use std::process;

#[tokio::main]
async fn main() {
    eprintln!("[vct-hub] v0.2.21 starting");

    match vct_hub::server::start_hub_server().await {
        Ok(port) => {
            eprintln!("[vct-hub] listening on http://127.0.0.1:{}", port);
        }
        Err(e) => {
            eprintln!("[vct-hub] failed to start: {}", e);
            process::exit(1);
        }
    }

    // `start_hub_server` spawns the listener as a tokio task and returns.
    // Park the main task so the process stays alive until terminated.
    // Step 5 replaces this with a proper signal-handling + lockfile
    // shutdown sequence.
    #[cfg(unix)]
    {
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
            _ = sigterm.recv() => eprintln!("[vct-hub] SIGTERM, shutting down"),
            _ = sigint.recv() => eprintln!("[vct-hub] SIGINT, shutting down"),
        }
    }

    #[cfg(windows)]
    {
        if let Err(e) = tokio::signal::ctrl_c().await {
            eprintln!("[vct-hub] ctrl_c handler error: {}", e);
        } else {
            eprintln!("[vct-hub] Ctrl-C, shutting down");
        }
    }
}

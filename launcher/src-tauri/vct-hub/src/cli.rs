//! Lifecycle CLI for the vct-hub binary.
//!
//! v0.2.21 Step 5. Minimal hand-rolled argument parser so vct-hub
//! avoids a `clap` dependency (clap brings ~25 transitive crates;
//! v0.2.21 commits to keeping the hub binary lean — see Cargo.toml).
//!
//! Subcommands:
//!   vct-hub                            → start in the foreground (Step 11
//!                                        wires this for systemd Type=simple)
//!   vct-hub --start-if-not-running     → if already running, exit 0
//!                                        immediately; else start
//!                                        detached + exit 0 (the spawned
//!                                        child takes over)
//!   vct-hub --stop                     → ask the running hub to shut
//!                                        down via /api/v1/internal/shutdown
//!                                        (Step 15) or SIGTERM, then wait
//!                                        up to 10s for hub.pid removal
//!   vct-hub --status                   → report Running(pid) / NotRunning /
//!                                        Stale(pid) on stdout
//!
//!   vct-hub --register-boot           → register OS boot-time auto-start
//!                                        (systemd-user unit on Linux,
//!                                        launchd LaunchAgent on macOS,
//!                                        Scheduled Task on Windows) and
//!                                        enable+start it; idempotent
//!   vct-hub --unregister-boot          → remove the OS boot-time
//!                                        auto-start unit; idempotent
//!   vct-hub --boot-status              → report the boot autostart state
//!                                        (enabled / disabled / not-installed)
//!
//! The boot sub-commands dispatch to `boot::run_*` (see `boot.rs`), which
//! renders + writes the per-OS init artifact. Boot autostart is DEFAULT-OFF
//! in v0.2.21 — the user opts in via the launcher GUI Preferences.

use std::env;

/// Parsed top-level command from argv[1..].
#[derive(Debug, PartialEq, Eq)]
pub enum Command {
    /// No flags. Default behaviour: start in the foreground until
    /// SIGINT/SIGTERM, parking the main task on signal listeners.
    Foreground,
    /// `--start-if-not-running`. Probe the lockfile; if alive, exit 0
    /// immediately. If not, spawn a detached child that calls
    /// `Foreground` and exit 0.
    StartIfNotRunning,
    /// `--stop`. Tell the running hub to release the lockfile + exit.
    Stop,
    /// `--status`. Print one line (`running pid=<N>` / `not-running` /
    /// `stale pid=<N>`) on stdout and exit with a corresponding code.
    Status,
    /// `--register-boot`. Register OS boot-time auto-start (systemd-user
    /// unit / launchd LaunchAgent / Windows Scheduled Task) and start it.
    RegisterBoot,
    /// `--unregister-boot`. Remove the OS boot-time auto-start unit.
    UnregisterBoot,
    /// `--boot-status`. Report whether boot autostart is
    /// enabled / disabled / not-installed.
    BootStatus,
    /// `--help` / `-h`. Print usage on stdout and exit 0.
    Help,
    /// Unrecognised argv shape. The caller should print usage to stderr
    /// and exit 64 (EX_USAGE).
    Usage,
}

/// Parse argv[1..] into a Command. We deliberately keep the surface
/// small; complex flag combos are out of scope.
pub fn parse_args(args: &[String]) -> Command {
    if args.is_empty() {
        return Command::Foreground;
    }
    if args.len() > 1 {
        return Command::Usage;
    }
    match args[0].as_str() {
        "--start-if-not-running" => Command::StartIfNotRunning,
        "--stop" => Command::Stop,
        "--status" => Command::Status,
        "--register-boot" => Command::RegisterBoot,
        "--unregister-boot" => Command::UnregisterBoot,
        "--boot-status" => Command::BootStatus,
        "--foreground" => Command::Foreground,
        "--help" | "-h" => Command::Help,
        _ => Command::Usage,
    }
}

/// Convenience wrapper that reads from `std::env::args()`.
pub fn parse_env_args() -> Command {
    let args: Vec<String> = env::args().skip(1).collect();
    parse_args(&args)
}

/// Usage banner. Single source of truth; `--help` and unrecognised
/// arg paths both render this.
pub fn usage() -> &'static str {
    "vct-hub — VibeCoded Tools HTTP hub (detached)

Usage:
  vct-hub                      Start the hub in the foreground (default).
                               Honours SIGTERM / SIGINT for graceful shutdown.
  vct-hub --foreground         Alias for the default. Use this from systemd
                               Type=simple service files.
  vct-hub --start-if-not-running
                               If the hub is already running, exit 0. Else
                               spawn a detached background instance and
                               exit 0.
  vct-hub --stop               Stop the running hub. Exits 0 on success or
                               when no hub was running. Exits 1 if a hub
                               was running and refused to stop within 10s.
  vct-hub --status             Print Running/NotRunning/Stale on stdout.
                               Exits 0 if running, 1 if not running,
                               2 if stale (pid file present but owner dead).
  vct-hub --register-boot      Register OS boot-time auto-start (systemd
                               user unit on Linux, launchd LaunchAgent on
                               macOS, Scheduled Task on Windows) and start
                               it now. Idempotent. Exits 0 on success, 1 on
                               error. Boot autostart is default-off; the
                               launcher GUI Preferences opts you in.
  vct-hub --unregister-boot    Remove the OS boot-time auto-start unit.
                               Idempotent. Exits 0 on success, 1 on error.
  vct-hub --boot-status        Print the boot autostart state on stdout.
                               Exits 0 enabled, 1 disabled, 2 not-installed,
                               3 inspection error.
  vct-hub --help               Show this banner.

Environment:
  VCT_STATE_DIR  Override the launcher state-root (default: ~/.vct/).
                 Affects hub.pid, hub.port, hub.token, etc.
  VCT_HUB_PORT   Bind port (default: 7700).
"
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(argv: &[&str]) -> Command {
        let args: Vec<String> = argv.iter().map(|s| s.to_string()).collect();
        parse_args(&args)
    }

    #[test]
    fn no_args_is_foreground() {
        assert_eq!(parse(&[]), Command::Foreground);
    }

    #[test]
    fn explicit_foreground_flag() {
        assert_eq!(parse(&["--foreground"]), Command::Foreground);
    }

    #[test]
    fn start_if_not_running() {
        assert_eq!(parse(&["--start-if-not-running"]), Command::StartIfNotRunning);
    }

    #[test]
    fn stop() {
        assert_eq!(parse(&["--stop"]), Command::Stop);
    }

    #[test]
    fn status() {
        assert_eq!(parse(&["--status"]), Command::Status);
    }

    #[test]
    fn boot_commands_recognised_as_their_own_variants() {
        assert_eq!(parse(&["--register-boot"]), Command::RegisterBoot);
        assert_eq!(parse(&["--unregister-boot"]), Command::UnregisterBoot);
        assert_eq!(parse(&["--boot-status"]), Command::BootStatus);
    }

    #[test]
    fn help_long_and_short() {
        assert_eq!(parse(&["--help"]), Command::Help);
        assert_eq!(parse(&["-h"]), Command::Help);
    }

    #[test]
    fn unknown_arg_is_usage_error() {
        assert_eq!(parse(&["--what"]), Command::Usage);
        assert_eq!(parse(&["bogus"]), Command::Usage);
    }

    #[test]
    fn multiple_args_rejected() {
        assert_eq!(parse(&["--stop", "extra"]), Command::Usage);
        assert_eq!(parse(&["--status", "--start-if-not-running"]), Command::Usage);
    }

    #[test]
    fn usage_banner_mentions_every_command() {
        let u = usage();
        for needle in [
            "--start-if-not-running",
            "--stop",
            "--status",
            "--register-boot",
            "--unregister-boot",
            "--boot-status",
            "--foreground",
            "--help",
            "VCT_STATE_DIR",
            "VCT_HUB_PORT",
        ] {
            assert!(
                u.contains(needle),
                "usage banner must mention {} — got:\n{}",
                needle,
                u
            );
        }
    }
}

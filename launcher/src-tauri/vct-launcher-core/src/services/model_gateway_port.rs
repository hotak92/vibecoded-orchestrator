// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Where this machine's model gateway lives — the ONE Rust answer (v0.2.95).
//!
//! Three Rust call-sites need it and used to hold three copies: the launcher's
//! Services card (`commands::model_gateway`), the hub's gateway supervisor
//! (`vct_hub::gateway_watchdog`), and the hub's `/services/status` skeleton
//! (`vct_hub::lifecycle_api`, which simply hard-coded `11436`). The last one
//! is what a third copy costs: a machine whose gateway fell back off the
//! documented port had `/services/status` reporting a health URL nothing
//! served, while the card beside it was correct.
//!
//! So the constants and the resolution live here, in the crate both binaries
//! already depend on, and the two former mirrors are now `use` statements.
//!
//! ## Why this is still a (C)-tier mirror of PYTHON, and what pins it
//!
//! The daemon owns these names: `claude_mcp_servers/model_router/config.py`
//! declares `DEFAULT_PORT`, `_PORT_BASENAME`, `LAST_PORT_BASENAME` and reads
//! `VCT_MODEL_GATEWAY_PORT`, and `model_router/server.py` answers `/health`
//! with `SERVICE_NAME`. Under the repo's A>B>C rule the (A) leg — ask the
//! Python resolver — is refused HERE for a specific reason: the hub's
//! supervisor ticks on a timer and its whole design property is that a healthy
//! machine spawns NO subprocess, and the launcher re-reads this on every
//! 5-second status poll, where shelling out would make the card that reports
//! "the gateway is not installed" depend on the gateway package importing.
//! The (B) leg needs a config file neither side has.
//!
//! The obligation that comes with (C) is the parity test at the bottom of this
//! file, which READS the Python source and fails when a literal moves. It is
//! the only such test now: before v0.2.95 the hub's copy pinned itself against
//! the launcher's copy AND against Python, which made a three-way agreement
//! that had to be maintained in three places. One Rust home, one pin.

use std::path::{Path, PathBuf};

use crate::paths::vct_root_dir;

/// The gateway's documented default port.
/// MUST MATCH `model_router.config.DEFAULT_PORT`.
pub const DEFAULT_GATEWAY_PORT: u16 = 11436;

/// Env pin, read first and honoured absolutely — a pinned port is a statement
/// about where this machine's gateway lives.
/// MUST MATCH the variable `model_router.config.explicit_port` reads.
pub const PORT_ENV: &str = "VCT_MODEL_GATEWAY_PORT";

/// Written by a RUNNING daemon, removed on its clean exit.
/// MUST MATCH `model_router.config._PORT_BASENAME`.
pub const PORT_BASENAME: &str = "model-gateway.port";

/// The launcher's record of the port it last STARTED a gateway on. Never
/// deleted, so a stopped gateway that had fallen off the default port stays
/// findable — the daemon unlinks its own port file on a clean exit, and
/// without this record the next resolution answers with the shipped default,
/// which on the reporter's machine is a legacy container (review R2-2).
/// MUST MATCH `model_router.config.LAST_PORT_BASENAME` and
/// `vco_lib.vscode_settings.LAST_PORT_BASENAME`.
pub const LAST_PORT_BASENAME: &str = "model-gateway.last-port";

/// `/health`'s `service` value. A port answering with anything else is NOT a
/// gateway, however plausible.
/// MUST MATCH `model_router.config.SERVICE_NAME`.
pub const GATEWAY_SERVICE: &str = "vct-model-gateway";

/// The running daemon's port file.
pub fn port_path() -> PathBuf {
    vct_root_dir().join(PORT_BASENAME)
}

/// The launcher's last-started-port record. Beside the port file by design:
/// same directory, different question.
pub fn last_port_path() -> PathBuf {
    vct_root_dir().join(LAST_PORT_BASENAME)
}

/// First line of `path` as a port, or `None`.
///
/// Absent, unreadable, unparseable and out-of-range all answer the same way —
/// "no evidence here" — because these files are read on a timer and on every
/// status poll: a damaged one must fall through to the next source rather than
/// error, blank the card, or resolve to something nonsensical.
pub fn read_port_file(path: &Path) -> Option<u16> {
    let raw = std::fs::read_to_string(path).ok()?;
    let first = raw.lines().next()?.trim().to_string();
    match first.parse::<u16>() {
        Ok(p) if p > 0 => Some(p),
        _ => None,
    }
}

/// env pin → the daemon's port file → the launcher's last-port record →
/// [`DEFAULT_GATEWAY_PORT`].
///
/// Each step is EVIDENCE; the default is the answer only when there is none.
/// Skipping the last-port record would probe the shipped default on a machine
/// whose gateway fell back to another port — that is, report a healthy gateway
/// as down on every tick, and start a second one on top of it.
///
/// MUST MATCH `model_router.config.resolve_port` (the first two steps are its
/// order; the third is this workspace's own memory, which
/// `vco_lib.vscode_settings.resolve_gateway_ports` also reads).
pub fn resolve_port() -> u16 {
    if let Ok(raw) = std::env::var(PORT_ENV) {
        if let Ok(v) = raw.trim().parse::<u16>() {
            if v > 0 {
                return v;
            }
        }
    }
    read_port_file(&port_path())
        .or_else(|| read_port_file(&last_port_path()))
        .unwrap_or(DEFAULT_GATEWAY_PORT)
}

/// The loopback base URL for a gateway on `port`.
///
/// `127.0.0.1`, never `localhost`: the gateway REFUSES a non-loopback bind and
/// rejects any request whose peer is not loopback, and `localhost` can resolve
/// to a routable address on a misconfigured host — a probe URL that does not
/// match the bind is a probe that reports the wrong thing.
pub fn base_url(port: u16) -> String {
    format!("http://127.0.0.1:{}", port)
}

/// `/health` URL for a gateway on `port`.
pub fn health_url(port: u16) -> String {
    format!("{}/health", base_url(port))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The repo root, from this crate's manifest dir. `None` when the sources
    /// are not beside the build (a packaged binary).
    fn repo_root() -> Option<PathBuf> {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .and_then(Path::parent)
            .map(Path::to_path_buf)
    }

    #[test]
    fn port_file_reader_is_damage_tolerant() {
        let dir = tempfile::tempdir().unwrap();
        let good = dir.path().join("good");
        std::fs::write(&good, "11460\n").unwrap();
        assert_eq!(read_port_file(&good), Some(11460));

        let empty = dir.path().join("empty");
        std::fs::write(&empty, "").unwrap();
        assert_eq!(read_port_file(&empty), None);

        let junk = dir.path().join("junk");
        std::fs::write(&junk, "not-a-port\n").unwrap();
        assert_eq!(read_port_file(&junk), None);

        let zero = dir.path().join("zero");
        std::fs::write(&zero, "0\n").unwrap();
        assert_eq!(read_port_file(&zero), None, "0 is not a port");

        assert_eq!(read_port_file(&dir.path().join("absent")), None);
    }

    #[test]
    fn health_url_is_loopback_and_carries_the_resolved_port() {
        assert_eq!(base_url(11437), "http://127.0.0.1:11437");
        assert_eq!(health_url(11437), "http://127.0.0.1:11437/health");
        assert!(!health_url(11436).contains("localhost"));
        assert!(!health_url(11436).contains("0.0.0.0"));
    }

    /// The (C)-tier mirror's obligation: these literals must still be the ones
    /// the DAEMON uses. Reads the Python source.
    ///
    /// This is the ONE parity pin for the port chain now. Before v0.2.95 the
    /// hub carried a second copy of both the constants and this test, and that
    /// test read the LAUNCHER'S source as well — a three-way agreement
    /// maintained by hand. The Rust side is one home; this pins it to Python.
    #[test]
    fn the_literals_still_match_the_gateway_package() {
        let Some(repo) = repo_root() else { return };
        let py_path = repo.join("claude_mcp_servers/model_router/config.py");
        // Packaged builds do not ship sources; skip rather than fail there.
        if !py_path.is_file() {
            return;
        }
        let py = std::fs::read_to_string(py_path).unwrap();
        for needle in [
            format!("DEFAULT_PORT = {}", DEFAULT_GATEWAY_PORT),
            format!("_PORT_BASENAME = \"{}\"", PORT_BASENAME),
            format!("LAST_PORT_BASENAME = \"{}\"", LAST_PORT_BASENAME),
            format!("SERVICE_NAME = \"{}\"", GATEWAY_SERVICE),
            // The env name is read inline rather than declared as a constant,
            // so this pins the expression the daemon actually evaluates.
            format!("os.environ.get(\"{}\")", PORT_ENV),
        ] {
            assert!(
                py.contains(&needle),
                "the mirrored constant `{}` is no longer in \
                 claude_mcp_servers/model_router/config.py — move BOTH sides, \
                 or every Rust reader probes a port nothing serves",
                needle
            );
        }
    }
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

//! v0.2.106: the argv that maps the HOST user onto a manifest's
//! `runtime.image_user` (the image's non-root `USER`), so a module container
//! can write its bind-mounted volumes and what it writes stays owned by the
//! host user. Both start-argument builders in
//! [`super::container_runtime`] append it (re-exported there).
//!
//! * podman >= 4.3 (every OS: rootless on Linux, the podman machine on
//!   macOS / Windows): `--userns=keep-id:uid=<uid>,gid=<gid>` maps the
//!   invoking user ONTO the image user, so the process keeps the image's
//!   own uid and still owns the mounts. Without a mapping the image user
//!   maps to a subordinate id that cannot write a directory the host user
//!   owns.
//! * podman < 4.3 (Ubuntu 22.04 ships 3.4), or a podman whose version could
//!   not be read: `keep-id`'s `uid=` / `gid=` options do not exist there
//!   (podman refuses the whole `run`), so plain `--userns=keep-id` (host
//!   uid -> the SAME uid inside, every podman since 1.x) plus
//!   `--user <host uid>:<host gid>`: the process runs as the host user,
//!   which is exactly the identity that owns the mounts — the same contract
//!   as docker on Linux below (the image's own files are world-readable,
//!   its writable paths are the mounts). When the host uid is 1000 this is
//!   the image user anyway. An unknown version takes this branch because
//!   it works on EVERY podman; the `uid=` form is used only when 4.3+ is
//!   confirmed.
//! * docker on Linux: `--user <host uid>:<host gid>`.
//! * docker elsewhere (Docker Desktop): nothing, its file sharing maps
//!   ownership itself.
//!
//! The podman version is read once per process (`podman --version`, the
//! client binary only — no daemon contact) and cached; tests inject it per
//! thread ([`with_podman_version_for_thread`]).

use std::collections::HashMap;
use std::io::Read;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use crate::process::CommandExt;

/// The first podman whose `--userns=keep-id` accepts `uid=` / `gid=`.
pub const PODMAN_KEEP_ID_UID_MIN: (u32, u32) = (4, 3);

/// `--version` is a local client call; anything slower is a broken binary.
const VERSION_PROBE_TIMEOUT: Duration = Duration::from_secs(10);

/// [`host_user_mapping_args_for`] with the live host identity, OS and (for
/// podman) engine version.
pub fn host_user_mapping_args(
    engine: &str,
    image_user: Option<crate::manifest::ImageUser>,
) -> Vec<String> {
    if image_user.is_none() {
        return Vec::new();
    }
    let version = if engine == "docker" { None } else { podman_version(engine) };
    host_user_mapping_args_for(engine, image_user, host_uid_gid(), cfg!(target_os = "linux"), version)
}

/// The mapping with host identity, OS and podman version injected (pure).
/// `podman_version` is `(major, minor)`; `None` = unknown (the conservative
/// branch). Ignored for docker.
pub fn host_user_mapping_args_for(
    engine: &str,
    image_user: Option<crate::manifest::ImageUser>,
    host: Option<(u32, u32)>,
    linux: bool,
    podman_version: Option<(u32, u32)>,
) -> Vec<String> {
    let Some(user) = image_user else {
        return Vec::new();
    };
    if engine == "docker" {
        return match (linux, host) {
            (true, Some((uid, gid))) => vec!["--user".into(), format!("{}:{}", uid, gid)],
            _ => Vec::new(),
        };
    }
    if podman_version.is_some_and(|v| v >= PODMAN_KEEP_ID_UID_MIN) {
        return vec![format!("--userns=keep-id:uid={},gid={}", user.uid, user.gid)];
    }
    let mut args = vec!["--userns=keep-id".to_string()];
    if let Some((uid, gid)) = host {
        args.push("--user".into());
        args.push(format!("{}:{}", uid, gid));
    }
    args
}

/// `(major, minor)` from a `podman --version` line (`podman version 3.4.4`,
/// `podman.exe version 5.0.0-rc1`): the first token that starts with a
/// digit. `None` when there is none.
pub fn parse_engine_version(output: &str) -> Option<(u32, u32)> {
    let tok = output
        .split_whitespace()
        .find(|t| t.chars().next().is_some_and(|c| c.is_ascii_digit()))?;
    let mut parts = tok.split('.').map(|p| {
        p.chars().take_while(|c| c.is_ascii_digit()).collect::<String>().parse::<u32>().ok()
    });
    let major = parts.next()??;
    let minor = parts.next().flatten().unwrap_or(0);
    Some((major, minor))
}

thread_local! {
    static VERSION_OVERRIDE: std::cell::Cell<Option<Option<(u32, u32)>>> =
        const { std::cell::Cell::new(None) };
}

/// Restores the previous per-thread version when dropped.
pub struct VersionGuard(Option<Option<(u32, u32)>>);

impl Drop for VersionGuard {
    fn drop(&mut self) {
        VERSION_OVERRIDE.with(|c| c.set(self.0));
    }
}

/// Answer the podman version with `version` (`None` = unreadable) on THIS
/// thread until the guard drops, without running podman — the test seam for
/// the start-argument builders. Production never sets it.
pub fn with_podman_version_for_thread(version: Option<(u32, u32)>) -> VersionGuard {
    VersionGuard(VERSION_OVERRIDE.with(|c| c.replace(Some(version))))
}

/// `(major, minor)`, `None` = could not be read.
type EngineVersion = Option<(u32, u32)>;

static VERSIONS: LazyLock<Mutex<HashMap<String, EngineVersion>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// The podman client's `(major, minor)`, read once per process and engine
/// binary; `None` when it cannot be read.
fn podman_version(engine: &str) -> Option<(u32, u32)> {
    if let Some(v) = VERSION_OVERRIDE.with(|c| c.get()) {
        return v;
    }
    if let Some(v) = VERSIONS.lock().ok().and_then(|m| m.get(engine).copied()) {
        return v;
    }
    let v = probe_version(engine);
    if v.is_none() {
        tracing::warn!(
            "[host-user-mapping] cannot read the {engine} version; mapping the host user with \
             plain --userns=keep-id + --user (works on every podman)"
        );
    }
    if let Ok(mut m) = VERSIONS.lock() {
        m.insert(engine.to_string(), v);
    }
    v
}

fn probe_version(engine: &str) -> Option<(u32, u32)> {
    let mut child = std::process::Command::new(engine)
        .silent()
        .arg("--version")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + VERSION_PROBE_TIMEOUT;
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(10)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    if !status.success() {
        return None;
    }
    let mut out = String::new();
    child.stdout.take()?.read_to_string(&mut out).ok()?;
    parse_engine_version(&out)
}

/// The invoking user's uid / gid (`None` off Unix).
fn host_uid_gid() -> Option<(u32, u32)> {
    #[cfg(unix)]
    {
        // SAFETY: getuid/getgid take no arguments and cannot fail.
        Some(unsafe { (libc::getuid(), libc::getgid()) })
    }
    #[cfg(not(unix))]
    {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const U: Option<crate::manifest::ImageUser> =
        Some(crate::manifest::ImageUser { uid: 1000, gid: 1001 });

    #[test]
    fn parse_reads_major_minor() {
        assert_eq!(parse_engine_version("podman version 3.4.4\n"), Some((3, 4)));
        assert_eq!(parse_engine_version("podman version 4.9.3"), Some((4, 9)));
        assert_eq!(parse_engine_version("podman.exe version 5.0.0-rc1"), Some((5, 0)));
        assert_eq!(parse_engine_version("podman version 4.3.0-dev"), Some((4, 3)));
        assert_eq!(parse_engine_version("podman version 5"), Some((5, 0)));
        assert_eq!(parse_engine_version("podman version unknown"), None);
        assert_eq!(parse_engine_version(""), None);
    }

    /// podman 4.3+ maps the host user ONTO the image user.
    #[test]
    fn podman_4_3_and_later_maps_onto_the_image_user() {
        for v in [(4, 3), (4, 9), (5, 0)] {
            assert_eq!(
                host_user_mapping_args_for("podman", U, Some((1000, 1000)), true, Some(v)),
                vec!["--userns=keep-id:uid=1000,gid=1001".to_string()],
                "{v:?}"
            );
            assert_eq!(
                host_user_mapping_args_for("podman", U, Some((501, 20)), false, Some(v)),
                vec!["--userns=keep-id:uid=1000,gid=1001".to_string()],
            );
        }
    }

    /// podman < 4.3 has no `keep-id:uid=`: plain keep-id + run as the host
    /// user; an unknown version takes the same, works-everywhere branch.
    #[test]
    fn older_or_unknown_podman_runs_as_the_host_user() {
        for v in [Some((3, 4)), Some((4, 2)), Some((2, 0)), None] {
            assert_eq!(
                host_user_mapping_args_for("podman", U, Some((1234, 5678)), true, v),
                vec!["--userns=keep-id".to_string(), "--user".to_string(), "1234:5678".to_string()],
                "{v:?}"
            );
            // No host identity (off Unix): keep-id alone.
            assert_eq!(
                host_user_mapping_args_for("podman", U, None, false, v),
                vec!["--userns=keep-id".to_string()],
            );
        }
    }

    /// Docker ignores the podman version; nothing without an image user.
    #[test]
    fn docker_and_no_image_user_are_unchanged() {
        for v in [Some((3, 4)), Some((4, 9)), None] {
            assert_eq!(
                host_user_mapping_args_for("docker", U, Some((1234, 5678)), true, v),
                vec!["--user".to_string(), "1234:5678".to_string()]
            );
            assert!(host_user_mapping_args_for("docker", U, Some((501, 20)), false, v).is_empty());
            assert!(host_user_mapping_args_for("podman", None, Some((1000, 1000)), true, v).is_empty());
        }
    }

    /// The live entry point reads the injected version (and never probes
    /// for an absent image user or for docker).
    #[test]
    fn the_live_entry_point_uses_the_version() {
        let _v = with_podman_version_for_thread(Some((3, 4)));
        assert_eq!(host_user_mapping_args("podman", U)[0], "--userns=keep-id");
        let _v = with_podman_version_for_thread(Some((4, 3)));
        assert_eq!(host_user_mapping_args("podman", U), vec!["--userns=keep-id:uid=1000,gid=1001"]);
        assert!(host_user_mapping_args("podman", None).is_empty());
    }
}

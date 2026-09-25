// SPDX-License-Identifier: AGPL-3.0-or-later
//! The ONE home for the env keys a scrubbed child process may receive
//! (v0.2.97 R12-bis P2-1).
//!
//! Every spawn site that does `env_clear()` re-injects an allowlist of
//! keys — the decide child (`services::runtime_verdict`), the launcher's
//! `python -m vco_lib.<module>` sandbox (`src/services/vco_lib_bridge`)
//! and the module-plane container spawn's reserved-name table
//! (`services::container_runtime::RESERVED_SPAWN_ENV`). Before this
//! module those were THREE hand-rolled shapes that had already diverged
//! (the decide child had no Windows home keys at all, so a healthy
//! Windows install's `python.exe` could fail to initialize and
//! `Path.home()` / the runtime CLIs' config lookups resolved wrong).
//! They all read THIS table now — one concern, one home.
//!
//! Contract shared by every consumer:
//!   * a key is re-injected only when PRESENT in the parent env; a
//!     missing key stays missing (never re-injected as empty);
//!   * the home family is per-OS — POSIX gets `HOME`, Windows gets the
//!     `USERPROFILE` family plus `SYSTEMROOT`/`COMSPEC` (the classic way
//!     a spawned `python.exe` fails to initialize without them).

/// Keys a scrubbed child receives on EVERY OS: `PATH` (finding helpers),
/// the temp family (atomic-write tempfiles land somewhere writable,
/// Windows + macOS especially), and the locale/session keys the runtime
/// CLIs and locale-aware children read.
pub const CHILD_ENV_KEYS: &[&str] = &[
    "PATH",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USER",
    "LANG",
    "LC_ALL",
    "XDG_RUNTIME_DIR",
];

/// The Windows home + system family. Windows normally has no `HOME`;
/// `Path.home()` / `expanduser("~")` and the runtime CLIs' per-user
/// config lookups (`docker.exe` context resolution, `podman.exe`
/// config) read these instead — without `SYSTEMROOT`/`COMSPEC` a
/// spawned `python.exe` fails to initialize (Winsock/crypto).
pub const CHILD_ENV_KEYS_WINDOWS: &[&str] = &[
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
    "SYSTEMROOT",
    "COMSPEC",
];

/// The POSIX home key — the counterpart of [`CHILD_ENV_KEYS_WINDOWS`].
pub const CHILD_ENV_KEY_POSIX_HOME: &str = "HOME";

/// The union of EVERY key the reinjection tables may hand a child across
/// OSes. This is also the reserved-name table: a module secret taking
/// one of these names would replace the runtime's own value, so
/// `container_runtime::RESERVED_SPAWN_ENV` IS this list — the reserved
/// check and the actual sandbox cannot drift apart.
pub const ALL_CHILD_ENV_KEYS: &[&str] = &[
    "PATH",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USER",
    "LANG",
    "LC_ALL",
    "XDG_RUNTIME_DIR",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
    "SYSTEMROOT",
    "COMSPEC",
];

/// The reinjection keys for the CURRENT OS: the every-OS keys plus this
/// platform's home family.
pub fn keys_for_current_os() -> Vec<&'static str> {
    let mut keys: Vec<&'static str> = CHILD_ENV_KEYS.to_vec();
    #[cfg(target_os = "windows")]
    keys.extend_from_slice(CHILD_ENV_KEYS_WINDOWS);
    #[cfg(not(target_os = "windows"))]
    keys.push(CHILD_ENV_KEY_POSIX_HOME);
    keys
}

/// The present-in-parent `(key, value)` pairs a scrubbed child
/// re-injects on this OS (absent keys stay absent). Both `Command`
/// flavors (std and tokio) consume this through the thin helpers below,
/// so callers never hand-roll the loop — or the table.
pub fn present_pairs() -> Vec<(&'static str, String)> {
    keys_for_current_os()
        .into_iter()
        .filter_map(|key| std::env::var(key).ok().map(|v| (key, v)))
        .collect()
}

/// `present_pairs` applied to a std `Command` — call AFTER the caller's
/// `env_clear()` (this never clears).
pub fn reinject_std(cmd: &mut std::process::Command) {
    for (key, value) in present_pairs() {
        cmd.env(key, value);
    }
}

/// [`reinject_std`] for a tokio `Command` (same table, same contract).
pub fn reinject_tokio(cmd: &mut tokio::process::Command) {
    for (key, value) in present_pairs() {
        cmd.env(key, value);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// R12-bis P2-1: the Windows home + system family is PINNED in the
    /// tables. Pre-fix, the decide child's hand-rolled allowlist had none
    /// of these — a healthy Windows install's verdict child had no home
    /// key at all and `python.exe` could fail to initialize. Removing any
    /// of them again must fail here.
    #[test]
    fn windows_home_and_system_family_is_pinned() {
        for key in [
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "HOMEDRIVE",
            "HOMEPATH",
            "SYSTEMROOT",
            "COMSPEC",
        ] {
            assert!(
                CHILD_ENV_KEYS_WINDOWS.contains(&key),
                "CHILD_ENV_KEYS_WINDOWS lost {key}"
            );
            assert!(
                ALL_CHILD_ENV_KEYS.contains(&key),
                "ALL_CHILD_ENV_KEYS lost the Windows key {key}"
            );
        }
    }

    /// The union table is EXACTLY the per-OS tables: every key comes from
    /// one of them, and every per-OS key is in the union — so
    /// `RESERVED_SPAWN_ENV` (= the union) can never miss a key the
    /// sandbox actually injects, nor forbid one it doesn't.
    #[test]
    fn union_table_is_exactly_the_per_os_tables() {
        for key in ALL_CHILD_ENV_KEYS {
            assert!(
                CHILD_ENV_KEYS.contains(key)
                    || CHILD_ENV_KEYS_WINDOWS.contains(key)
                    || *key == CHILD_ENV_KEY_POSIX_HOME,
                "{key} is in no source table"
            );
        }
        for key in CHILD_ENV_KEYS.iter().chain(CHILD_ENV_KEYS_WINDOWS.iter()) {
            assert!(ALL_CHILD_ENV_KEYS.contains(key), "{key} missing from the union");
        }
        assert!(ALL_CHILD_ENV_KEYS.contains(&CHILD_ENV_KEY_POSIX_HOME));
    }

    /// On POSIX the reinjection set carries `HOME` and never a
    /// Windows-only family key (the mirror assertion runs on Windows CI,
    /// which compiles the `#[cfg]` the other way).
    #[cfg(not(target_os = "windows"))]
    #[test]
    fn posix_keys_carry_home_not_the_windows_family() {
        let keys = keys_for_current_os();
        assert!(keys.contains(&CHILD_ENV_KEY_POSIX_HOME), "keys: {keys:?}");
        for key in CHILD_ENV_KEYS_WINDOWS {
            assert!(!keys.contains(key), "Windows-only {key} on POSIX: {keys:?}");
        }
    }
}

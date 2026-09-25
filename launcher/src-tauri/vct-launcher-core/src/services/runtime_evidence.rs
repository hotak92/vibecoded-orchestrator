// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Copyright (C) VibeCoded Tools — licensed under AGPL-3.0-or-later.
//
//! Where VCO's data is, and why a stale runtime record was NOT switched
//! (v0.2.97 review R10 J2 / J6 / J8) — the pure halves the stale-record
//! reconcile arm of [`super::container_runtime`] needs, split out of that
//! 5 800-line module.
//!
//! Every function here is a declared mirror of `vco_lib.runtime_reconcile`
//! (or, for [`compose_project_name`], `vco_lib.containers`), pinned by ONE
//! fixture both suites run: `tests/fixtures/runtime_data_evidence_cases.json`.
//! The refusal WORDING is not mirrored at all — both languages render the
//! same committed table, `vco_lib/runtime_reconcile_messages.toml` (tier B).

use std::path::{Path, PathBuf};

use super::container_runtime::{other_runtime, VCO_OUR_CONTAINER_NAMES, VCO_VOLUME_NAMES};

/// The compose knobs that turn a service's data mount into a BIND mount of a
/// host folder — the DATA_SOURCE half of `vco_lib.compose_env.DATA_KNOBS`,
/// in its order. MUST MATCH `vco_lib.runtime_reconcile.DATA_SOURCE_KEYS`.
pub const VCO_DATA_SOURCE_KEYS: &[&str] = &[
    "VCT_WEAVIATE_DATA_SOURCE",
    "VCT_OLLAMA_DATA_SOURCE",
    "VCT_CODE_EMBED_CACHE_SOURCE",
];

/// The compose project a compose dir runs under — MUST MATCH
/// `vco_lib.containers.compose_project_name` (the compose identity guard's
/// derivation): a top-level `name:` key in the file wins (optionally
/// quoted, up to whitespace / a quote / `#`); otherwise the directory
/// basename lower-cased, every character outside `[a-z0-9_-]` dropped and
/// leading `-`/`_` trimmed.
pub fn compose_project_name(compose_dir: &Path, compose_text: &str) -> String {
    for line in compose_text.lines() {
        let Some(rest) = line.strip_prefix("name:") else { continue };
        let rest = rest.trim_start();
        let rest = rest.strip_prefix(['\'', '"']).unwrap_or(rest);
        let value: String = rest
            .chars()
            .take_while(|c| !matches!(c, '\'' | '"' | '#') && !c.is_whitespace())
            .collect();
        if !value.is_empty() {
            return value;
        }
    }
    let raw = compose_dir
        .file_name()
        .map(|n| n.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    let kept: String = raw
        .chars()
        .filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == '_' || *c == '-')
        .collect();
    kept.trim_start_matches(['-', '_']).to_string()
}

/// The compose project VCO's own stack runs under (`<root>/infrastructure`),
/// so a volume compose created for VCO carries it in its
/// `com.docker.compose.project` label. `""` without a root. MUST MATCH
/// `vco_lib.runtime_reconcile.vco_compose_project`.
pub fn vco_compose_project(install_root: Option<&Path>) -> String {
    let Some(root) = install_root else {
        return String::new();
    };
    let infra = root.join("infrastructure");
    let text = std::fs::read_to_string(infra.join("docker-compose.yml")).unwrap_or_default();
    compose_project_name(&infra, &text)
}

/// Do these listings show VCO's data under ONE runtime — the runtime a stale
/// record would be SWITCHED TO (the only one this side ever asks about)?
/// A VCO container or a `vco_*` default volume counts on its own; a volume
/// named by a `VCT_*_VOLUME_NAME` override (`names` beyond the defaults) is a
/// name the USER picked, so it counts only when its compose project label
/// (`label_of`) is `own_project` (R10 J8). MUST MATCH
/// `vco_lib.runtime_reconcile.data_evidence` with `corroborate_overrides=True`.
pub fn data_evidence(
    containers: &[String],
    volumes: &[String],
    names: &[String],
    own_project: &str,
    label_of: &dyn Fn(&str) -> Option<String>,
) -> bool {
    if containers.iter().any(|n| VCO_OUR_CONTAINER_NAMES.contains(&n.as_str())) {
        return true;
    }
    names.iter().filter(|n| volumes.contains(n)).any(|name| {
        VCO_VOLUME_NAMES.contains(&name.as_str())
            || (!own_project.is_empty() && label_of(name).as_deref() == Some(own_project))
    })
}

/// The override-named volumes (not a `vco_*` default) present in `volumes`
/// — the only ones [`data_evidence`] may need a label for.
pub fn override_volumes_present<'a>(volumes: &[String], names: &'a [String]) -> Vec<&'a str> {
    names
        .iter()
        .filter(|n| !VCO_VOLUME_NAMES.contains(&n.as_str()) && volumes.contains(n))
        .map(|n| n.as_str())
        .collect()
}

/// Compose's rule for a volume SOURCE: a path (`/`, `.`, `~`, `\` or a
/// drive letter) is a bind mount; anything else names a volume.
fn is_bind_source(value: &str) -> bool {
    let b = value.as_bytes();
    value.starts_with(['/', '.', '~', '\\'])
        || (b.len() >= 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && (b[2] == b'\\' || b[2] == b'/'))
}

/// Every non-empty [`VCO_DATA_SOURCE_KEYS`] value that is a PATH (a bind
/// mount), in `env_file_text` (file order, the `vco_lib.envfile` line rule)
/// then in `env` (key order), each once. MUST MATCH
/// `vco_lib.runtime_reconcile.bind_sources_from` (the shared fixture).
pub fn bind_sources_from(env_file_text: &str, env: &dyn Fn(&str) -> Option<String>) -> Vec<String> {
    let mut found: Vec<String> = Vec::new();
    let mut add = |value: &str| {
        let v = value.trim();
        if !v.is_empty() && is_bind_source(v) && !found.iter().any(|f| f == v) {
            found.push(v.to_string());
        }
    };
    for line in env_file_text.lines() {
        if let Some((k, v)) = super::container_runtime::parse_env_line(line) {
            if VCO_DATA_SOURCE_KEYS.contains(&k.as_str()) {
                add(&v);
            }
        }
    }
    for key in VCO_DATA_SOURCE_KEYS {
        if let Some(v) = env(key) {
            add(&v);
        }
    }
    found
}

fn home_dir() -> Option<PathBuf> {
    #[cfg(windows)]
    let raw = std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME"));
    #[cfg(not(windows))]
    let raw = std::env::var_os("HOME");
    raw.filter(|h| !h.is_empty()).map(PathBuf::from)
}

/// The first bind-mounted data folder of this install that EXISTS and is not
/// empty (or cannot be listed — not provably empty), else `None`. Relative
/// sources resolve against `infrastructure/` (compose's project dir), `~`
/// against the home directory. Such a folder is VCO's data on the HOST, under
/// neither runtime (R10 J2) — so no runtime's listing can justify switching
/// the record. MUST MATCH `vco_lib.runtime_reconcile.bind_data_source`.
pub fn bind_data_source(install_root: Option<&Path>) -> Option<PathBuf> {
    let root = install_root?;
    let infra = root.join("infrastructure");
    let text = std::fs::read_to_string(infra.join(".env")).unwrap_or_default();
    for src in bind_sources_from(&text, &|k| std::env::var(k).ok()) {
        let expanded = match src.strip_prefix('~') {
            Some(rest) if rest.is_empty() || rest.starts_with(['/', '\\']) => match home_dir() {
                Some(home) => home.join(rest.trim_start_matches(['/', '\\'])),
                None => PathBuf::from(&src),
            },
            _ => PathBuf::from(&src),
        };
        let joined = if expanded.is_absolute() { expanded } else { infra.join(expanded) };
        // `.` components dropped, as pathlib does — the folder is named in
        // the refusal text, which must read the same from both languages.
        let path: PathBuf = joined
            .components()
            .filter(|c| !matches!(c, std::path::Component::CurDir))
            .collect();
        if !path.is_dir() {
            continue;
        }
        let empty = match std::fs::read_dir(&path) {
            Ok(mut entries) => entries.next().is_none(),
            Err(_) => false, // a data folder this user cannot list is not provably empty
        };
        if !empty {
            return Some(path);
        }
    }
    None
}

/// WHY the read-only stale-record reconcile did NOT switch — each variant is
/// a key of `[not_switched]` in `vco_lib/runtime_reconcile_messages.toml`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReconcileDecline {
    /// The record is the user's `install.py --container` choice (R9 H2).
    Confirmed,
    /// VCO's data is a bind-mounted host folder (R10 J2).
    BindData,
    /// The other runtime does not answer.
    OtherNotUsable,
    /// The other runtime's containers/volumes could not be listed.
    Unlistable,
    /// The other runtime holds none of VCO's data (R9 H1: not evidence).
    NoData,
}

impl ReconcileDecline {
    pub fn key(self) -> &'static str {
        match self {
            ReconcileDecline::Confirmed => "confirmed",
            ReconcileDecline::BindData => "bind_data",
            ReconcileDecline::OtherNotUsable => "other_not_usable",
            ReconcileDecline::Unlistable => "unlistable",
            ReconcileDecline::NoData => "no_data",
        }
    }
}

/// The shared wording table — the SAME file `vco_lib.runtime_reconcile`
/// renders (`MESSAGES_PATH`).
const MESSAGES_TOML: &str = include_str!("../../../../../vco_lib/runtime_reconcile_messages.toml");

#[derive(serde::Deserialize)]
struct Messages {
    format_version: u32,
    unusable: std::collections::HashMap<String, String>,
    not_switched: std::collections::HashMap<String, String>,
}

static MESSAGES: std::sync::LazyLock<Messages> = std::sync::LazyLock::new(|| {
    let parsed: Messages = toml::from_str(MESSAGES_TOML)
        .expect("vco_lib/runtime_reconcile_messages.toml is committed and parses");
    assert_eq!(parsed.format_version, 1, "runtime_reconcile_messages.toml format_version");
    parsed
});

/// `{name}` → value, literally — what `vco_lib.runtime_reconcile._fill` does.
fn fill(template: &str, values: &[(&str, &str)]) -> String {
    let mut out = template.to_string();
    for (key, value) in values {
        out = out.replace(&format!("{{{key}}}"), value);
    }
    out
}

fn message(section: &std::collections::HashMap<String, String>, key: &str) -> String {
    section
        .get(key)
        .cloned()
        .unwrap_or_else(|| panic!("runtime_reconcile_messages.toml lacks {key:?}"))
}

/// Why the recorded `pin` (not installed — the only state this arm reaches)
/// was not switched, as `vco_lib.runtime_reconcile.unusable_detail(pin,
/// source, "missing", decline, bind=bind)` renders it: the SAME table, the
/// same substitution. `source` is where the pin came from (the runtime.txt
/// path). The Python refusal carries it as "(not switched: <this>)", and so
/// does the launcher's / hub's ([`not_switched_clause`]).
pub fn record_reconcile_note(pin: &str, source: &str, decline: ReconcileDecline, bind: &str) -> String {
    let m = &*MESSAGES;
    let other = other_runtime(pin);
    let what = fill(&message(&m.unusable, "missing"), &[("pinned", pin)])
        + &fill(
            &message(&m.not_switched, decline.key()),
            &[("pinned", pin), ("other", other), ("bind", bind)],
        );
    fill(&message(&m.unusable, "head"), &[("pinned", pin), ("source", source), ("what", &what)])
}

/// The clause a refusal gains — `vco_lib.containers.resolve` appends the same.
pub fn not_switched_clause(note: &str) -> String {
    format!(" (not switched: {note})")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn fixture() -> serde_json::Value {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../tests/fixtures/runtime_data_evidence_cases.json");
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        serde_json::from_str(&text).expect("fixture parses")
    }

    fn env_of(case: &serde_json::Value) -> HashMap<String, String> {
        case["env"]
            .as_object()
            .map(|m| m.iter().map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string())).collect())
            .unwrap_or_default()
    }

    fn strings(v: &serde_json::Value) -> Vec<String> {
        v.as_array().unwrap().iter().map(|s| s.as_str().unwrap().to_string()).collect()
    }

    #[test]
    fn compose_project_matches_the_shared_fixture() {
        let fx = fixture();
        let cases = fx["compose_project"].as_array().unwrap();
        assert!(cases.len() >= 4, "fixture shrank");
        for case in cases {
            let dir = Path::new("/x").join(case["compose_dir"].as_str().unwrap());
            assert_eq!(
                compose_project_name(&dir, case["compose_text"].as_str().unwrap()),
                case["expect"].as_str().unwrap(),
                "case {}",
                case["name"]
            );
        }
    }

    #[test]
    fn bind_sources_match_the_shared_fixture() {
        let fx = fixture();
        let cases = fx["bind_sources"].as_array().unwrap();
        assert!(cases.len() >= 4, "fixture shrank");
        for case in cases {
            let env = env_of(case);
            let got = bind_sources_from(case["env_file"].as_str().unwrap(), &|k| env.get(k).cloned());
            assert_eq!(got, strings(&case["expect"]), "case {}", case["name"]);
        }
    }

    /// R10 J8: the runtime a stale record would switch TO holds VCO's data
    /// only on corroborated evidence.
    #[test]
    fn switch_evidence_matches_the_shared_fixture() {
        let fx = fixture();
        let cases = fx["evidence"].as_array().unwrap();
        assert!(cases.len() >= 6, "fixture shrank");
        for case in cases {
            let env = env_of(case);
            let names = super::super::container_runtime::volume_names_from(
                case["env_file"].as_str().unwrap(),
                &|k| env.get(k).cloned(),
            );
            let labels = case["labels"].as_object().unwrap().clone();
            let got = data_evidence(
                &strings(&case["containers"]),
                &strings(&case["volumes"]),
                &names,
                case["own_project"].as_str().unwrap(),
                &|v| labels.get(v).and_then(|l| l.as_str()).map(str::to_string),
            );
            assert_eq!(got, case["expect"].as_bool().unwrap(), "case {}", case["name"]);
        }
    }

    /// R10 J6: the "not switched" wording is the Python wording, key by key.
    #[test]
    fn not_switched_wording_matches_the_shared_fixture() {
        let fx = fixture();
        let cases = fx["not_switched"].as_array().unwrap();
        let all = [
            ReconcileDecline::Confirmed,
            ReconcileDecline::BindData,
            ReconcileDecline::OtherNotUsable,
            ReconcileDecline::Unlistable,
            ReconcileDecline::NoData,
        ];
        for decline in all {
            assert!(
                cases.iter().any(|c| c["decline"].as_str() == Some(decline.key())),
                "no fixture row for {decline:?}"
            );
        }
        for case in cases {
            let decline = *all
                .iter()
                .find(|d| Some(d.key()) == case["decline"].as_str())
                .expect("a known decline key");
            let got = record_reconcile_note(
                case["pinned"].as_str().unwrap(),
                case["source"].as_str().unwrap(),
                decline,
                case["bind"].as_str().unwrap(),
            );
            assert_eq!(got, case["expect"].as_str().unwrap(), "case {}", case["name"]);
        }
    }

    #[test]
    fn a_non_empty_bind_folder_is_data_and_an_empty_one_is_not() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("clone");
        let folder = dir.path().join("srv-weaviate");
        std::fs::create_dir_all(root.join("infrastructure")).unwrap();
        std::fs::create_dir_all(&folder).unwrap();
        std::fs::write(
            root.join("infrastructure").join(".env"),
            format!("VCT_WEAVIATE_DATA_SOURCE={}\nVCT_WEAVIATE_VOLUME_NAME=\n", folder.display()),
        )
        .unwrap();
        assert_eq!(bind_data_source(Some(&root)), None, "an empty folder is not data");
        std::fs::write(folder.join("classifications.db"), "x").unwrap();
        assert_eq!(bind_data_source(Some(&root)), Some(folder.clone()));
        assert_eq!(bind_data_source(None), None);
    }

    #[test]
    fn a_relative_bind_source_resolves_against_infrastructure() {
        let dir = tempfile::tempdir().unwrap();
        let infra = dir.path().join("infrastructure");
        std::fs::create_dir_all(infra.join("data").join("ollama")).unwrap();
        std::fs::write(infra.join("data").join("ollama").join("blob"), "x").unwrap();
        std::fs::write(infra.join(".env"), "VCT_OLLAMA_DATA_SOURCE=./data/ollama\n").unwrap();
        assert_eq!(
            bind_data_source(Some(dir.path())),
            Some(infra.join("data").join("ollama"))
        );
    }

    #[test]
    fn vcos_compose_project_reads_the_installer_compose() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
        let infra = root.join("infrastructure");
        let text = std::fs::read_to_string(infra.join("docker-compose.yml")).unwrap();
        assert_eq!(vco_compose_project(Some(&root)), compose_project_name(&infra, &text));
        assert_eq!(vco_compose_project(None), "");
    }
}

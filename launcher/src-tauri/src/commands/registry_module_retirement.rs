// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.106: what a module leaves behind when it moves from a REGISTRY image
//! (`install.method = "container_pull"`, the paid RL reranker up to v0.2.105)
//! to a BUNDLED build (`container_build`, built from this checkout).
//!
//! Called by `modules::install_module_for_project` once the bundled container
//! has STARTED (a migration that did not reach a running container retires
//! nothing — the old image is still the fallback). Three things, each tested
//! both ways (act + leave-alone):
//!
//! 1. **The registry image** (`<previous install.container.image>:<tag>`,
//!    several GB): removed with a plain `rmi` per tag — never forced, so an
//!    image a container still uses (even a stopped one) stays, and the
//!    refusal is only logged. Regenerable, not user data. Never the new
//!    bundled image (a repository equal to the new one is refused).
//! 2. **Launcher-placed default weights**: the download cache
//!    (`<vct_root>/modules/<id>/weights/`) and the per-project weights slots
//!    of the old layout (`<vct_root>/data/<id>/<slug>/state/`). Only files
//!    the ownership table (`vco_lib/rl_weights_ownership.toml`) calls
//!    MANAGED are removed — a symlink, or a copy with its marker (removed
//!    with it); every other file is the user's and stays.
//! 3. **The old per-project data dirs** (`<vct_root>/data/<id>/<slug>/state/`)
//!    are NEVER deleted or moved — they hold what the old container trained.
//!    When one still holds files, the install reports a notice naming it, so
//!    the user knows the data exists and that the bundled module does not
//!    read it (its own mounts are `<vct_root>/data/<id>/_bundled/…`, a name
//!    no project slug can take: slugs are `[a-z0-9-]`).

use std::path::{Path, PathBuf};
use std::process::Stdio;

use tokio::process::Command;
use vct_launcher_core::process::CommandExt as _;

use crate::commands::module_default_weights::{
    classify_weights_owner, managed_marker_path, managed_marker_suffix, WeightsOwner,
};
use crate::manifest::{InstallMethod, ModuleManifest};

/// What the retirement did (audited; `notices` reach the user).
#[derive(Debug, Default, Clone, PartialEq, Eq, serde::Serialize)]
pub struct RetirementReport {
    /// `repo:tag` references removed.
    pub images_removed: Vec<String>,
    /// `repo:tag` references the runtime refused to remove (still in use).
    pub images_kept: Vec<String>,
    /// Managed weights files (and their markers) removed.
    pub weights_removed: Vec<PathBuf>,
    /// The user's own files left in place (count).
    pub user_files_kept: usize,
    /// One line per old data dir that still holds files.
    pub notices: Vec<String>,
}

/// The registry image repository to retire when `previous` was a registry
/// install of the same module that `current` now builds locally; `None`
/// for every other pair (nothing to retire).
pub fn retired_registry_image(previous: &ModuleManifest, current: &ModuleManifest) -> Option<String> {
    if previous.id != current.id
        || previous.install.method != InstallMethod::ContainerPull
        || current.install.method != InstallMethod::ContainerBuild
    {
        return None;
    }
    let old = previous.install.container.as_ref()?.image.trim().to_string();
    let new = current.install.container.as_ref().map(|c| c.image.trim().to_string());
    if old.is_empty() || Some(&old) == new.as_ref() {
        return None;
    }
    Some(old)
}

/// The old layout's per-project weights/state dirs:
/// `<data_dir>/<module_id>/<slug>/state` for every slug present. Only
/// slug-shaped children count (`[a-z0-9-]+`, `db::slug`), so the bundled
/// module's own `_bundled/` mounts are never taken for old data.
pub fn legacy_state_dirs(data_dir: &Path, module_id: &str) -> Vec<PathBuf> {
    let root = data_dir.join(module_id);
    let Ok(entries) = std::fs::read_dir(&root) else {
        return Vec::new();
    };
    let slug_shaped = |n: &str| {
        !n.is_empty() && n.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
    };
    let mut out: Vec<PathBuf> = entries
        .filter_map(|e| e.ok())
        .filter(|e| slug_shaped(&e.file_name().to_string_lossy()))
        .map(|e| e.path().join("state"))
        .filter(|p| p.is_dir())
        .collect();
    out.sort();
    out
}

/// Remove the MANAGED weights files directly in `dir` (symlinks; copies
/// with their marker, marker included). Returns `(removed, user_files_kept)`.
/// Subdirectories and every other file are left alone.
pub fn remove_managed_weights(dir: &Path) -> (Vec<PathBuf>, usize) {
    let mut removed = Vec::new();
    let mut kept = 0usize;
    let Ok(entries) = std::fs::read_dir(dir) else {
        return (removed, kept);
    };
    let mut paths: Vec<PathBuf> = entries.filter_map(|e| e.ok()).map(|e| e.path()).collect();
    paths.sort();
    for p in paths {
        let Ok(meta) = std::fs::symlink_metadata(&p) else { continue };
        if meta.is_dir() {
            continue;
        }
        let is_symlink = meta.file_type().is_symlink();
        let name = p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
        // A marker is judged with the file it marks, never on its own.
        if !is_symlink && name.ends_with(managed_marker_suffix()) {
            continue;
        }
        let marker = managed_marker_path(&p);
        let has_marker = !is_symlink && marker.is_file();
        match classify_weights_owner(is_symlink, has_marker) {
            WeightsOwner::Managed => match std::fs::remove_file(&p) {
                Ok(()) => {
                    if has_marker {
                        let _ = std::fs::remove_file(&marker);
                    }
                    removed.push(p);
                }
                Err(e) => {
                    tracing::warn!("[module_retirement] could not remove {}: {}", p.display(), e);
                }
            },
            WeightsOwner::User => kept += 1,
        }
    }
    (removed, kept)
}

/// True when `dir` holds anything at all (files or subdirectories).
fn has_entries(dir: &Path) -> bool {
    std::fs::read_dir(dir).map(|mut it| it.next().is_some()).unwrap_or(false)
}

/// `repo:tag` of every local image of `repo` (untagged ones skipped).
async fn list_repo_images(runtime: &str, repo: &str) -> Vec<String> {
    let out = Command::new(runtime)
        .silent()
        .args(["images", "--format", "{{.Repository}}:{{.Tag}}", "--filter"])
        .arg(format!("reference={}", repo))
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .await;
    let Ok(out) = out else { return Vec::new() };
    if !out.status.success() {
        return Vec::new();
    }
    let mut refs: Vec<String> = String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(str::trim)
        .filter(|l| {
            l.rsplit_once(':')
                .is_some_and(|(r, t)| r == repo && !t.is_empty() && t != "<none>")
        })
        .map(str::to_string)
        .collect();
    refs.sort();
    refs.dedup();
    refs
}

/// Remove every tag of `repo` with a plain (non-forced) `rmi`.
async fn remove_repo_images(runtime: &str, repo: &str, report: &mut RetirementReport) {
    for r in list_repo_images(runtime, repo).await {
        let ok = Command::new(runtime)
            .silent()
            .args(["rmi", &r])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await
            .map(|s| s.success())
            .unwrap_or(false);
        if ok {
            report.images_removed.push(r);
        } else {
            tracing::info!("[module_retirement] {} kept (still in use, or the runtime refused)", r);
            report.images_kept.push(r);
        }
    }
}

/// The whole retirement for a module that just started from its bundled
/// build. `vct_root` is `<vct_root_dir>` (tests pass a tempdir); `runtime`
/// the container engine binary. A pair that is not a registry → bundled
/// migration retires nothing and returns an empty report.
pub async fn retire_registry_install(
    previous: &ModuleManifest,
    current: &ModuleManifest,
    runtime: &str,
    vct_root: &Path,
) -> RetirementReport {
    let mut report = RetirementReport::default();
    let Some(repo) = retired_registry_image(previous, current) else {
        return report;
    };
    remove_repo_images(runtime, &repo, &mut report).await;

    let module_id = current.id.as_str();
    let cache = vct_root.join("modules").join(module_id).join("weights");
    let (removed, kept) = remove_managed_weights(&cache);
    report.weights_removed.extend(removed);
    report.user_files_kept += kept;
    if cache.is_dir() && !has_entries(&cache) {
        let _ = std::fs::remove_dir(&cache);
    }

    for dir in legacy_state_dirs(&vct_root.join("data"), module_id) {
        let (removed, kept) = remove_managed_weights(&dir);
        report.weights_removed.extend(removed);
        report.user_files_kept += kept;
        if has_entries(&dir) {
            report.notices.push(format!(
                "{}: the data of the earlier (registry) {} install is kept at {} — the bundled \
                 module does not read it; delete it when you no longer need it.",
                current.name,
                module_id,
                dir.display()
            ));
        }
    }
    report
}

#[cfg(test)]
mod tests {
    use super::*;

    fn manifest(method: &str, image: &str) -> ModuleManifest {
        let json = serde_json::json!({
            "manifest_version": 1,
            "id": "vct-rl-reranker",
            "name": "RL Reranker",
            "version": "0.3.1",
            "description": "x",
            "category": "core",
            "compatibility": { "hosts": ["base"] },
            "license": { "required": false },
            "install": {
                "method": method,
                "container": { "image": image, "tag_from_version": true,
                               "pull_token_endpoint": "https://example.org/x" },
                "scope": "global"
            },
            "runtime": { "type": "service" }
        });
        serde_json::from_value(json).expect("fixture manifest")
    }

    const OLD: &str = "ghcr.io/example/vct-rl-reranker";
    const NEW: &str = "localhost/vco-rl-reranker";

    #[test]
    fn only_a_registry_to_bundled_move_retires_an_image() {
        let pull = manifest("container_pull", OLD);
        let build = manifest("container_build", NEW);
        assert_eq!(retired_registry_image(&pull, &build).as_deref(), Some(OLD));
        // leave-alone: build -> build, pull -> pull, same repo, other module
        assert_eq!(retired_registry_image(&build, &build), None);
        assert_eq!(retired_registry_image(&pull, &manifest("container_pull", OLD)), None);
        assert_eq!(retired_registry_image(&manifest("container_pull", NEW), &build), None);
        let mut other = build.clone();
        other.id = "vct-other".into();
        assert_eq!(retired_registry_image(&pull, &other), None);
    }

    #[cfg(unix)]
    fn managed_and_user_files(dir: &Path) -> (PathBuf, PathBuf, PathBuf) {
        std::fs::create_dir_all(dir).unwrap();
        let target = dir.join("download.bin");
        std::fs::write(&target, b"w").unwrap();
        let link = dir.join("rl_model_qwen3_0.2.9.pt");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        let copy = dir.join("rl_model_arctic_0.2.9.pt");
        std::fs::write(&copy, b"c").unwrap();
        std::fs::write(managed_marker_path(&copy), b"").unwrap();
        let user = dir.join("rl_model_qwen3_local.pt");
        std::fs::write(&user, b"trained").unwrap();
        (link, copy, user)
    }

    #[cfg(unix)]
    #[test]
    fn managed_weights_go_and_the_users_own_files_stay() {
        let tmp = tempfile::tempdir().unwrap();
        let (link, copy, user) = managed_and_user_files(tmp.path());
        let (removed, kept) = remove_managed_weights(tmp.path());
        assert_eq!(removed, vec![copy.clone(), link.clone()]);
        assert!(!link.exists() && std::fs::symlink_metadata(&link).is_err());
        assert!(!copy.exists() && !managed_marker_path(&copy).exists());
        assert!(user.is_file(), "a user file is never removed");
        assert!(tmp.path().join("download.bin").is_file(), "an unmarked file is the user's");
        assert_eq!(kept, 2);
    }

    /// A fake engine: `images` prints the listing file, `rmi <ref>` succeeds
    /// unless the ref is named in the in-use file, and every call is logged.
    #[cfg(unix)]
    fn fake_engine(dir: &Path, listing: &str, in_use: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        std::fs::write(dir.join("listing.txt"), listing).unwrap();
        std::fs::write(dir.join("in_use.txt"), in_use).unwrap();
        let script = dir.join("fake-engine");
        let body = format!(
            "#!/bin/sh\nD={d}\necho \"$*\" >> \"$D/calls.txt\"\ncase \"$1\" in\n  images) cat \
             \"$D/listing.txt\";;\n  rmi) grep -qx \"$2\" \"$D/in_use.txt\" && exit 1; exit 0;;\nesac\n",
            d = dir.display()
        );
        std::fs::write(&script, body).unwrap();
        let mut perms = std::fs::metadata(&script).unwrap().permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(&script, perms).unwrap();
        script
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn a_migration_removes_the_registry_image_and_managed_weights_and_names_kept_data() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("vct");
        let cache = root.join("modules/vct-rl-reranker/weights");
        let (cache_link, _, _) = managed_and_user_files(&cache);
        std::fs::remove_file(cache.join("rl_model_qwen3_local.pt")).unwrap();
        std::fs::remove_file(cache.join("download.bin")).unwrap();
        let slug_state = root.join("data/vct-rl-reranker/proj/state");
        let (_, _, user) = managed_and_user_files(&slug_state);
        let empty_slug = root.join("data/vct-rl-reranker/global/state");
        std::fs::create_dir_all(&empty_slug).unwrap();
        // the bundled module's own mount: never old data, never touched
        let bundled_state = root.join("data/vct-rl-reranker/_bundled/state");
        let (bundled_link, _, _) = managed_and_user_files(&bundled_state);
        let engine = fake_engine(
            tmp.path(),
            &format!("{OLD}:0.2.9-cuda\n{OLD}:0.1.0\n{OLD}:<none>\nother/repo:1\n"),
            &format!("{OLD}:0.1.0\n"),
        );

        let report = retire_registry_install(
            &manifest("container_pull", OLD),
            &manifest("container_build", NEW),
            engine.to_str().unwrap(),
            &root,
        )
        .await;

        assert_eq!(report.images_removed, vec![format!("{OLD}:0.2.9-cuda")]);
        assert_eq!(report.images_kept, vec![format!("{OLD}:0.1.0")]);
        let calls = std::fs::read_to_string(tmp.path().join("calls.txt")).unwrap();
        assert!(!calls.contains("rmi -f") && !calls.contains("--force"), "never forced: {calls}");
        assert!(!calls.contains("rmi other/repo"), "only the retired repository: {calls}");
        assert!(std::fs::symlink_metadata(&cache_link).is_err());
        assert!(!cache.exists(), "an emptied download cache dir is removed");
        // the old data dirs stay, with the user's file; only the one with
        // content is named to the user
        assert!(user.is_file() && slug_state.is_dir() && empty_slug.is_dir());
        assert_eq!(report.notices.len(), 1, "{:?}", report.notices);
        assert!(std::fs::symlink_metadata(&bundled_link).is_ok(), "the bundled mount is left alone");
        assert!(report.notices[0].contains(&slug_state.display().to_string()));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn no_migration_touches_nothing() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("vct");
        let slug_state = root.join("data/vct-rl-reranker/proj/state");
        let (link, copy, user) = managed_and_user_files(&slug_state);
        let engine = fake_engine(tmp.path(), &format!("{OLD}:0.2.9-cuda\n"), "");
        let build = manifest("container_build", NEW);
        let report = retire_registry_install(&build, &build, engine.to_str().unwrap(), &root).await;
        assert_eq!(report, RetirementReport::default());
        assert!(!tmp.path().join("calls.txt").exists(), "the engine was never called");
        assert!(std::fs::symlink_metadata(&link).is_ok() && copy.is_file() && user.is_file());
    }
}

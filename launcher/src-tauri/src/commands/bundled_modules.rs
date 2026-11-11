// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.106: modules whose source ships INSIDE the orchestrator checkout.
//!
//! A bundled module is installed with `install.method = "container_build"`:
//! its image is built locally from a directory of this checkout (the RL
//! reranker: `claude_mcp_servers/rl_reranker`), never pulled from a
//! registry, and it needs no licence. Its manifest is the checkout's own
//! `<dir>/vct-module.json` — the single source of truth for its version, so
//! updating the orchestrator is what updates the module.
//!
//! This file is the ONE home for that logic; the call sites are:
//! * `modules::list_module_catalog_impl_with_l0` — the catalog card
//!   ([`list_bundled_manifests`]), independent of the L0 catalog (works
//!   offline, and replaces any older paid-catalog record with the same id);
//! * `modules::resolve_manifest_for_install` — install AND update read the
//!   checkout manifest first ([`read_bundled_manifest`]);
//! * `installer_engine` — build the image ([`container_build`]) and
//!   materialise `~/.vct/modules/<id>/` (manifest + DB migrations) from the
//!   build context ([`materialize_install_dir`]), so every reader of the
//!   installed manifest works unchanged;
//! * `module_updates` — update availability compares installed rows against
//!   [`bundled_versions`].

use std::path::{Component, Path, PathBuf};
use std::process::Stdio;

use tokio::process::Command;
use vct_launcher_core::process::CommandExt as _;

use crate::commands::gpu_policy::GpuMode;
use crate::manifest::{InstallMethod, ModuleManifest};

/// A module that ships in the checkout: its id and its directory, relative
/// to the orchestrator root.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BundledModule {
    pub id: &'static str,
    pub dir: &'static str,
}

/// Every bundled module. Adding one is a source change (a directory with a
/// `Dockerfile` and a `vct-module.json` using `container_build`), not data.
pub const BUNDLED_MODULES: &[BundledModule] = &[BundledModule {
    id: "vct-rl-reranker",
    dir: "claude_mcp_servers/rl_reranker",
}];

/// File name of a module manifest.
const MANIFEST_FILE: &str = "vct-module.json";

/// The registry entry for `module_id`, if it is bundled.
pub fn bundled_entry(module_id: &str) -> Option<&'static BundledModule> {
    BUNDLED_MODULES.iter().find(|m| m.id == module_id)
}

/// True when `module_id` ships in the checkout.
pub fn is_bundled(module_id: &str) -> bool {
    bundled_entry(module_id).is_some()
}

/// The orchestrator checkout this binary belongs to.
fn checkout_root() -> Result<PathBuf, String> {
    crate::commands::installer::find_local_repo_root()
}

/// Path of the bundled manifest for `module_id` under `root`.
pub fn manifest_path_in(root: &Path, module_id: &str) -> Option<PathBuf> {
    bundled_entry(module_id).map(|m| root.join(m.dir).join(MANIFEST_FILE))
}

/// Read + validate the bundled manifest for `module_id` under `root`.
///
/// `None` when the module is not bundled. `Some(Err)` when it is bundled
/// but its manifest is missing, unparseable, names another id, or does not
/// use `container_build` — a broken checkout, surfaced to the caller.
pub fn read_bundled_manifest_in(
    root: &Path,
    module_id: &str,
) -> Option<Result<(ModuleManifest, PathBuf), String>> {
    let path = manifest_path_in(root, module_id)?;
    Some(read_and_validate(&path, module_id).map(|m| (m, path)))
}

/// [`read_bundled_manifest_in`] against this binary's checkout.
pub fn read_bundled_manifest(module_id: &str) -> Option<Result<(ModuleManifest, PathBuf), String>> {
    if !is_bundled(module_id) {
        return None;
    }
    match checkout_root() {
        Ok(root) => read_bundled_manifest_in(&root, module_id),
        Err(e) => Some(Err(format!(
            "bundled module {}: cannot locate the orchestrator checkout: {}",
            module_id, e
        ))),
    }
}

fn read_and_validate(path: &Path, module_id: &str) -> Result<ModuleManifest, String> {
    let raw = std::fs::read_to_string(path)
        .map_err(|e| format!("bundled module {}: read {}: {}", module_id, path.display(), e))?;
    let manifest = ModuleManifest::from_json(&raw)
        .map_err(|e| format!("bundled module {}: parse {}: {}", module_id, path.display(), e))?;
    if manifest.id != module_id {
        return Err(format!(
            "bundled module {}: {} declares id {:?}",
            module_id,
            path.display(),
            manifest.id
        ));
    }
    if manifest.install.method != InstallMethod::ContainerBuild {
        return Err(format!(
            "bundled module {}: {} must use install.method \"container_build\"",
            module_id,
            path.display()
        ));
    }
    Ok(manifest)
}

/// Every bundled manifest that reads cleanly from `root`, with its path.
/// A broken one is logged and left out (the catalog must still render).
pub fn list_bundled_manifests_in(root: &Path) -> Vec<(ModuleManifest, PathBuf)> {
    BUNDLED_MODULES
        .iter()
        .filter_map(|m| match read_bundled_manifest_in(root, m.id)? {
            Ok(pair) => Some(pair),
            Err(e) => {
                tracing::error!("[bundled_modules] {}", e);
                None
            }
        })
        .collect()
}

/// [`list_bundled_manifests_in`] against this binary's checkout (empty
/// when the checkout cannot be located).
pub fn list_bundled_manifests() -> Vec<(ModuleManifest, PathBuf)> {
    match checkout_root() {
        Ok(root) => list_bundled_manifests_in(&root),
        Err(e) => {
            tracing::warn!("[bundled_modules] checkout root unresolved: {}", e);
            Vec::new()
        }
    }
}

/// `(module_id, version)` for every bundled module — what `module_updates`
/// compares installed rows against.
pub fn bundled_versions() -> Vec<(String, String)> {
    list_bundled_manifests()
        .into_iter()
        .map(|(m, _)| (m.id, m.version))
        .collect()
}

/// What an image build reads: the context directory and the Dockerfile.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BuildInputs {
    pub context_dir: PathBuf,
    pub dockerfile: PathBuf,
}

/// `rel` under `root`, refusing an absolute path or any `..` component — a
/// build input must stay inside the checkout.
fn checkout_relative(root: &Path, rel: &str, what: &str, module_id: &str) -> Result<PathBuf, String> {
    let rel_path = Path::new(rel);
    if rel_path.is_absolute()
        || rel_path
            .components()
            .any(|c| !matches!(c, Component::Normal(_) | Component::CurDir))
    {
        return Err(format!(
            "module {}: {} {:?} must be a relative path inside the checkout",
            module_id, what, rel
        ));
    }
    Ok(root.join(rel_path))
}

/// The image build inputs: `install.container.build_context` and
/// `install.container.dockerfile` (default `<build_context>/Dockerfile`),
/// both resolved under `root` and both refused outside it. The Dockerfile
/// must exist.
pub fn build_inputs(root: &Path, manifest: &ModuleManifest) -> Result<BuildInputs, String> {
    let container = manifest.install.container.as_ref();
    let rel = container
        .and_then(|c| c.build_context.as_deref())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            format!(
                "module {}: install.container.build_context is required for container_build",
                manifest.id
            )
        })?;
    let context_dir = checkout_relative(root, rel, "build_context", &manifest.id)?;
    let dockerfile = match container
        .and_then(|c| c.dockerfile.as_deref())
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        Some(df) => checkout_relative(root, df, "dockerfile", &manifest.id)?,
        None => context_dir.join("Dockerfile"),
    };
    if !dockerfile.is_file() {
        return Err(format!(
            "module {}: Dockerfile not found: {}",
            manifest.id,
            dockerfile.display()
        ));
    }
    Ok(BuildInputs { context_dir, dockerfile })
}

/// The `VARIANT` build argument for a GPU mode — the same three-way split
/// `installer_engine::resolve_variant_tag` uses for the image tag (Metal
/// has no dedicated torch wheel and builds the CPU variant).
pub fn variant_build_arg(gpu_mode: GpuMode) -> &'static str {
    match gpu_mode {
        GpuMode::Cuda => "cuda",
        GpuMode::Rocm => "rocm",
        GpuMode::Cpu | GpuMode::Metal => "cpu",
    }
}

/// Arguments after the engine binary for one image build.
pub fn build_argv(image_ref: &str, variant: &str, inputs: &BuildInputs) -> Vec<String> {
    vec![
        "build".to_string(),
        "--build-arg".to_string(),
        format!("VARIANT={}", variant),
        "--file".to_string(),
        inputs.dockerfile.display().to_string(),
        "--tag".to_string(),
        image_ref.to_string(),
        inputs.context_dir.display().to_string(),
    ]
}

/// Build `image_ref` from `inputs` with `runtime` (`podman`/`docker`).
///
/// No timeout: a first build downloads the PyTorch wheel for the variant
/// and can take many minutes on a slow link (shipped timeouts are long by
/// design; the GUI shows the "Building image" stage meanwhile). A failure
/// returns the tail of the engine's stderr.
pub async fn container_build(
    runtime: &str,
    image_ref: &str,
    variant: &str,
    inputs: &BuildInputs,
) -> Result<(), String> {
    tracing::info!(
        "[bundled_modules] {} build {} (VARIANT={}) from {} ({})",
        runtime,
        image_ref,
        variant,
        inputs.context_dir.display(),
        inputs.dockerfile.display()
    );
    let output = Command::new(runtime)
        .silent()
        .args(build_argv(image_ref, variant, inputs))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("spawn {} build: {}", runtime, e))?;
    if output.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    let tail: String = {
        let chars: Vec<char> = stderr.chars().collect();
        let start = chars.len().saturating_sub(2000);
        chars[start..].iter().collect()
    };
    Err(format!(
        "{} build of {} failed (exit {}): {}",
        runtime,
        image_ref,
        output.status.code().unwrap_or(-1),
        tail.trim()
    ))
}

/// Make `install_dir` (`~/.vct/modules/<id>/`) mirror the module
/// directory's `vct-module.json` and `db/` migrations — what
/// `container_pull` modules get from `module_manifest_extract`, read here
/// straight from the checkout. The manifest is validated (parse + id)
/// before anything is written; `db/` is replaced as a whole so a migration
/// the checkout no longer ships does not linger.
pub fn materialize_install_dir(
    module_dir: &Path,
    install_dir: &Path,
    module_id: &str,
) -> Result<(), String> {
    let src_manifest = module_dir.join(MANIFEST_FILE);
    read_and_validate(&src_manifest, module_id)?;
    std::fs::create_dir_all(install_dir)
        .map_err(|e| format!("create {}: {}", install_dir.display(), e))?;

    let dest_manifest = install_dir.join(MANIFEST_FILE);
    let tmp = install_dir.join(format!("{}.new", MANIFEST_FILE));
    std::fs::copy(&src_manifest, &tmp)
        .map_err(|e| format!("copy {} -> {}: {}", src_manifest.display(), tmp.display(), e))?;
    std::fs::rename(&tmp, &dest_manifest)
        .map_err(|e| format!("rename {} -> {}: {}", tmp.display(), dest_manifest.display(), e))?;

    let src_db = module_dir.join("db");
    let dest_db = install_dir.join("db");
    if dest_db.exists() {
        std::fs::remove_dir_all(&dest_db)
            .map_err(|e| format!("remove stale {}: {}", dest_db.display(), e))?;
    }
    if src_db.is_dir() {
        std::fs::create_dir_all(&dest_db)
            .map_err(|e| format!("create {}: {}", dest_db.display(), e))?;
        let entries = std::fs::read_dir(&src_db)
            .map_err(|e| format!("read {}: {}", src_db.display(), e))?;
        for entry in entries {
            let entry = entry.map_err(|e| format!("read {}: {}", src_db.display(), e))?;
            let path = entry.path();
            if path.is_file() {
                let name = entry.file_name();
                std::fs::copy(&path, dest_db.join(&name))
                    .map_err(|e| format!("copy {}: {}", path.display(), e))?;
            }
        }
    }
    Ok(())
}

/// The install/upgrade step for a `container_build` module, shared by
/// `installer_engine::run_install_inner` and `refetch_artifact`: build the
/// variant image from the checkout, then materialise `install_dir`.
/// Returns the image tag that was built (the `container_pull` paths return
/// the pulled tag the same way).
pub async fn build_and_materialize(
    manifest: &ModuleManifest,
    install_dir: &Path,
    gpu_mode: GpuMode,
) -> Result<String, String> {
    let entry = bundled_entry(&manifest.id).ok_or_else(|| {
        format!("module {}: container_build is only for modules bundled in the checkout", manifest.id)
    })?;
    let root = checkout_root()?;
    let inputs = build_inputs(&root, manifest)?;
    // The SAME image reference every start path runs (one resolver: base tag
    // from version/ref + the GPU-variant suffix), so the built tag and the
    // started tag cannot disagree.
    let image_ref = vct_launcher_core::services::container_runtime::resolve_image_ref(
        "{install.container.image}:{install.container.tag}",
        manifest,
        Some(gpu_mode),
    )?;
    let tag = image_ref
        .rsplit_once(':')
        .map(|(_, t)| t.to_string())
        .ok_or_else(|| format!("module {}: image reference without a tag: {}", manifest.id, image_ref))?;
    let runtime = crate::installer_engine::detect_container_runtime().await?;
    container_build(&runtime, &image_ref, variant_build_arg(gpu_mode), &inputs).await?;
    materialize_install_dir(&root.join(entry.dir), install_dir, &manifest.id)?;
    Ok(tag)
}

#[cfg(test)]
mod tests {
    use super::*;

    const MANIFEST: &str = r#"{
        "manifest_version": 1,
        "id": "vct-rl-reranker",
        "name": "RL Reranker",
        "version": "0.3.0",
        "description": "x",
        "category": "core",
        "compatibility": { "hosts": ["base"] },
        "license": { "required": false },
        "install": {
            "method": "container_build",
            "container": {
                "image": "localhost/vco-rl-reranker",
                "tag_from_version": true,
                "build_context": ".",
                "dockerfile": "claude_mcp_servers/rl_reranker/Dockerfile"
            },
            "scope": "global"
        },
        "runtime": { "type": "service" },
        "db": { "migrations_dir": "db/", "namespace": "rl" }
    }"#;

    fn checkout_with(manifest: &str) -> tempfile::TempDir {
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join("claude_mcp_servers/rl_reranker");
        std::fs::create_dir_all(dir.join("db")).unwrap();
        std::fs::write(dir.join("vct-module.json"), manifest).unwrap();
        std::fs::write(dir.join("Dockerfile"), "FROM scratch\n").unwrap();
        std::fs::write(dir.join("db/0001_a.sql"), "SELECT 1;\n").unwrap();
        root
    }

    #[test]
    fn registry_names_the_rl_reranker_only() {
        assert!(is_bundled("vct-rl-reranker"));
        assert!(!is_bundled("vct-mao"));
        assert!(read_bundled_manifest("vct-mao").is_none());
    }

    #[test]
    fn reads_and_validates_the_checkout_manifest() {
        let root = checkout_with(MANIFEST);
        let (m, path) = read_bundled_manifest_in(root.path(), "vct-rl-reranker")
            .expect("bundled")
            .expect("valid");
        assert_eq!(m.version, "0.3.0");
        assert_eq!(m.install.method, InstallMethod::ContainerBuild);
        assert!(path.ends_with("claude_mcp_servers/rl_reranker/vct-module.json"));
        assert_eq!(list_bundled_manifests_in(root.path()).len(), 1);
    }

    #[test]
    fn a_manifest_that_is_not_container_build_is_refused() {
        let root = checkout_with(&MANIFEST.replace("container_build", "container_pull"));
        let err = read_bundled_manifest_in(root.path(), "vct-rl-reranker")
            .unwrap()
            .unwrap_err();
        assert!(err.contains("container_build"), "{err}");
        assert!(list_bundled_manifests_in(root.path()).is_empty());
    }

    #[test]
    fn a_manifest_for_another_id_is_refused() {
        let root = checkout_with(&MANIFEST.replace("\"vct-rl-reranker\"", "\"other\""));
        let err = read_bundled_manifest_in(root.path(), "vct-rl-reranker")
            .unwrap()
            .unwrap_err();
        assert!(err.contains("declares id"), "{err}");
    }

    #[test]
    fn build_inputs_resolve_inside_the_checkout_only() {
        let root = checkout_with(MANIFEST);
        let m = ModuleManifest::from_json(MANIFEST).unwrap();
        let inputs = build_inputs(root.path(), &m).unwrap();
        assert_eq!(inputs.context_dir, root.path().join("."));
        assert_eq!(
            inputs.dockerfile,
            root.path().join("claude_mcp_servers/rl_reranker/Dockerfile")
        );

        for bad in ["../outside", "/abs/path", "claude_mcp_servers/../../x"] {
            let m = ModuleManifest::from_json(&MANIFEST.replace(
                "\"build_context\": \".\"",
                &format!("\"build_context\": \"{bad}\""),
            ))
            .unwrap();
            let err = build_inputs(root.path(), &m).unwrap_err();
            assert!(err.contains("inside the checkout"), "{bad}: {err}");
            let m = ModuleManifest::from_json(
                &MANIFEST.replace("claude_mcp_servers/rl_reranker/Dockerfile", bad),
            )
            .unwrap();
            let err = build_inputs(root.path(), &m).unwrap_err();
            assert!(err.contains("dockerfile"), "{bad}: {err}");
        }

        // Without a `dockerfile`, the context's own Dockerfile is used.
        let m = ModuleManifest::from_json(&MANIFEST.replace(
            ",\n                \"dockerfile\": \"claude_mcp_servers/rl_reranker/Dockerfile\"",
            "",
        ))
        .unwrap();
        assert!(m.install.container.as_ref().unwrap().dockerfile.is_none());
        assert!(build_inputs(root.path(), &m).unwrap_err().contains("Dockerfile not found"));
    }

    #[test]
    fn variant_build_arg_matches_the_tag_split() {
        assert_eq!(variant_build_arg(GpuMode::Cuda), "cuda");
        assert_eq!(variant_build_arg(GpuMode::Rocm), "rocm");
        assert_eq!(variant_build_arg(GpuMode::Cpu), "cpu");
        assert_eq!(variant_build_arg(GpuMode::Metal), "cpu");
    }

    #[test]
    fn build_argv_shape() {
        let inputs = BuildInputs {
            context_dir: PathBuf::from("/ctx"),
            dockerfile: PathBuf::from("/ctx/claude_mcp_servers/rl_reranker/Dockerfile"),
        };
        let argv = build_argv("localhost/vco-rl-reranker:0.3.0-cuda", "cuda", &inputs);
        assert_eq!(
            argv,
            vec![
                "build",
                "--build-arg",
                "VARIANT=cuda",
                "--file",
                "/ctx/claude_mcp_servers/rl_reranker/Dockerfile",
                "--tag",
                "localhost/vco-rl-reranker:0.3.0-cuda",
                "/ctx"
            ]
        );
    }

    #[test]
    fn materialize_copies_manifest_and_replaces_db() {
        let root = checkout_with(MANIFEST);
        let ctx = root.path().join("claude_mcp_servers/rl_reranker");
        let install = tempfile::tempdir().unwrap();
        let install_dir = install.path().join("vct-rl-reranker");
        std::fs::create_dir_all(install_dir.join("db")).unwrap();
        std::fs::write(install_dir.join("db/0099_stale.sql"), "x").unwrap();

        materialize_install_dir(&ctx, &install_dir, "vct-rl-reranker").unwrap();
        assert_eq!(
            std::fs::read_to_string(install_dir.join("vct-module.json")).unwrap(),
            MANIFEST
        );
        assert!(install_dir.join("db/0001_a.sql").is_file());
        assert!(!install_dir.join("db/0099_stale.sql").exists(), "stale migration must go");
    }

    #[test]
    fn materialize_refuses_a_mismatched_manifest_without_writing() {
        let root = checkout_with(&MANIFEST.replace("\"vct-rl-reranker\"", "\"other\""));
        let ctx = root.path().join("claude_mcp_servers/rl_reranker");
        let install = tempfile::tempdir().unwrap();
        let install_dir = install.path().join("vct-rl-reranker");
        assert!(materialize_install_dir(&ctx, &install_dir, "vct-rl-reranker").is_err());
        assert!(!install_dir.join("vct-module.json").exists());
    }

    /// The SHIPPED manifest in this checkout reads cleanly through the same
    /// path the launcher uses, and its build context holds a Dockerfile.
    #[test]
    fn the_shipped_rl_reranker_manifest_is_a_valid_bundled_manifest() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let (m, _) = read_bundled_manifest_in(&root, "vct-rl-reranker")
            .expect("bundled")
            .expect("the shipped manifest must validate");
        assert!(!m.license.required, "the RL reranker needs no licence");
        assert!(m.install.scope.is_global());
        let inputs = build_inputs(&root, &m).expect("shipped build inputs");
        // The root `.dockerignore` is what keeps a checkout-root context small.
        assert!(inputs.context_dir.join(".dockerignore").is_file());
        assert!(
            m.validate_for_container_start()
                .iter()
                .all(|w| w.severity != crate::manifest::WarningSeverity::Error),
            "shipped manifest has container-start errors"
        );
    }
}

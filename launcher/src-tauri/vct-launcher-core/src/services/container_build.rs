// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! The ONE home of the local image build for `container_build` modules
//! (v0.2.106) and of the start-path "is the image there?" check.
//!
//! A bundled module's image is built LOCALLY from a directory of the
//! orchestrator checkout (never pulled from a registry, no licence). The
//! build itself ([`container_build`]) is shared by every caller — the
//! installer (`installer_engine` via `commands::bundled_modules::
//! build_and_materialize`) and, since v0.2.106, the module START paths:
//! an image can vanish (a prune) or never exist for the variant a start
//! resolves (a GPU added/removed changes the `-cuda`/`-rocm`/`-cpu`
//! suffix), and pre-v0.2.106 such a start just failed with a `podman run`
//! error nobody could act on. [`ensure_bundled_image`] probes the exact
//! image reference the start would run and rebuilds it through the SAME
//! build function when it is missing — one implementation, two callers,
//! no second copy.
//!
//! This lives in `vct-launcher-core` (not the launcher crate) because the
//! hub's module supervisor starts global containers and needs the same
//! ensure step the launcher's own start path runs.

use std::path::{Component, Path, PathBuf};
use std::process::Stdio;

use tokio::process::Command;

use crate::manifest::{InstallMethod, ModuleManifest};
use crate::process::CommandExt as _;
use crate::services::container_runtime::resolve_image_ref;
use crate::services::gpu_mode::GpuMode;

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
        "[container_build] {} build {} (VARIANT={}) from {} ({})",
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

/// True iff `image_ref` exists in the local image store. Never raises: an
/// engine that cannot even answer `image inspect` counts as "not present"
/// (the caller's build/run will surface the real engine error).
pub async fn image_present(runtime: &str, image_ref: &str) -> bool {
    let status = Command::new(runtime)
        .silent()
        .args(["image", "inspect", image_ref])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await;
    matches!(status, Ok(s) if s.success())
}

/// The outcome of [`ensure_bundled_image`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EnsureImageOutcome {
    /// The image the start would run is already in the local store.
    Present,
    /// It was missing (pruned, or built for another GPU variant) and was
    /// rebuilt from the checkout through the same build the installer uses.
    Rebuilt,
}

/// The start-path guard for `container_build` modules (v0.2.106): probe the
/// EXACT image reference the start resolves (`{install.container.image}:
/// {version}-{variant}`) and rebuild it from the checkout when it is absent.
/// A no-op for every other install method ([`EnsureImageOutcome`] is only
/// meaningful together with `Ok`).
///
/// The checkout root is resolved the way the installer's build does
/// (`install_root::resolve_current_exe_without_db` — identity-checked walk
/// from this binary), so a hub running from `launcher/dist/` and the
/// launcher resolve the same checkout.
///
/// Callers surface the outcome themselves (log line + audit row); an `Err`
/// propagates to the start's existing failure surface (the module tile's
/// `last_error`, the HTTP error envelope).
pub async fn ensure_bundled_image(
    manifest: &ModuleManifest,
    gpu_mode: Option<GpuMode>,
    runtime: &str,
) -> Result<(String, EnsureImageOutcome), String> {
    if manifest.install.method != InstallMethod::ContainerBuild {
        return Ok((String::new(), EnsureImageOutcome::Present));
    }
    let root = crate::services::install_root::resolve_current_exe_without_db()
        .map_err(|e| format!("module {}: cannot locate the orchestrator checkout: {}", manifest.id, e))?
        .path;
    ensure_bundled_image_in(&root, manifest, gpu_mode, runtime).await
}

/// [`ensure_bundled_image`] against an explicit checkout root (what the
/// resolver supplies in production; tests pass a tempdir).
pub async fn ensure_bundled_image_in(
    root: &Path,
    manifest: &ModuleManifest,
    gpu_mode: Option<GpuMode>,
    runtime: &str,
) -> Result<(String, EnsureImageOutcome), String> {
    if manifest.install.method != InstallMethod::ContainerBuild {
        return Ok((String::new(), EnsureImageOutcome::Present));
    }
    let inputs = build_inputs(root, manifest)?;
    // The SAME image reference every start path runs (one resolver: base tag
    // from version/ref + the GPU-variant suffix), so the built tag and the
    // started tag cannot disagree.
    let image_ref = resolve_image_ref(
        "{install.container.image}:{install.container.tag}",
        manifest,
        gpu_mode,
    )?;
    if image_present(runtime, &image_ref).await {
        return Ok((image_ref, EnsureImageOutcome::Present));
    }
    tracing::warn!(
        module_id = %manifest.id,
        image = %image_ref,
        "[container_build] image missing for start (pruned, or built for another \
         GPU variant); rebuilding from {}",
        root.display()
    );
    let variant = match gpu_mode {
        Some(g) => variant_build_arg(g),
        None => "cpu",
    };
    container_build(runtime, &image_ref, variant, &inputs).await?;
    Ok((image_ref, EnsureImageOutcome::Rebuilt))
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
        "runtime": {
            "type": "service",
            "gpu_image_variants": {
                "cuda": "{version}-cuda",
                "rocm": "{version}-rocm",
                "cpu": "{version}-cpu"
            }
        }
    }"#;

    fn manifest() -> ModuleManifest {
        serde_json::from_str(MANIFEST).expect("fixture manifest parses")
    }

    fn checkout_with() -> tempfile::TempDir {
        let root = tempfile::tempdir().unwrap();
        let df = root.path().join("claude_mcp_servers/rl_reranker/Dockerfile");
        std::fs::create_dir_all(df.parent().unwrap()).unwrap();
        std::fs::write(&df, "FROM scratch\n").unwrap();
        root
    }

    /// A fake container engine: `image inspect <ref>` answers from a
    /// state file (a line "present" = exit 0); `build ...` appends its
    /// `--tag <ref>` to the same file. Lets the ensure step run its real
    /// argv against a real subprocess with no engine installed.
    fn fake_runtime(dir: &Path, present: bool) -> PathBuf {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let state = dir.join("state.txt");
            std::fs::write(&state, if present { "present\n" } else { "" }).unwrap();
            let script = dir.join("fake-engine");
            let body = format!(
                "#!/bin/sh\nSTATE={}\ncase \"$1 $2\" in\n  'image inspect') grep -qx present \
                 \"$STATE\" && exit 0 || exit 1;;\n  build*) echo \"$6 $7\" >> \"$STATE\"; exit 0;;\n\
                 *) exit 0;;\nesac\n",
                state.display()
            );
            std::fs::write(&script, body).unwrap();
            let mut perms = std::fs::metadata(&script).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&script, perms).unwrap();
            script
        }
        #[cfg(not(unix))]
        {
            let _ = (dir, present);
            unimplemented!("fake engine test is unix-only; covered by CI's linux runner")
        }
    }

    #[tokio::test]
    async fn a_present_image_is_not_rebuilt() {
        let root = checkout_with();
        let bin = fake_runtime(root.path(), true);
        let (image, outcome) =
            ensure_bundled_image_in(root.path(), &manifest(), Some(GpuMode::Cpu), bin.to_str().unwrap())
                .await
                .unwrap();
        assert_eq!(outcome, EnsureImageOutcome::Present);
        assert_eq!(image, "localhost/vco-rl-reranker:0.3.0-cpu");
        // No build ran: the state file still holds only "present".
        assert_eq!(
            std::fs::read_to_string(root.path().join("state.txt")).unwrap(),
            "present\n"
        );
    }

    #[tokio::test]
    async fn a_missing_image_is_rebuilt_with_the_installer_build_argv() {
        let root = checkout_with();
        let bin = fake_runtime(root.path(), false);
        let (image, outcome) =
            ensure_bundled_image_in(root.path(), &manifest(), Some(GpuMode::Cuda), bin.to_str().unwrap())
                .await
                .unwrap();
        assert_eq!(outcome, EnsureImageOutcome::Rebuilt);
        // The exact variant tag this start resolves is the one built.
        assert_eq!(image, "localhost/vco-rl-reranker:0.3.0-cuda");
        // The build the ensure ran IS the installer's build (same argv
        // shape): the recorded `--tag <ref>` pair names the variant ref.
        let state = std::fs::read_to_string(root.path().join("state.txt")).unwrap();
        assert_eq!(state.trim(), "--tag localhost/vco-rl-reranker:0.3.0-cuda");
    }

    #[tokio::test]
    async fn a_build_failure_fails_the_ensure_loudly() {
        let root = checkout_with();
        // An "engine" whose build always fails.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let script = root.path().join("failing-engine");
            std::fs::write(&script, "#!/bin/sh\ncase \"$1\" in build) echo boom >&2; exit 7;; \
                                    image) exit 1;; *) exit 0;; esac\n")
                .unwrap();
            let mut perms = std::fs::metadata(&script).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&script, perms).unwrap();
            let err = ensure_bundled_image_in(
                root.path(), &manifest(), Some(GpuMode::Cpu), script.to_str().unwrap(),
            )
            .await
            .unwrap_err();
            assert!(err.contains("failed") && err.contains("boom"), "{err}");
        }
    }

    #[tokio::test]
    async fn non_build_modules_are_a_no_op() {
        let mut m = manifest();
        m.install.method = InstallMethod::ContainerPull;
        // A runtime that does not exist: the no-op path must never spawn it.
        let (image, outcome) =
            ensure_bundled_image_in(Path::new("/nonexistent"), &m, None, "no-such-engine")
                .await
                .unwrap();
        assert_eq!(outcome, EnsureImageOutcome::Present);
        assert_eq!(image, "");
    }

    #[tokio::test]
    async fn image_present_is_false_when_the_engine_cannot_answer() {
        // A binary that does not exist cannot report success: the probe
        // counts as "not present" instead of raising (the caller's build
        // or run surfaces the real engine error).
        assert!(!image_present("no-such-engine", "x:y").await);
    }
}

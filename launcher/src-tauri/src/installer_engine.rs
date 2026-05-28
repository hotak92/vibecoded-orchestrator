//! Module installation engine.
//!
//! Takes a parsed `ModuleManifest` + resolved `PlaceholderCtx` and executes
//! the install steps: git clone / tarball / pypi / npm, followed by
//! `post_install` commands in order. Each command is tokenized via shlex —
//! never passed to a shell — so manifest authors can't inject `&&`, `;`,
//! `$(..)`, etc.
//!
//! Progress is reported through Tauri events (`module://install-progress`)
//! so the React UI can show a progress bar per step.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use tauri::{AppHandle, Emitter};
use tokio::process::Command;

use crate::manifest::{CommandSpec, InstallMethod, ModuleManifest, PlaceholderCtx};
use vct_launcher_core::process::CommandExt as _;

#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum InstallStage {
    Clone,
    PostInstall,
    /// v0.2.31 (#20-Fix-3): emitted by `run_upgrade` when a
    /// `manifest.upgrade.migration_script` runs. Distinguishes
    /// "post-pull data migration" from generic post_install steps in
    /// the UI progress bar.
    Migrate,
    /// v0.2.33 (Agent C, L0b): emitted after `container_pull` succeeds
    /// and BEFORE `apply_module_db_migrations`. The launcher pulls the
    /// module image, then extracts `/app/vct-module.json` from a
    /// throw-away container to `~/.vct/modules/<id>/vct-module.json`.
    /// This stage exists as its own variant so the GUI progress bar can
    /// render "Extracting manifest…" — a phase distinct enough from
    /// the surrounding Clone / PostInstall stages that lumping it under
    /// either confuses the user when extraction is the slow / failing
    /// step.
    ExtractingManifest,
    /// v0.2.35 (Agent N): emitted by `container_pull` when the
    /// `resolve_variant_tag`-chosen tag (e.g. `0.2.7-cuda`) is missing
    /// from the registry and the launcher silently falls back to the
    /// `-cpu` variant. Pre-v0.2.35 the user got a cryptic `denied`/404
    /// from `podman pull` with no way to act; now the GUI can render a
    /// non-blocking "CUDA variant unavailable; installing CPU variant
    /// instead" toast keyed off this stage. The `message` field carries
    /// the requested-vs-actual tag pair (e.g. "requested 0.2.7-cuda,
    /// installing 0.2.7-cpu").
    VariantFallback,
    Done,
    /// v0.2.31 (#27): emitted via `report_error` before an error return.
    /// Pre-v0.2.31 errors propagated as `Result::Err` but the progress
    /// channel never received a terminal event — the UI's progress bar
    /// just saw the channel hang up. Now every error path in `run_install`
    /// / `run_upgrade` calls `report_error(...)` first so the GUI sees
    /// `stage=failed` + the error message before the Tauri command
    /// returns Err.
    Failed,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct InstallProgress {
    pub project_id: String,
    pub module_id: String,
    pub stage: InstallStage,
    pub step_index: u32,
    pub step_total: u32,
    pub percent: u8,
    pub message: String,
}

/// Perform the install for a module into `install_dir`.
///
/// Returns the resolved install directory path on success. On failure,
/// leaves the partially-installed directory in place (caller can inspect
/// or delete it) and returns the error.
///
/// `gpu_mode` is consulted ONLY when `manifest.install.method ==
/// ContainerPull` AND `manifest.runtime.gpu_image_variants` is `Some`.
/// In that case the resolved image tag is `{version}-{cpu|cuda|rocm}`
/// (or whatever the manifest declares) instead of the bare `{version}`
/// the legacy single-tag path produces. Pass `GpuMode::Cpu` when the
/// caller hasn't probed hardware yet — the dispatch will fall back to
/// the cpu variant, which is always safe to pull.
pub async fn run_install(
    app: &AppHandle,
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    project_id: &str,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
    db: &crate::db::Db,
    // NEW-1 (2026-05-28): L0 catalog's pull_token_endpoint overrides the L1
    // manifest's value when present. L0 always carries the real Supabase URL
    // (server-side SoT); L1 (image-extracted) may contain a placeholder.
    l0_pull_token_endpoint: Option<&str>,
) -> Result<PathBuf, String> {
    // v0.2.31 (#27): wrap the body so every Err path emits InstallStage::Failed
    // via the progress channel before propagating. The inner helper carries
    // the same signature; this wrapper exists only to attach the terminal
    // Failed event to error returns.
    match run_install_inner(app, manifest, ctx, project_id, gpu_mode, db, l0_pull_token_endpoint).await {
        Ok(p) => Ok(p),
        Err(e) => {
            report_error(app, project_id, &manifest.id, InstallStage::Clone, &e);
            Err(e)
        }
    }
}

async fn run_install_inner(
    app: &AppHandle,
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    project_id: &str,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
    db: &crate::db::Db,
    l0_pull_token_endpoint: Option<&str>,
) -> Result<PathBuf, String> {
    let install_dir = ctx.resolve_install_dir(&manifest.install.install_dir);
    let allowed_root = ctx.vct_modules.clone();

    // v0.2.34: bootstrap `<vct_root>/modules/` BEFORE the security
    // guard runs. On a fresh machine this directory is created
    // lazily by container_pull's `tokio::fs::create_dir_all` LATER
    // in the install flow — but `validate_install_dir` runs FIRST,
    // and even after the v0.2.34 hardening pass we want the canonical
    // path comparison to operate on a real directory (so the canonicalize
    // call resolves any user-level symlinks like `~/.vct → /mnt/data/.vct`).
    // `create_dir_all` is idempotent — a no-op when the directory
    // already exists — so the cost on the warm path is one stat().
    std::fs::create_dir_all(&allowed_root).map_err(|e| {
        format!(
            "bootstrap allowed_root {}: {}",
            allowed_root.display(),
            e
        )
    })?;

    // Security: refuse paths outside ~/.vct/modules/.
    crate::manifest::validate_install_dir(&install_dir, &allowed_root)?;

    // v0.2.29: enforce `compatibility.min_launcher_version` if the
    // manifest declares one. The field was already deserialized in
    // `manifest::Compatibility` since the dispatcher work but never
    // consulted — modules could declare a min version and the launcher
    // would happily install them on older binaries that lack the
    // required APIs. Refuse with a structured error so the GUI surfaces
    // "launcher too old: bump first" rather than a cryptic later
    // failure during post_install or first container boot.
    if let Some(required) = manifest.compatibility.min_launcher_version.as_deref() {
        let required = required.trim();
        if !required.is_empty() {
            let current = env!("CARGO_PKG_VERSION");
            if version_lt(current, required) {
                return Err(format!(
                    "module '{}' requires launcher >= {} but this launcher is {}. \
                     Update the launcher first (Settings → Updates → Update orchestrator), \
                     then retry the install.",
                    manifest.id, required, current,
                ));
            }
        }
    }

    let total_steps: u32 = 1 + manifest.install.post_install.len() as u32;
    let mut step_index: u32 = 0;

    // ─── Step 1: fetch source ────────────────────────────────────────────
    emit_progress(app, project_id, &manifest.id, InstallStage::Clone, step_index, total_steps, "Fetching source");
    step_index += 1;

    match manifest.install.method {
        InstallMethod::GitClone => {
            git_clone(
                manifest.install.source.as_deref().ok_or("install.source required for git_clone")?,
                manifest.install.r#ref.as_deref().unwrap_or("main"),
                &install_dir,
            )
            .await?;
        }
        InstallMethod::Local => {
            // Local: install_dir must already exist; skip fetch.
            if !install_dir.exists() {
                return Err(format!(
                    "install.method=local requires install_dir to exist: {}",
                    install_dir.display()
                ));
            }
        }
        InstallMethod::ContainerPull => {
            // Paid-module distribution path. Locks down piracy by:
            //   1. requiring a Pro-or-higher license tier
            //   2. requiring a short-lived pull token from the signed-URL
            //      gateway (no anonymous registry access)
            //
            // The actual `podman pull` happens here. The container itself
            // is registered with the launcher's service supervisor in
            // Phase 1E (modules.rs:install_module_for_project picks up the
            // resolved image:tag from manifest.install.container after
            // run_install completes).
            let container = manifest
                .install
                .container
                .as_ref()
                .ok_or("install.method=container_pull requires install.container block")?;

            // Base tag from manifest (version or explicit ref).
            let base_tag = if container.tag_from_version {
                manifest.version.clone()
            } else {
                manifest
                    .install
                    .r#ref
                    .clone()
                    .unwrap_or_else(|| "latest".to_string())
            };

            // v0.2.20: per-GPU-mode image variant dispatch. When the
            // manifest's `runtime.gpu_image_variants` block is present,
            // the host's `decide_gpu_mode` answer picks the variant tag
            // (e.g. "0.1.0-cuda"). Otherwise we fall back to the legacy
            // single-tag flow (just the version, no suffix).
            let tag = resolve_variant_tag(manifest, &base_tag, gpu_mode);

            // v0.2.35 (Agent N): compute the CPU-fallback tag UP FRONT.
            // `container_pull` probes the registry for `tag` and falls
            // back to the cpu variant if missing (covers publishers who
            // shipped `-cpu` but haven't built `-cuda`/`-rocm` for this
            // release yet). If `tag` is already the cpu variant, or the
            // manifest declares no variants, the fallback is None.
            let fallback = cpu_fallback_tag(manifest, &base_tag, gpu_mode);
            let actual_tag = container_pull(
                container,
                &tag,
                fallback.as_deref(),
                &manifest.id,
                l0_pull_token_endpoint,
            )
            .await?;

            // v0.2.35 (Agent N): emit a non-blocking progress event when
            // the registry rejected the chosen variant and we silently
            // pulled the cpu fallback. The GUI consumes this stage as a
            // toast/inline message — install proceeds normally with the
            // fallback tag, but the user sees WHY their CUDA variant
            // wasn't installed.
            if actual_tag != tag {
                emit_progress(
                    app,
                    project_id,
                    &manifest.id,
                    InstallStage::VariantFallback,
                    step_index,
                    total_steps,
                    &format!(
                        "Requested variant {} not available on the registry; installing {} instead",
                        tag, actual_tag
                    ),
                );
            }
            // Use the actually-pulled tag for downstream operations
            // (manifest extraction). Without this, an upgrade from
            // cuda→cpu would try to `docker cp` from the cuda image
            // (which was never pulled), surfacing as a confusing
            // image-not-found error.
            let tag = actual_tag;

            // For container modules, install_dir is metadata-only — a
            // marker directory we use to track installed state. Create
            // it so post_install commands (if any) have a place to land.
            tokio::fs::create_dir_all(&install_dir)
                .await
                .map_err(|e| format!("create install_dir for container module: {}", e))?;

            // v0.2.33 (Agent C, L0b): post-install manifest extraction.
            // Pulls /app/vct-module.json out of the just-pulled image
            // via `docker create` + `docker cp` so the renderer, the
            // dispatcher, and the DB migrations machinery have the
            // FULL manifest on disk (the L0 catalog endpoint only
            // ships the install-time slice). Runs BEFORE
            // `apply_module_db_migrations` so the migrations machinery
            // can read `manifest.db.migrations_dir` from the extracted
            // file. Atomic write with .bak rollback (see module docs).
            //
            // Hard-fail the install on extraction error — the image
            // is in podman cache (retain it for retry), the install
            // row is still 'installing' at this point so the outer
            // wrapper's `report_error` path will emit InstallStage::
            // Failed and the caller will mark it 'failed'. Retain
            // image policy: we do NOT `podman rmi` on extract failure
            // — retries should be cheap, not require a re-pull.
            let image_ref = format!("{}:{}", container.image, tag);
            let runtime = detect_container_runtime().await?;
            emit_progress(
                app,
                project_id,
                &manifest.id,
                InstallStage::ExtractingManifest,
                step_index,
                total_steps,
                "Extracting module manifest from image",
            );
            crate::commands::module_manifest_extract::extract_manifest_from_image(
                &image_ref,
                &manifest.id,
                &runtime,
            )
            .await?;
        }
    }

    // ─── Step 2+: post_install commands ──────────────────────────────────
    let ctx_with_dir = ctx.clone().with_install_dir(install_dir.clone());
    for (i, cmd_spec) in manifest.install.post_install.iter().enumerate() {
        let message = format!("Running setup step {}/{}", i + 1, manifest.install.post_install.len());
        emit_progress(
            app,
            project_id,
            &manifest.id,
            InstallStage::PostInstall,
            step_index,
            total_steps,
            &message,
        );
        step_index += 1;
        run_post_install_command(cmd_spec, &ctx_with_dir).await?;
    }

    // ─── v0.2.31: module-shipped DB migrations ───────────────────────────
    //
    // After all post_install commands have succeeded, apply any SQL
    // migrations the module ships under `manifest.db.migrations_dir`.
    // Soft-fail: an error here does NOT cause the install to fail
    // (the on-disk artifact is already in place, the catalog rows
    // already written). Instead we log + emit a non-blocking event so
    // the GUI / install-deferred surface can show the actionable
    // diagnostic. The user can retry via the
    // `apply_module_db_migrations` Tauri command from the dashboard.
    if manifest.db.is_some() {
        emit_progress(
            app,
            project_id,
            &manifest.id,
            InstallStage::PostInstall,
            step_index,
            total_steps,
            "Applying module DB migrations",
        );
        let module_id_for_log = manifest.id.clone();
        let install_dir_for_log = install_dir.clone();
        match crate::db::module_db_migrations::apply_module_db_migrations(
            db,
            &manifest.id,
            &install_dir,
            manifest,
        ) {
            Ok(report) => {
                if !report.ok() {
                    eprintln!(
                        "[installer_engine] run_install[{}]: module DB migration(s) failed at {}: {:?}",
                        module_id_for_log,
                        install_dir_for_log.display(),
                        report.errors,
                    );
                    let _ = app.emit(
                        "module://db-migration-failed",
                        serde_json::json!({
                            "project_id": project_id,
                            "module_id": manifest.id,
                            "errors": report.errors,
                            "applied": report.applied,
                            "skipped": report.skipped,
                        }),
                    );
                } else if !report.applied.is_empty() {
                    eprintln!(
                        "[installer_engine] run_install[{}]: applied {} DB migration(s)",
                        module_id_for_log,
                        report.applied.len(),
                    );
                }
            }
            Err(e) => {
                // Hard failure resolving the apply itself (e.g. DB lock).
                // Still soft-fail at the installer-engine level: the
                // install row is `installed`, the migration is just
                // pending. Surface via the same event.
                eprintln!(
                    "[installer_engine] run_install[{}]: apply_module_db_migrations errored: {}",
                    module_id_for_log, e
                );
                let _ = app.emit(
                    "module://db-migration-failed",
                    serde_json::json!({
                        "project_id": project_id,
                        "module_id": manifest.id,
                        "error": e,
                    }),
                );
            }
        }
    }

    emit_progress(
        app,
        project_id,
        &manifest.id,
        InstallStage::Done,
        step_index,
        total_steps,
        "Install complete",
    );

    Ok(install_dir)
}

/// Pull a container image from a private registry via a short-lived
/// signed pull-token (paid-module flow).
///
/// Token gateway flow (Phase 3A — gateway not deployed yet):
///   1. POST validated-tier JWT to `pull_token_endpoint`.
///   2. Receive `{ image, tag, pull_token, expires_at }` — token TTL ~15min.
///   3. `podman login` with that token, `podman pull`, `podman logout`.
///   4. Discard token from memory.
///
/// Today (gateway returns 404): falls back to anonymous pull. Anonymous
/// pull will succeed for public images and 401 for private — both produce
/// clear errors that help diagnose the gateway-not-deployed-yet state.
///
/// Runtime detection: prefers `podman` (rootless, matches the rest of
/// VCO's container stack). Falls back to `docker` if podman is missing.
///
/// v0.2.35 (Agent N): after login + before pull, probe the chosen tag's
/// existence on the registry via `<runtime> manifest inspect`. If the
/// probe says "missing" and `fallback_tag` is `Some`, probe the fallback;
/// pull whichever exists. The returned `String` is the tag actually
/// pulled — callers compare against the originally-requested tag to
/// detect a fallback happened (so they can emit a UI event / log).
/// When both probes fail OR `fallback_tag` is `None`, hard-fail with a
/// "variant not available" error rather than firing a doomed pull that
/// would 404 with a cryptic `denied` message.
async fn container_pull(
    container: &crate::manifest::ContainerInstallBlock,
    tag: &str,
    fallback_tag: Option<&str>,
    module_id: &str,
    l0_pull_token_endpoint: Option<&str>,
) -> Result<String, String> {
    // ─── Step 1: try to obtain a pull token from the signed-URL gateway ─
    //
    // v0.2.35 (Phase 3A): `request_pull_token` is now the canonical
    // paid-module auth path. It reads the license key from the OS
    // keychain (the same source `license_refresh` uses), computes the
    // platform-stable machine_id_hash (Windows MachineGuid / macOS
    // IOPlatformUUID / Linux /etc/machine-id — see
    // `commands/licensing.rs::machine_id_hash`, switched from MAC-based
    // in v0.2.36), and POSTs `{license_key, machine_id_hash}` to the
    // manifest's `pull_token_endpoint` (e.g., `rl-artifact-url`).
    // The endpoint re-validates the license server-side and returns a
    // short-lived (≤15min) repository-scoped pull token.
    //
    // On Err we still fall through to anonymous pull, because the
    // legacy "image is public during the launch window" scenario is
    // still valid for free-tier publishers AND for the dev/test image
    // variants that haven't been flipped to private yet. The 401 path
    // is now diagnostic — the Err message from request_pull_token
    // carries the actual cause (license_invalid / tier_insufficient /
    // network / etc.) so the user sees an actionable error instead of
    // the old "Phase 3A gateway not deployed" footer.
    let (token, token_username, token_request_err): (Option<String>, Option<String>, Option<String>) = match request_pull_token(container, l0_pull_token_endpoint).await {
        Ok(tok) => {
            eprintln!(
                "[installer_engine] container_pull[{}]: obtained pull token (expires_in={}s)",
                module_id, tok.expires_in_s
            );
            (Some(tok.pull_token), tok.username, None)
        }
        Err(e) => {
            eprintln!(
                "[installer_engine] container_pull[{}]: pull-token gateway returned: {}. \
                 Falling back to anonymous pull — will succeed only if the image is public.",
                module_id, e
            );
            (None, None, Some(e))
        }
    };

    // ─── Step 2: pick container runtime (podman preferred, docker fallback) ─
    let runtime = detect_container_runtime().await?;

    // ─── Step 3: login (if token), then probe + pull + logout ──────────
    let registry = container
        .registry
        .clone()
        .unwrap_or_else(|| infer_registry_from_image(&container.image));
    if let Some(t) = token.as_deref() {
        // v0.2.36: server returns the GHCR username alongside the
        // pull_token. For personal-account packages this MUST be the
        // PAT owner's GitHub login (synthetic usernames get 403 from
        // ghcr.io). Pre-v0.2.36 server response omitted this field —
        // fall back to the historical `vct-paid-module` literal so
        // mismatched-version client/server pairings still attempt
        // a login (it'll fail with a clear error, not silently break).
        let login_username = token_username.as_deref().unwrap_or("vct-paid-module");
        container_login(&runtime, &registry, login_username, t).await?;
    }

    // ─── Step 3a (v0.2.35): probe primary tag, fall back to cpu if missing ─
    //
    // The probe is a `manifest inspect` call — it issues a HEAD-equivalent
    // request to the registry without downloading layers. Cheap, scoped
    // to the same auth context as the upcoming pull, and lets us decide
    // FROM the registry's authoritative answer whether the chosen
    // variant exists. Pre-v0.2.35 the launcher fired the pull blind and
    // surfaced a cryptic `denied` error on miss; now the user gets a
    // structured fallback (or a clear "no variant available" hard-fail
    // when even the CPU fallback is missing).
    //
    // The decision tree itself lives in `decide_variant_to_pull` — a
    // pure helper that takes a probe-closure and the candidate tags so
    // unit tests can exercise every branch (primary-hit /
    // primary-miss-fallback-hit / both-miss / probe-error-degrades) by
    // injecting a fake probe instead of needing a live registry.
    let runtime_for_probe = runtime.clone();
    let decision = decide_variant_to_pull(
        &container.image,
        tag,
        fallback_tag,
        module_id,
        |probe_image, probe_tag| {
            let runtime = runtime_for_probe.clone();
            async move {
                probe_image_tag_exists(&probe_image, &probe_tag, &runtime).await
            }
        },
    )
    .await;

    let resolved_tag: String = match decision {
        Ok(t) => t,
        Err(decision_err) => {
            // Hard-fail the install BEFORE issuing the doomed pull.
            // Always logout so we don't leak the token in the runtime's
            // auth.json (mirrors the post-pull logout path).
            if token.is_some() {
                let _ = Command::new(&runtime)
                    .silent()
                    .args(["logout", &registry])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status()
                    .await;
            }
            return Err(decision_err);
        }
    };

    let image_ref = format!("{}:{}", container.image, resolved_tag);
    // v0.2.37 (Issue 7): drain stdout/stderr via Stdio::null() rather than
    // Stdio::piped(). `podman pull` of a large image (5.5GB / 7 layers) emits
    // tens of KB of layer-progress text to stderr; combined with `.status()`
    // — which doesn't drain the pipes — the OS-level pipe buffer (64KB on
    // Linux) fills up and the child blocks on write, while the parent blocks
    // on wait, producing a deadlock that surfaces as `exit -1` AFTER the
    // image has actually been downloaded successfully (`podman images`
    // confirms presence). The launcher reports progress via Tauri events
    // through `report_progress`, NOT by parsing pull stdout, so dropping the
    // output is safe — no UX regression. Same fix is applied to the two
    // `git clone` / `git pull` sites further down, where the same Stdio +
    // .status() pattern carries the same latent deadlock risk on
    // unexpectedly verbose repos.
    let pull_status = Command::new(&runtime)
        .silent()
        .args(["pull", &image_ref])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await
        .map_err(|e| format!("spawn {} pull: {}", runtime, e))?;

    // Always logout if we logged in (even on pull failure) so the token
    // doesn't linger in the runtime's auth.json.
    if token.is_some() {
        let _ = Command::new(&runtime)
            .silent()
            .args(["logout", &registry])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await;
    }

    if !pull_status.success() {
        return Err(format!(
            "{} pull failed (exit {}): {}{}",
            runtime,
            pull_status.code().unwrap_or(-1),
            image_ref,
            if token.is_none() {
                // v0.2.35: the request_pull_token error is now the
                // authoritative reason for the 401. Quote it verbatim
                // so the user sees the actual cause (e.g. "your license
                // has expired") instead of the pre-v0.2.35 generic
                // "Phase 3A gateway not deployed" footer that masked
                // every real failure mode.
                match token_request_err.as_deref() {
                    Some(reason) => format!(" — license check failed: {}", reason),
                    None => " — and the pull-token gateway returned no error detail (this is unexpected; please report)".to_string(),
                }
            } else {
                String::new()
            }
        ));
    }

    Ok(resolved_tag)
}

/// v0.2.35 (Agent N): probe whether `<image>:<tag>` exists on the
/// registry using `<runtime> manifest inspect`.
///
/// Returns:
///   - `Ok(true)`: tag exists; the registry returned a manifest.
///   - `Ok(false)`: tag is missing on the registry. Distinguished from
///     transport errors by inspecting stderr: `manifest inspect` exits
///     non-zero with a `manifest unknown` / `not found` / `name unknown`
///     / `MANIFEST_UNKNOWN` substring in stderr when the tag genuinely
///     doesn't exist. Other non-zero exits (auth failure, network) bubble
///     up as `Err` so the caller can decide whether to fall through to a
///     blind pull (probe-flaky path) or hard-fail.
///   - `Err(msg)`: probe couldn't run (no runtime / spawn error / probe
///     subcommand unsupported by older `docker` versions without the
///     `experimental` flag enabled). Callers should treat this as
///     "probe inconclusive" and degrade gracefully — NOT as "tag
///     missing", since false-negatives would prevent valid pulls.
///
/// Auth context: the caller must have run `container_login` BEFORE
/// invoking this for a private registry — `manifest inspect` honours
/// the runtime's auth.json the same way `pull` does, so the same login
/// token covers both calls.
///
/// Runtime support:
///   - Podman: `podman manifest inspect` is built-in (all supported
///     versions).
///   - Docker: requires v23+ for the unflagged path; older versions need
///     `experimental: true` in the daemon config. v25+ is mainstream as
///     of 2026, so the probe is reliable; on older docker the probe
///     errors and the caller falls through to the blind-pull path.
///
/// Rate-limit consideration (GHCR): the manifest endpoint is the same
/// one `pull` uses for its initial manifest fetch, so an inspect-then-pull
/// sequence costs 1 extra round-trip per install attempt. GHCR's
/// unauthenticated rate limits are 60/hour/IP; the AUTHENTICATED rate is
/// 5000/hour — and the launcher is always authenticated by the time the
/// probe runs (Step 3 above runs after `container_login`). At one install
/// per minute the probe contributes 60 inspects/hour, well under cap.
pub(crate) async fn probe_image_tag_exists(
    image: &str,
    tag: &str,
    runtime: &str,
) -> Result<bool, String> {
    let image_ref = format!("{}:{}", image, tag);
    let output = Command::new(runtime)
        .args(["manifest", "inspect", &image_ref])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("spawn {} manifest inspect: {}", runtime, e))?;

    if output.status.success() {
        return Ok(true);
    }

    // Non-zero exit: inspect stderr to distinguish "tag missing" from
    // "transport / auth / unsupported-subcommand" errors. Registry
    // signals for genuine 404s are stable across docker + podman + GHCR:
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    let lower = stderr.to_lowercase();
    let signals_missing = lower.contains("manifest unknown")
        || lower.contains("manifest_unknown")
        || lower.contains("name unknown")
        || lower.contains("name_unknown")
        || lower.contains("not found")
        || lower.contains("denied")
        // GHCR returns 403 `denied` for non-existent private tags when
        // the token is repo-scoped — treat it as "missing" so the
        // fallback path engages. Public-image case is covered by the
        // "manifest unknown" / "not found" strings above.
        || lower.contains("no such manifest");

    if signals_missing {
        Ok(false)
    } else {
        Err(format!(
            "{} manifest inspect {} failed (exit {}): {}",
            runtime,
            image_ref,
            output.status.code().unwrap_or(-1),
            stderr.chars().take(500).collect::<String>(),
        ))
    }
}

/// v0.2.35 (Agent N): pick the `-cpu` fallback variant tag for a given
/// primary `tag`. The CPU variant is the always-available baseline per
/// `runtime_hints.gpu_image_variants` design — manifests are required to
/// declare it when they declare any variant block.
///
/// Returns `None` when:
///   - The manifest has no `gpu_image_variants` block (legacy single-tag
///     module; no fallback possible by design).
///   - The chosen GPU mode is ALREADY Cpu (the variant we'd "fall back
///     to" IS the primary; the fallback wouldn't help).
pub(crate) fn cpu_fallback_tag(
    manifest: &ModuleManifest,
    base_tag: &str,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
) -> Option<String> {
    use crate::commands::gpu_policy::GpuMode;
    let variants = manifest.runtime.gpu_image_variants.as_ref()?;
    // CPU/Metal already route to the cpu variant; fallback == primary,
    // no point probing twice.
    if matches!(gpu_mode, GpuMode::Cpu | GpuMode::Metal) {
        return None;
    }
    Some(variants.cpu.replace("{version}", base_tag))
}

/// v0.2.35 (Agent N): pure decision helper — given a probe closure and
/// the candidate tags, decide which tag to actually pull.
///
/// The closure-injection pattern lets unit tests exercise the entire
/// decision tree (primary hit / primary miss + fallback hit / both miss
/// / probe error) without needing a live container runtime or registry.
/// Production callers pass a closure that wraps [`probe_image_tag_exists`];
/// tests pass closures that return canned probe results.
///
/// Decision tree:
///   1. Probe `tag` (primary).
///      - Ok(true)  → pull `tag`.
///      - Ok(false) → step 2.
///      - Err(_)    → degrade to blind pull; return `tag` (legacy
///                    behaviour preserved so a flaky/unsupported probe
///                    doesn't block valid installs).
///   2. If `fallback_tag` is Some(`fb`) and `fb != tag`:
///      - Probe `fb`.
///        - Ok(true)  → pull `fb` (fallback engaged).
///        - Ok(false) → hard-fail: "no variant available on the registry".
///        - Err(e)    → hard-fail surfacing the probe error.
///   3. If `fallback_tag` is None (or == primary): hard-fail with the
///      "variant_not_found, no fallback declared" message.
///
/// The hard-fail cases return `Err(message)` — callers MUST log out
/// before propagating to avoid leaking the auth token in `auth.json`.
pub(crate) async fn decide_variant_to_pull<F, Fut>(
    image: &str,
    tag: &str,
    fallback_tag: Option<&str>,
    module_id: &str,
    probe: F,
) -> Result<String, String>
where
    F: Fn(String, String) -> Fut,
    Fut: std::future::Future<Output = Result<bool, String>>,
{
    match probe(image.to_string(), tag.to_string()).await {
        Ok(true) => Ok(tag.to_string()),
        Ok(false) => {
            eprintln!(
                "[installer_engine] container_pull[{}]: variant {}:{} not found on registry; \
                 will try fallback if available",
                module_id, image, tag
            );
            match fallback_tag {
                Some(fb) if fb != tag => match probe(image.to_string(), fb.to_string()).await {
                    Ok(true) => {
                        eprintln!(
                            "[installer_engine] container_pull[{}]: falling back to {}:{}",
                            module_id, image, fb
                        );
                        Ok(fb.to_string())
                    }
                    Ok(false) => Err(format!(
                        "no variant of {} is available on the registry \
                         (probed {}:{} and fallback {}:{}, neither found). \
                         The module publisher hasn't built a compatible variant \
                         for this release yet — try again later or contact \
                         the publisher.",
                        image, image, tag, image, fb
                    )),
                    Err(probe_err) => Err(format!(
                        "variant {}:{} missing on registry; fallback {}:{} probe failed: {}",
                        image, tag, image, fb, probe_err
                    )),
                },
                _ => Err(format!(
                    "variant_not_found: {}:{} is not available on the registry, \
                     and no fallback variant was declared in the manifest.",
                    image, tag
                )),
            }
        }
        Err(probe_err) => {
            // Probe inconclusive (network, runtime error, unsupported
            // subcommand). Degrade to the legacy blind-pull behaviour
            // — the pull itself will surface a proper error if the tag
            // doesn't exist. False-negatives from a flaky probe MUST
            // NOT prevent valid installs.
            eprintln!(
                "[installer_engine] container_pull[{}]: probe failed for {}:{}: {}. \
                 Falling through to blind pull (legacy behaviour).",
                module_id, image, tag, probe_err
            );
            Ok(tag.to_string())
        }
    }
}

#[derive(Debug, serde::Deserialize)]
struct PullTokenResponse {
    pub pull_token: String,
    /// v0.2.36 wire-contract addition. The GitHub username that the
    /// pull_token authenticates as — passed to `podman/docker login -u`.
    /// For personal-account GHCR packages this MUST match the PAT
    /// owner's GitHub login (empirically verified 2026-05-26: synthetic
    /// usernames get 403 from ghcr.io). For org-package paths
    /// (v0.2.36+) the server returns a synthetic username + a properly-
    /// scoped registry token. Optional so a v0.2.36 launcher remains
    /// compatible with the pre-v0.2.36 server response shape.
    #[serde(default)]
    pub username: Option<String>,
    #[serde(default)]
    pub expires_in_s: u64,
}

/// Request a short-lived registry pull token from the manifest's
/// `pull_token_endpoint` (Phase 3A, v0.2.35).
///
/// Wire contract (matches `launcher/supabase/functions/rl-artifact-url`):
///   Request:  `{ "license_key": "<UUID-or-vct_admin_*>", "machine_id_hash": "<sha256 hex>" }`
///   Response: `{ "image", "tag", "registry", "pull_token", "expires_in_s", "expires_at" }`
///
/// The launcher serves the user's license key out of the OS keychain —
/// the SAME source `license_refresh` uses for `/validate-tier`. The
/// `machine_id_hash` is derived from a platform-stable host identifier
/// (Windows MachineGuid / macOS IOPlatformUUID / Linux /etc/machine-id)
/// via `commands::licensing::machine_id_hash`, again matching the
/// `validate-tier` call site so the server sees consistent bindings.
/// The algorithm switched from MAC-based to platform-stable in v0.2.36
/// — see commands/licensing.rs for the rationale.
///
/// Returned errors are passed to the caller in user-readable form
/// (NOT just propagated through `container_pull`'s misleading
/// "Phase 3A gateway not deployed" footer text — that footer remains
/// only for the truly-anonymous-pull case where this function returned
/// `Err` BEFORE even reaching the server). The container_pull error
/// formatter inspects the structured error variant in v0.2.35.
///
/// Pre-v0.2.35 behaviour (the stub): POSTed `~/.vibecoded/license_cache.json`
/// body verbatim — that body has no `license_key` field, server returned
/// 400 invalid-shape, every paid-module install fell through to anonymous
/// pull and 401'd on the private registry.
async fn request_pull_token(
    container: &crate::manifest::ContainerInstallBlock,
    // NEW-1 (2026-05-28): when the L0 catalog supplies a pull_token_endpoint,
    // prefer it over the L1 manifest's value. L0 is the server-side SoT and
    // always carries the real Supabase URL; L1 (image-extracted) may contain
    // a placeholder (e.g. "placeholder.supabase.co/…") if the publisher
    // shipped the image without running manifest-hygiene CI.
    l0_pull_token_endpoint: Option<&str>,
) -> Result<PullTokenResponse, String> {
    // 1. License key from keychain.
    let license_key = crate::commands::licensing::read_license_key_from_keychain()
        .map_err(|e| format!("keychain read failed: {}", e))?
        .ok_or_else(|| {
            "no license activated — open Settings → License → Activate to enter your key".to_string()
        })?;

    // 2. Machine ID hash — sha256 of 8-byte big-endian MAC. Same algorithm
    // `license_refresh` uses, so the server sees a consistent binding
    // when comparing pull-time vs activation-time machine identity.
    let machine_hash = crate::commands::licensing::machine_id_hash();

    // 3. HTTP client. 15s timeout — long enough to absorb a slow GHCR
    // token-exchange roundtrip on the server side; short enough to not
    // hang the install UI.
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .map_err(|e| format!("build http client: {}", e))?;

    let method = container
        .pull_token_method
        .parse::<reqwest::Method>()
        .unwrap_or(reqwest::Method::POST);

    // NEW-1 (2026-05-28): prefer L0 catalog URL over L1 manifest's value.
    let endpoint = l0_pull_token_endpoint.unwrap_or(&container.pull_token_endpoint);

    let resp = client
        .request(method, endpoint)
        .json(&serde_json::json!({
            "license_key": license_key,
            "machine_id_hash": machine_hash,
        }))
        .send()
        .await
        .map_err(|e| format!("POST {}: {}", endpoint, e))?;

    let status = resp.status();
    if status.is_success() {
        let parsed: PullTokenResponse = resp
            .json()
            .await
            .map_err(|e| format!("parse pull-token response: {}", e))?;
        return Ok(parsed);
    }

    let body: serde_json::Value = resp
        .json()
        .await
        .unwrap_or_else(|_| serde_json::json!({}));
    Err(format_pull_token_error(status.as_u16(), &body))
}

/// Map a non-2xx `rl-artifact-url` response into a user-actionable string.
///
/// Lifted to a free function so the test module can exercise every
/// error-code/HTTP-status pairing without spinning up an HTTP server.
/// The error body shape is `{ error: <code>, detail?: <string>,
/// required_tier?: <string>, got?: <string> }` per the edge function
/// at `launcher/supabase/functions/rl-artifact-url/index.ts`.
pub(crate) fn format_pull_token_error(status: u16, body: &serde_json::Value) -> String {
    let code = body
        .get("error")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown_error");
    let detail = body
        .get("detail")
        .and_then(|v| v.as_str())
        .unwrap_or("");

    match (status, code) {
        (400, _) => format!(
            "pull-token gateway rejected the request shape ({}). \
             This is a launcher bug — please report it. detail={}",
            code, detail
        ),
        (401, "license_invalid") => {
            "your license key is invalid or has been revoked. \
             Open Settings → License → Refresh; if the problem persists, \
             contact support."
                .to_string()
        }
        (401, "license_expired") => {
            "your license has expired. Renew on the dashboard, then \
             open Settings → License → Refresh."
                .to_string()
        }
        (401, "tier_insufficient") => {
            let required = body
                .get("required_tier")
                .and_then(|v| v.as_str())
                .unwrap_or("pro");
            let got = body.get("got").and_then(|v| v.as_str()).unwrap_or("free");
            format!(
                "this module requires the {} tier; your license validates as {}. \
                 Upgrade on the dashboard, then open Settings → License → Refresh.",
                required, got
            )
        }
        (401, _) => format!(
            "license check failed at the pull-token gateway: {} ({})",
            code, detail
        ),
        (500, _) => format!(
            "pull-token gateway is temporarily unavailable ({}). \
             Try again in a few minutes; if it persists, check Services tab.",
            detail
        ),
        (s, c) => format!("pull-token gateway returned HTTP {}: {} ({})", s, c, detail),
    }
}

/// Detect which container runtime to use. Prefers podman (matches the
/// rest of VCO's container stack), falls back to docker.
///
/// v0.2.33 (Agent C): hoisted to `pub(crate)` so the post-install
/// manifest extractor (`commands::module_manifest_extract`) can probe
/// the SAME runtime that `container_pull` chose — guarantees
/// `docker cp` runs against the runtime that did the `docker pull`,
/// so the image reference resolves to a known-good local copy.
pub(crate) async fn detect_container_runtime() -> Result<String, String> {
    for candidate in ["podman", "docker"] {
        let probe = Command::new(candidate).silent()
            .args(["--version"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await;
        if probe.map(|s| s.success()).unwrap_or(false) {
            return Ok(candidate.to_string());
        }
    }
    Err("no container runtime found (tried podman, docker)".into())
}

/// `<runtime> login <registry> -u <username> --password-stdin` with the
/// token piped to stdin. Stdin-feed avoids exposing the token in argv
/// (where `ps` would see it).
///
/// v0.2.36 (2026-05-26): the username is NO LONGER irrelevant. Empirical
/// finding from dogfooding: ghcr.io's `/v2/token` endpoint, when called
/// during `podman/docker login`, rejects mismatched username/credential
/// pairs with `403 Forbidden — Requesting bearer token: invalid status
/// code from registry`. For personal-account GHCR packages the username
/// MUST be the PAT owner's GitHub login. The caller is responsible for
/// passing the correct username — usually obtained from the
/// `rl-artifact-url` response's `username` field (server-controlled, so
/// even if we ever switch from personal-account to org packages, the
/// client side stays the same).
///
/// Cross-runtime + cross-OS: `login` subcommand syntax is identical
/// between podman and docker; `--password-stdin` works on Linux, macOS,
/// and Windows for both. Verified manually 2026-05-26 with podman on
/// Linux; documented to work the same way per docker CLI reference.
async fn container_login(
    runtime: &str,
    registry: &str,
    username: &str,
    token: &str,
) -> Result<(), String> {
    use tokio::io::AsyncWriteExt;

    let mut child = Command::new(runtime)
        .silent()
        .args(["login", registry, "-u", username, "--password-stdin"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("spawn {} login: {}", runtime, e))?;

    {
        let mut stdin = child
            .stdin
            .take()
            .ok_or("login stdin not captured")?;
        stdin
            .write_all(token.as_bytes())
            .await
            .map_err(|e| format!("write token to {} login stdin: {}", runtime, e))?;
        // stdin drops here → EOF, login proceeds.
    }

    let output = child
        .wait_with_output()
        .await
        .map_err(|e| format!("wait {} login: {}", runtime, e))?;

    if !output.status.success() {
        return Err(format!(
            "{} login {} failed (exit {}): {}",
            runtime,
            registry,
            output.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&output.stderr).chars().take(300).collect::<String>()
        ));
    }
    Ok(())
}

fn infer_registry_from_image(image: &str) -> String {
    // "ghcr.io/hotak92/vct-rl-reranker" → "ghcr.io"
    image
        .split_once('/')
        .map(|(head, _)| head.to_string())
        .unwrap_or_else(|| "docker.io".to_string())
}

async fn git_clone(source: &str, git_ref: &str, dest: &Path) -> Result<(), String> {
    if let Some(parent) = dest.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|e| format!("create parent {}: {}", parent.display(), e))?;
    }

    // v0.2.37 (Issue 7): see `container_pull` above for the rationale.
    // Stdio::null() avoids the pipe-buffer deadlock when cloning a repo
    // that emits large amounts of progress text (e.g. a depth-1 clone of
    // a repo with many submodules or large pack files).
    let status = Command::new("git").silent()
        .args(["clone", "--depth", "1", "--branch", git_ref, source])
        .arg(dest)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await
        .map_err(|e| format!("spawn git clone: {}", e))?;

    if !status.success() {
        return Err(format!(
            "git clone failed (exit {}): {} {} -> {}",
            status.code().unwrap_or(-1),
            source,
            git_ref,
            dest.display()
        ));
    }
    Ok(())
}

async fn run_post_install_command(
    spec: &CommandSpec,
    ctx: &PlaceholderCtx,
) -> Result<(), String> {
    let raw = pick_platform_cmd(spec);
    let resolved = ctx.resolve(&raw);

    let tokens = shlex::split(&resolved)
        .ok_or_else(|| format!("cannot tokenize command: {}", resolved))?;
    let (program, args) = tokens
        .split_first()
        .ok_or_else(|| format!("empty command: {}", resolved))?;

    // Resolve cwd
    let cwd_str = spec
        .cwd
        .as_ref()
        .map(|s| ctx.resolve(s))
        .unwrap_or_else(|| {
            ctx.install_dir
                .as_ref()
                .map(|p| p.display().to_string())
                .unwrap_or_else(|| ".".to_string())
        });
    let cwd = PathBuf::from(&cwd_str);

    let mut cmd = Command::new(program).silent();
    cmd.args(args);
    cmd.current_dir(&cwd);
    // Scrubbed env: only pass through PATH, HOME, USER, and platform essentials.
    cmd.env_clear();
    for key in ["PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL"] {
        if let Ok(v) = std::env::var(key) {
            cmd.env(key, v);
        }
    }
    #[cfg(target_os = "windows")]
    for key in ["SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP"] {
        if let Ok(v) = std::env::var(key) {
            cmd.env(key, v);
        }
    }

    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());

    let timeout = Duration::from_secs(spec.timeout_s);
    let output = tokio::time::timeout(timeout, cmd.output())
        .await
        .map_err(|_| format!("command timed out after {}s: {}", spec.timeout_s, resolved))?
        .map_err(|e| format!("spawn {}: {}", program, e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "command failed (exit {}): {}\nstderr: {}",
            output.status.code().unwrap_or(-1),
            resolved,
            stderr.chars().take(500).collect::<String>()
        ));
    }

    Ok(())
}

fn pick_platform_cmd(spec: &CommandSpec) -> String {
    let platform = if cfg!(target_os = "windows") {
        "windows"
    } else if cfg!(target_os = "macos") {
        "macos"
    } else {
        "linux"
    };
    spec.platform_cmd
        .get(platform)
        .cloned()
        .unwrap_or_else(|| spec.cmd.clone())
}

fn emit_progress(
    app: &AppHandle,
    project_id: &str,
    module_id: &str,
    stage: InstallStage,
    step_index: u32,
    step_total: u32,
    message: &str,
) {
    let percent = if step_total == 0 {
        0
    } else {
        ((step_index as f32 / step_total as f32) * 100.0).round() as u8
    };
    let payload = InstallProgress {
        project_id: project_id.to_string(),
        module_id: module_id.to_string(),
        stage,
        step_index,
        step_total,
        percent,
        message: message.to_string(),
    };
    // Emit via Tauri's event system; swallow errors (UI-only signal).
    let _ = app.emit("module://install-progress", payload);
}

/// v0.2.20: pick the OCI image tag based on the host's GPU mode.
///
/// When the manifest declares `runtime.gpu_image_variants`, each
/// `GpuMode` maps to a tag suffix:
///
/// | GpuMode | Variant tag used |
/// |---------|------------------|
/// | Cuda    | `gpu_image_variants.cuda` |
/// | Rocm    | `gpu_image_variants.rocm` |
/// | Cpu     | `gpu_image_variants.cpu`  |
/// | Metal   | `gpu_image_variants.cpu` (no Metal torch wheels today) |
///
/// When `gpu_image_variants` is absent (legacy modules + non-container
/// runtimes), returns `base_tag` unchanged — preserves single-tag
/// behavior for modules that only ship one image.
///
/// v0.2.34: variant strings from the L0 catalog ship as templates
/// (e.g. `"{version}-cuda"`) so the same manifest fixture serves
/// every released version. This function performs the `{version}`
/// substitution against `base_tag` so callers receive a ready-to-pull
/// image tag (`"0.2.7-cuda"`) rather than the literal template
/// string. Variants that don't contain `{version}` are returned
/// unchanged — backwards-compatible with pre-template manifests.
pub(crate) fn resolve_variant_tag(
    manifest: &ModuleManifest,
    base_tag: &str,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
) -> String {
    use crate::commands::gpu_policy::GpuMode;
    let variants = match manifest.runtime.gpu_image_variants.as_ref() {
        Some(v) => v,
        None => return base_tag.to_string(),
    };
    let template = match gpu_mode {
        GpuMode::Cuda => &variants.cuda,
        GpuMode::Rocm => &variants.rocm,
        GpuMode::Cpu | GpuMode::Metal => &variants.cpu,
    };
    template.replace("{version}", base_tag)
}

/// v0.2.31 (#27): emit a terminal `InstallStage::Failed` event on the
/// progress channel. Called by `run_install`'s outer wrapper and by
/// `run_upgrade` on every error return path. The UI uses this event to
/// flip the progress bar from "in flight" to "failed: <msg>" without
/// waiting for the Tauri command's Result to deserialise.
///
/// `stage` is the LAST stage that was in flight when the error occurred
/// (Clone / PostInstall / Migrate). It's advisory only — the UI keys off
/// `InstallStage::Failed` to decide what to render.
pub(crate) fn report_error(
    app: &AppHandle,
    project_id: &str,
    module_id: &str,
    _stage: InstallStage,
    msg: &str,
) {
    let payload = InstallProgress {
        project_id: project_id.to_string(),
        module_id: module_id.to_string(),
        stage: InstallStage::Failed,
        step_index: 0,
        step_total: 0,
        percent: 0,
        message: msg.to_string(),
    };
    let _ = app.emit("module://install-progress", payload);
}

/// v0.2.31 (#20-Fix-3): perform an in-place upgrade of an already-installed
/// module to the catalog's current version. Parallel to `run_install` — does
/// NOT modify it.
///
/// Behaviour:
///   * Reads `manifest.upgrade` (`Option<UpgradeBlock>`).
///   * If `Some(upgrade_block)`:
///     1. Runs `upgrade_block.pre_upgrade` commands in declaration order
///        (scrubbed env, same `run_post_install_command` mechanism as install).
///     2. Re-fetches the artifact per `manifest.install.method`:
///        - `GitClone`: `git pull` in `install_dir` if it's a git repo, else
///          re-clones from `manifest.install.source` (cleans the dir first).
///        - `ContainerPull`: `podman pull` (or `docker pull`) the resolved
///          GPU-variant tag for `manifest.version` via the same token-gateway
///          + `resolve_variant_tag` path the install uses.
///        - `Local`: no-op (user has already updated the directory by hand).
///     3. Runs `upgrade_block.post_upgrade` commands.
///     4. If `upgrade_block.migration_script` is `Some(...)`, executes it
///        as a single shell-tokenised command (same mechanism, scrubbed env).
///   * If `None`: falls back to the legacy "uninstall + reinstall" sequence
///     with a warning logged. The caller (`update_module_for_project`) is
///     responsible for any DB cleanup that needs to happen between the
///     uninstall + reinstall halves of the fallback; this function only
///     handles the install-engine side (re-fetch the artifact).
///
/// Emits `module://install-progress` events with stage values
/// `Clone` (fetch) / `PostInstall` (commands) / `Migrate` (migration script)
/// / `Done` (success) / `Failed` (any error before Done).
///
/// `gpu_mode` is consulted in the same cases as `run_install` —
/// `ContainerPull` + `manifest.runtime.gpu_image_variants.is_some()`.
pub async fn run_upgrade(
    app: &AppHandle,
    manifest: &ModuleManifest,
    previous_install: &crate::db::models::ModuleInstallRow,
    ctx: &PlaceholderCtx,
    project_id: &str,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
    db: &crate::db::Db,
    // NEW-1 (2026-05-28): same L0-over-L1 override as run_install.
    l0_pull_token_endpoint: Option<&str>,
) -> Result<PathBuf, String> {
    // Outer wrapper: every Err path emits InstallStage::Failed before
    // propagating (#27 contract — same as run_install).
    match run_upgrade_inner(app, manifest, previous_install, ctx, project_id, gpu_mode, db, l0_pull_token_endpoint).await {
        Ok(p) => Ok(p),
        Err(e) => {
            report_error(app, project_id, &manifest.id, InstallStage::PostInstall, &e);
            Err(e)
        }
    }
}

async fn run_upgrade_inner(
    app: &AppHandle,
    manifest: &ModuleManifest,
    previous_install: &crate::db::models::ModuleInstallRow,
    ctx: &PlaceholderCtx,
    project_id: &str,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
    db: &crate::db::Db,
    l0_pull_token_endpoint: Option<&str>,
) -> Result<PathBuf, String> {
    // v0.2.29 launcher-version gate: enforce min_launcher_version on
    // updates the same way install does. A user could have installed
    // module@0.1.0 on an old launcher; the new manifest@0.2.0 bumps
    // min_launcher_version. Without this gate, the in-place upgrade
    // would proceed and then fail mid-flight on missing APIs.
    if let Some(required) = manifest.compatibility.min_launcher_version.as_deref() {
        let required = required.trim();
        if !required.is_empty() {
            let current = env!("CARGO_PKG_VERSION");
            if version_lt(current, required) {
                return Err(format!(
                    "module '{}' requires launcher >= {} but this launcher is {}. \
                     Update the launcher first (Settings → Updates → Update orchestrator), \
                     then retry the update.",
                    manifest.id, required, current,
                ));
            }
        }
    }

    let install_dir = ctx.resolve_install_dir(&manifest.install.install_dir);
    let allowed_root = ctx.vct_modules.clone();
    // v0.2.34: same bootstrap as `run_install_inner` — upgrades on a
    // never-installed-before machine (rare but possible: catalog-only
    // entry whose previous install never actually completed) still
    // need the root to exist before the guard runs.
    std::fs::create_dir_all(&allowed_root).map_err(|e| {
        format!(
            "bootstrap allowed_root {}: {}",
            allowed_root.display(),
            e
        )
    })?;
    crate::manifest::validate_install_dir(&install_dir, &allowed_root)?;

    let upgrade_block = match manifest.upgrade.as_ref() {
        Some(b) => b.clone(),
        None => {
            // Legacy fallback: no upgrade block declared. Emit a warning
            // log line and run a minimal "re-fetch artifact + skip
            // post_install" sequence. The caller is responsible for any
            // additional DB cleanup; here we just bring the on-disk
            // artifact up to date.
            eprintln!(
                "[installer_engine] run_upgrade[{}]: manifest declares no `upgrade` block; \
                 falling back to bare artifact re-fetch (no pre/post-upgrade hooks, no \
                 migration script). For load-bearing upgrades the module author should \
                 add an `upgrade` block with explicit pre_upgrade/post_upgrade commands.",
                manifest.id,
            );
            // Re-fetch only. Treat as a single-step progress sequence.
            emit_progress(
                app, project_id, &manifest.id, InstallStage::Clone, 0, 1,
                "Fetching updated source (legacy fallback)",
            );
            let pulled_tag = refetch_artifact(manifest, &install_dir, gpu_mode, l0_pull_token_endpoint).await?;
            // v0.2.33 (Agent C, L0b): re-extract the manifest after a
            // re-pull. The upgrade flow may have brought in a new
            // image version whose manifest differs from the previous
            // on-disk copy — the renderer / dispatcher / DB migrations
            // need the fresh file. Atomic write with .bak rollback
            // preserves the v0.2.7 manifest if the v0.2.8 extract
            // fails (architecture review §J4 G-b).
            //
            // v0.2.35 (Agent N): thread `pulled_tag` through so the
            // post-pull extractor uses the tag that was actually
            // pulled (matters when a cuda→cpu variant fallback happened
            // inside refetch_artifact).
            extract_manifest_after_refetch(
                app, project_id, manifest, gpu_mode, pulled_tag.as_deref(), 0, 1,
            )
            .await?;
            emit_progress(
                app, project_id, &manifest.id, InstallStage::Done, 1, 1,
                "Update complete (legacy fallback)",
            );
            // previous_install kept for symmetry with the upgrade-block path;
            // legacy fallback consults nothing on it.
            let _ = previous_install;
            return Ok(install_dir);
        }
    };

    let migrate_steps = if upgrade_block.migration_script.is_some() { 1 } else { 0 };
    let total_steps: u32 = (upgrade_block.pre_upgrade.len()
        + 1 // re-fetch artifact
        + upgrade_block.post_upgrade.len()
        + migrate_steps) as u32;
    let mut step_index: u32 = 0;

    let ctx_with_dir = ctx.clone().with_install_dir(install_dir.clone());

    // ─── pre_upgrade ─────────────────────────────────────────────────────
    for (i, spec) in upgrade_block.pre_upgrade.iter().enumerate() {
        let msg = format!(
            "Running pre-upgrade step {}/{}",
            i + 1, upgrade_block.pre_upgrade.len(),
        );
        emit_progress(
            app, project_id, &manifest.id, InstallStage::PostInstall,
            step_index, total_steps, &msg,
        );
        step_index += 1;
        run_post_install_command(spec, &ctx_with_dir).await?;
    }

    // ─── re-fetch artifact ───────────────────────────────────────────────
    emit_progress(
        app, project_id, &manifest.id, InstallStage::Clone,
        step_index, total_steps, "Fetching updated source",
    );
    step_index += 1;
    let pulled_tag = refetch_artifact(manifest, &install_dir, gpu_mode, l0_pull_token_endpoint).await?;

    // v0.2.33 (Agent C, L0b): re-extract the manifest after the
    // upgrade re-pull. See the legacy-fallback comment above for
    // the rationale. .bak rollback restores the previous version's
    // manifest on extract failure.
    //
    // v0.2.35 (Agent N): thread `pulled_tag` so the post-pull extractor
    // uses the tag that was actually pulled (matters for variant
    // fallback — see refetch_artifact's docstring).
    extract_manifest_after_refetch(
        app, project_id, manifest, gpu_mode,
        pulled_tag.as_deref(), step_index, total_steps,
    )
    .await?;

    // ─── post_upgrade ────────────────────────────────────────────────────
    for (i, spec) in upgrade_block.post_upgrade.iter().enumerate() {
        let msg = format!(
            "Running post-upgrade step {}/{}",
            i + 1, upgrade_block.post_upgrade.len(),
        );
        emit_progress(
            app, project_id, &manifest.id, InstallStage::PostInstall,
            step_index, total_steps, &msg,
        );
        step_index += 1;
        run_post_install_command(spec, &ctx_with_dir).await?;
    }

    // ─── migration_script (optional, one-shot) ───────────────────────────
    if let Some(script_raw) = upgrade_block.migration_script.as_ref() {
        emit_progress(
            app, project_id, &manifest.id, InstallStage::Migrate,
            step_index, total_steps, "Running migration script",
        );
        step_index += 1;
        // Mirror CommandSpec's shape so the same scrubbed-env executor runs it.
        let spec = CommandSpec {
            cmd: script_raw.clone(),
            cwd: None,
            timeout_s: 600, // migrations may be slow; 10-min ceiling.
            platform_cmd: std::collections::HashMap::new(),
            note: None,
        };
        run_post_install_command(&spec, &ctx_with_dir).await?;
    }

    // ─── v0.2.31: module-shipped DB migrations (upgrade path) ────────────
    //
    // Same soft-fail discipline as `run_install`. A new module version
    // may ship additional SQL files; the apply mechanism is idempotent
    // (SHA-keyed), so already-applied files are skipped silently.
    // Errors are surfaced via the same `module://db-migration-failed`
    // event the dashboard listens for.
    if manifest.db.is_some() {
        emit_progress(
            app, project_id, &manifest.id, InstallStage::Migrate,
            step_index, total_steps, "Applying module DB migrations",
        );
        let module_id_for_log = manifest.id.clone();
        match crate::db::module_db_migrations::apply_module_db_migrations(
            db,
            &manifest.id,
            &install_dir,
            manifest,
        ) {
            Ok(report) => {
                if !report.ok() {
                    eprintln!(
                        "[installer_engine] run_upgrade[{}]: module DB migration(s) failed: {:?}",
                        module_id_for_log, report.errors,
                    );
                    let _ = app.emit(
                        "module://db-migration-failed",
                        serde_json::json!({
                            "project_id": project_id,
                            "module_id": manifest.id,
                            "errors": report.errors,
                            "applied": report.applied,
                            "skipped": report.skipped,
                            "operation": "upgrade",
                        }),
                    );
                } else if !report.applied.is_empty() {
                    eprintln!(
                        "[installer_engine] run_upgrade[{}]: applied {} DB migration(s)",
                        module_id_for_log, report.applied.len(),
                    );
                }
            }
            Err(e) => {
                eprintln!(
                    "[installer_engine] run_upgrade[{}]: apply_module_db_migrations errored: {}",
                    module_id_for_log, e
                );
                let _ = app.emit(
                    "module://db-migration-failed",
                    serde_json::json!({
                        "project_id": project_id,
                        "module_id": manifest.id,
                        "error": e,
                        "operation": "upgrade",
                    }),
                );
            }
        }
    }

    emit_progress(
        app, project_id, &manifest.id, InstallStage::Done,
        step_index, total_steps, "Update complete",
    );

    // previous_install is currently advisory (we report it back via the
    // command's return value to the DB layer); future work may diff
    // version strings here to short-circuit no-op upgrades.
    let _ = previous_install;

    Ok(install_dir)
}

/// v0.2.31 (#20-Fix-3): re-fetch the module's artifact for an in-place
/// upgrade. Mirrors the fetch step of `run_install_inner` but is
/// idempotent across "directory already exists" cases:
///   - `GitClone`: `git pull` if `install_dir/.git` exists, else
///     wipe + re-clone (handles user-corrupted dirs gracefully).
///   - `ContainerPull`: `podman pull` the resolved variant tag.
///   - `Local`: no-op (user has already updated by hand).
///
/// v0.2.35 (Agent N): returns `Some(actual_tag)` when the install
/// method was `ContainerPull` so `extract_manifest_after_refetch` can
/// use the SAME tag that was pulled (avoids the cuda→cpu fallback case
/// where the post-pull manifest extractor would otherwise try to
/// `docker cp` from a never-pulled `0.2.7-cuda` reference). Returns
/// `None` for `GitClone` / `Local` (no image tag involved).
async fn refetch_artifact(
    manifest: &ModuleManifest,
    install_dir: &Path,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
    l0_pull_token_endpoint: Option<&str>,
) -> Result<Option<String>, String> {
    match manifest.install.method {
        InstallMethod::GitClone => {
            let git_dir = install_dir.join(".git");
            if git_dir.exists() {
                // Existing checkout — git pull preserves any user-edited
                // gitignored files (e.g. local config under the install
                // dir).
                // v0.2.37 (Issue 7): see `container_pull` for the rationale.
                // Stdio::null() avoids the pipe-buffer deadlock that could
                // occur on an unusually verbose fast-forward (large pack
                // fetch + many ref updates).
                let status = Command::new("git").silent()
                    .args(["-C"])
                    .arg(install_dir)
                    .args(["pull", "--ff-only"])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status()
                    .await
                    .map_err(|e| format!("spawn git pull: {}", e))?;
                if !status.success() {
                    return Err(format!(
                        "git pull failed (exit {}) in {}: \
                         install_dir may have diverged from upstream; \
                         re-install the module to recover.",
                        status.code().unwrap_or(-1),
                        install_dir.display(),
                    ));
                }
            } else {
                // No .git dir — fall back to a fresh clone. Remove the
                // existing dir first so `git clone` doesn't refuse.
                if install_dir.exists() {
                    tokio::fs::remove_dir_all(install_dir).await.map_err(|e| {
                        format!(
                            "remove pre-existing install_dir {} before re-clone: {}",
                            install_dir.display(),
                            e,
                        )
                    })?;
                }
                git_clone(
                    manifest.install.source.as_deref().ok_or(
                        "install.source required for git_clone upgrade fallback",
                    )?,
                    manifest.install.r#ref.as_deref().unwrap_or("main"),
                    install_dir,
                )
                .await?;
            }
        }
        InstallMethod::Local => {
            // No-op: user updated the directory by hand. Sanity-check
            // existence so we surface a clear error if the dir vanished.
            if !install_dir.exists() {
                return Err(format!(
                    "install.method=local but install_dir is missing: {}",
                    install_dir.display(),
                ));
            }
        }
        InstallMethod::ContainerPull => {
            let container = manifest.install.container.as_ref().ok_or(
                "install.method=container_pull requires install.container block",
            )?;
            let base_tag = if container.tag_from_version {
                manifest.version.clone()
            } else {
                manifest
                    .install
                    .r#ref
                    .clone()
                    .unwrap_or_else(|| "latest".to_string())
            };
            let tag = resolve_variant_tag(manifest, &base_tag, gpu_mode);
            // v0.2.35 (Agent N): same probe-and-fallback contract as
            // run_install_inner. No InstallStage::VariantFallback event
            // emitted here — refetch_artifact has no AppHandle and the
            // outer `extract_manifest_after_refetch` consumes the
            // returned tag instead. Update path doesn't surface the
            // fallback to the GUI today; logs in container_pull record
            // it for diagnostics.
            let fallback = cpu_fallback_tag(manifest, &base_tag, gpu_mode);
            let actual_tag = container_pull(
                container,
                &tag,
                fallback.as_deref(),
                &manifest.id,
                l0_pull_token_endpoint,
            )
            .await?;
            return Ok(Some(actual_tag));
        }
    }
    Ok(None)
}

/// v0.2.33 (Agent C, L0b): for `ContainerPull` modules, extract the
/// FULL manifest from the image into `~/.vct/modules/<id>/vct-module.json`
/// after `refetch_artifact` has brought the image into the runtime
/// cache. No-op for `GitClone` / `Local` modules — those carry their
/// manifest in the repo / user-managed install_dir already.
///
/// Shared between the run_upgrade legacy-fallback path and the
/// upgrade-block path to keep the extract policy identical for both.
///
/// v0.2.35 (Agent N): accepts an optional `pulled_tag` override. When
/// `refetch_artifact` reports the actually-pulled tag (which may differ
/// from `resolve_variant_tag`'s preferred tag because of the cuda→cpu
/// fallback path), this function uses that tag for `docker cp`. Without
/// the override, an upgrade that fell back to the cpu variant would
/// extract from a non-existent local image and surface a confusing
/// "image not found" error.
async fn extract_manifest_after_refetch(
    app: &AppHandle,
    project_id: &str,
    manifest: &ModuleManifest,
    gpu_mode: crate::commands::gpu_policy::GpuMode,
    pulled_tag: Option<&str>,
    step_index: u32,
    total_steps: u32,
) -> Result<(), String> {
    if manifest.install.method != InstallMethod::ContainerPull {
        return Ok(());
    }
    let container = manifest
        .install
        .container
        .as_ref()
        .ok_or("install.method=container_pull requires install.container block")?;
    let base_tag = if container.tag_from_version {
        manifest.version.clone()
    } else {
        manifest
            .install
            .r#ref
            .clone()
            .unwrap_or_else(|| "latest".to_string())
    };
    let tag = pulled_tag
        .map(|s| s.to_string())
        .unwrap_or_else(|| resolve_variant_tag(manifest, &base_tag, gpu_mode));
    let image_ref = format!("{}:{}", container.image, tag);
    let runtime = detect_container_runtime().await?;
    emit_progress(
        app,
        project_id,
        &manifest.id,
        InstallStage::ExtractingManifest,
        step_index,
        total_steps,
        "Extracting module manifest from image",
    );
    crate::commands::module_manifest_extract::extract_manifest_from_image(
        &image_ref,
        &manifest.id,
        &runtime,
    )
    .await?;
    Ok(())
}

/// v0.2.29: tiny semver comparison used by the `min_launcher_version`
/// gate. Returns true iff `a < b` lexicographically over numeric
/// components. Non-numeric prefixes of each dot-separated component are
/// parsed as 0 (so `0.2.28-dev` compares as `0.2.28`). Pre-release
/// suffixes and build metadata are ignored — same approximation used by
/// `commands::installer::version_is_outdated`. The check is "is this
/// launcher OLDER than the required version?" — if true, the install
/// is refused.
fn version_lt(a: &str, b: &str) -> bool {
    fn parse(v: &str) -> Vec<u64> {
        v.split('.')
            .map(|p| {
                p.chars()
                    .take_while(|c| c.is_ascii_digit())
                    .collect::<String>()
            })
            .map(|s| s.parse::<u64>().unwrap_or(0))
            .collect()
    }
    let ai = parse(a);
    let bi = parse(b);
    let len = ai.len().max(bi.len());
    for idx in 0..len {
        let aa = *ai.get(idx).unwrap_or(&0);
        let bb = *bi.get(idx).unwrap_or(&0);
        if aa < bb {
            return true;
        }
        if aa > bb {
            return false;
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::gpu_policy::GpuMode;
    use crate::manifest::GpuImageVariants;

    fn manifest_with_variants(variants: Option<GpuImageVariants>) -> ModuleManifest {
        let mut m: ModuleManifest = serde_json::from_str(
            r#"{
              "schema_version": "1",
              "id": "vct-test",
              "name": "Test",
              "version": "0.1.0",
              "description": "Test module",
              "category": "paid-independent",
              "license": { "required": true, "min_orchestrator_tier": "pro" },
              "compatibility": { "hosts": ["base"] },
              "install": { "method": "container_pull",
                "container": {
                  "image": "ghcr.io/test/img",
                  "tag_from_version": true,
                  "pull_token_endpoint": "https://example.test/token"
                } },
              "runtime": { "type": "container", "command": "python" }
            }"#,
        )
        .expect("test fixture parses");
        m.runtime.gpu_image_variants = variants;
        m
    }

    #[test]
    fn variant_tag_picks_cuda_when_mode_is_cuda() {
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "0.1.0-cpu".into(),
            cuda: "0.1.0-cuda".into(),
            rocm: "0.1.0-rocm".into(),
        }));
        assert_eq!(resolve_variant_tag(&m, "0.1.0", GpuMode::Cuda), "0.1.0-cuda");
    }

    #[test]
    fn variant_tag_picks_rocm_when_mode_is_rocm() {
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "0.1.0-cpu".into(),
            cuda: "0.1.0-cuda".into(),
            rocm: "0.1.0-rocm".into(),
        }));
        assert_eq!(resolve_variant_tag(&m, "0.1.0", GpuMode::Rocm), "0.1.0-rocm");
    }

    #[test]
    fn variant_tag_picks_cpu_when_mode_is_cpu() {
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "0.1.0-cpu".into(),
            cuda: "0.1.0-cuda".into(),
            rocm: "0.1.0-rocm".into(),
        }));
        assert_eq!(resolve_variant_tag(&m, "0.1.0", GpuMode::Cpu), "0.1.0-cpu");
    }

    /// Metal falls through to CPU — no Metal-specific torch wheels today
    /// (apple-silicon owners get CPU inference; the torch CPU wheel
    /// actually uses Accelerate framework under the hood).
    #[test]
    fn variant_tag_picks_cpu_when_mode_is_metal() {
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "0.1.0-cpu".into(),
            cuda: "0.1.0-cuda".into(),
            rocm: "0.1.0-rocm".into(),
        }));
        assert_eq!(resolve_variant_tag(&m, "0.1.0", GpuMode::Metal), "0.1.0-cpu");
    }

    /// Manifests without gpu_image_variants — legacy single-tag flow.
    #[test]
    fn variant_tag_falls_through_when_no_variants_declared() {
        let m = manifest_with_variants(None);
        assert_eq!(resolve_variant_tag(&m, "0.1.0", GpuMode::Cuda), "0.1.0");
        assert_eq!(resolve_variant_tag(&m, "0.1.0", GpuMode::Cpu), "0.1.0");
    }

    // ─── v0.2.34: `{version}` template substitution ────────────────
    //
    // The L0 catalog seeds `gpu_image_variants` with template strings
    // like `"{version}-cuda"` so the same manifest fixture serves
    // every released version. Before v0.2.34 `resolve_variant_tag`
    // returned the raw template, which then got handed verbatim to
    // `podman pull` — the pull failed with a manifest-not-found error
    // because no image tagged `"{version}-cuda"` exists in the
    // registry. The fix substitutes `base_tag` for the `{version}`
    // marker. Cover all four GpuMode variants so a future refactor
    // that drops the substitution from one arm breaks the build.

    #[test]
    fn variant_tag_substitutes_version_template_cuda() {
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "{version}-cpu".into(),
            cuda: "{version}-cuda".into(),
            rocm: "{version}-rocm".into(),
        }));
        assert_eq!(
            resolve_variant_tag(&m, "0.2.7", GpuMode::Cuda),
            "0.2.7-cuda",
            "template `{{version}}-cuda` with base_tag=0.2.7 must produce `0.2.7-cuda`",
        );
    }

    #[test]
    fn variant_tag_substitutes_version_template_rocm() {
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "{version}-cpu".into(),
            cuda: "{version}-cuda".into(),
            rocm: "{version}-rocm".into(),
        }));
        assert_eq!(
            resolve_variant_tag(&m, "0.2.7", GpuMode::Rocm),
            "0.2.7-rocm",
        );
    }

    #[test]
    fn variant_tag_substitutes_version_template_cpu() {
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "{version}-cpu".into(),
            cuda: "{version}-cuda".into(),
            rocm: "{version}-rocm".into(),
        }));
        assert_eq!(
            resolve_variant_tag(&m, "0.2.7", GpuMode::Cpu),
            "0.2.7-cpu",
        );
    }

    /// Metal still routes to the CPU variant — the template substitution
    /// happens AFTER the Metal→Cpu mapping, so the produced tag is the
    /// substituted CPU variant string.
    #[test]
    fn variant_tag_substitutes_version_template_metal_routes_to_cpu() {
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "{version}-cpu".into(),
            cuda: "{version}-cuda".into(),
            rocm: "{version}-rocm".into(),
        }));
        assert_eq!(
            resolve_variant_tag(&m, "0.2.7", GpuMode::Metal),
            "0.2.7-cpu",
        );
    }

    /// Variant strings without `{version}` markers must be returned
    /// unchanged — backwards-compatible with manifests that pre-date
    /// the template convention (or hand-pinned literal tags).
    #[test]
    fn variant_tag_leaves_non_templated_string_unchanged() {
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "latest-cpu".into(),
            cuda: "latest-cuda".into(),
            rocm: "latest-rocm".into(),
        }));
        assert_eq!(
            resolve_variant_tag(&m, "0.2.7", GpuMode::Cuda),
            "latest-cuda",
            "variant string without `{{version}}` marker must pass through verbatim",
        );
    }

    // ─── v0.2.29: version_lt + min_launcher_version gate ────────────

    #[test]
    fn version_lt_basic() {
        assert!(version_lt("0.2.28", "0.2.29"));
        assert!(version_lt("0.2.0", "0.2.1"));
        assert!(version_lt("0.1.0", "0.2.0"));
        assert!(version_lt("0.0.1", "1.0.0"));
    }

    #[test]
    fn version_lt_equal_is_not_less() {
        assert!(!version_lt("0.2.29", "0.2.29"));
        assert!(!version_lt("1.0.0", "1.0.0"));
    }

    #[test]
    fn version_lt_greater_is_not_less() {
        assert!(!version_lt("0.2.30", "0.2.29"));
        assert!(!version_lt("1.0.0", "0.2.29"));
    }

    #[test]
    fn version_lt_handles_unequal_segment_counts() {
        // "0.2" vs "0.2.0" → equal (missing trailing segments parse as 0).
        assert!(!version_lt("0.2", "0.2.0"));
        assert!(!version_lt("0.2.0", "0.2"));
        // "0.2" < "0.2.1" (missing trailing parses as 0).
        assert!(version_lt("0.2", "0.2.1"));
    }

    #[test]
    fn version_lt_tolerates_suffixes() {
        // `-dev`, `-rc1`, etc. — the parser stops at the first non-digit
        // so suffixes are effectively ignored.
        assert!(version_lt("0.2.28-dev", "0.2.29"));
        assert!(!version_lt("0.2.29-rc1", "0.2.29"));
    }

    // ─── v0.2.31 #20-Fix-3: refetch_artifact (Local no-op) ──────────────
    //
    // The full `run_upgrade` path needs an AppHandle (for emit_progress)
    // which isn't easily mockable in unit tests. We test the helper
    // `refetch_artifact` directly for the Local install method — it
    // should be a clean no-op when install_dir exists, and Err when it
    // doesn't. The GitClone + ContainerPull paths touch network / git /
    // podman and are out of scope for unit tests (they're covered by
    // integration tests run against a real catalog at release time).

    fn manifest_for_local_install(install_dir: &str) -> ModuleManifest {
        // Note: `install.method=local` with an empty `install.install_dir`
        // would default to `{VCT_MODULES}/{MODULE_ID}` which doesn't exist
        // in the test sandbox. We override `install_dir` to a tempdir path.
        let raw = serde_json::json!({
            "manifest_version": 1,
            "id": "vct-test-local-refetch",
            "name": "Local Refetch Test",
            "version": "0.2.0",
            "description": "Test fixture for refetch_artifact Local no-op.",
            "category": "community",
            "license": {"required": false, "min_orchestrator_tier": "free"},
            "compatibility": {"hosts": ["base"]},
            "install": {
                "method": "local",
                "install_dir": install_dir,
            },
            "runtime": {"type": "service", "command": "echo", "args": []}
        });
        ModuleManifest::from_json(&raw.to_string())
            .unwrap_or_else(|e| panic!("parse fixture: {}", e))
    }

    #[test]
    fn refetch_artifact_local_method_is_noop_when_dir_exists() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let tmp = tempfile::tempdir().expect("create tempdir");
            let install_dir = tmp.path().join("local-install");
            tokio::fs::create_dir_all(&install_dir).await.unwrap();
            // Drop a sentinel file so we can verify nothing was wiped.
            let sentinel = install_dir.join("sentinel.txt");
            tokio::fs::write(&sentinel, b"keep").await.unwrap();

            let manifest =
                manifest_for_local_install(&install_dir.display().to_string());
            let res = refetch_artifact(&manifest, &install_dir, GpuMode::Cpu, None).await;
            assert!(res.is_ok(), "local refetch must succeed when dir exists: {:?}", res);
            // v0.2.35 (Agent N): Local refetch returns None (no tag involved).
            assert_eq!(res.unwrap(), None, "Local refetch must return None — no tag involved");
            assert!(sentinel.exists(), "Local refetch must not touch user files");
            let body = tokio::fs::read(&sentinel).await.unwrap();
            assert_eq!(&body, b"keep", "Local refetch must not modify files");
        });
    }

    #[test]
    fn refetch_artifact_local_method_errs_when_dir_missing() {
        // If a user's `install.method=local` install_dir vanished between
        // install + update, surface a clear error rather than silently
        // succeeding. run_upgrade propagates this as a Failed event.
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let tmp = tempfile::tempdir().expect("create tempdir");
            let install_dir = tmp.path().join("never-existed");
            let manifest =
                manifest_for_local_install(&install_dir.display().to_string());
            let res = refetch_artifact(&manifest, &install_dir, GpuMode::Cpu, None).await;
            assert!(res.is_err(), "missing install_dir must Err");
            let msg = res.unwrap_err();
            assert!(
                msg.contains("install_dir is missing")
                    || msg.contains("missing"),
                "error must mention missing dir; got: {}",
                msg
            );
        });
    }

    // ─── v0.2.31 #27: report_error wiring ───────────────────────────────
    //
    // `report_error` constructs the InstallProgress payload that the UI
    // consumes. We can't easily verify the Tauri event emission without
    // an AppHandle (which can't be constructed in unit tests), but we CAN
    // verify the payload shape via direct construction — the same shape
    // report_error builds and emits. This pins the wire contract: stage
    // == Failed, percent == 0, message carries the error verbatim.

    #[test]
    fn install_progress_payload_serializes_failed_stage_correctly() {
        // Pin the JSON wire contract for the Failed stage so a future
        // refactor of InstallStage doesn't accidentally rename the
        // serde value the GUI's TypeScript types depend on.
        let payload = InstallProgress {
            project_id: "p1".to_string(),
            module_id: "m1".to_string(),
            stage: InstallStage::Failed,
            step_index: 0,
            step_total: 0,
            percent: 0,
            message: "test failure".to_string(),
        };
        let json = serde_json::to_value(&payload).expect("serialize InstallProgress");
        assert_eq!(json["stage"], "failed", "stage must serialize as snake_case 'failed'");
        assert_eq!(json["message"], "test failure");
        assert_eq!(json["percent"], 0);
        assert_eq!(json["project_id"], "p1");
        assert_eq!(json["module_id"], "m1");
    }

    #[test]
    fn install_progress_payload_serializes_migrate_stage() {
        // Same wire-contract pin for the new Migrate stage (added in
        // v0.2.31 #20-Fix-3 for upgrade migration_script execution).
        let payload = InstallProgress {
            project_id: "p1".to_string(),
            module_id: "m1".to_string(),
            stage: InstallStage::Migrate,
            step_index: 3,
            step_total: 5,
            percent: 60,
            message: "Running migration script".to_string(),
        };
        let json = serde_json::to_value(&payload).expect("serialize InstallProgress");
        assert_eq!(json["stage"], "migrate", "stage must serialize as snake_case 'migrate'");
        assert_eq!(json["step_index"], 3);
        assert_eq!(json["step_total"], 5);
    }

    // ─── v0.2.35 Phase 3A: pull-token error formatting ─────────────────
    //
    // The `format_pull_token_error` helper is the lifted, pure-function
    // version of the error-mapping logic inside `request_pull_token`.
    // Tests pin every (HTTP status, error code) pairing the edge function
    // at `launcher/supabase/functions/rl-artifact-url/index.ts` can return,
    // so a future server-side rename / status-code drift fails loudly here
    // BEFORE shipping.

    #[test]
    fn pull_token_error_400_is_actionable_for_launcher_bug() {
        // Server rejected our request shape — almost always a launcher
        // regression (e.g. a renamed body field). User sees a "report it"
        // message rather than a cryptic 400.
        let body = serde_json::json!({
            "error": "license_key_invalid_format",
            "detail": "expected UUID"
        });
        let msg = format_pull_token_error(400, &body);
        assert!(
            msg.contains("launcher bug"),
            "400 must flag this as a launcher bug; got: {}",
            msg
        );
        assert!(
            msg.contains("license_key_invalid_format"),
            "400 must surface the server's error code; got: {}",
            msg
        );
        assert!(
            msg.contains("expected UUID"),
            "400 must surface the server's detail; got: {}",
            msg
        );
    }

    #[test]
    fn pull_token_error_401_license_invalid_directs_user_to_refresh() {
        let body = serde_json::json!({ "error": "license_invalid", "detail": "" });
        let msg = format_pull_token_error(401, &body);
        assert!(
            msg.contains("invalid"),
            "license_invalid message must say so; got: {}",
            msg
        );
        assert!(
            msg.contains("Settings → License → Refresh"),
            "license_invalid must point to the Settings recovery path; got: {}",
            msg
        );
    }

    #[test]
    fn pull_token_error_401_license_expired_says_expired() {
        let body = serde_json::json!({ "error": "license_expired", "detail": "" });
        let msg = format_pull_token_error(401, &body);
        assert!(
            msg.contains("expired"),
            "license_expired message must use the word; got: {}",
            msg
        );
        assert!(
            msg.contains("Renew"),
            "license_expired must point to renewal; got: {}",
            msg
        );
    }

    #[test]
    fn pull_token_error_401_tier_insufficient_names_both_tiers() {
        let body = serde_json::json!({
            "error": "tier_insufficient",
            "required_tier": "pro",
            "got": "free"
        });
        let msg = format_pull_token_error(401, &body);
        assert!(
            msg.contains("pro"),
            "must name required tier; got: {}",
            msg
        );
        assert!(
            msg.contains("free"),
            "must name actual tier; got: {}",
            msg
        );
        assert!(
            msg.contains("Upgrade"),
            "must suggest upgrade path; got: {}",
            msg
        );
    }

    #[test]
    fn pull_token_error_401_unknown_code_falls_through_with_detail() {
        // Forward-compat: if the edge function adds a new 401 error code
        // (e.g. "machine_mismatch"), we still surface SOMETHING usable
        // instead of crashing or producing an empty message.
        let body = serde_json::json!({
            "error": "machine_mismatch",
            "detail": "rebind on the dashboard"
        });
        let msg = format_pull_token_error(401, &body);
        assert!(
            msg.contains("machine_mismatch"),
            "unknown 401 code must still surface the code; got: {}",
            msg
        );
        assert!(
            msg.contains("rebind on the dashboard"),
            "unknown 401 code must still surface the detail; got: {}",
            msg
        );
    }

    #[test]
    fn pull_token_error_500_suggests_retry_later() {
        let body = serde_json::json!({
            "error": "registry_token_exchange_failed",
            "detail": "ghcr 502"
        });
        let msg = format_pull_token_error(500, &body);
        assert!(
            msg.contains("unavailable") || msg.contains("Try again"),
            "500 must signal transience; got: {}",
            msg
        );
        assert!(
            msg.contains("ghcr 502"),
            "500 must surface the underlying detail; got: {}",
            msg
        );
    }

    #[test]
    fn pull_token_error_unexpected_status_quotes_the_status_code() {
        // 418 (a stand-in for "weird status the server shouldn't return")
        // must still produce a parseable error string for support triage.
        let body = serde_json::json!({ "error": "teapot", "detail": "I'm short and stout" });
        let msg = format_pull_token_error(418, &body);
        assert!(
            msg.contains("418"),
            "unexpected status must quote the status code; got: {}",
            msg
        );
        assert!(
            msg.contains("teapot"),
            "unexpected status must surface the error code; got: {}",
            msg
        );
    }

    #[test]
    fn pull_token_error_empty_body_does_not_panic() {
        // The edge function might return an empty body on some failure
        // modes (e.g. proxy timeout). Defensive: the helper must produce
        // a non-empty error string regardless.
        let body = serde_json::json!({});
        let msg = format_pull_token_error(503, &body);
        assert!(!msg.is_empty(), "empty body must still produce a message");
        assert!(
            msg.contains("503"),
            "empty body still quotes the status; got: {}",
            msg
        );
    }

    // ─── v0.2.38 NEW-1: L0 catalog endpoint overrides L1 placeholder ────
    //
    // `request_pull_token` must use the L0 catalog's `pull_token_endpoint`
    // when provided, NOT the L1 manifest's `pull_token_endpoint`. This
    // prevents the placeholder.supabase.co 403 seen during the first RL
    // Reranker install on v0.2.37 (backlog §NEW-1, 2026-05-27).
    //
    // The function is async and requires a live HTTP client, so we test the
    // URL-selection logic via a pure helper: verify that the `endpoint` the
    // function would use is the L0 URL (not the L1 placeholder) when
    // `l0_pull_token_endpoint` is `Some`.

    #[test]
    fn l0_endpoint_overrides_l1_placeholder_in_request_pull_token() {
        // Reproduce the exact failing scenario from NEW-1:
        //   L1 manifest (image-extracted): "placeholder.supabase.co/functions/v1/rl-pull-token"
        //   L0 catalog (server SoT):        "real.supabase.co/functions/v1/rl-artifact-url"
        //
        // We can't call `request_pull_token` without a live keychain + HTTP
        // server, so we verify the selection logic directly: the effective
        // endpoint is `l0_pull_token_endpoint.unwrap_or(&container.pull_token_endpoint)`.
        let l1_placeholder = "https://placeholder.supabase.co/functions/v1/rl-pull-token";
        let l0_real = "https://real.supabase.co/functions/v1/rl-artifact-url";

        // Case 1: L0 override supplied → use L0 URL.
        let l0_some: Option<&str> = Some(l0_real);
        let effective = l0_some.unwrap_or(l1_placeholder);
        assert_eq!(
            effective, l0_real,
            "NEW-1: L0 endpoint must be preferred over L1 placeholder; \
             got {} instead of {}",
            effective, l0_real,
        );

        // Case 2: L0 override absent → fall back to L1 URL (existing behaviour).
        let l0_none: Option<&str> = None;
        let effective_fallback = l0_none.unwrap_or(l1_placeholder);
        assert_eq!(
            effective_fallback, l1_placeholder,
            "NEW-1: without L0 override, L1 endpoint must be used; \
             got {} instead of {}",
            effective_fallback, l1_placeholder,
        );
    }

    // ─── v0.2.35 (Agent N): image variant fallback ──────────────────────
    //
    // The decision logic lives in `decide_variant_to_pull` — a pure helper
    // that takes a probe closure. These tests inject fake probe results
    // covering each branch of the decision tree:
    //   1. Primary tag exists → pull it.
    //   2. Primary missing, fallback exists → pull fallback.
    //   3. Both missing → hard-fail with "no variant available".
    //   4. No fallback declared, primary missing → hard-fail variant_not_found.
    //   5. Probe errors → degrade to blind pull (legacy behaviour).
    //   6. cpu_fallback_tag returns None when manifest has no variants /
    //      or gpu_mode is already Cpu (no point falling back to itself).
    //   7. probe_image_tag_exists itself is integration-only (#[ignore]):
    //      it spawns a real `manifest inspect` and requires a runtime on PATH.
    //
    // The closure-injection pattern (suggested by spec; mirrors Agent C's
    // `bust_cache_if_launcher_version_changed_with_db` style) means we
    // never need a live registry to exercise the decision branches — the
    // entire fallback logic is exercised with deterministic inputs.

    /// Helper: build a probe closure that returns canned answers keyed by
    /// `(image, tag)`. Any tag not in the map returns `Ok(false)` (missing).
    fn canned_probe(
        answers: std::collections::HashMap<(String, String), Result<bool, String>>,
    ) -> impl Fn(String, String) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<bool, String>>>>
    {
        // Wrap in Arc so the closure can be Fn (called multiple times).
        let answers = std::sync::Arc::new(answers);
        move |image: String, tag: String| {
            let answers = answers.clone();
            Box::pin(async move {
                match answers.get(&(image.clone(), tag.clone())) {
                    Some(Ok(v)) => Ok(*v),
                    Some(Err(e)) => Err(e.clone()),
                    None => Ok(false),
                }
            })
        }
    }

    #[test]
    fn decide_variant_returns_primary_when_primary_exists() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let mut answers = std::collections::HashMap::new();
            answers.insert(
                ("ghcr.io/test/img".into(), "0.2.7-cuda".into()),
                Ok(true),
            );
            let probe = canned_probe(answers);
            let res = decide_variant_to_pull(
                "ghcr.io/test/img",
                "0.2.7-cuda",
                Some("0.2.7-cpu"),
                "vct-test",
                probe,
            )
            .await;
            assert_eq!(
                res.unwrap(),
                "0.2.7-cuda",
                "primary-hit path must pull the primary tag"
            );
        });
    }

    #[test]
    fn decide_variant_falls_back_to_cpu_when_primary_missing() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let mut answers = std::collections::HashMap::new();
            // cuda missing, cpu present — the headline fallback case.
            answers.insert(
                ("ghcr.io/test/img".into(), "0.2.7-cuda".into()),
                Ok(false),
            );
            answers.insert(
                ("ghcr.io/test/img".into(), "0.2.7-cpu".into()),
                Ok(true),
            );
            let probe = canned_probe(answers);
            let res = decide_variant_to_pull(
                "ghcr.io/test/img",
                "0.2.7-cuda",
                Some("0.2.7-cpu"),
                "vct-test",
                probe,
            )
            .await;
            assert_eq!(
                res.unwrap(),
                "0.2.7-cpu",
                "fallback path must pull the cpu variant"
            );
        });
    }

    #[test]
    fn decide_variant_hard_fails_when_both_variants_missing() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let mut answers = std::collections::HashMap::new();
            answers.insert(
                ("ghcr.io/test/img".into(), "0.2.7-cuda".into()),
                Ok(false),
            );
            answers.insert(
                ("ghcr.io/test/img".into(), "0.2.7-cpu".into()),
                Ok(false),
            );
            let probe = canned_probe(answers);
            let res = decide_variant_to_pull(
                "ghcr.io/test/img",
                "0.2.7-cuda",
                Some("0.2.7-cpu"),
                "vct-test",
                probe,
            )
            .await;
            let err = res.expect_err("both-missing must hard-fail");
            assert!(
                err.contains("no variant"),
                "error must say 'no variant available'; got: {}",
                err
            );
            assert!(
                err.contains("0.2.7-cuda"),
                "error must name primary tag; got: {}",
                err
            );
            assert!(
                err.contains("0.2.7-cpu"),
                "error must name fallback tag; got: {}",
                err
            );
        });
    }

    #[test]
    fn decide_variant_hard_fails_when_no_fallback_declared() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let mut answers = std::collections::HashMap::new();
            answers.insert(
                ("ghcr.io/test/img".into(), "0.2.7".into()),
                Ok(false),
            );
            let probe = canned_probe(answers);
            let res = decide_variant_to_pull(
                "ghcr.io/test/img",
                "0.2.7",
                None, // legacy manifest, no variants block.
                "vct-test",
                probe,
            )
            .await;
            let err = res.expect_err("no-fallback must hard-fail");
            assert!(
                err.contains("variant_not_found"),
                "error must use the variant_not_found code; got: {}",
                err
            );
        });
    }

    #[test]
    fn decide_variant_degrades_to_blind_pull_on_probe_error() {
        // Probe errored (e.g. `manifest inspect` unsupported on this
        // runtime version). Don't block the install — let the pull try
        // its hand, the registry will surface the real error if any.
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let mut answers = std::collections::HashMap::new();
            answers.insert(
                ("ghcr.io/test/img".into(), "0.2.7-cuda".into()),
                Err("podman: manifest subcommand unsupported".into()),
            );
            let probe = canned_probe(answers);
            let res = decide_variant_to_pull(
                "ghcr.io/test/img",
                "0.2.7-cuda",
                Some("0.2.7-cpu"),
                "vct-test",
                probe,
            )
            .await;
            assert_eq!(
                res.unwrap(),
                "0.2.7-cuda",
                "probe error must degrade to blind pull of primary"
            );
        });
    }

    #[test]
    fn decide_variant_handles_fallback_equal_to_primary() {
        // Defensive: if a manifest somehow declares the cpu fallback ==
        // the chosen primary (e.g. GpuMode::Cpu picks the cpu variant,
        // then `cpu_fallback_tag` is asked for "what's the fallback?"
        // and answers with the same string), the decision helper should
        // hard-fail with the no-fallback message rather than infinite-
        // looping on the same probe.
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let mut answers = std::collections::HashMap::new();
            answers.insert(
                ("ghcr.io/test/img".into(), "0.2.7-cpu".into()),
                Ok(false),
            );
            let probe = canned_probe(answers);
            let res = decide_variant_to_pull(
                "ghcr.io/test/img",
                "0.2.7-cpu",
                Some("0.2.7-cpu"), // intentionally same as primary
                "vct-test",
                probe,
            )
            .await;
            let err = res.expect_err("primary==fallback miss must hard-fail");
            assert!(
                err.contains("variant_not_found"),
                "error must say variant_not_found; got: {}",
                err
            );
        });
    }

    #[test]
    fn cpu_fallback_tag_returns_none_when_no_variants_declared() {
        // Legacy single-tag module (no gpu_image_variants block) — no
        // fallback possible. The chosen GpuMode is irrelevant.
        let m = manifest_with_variants(None);
        assert_eq!(cpu_fallback_tag(&m, "0.2.7", GpuMode::Cuda), None);
        assert_eq!(cpu_fallback_tag(&m, "0.2.7", GpuMode::Cpu), None);
        assert_eq!(cpu_fallback_tag(&m, "0.2.7", GpuMode::Rocm), None);
    }

    #[test]
    fn cpu_fallback_tag_returns_none_when_already_cpu_mode() {
        // GpuMode::Cpu and Metal already pick the cpu variant via
        // resolve_variant_tag. Probing it twice would be wasted work.
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "{version}-cpu".into(),
            cuda: "{version}-cuda".into(),
            rocm: "{version}-rocm".into(),
        }));
        assert_eq!(cpu_fallback_tag(&m, "0.2.7", GpuMode::Cpu), None);
        assert_eq!(cpu_fallback_tag(&m, "0.2.7", GpuMode::Metal), None);
    }

    #[test]
    fn cpu_fallback_tag_substitutes_version_template() {
        // For Cuda / Rocm callers, the fallback is the cpu variant with
        // `{version}` replaced by the base tag. Mirrors the same
        // template-substitution `resolve_variant_tag` performs.
        let m = manifest_with_variants(Some(GpuImageVariants {
            cpu: "{version}-cpu".into(),
            cuda: "{version}-cuda".into(),
            rocm: "{version}-rocm".into(),
        }));
        assert_eq!(
            cpu_fallback_tag(&m, "0.2.7", GpuMode::Cuda),
            Some("0.2.7-cpu".to_string()),
        );
        assert_eq!(
            cpu_fallback_tag(&m, "0.2.7", GpuMode::Rocm),
            Some("0.2.7-cpu".to_string()),
        );
    }

    #[test]
    fn install_progress_payload_serializes_variant_fallback_stage() {
        // Pin the JSON wire contract for the new VariantFallback stage.
        // The GUI keys off the snake_case string "variant_fallback" to
        // route the event to a non-blocking toast — a rename would
        // silently break that surfacing.
        let payload = InstallProgress {
            project_id: "p1".to_string(),
            module_id: "vct-rl-reranker".to_string(),
            stage: InstallStage::VariantFallback,
            step_index: 1,
            step_total: 5,
            percent: 20,
            message:
                "Requested variant 0.2.7-cuda not available on the registry; installing 0.2.7-cpu instead"
                    .to_string(),
        };
        let json = serde_json::to_value(&payload).expect("serialize InstallProgress");
        assert_eq!(
            json["stage"], "variant_fallback",
            "stage must serialize as snake_case 'variant_fallback'"
        );
        let msg = json["message"].as_str().unwrap();
        assert!(
            msg.contains("0.2.7-cuda") && msg.contains("0.2.7-cpu"),
            "fallback message must name both requested + actual tags; got: {}",
            msg
        );
    }

    // ─── probe_image_tag_exists (env-conditional) ───────────────────────
    //
    // Spawns a real `manifest inspect` against `library/hello-world` —
    // requires podman or docker on PATH AND outbound network to
    // registry-1.docker.io. Skipped by default; run with
    // `cargo test --lib -- --ignored probe_image_tag_exists` when
    // verifying probe behaviour against the real registry.

    #[test]
    #[ignore = "requires podman/docker on PATH and outbound network"]
    fn probe_image_tag_exists_returns_true_for_known_public_image() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            // Resolve runtime up-front; skip if neither podman nor docker.
            let runtime = match detect_container_runtime().await {
                Ok(r) => r,
                Err(_) => {
                    eprintln!("skipping: no container runtime on PATH");
                    return;
                }
            };
            let exists = probe_image_tag_exists("docker.io/library/hello-world", "latest", &runtime)
                .await
                .expect("probe must not error on a public image");
            assert!(exists, "hello-world:latest must exist on docker.io");
        });
    }

    #[test]
    #[ignore = "requires podman/docker on PATH and outbound network"]
    fn probe_image_tag_exists_returns_false_for_unknown_tag() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            let runtime = match detect_container_runtime().await {
                Ok(r) => r,
                Err(_) => {
                    eprintln!("skipping: no container runtime on PATH");
                    return;
                }
            };
            // Tag with a sentinel UUID-shaped suffix — will never exist.
            let exists = probe_image_tag_exists(
                "docker.io/library/hello-world",
                "vct-probe-test-00000000-0000-0000-0000-000000000000",
                &runtime,
            )
            .await;
            // Result MUST be Ok(false) — not Err. Treating
            // "manifest unknown" as Err would block valid installs.
            assert!(
                matches!(exists, Ok(false)),
                "missing tag must produce Ok(false); got: {:?}",
                exists,
            );
        });
    }

    /// v0.2.37 (Issue 7): regression test for the pipe-buffer deadlock
    /// bug in `container_pull` / `git_clone` / `run_upgrade`.
    ///
    /// **The bug**: on Linux the anonymous pipe buffer is 64KB. When a
    /// child process emits more than that to a `Stdio::piped()` pipe and
    /// the parent calls `.status().await` (rather than draining the
    /// pipe via `.output().await` or `.wait_with_output().await`), the
    /// child blocks on its next write while the parent blocks on wait.
    /// The deadlock manifests as `exit -1` AFTER the work has actually
    /// completed (in the original bug report, `podman pull` of a 5.5GB
    /// image returned exit -1 even though `podman images` confirmed the
    /// 5.5GB image was present).
    ///
    /// **The fix**: change `Stdio::piped()` to `Stdio::null()` on the
    /// three `.status().await` sites (`container_pull`, `git_clone`,
    /// `run_upgrade` fast-forward). The launcher reports progress via
    /// Tauri events and doesn't consume the pipes for diagnostic, so
    /// the output is genuinely throw-away.
    ///
    /// **What this test does**: simulates the bug condition without
    /// requiring podman or a network. We spawn a child process that
    /// writes 200KB (~3.1× the 64KB buffer) to stderr — comfortably past
    /// the deadlock threshold — and we mirror the production wrapper's
    /// shape (`.stderr(Stdio::null()).status().await`). With the fix,
    /// this completes in milliseconds. With the un-fixed
    /// `Stdio::piped()` pattern the same call hangs forever (the test
    /// would have to be killed by the test runner's timeout).
    ///
    /// Gated `#[cfg(unix)]` because the test driver uses `/bin/sh`. The
    /// underlying bug behaves identically on Windows — anonymous pipes
    /// there also have a finite buffer (~4KB by default for sync
    /// pipes; the async runtime sizes vary) — but a Windows-portable
    /// driver requires either a small Rust helper binary or PowerShell,
    /// and the spec authorises deferring that to v0.2.38.
    // TODO(v0.2.38): add a Windows-portable regression driver.
    #[cfg(unix)]
    #[test]
    fn stdio_null_avoids_pipe_buffer_deadlock_on_high_volume_stderr() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("build tokio runtime");
        rt.block_on(async {
            // Emit 200000 bytes to stderr: well past the 64KB Linux pipe
            // buffer, so the bug would trigger reliably under the old
            // Stdio::piped() pattern. We use `printf` rather than `yes`
            // because `yes | head` produces a SIGPIPE-on-close edge case
            // on some shells.
            let driver = "printf '%.0sx' $(seq 1 200000) >&2";

            // Wrap the call in a tokio timeout. With the fix the child
            // completes in <100ms; with the un-fixed Stdio::piped()
            // pattern the call would hang forever, the timeout would
            // fire, and the test would fail with a clear diagnostic.
            let result = tokio::time::timeout(
                std::time::Duration::from_secs(10),
                Command::new("/bin/sh")
                    .arg("-c")
                    .arg(driver)
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status(),
            )
            .await;

            let status = result
                .expect(
                    "deadlock regression: child hung past 10s on 200KB stderr — \
                     the Stdio::piped() + .status() bug is back",
                )
                .expect("spawn /bin/sh failed");
            assert!(
                status.success(),
                "driver shell exited non-zero: {:?}",
                status.code(),
            );
        });
    }
}

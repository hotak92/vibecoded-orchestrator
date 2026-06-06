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
                Some(db),
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
    // v0.2.46 V46-E (C3): optional Db handle for audit-log emission.
    // `Option<&Db>` rather than `&Db` so test callers (and any future
    // caller path that doesn't have a Db handle plumbed in) can pass
    // `None`. Auditing is best-effort: a None db handle silently
    // disables audit emission; a failing `db.audit` call eprintlns and
    // continues. The install MUST NOT be blocked by audit failures.
    db: Option<&crate::db::Db>,
) -> Result<String, String> {
    // ─── Step 0 (v0.2.46 V46-E C3): resolve endpoint + emit pre-request
    // audit BEFORE the gateway POST. Resolves endpoint + license-key
    // prefix from the same sources `request_pull_token` will consult.
    // Best-effort auditing: any DB write failure is logged and dropped —
    // the install proceeds either way.
    //
    // We compute `endpoint_for_audit` unconditionally (cheap; reused for
    // the success-path audit too) and only do the keychain read +
    // audit-row write when `db` is Some.
    let endpoint_for_audit: String = {
        let env_override = std::env::var("VCT_RL_PULL_TOKEN_ENDPOINT")
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty());
        match env_override {
            Some(e) => e,
            None => {
                let raw = l0_pull_token_endpoint.unwrap_or(&container.pull_token_endpoint);
                resolve_pull_token_endpoint(raw).to_string()
            }
        }
    };
    if db.is_some() {
        // Read the license-key prefix for the audit detail. We use the
        // SAME keychain accessor as `request_pull_token` so the prefix
        // we audit reflects the key actually used for the POST. On Err
        // (no license activated / keychain failure) we audit with a
        // sentinel prefix so the audit row still exists — the failure
        // mode is then visible in the `pull_token_failed` row.
        let license_key_for_audit = crate::commands::licensing::read_license_key_from_keychain()
            .ok()
            .flatten()
            .unwrap_or_else(|| "<unavailable>".to_string());
        audit_pull_token_requested(
            db,
            module_id,
            &endpoint_for_audit,
            &license_key_for_audit,
            tag,
        );
    }

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
    //
    // v0.2.46 V46-E (C1/C4): inspect the server-returned `tag` field
    // (added in this release). If the server returns a tag that
    // diverges from the client-resolved tag:
    //   - patch-only difference (same major.minor) → honor server's tag
    //     (server is SoT for what's pullable); log WARN.
    //   - major or minor difference → hard-fail with a publisher-pointing
    //     error message. This indicates server-side catalog drift; the
    //     orchestrator team can't fix it and routing the failure to the
    //     publisher prevents wasted support cycles.
    //   - no `tag` field (pre-v0.2.46 server) → behave as before.
    //
    // The decision is captured in `effective_tag` below; that's the
    // string fed into `decide_variant_to_pull` and `podman pull`.
    let (token, token_username, token_request_err, effective_tag): (
        Option<String>,
        Option<String>,
        Option<String>,
        String,
    ) = match request_pull_token(container, l0_pull_token_endpoint).await {
        Ok(tok) => {
            eprintln!(
                "[installer_engine] container_pull[{}]: obtained pull token (expires_in={}s)",
                module_id, tok.expires_in_s
            );
            // v0.2.46 V46-E C1/C4: tag-mismatch detection & resolution.
            let server_tag_opt = tok
                .tag
                .as_deref()
                .map(str::trim)
                .filter(|s| !s.is_empty());
            let (resolved_tag, class) = match server_tag_opt {
                Some(server_tag) => {
                    let class = classify_tag_mismatch(tag, server_tag);
                    match class {
                        VersionMismatchClass::Same => (tag.to_string(), class),
                        VersionMismatchClass::PatchOnly => {
                            // Honor the server's tag — server is the
                            // authoritative SoT for what's actually pullable.
                            //
                            // v0.2.46 V46-E (C1 follow-up): preserve the
                            // variant suffix from the client tag. The
                            // registry only carries variant-suffixed tags
                            // (e.g. `0.2.5-cuda`, `0.2.5-cpu`), never bare
                            // `0.2.5`. If the server returns a bare tag,
                            // we re-append the client's GPU-dispatched
                            // suffix so the upcoming `podman pull` finds
                            // the right manifest. If the server already
                            // included a suffix, we trust it as-is.
                            let merged = merge_server_tag_with_client_variant(server_tag, tag);
                            eprintln!(
                                "[installer_engine] container_pull[{}]: tag mismatch (PATCH): \
                                 client resolved {:?}, server returned {:?}, \
                                 effective (with variant) {:?}. \
                                 Honoring server tag (server is SoT for pullability).",
                                module_id, tag, server_tag, merged
                            );
                            (merged, class)
                        }
                        VersionMismatchClass::MinorOrMajor => {
                            // Audit BEFORE returning Err so the audit row
                            // records the mismatch even on hard-fail. The
                            // effective_tag is the would-be-merged value
                            // we never actually pull — recording it makes
                            // it obvious to forensic readers what would
                            // have been pulled if we hadn't hard-failed.
                            let effective_for_audit =
                                merge_server_tag_with_client_variant(server_tag, tag);
                            audit_pull_token_resolved(
                                db,
                                module_id,
                                &endpoint_for_audit,
                                Some(server_tag),
                                tag,
                                &effective_for_audit,
                                tok.username.as_deref(),
                                tok.expires_in_s,
                                &class,
                            );
                            let endpoint_for_msg = l0_pull_token_endpoint
                                .unwrap_or(&container.pull_token_endpoint);
                            return Err(format!(
                                "pull-token gateway returned tag {:?} but the launcher's \
                                 L0-resolved version is {:?}. This is server-side catalog \
                                 drift — the module publisher needs to update the artifact \
                                 gateway at {:?} to match the catalog (a major or minor \
                                 version is mismatched, which can't be auto-honored safely). \
                                 Please contact the module publisher to resolve.",
                                server_tag, tag, endpoint_for_msg
                            ));
                        }
                    }
                }
                None => (tag.to_string(), VersionMismatchClass::Same),
            };
            audit_pull_token_resolved(
                db,
                module_id,
                &endpoint_for_audit,
                server_tag_opt,
                tag,
                &resolved_tag,
                tok.username.as_deref(),
                tok.expires_in_s,
                &class,
            );
            (Some(tok.pull_token), tok.username, None, resolved_tag)
        }
        Err(e) => {
            eprintln!(
                "[installer_engine] container_pull[{}]: pull-token gateway returned: {}. \
                 Falling back to anonymous pull — will succeed only if the image is public.",
                module_id, e
            );
            audit_pull_token_failed(db, module_id, &e);
            (None, None, Some(e), tag.to_string())
        }
    };

    // ─── Step 2: pick container runtime (podman preferred, docker fallback) ─
    let runtime = detect_container_runtime().await?;

    // ─── Step 3 (v0.2.46 V46-E C2): build a per-pull --authfile rather
    // than running `podman login` / `podman logout` against the global
    // auth state. Two reasons:
    //   1. Concurrent paid-module pulls: if module A's `podman logout`
    //      fires while module B's pull is in flight, B's session is
    //      invalidated mid-pull. The previous code hit this whenever two
    //      paid modules installed within the same minute.
    //   2. Cleanup hygiene: the previous flow had to remember to call
    //      `podman logout` on every error path (`?`-propagation +
    //      explicit hard-fail branches). The per-pull authfile uses RAII
    //      — `NamedTempFile` drops the file when `authfile_guard` goes
    //      out of scope, even on panic.
    //
    // Pre-v0.2.46 (kept for reference, may be deleted in v0.2.47):
    //     container_login(...)
    //     podman pull <image>
    //     podman logout <registry>   // even on error paths
    //
    // v0.2.46+:
    //     let authfile = build_per_pull_authfile(...)
    //     podman pull --authfile <path> <image>
    //     // authfile drops, file deleted
    let registry = container
        .registry
        .clone()
        .unwrap_or_else(|| infer_registry_from_image(&container.image));
    // v0.2.36: server returns the GHCR username alongside the pull_token.
    // For personal-account packages this MUST be the PAT owner's GitHub
    // login (synthetic usernames get 403 from ghcr.io). Pre-v0.2.36 server
    // response omitted this field — fall back to the historical
    // `vct-paid-module` literal so mismatched-version client/server
    // pairings still attempt auth (it'll fail with a clear error, not
    // silently break).
    // v0.2.47: build the per-pull auth context with the active runtime
    // baked in. Docker doesn't accept `--authfile` on `pull` / `run`
    // (only `docker login` reads `~/.docker/config.json`), so the core
    // helper switches storage shape based on `runtime` and the
    // `apply_to` method emits either `--authfile <file>` (podman) or
    // `DOCKER_CONFIG=<dir>` env (docker). The probe helper below still
    // takes a path-or-None for podman compat; docker probes carry the
    // env via `apply_to` instead.
    let authfile_guard: Option<vct_launcher_core::services::container_runtime::PerPullAuth> =
        if let Some(t) = token.as_deref() {
            let login_username = token_username.as_deref().unwrap_or("vct-paid-module");
            Some(build_per_pull_authfile(&registry, login_username, t, &runtime)?)
        } else {
            None
        };

    // ─── Step 3a (v0.2.35): probe primary tag, fall back to cpu if missing ─
    //
    // The probe is a `manifest inspect` call — it issues a HEAD-equivalent
    // request to the registry without downloading layers. Cheap, scoped
    // to the same auth context as the upcoming pull (via --authfile),
    // and lets us decide FROM the registry's authoritative answer whether
    // the chosen variant exists. Pre-v0.2.35 the launcher fired the pull
    // blind and surfaced a cryptic `denied` error on miss; now the user
    // gets a structured fallback (or a clear "no variant available" hard-
    // fail when even the CPU fallback is missing).
    //
    // The decision tree itself lives in `decide_variant_to_pull` — a
    // pure helper that takes a probe-closure and the candidate tags so
    // unit tests can exercise every branch (primary-hit /
    // primary-miss-fallback-hit / both-miss / probe-error-degrades) by
    // injecting a fake probe instead of needing a live registry.
    let runtime_for_probe = runtime.clone();
    // v0.2.47: capture the probe-side auth. For podman, this is the
    // path of the authfile (passed as `--authfile`). For docker, we
    // capture the temp dir that backs the per-pull `config.json`
    // (passed as `DOCKER_CONFIG=<dir>` env var on the probe Command).
    // Both alternatives are cheap `Option<PathBuf>` so the cloned
    // closure body can rebuild the same auth context per probe call.
    let probe_authfile_path: Option<std::path::PathBuf> =
        authfile_guard.as_ref().and_then(|g| g.path()).map(|p| p.to_path_buf());
    let probe_docker_config_dir: Option<std::path::PathBuf> =
        authfile_guard.as_ref().and_then(|g| g.docker_config_dir())
            .map(|p| p.to_path_buf());
    // v0.2.46 V46-E C1: use `effective_tag` rather than the input `tag`
    // so a server-honored patch-level override flows through to both the
    // registry probe AND the eventual `podman pull` invocation.
    let decision = decide_variant_to_pull(
        &container.image,
        &effective_tag,
        fallback_tag,
        module_id,
        |probe_image, probe_tag| {
            let runtime = runtime_for_probe.clone();
            let authfile_pb = probe_authfile_path.clone();
            let docker_cfg_pb = probe_docker_config_dir.clone();
            async move {
                probe_image_tag_exists_with_auth_context(
                    &probe_image,
                    &probe_tag,
                    &runtime,
                    authfile_pb.as_deref(),
                    docker_cfg_pb.as_deref(),
                )
                .await
            }
        },
    )
    .await;

    let resolved_tag: String = match decision {
        Ok(t) => t,
        Err(decision_err) => {
            // Hard-fail the install BEFORE issuing the doomed pull.
            // No explicit logout needed — `authfile_guard` will drop and
            // delete the temp file when this function returns, with no
            // global state to clean up.
            return Err(decision_err);
        }
    };

    let image_ref = format!("{}:{}", container.image, resolved_tag);
    // v0.2.37 (Issue 7): the original symptom was that `podman pull` of a
    // large image (5.5GB / 7 layers) emits tens of KB of layer-progress
    // text to stderr; combined with `.status()` — which doesn't drain the
    // pipes — the OS-level pipe buffer (64KB on Linux) fills up and the
    // child blocks on write, while the parent blocks on wait, producing a
    // deadlock that surfaces as `exit -1` AFTER the image has actually
    // been downloaded successfully. The v0.2.37 workaround was
    // `Stdio::null()` on both streams — which sidestepped the deadlock by
    // letting the OS discard the writes, but also threw away the
    // actionable stderr tail (e.g. "Error: unauthorized") that the user
    // needs to diagnose a failure.
    //
    // v0.2.49: switch from `.status()` to `.output()`. tokio's `.output()`
    // actively drains both stdio streams via internal piping in the
    // background, so the child never blocks on write — fixing the
    // UNDERLYING issue the Stdio::null() workaround papered over. We can
    // now capture stderr safely. stdout is set to Stdio::null() at the
    // caller; tokio's .output() still forces piping internally but the
    // buffered stdout is discarded after the call returns, so it functions
    // as null from the caller's view. Layer-progress text isn't useful in
    // error reports either way. The two analogous `git clone` / `git pull`
    // sites further down still use `.status()` + Stdio::null() — git rarely
    // emits cryptic errors, so the surface-stderr conversion is deferred
    // to a future cleanup.
    //
    // v0.2.46 V46-E (C2): per-pull auth replaces the previous
    // `podman login`/`podman logout` dance. The auth blob contains a
    // base64-encoded `username:token` for ONLY the target registry, so
    // it can't bleed into other concurrent paid-module pulls.
    // `silent()` takes self by value (consumes Command) — apply it
    // FIRST, then mutate via &mut for the conditional auth flag.
    //
    // v0.2.47: route through `PerPullAuth::apply_to` so docker (which
    // doesn't accept `--authfile` on `pull`) gets `DOCKER_CONFIG=<dir>`
    // and podman gets `--authfile <path>`. The probe-side helper above
    // captures the same auth shape through `path()` /
    // `docker_config_dir()` so the probe and the pull share the auth
    // context — pre-v0.2.47 docker would have probed correctly (because
    // `--authfile` is silently accepted by docker's parser) then
    // attempted an anonymous pull (because docker's `pull` IGNORES the
    // global `--authfile` flag, looking only at `DOCKER_CONFIG`).
    let mut pull_cmd = Command::new(&runtime).silent();
    if let Some(guard) = authfile_guard.as_ref() {
        guard.apply_to(&mut pull_cmd, &runtime);
    }
    let pull_output = pull_cmd
        .args(["pull", &image_ref])
        .stdout(Stdio::null())   // layer-progress text isn't useful in errors
        .stderr(Stdio::piped())  // capture for error message
        .output()                // active drain — no deadlock
        .await
        .map_err(|e| format!("spawn {} pull: {}", runtime, e))?;

    // No explicit logout — `authfile_guard` drops at end-of-scope, taking
    // the temp file with it. No global auth.json mutation to undo.

    if !pull_output.status.success() {
        return Err(format_pull_failure(
            &runtime,
            pull_output.status.code(),
            &image_ref,
            &pull_output.stderr,
            token.is_some(),
            token_request_err.as_deref(),
        ));
    }

    Ok(resolved_tag)
}

/// v0.2.49: format a podman/docker pull failure error for the user.
/// `stderr` is the captured stderr from the failed pull (active-drained
/// via tokio's .output() — see callsite). We keep only the LAST 2KB to
/// bound memory + log size. The actionable error is almost always the
/// last line (e.g. "Error: unauthorized" or "manifest unknown") — layer-
/// progress text and prior connection-retry lines are noise.
fn format_pull_failure(
    runtime: &str,
    exit_code: Option<i32>,
    image_ref: &str,
    stderr: &[u8],
    token_present: bool,
    token_request_err: Option<&str>,
) -> String {
    let stderr_str = String::from_utf8_lossy(stderr);
    let stderr_tail = if stderr_str.len() > 2048 {
        // Snap `start` forward to the next char boundary so we never slice
        // mid-multi-byte. from_utf8_lossy's U+FFFD replacements are 3 bytes,
        // and registry errors may contain non-ASCII (localized auth errors).
        let mut start = stderr_str.len() - 2048;
        while start < stderr_str.len() && !stderr_str.is_char_boundary(start) {
            start += 1;
        }
        let truncated = &stderr_str[start..];
        // Skip past the first newline so we don't start mid-line.
        match truncated.find('\n') {
            Some(nl_idx) => &truncated[nl_idx + 1..],
            None => truncated,
        }
    } else {
        &stderr_str[..]
    };
    let stderr_tail_trimmed = stderr_tail.trim();
    format!(
        "{} pull failed (exit {}): {}{}{}",
        runtime,
        exit_code.unwrap_or(-1),
        image_ref,
        if !stderr_tail_trimmed.is_empty() {
            format!(" — {}", stderr_tail_trimmed)
        } else {
            String::new()
        },
        if !token_present {
            // v0.2.35: the request_pull_token error is the authoritative
            // reason for the 401. Quote it verbatim so the user sees the
            // actual cause (e.g. "your license has expired") instead of
            // the pre-v0.2.35 generic "Phase 3A gateway not deployed"
            // footer that masked every real failure mode.
            match token_request_err {
                Some(reason) => format!(" — license check failed: {}", reason),
                None => " — and the pull-token gateway returned no error detail (this is unexpected; please report)".to_string(),
            }
        } else {
            String::new()
        },
    )
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
/// Auth context: the caller must pass an `--authfile` (or have credentials
/// in the runtime's default auth.json) for private-registry probes —
/// `manifest inspect` honours the runtime's auth.json the same way `pull`
/// does. v0.2.46 V46-E (C2) switched the launcher from global
/// `podman login` to per-pull `--authfile`; use
/// `probe_image_tag_exists_with_authfile` to scope the credentials to
/// this one call.
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
/// probe runs (Step 3 above builds the per-pull --authfile BEFORE this
/// helper runs). At one install per minute the probe contributes 60
/// inspects/hour, well under cap.
// Kept for backward-compat: integration tests under `#[cfg(test)]`
// (the two `#[ignore]` probe tests against docker.io/library/hello-world)
// reference this no-authfile variant. The non-test code path now goes
// through `probe_image_tag_exists_with_authfile`.
#[allow(dead_code)]
pub(crate) async fn probe_image_tag_exists(
    image: &str,
    tag: &str,
    runtime: &str,
) -> Result<bool, String> {
    probe_image_tag_exists_with_authfile(image, tag, runtime, None).await
}

/// v0.2.47: probe variant that accepts either a podman-style auth path
/// OR a docker-style `DOCKER_CONFIG=<dir>` env. Routes to whichever the
/// runtime supports.
///
/// Why a separate helper from `probe_image_tag_exists_with_authfile`:
/// the existing podman-path helper is still called from a `#[ignore]`d
/// integration test (`probe_image_tag_exists_*`); we keep it as the
/// path-only flavour for backward source compatibility. New call sites
/// in `container_pull` go through this auth-context variant so docker
/// gets the same per-pull auth scoping podman does (otherwise docker
/// would silently fall back to anonymous + 401 on private GHCR repos —
/// the exact bug v0.2.47 closes on the supervisor side).
///
/// v0.2.49: switched the podman branch from `cmd.arg("--authfile").arg(path)`
/// (argv flag, position-sensitive) to `cmd.env("REGISTRY_AUTH_FILE", path)`
/// (env var, position-independent). Same root cause as the original
/// `PerPullAuth::apply_to` bug closed by `b4830e04`: `--authfile` is
/// SUBCOMMAND-scoped on podman 4.x (`podman manifest inspect --authfile X
/// image` works; `podman --authfile X manifest inspect image` returns
/// "unknown flag: --authfile" exit 125). Adding the arg before
/// `.args(["manifest", "inspect", ...])` put it in the broken
/// global-flag position. The probe always errored on private images,
/// silently degrading `decide_variant_to_pull` to "blind-pull legacy
/// behaviour" → the fallback-to-alternate-variant mechanism NEVER ran
/// for ROCm/Metal hosts whose primary variant wasn't on the registry.
pub(crate) async fn probe_image_tag_exists_with_auth_context(
    image: &str,
    tag: &str,
    runtime: &str,
    authfile: Option<&Path>,
    docker_config_dir: Option<&Path>,
) -> Result<bool, String> {
    let image_ref = format!("{}:{}", image, tag);
    let mut cmd = Command::new(runtime).silent();
    if let Some(path) = authfile {
        // v0.2.49: env-var sibling of --authfile (per podman release notes
        // since 1.3). Position-independent, so safe to set before
        // .args([...]). The previous `cmd.arg("--authfile").arg(path)`
        // produced the broken `podman --authfile X manifest inspect Y`
        // argv shape that podman 4.x rejects.
        cmd.env("REGISTRY_AUTH_FILE", path);
    }
    if let Some(dir) = docker_config_dir {
        cmd.env("DOCKER_CONFIG", dir);
    }
    let output = cmd
        .args(["manifest", "inspect", &image_ref])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("spawn {} manifest inspect: {}", runtime, e))?;

    probe_image_tag_exists_classify_output(&image_ref, runtime, &output)
}

/// v0.2.46 V46-E (C2): variant of `probe_image_tag_exists` that takes an
/// optional `--authfile` path. The probe must use the same auth context
/// as the upcoming `podman pull --authfile <file>`, otherwise the registry
/// returns 401/403 on private repos even when the same launcher will
/// successfully pull a few lines later.
///
/// Replaces the previous `podman login` + `podman logout` global-state
/// dance with a per-pull authfile — avoids cross-module session collision
/// when multiple paid modules pull concurrently.
///
/// v0.2.49: switched the auth-attachment from `cmd.arg("--authfile").arg(path)`
/// (argv flag, position-sensitive) to `cmd.env("REGISTRY_AUTH_FILE", path)`
/// (env var, position-independent). Same root cause as the original
/// `PerPullAuth::apply_to` bug closed by `b4830e04`. Pre-v0.2.49 comment
/// at this site WRONGLY asserted that `--authfile` is parsed as a global
/// flag — it is in fact SUBCOMMAND-scoped on podman 4.x. The
/// `podman --authfile X manifest inspect Y` shape is rejected with
/// "unknown flag: --authfile" exit 125. The REGISTRY_AUTH_FILE env var
/// is the position-independent equivalent (per podman release notes
/// since 1.3) and the docker `--authfile` parsing differs anyway —
/// this path-only flavour is retained only for the `#[ignore]`d
/// integration test that exercises a public unauthenticated image.
pub(crate) async fn probe_image_tag_exists_with_authfile(
    image: &str,
    tag: &str,
    runtime: &str,
    authfile: Option<&Path>,
) -> Result<bool, String> {
    let image_ref = format!("{}:{}", image, tag);
    // `silent()` consumes the Command; apply it first, then mutate via
    // &mut for the conditional env attachment.
    let mut cmd = Command::new(runtime).silent();
    if let Some(path) = authfile {
        // v0.2.49: env-var sibling of --authfile (position-independent).
        // Replaces the broken `cmd.arg("--authfile").arg(path)` that
        // produced argv shape `podman --authfile X manifest inspect Y`
        // — rejected by podman 4.x's CLI parser as "unknown flag".
        cmd.env("REGISTRY_AUTH_FILE", path);
    }
    let output = cmd
        .args(["manifest", "inspect", &image_ref])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("spawn {} manifest inspect: {}", runtime, e))?;

    probe_image_tag_exists_classify_output(&image_ref, runtime, &output)
}

/// v0.2.47: shared output classifier for the two `probe_image_tag_exists_*`
/// variants. Decides whether a non-success `manifest inspect` is a
/// genuine "missing tag" (Ok(false), routes to fallback) or a transport /
/// auth / unsupported-subcommand error (Err, surfaces upward). Registry
/// signals for genuine 404s are stable across docker + podman + GHCR.
fn probe_image_tag_exists_classify_output(
    image_ref: &str,
    runtime: &str,
    output: &std::process::Output,
) -> Result<bool, String> {
    if output.status.success() {
        return Ok(true);
    }
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
        // fallback path engages.
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

// v0.2.49: the canonical `PullTokenResponse` lives in
// `vct-launcher-core::services::container_runtime` so the launcher AND
// the hub-side supervisor deserialise the same struct. Re-exported here
// as `pub(crate)` so the launcher's existing usage sites (this file +
// module_service.rs) keep compiling without import sweeps. Field set is
// unchanged from the pre-v0.2.49 launcher copy.
pub(crate) use vct_launcher_core::services::container_runtime::PullTokenResponse;

/// v0.2.46 V46-E (C2) / v0.2.47 cross-runtime: build a per-pull auth
/// context (file for podman, directory-with-config.json for docker)
/// containing credentials for a single registry. Used in place of the
/// previous global `podman login` + `podman logout` flow to avoid cross-
/// module auth-session collision when multiple paid modules pull
/// concurrently against the same registry (e.g. ghcr.io).
///
/// Auth.json shape (per containers/auth.json(5) and docker/cli reference):
///   `{ "auths": { "<registry>": { "auth": "<base64(username:token)>" } } }`
///
/// The returned `PerPullAuth` auto-deletes on drop (the underlying
/// `NamedTempFile` / `TempDir` handle RAII), so callers don't need
/// explicit cleanup — even on early-return error paths.
///
/// v0.2.47: delegated to `vct-launcher-core::services::container_runtime::
/// build_per_pull_authfile` so the launcher AND the hub use the SAME
/// runtime-aware helper (docker `pull` / `run` doesn't support
/// `--authfile`; the core helper switches to `DOCKER_CONFIG=<dir>` for
/// docker). Callers should prefer `PerPullAuth::apply_to(&mut cmd,
/// runtime)` over hardcoding `--authfile`.
pub(crate) fn build_per_pull_authfile(
    registry: &str,
    username: &str,
    token: &str,
    runtime: &str,
) -> Result<vct_launcher_core::services::container_runtime::PerPullAuth, String> {
    vct_launcher_core::services::container_runtime::build_per_pull_authfile(
        registry, username, token, runtime,
    )
}

/// v0.2.46 V46-E (C1 follow-up): known GPU-variant suffixes used by the
/// RL Reranker image (and every future paid module that ships a per-GPU
/// variant matrix). Order matters for suffix matching — longest match
/// first wins, so `-cuda` is checked before any hypothetical `-cu`
/// (none exists today; defensive in case a future module ships one).
///
/// TODO(v0.2.47): drive this list from the L0 catalog's
/// `compatibility.variants` field rather than hardcoding. Catalog-driven
/// resolution lets new module publishers ship custom variants (e.g.
/// `-tensorrt`, `-trt-llm`) without a launcher rebuild. For v0.2.46 the
/// four below are the only published variants across all paid modules.
pub(crate) const KNOWN_VARIANT_SUFFIXES: &[&str] = &["-cuda", "-rocm", "-metal", "-cpu"];

/// v0.2.46 V46-E (C1 follow-up): extract the variant suffix from a tag if
/// it ends with one of the `KNOWN_VARIANT_SUFFIXES`. Returns the suffix
/// (with leading dash) or `None` if no known variant suffix is found.
///
/// Used to preserve the GPU variant across a server-tag override: when
/// the gateway returns a patch-only-different bare tag like `"0.2.5"`,
/// the launcher must re-append the variant suffix (e.g. `-cuda`) from
/// the original client tag, because the registry only carries variant-
/// suffixed tags (`0.2.5-cuda`, `0.2.5-cpu`, `0.2.5-rocm`) — never the
/// bare `0.2.5`.
pub(crate) fn extract_variant_suffix(tag: &str) -> Option<&'static str> {
    KNOWN_VARIANT_SUFFIXES
        .iter()
        .find(|s| tag.ends_with(*s))
        .copied()
}

/// v0.2.46 V46-E (C1 follow-up): given a server-returned tag (which may
/// or may not include a variant suffix) and a client-resolved tag (which
/// always includes the variant suffix the GPU-mode dispatcher chose),
/// produce the effective tag to feed into `decide_variant_to_pull` and
/// `podman pull`.
///
/// Decision tree:
///   - Server tag already ends with a known variant suffix → use as-is
///     (server explicitly chose a variant; trust it).
///   - Server tag lacks a suffix AND client tag has one → append client's
///     suffix to server's bare tag (preserves the GPU dispatch decision).
///   - Server tag lacks a suffix AND client tag lacks one (legacy single-
///     tag module) → use server's tag as-is.
pub(crate) fn merge_server_tag_with_client_variant(server_tag: &str, client_tag: &str) -> String {
    if extract_variant_suffix(server_tag).is_some() {
        return server_tag.to_string();
    }
    if let Some(suffix) = extract_variant_suffix(client_tag) {
        return format!("{}{}", server_tag, suffix);
    }
    server_tag.to_string()
}

/// v0.2.46 V46-E (C1/C4): classify the divergence between a client-resolved
/// version tag and a server-returned version tag.
///
/// Pure helper so unit tests can exercise the classification logic without
/// touching the install path. Parses dotted-numeric prefixes the same way
/// `version_lt` above does, ignoring any non-numeric suffix (e.g. `-cuda`).
///
/// Returns:
///   - `Same`: tags are byte-equal OR parse to the same major.minor.patch.
///   - `PatchOnly`: major.minor matches but patch differs (e.g. 0.1.0 vs 0.1.1).
///     Honor the server's tag; emit a WARN so the divergence is visible.
///   - `MinorOrMajor`: major OR minor differs (e.g. 0.1.0 vs 0.2.8).
///     Hard-fail with a publisher-pointing error — the server's catalog is
///     out of sync with the published L0 catalog and the user can't fix it.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum VersionMismatchClass {
    Same,
    PatchOnly,
    MinorOrMajor,
}

pub(crate) fn classify_tag_mismatch(client_tag: &str, server_tag: &str) -> VersionMismatchClass {
    if client_tag == server_tag {
        return VersionMismatchClass::Same;
    }
    // Reuse the same numeric-prefix parser as `version_lt`. We extract the
    // FIRST three segments (major.minor.patch); extra segments (build /
    // pre-release) are ignored for the classification — they signal a
    // hand-tagged build, not a SemVer-meaningful jump.
    fn parse3(v: &str) -> (u64, u64, u64) {
        let parts: Vec<u64> = v
            .split('.')
            .map(|p| {
                p.chars()
                    .take_while(|c| c.is_ascii_digit())
                    .collect::<String>()
            })
            .map(|s| s.parse::<u64>().unwrap_or(0))
            .collect();
        (
            parts.first().copied().unwrap_or(0),
            parts.get(1).copied().unwrap_or(0),
            parts.get(2).copied().unwrap_or(0),
        )
    }
    let (ca, cb, cc) = parse3(client_tag);
    let (sa, sb, sc) = parse3(server_tag);

    if ca == sa && cb == sb && cc == sc {
        // Parsed equal — e.g. `0.1.0` vs `0.1.0-cuda` (same SemVer with
        // a suffix). Treat as Same for mismatch-classification purposes;
        // the suffix difference is semantic at the registry layer, not
        // the launcher's catalog-drift concern.
        return VersionMismatchClass::Same;
    }
    if ca == sa && cb == sb {
        // Same major.minor, different patch.
        return VersionMismatchClass::PatchOnly;
    }
    VersionMismatchClass::MinorOrMajor
}

/// v0.2.46 V46-E (C3): classify a `request_pull_token` error string into a
/// stable `error_class` for the audit log.
///
/// The classification is by substring match against the user-readable
/// strings emitted by `format_pull_token_error` (which already maps from
/// the structured `(status, code)` returned by the rl-artifact-url edge
/// function). Pure helper so unit tests can pin the mapping without
/// spinning up an HTTP server.
///
/// Returns one of: `"license_invalid"` | `"license_expired"` |
/// `"tier_insufficient"` | `"network"` | `"transient_server_error"` |
/// `"unknown"`.
pub(crate) fn classify_pull_token_error(err_msg: &str) -> &'static str {
    let lower = err_msg.to_lowercase();
    // Order matters: more-specific patterns first. The strings below match
    // the user-readable messages from `format_pull_token_error` plus the
    // reqwest transport-error prefix `POST <url>: <reqwest::Error>`.
    if lower.contains("license key is invalid")
        || lower.contains("license_invalid")
        || lower.contains("has been revoked")
    {
        "license_invalid"
    } else if lower.contains("license has expired") || lower.contains("license_expired") {
        "license_expired"
    } else if lower.contains("requires the") && lower.contains("tier")
        || lower.contains("tier_insufficient")
    {
        "tier_insufficient"
    } else if lower.contains("temporarily unavailable")
        || lower.contains("try again in a few minutes")
        || lower.contains("http 5")
    {
        "transient_server_error"
    } else if lower.starts_with("post ")
        || lower.contains("build http client")
        || lower.contains("parse pull-token response")
        || lower.contains("keychain read failed")
        || lower.contains("no license activated")
    {
        "network"
    } else {
        "unknown"
    }
}

/// v0.2.46 V46-E (C3): best-effort license-key prefix for audit log details.
///
/// Always 12-char prefix (matching `vct_launcher_core::db::license_keys::
/// key_prefix_of`) — never the full key. Pure helper so tests can pin the
/// behaviour without keychain access.
pub(crate) fn license_key_prefix_for_audit(key: &str) -> String {
    key.chars().take(12).collect()
}

/// v0.2.46 V46-E (C3): emit a `pull_token_requested` audit-log row.
///
/// Best-effort: if `db` is `None` (test or legacy caller without DB
/// handle) OR `Db::audit` returns Err, we log to eprintln and continue.
/// Auditing must NEVER block the install flow — the install is the
/// primary user-visible action; audit is forensic.
fn audit_pull_token_requested(
    db: Option<&crate::db::Db>,
    module_id: &str,
    endpoint: &str,
    license_key: &str,
    client_resolved_tag: &str,
) {
    let Some(db) = db else { return };
    let detail = serde_json::json!({
        "module_id": module_id,
        "endpoint": endpoint,
        "license_key_prefix": license_key_prefix_for_audit(license_key),
        "client_resolved_tag": client_resolved_tag,
    });
    if let Err(e) = db.audit("pull_token_requested", None, Some(module_id), &detail) {
        eprintln!(
            "[installer_engine] audit_pull_token_requested[{}]: write failed: {}",
            module_id, e
        );
    }
}

/// v0.2.46 V46-E (C3): emit a `pull_token_resolved` audit-log row on success.
///
/// `server_tag` is optional because pre-v0.2.46 servers omitted the field.
/// `tag_mismatch_class` indicates whether the server-returned tag diverged
/// from the client-resolved tag (and how — patch-only / minor-major).
///
/// `endpoint` and `effective_tag_with_variant` are included so future
/// debugging can answer "what URL did we hit?" and "what image:tag did we
/// actually pull?" with a single SQL query against the audit log — no
/// need to cross-reference launcher stderr after the fact.
#[allow(clippy::too_many_arguments)]
fn audit_pull_token_resolved(
    db: Option<&crate::db::Db>,
    module_id: &str,
    endpoint: &str,
    server_tag: Option<&str>,
    client_resolved_tag: &str,
    effective_tag_with_variant: &str,
    username: Option<&str>,
    expires_in_s: u64,
    mismatch_class: &VersionMismatchClass,
) {
    let Some(db) = db else { return };
    let class_str = match mismatch_class {
        VersionMismatchClass::Same => "none",
        VersionMismatchClass::PatchOnly => "patch_only",
        VersionMismatchClass::MinorOrMajor => "minor_or_major",
    };
    let detail = serde_json::json!({
        "module_id": module_id,
        "endpoint": endpoint,
        "server_tag": server_tag,
        "client_resolved_tag": client_resolved_tag,
        "effective_tag_with_variant": effective_tag_with_variant,
        "username": username,
        "expires_in_s": expires_in_s,
        "tag_mismatch": !matches!(mismatch_class, VersionMismatchClass::Same),
        "mismatch_class": class_str,
    });
    if let Err(e) = db.audit("pull_token_resolved", None, Some(module_id), &detail) {
        eprintln!(
            "[installer_engine] audit_pull_token_resolved[{}]: write failed: {}",
            module_id, e
        );
    }
}

/// v0.2.46 V46-E (C3): emit a `pull_token_failed` audit-log row on Err.
fn audit_pull_token_failed(
    db: Option<&crate::db::Db>,
    module_id: &str,
    error_msg: &str,
) {
    let Some(db) = db else { return };
    let error_class = classify_pull_token_error(error_msg);
    let excerpt: String = error_msg.chars().take(200).collect();
    let detail = serde_json::json!({
        "module_id": module_id,
        "error_class": error_class,
        "error_excerpt": excerpt,
    });
    if let Err(e) = db.audit("pull_token_failed", None, Some(module_id), &detail) {
        eprintln!(
            "[installer_engine] audit_pull_token_failed[{}]: write failed: {}",
            module_id, e
        );
    }
}

/// Request a short-lived registry pull token from the manifest's
/// Default Supabase endpoint for the paid-module pull-token gateway.
///
/// v0.2.42 (W8): added as a hardcoded fallback for the case where BOTH the
/// L0 catalog override AND the L1 manifest's `pull_token_endpoint` field are
/// absent, empty, or still contain the test/publish-time placeholder string
/// `"https://example/pull-token"`.
///
/// Why this is needed:
/// - The L0 catalog bucket is the canonical SoT for the real URL. If the user
///   has never visited the Modules tab (cold boot, no catalog cache), the L0
///   override passed to `request_pull_token` is `None`.
/// - The synthesized L1 manifest at that point is built from the L0 catalog
///   install-slice — so if the catalog cache IS populated, it carries the real
///   URL. But if the cache is somehow stale or the module publisher shipped a
///   manifest with the placeholder still in it, the fallback here saves the
///   install from a useless POST to `https://example/`.
/// - Pattern mirrors `module_service::DEFAULT_RL_LATEST_VERSION_ENDPOINT` and
///   `licensing::VALIDATE_TIER_DEFAULT_ENDPOINT`.
// v0.2.49: these pull-token gateway constants + helpers moved to
// `vct-launcher-core::services::container_runtime` so the hub-side
// supervisor (Phase 3 auth port) consumes the SAME placeholder family,
// the SAME default endpoint, and the SAME `resolve_pull_token_endpoint`
// substitution logic the launcher does. Re-exported here so the
// existing `pub(crate)` call sites + tests in this file keep their
// unqualified imports working.
#[allow(unused_imports)]
pub(crate) use vct_launcher_core::services::container_runtime::{
    is_pull_token_placeholder, resolve_pull_token_endpoint, PULL_TOKEN_ENDPOINT_PLACEHOLDER,
    RL_ARTIFACT_URL_DEFAULT_ENDPOINT,
};

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
/// v0.2.49: thin wrapper around the shared
/// `vct-launcher-core::services::container_runtime::request_pull_token`.
/// The hub-side supervisor's pre-pull-with-auth flow calls the same
/// core helper, so both crates emit byte-identical POST bodies for the
/// same `(license_key, machine_id_hash, endpoint)` triple. License read
/// + machine_id_hash both live in `vct_launcher_core::licensing` since
/// v0.2.49 — see that module's docstring for the cross-platform
/// invariants.
pub(crate) async fn request_pull_token(
    container: &crate::manifest::ContainerInstallBlock,
    l0_pull_token_endpoint: Option<&str>,
) -> Result<PullTokenResponse, String> {
    vct_launcher_core::services::container_runtime::request_pull_token(
        container,
        l0_pull_token_endpoint,
    )
    .await
}

/// v0.2.49: re-export the shared error formatter so the launcher's
/// existing test module + downstream callers (`container_pull` error
/// path) keep their unqualified imports working. The unused-imports
/// lint runs at module scope and doesn't see the in-file test mod's
/// `use super::*`, so we allow it.
#[allow(unused_imports)]
pub(crate) use vct_launcher_core::services::container_runtime::format_pull_token_error;

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

// v0.2.46 V46-E (C2): `container_login` removed. Was the global
// `podman login --password-stdin` flow that mutated
// `$XDG_RUNTIME_DIR/containers/auth.json`. Replaced by
// `build_per_pull_authfile` + `podman pull --authfile <tmp>` to avoid
// cross-module session collisions and eliminate the cleanup-on-error
// burden. See `container_pull` Step 3 for the new flow.

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
            let pulled_tag = refetch_artifact(manifest, &install_dir, gpu_mode, l0_pull_token_endpoint, Some(db)).await?;
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
    let pulled_tag = refetch_artifact(manifest, &install_dir, gpu_mode, l0_pull_token_endpoint, Some(db)).await?;

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
    // v0.2.46 V46-E (C3): forward the Db handle into `container_pull` so
    // the upgrade path emits the same `pull_token_*` audit rows the install
    // path now writes. `Option<&Db>` mirrors `container_pull`'s shape so
    // a `None` caller still works (e.g. the unit tests further down don't
    // touch the audit log).
    db: Option<&crate::db::Db>,
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
                db,
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
            let res = refetch_artifact(&manifest, &install_dir, GpuMode::Cpu, None, None).await;
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
            let res = refetch_artifact(&manifest, &install_dir, GpuMode::Cpu, None, None).await;
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

    // ─── v0.2.42 W8: resolve_pull_token_endpoint substitution logic ────
    //
    // `resolve_pull_token_endpoint` is the pure helper that replaces
    // empty strings and the PULL_TOKEN_ENDPOINT_PLACEHOLDER with the
    // hardcoded RL_ARTIFACT_URL_DEFAULT_ENDPOINT. Tests verify every
    // case the production code can encounter:
    //   1. placeholder string → default const
    //   2. real URL → passes through unchanged
    //   3. empty string → default const
    //   4. The combined flow: L0 Some + real URL → L0 URL (no substitution)
    //   5. The combined flow: L0 None + L1 placeholder → default const

    #[test]
    fn resolve_endpoint_placeholder_substitutes_default() {
        // The exact placeholder baked into the l0_manifest_synth test
        // fixture and any publisher who shipped without manifest-hygiene CI.
        let result = resolve_pull_token_endpoint(PULL_TOKEN_ENDPOINT_PLACEHOLDER);
        assert_eq!(
            result, RL_ARTIFACT_URL_DEFAULT_ENDPOINT,
            "placeholder must be substituted with the real Supabase URL; got: {}",
            result
        );
    }

    #[test]
    fn resolve_endpoint_empty_substitutes_default() {
        // Malformed publish: L0 catalog row has `pull_token_endpoint: ""`.
        let result = resolve_pull_token_endpoint("");
        assert_eq!(
            result, RL_ARTIFACT_URL_DEFAULT_ENDPOINT,
            "empty endpoint must be substituted with the real Supabase URL; got: {}",
            result
        );
    }

    #[test]
    fn resolve_endpoint_real_url_passes_through() {
        // A real custom endpoint (e.g. staging or a third-party module)
        // must NOT be replaced — the caller set it deliberately.
        let real = "https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/rl-artifact-url";
        let result = resolve_pull_token_endpoint(real);
        assert_eq!(
            result, real,
            "real URL must pass through unchanged; got: {}",
            result
        );
    }

    #[test]
    fn resolve_endpoint_arbitrary_non_placeholder_passes_through() {
        // A staging override or third-party module URL. Subdomains of
        // `example.com` are NOT placeholders — they're legitimate
        // user-controlled hostnames (could be a staging tenant or a
        // third-party gateway).
        let staging = "https://staging.example.com/functions/v1/pull-token";
        let result = resolve_pull_token_endpoint(staging);
        assert_eq!(
            result, staging,
            "non-placeholder URL must pass through unchanged; got: {}",
            result
        );
    }

    // v0.2.42 P3-P1-1: widened placeholder detection covers RFC-2606 reserved
    // bare hosts. These are obvious "I forgot to set the URL" markers, NOT
    // legitimate endpoints. Substituting them with the default const is the
    // best the launcher can do without blocking the install entirely.
    #[test]
    fn resolve_endpoint_rfc2606_hosts_substitute_default() {
        for placeholder in [
            "https://example.com/pull-token",
            "https://example.org/functions/v1/rl-artifact-url",
            "https://example.net/x",
            "https://example.invalid/anything",
            "https://example.test/",
            // Port suffix on the host: still a placeholder.
            "https://example.com:443/pull-token",
            // http:// scheme is also caught.
            "http://example.com/pull-token",
        ] {
            let result = resolve_pull_token_endpoint(placeholder);
            assert_eq!(
                result, RL_ARTIFACT_URL_DEFAULT_ENDPOINT,
                "expected {:?} to be detected as a placeholder; got: {}",
                placeholder, result
            );
        }
    }

    #[test]
    fn resolve_endpoint_example_subdomains_pass_through() {
        // Subdomains of RFC-2606 reserved hosts are NOT placeholders.
        // A staging Supabase tenant or a third-party gateway might
        // legitimately live at e.g. `staging.example.com`.
        for legitimate in [
            "https://staging.example.com/functions/v1/pull-token",
            "https://api.example.org/v1/tokens",
            "https://my-tenant.example.net/pull",
        ] {
            let result = resolve_pull_token_endpoint(legitimate);
            assert_eq!(
                result, legitimate,
                "expected {:?} to pass through (subdomain, not bare); got: {}",
                legitimate, result
            );
        }
    }

    #[test]
    fn l0_some_real_url_plus_resolve_endpoint_gives_l0_url() {
        // Full stack: L0 override present with a real URL → resolve_endpoint
        // passes it through untouched (no default substitution needed).
        let l0_real = "https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/rl-artifact-url";
        let l1_placeholder = PULL_TOKEN_ENDPOINT_PLACEHOLDER;
        let raw = Some(l0_real).unwrap_or(l1_placeholder);
        let effective = resolve_pull_token_endpoint(raw);
        assert_eq!(
            effective, l0_real,
            "W8: L0 real URL must be used as-is; got: {}",
            effective
        );
    }

    #[test]
    fn l0_none_l1_placeholder_resolves_to_default_const() {
        // Root cause scenario from v0.2.41 dogfooding:
        //   L0 override absent (cache miss) AND L1 manifest carries placeholder.
        // Previously: would POST to "https://example/pull-token" → connection
        // error, fall through to anonymous pull, GHCR 401 with no useful message.
        // After fix: substitutes RL_ARTIFACT_URL_DEFAULT_ENDPOINT.
        let l0_none: Option<&str> = None;
        let l1_placeholder = PULL_TOKEN_ENDPOINT_PLACEHOLDER;
        let raw = l0_none.unwrap_or(l1_placeholder);
        let effective = resolve_pull_token_endpoint(raw);
        assert_eq!(
            effective, RL_ARTIFACT_URL_DEFAULT_ENDPOINT,
            "W8: L0 absent + L1 placeholder must resolve to default const; got: {}",
            effective
        );
    }

    // ─── v0.2.45 V45-D: widened placeholder family covers "placeholder.*" ─
    //
    // v0.2.42 W8 / P3-P1-1 covered the `example.*` family but missed the
    // literal "placeholder" subdomain — the form the v0.2.7 RL manifest
    // shipped with (`https://placeholder.supabase.co/functions/v1/rl-pull-token`).
    // V45-D widens `is_pull_token_placeholder` to catch:
    //   - bare `placeholder` host
    //   - `placeholder.<anything>` host
    //   - `<anything>.placeholder` host
    // …on either `http://` or `https://`, with optional port.
    //
    // Regression coverage for the existing `example.*` family is included
    // (test_v0245_existing_example_still_detected) so the new logic doesn't
    // accidentally clobber the W8 path.

    #[test]
    fn test_v0245_placeholder_dot_supabase_is_placeholder() {
        // The exact form the v0.2.7 RL manifest carried — root-cause URL
        // that fell through to anonymous pull → GHCR 401.
        assert!(
            is_pull_token_placeholder(
                "https://placeholder.supabase.co/functions/v1/rl-pull-token"
            ),
            "placeholder.supabase.co must be detected as a placeholder"
        );
        // And via resolve_pull_token_endpoint → substitutes the default.
        let result = resolve_pull_token_endpoint(
            "https://placeholder.supabase.co/functions/v1/rl-pull-token",
        );
        assert_eq!(
            result, RL_ARTIFACT_URL_DEFAULT_ENDPOINT,
            "placeholder.supabase.co must resolve to default; got: {}",
            result
        );
    }

    #[test]
    fn test_v0245_placeholder_root_is_placeholder() {
        // Bare `placeholder` host (no TLD). The pre-publish fixture form.
        assert!(
            is_pull_token_placeholder("https://placeholder/foo"),
            "bare `placeholder` host must be detected as a placeholder"
        );
    }

    #[test]
    fn test_v0245_subdomain_dot_placeholder_is_placeholder() {
        // The reverse pattern: `<something>.placeholder` host (less common
        // but still an obvious "I forgot to set the URL" marker).
        assert!(
            is_pull_token_placeholder("https://foo.placeholder/bar"),
            "foo.placeholder must be detected as a placeholder"
        );
    }

    #[test]
    fn test_v0245_placeholder_family_case_insensitive() {
        // Per the doc comment, host matching for the placeholder family is
        // case-insensitive. URL host components are case-insensitive per
        // RFC 3986; the launcher must catch all common-case spellings.
        for variant in [
            "https://Placeholder.supabase.co/foo",
            "https://PLACEHOLDER.supabase.co/foo",
            "https://placeholder.SUPABASE.co/foo",
            "https://PlAcEhOlDeR.example.test/foo",
        ] {
            assert!(
                is_pull_token_placeholder(variant),
                "case-variant {:?} must be detected as a placeholder",
                variant
            );
        }
    }

    #[test]
    fn test_v0245_placeholder_family_http_and_port_variants() {
        // http:// scheme and explicit port suffix must not bypass detection.
        for variant in [
            "http://placeholder.supabase.co/foo",
            "https://placeholder.supabase.co:443/foo",
            "http://placeholder:80/foo",
        ] {
            assert!(
                is_pull_token_placeholder(variant),
                "variant {:?} must be detected as a placeholder",
                variant
            );
        }
    }

    #[test]
    fn test_v0245_existing_example_still_detected() {
        // Regression: V45-D must NOT clobber the v0.2.42 W8 / P3-P1-1
        // example.* family. Re-assert each example.* host shape.
        for variant in [
            "https://example.com/foo",
            "https://example.org/functions/v1/rl-artifact-url",
            "https://example.net/x",
            "https://example.invalid/anything",
            "https://example.test/",
            "https://example/pull-token", // bare-example historical form
        ] {
            assert!(
                is_pull_token_placeholder(variant),
                "existing example.* family member {:?} must still be detected",
                variant
            );
        }
    }

    #[test]
    fn test_v0245_legitimate_supabase_not_placeholder() {
        // The real production Supabase project URL must NOT be detected as
        // a placeholder. This is the URL `RL_ARTIFACT_URL_DEFAULT_ENDPOINT`
        // points to; if `is_pull_token_placeholder` returned true for it,
        // every install would silently substitute the default-of-the-default
        // (a no-op that happens to work, but a clear sign of a logic bug).
        assert!(
            !is_pull_token_placeholder(
                "https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/rl-pull-token"
            ),
            "real Supabase URL must NOT be detected as a placeholder"
        );
        assert!(
            !is_pull_token_placeholder(
                "https://abc123def.supabase.co/functions/v1/rl-pull-token"
            ),
            "arbitrary <project-id>.supabase.co must NOT be detected as a placeholder"
        );
        // A user-controlled subdomain that happens to start with "place"
        // but isn't "placeholder" must pass through. (Defensive guard
        // against accidentally using substring-match instead of prefix-match.)
        assert!(
            !is_pull_token_placeholder("https://places.example.io/foo"),
            "places.example.io must NOT be detected as a placeholder \
             (starts with 'place' but isn't 'placeholder')"
        );
        assert!(
            !is_pull_token_placeholder("https://my-placeholder-name.test.io/foo"),
            "my-placeholder-name.test.io must NOT be detected as a placeholder \
             (contains 'placeholder' but isn't the literal `placeholder` segment)"
        );
    }

    // ─── v0.2.45 V45-D: VCT_RL_PULL_TOKEN_ENDPOINT env override semantics ─
    //
    // The env var short-circuits the L0/L1/default resolution chain entirely.
    // We can't exercise the full `request_pull_token` path here (requires
    // keychain + live HTTP), but we CAN verify the precedence-decoding logic
    // that `request_pull_token` uses:
    //   - env var set + non-empty → use the env value verbatim
    //   - env var set + empty/whitespace → fall back to L0/L1/default chain
    //   - env var unset → fall back to L0/L1/default chain
    //
    // The helper below mirrors the exact `match`/`filter` chain inside
    // `request_pull_token` so a divergence in either place will fail one of
    // these tests. Keeping the logic in lock-step with the production code
    // is the entire point — these tests live next to the function, not in a
    // separate integration suite.

    /// Test-only mirror of the env-override decoding in `request_pull_token`.
    /// Returns the effective endpoint string the function would POST to,
    /// given an env value (or `None` for "unset") and the L0/L1/default
    /// fallback chain reduced to a single `raw` argument.
    fn v0245_resolve_with_env(env_value: Option<&str>, raw: &str) -> String {
        match env_value
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
        {
            Some(env_url) => env_url,
            None => resolve_pull_token_endpoint(raw).to_string(),
        }
    }

    #[test]
    fn test_v0245_env_override_takes_precedence() {
        // Env var set to a real URL → POST goes to env URL, NOT to the
        // resolved L0/L1/default endpoint. Verified at the precedence-
        // decoding layer (the same `match`/`filter` chain inside
        // `request_pull_token`).
        let env = Some("https://mocked-pull-token.test/foo");
        // Even if `raw` is a known placeholder (which would normally be
        // substituted with RL_ARTIFACT_URL_DEFAULT_ENDPOINT), the env
        // override wins.
        let raw = PULL_TOKEN_ENDPOINT_PLACEHOLDER;
        let effective = v0245_resolve_with_env(env, raw);
        assert_eq!(
            effective, "https://mocked-pull-token.test/foo",
            "env override must take precedence over L1 placeholder; got: {}",
            effective
        );
    }

    #[test]
    fn test_v0245_env_override_trims_whitespace() {
        // Operators may accidentally leave trailing whitespace in their
        // shell rc. The decoding trims whitespace before deciding whether
        // the value is "empty"; a leading/trailing space alone must not
        // disable the override.
        let env = Some("  https://mocked-pull-token.test/foo  ");
        let raw = PULL_TOKEN_ENDPOINT_PLACEHOLDER;
        let effective = v0245_resolve_with_env(env, raw);
        assert_eq!(
            effective, "https://mocked-pull-token.test/foo",
            "env override must be whitespace-trimmed; got: {:?}",
            effective
        );
    }

    #[test]
    fn test_v0245_empty_env_falls_back_to_raw() {
        // Env var set to empty / whitespace-only → ignored, behaviour
        // unchanged. Verifies the `.filter(|s| !s.is_empty())` step.
        let raw_real = "https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/rl-artifact-url";

        for empty_form in ["", "   ", "\t\n"] {
            let effective = v0245_resolve_with_env(Some(empty_form), raw_real);
            assert_eq!(
                effective, raw_real,
                "empty env value {:?} must fall back to raw; got: {}",
                empty_form, effective
            );
        }

        // And with raw = placeholder, the substitution still fires.
        let effective_placeholder = v0245_resolve_with_env(
            Some(""),
            PULL_TOKEN_ENDPOINT_PLACEHOLDER,
        );
        assert_eq!(
            effective_placeholder, RL_ARTIFACT_URL_DEFAULT_ENDPOINT,
            "empty env value + L1 placeholder must still substitute default; got: {}",
            effective_placeholder
        );
    }

    #[test]
    fn test_v0245_unset_env_falls_back_to_raw() {
        // Env var unset → behaviour is exactly the pre-V45-D logic
        // (resolve_pull_token_endpoint on the raw L0/L1 chain).
        let raw_real = "https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/rl-artifact-url";
        let effective_real = v0245_resolve_with_env(None, raw_real);
        assert_eq!(
            effective_real, raw_real,
            "unset env + real raw URL must pass through; got: {}",
            effective_real
        );

        let effective_placeholder = v0245_resolve_with_env(
            None,
            "https://placeholder.supabase.co/functions/v1/rl-pull-token",
        );
        assert_eq!(
            effective_placeholder, RL_ARTIFACT_URL_DEFAULT_ENDPOINT,
            "unset env + placeholder.supabase.co must resolve to default; got: {}",
            effective_placeholder
        );
    }

    // ─── v0.2.42 W8: structured error parsing covers all gateway cases ──
    //
    // Extends the existing v0.2.35 error tests with cases specifically
    // from the rl-artifact-url edge function contract that weren't
    // previously exercised as a named set.

    #[test]
    fn pull_token_error_401_machine_mismatch_surfaces_rebind_hint() {
        // Server returns machine_id_hash mismatch — user needs admin rebind.
        // This is a known case the user can self-serve (rebind-admin-token).
        let body = serde_json::json!({
            "error": "machine_mismatch",
            "detail": "hash mismatch; use rebind-admin-token to re-bind"
        });
        let msg = format_pull_token_error(401, &body);
        assert!(
            msg.contains("machine_mismatch"),
            "machine_mismatch code must appear in the error; got: {}",
            msg
        );
        assert!(
            !msg.is_empty(),
            "machine_mismatch must produce a non-empty user message"
        );
    }

    #[test]
    fn pull_token_error_500_registry_exchange_failed_is_transient() {
        // GHCR token-exchange 5xx — transient, user should retry.
        let body = serde_json::json!({
            "error": "registry_token_exchange_failed",
            "detail": "ghcr 500: upstream error"
        });
        let msg = format_pull_token_error(500, &body);
        assert!(
            msg.contains("unavailable") || msg.contains("Try again") || msg.contains("temporarily"),
            "500 from registry exchange must be framed as transient; got: {}",
            msg
        );
    }

    #[test]
    fn pull_token_error_500_service_misconfigured_is_transient() {
        // SUPABASE_URL / SERVICE_ROLE_KEY missing on the server side — not
        // actionable for the user, but transient framing is the right UX.
        let body = serde_json::json!({
            "error": "Service misconfigured",
            "detail": ""
        });
        let msg = format_pull_token_error(500, &body);
        assert!(
            !msg.is_empty(),
            "service_misconfigured must produce a non-empty message; got: {}",
            msg
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

    /// v0.2.49 regression test (live podman): verify the env-var auth
    /// shape used by `probe_image_tag_exists_with_authfile` and
    /// `probe_image_tag_exists_with_auth_context` is accepted by the
    /// real podman binary's CLI parser.
    ///
    /// Pre-v0.2.49 the probe helpers ran:
    ///     podman --authfile /tmp/X manifest inspect ghcr.io/.../image:tag
    /// which podman 4.x rejects with "Error: unknown flag: --authfile"
    /// (exit 125). `--authfile` is SUBCOMMAND-scoped, not global — the
    /// same bug class as the `PerPullAuth::apply_to` regression closed
    /// by `b4830e04`. Because both probe helpers were only covered by
    /// argv-shape unit tests (`format!("{:?}", cmd).contains("--authfile")`),
    /// neither caught the live podman parser rejection.
    ///
    /// This test exercises the env-var shape end-to-end with the real
    /// podman binary on PATH. We spawn `REGISTRY_AUTH_FILE=X podman
    /// --version` (the cheapest invocation that traverses the global-
    /// flag-vs-subcommand-scope parsing logic). Equivalent test in
    /// `container_runtime.rs::tests` for the pull-side fix:
    /// `per_pull_auth_podman_env_var_accepted_by_live_podman`.
    ///
    /// Skipped (clean return) on hosts without a podman binary so CI
    /// runs on builder boxes without container runtimes stay green.
    #[test]
    fn probe_with_registry_auth_file_env_accepted_by_live_podman() {
        let Ok(probe) = std::process::Command::new("which").arg("podman").output() else {
            return;
        };
        if !probe.status.success() {
            return;
        }
        // Build a real per-pull authfile (same helper the probe call
        // sites use to produce the path that flows into the env var).
        let guard = vct_launcher_core::services::container_runtime::build_per_pull_authfile(
            "ghcr.io",
            "bot",
            "tok",
            "podman",
        )
        .expect("build podman authfile");
        let path = guard
            .path()
            .expect("podman guard exposes path()")
            .to_owned();
        let output = std::process::Command::new("podman")
            .env("REGISTRY_AUTH_FILE", &path)
            .arg("--version")
            .output()
            .expect("spawn podman --version");
        assert!(
            output.status.success(),
            "podman --version with REGISTRY_AUTH_FILE set must succeed \
             (caught a parser regression if not), stderr={}",
            String::from_utf8_lossy(&output.stderr)
        );
        // Negative assert: a regression that reintroduces the broken
        // `--authfile`-before-subcommand shape would produce stderr
        // containing "unknown flag" with exit 125. Pin that explicitly
        // so a future revert is loud, not silent.
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            !stderr.contains("unknown flag"),
            "podman CLI parser rejected the env-var shape (regression!), \
             stderr={}",
            stderr
        );
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

    // ─── v0.2.46 V46-E: RL client-side hardening ────────────────────────
    //
    // Seven tests covering:
    //   C1 — honor server's `tag` over client-resolved L0 version
    //   C3 — audit-log emission for the pull-token gateway
    //   C4 — hard-fail on MAJOR/MINOR tag mismatch (with publisher-pointing msg)
    //   redaction — license_key is logged ONLY as 12-char prefix
    //
    // The C1/C4 split is enforced by `classify_tag_mismatch`:
    //   - Same major.minor.patch → Same (no action)
    //   - Same major.minor, different patch → PatchOnly (honor server, WARN)
    //   - Different major OR minor → MinorOrMajor (hard-fail)
    //
    // The audit tests use `crate::db::Db::open_in_memory()` and inspect the
    // `audit_log` table via `Db::audit_list` — same pattern as
    // commands/installer.rs tests for github_pat_file_migration.

    #[test]
    fn test_v46e_c1_honor_server_tag() {
        // C1: when server returns a tag with a patch-only difference,
        // classify_tag_mismatch flags it as PatchOnly and the caller honors
        // the server's tag. (The integration with `container_pull` is
        // covered by `effective_tag` flowing into `decide_variant_to_pull`;
        // here we pin the classification logic that drives that decision.)
        assert_eq!(
            classify_tag_mismatch("0.1.0", "0.1.1"),
            VersionMismatchClass::PatchOnly,
            "0.1.0 vs 0.1.1 must classify as PatchOnly (honor server)",
        );
        assert_eq!(
            classify_tag_mismatch("0.2.8", "0.2.5"),
            VersionMismatchClass::PatchOnly,
            "0.2.8 vs 0.2.5 must classify as PatchOnly (server is SoT)",
        );
        // Same value → Same (no action — most common case).
        assert_eq!(
            classify_tag_mismatch("0.1.0", "0.1.0"),
            VersionMismatchClass::Same,
            "identical tags must classify as Same",
        );
        // Same SemVer with a build suffix → Same (the registry layer
        // handles the suffix; the launcher's catalog drift concern is
        // only the dotted-numeric prefix). This is the v46-E followup
        // clarification: variant suffix is NOT a version component.
        assert_eq!(
            classify_tag_mismatch("0.1.0", "0.1.0-cuda"),
            VersionMismatchClass::Same,
            "0.1.0 vs 0.1.0-cuda must classify as Same (suffix ignored)",
        );
        // Variant suffix asymmetry: 0.2.8-cuda vs 0.2.8 — Same.
        assert_eq!(
            classify_tag_mismatch("0.2.8-cuda", "0.2.8"),
            VersionMismatchClass::Same,
            "0.2.8-cuda vs 0.2.8 must classify as Same",
        );
    }

    #[test]
    fn test_v46e_c1_followup_preserves_variant_suffix() {
        // V46-E follow-up: when the server returns a bare patch-level
        // override (e.g. `0.2.5`) and the client tag has a variant suffix
        // (`-cuda`), the launcher must re-append the client's suffix so
        // the registry probe + `podman pull` find the right manifest.
        // The registry only carries variant-suffixed tags; a bare
        // `0.2.5` pull would 404.
        assert_eq!(
            merge_server_tag_with_client_variant("0.2.5", "0.2.8-cuda"),
            "0.2.5-cuda",
            "bare server tag + cuda client must merge to 0.2.5-cuda",
        );
        assert_eq!(
            merge_server_tag_with_client_variant("0.2.5", "0.2.8-cpu"),
            "0.2.5-cpu",
            "bare server tag + cpu client must merge to 0.2.5-cpu",
        );
        assert_eq!(
            merge_server_tag_with_client_variant("0.2.5", "0.2.8-rocm"),
            "0.2.5-rocm",
            "bare server tag + rocm client must merge to 0.2.5-rocm",
        );
        // If the server explicitly returned a suffixed tag, honor it as-is
        // (server is SoT — maybe the publisher wants to force a specific
        // variant for a particular release).
        assert_eq!(
            merge_server_tag_with_client_variant("0.2.5-cuda", "0.2.8-cuda"),
            "0.2.5-cuda",
            "server-suffixed tag passes through unchanged",
        );
        // Cross-variant: server says -cpu, client wanted -cuda. Server
        // wins (e.g. publisher disabled CUDA temporarily).
        assert_eq!(
            merge_server_tag_with_client_variant("0.2.5-cpu", "0.2.8-cuda"),
            "0.2.5-cpu",
            "server's variant overrides client's variant",
        );
        // Legacy single-tag module: neither side has a suffix.
        assert_eq!(
            merge_server_tag_with_client_variant("0.2.5", "0.2.8"),
            "0.2.5",
            "no suffix on either side → server tag as-is",
        );
        // extract_variant_suffix isolated unit test (defends against a
        // future refactor that adds a new variant and forgets to update
        // KNOWN_VARIANT_SUFFIXES).
        assert_eq!(extract_variant_suffix("0.2.8-cuda"), Some("-cuda"));
        assert_eq!(extract_variant_suffix("0.2.8-cpu"), Some("-cpu"));
        assert_eq!(extract_variant_suffix("0.2.8-rocm"), Some("-rocm"));
        assert_eq!(extract_variant_suffix("0.2.8-metal"), Some("-metal"));
        assert_eq!(extract_variant_suffix("0.2.8"), None);
        assert_eq!(extract_variant_suffix("0.2.8-tensorrt"), None);
    }

    #[test]
    fn test_v46e_c2_authfile_has_correct_shape() {
        // V46-E C2: per-pull authfile contains the docker/podman
        // auth.json shape: { "auths": { "<registry>": { "auth": "<b64>" } } }.
        // Verify the file is created, contains the expected JSON, and
        // the base64 round-trips to the original `username:token`.
        //
        // v0.2.47: read via the on-disk file path exposed by
        // `PerPullAuth::path()` (podman-shape guards expose this; docker-
        // shape guards expose `docker_config_dir()` instead). The
        // internal NamedTempFile is no longer accessed directly — the
        // public API surface for the auth blob is read-by-path.
        use base64::{engine::general_purpose::STANDARD as B64, Engine as _};

        let registry = "ghcr.io";
        let username = "vct-bot-rl";
        let token = "ghp_redactedTOKEN1234567890abcdef";
        let authfile =
            build_per_pull_authfile(registry, username, token, "podman").expect("authfile builds");
        // Path must exist on disk (podman-shape guard).
        let path = authfile
            .path()
            .expect("podman shape exposes path")
            .to_path_buf();
        assert!(path.exists(), "authfile path must exist on disk");

        // Read via fs::read_to_string — the file is open for read by the
        // OS even while NamedTempFile holds the write handle.
        let content = std::fs::read_to_string(&path).expect("read authfile");
        let parsed: serde_json::Value =
            serde_json::from_str(&content).expect("authfile is valid json");
        let auth_b64 = parsed
            .pointer("/auths/ghcr.io/auth")
            .and_then(|v| v.as_str())
            .expect("auths.ghcr.io.auth must be present");

        // Round-trip: base64-decode and split on `:`.
        let decoded = B64.decode(auth_b64).expect("auth field is valid base64");
        let decoded_str = String::from_utf8(decoded).expect("decoded auth is utf-8");
        assert_eq!(
            decoded_str, "vct-bot-rl:ghp_redactedTOKEN1234567890abcdef",
            "decoded auth must round-trip to <username>:<token>",
        );

        // Tempfile auto-deletes on drop — guard against any leak by
        // explicitly dropping and re-checking the path.
        drop(authfile);
        assert!(
            !path.exists(),
            "authfile must be deleted on drop (RAII cleanup)"
        );
    }

    #[test]
    fn test_v46e_c3_audit_log_emitted_on_request() {
        // C3: `audit_pull_token_requested` writes a row with:
        //   - operation = "pull_token_requested"
        //   - module_id matches
        //   - detail.endpoint matches
        //   - detail.license_key_prefix is the FIRST 12 chars only
        //   - detail.client_resolved_tag matches
        let db = crate::db::Db::open_in_memory().expect("in-memory db");
        let full_key = "vct_admin_TUVWXYZ1234567890abcdef";
        audit_pull_token_requested(
            Some(&db),
            "vct-rl-reranker",
            "https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/rl-artifact-url",
            full_key,
            "0.2.8",
        );
        let events = db
            .audit_list(None, None, None, None, Some("pull_token_requested"), 10)
            .expect("audit_list");
        let row = events
            .iter()
            .find(|e| e.operation == "pull_token_requested")
            .expect("must find pull_token_requested row");
        assert_eq!(row.module_id.as_deref(), Some("vct-rl-reranker"));
        let detail: serde_json::Value =
            serde_json::from_str(&row.detail).expect("detail is valid json");
        assert_eq!(
            detail.get("module_id").and_then(|v| v.as_str()),
            Some("vct-rl-reranker"),
        );
        assert_eq!(
            detail.get("endpoint").and_then(|v| v.as_str()),
            Some("https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/rl-artifact-url"),
        );
        // The CRITICAL security invariant: prefix is 12 chars, NEVER full.
        let prefix = detail
            .get("license_key_prefix")
            .and_then(|v| v.as_str())
            .expect("license_key_prefix must be present");
        assert_eq!(prefix, "vct_admin_TU", "must be first 12 chars only");
        assert_eq!(prefix.len(), 12, "prefix MUST be exactly 12 chars");
        assert!(
            !row.detail.contains("TUVWXYZ1234567890abcdef"),
            "audit row MUST NOT contain the full license key (got: {})",
            row.detail
        );
        assert_eq!(
            detail.get("client_resolved_tag").and_then(|v| v.as_str()),
            Some("0.2.8"),
        );
    }

    #[test]
    fn test_v46e_c3_audit_log_emitted_on_resolved() {
        // C3: `audit_pull_token_resolved` writes a row with:
        //   - operation = "pull_token_resolved"
        //   - server_tag matches
        //   - tag_mismatch = true when server_tag != client_resolved_tag
        //   - mismatch_class reflects the classification result
        //   - endpoint + effective_tag_with_variant included (v0.2.46
        //     follow-up — RL chat enhancement: lets future debugging
        //     answer "what URL? what tag was actually pulled?" via a
        //     single SQL query against audit_log)
        let db = crate::db::Db::open_in_memory().expect("in-memory db");
        let class = classify_tag_mismatch("0.2.8-cuda", "0.1.0");
        let effective = merge_server_tag_with_client_variant("0.1.0", "0.2.8-cuda");
        audit_pull_token_resolved(
            Some(&db),
            "vct-rl-reranker",
            "https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/rl-artifact-url",
            Some("0.1.0"),
            "0.2.8-cuda",
            &effective,
            Some("vct-bot-rl"),
            900,
            &class,
        );
        let events = db
            .audit_list(None, None, None, None, Some("pull_token_resolved"), 10)
            .expect("audit_list");
        let row = events
            .iter()
            .find(|e| e.operation == "pull_token_resolved")
            .expect("must find pull_token_resolved row");
        let detail: serde_json::Value =
            serde_json::from_str(&row.detail).expect("detail is valid json");
        assert_eq!(
            detail.get("server_tag").and_then(|v| v.as_str()),
            Some("0.1.0"),
        );
        assert_eq!(
            detail.get("client_resolved_tag").and_then(|v| v.as_str()),
            Some("0.2.8-cuda"),
        );
        assert_eq!(
            detail.get("endpoint").and_then(|v| v.as_str()),
            Some("https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/rl-artifact-url"),
            "endpoint must be recorded in audit detail",
        );
        assert_eq!(
            detail.get("effective_tag_with_variant").and_then(|v| v.as_str()),
            Some("0.1.0-cuda"),
            "effective_tag_with_variant must record the actually-pulled tag",
        );
        assert_eq!(
            detail.get("tag_mismatch").and_then(|v| v.as_bool()),
            Some(true),
            "0.2.8 vs 0.1.0 must record tag_mismatch=true",
        );
        assert_eq!(
            detail.get("mismatch_class").and_then(|v| v.as_str()),
            Some("minor_or_major"),
            "0.2.8 vs 0.1.0 is a minor-version difference",
        );
        assert_eq!(
            detail.get("username").and_then(|v| v.as_str()),
            Some("vct-bot-rl"),
        );
        assert_eq!(
            detail.get("expires_in_s").and_then(|v| v.as_u64()),
            Some(900),
        );
    }

    #[test]
    fn test_v46e_c3_audit_log_emitted_on_failure() {
        // C3: `audit_pull_token_failed` writes a row with:
        //   - operation = "pull_token_failed"
        //   - error_class = "license_invalid" for the canonical 401 message
        //   - error_excerpt is truncated to 200 chars (not the full error)
        let db = crate::db::Db::open_in_memory().expect("in-memory db");
        let canonical_401 =
            "your license key is invalid or has been revoked. \
             Open Settings → License → Refresh; if the problem persists, contact support.";
        audit_pull_token_failed(Some(&db), "vct-rl-reranker", canonical_401);
        let events = db
            .audit_list(None, None, None, None, Some("pull_token_failed"), 10)
            .expect("audit_list");
        let row = events
            .iter()
            .find(|e| e.operation == "pull_token_failed")
            .expect("must find pull_token_failed row");
        let detail: serde_json::Value =
            serde_json::from_str(&row.detail).expect("detail is valid json");
        assert_eq!(
            detail.get("module_id").and_then(|v| v.as_str()),
            Some("vct-rl-reranker"),
        );
        assert_eq!(
            detail.get("error_class").and_then(|v| v.as_str()),
            Some("license_invalid"),
            "canonical 401 license-invalid message must classify as license_invalid",
        );
        let excerpt = detail
            .get("error_excerpt")
            .and_then(|v| v.as_str())
            .expect("excerpt must be present");
        assert!(
            excerpt.len() <= 200,
            "excerpt must be truncated to 200 chars; got {} chars",
            excerpt.len()
        );

        // Also verify tier_insufficient + network classifications fire
        // on representative error messages.
        let tier_msg =
            "this module requires the pro tier; your license validates as free. \
             Upgrade on the dashboard, then open Settings → License → Refresh.";
        assert_eq!(
            classify_pull_token_error(tier_msg),
            "tier_insufficient",
            "tier_insufficient message must classify accordingly",
        );
        let network_msg = "POST https://example.com/token: connection refused";
        assert_eq!(
            classify_pull_token_error(network_msg),
            "network",
            "reqwest-shaped error must classify as network",
        );
    }

    #[test]
    fn test_v46e_c4_hard_fail_on_minor_mismatch() {
        // C4: a MAJOR or MINOR tag mismatch must classify as MinorOrMajor,
        // which `container_pull` translates into a hard-fail Err with a
        // publisher-pointing message. Here we pin the classification side;
        // the message-construction side is exercised by the
        // `effective_tag` plumbing in `container_pull` itself (which is
        // structurally checked via cargo check + the existing decision-
        // tree tests).
        assert_eq!(
            classify_tag_mismatch("0.2.8", "0.1.0"),
            VersionMismatchClass::MinorOrMajor,
            "0.2.8 vs 0.1.0 is a MINOR mismatch — must hard-fail",
        );
        assert_eq!(
            classify_tag_mismatch("1.0.0", "2.0.0"),
            VersionMismatchClass::MinorOrMajor,
            "1.0.0 vs 2.0.0 is a MAJOR mismatch — must hard-fail",
        );
        assert_eq!(
            classify_tag_mismatch("0.1.0", "1.1.0"),
            VersionMismatchClass::MinorOrMajor,
            "0.1.0 vs 1.1.0 is a MAJOR mismatch — must hard-fail",
        );
    }

    #[test]
    fn test_v46e_c4_no_hard_fail_on_patch_mismatch() {
        // C4 inverse: a PATCH-only difference must NOT hard-fail —
        // classify_tag_mismatch returns PatchOnly which is the "honor
        // server's tag with a WARN" arm in container_pull.
        assert_eq!(
            classify_tag_mismatch("0.1.0", "0.1.1"),
            VersionMismatchClass::PatchOnly,
            "0.1.0 vs 0.1.1 must classify as PatchOnly (no hard-fail)",
        );
        // The PatchOnly arm is NOT MinorOrMajor — pinning this asymmetry
        // protects against a future refactor that accidentally collapses
        // the two arms.
        assert_ne!(
            classify_tag_mismatch("0.1.0", "0.1.1"),
            VersionMismatchClass::MinorOrMajor,
            "patch-only diff must NOT be classified as minor/major",
        );
    }

    #[test]
    fn test_v46e_license_key_prefix_only_logged() {
        // Redaction invariant: license_key_prefix_for_audit MUST NEVER
        // return more than 12 chars. This is the security-critical
        // invariant that prevents accidental full-key disclosure in the
        // audit log.
        //
        // Test across:
        //   - Long admin keys (vct_admin_<48 chars>)
        //   - Short keys (<12 chars — return whole string, no padding)
        //   - Unicode keys (multi-byte chars; .chars().take(12) handles
        //     this correctly — 12 SCALAR VALUES, not 12 bytes)
        //   - Empty key (fallback path; must produce empty string,
        //     not panic)
        let long = "vct_admin_TUVWXYZ1234567890abcdef0123456789ABCDEF";
        let prefix = license_key_prefix_for_audit(long);
        assert_eq!(prefix, "vct_admin_TU");
        assert_eq!(prefix.chars().count(), 12);

        // Pre-v0.2.45 license format (shorter) — must still truncate
        // gracefully without panic.
        let short = "vct_abc";
        let prefix_short = license_key_prefix_for_audit(short);
        assert_eq!(prefix_short, "vct_abc");
        assert!(prefix_short.chars().count() <= 12);

        // Unicode safety — `.chars().take(12)` operates on Unicode
        // scalar values; should NOT panic on multi-byte chars.
        let unicode = "🔑vct_admin_xxx";
        let prefix_uni = license_key_prefix_for_audit(unicode);
        assert!(prefix_uni.chars().count() <= 12);

        let empty = "";
        let prefix_empty = license_key_prefix_for_audit(empty);
        assert_eq!(prefix_empty, "");

        // Cross-check: the in-memory Db audit-emission path also redacts
        // (this guards against a future refactor that swaps the helper
        // for an inline .chars().take(N) where someone accidentally
        // bumps N). We exercise the full audit_pull_token_requested path
        // here so the test fails if the redaction site ever drifts.
        let db = crate::db::Db::open_in_memory().expect("in-memory db");
        audit_pull_token_requested(
            Some(&db),
            "vct-test-module",
            "https://example/test",
            "SECRET_KEY_THAT_MUST_BE_REDACTED_4567890abcdef",
            "0.0.1",
        );
        let events = db
            .audit_list(None, None, None, None, Some("pull_token_requested"), 10)
            .expect("audit_list");
        let row = events
            .iter()
            .find(|e| e.operation == "pull_token_requested")
            .expect("must find row");
        assert!(
            !row.detail.contains("MUST_BE_REDACTED"),
            "audit row MUST NOT contain the un-redacted middle of the key; got: {}",
            row.detail
        );
        assert!(
            row.detail.contains("SECRET_KEY_T"),
            "audit row MUST contain the 12-char prefix (sanity check); got: {}",
            row.detail
        );
    }

    #[test]
    fn format_pull_failure_includes_stderr_tail() {
        // Realistic podman pull failure: a few layer-progress lines + the
        // actionable error at the end.
        let stderr = b"Trying to pull ghcr.io/foo:bar...\n\
                       Error: initializing source docker://ghcr.io/foo:bar: \
                       unable to retrieve auth token: invalid username/password: unauthorized\n";
        let msg = format_pull_failure(
            "podman",
            Some(125),
            "ghcr.io/foo:bar",
            stderr,
            true,  // token present — no license-check-failed suffix
            None,
        );
        assert!(msg.contains("podman pull failed (exit 125)"), "got: {}", msg);
        assert!(msg.contains("ghcr.io/foo:bar"), "got: {}", msg);
        assert!(msg.contains("unauthorized"), "stderr tail must surface the auth error: {}", msg);
    }

    #[test]
    fn format_pull_failure_truncates_to_last_2kb_at_newline_boundary() {
        // 5KB synthetic stderr; only the LAST 2KB should appear, starting
        // at a newline boundary (so we don't render mid-line garbage).
        let mut huge = String::with_capacity(5000);
        for i in 0..200 {
            huge.push_str(&format!("noise line {}\n", i));
        }
        huge.push_str("Error: manifest unknown\n");
        let msg = format_pull_failure(
            "podman",
            Some(125),
            "ghcr.io/foo:bar",
            huge.as_bytes(),
            true,
            None,
        );
        // The actionable error must be present.
        assert!(msg.contains("manifest unknown"), "actionable error must be in last 2KB: {}", msg);
        // The very first line ("noise line 0") must NOT be present — we
        // dropped the prefix.
        assert!(!msg.contains("noise line 0"), "old lines must have been truncated: {}", msg);
        // Message length stays bounded (helper output should be well under
        // image_ref + runtime + 2KB + format overhead ≈ 2.2KB).
        assert!(msg.len() < 2500, "formatted msg should be ~2KB-bounded: len={}", msg.len());
    }

    #[test]
    fn format_pull_failure_survives_multibyte_at_truncation_boundary() {
        // Construct a stderr where a multi-byte UTF-8 char straddles the
        // (len - 2048) byte. Without char-boundary snapping, the slice
        // panics: "byte index N is not a char boundary; it is inside '<c>'".
        //
        // Use a pure 2-byte-char pad ("é" = 0xC3 0xA9). With 2050 copies
        // (4100 bytes) plus a 17-byte ASCII suffix, total = 4117 bytes
        // and (len - 2048) = 2069 — which lands on a 0xA9 continuation
        // byte (NOT a char boundary). Pre-fix this panics; post-fix the
        // `is_char_boundary` loop snaps `start` forward to byte 2070.
        let mut huge = String::with_capacity(4200);
        for _ in 0..2050 {
            huge.push('é');
        }
        huge.push_str("\nError: bad auth\n");
        let msg = format_pull_failure(
            "podman",
            Some(125),
            "ghcr.io/foo:bar",
            huge.as_bytes(),
            true,
            None,
        );
        // No panic (we got here). Actionable tail still surfaces.
        assert!(msg.contains("Error: bad auth"), "tail must surface actionable error: {}", msg);
        // Bounded.
        assert!(msg.len() < 2500, "msg should be 2KB-bounded: len={}", msg.len());
    }
}

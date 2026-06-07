// SPDX-License-Identifier: AGPL-3.0-or-later
//
// l0_manifest_synth — Cold-start `ModuleManifest` synthesis from L0 catalog
// data (v0.2.33 B2).
//
// Background
// ----------
// Agent B's L0a refactor (v0.2.33) split the legacy `find_manifest` into:
//   * `resolve_install_metadata` — pre-install, returns the L0 install-slice
//   * `find_installed_manifest`  — post-install, reads the extracted
//     `~/.vct/modules/<id>/vct-module.json`
//
// The install path (`install_module_for_project`) currently uses
// `install_path_manifest_lookup` which tries (a) on-disk installed
// manifest and (b) the dev-affordance `paid-modules/<id>/vct-module.json`.
// Neither exists on a clean cold-start install on a real-user machine — so
// the launcher errored out with a "v0.2.34 follow-up" message.
//
// `installer_engine::run_install` consumes a `&ModuleManifest`. To drive
// `container_pull` from the L0 install-slice we need to construct a thin
// `ModuleManifest` from `L0CatalogModule`. That synthesized manifest is
// INTENTIONALLY MINIMAL: its lifespan is the install run only. Agent C's
// post-install `extract_manifest_from_image` REPLACES the on-disk file
// with the real manifest from inside the pulled image immediately after
// `container_pull` succeeds (`installer_engine.rs` step labelled
// `InstallStage::ExtractingManifest`). Everything that needs the full
// manifest (config-tab rendering, dispatcher, DB migrations) runs AFTER
// that replacement, against the real file — never against the synth.
//
// What the synth MUST carry
// -------------------------
// The fields that `installer_engine::run_install_inner` actually reads
// between entry and the extract step:
//   * `compatibility.min_launcher_version` (host-version gate)
//   * `install.method` (matches `ContainerPull`)
//   * `install.container.{image, tag_from_version, registry,
//      pull_token_endpoint, pull_token_method}` (drives the pull)
//   * `install.r#ref` (fallback tag when `tag_from_version = false`)
//   * `install.install_dir` (marker directory for container modules)
//   * `install.post_install` (default empty; real manifest from extract
//      replaces this BEFORE the loop on line 242 runs — but we still need
//      a Vec, hence `Vec::new()`)
//   * `runtime.r#type` ("container" — drives `resolve_variant_tag` gating
//      and the per-project supervisor branch in
//      `install_module_for_project` line 1172)
//   * `runtime.gpu_image_variants` (per-mode tag dispatch when
//      `decide_gpu_mode` returns Cuda/Rocm)
//   * `id`, `name`, `version`, `category`, `tags`, `description`
//      (read by `install_module_for_project` for the audit row + the
//      `ModuleInstallRow` we return to the renderer)
//   * `license.{required, min_orchestrator_tier, variant_ids}` (license
//      gate at line 1110)
//
// What the synth MUST NOT carry (defaulted, replaced by extract)
// --------------------------------------------------------------
//   * `gui.config_tab` — pre-install we don't have it. Default `None`.
//   * `db.migrations_dir`, `db.namespace` — applied AFTER extract.
//     Default `None`.
//   * `runtime.command`, `runtime.args`, `runtime.ports`, `runtime.volumes`,
//     `runtime.image_ref`, `runtime.container_name_template`, etc. —
//     the per-project supervisor reads these from the extracted manifest
//     when `start_container_after_install` runs (line 1174 in modules.rs),
//     which is AFTER `run_install` returns + AFTER extract has written
//     the real file. The supervisor calls `find_installed_manifest`
//     itself, not the synth. So we leave them empty / None.
//   * `secrets`, `settings`, `provides`, `consumes`, `mcp_registration`,
//     `setup_wizard`, `upgrade`, `telemetry`, `uninstall` — pre-install
//     none of these are read by `run_install_inner` before extract.
//
// Failure mode
// ------------
// If L0 lacks a non-defaultable field that we genuinely need (today: any
// of the install.container fields above, all required by the L0Install
// struct itself), serde-de of the L0 envelope would have already failed
// at the `module_catalog_client::parse_response_text` step — long before
// the synth runs. So the only "missing required field" case the synth
// itself must guard is `image.is_empty()` (a publisher who literally
// shipped `""` for the image — schema-valid wire shape but semantically
// useless). That's the explicit `L0 install-slice lacks required field
// install.container.image` error.
//
// We do NOT call `ModuleManifest::from_json` on the synthesized struct:
//   * the synth is in-memory only, no JSON round-trip
//   * `from_json`'s validation includes "license.required=true AND
//      variant_ids.is_empty() AND tier=free → reject" which we don't want
//      to apply to L0 records (a Pro module with `license_required=true`
//      and tier=pro is valid, and L0 doesn't carry variant_ids by default)
//   * the run_install_inner code path doesn't re-validate against from_json
//      anyway — it consumes the struct directly.
//
// Future evolution
// ----------------
// If a future L0 schema_version exposes more install-time fields (e.g.
// pre-flight `secrets` declarations a publisher wants the launcher to
// prompt for before the pull), extend this synthesis additively — never
// require that the synth grow to handle full config-tab rendering, that
// path belongs in the post-extract manifest.

use crate::commands::module_catalog_client::L0CatalogModule;

use crate::manifest::{
    Compatibility, ContainerInstallBlock, GpuImageVariants, InstallBlock, InstallMethod,
    InstallScope, LicenseBlock, ModuleCategory, ModuleManifest, Requirements, RuntimeBlock,
};

/// Synthesise a thin `ModuleManifest` from an L0 catalog record so the
/// installer engine can drive `container_pull` BEFORE the real manifest
/// has been extracted from the pulled image. See module docs above for
/// the design rationale.
///
/// Returns Err with a structured message when the L0 record lacks a
/// load-bearing field that the installer engine genuinely needs. Today
/// that's only `install.container.image` — every other required slot is
/// either guaranteed by `L0CatalogModule`'s serde shape or can be
/// reasonably defaulted from the L0 hints.
///
/// **Lifespan**: the synthesised manifest is IN-MEMORY ONLY. After
/// `container_pull` succeeds, Agent C's `extract_manifest_from_image`
/// writes the REAL manifest to `~/.vct/modules/<id>/vct-module.json`
/// from inside the pulled image. The synth is never persisted, never
/// rendered for a config-tab, never read by the dispatcher.
pub fn synthesize_install_manifest_from_l0(
    l0: &L0CatalogModule,
) -> Result<ModuleManifest, String> {
    // ─── Guard: load-bearing fields ──────────────────────────────────
    //
    // L0Install requires image / pull_token_endpoint via serde, but an
    // EMPTY string would pass deserialisation while breaking the pull
    // step (podman would try to pull `:tag` and 400). Guard with an
    // explicit error so the user sees "publisher's L0 entry is
    // incomplete" instead of a confusing podman parse failure.
    if l0.install.container.image.is_empty() {
        return Err(format!(
            "L0 install-slice lacks required field install.container.image \
             for module {} — publisher's L0 entry is incomplete, ask them \
             to republish via the module-catalog edge function",
            l0.id,
        ));
    }

    // ─── Category mapping ───────────────────────────────────────────
    //
    // L0 carries `category` as a free-form string (the wire format
    // doesn't enforce the kebab-case enum). Map to the typed enum;
    // unknown values default to `PaidIndependent` (the safest "paid
    // module of unknown affiliation" bucket — neither core nor
    // community).
    let category = match l0.category.as_str() {
        "core" => ModuleCategory::Core,
        "paid-orchestrator" => ModuleCategory::PaidOrchestrator,
        "paid-independent" => ModuleCategory::PaidIndependent,
        "community" => ModuleCategory::Community,
        _ => ModuleCategory::PaidIndependent,
    };

    // ─── Compatibility ──────────────────────────────────────────────
    //
    // L0Compatibility.hosts is a Vec<String>. The downstream consumers
    // (`installer_engine::run_install_inner` for the launcher-version
    // gate, `install_module_for_project` for the host-match gate)
    // both treat hosts AS-IS without re-validating against the
    // closed-set {base, mao, orchestrator_root, standalone}. We don't
    // re-validate here either: a malformed L0 hosts list would surface
    // as a host-incompat error at install time, which is the right
    // message.
    let compatibility = Compatibility {
        hosts: l0.compatibility.hosts.clone(),
        min_launcher_version: l0.compatibility.min_launcher_version.clone(),
    };

    // ─── License gate ───────────────────────────────────────────────
    //
    // LicenseBlock.r#type is Option<String>; L0 doesn't carry a
    // license type discriminator (subscription vs perpetual etc.) —
    // leave as None. The is_module_licensed check reads
    // {required, variant_ids, min_orchestrator_tier} only; the type
    // field is informational.
    //
    // trial_days: L0 carries Option<u32>; the manifest field is u32
    // with #[serde(default)] (so deserialises to 0). Map None → 0.
    let license = LicenseBlock {
        required: l0.license_required,
        r#type: None,
        variant_ids: l0.license_variant_ids.clone(),
        min_orchestrator_tier: l0.min_orchestrator_tier.clone(),
        trial_days: l0.trial_days.unwrap_or(0),
    };

    // ─── Requirements ───────────────────────────────────────────────
    //
    // L0Requirements is optional (publishers omit it when their module
    // has trivial requirements). When absent, Requirements::default()
    // is the "no specific requirements" state, which matches the
    // installer's behaviour for pre-v0.2.33 manifests that never
    // declared the block.
    let requirements = match &l0.requirements {
        Some(r) => Requirements {
            os: r.os.clone(),
            python: None,
            node: None,
            memory_mb: r.memory_mb.unwrap_or(0),
            disk_mb: r.disk_mb.unwrap_or(0),
            network: Vec::new(),
            gpu: r.gpu,
            depends_on: Vec::new(),
        },
        None => Requirements::default(),
    };

    // ─── Install block ──────────────────────────────────────────────
    //
    // The pull is the ONLY thing the installer does pre-extract. We
    // map L0Install → InstallBlock 1:1 for the container fields, hard-
    // pin `method = ContainerPull` (L0 only advertises one install
    // method today — `"container_pull"`; future methods land via
    // explicit L0 schema_version bumps), and leave post_install empty
    // (the real manifest from extract carries the post_install
    // commands; for the pull step there's nothing to run).
    //
    // `r#ref` is None: when tag_from_version=true (the v0.2.7 RL case),
    // the tag comes from manifest.version. When tag_from_version=false,
    // installer_engine falls back to manifest.install.r#ref then to
    // "latest" — L0 doesn't carry an explicit ref, so we leave None
    // and rely on the fallback chain.
    //
    // `install_dir` uses the same default the manifest schema does
    // (`{VCT_MODULES}/{MODULE_ID}`). For container modules this is a
    // marker directory only (real install_dir post-extract may differ
    // if the extracted manifest specifies something else).
    let container_block = ContainerInstallBlock {
        image: l0.install.container.image.clone(),
        tag_from_version: l0.install.container.tag_from_version,
        registry: l0.install.container.registry.clone(),
        pull_token_endpoint: l0.install.container.pull_token_endpoint.clone(),
        pull_token_method: l0.install.container.pull_token_method.clone(),
        // L0 doesn't currently carry weight-rotation hints; leave the
        // optional gateway off here and rely on the post-extract
        // manifest to wire it up (the rotate_weights_endpoint URL is
        // sensitive enough that it belongs inside the image, not in the
        // public L0 envelope). Daily-poll behaviour is gated on the
        // extracted manifest's flags, not the synth's.
        rotate_weights: false,
        rotate_weights_endpoint: None,
    };
    let install = InstallBlock {
        method: InstallMethod::ContainerPull,
        source: None,
        r#ref: None,
        install_dir: "{VCT_MODULES}/{MODULE_ID}".into(),
        post_install: Vec::new(),
        container: Some(container_block),
        // v0.2.49 Stream A: synth defaults to per-project. The L0
        // catalog slice today carries only install-time metadata; the
        // post-pull extracted manifest is the source of truth for
        // `install.scope`. Default keeps pre-v0.2.49 behaviour for
        // every module whose extracted manifest hasn't shipped yet.
        scope: InstallScope::PerProject,
    };

    // ─── Runtime block ──────────────────────────────────────────────
    //
    // Required fields: `r#type: String`, `command: String`. For
    // container_pull modules `type = "container"` is the only sensible
    // value (the per-project supervisor's `start_container_after_install`
    // branches on it at modules.rs:1172). `command` is consumed during
    // container START (image entrypoint), not during the install pull,
    // and the post-extract manifest replaces this struct entirely
    // before the supervisor reads `command`. Empty string is safe.
    //
    // `gpu_image_variants`: when L0 advertises runtime_hints with the
    // canonical {cpu, cuda, rocm} keys, surface them so
    // `resolve_variant_tag` can pick the right tag for the user's GPU
    // mode. Missing keys default to the version-only tag (legacy
    // single-tag path) — same back-compat behaviour as Stage 1B.
    let gpu_image_variants = l0
        .runtime_hints
        .as_ref()
        .and_then(|hints| {
            let cpu = hints.gpu_image_variants.get("cpu").cloned();
            let cuda = hints.gpu_image_variants.get("cuda").cloned();
            let rocm = hints.gpu_image_variants.get("rocm").cloned();
            match (cpu, cuda, rocm) {
                (Some(cpu), Some(cuda), Some(rocm)) => {
                    Some(GpuImageVariants { cpu, cuda, rocm })
                }
                // Partial variants — fall back to the legacy single-tag
                // path. GpuImageVariants requires all three; we can't
                // synthesise it from a partial set.
                _ => None,
            }
        });

    let runtime = RuntimeBlock {
        r#type: "container".into(),
        command: String::new(),
        args: Vec::new(),
        platform_command: Default::default(),
        cwd: None,
        env_from_secrets: Vec::new(),
        env_from_settings: Vec::new(),
        env_fixed: Default::default(),
        health_check: None,
        auto_restart: false,
        log_file: None,
        container_name_template: None,
        image_ref: None,
        ports: Vec::new(),
        volumes: Vec::new(),
        env_derived: Default::default(),
        log_path_template: None,
        min_gpu_vram_gb: None,
        // L0 carries `requirements.gpu: bool` (whether the module
        // REQUIRES a GPU). The synth maps this to `gpu_optional = !gpu`
        // when requirements are present: gpu=false → gpu_optional=true
        // (CPU acceptable). When requirements are absent we default to
        // `gpu_optional=false` (matches the manifest's serde default).
        gpu_optional: l0
            .requirements
            .as_ref()
            .map(|r| !r.gpu)
            .unwrap_or(false),
        gpu_image_variants,
    };

    Ok(ModuleManifest {
        manifest_version: 1,
        id: l0.id.clone(),
        name: l0.name.clone(),
        version: l0.version.clone(),
        description: l0.description.clone(),
        publisher: if l0.publisher.is_empty() {
            None
        } else {
            Some(l0.publisher.clone())
        },
        homepage: if l0.homepage.is_empty() {
            None
        } else {
            Some(l0.homepage.clone())
        },
        repository: None,
        icon: None,
        category,
        tags: l0.tags.clone(),
        compatibility,
        license,
        requirements,
        install,
        secrets: Vec::new(),
        settings: Vec::new(),
        runtime,
        mcp_registration: None,
        setup_wizard: None,
        upgrade: None,
        telemetry: None,
        uninstall: None,
        provides: Vec::new(),
        consumes: Vec::new(),
        // Synthesized manifests intentionally carry no `gui.config_tab`
        // — the pre-install renderer never reads it (catalog tile uses
        // L0 directly), and the post-extract manifest will carry the
        // real one. Leaving as None.
        gui: None,
        // Same logic for `db`: migrations apply after extract, never
        // against the synth.
        db: None,
    })
}

// ──────────────────────────────────────────────────────────────────────────
// Tests
// ──────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::module_catalog_client::{
        L0Compatibility, L0Install, L0InstallContainer, L0Requirements, L0RuntimeHints,
    };
    use std::collections::HashMap;

    /// Canonical L0 fixture for the v0.2.7 RL reranker — mirrors what the
    /// edge function returns on production. Keeping a single shared
    /// fixture across all synth tests means a future L0-shape evolution
    /// only needs one update point.
    fn canonical_l0_rl() -> L0CatalogModule {
        let mut variants = HashMap::new();
        variants.insert("cpu".into(), "{version}-cpu".into());
        variants.insert("cuda".into(), "{version}-cuda".into());
        variants.insert("rocm".into(), "{version}-rocm".into());

        L0CatalogModule {
            id: "vct-rl-reranker".into(),
            name: "RL Reranker".into(),
            version: "0.2.7".into(),
            description: "RL-based reranker for KG retrieval".into(),
            category: "paid-independent".into(),
            tags: vec!["pro".into(), "reranking".into()],
            homepage: "https://example/rl".into(),
            publisher: "VibeCoded Tools".into(),
            license_required: true,
            min_orchestrator_tier: "pro".into(),
            license_variant_ids: vec!["lemonsqueezy-rl-pro".into()],
            trial_days: Some(7),
            compatibility: L0Compatibility {
                hosts: vec!["base".into(), "mao".into(), "orchestrator_root".into()],
                min_launcher_version: Some("0.2.33".into()),
            },
            install: L0Install {
                method: "container_pull".into(),
                container: L0InstallContainer {
                    image: "ghcr.io/hotak92/vct-rl-reranker".into(),
                    tag_from_version: true,
                    registry: Some("ghcr.io".into()),
                    pull_token_endpoint: "https://example/pull-token".into(),
                    pull_token_method: "POST".into(),
                },
                scope: crate::manifest::InstallScope::PerProject,
            },
            requirements: Some(L0Requirements {
                os: vec!["linux".into(), "macos".into(), "windows".into()],
                memory_mb: Some(2048),
                disk_mb: Some(1500),
                gpu: false,
            }),
            runtime_hints: Some(L0RuntimeHints {
                gpu_image_variants: variants,
            }),
            deprecated: false,
            deprecation_message: String::new(),
            deprecation_eol_date: String::new(),
            deprecation_migration_url: String::new(),
            post_install_manifest_path: "vct-module.json".into(),
        }
    }

    /// Test 1: every L0 display field round-trips into the synthesized
    /// manifest verbatim. The audit row + ModuleInstallRow returned to
    /// the renderer depend on id / name / version / description being
    /// present and accurate — otherwise the GUI's spinner would render
    /// against placeholder strings during the install.
    #[test]
    fn synthesize_install_manifest_carries_l0_display_fields() {
        let l0 = canonical_l0_rl();
        let m = synthesize_install_manifest_from_l0(&l0).expect("must synthesize");
        assert_eq!(m.id, "vct-rl-reranker", "id round-trip");
        assert_eq!(m.name, "RL Reranker", "name round-trip");
        assert_eq!(m.version, "0.2.7", "version round-trip");
        assert_eq!(
            m.description, "RL-based reranker for KG retrieval",
            "description round-trip"
        );
        assert_eq!(m.tags, vec!["pro".to_string(), "reranking".to_string()]);
        assert_eq!(m.category, ModuleCategory::PaidIndependent);
        assert_eq!(m.homepage.as_deref(), Some("https://example/rl"));
        assert_eq!(m.publisher.as_deref(), Some("VibeCoded Tools"));
        // manifest_version always 1 for v0.2.33 synthesis (matches the
        // first published manifest_version any real RL module ships).
        assert_eq!(m.manifest_version, 1);
    }

    /// Test 2: the license block carries the L0 gate verbatim — every
    /// field the catalog's `is_module_licensed_v2` check reads must
    /// match the L0 record. A drift here would cause the install path
    /// to gate differently from the catalog tile (button says
    /// "Install" but the install errors with "license required" — or
    /// vice versa).
    #[test]
    fn synthesize_install_manifest_carries_l0_license_block() {
        let l0 = canonical_l0_rl();
        let m = synthesize_install_manifest_from_l0(&l0).expect("must synthesize");
        assert!(m.license.required, "license.required carries from L0");
        assert_eq!(
            m.license.min_orchestrator_tier, "pro",
            "min_orchestrator_tier carries from L0"
        );
        assert_eq!(
            m.license.variant_ids,
            vec!["lemonsqueezy-rl-pro".to_string()],
            "variant_ids carry from L0"
        );
        assert_eq!(
            m.license.trial_days, 7,
            "trial_days unwraps Some(7) → 7"
        );
        assert!(
            m.license.r#type.is_none(),
            "L0 doesn't carry license.type — must be None on the synth"
        );
    }

    /// Test 3: the install block carries the container reference
    /// verbatim — the field installer_engine::container_pull reads is
    /// `manifest.install.container.image`, NOT L0's nested struct.
    /// Drift here would mean podman tries to pull the wrong image (or
    /// fails at the token-gateway with a 400).
    #[test]
    fn synthesize_install_manifest_carries_l0_install_container() {
        let l0 = canonical_l0_rl();
        let m = synthesize_install_manifest_from_l0(&l0).expect("must synthesize");
        assert_eq!(
            m.install.method,
            InstallMethod::ContainerPull,
            "install method pinned to ContainerPull (L0 only advertises this method today)"
        );
        let c = m.install.container.as_ref().expect("container block present");
        assert_eq!(c.image, "ghcr.io/hotak92/vct-rl-reranker");
        assert!(c.tag_from_version);
        assert_eq!(c.registry.as_deref(), Some("ghcr.io"));
        assert_eq!(c.pull_token_endpoint, "https://example/pull-token");
        assert_eq!(c.pull_token_method, "POST");
        // The synth never carries weight-rotation hints — the real
        // (extracted) manifest does. v0.2.33 design choice: the
        // rotate_weights endpoint URL is sensitive enough to stay
        // inside the image, not in the public L0 envelope.
        assert!(!c.rotate_weights);
        assert!(c.rotate_weights_endpoint.is_none());

        // RuntimeBlock.gpu_image_variants: the L0 hints map should
        // round-trip into the typed struct.
        let variants = m
            .runtime
            .gpu_image_variants
            .as_ref()
            .expect("gpu_image_variants synthesised from L0 runtime_hints");
        assert_eq!(variants.cpu, "{version}-cpu");
        assert_eq!(variants.cuda, "{version}-cuda");
        assert_eq!(variants.rocm, "{version}-rocm");
    }

    /// Test 4: a malformed L0 record (empty container.image) must
    /// surface as a structured Err — the user sees a clear "publisher
    /// L0 entry is incomplete" toast instead of a confusing podman
    /// parse failure. The error message must name the offending field
    /// so the publisher can fix it from the error alone.
    #[test]
    fn synthesize_install_manifest_returns_err_on_missing_required_field() {
        let mut l0 = canonical_l0_rl();
        l0.install.container.image = String::new();
        let result = synthesize_install_manifest_from_l0(&l0);
        let err = result.expect_err("empty image must Err");
        assert!(
            err.contains("install.container.image"),
            "error message must name the missing field, got: {}",
            err,
        );
        assert!(
            err.contains("vct-rl-reranker"),
            "error message must name the module so the publisher can find their L0 entry, got: {}",
            err,
        );
    }

    // ─── Auxiliary coverage (not in the 7-test spec but cheap to add) ─

    /// Default for absent L0 hints: gpu_image_variants must be None
    /// (legacy single-tag path), gpu_optional must default to false.
    #[test]
    fn synthesize_install_manifest_handles_absent_runtime_hints() {
        let mut l0 = canonical_l0_rl();
        l0.runtime_hints = None;
        l0.requirements = None;
        let m = synthesize_install_manifest_from_l0(&l0).expect("must synthesize");
        assert!(
            m.runtime.gpu_image_variants.is_none(),
            "absent runtime_hints → no GPU variant dispatch"
        );
        // Absent requirements → gpu_optional defaults to false (matches
        // RuntimeBlock's serde default for the field).
        assert!(!m.runtime.gpu_optional);
    }

    /// Partial gpu_image_variants (missing cuda) → fall back to legacy
    /// single-tag path. The GpuImageVariants struct requires all three
    /// keys; we can't synthesise it from a partial set.
    #[test]
    fn synthesize_install_manifest_falls_back_when_gpu_variants_partial() {
        let mut l0 = canonical_l0_rl();
        if let Some(hints) = l0.runtime_hints.as_mut() {
            hints.gpu_image_variants.remove("cuda");
        }
        let m = synthesize_install_manifest_from_l0(&l0).expect("must synthesize");
        assert!(
            m.runtime.gpu_image_variants.is_none(),
            "partial variants → fall back to single-tag (resolve_variant_tag returns base_tag)"
        );
    }

    /// An L0 with `requirements.gpu = true` → `runtime.gpu_optional =
    /// false` (the module REQUIRES a GPU). Inverse pinning for the
    /// gpu_optional mapping.
    #[test]
    fn synthesize_install_manifest_maps_gpu_required_to_gpu_optional_false() {
        let mut l0 = canonical_l0_rl();
        if let Some(r) = l0.requirements.as_mut() {
            r.gpu = true;
        }
        let m = synthesize_install_manifest_from_l0(&l0).expect("must synthesize");
        assert!(
            !m.runtime.gpu_optional,
            "gpu=true → gpu_optional=false (module requires a GPU)"
        );
    }
}

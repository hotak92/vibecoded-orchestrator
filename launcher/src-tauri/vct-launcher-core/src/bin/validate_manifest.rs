// SPDX-License-Identifier: AGPL-3.0-or-later
//! `validate-manifest` — round-trip every `vct-module.json` arg through
//! [`vct_launcher_core::manifest::ModuleManifest::from_json`].
//!
//! Wired into CI by `.github/workflows/manifest-validate.yml` (v0.2.33,
//! Agent F, C2). The job runs the bin against every committed paid-module
//! manifest fixture; any deserialisation failure fails the PR.
//!
//! **Strict mode is mandatory in CI**: `VCT_LAUNCHER_STRICT_MANIFEST=1`
//! is set in the workflow env so the lenient `Unsupported` ConfigControl
//! fallback (Agent D, v0.2.33) DOESN'T mask schema errors during
//! validation. A real-user launcher running this same parse path would
//! be lenient; CI is strict because the goal here is to catch typos /
//! genuinely-unknown kinds at PR time, not at install time on a paying
//! customer's machine. The bin honours the env var via the same
//! `strict_manifest_mode()` plumbing the runtime parser uses — no
//! special wiring required.
//!
//! Why this exists: v0.2.32 validation shipped a manifest schema bug
//! where the the v0.2.7 module manifest declared a `tauri_command` step
//! kind that the launcher's `ActionDescriptor` enum didn't know about.
//! The parse silently failed → the catalog tile showed a stale v0.1.1
//! placeholder for weeks. With this CI gate, the same class of bug
//! becomes a one-line PR diff failure instead of a customer-facing
//! incident.
//!
//! ## Print policy
//!
//! EVERY `println!` / `eprintln!` in this file is CLI OUTPUT, not a
//! diagnostic, and is annotated `// [vct-print-contract]` for the
//! no-bare-prints ratchet. This binary is a standalone CI tool whose
//! stdout/stderr report (`[OK]` / `[FAIL]` / `[error]` / `[deprecation]`
//! lines) IS its result; it installs no `tracing` subscriber and must
//! not, since a log level could then suppress the very findings the PR
//! gate exists to surface.
//!
//! Usage:
//!   validate-manifest [--known-module <id> ...] path/to/vct-module.json [more.json ...]
//!   (glob expansion is the shell's job, not the bin's.)
//!
//! Dependencies (v0.2.97, review R6 round 2): every `requirements.depends_on`
//! id must be a KNOWN module id — a bundled core module (embedded in this
//! binary), another manifest validated in the same run, or an id named with
//! `--known-module` (a module published only in the catalog, which this
//! offline tool cannot fetch). An unknown id fails the file: the launcher
//! refuses to install a module whose dependency is absent
//! (`vct_launcher_core::module_deps`), so a dependency nothing can provide
//! makes the module uninstallable.
//!
//! Exit codes:
//!   0 — every file parsed cleanly.
//!   1 — at least one file failed to read or parse.
//!   2 — usage error (no args, or `--help`).
//!
//! Soft-pass on empty arg list AFTER `--allow-empty`: the CI job uses
//! `--allow-empty` so the gate doesn't fail when the dev-only
//! `paid-modules/` dir isn't committed (real-world public repo state).
//! The launcher-internal CI fixture path is always present, so when
//! both are checked the empty case is genuinely degenerate.

use std::collections::BTreeSet;
use std::path::PathBuf;
use std::process::ExitCode;

use vct_launcher_core::manifest::{ModuleManifest, WarningSeverity};
use vct_launcher_core::module_deps;

fn print_usage() {
    // [vct-print-contract] CLI output, not diagnostics.
    eprintln!(
        "usage: validate-manifest [--allow-empty] [--known-module <id> ...] <manifest.json> [...]\n\
         \n\
         Validates one or more vct-module.json files against the launcher's\n\
         ModuleManifest schema. Exits 0 on full success, 1 on any failure,\n\
         2 on usage error.\n\
         \n\
         Env:\n\
           VCT_LAUNCHER_STRICT_MANIFEST=1  — reject unknown ConfigControl\n\
                                              kinds (recommended in CI).\n\
         \n\
         Flags:\n\
           --allow-empty  — exit 0 when no manifest paths are passed (used\n\
                            by the CI job to handle repos without paid-modules/).\n\
           --known-module <id>  — treat <id> as a known module for\n\
                            requirements.depends_on (a catalog-only module).\n\
                            Bundled ids and the other files of this run are\n\
                            always known.\n\
           --help, -h     — print this message and exit 2.\n"
    );
}

fn main() -> ExitCode {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|a| a == "--help" || a == "-h") {
        print_usage();
        return ExitCode::from(2);
    }
    let allow_empty = if let Some(pos) = args.iter().position(|a| a == "--allow-empty") {
        args.remove(pos);
        true
    } else {
        false
    };
    let mut known: BTreeSet<String> = module_deps::bundled_module_ids().into_iter().collect();
    let mut rest = Vec::new();
    let mut iter = args.into_iter();
    while let Some(arg) = iter.next() {
        if arg == "--known-module" {
            match iter.next() {
                Some(id) => {
                    known.insert(id);
                }
                None => {
                    print_usage();
                    return ExitCode::from(2);
                }
            }
        } else {
            rest.push(arg);
        }
    }
    let paths: Vec<PathBuf> = rest.into_iter().map(PathBuf::from).collect();
    // Every manifest of this run is known to the others (read errors are
    // reported by `validate_one` below).
    for path in &paths {
        if let Ok(m) = std::fs::read_to_string(path)
            .map_err(|e| e.to_string())
            .and_then(|raw| ModuleManifest::from_json(&raw))
        {
            known.insert(m.id);
        }
    }

    if paths.is_empty() {
        if allow_empty {
            // [vct-print-contract] CLI output, not diagnostics.
            println!("[skip] no manifest paths provided (--allow-empty)");
            return ExitCode::SUCCESS;
        }
        print_usage();
        return ExitCode::from(2);
    }

    let mut errors = 0usize;
    let mut ok = 0usize;
    for path in &paths {
        match validate_one(path, &known) {
            Ok(warnings) => {
                // NEW-3.D (2026-05-28): print deprecation warnings even on
                // parse-OK manifests. Exit 0 for deprecations only (backward
                // compatible with existing RL Reranker manifests). Exit 1 only
                // when Error-severity warnings are present.
                let has_errors = warnings.iter()
                    .any(|w| w.severity == WarningSeverity::Error);
                if has_errors {
                    // Error-severity: print + count as failure.
                    for w in &warnings {
                        let prefix = match w.severity {
                            WarningSeverity::Error       => "[error]",
                            WarningSeverity::Deprecation => "[deprecation]",
                        };
                        // [vct-print-contract] CLI output, not diagnostics.
                        println!("{} {}: {}: {}", prefix, path.display(), w.field, w.message);
                    }
                    // [vct-print-contract] CLI output, not diagnostics.
                    eprintln!("[FAIL] {}: manifest contract validation failed", path.display());
                    errors += 1;
                } else {
                    // No errors. Print any deprecation warnings, then OK.
                    for w in &warnings {
                        // [vct-print-contract] CLI output, not diagnostics.
                        println!("[deprecation] {}: {}: {}", path.display(), w.field, w.message);
                    }
                    // [vct-print-contract] CLI output, not diagnostics.
                    println!("[OK]   {}", path.display());
                    ok += 1;
                }
            }
            Err(msg) => {
                // [vct-print-contract] CLI output, not diagnostics.
                eprintln!("[FAIL] {}: {}", path.display(), msg);
                errors += 1;
            }
        }
    }

    // [vct-print-contract] CLI output, not diagnostics.
    println!(
        "\nvalidate-manifest: {} ok, {} failed (of {} total)",
        ok,
        errors,
        paths.len()
    );
    if errors > 0 {
        // Hint at the most common cause so the PR author isn't left
        // wondering. The serde error chain usually points at the
        // offending field directly.
        // [vct-print-contract] CLI output, not diagnostics.
        eprintln!(
            "\nHint: if a paid module shipped a NEW control kind / step kind that\n\
             this launcher version doesn't know about, the launcher itself needs\n\
             to be updated first. Lenient mode (VCT_LAUNCHER_STRICT_MANIFEST=0)\n\
             would render the unknown kind as a placeholder at runtime, but CI\n\
             intentionally runs strict so the gap is caught at PR time.\n\
             \n\
             For manifest contract errors (missing install.container.image etc.),\n\
             see the [error] lines above and update the module manifest."
        );
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

// NEW-3.D (2026-05-28): returns Ok(warnings) on parse success, Err(msg) on
// parse failure. Warnings from validate_for_container_start are non-fatal
// at the parse level — the caller decides whether to block on Error severity.
fn validate_one(
    path: &PathBuf,
    known: &BTreeSet<String>,
) -> Result<Vec<vct_launcher_core::manifest::ManifestWarning>, String> {
    let raw = std::fs::read_to_string(path).map_err(|e| format!("read error: {}", e))?;
    let manifest = ModuleManifest::from_json(&raw)?;
    let unknown = module_deps::unknown_dependencies(&manifest, known);
    if !unknown.is_empty() {
        return Err(format!(
            "requirements.depends_on names module id(s) that are not known: {} \
             (known: the bundled core modules, the other manifests of this run, \
             and any --known-module id)",
            unknown.join(", ")
        ));
    }
    let warnings = manifest.validate_for_container_start();
    Ok(warnings)
}

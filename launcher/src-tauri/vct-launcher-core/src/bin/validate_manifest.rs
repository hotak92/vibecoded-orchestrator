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
//! Why this exists: v0.2.32 dogfooding shipped a manifest schema bug
//! where the RL chat's v0.2.7 manifest declared a `tauri_command` step
//! kind that the launcher's `ActionDescriptor` enum didn't know about.
//! The parse silently failed → the catalog tile showed a stale v0.1.1
//! placeholder for weeks. With this CI gate, the same class of bug
//! becomes a one-line PR diff failure instead of a customer-facing
//! incident.
//!
//! Usage:
//!   validate-manifest path/to/vct-module.json [more.json ...]
//!   (glob expansion is the shell's job, not the bin's.)
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

use std::path::PathBuf;
use std::process::ExitCode;

use vct_launcher_core::manifest::ModuleManifest;

fn print_usage() {
    eprintln!(
        "usage: validate-manifest [--allow-empty] <manifest.json> [...]\n\
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
    let paths: Vec<PathBuf> = args.into_iter().map(PathBuf::from).collect();

    if paths.is_empty() {
        if allow_empty {
            println!("[skip] no manifest paths provided (--allow-empty)");
            return ExitCode::SUCCESS;
        }
        print_usage();
        return ExitCode::from(2);
    }

    let mut errors = 0usize;
    let mut ok = 0usize;
    for path in &paths {
        match validate_one(path) {
            Ok(()) => {
                println!("[OK]   {}", path.display());
                ok += 1;
            }
            Err(msg) => {
                eprintln!("[FAIL] {}: {}", path.display(), msg);
                errors += 1;
            }
        }
    }

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
        eprintln!(
            "\nHint: if a paid module shipped a NEW control kind / step kind that\n\
             this launcher version doesn't know about, the launcher itself needs\n\
             to be updated first. Lenient mode (VCT_LAUNCHER_STRICT_MANIFEST=0)\n\
             would render the unknown kind as a placeholder at runtime, but CI\n\
             intentionally runs strict so the gap is caught at PR time."
        );
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

fn validate_one(path: &PathBuf) -> Result<(), String> {
    let raw = std::fs::read_to_string(path).map_err(|e| format!("read error: {}", e))?;
    ModuleManifest::from_json(&raw).map(|_| ()).map_err(|e| e)
}

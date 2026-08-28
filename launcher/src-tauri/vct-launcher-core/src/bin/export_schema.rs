// SPDX-License-Identifier: AGPL-3.0-or-later
//! `export-schema` — print the JSON Schema (Draft 2020-12 shape) for
//! [`vct_launcher_core::manifest::ModuleManifest`] to stdout.
//!
//! Wired into CI by `.github/workflows/manifest-validate.yml`: a job runs
//! this binary, diffs the output against the committed copy at
//! `docs/schemas/vct-module.schema.json`, and fails the PR if they
//! diverge. This forces PR authors who touch `manifest.rs` to also
//! refresh the schema artifact — both repos (launcher + paid-module / future
//! paid modules) consume the SAME schema for validation, so drift kills
//! everyone downstream.
//!
//! The schema is generated via `schemars 0.8`. Notes on shape:
//!   - Most types use `#[derive(JsonSchema)]` — straightforward.
//!   - `ConfigControl` has a custom `Deserialize` impl (lenient fallback
//!     to `Unsupported` for forward-compat). The schema describes the
//!     STRICT-mode shape: known variants only. `Unsupported` is a
//!     runtime-only receptacle, never a thing module authors should
//!     declare in their manifest.
//!   - `SelectOption` has a custom `Deserialize` that accepts bare
//!     strings AND structured objects. The schema describes the
//!     structured form only — bare-string back-compat is documented in
//!     the schema's `description` so publishers know it's tolerated by
//!     the parser even though it's not blessed.
//!   - `serde_json::Value` fields (e.g. `TauriCommand.args`, `Setting.default`,
//!     `mcp_registration.target_projects`) render as a permissive `{}`
//!     (any JSON value) — the launcher accepts and forwards opaquely.
//!
//! ## Print policy
//!
//! EVERY `println!` / `eprintln!` in this file is CLI OUTPUT, not a
//! diagnostic, and is annotated `// [vct-print-contract]` for the
//! no-bare-prints ratchet. This binary is a standalone CI tool: its
//! stdout IS the schema (`export-schema > file`), and its stderr IS its
//! report. It installs no `tracing` subscriber and must not — routing a
//! tool's own output through a level-gated logger would let a log level
//! silently empty the file CI diffs against.
//!
//! The annotations are per-line rather than a blanket file exemption so
//! that a genuine diagnostic added here later still has to be justified
//! one line at a time.
//!
//! Usage:
//!   export-schema                  → stdout (used by CI for `> file && diff`)
//!   export-schema --out <path>     → write to <path> directly (atomic-rename)
//!   export-schema --check <path>   → exit 1 if the schema would differ
//!                                    from the file at <path>. CI-friendly:
//!                                    no temp file, no `git diff` round-trip.

use std::path::PathBuf;
use std::process::ExitCode;

use schemars::schema_for;
use vct_launcher_core::manifest::ModuleManifest;

fn print_usage() {
    // [vct-print-contract] CLI output, not diagnostics.
    eprintln!(
        "usage: export-schema [--out <path> | --check <path>]\n\
         \n\
         Emits the launcher's vct-module.json schema as JSON Schema\n\
         (Draft 2020-12, via schemars 0.8). With no arg, writes to stdout.\n\
         \n\
         Flags:\n\
           --out <path>    — write atomically to <path> instead of stdout.\n\
           --check <path>  — compare against <path>; exit 1 on mismatch.\n\
                             (CI calls this against docs/schemas/vct-module.schema.json.)\n\
           --help, -h      — print this message and exit 2.\n"
    );
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let schema_json = generate_schema_string();

    // Parse the (very small) arg set.
    let mut out: Option<PathBuf> = None;
    let mut check: Option<PathBuf> = None;
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        match a.as_str() {
            "--help" | "-h" => {
                print_usage();
                return ExitCode::from(2);
            }
            "--out" => {
                let Some(p) = args.get(i + 1) else {
                    // [vct-print-contract] CLI output, not diagnostics.
                    eprintln!("--out requires a path");
                    return ExitCode::from(2);
                };
                out = Some(PathBuf::from(p));
                i += 2;
            }
            "--check" => {
                let Some(p) = args.get(i + 1) else {
                    // [vct-print-contract] CLI output, not diagnostics.
                    eprintln!("--check requires a path");
                    return ExitCode::from(2);
                };
                check = Some(PathBuf::from(p));
                i += 2;
            }
            other => {
                // [vct-print-contract] CLI output, not diagnostics.
                eprintln!("unknown flag: {}", other);
                print_usage();
                return ExitCode::from(2);
            }
        }
    }

    if out.is_some() && check.is_some() {
        // [vct-print-contract] CLI output, not diagnostics.
        eprintln!("--out and --check are mutually exclusive");
        return ExitCode::from(2);
    }

    if let Some(path) = check {
        return run_check(&path, &schema_json);
    }
    if let Some(path) = out {
        return write_atomically(&path, &schema_json);
    }
    // Default: stdout.
    // [vct-print-contract] CLI output, not diagnostics.
    println!("{}", schema_json);
    ExitCode::SUCCESS
}

fn generate_schema_string() -> String {
    let schema = schema_for!(ModuleManifest);
    let mut s = serde_json::to_string_pretty(&schema)
        .expect("schema serialises to JSON — schemars output is always valid");
    s.push('\n'); // trailing newline so the file is POSIX-clean
    s
}

fn run_check(committed_path: &PathBuf, generated: &str) -> ExitCode {
    let on_disk = match std::fs::read_to_string(committed_path) {
        Ok(s) => s,
        Err(e) => {
            // [vct-print-contract] CLI output, not diagnostics.
            eprintln!(
                "[FAIL] cannot read committed schema at {}: {}\n\
                 Run: cargo run -p vct-launcher-core --bin export-schema --out {}",
                committed_path.display(),
                e,
                committed_path.display()
            );
            return ExitCode::FAILURE;
        }
    };
    if on_disk == generated {
        // [vct-print-contract] CLI output, not diagnostics.
        println!("[OK] {} matches the live schema", committed_path.display());
        ExitCode::SUCCESS
    } else {
        // [vct-print-contract] CLI output, not diagnostics.
        eprintln!(
            "[FAIL] {} is out of sync with the live schema.\n\
             Run: cargo run -p vct-launcher-core --bin export-schema --out {}\n\
             then commit the regenerated file.",
            committed_path.display(),
            committed_path.display()
        );
        // Print a unified-ish diff hint so CI logs surface the drift.
        let on_disk_lines: Vec<&str> = on_disk.lines().collect();
        let gen_lines: Vec<&str> = generated.lines().collect();
        // [vct-print-contract] CLI output, not diagnostics.
        eprintln!(
            "(committed: {} lines, generated: {} lines)",
            on_disk_lines.len(),
            gen_lines.len()
        );
        ExitCode::FAILURE
    }
}

fn write_atomically(path: &PathBuf, contents: &str) -> ExitCode {
    let parent = path.parent().unwrap_or_else(|| std::path::Path::new("."));
    if let Err(e) = std::fs::create_dir_all(parent) {
        // [vct-print-contract] CLI output, not diagnostics.
        eprintln!("[FAIL] cannot create parent dir {}: {}", parent.display(), e);
        return ExitCode::FAILURE;
    }
    let tmp = parent.join(format!(
        ".{}.tmp",
        path.file_name()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_else(|| "schema".to_string())
    ));
    if let Err(e) = std::fs::write(&tmp, contents) {
        // [vct-print-contract] CLI output, not diagnostics.
        eprintln!("[FAIL] cannot write tempfile {}: {}", tmp.display(), e);
        return ExitCode::FAILURE;
    }
    if let Err(e) = std::fs::rename(&tmp, path) {
        // [vct-print-contract] CLI output, not diagnostics.
        eprintln!(
            "[FAIL] cannot atomic-rename {} → {}: {}",
            tmp.display(),
            path.display(),
            e
        );
        // Best-effort cleanup.
        let _ = std::fs::remove_file(&tmp);
        return ExitCode::FAILURE;
    }
    // [vct-print-contract] CLI output, not diagnostics.
    println!("wrote {}", path.display());
    ExitCode::SUCCESS
}

// SPDX-License-Identifier: AGPL-3.0-or-later
//! Shared Python-bridge deferral writer.
//!
//! ## Why this module exists (v0.2.77 Part 7c task 4)
//!
//! `vco_lib/deferral_report.py` is the CANONICAL writer for
//! `UPDATE_DEFERRED.md`: it does atomic markdown writes, injects the
//! CLAUDE.md "see UPDATE_DEFERRED.md" reminder block, and round-trips the
//! read/parse cycle (condition-id dedup on `add_entry`). Rather than
//! re-implement that markdown machinery in Rust, the launcher's various
//! deferral emitters all shell out to a tiny `python -c` snippet that
//! imports `DeferralEntry` + `DeferralReport` and appends one entry.
//!
//! Before this module, SIX call-sites carried a byte-for-byte copy of the
//! same three helpers each — `py_quote` (Python-string escaper),
//! `pick_python` (interpreter resolver), and the `-c` snippet template
//! (`import sys; sys.path.insert(...); from vco_lib.deferral_report import
//! ...; report.add_entry(entry); report.write(folder)`):
//!
//!   * `storage_ux::emit_deferral`
//!   * `module_updates::write_partial_failure_deferral`
//!   * `git_user_editable_merge::emit_orchestrator_user_modified_deferrals`
//!   * `projects_v2::emit_codegraph_rename_deferral`
//!   * `codegraph::emit_stale_wrapper_deferral`
//!   * (`chunker_revision_deferral` uses a DIFFERENT vco_lib entry point —
//!     `_emit_chunker_resync_deferral` — so it is intentionally NOT routed
//!     through this DeferralReport-direct writer.)
//!
//! Six copies of a snippet that embeds user-controlled strings into a
//! Python `-c` program is exactly where an escaping bug would hide. This
//! module is the ONE home: [`emit_deferral_entry`] builds the injection-
//! safe snippet once and every caller reduces to "compute the six entry
//! fields, then call the writer."
//!
//! ## Best-effort contract
//!
//! Returns `Result<(), String>` so callers that want to log a failure can,
//! but deferrals are an FYI mechanism: a failure here (no python, malformed
//! clone, subprocess non-zero) must NEVER mask the original operation's
//! outcome. Callers keep their existing log-and-swallow behaviour.
//!
//! ## Interpreter resolution
//!
//! Uses the shared RT-4 ladder
//! (`vct_launcher_core::python_resolve::resolve_python_for_vco_lib`) so the
//! deferral `-c` snippet — which does `import ...vco_lib.deferral_report` —
//! resolves the orchestrator venv (which HAS `vco_lib`) before falling back
//! to a bare PATH `python3`. This is the same upgrade Part 7c task 1 applied
//! to the per-site `pick_python` copies.
//!
//! ## `.silent()` note
//!
//! The single `Command::new` here carries `.silent()`; the
//! `command_silent_gate` integration test scans this file by path.

use std::path::Path;

// Brings the chainable `.silent()` marker onto `std::process::Command`
// (CREATE_NO_WINDOW on Windows, no-op elsewhere). Required so the single
// spawn below satisfies the command_silent_gate.
use vct_launcher_core::process::CommandExt as _;

/// The six free-form fields of a `vco_lib.deferral_report.DeferralEntry`.
///
/// `severity` must be one of the Python side's `SEVERITY_ORDER` values
/// (`critical` | `warning` | `info`) — anything else makes
/// `DeferralEntry.__post_init__` raise `ValueError`, the `-c` snippet exit
/// non-zero, and the deferral silently go unwritten (the v0.2.75
/// `module_updates` "medium" bug). Callers pass a valid value; this writer
/// does not validate (it would only duplicate the Python-side check).
pub struct DeferralEntryFields<'a> {
    pub condition_id: &'a str,
    pub title: &'a str,
    pub detected: &'a str,
    pub why_deferred: &'a str,
    pub command_to_apply: &'a str,
    pub severity: &'a str,
}

/// Emit one deferral entry into `report_folder`'s `UPDATE_DEFERRED.md`,
/// shelling out to `vco_lib.deferral_report` via a `python -c` snippet.
///
/// * `sys_path_root` — prepended to `sys.path` so `import vco_lib...`
///   resolves the in-tree namespace package (the orchestrator clone root).
/// * `report_folder` — the folder whose `.claude/context/UPDATE_DEFERRED.md`
///   the entry lands in (`DeferralReport.read(folder)` /
///   `report.write(folder)`). Often equal to `sys_path_root` (orchestrator-
///   root deferrals) but distinct for per-project deferrals (e.g. a rename
///   deferral that lands in the renamed project's folder while importing
///   `vco_lib` from the orchestrator clone).
///
/// Returns `Err` on any failure; callers decide whether to log-and-swallow.
pub fn emit_deferral_entry(
    sys_path_root: &Path,
    report_folder: &Path,
    fields: &DeferralEntryFields<'_>,
) -> Result<(), String> {
    let python = vct_launcher_core::python_resolve::resolve_python_for_vco_lib()
        .ok_or_else(|| "no python interpreter found to emit deferral".to_string())?;

    let root_py = py_quote(&sys_path_root.to_string_lossy());
    let folder_py = py_quote(&report_folder.to_string_lossy());
    let cid_py = py_quote(fields.condition_id);
    let title_py = py_quote(fields.title);
    let det_py = py_quote(fields.detected);
    let why_py = py_quote(fields.why_deferred);
    let cmd_py = py_quote(fields.command_to_apply);
    let sev_py = py_quote(fields.severity);

    let script = format!(
        "import sys\n\
         sys.path.insert(0, {root_py})\n\
         from pathlib import Path\n\
         from vco_lib.deferral_report import DeferralEntry, DeferralReport\n\
         folder = Path({folder_py})\n\
         report = DeferralReport.read(folder)\n\
         entry = DeferralEntry(\n\
         \x20\x20\x20\x20condition_id={cid_py},\n\
         \x20\x20\x20\x20title={title_py},\n\
         \x20\x20\x20\x20detected={det_py},\n\
         \x20\x20\x20\x20why_deferred={why_py},\n\
         \x20\x20\x20\x20command_to_apply={cmd_py},\n\
         \x20\x20\x20\x20severity={sev_py},\n\
         )\n\
         report.add_entry(entry)\n\
         report.write(folder)\n",
    );

    let status = std::process::Command::new(&python)
        .silent()
        .arg("-c")
        .arg(&script)
        .status();
    match status {
        Ok(s) if s.success() => Ok(()),
        Ok(s) => Err(format!("deferral helper exited {}", s)),
        Err(e) => Err(format!("deferral helper spawn failed: {}", e)),
    }
}

/// Quote `s` as a Python double-quoted string literal, escaping
/// backslashes, double-quotes, and control characters so the result is
/// safe to embed in a Python `-c` snippet. The single canonical copy of
/// what were six byte-identical per-site `py_quote` functions.
pub fn py_quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn py_quote_escapes_injection_chars() {
        assert_eq!(py_quote("plain"), "\"plain\"");
        // Double-quote + backslash must be escaped so the `-c` string
        // literal can't be broken out of.
        assert_eq!(py_quote("a\"b"), "\"a\\\"b\"");
        assert_eq!(py_quote("a\\b"), "\"a\\\\b\"");
        // Newline / tab / control chars.
        assert_eq!(py_quote("a\nb"), "\"a\\nb\"");
        assert_eq!(py_quote("a\tb"), "\"a\\tb\"");
        assert_eq!(py_quote("\u{0001}"), "\"\\u0001\"");
    }

    /// A crafted string that would break out of the literal if unescaped
    /// (`"); import os; os.system("...`) must survive round-trip as a
    /// single quoted token with no unescaped `"`.
    #[test]
    fn py_quote_neutralises_breakout_attempt() {
        let hostile = "\"); import os; os.system(\"rm -rf /\"); x=(\"";
        let quoted = py_quote(hostile);
        // Every interior double-quote is backslash-escaped: there is no
        // `"` in the output that is not immediately preceded by `\`.
        let bytes: Vec<char> = quoted.chars().collect();
        for (i, c) in bytes.iter().enumerate() {
            if *c == '"' && i != 0 && i != bytes.len() - 1 {
                assert_eq!(
                    bytes[i - 1],
                    '\\',
                    "unescaped interior quote at {i} in {quoted}"
                );
            }
        }
    }
}

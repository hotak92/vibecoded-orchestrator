// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! The ONE Rust bridge to the locked Python deferral writer
//! (`vco_lib.deferral_emit`) — shared by the launcher and the hub.
//!
//! v0.2.100 (WP-06): moved here VERBATIM from the launcher crate's
//! `src/services/deferral.rs` (which now only re-exports these items) so the
//! detached hub — which cannot depend on the launcher crate — records its own
//! rows (`watchdog_foreign_container`, `module_container_unlabelled`) through
//! the SAME injection-safe `python -c` payload and the SAME lock, instead of a
//! second copy. The design notes (why a `python -c` payload, the best-effort
//! contract, interpreter resolution, the `.silent()` rule) stay on the
//! launcher module's header, where the history lives; nothing here changed.

use std::path::Path;

// Brings the chainable `.silent()` marker onto `std::process::Command`
// (CREATE_NO_WINDOW on Windows, no-op elsewhere) — the command_silent_gate.
use crate::process::CommandExt as _;

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
    let python = crate::python_resolve::resolve_python_for_vco_lib()
        .ok_or_else(|| "no python interpreter found to emit deferral".to_string())?;

    let script = build_deferral_emit_script(sys_path_root, report_folder, fields);

    run_deferral_payload(&python, &script, "deferral emit")
}

/// Upper bound on one deferral payload (v0.2.100 W4R-05 / F-W4-05). The
/// payload is a lock + a small file write: seconds at most. A Python that
/// does not finish in this time is stuck (a venv on a stalled network
/// filesystem, an import waiting on a lock), and every caller — the hub's
/// watchdog tick, the reaper passes, the launcher's quit path — must not be
/// parked behind it. Callers already log-and-swallow an `Err`.
pub const DEFERRAL_PAYLOAD_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(30);

/// Run one `python -c` deferral payload, mapping failure to an `Err` that
/// SAYS WHY.
///
/// The ONE spawn in this module — both public writers route through it, so
/// the interpreter handling, the stream policy and the error shape cannot
/// drift between "emit an entry" and "settle an entry".
///
/// Runs through [`crate::process::output_bounded`] (the one bounded runner):
/// a payload still running after [`DEFERRAL_PAYLOAD_TIMEOUT`] is killed and
/// reported as `Err("… timed out after 30 s (killed)")`. Blocking — an async
/// caller runs it through `tokio::task::spawn_blocking`.
///
/// Captures the child's streams for two reasons:
///
/// * the caller's log line becomes diagnostic. `deferral helper exited exit
///   status: 1` names nothing; `… ModuleNotFoundError: No module named
///   'vco_lib.deferral_emit'` names the whole problem;
/// * an inherited stderr writes the child's traceback to whatever stream the
///   launcher (or a test harness) happens to own, out of band and out of
///   context. In CI run 34118502499 that put raw Python tracebacks in the
///   middle of a passing Rust test log.
///
/// `what` names the operation for the message ("deferral emit" / "deferral
/// resolve"). Success discards the captured output — a payload that succeeds
/// has nothing to say.
pub fn run_deferral_payload(python: &Path, script: &str, what: &str) -> Result<(), String> {
    run_deferral_payload_within(python, script, what, DEFERRAL_PAYLOAD_TIMEOUT)
}

/// [`run_deferral_payload`] with an explicit bound (tests drive the timeout
/// arm with a short one).
pub fn run_deferral_payload_within(
    python: &Path,
    script: &str,
    what: &str,
    limit: std::time::Duration,
) -> Result<(), String> {
    let mut cmd = std::process::Command::new(python).silent();
    cmd.arg("-c").arg(script);
    match crate::process::output_bounded(&mut cmd, None, limit) {
        Ok(o) if o.status.success() => Ok(()),
        Ok(o) => Err(format!(
            "{what} helper ({}) exited {}: {}",
            python.display(),
            o.status,
            summarise_child_output(&o.stderr, &o.stdout)
        )),
        Err(e @ crate::process::BoundedError::TimedOut { .. }) => Err(format!(
            "{what} helper ({}) {e}",
            python.display()
        )),
        Err(e) => Err(format!("{what} helper ({}) {e}", python.display())),
    }
}

/// Condense a failed child's output into ONE log-line-sized explanation.
///
/// Prefers stderr (where Python writes tracebacks) and falls back to stdout.
/// Keeps the LAST few non-empty lines, because the line that says what went
/// wrong (`ModuleNotFoundError: …`) is the last one — a traceback's leading
/// frames are the least informative part of it. Bounded so a pathological
/// child cannot flood the log.
pub fn summarise_child_output(stderr: &[u8], stdout: &[u8]) -> String {
    /// Lines kept from the tail. Three covers "File …, line N" + the
    /// exception, with one spare.
    const KEEP_LINES: usize = 3;
    /// Hard ceiling on the rendered summary.
    const MAX_CHARS: usize = 500;

    let pick = |raw: &[u8]| -> Option<String> {
        let text = String::from_utf8_lossy(raw);
        let kept: Vec<&str> = text
            .lines()
            .map(str::trim)
            .filter(|l| !l.is_empty())
            .collect();
        if kept.is_empty() {
            return None;
        }
        Some(kept[kept.len().saturating_sub(KEEP_LINES)..].join(" | "))
    };

    let summary = match pick(stderr).or_else(|| pick(stdout)) {
        Some(s) => s,
        None => return "(no output)".to_string(),
    };

    // Truncate from the FRONT: the tail is the informative end.
    if summary.chars().count() > MAX_CHARS {
        let skip = summary.chars().count() - MAX_CHARS;
        format!("…{}", summary.chars().skip(skip).collect::<String>())
    } else {
        summary
    }
}

/// v0.2.88 (MAJOR-3) — mark one or more deferral condition IDs RESOLVED on the
/// on-disk report, under the shared deferral lock, via
/// `vco_lib.deferral_emit.resolve_conditions`. This is the belt-and-suspenders
/// companion to the install.py `_INSTALL_OWNED_CONDITION_IDS` self-clear: a GUI
/// resolver (untracked-collision / autostash-pop modals) that resolves a
/// collision but whose retry does NOT reach install.py (e.g. the resume errors
/// before finalize) still settles its own row immediately, so the stale
/// "pending action" nag + the destructive stale-command hazard don't outlive
/// the fix.
///
/// `resolve_conditions` drops each present entry AND tombstones it for the
/// locked cycle, deleting `UPDATE_DEFERRED.{md,json}` when no entries remain.
/// Resolving an absent ID is a safe no-op. Best-effort: returns `Err` on any
/// subprocess failure; callers log-and-swallow (a deferral-settle failure must
/// never mask the resolution outcome).
pub fn resolve_deferral_conditions(
    sys_path_root: &Path,
    report_folder: &Path,
    condition_ids: &[&str],
) -> Result<(), String> {
    let python = crate::python_resolve::resolve_python_for_vco_lib()
        .ok_or_else(|| "no python interpreter found to settle deferral".to_string())?;

    let script = build_deferral_resolve_script(sys_path_root, report_folder, condition_ids);

    run_deferral_payload(&python, &script, "deferral resolve")
}

/// Build the injection-safe `python -c` payload that marks condition IDs
/// resolved via the LOCKED `vco_lib.deferral_emit.resolve_conditions`. Extracted
/// as a pure helper so the structural payload test can assert the snippet
/// without spawning a subprocess.
pub fn build_deferral_resolve_script(
    sys_path_root: &Path,
    report_folder: &Path,
    condition_ids: &[&str],
) -> String {
    let root_py = py_quote(&sys_path_root.to_string_lossy());
    let folder_py = py_quote(&report_folder.to_string_lossy());
    let ids_py: String = condition_ids
        .iter()
        .map(|c| py_quote(c))
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "import sys\n\
         sys.path.insert(0, {root_py})\n\
         from pathlib import Path\n\
         from vco_lib.deferral_emit import resolve_conditions\n\
         folder = Path({folder_py})\n\
         resolve_conditions(folder, [{ids_py}])\n\
         sys.exit(0)\n",
    )
}

/// Build the injection-safe `python -c` payload that emits one deferral entry.
///
/// v0.2.83 WP-B6: routes through the LOCKED emitter `vco_lib.deferral_emit`
/// (`DeferralEntry` + `emit`), NOT the raw `DeferralReport.read/add_entry/write`
/// triplet used through v0.2.82. `deferral_emit.emit` holds an exclusive `flock`
/// on `<folder>/.claude/context/.update-deferred.lock` for the whole
/// read → mutate → write cycle, so all six delegating Rust call-sites serialize
/// on the SAME lock as every other UPDATE_DEFERRED writer (Python writers, and —
/// via [`crate::services::deferral_lock`] — the launcher's direct
/// `std::fs` deferral writers).
///
/// `emit` preserves FOREIGN entries (last-write-wins per condition_id) and
/// swallows I/O errors internally, returning `True` when the report holds ≥1
/// entry after the write (our single add always does unless the add raised) and
/// `False` on error. The payload maps `False` → `sys.exit(1)` so the caller's
/// "subprocess non-zero ⇒ `Err`" soft-fail posture stays byte-for-byte
/// identical to the pre-WP-B6 raw-write payload.
///
/// Extracted as a pure helper so the structural payload test can assert the
/// snippet references `vco_lib.deferral_emit` without spawning a subprocess.
pub fn build_deferral_emit_script(
    sys_path_root: &Path,
    report_folder: &Path,
    fields: &DeferralEntryFields<'_>,
) -> String {
    let root_py = py_quote(&sys_path_root.to_string_lossy());
    let folder_py = py_quote(&report_folder.to_string_lossy());
    let cid_py = py_quote(fields.condition_id);
    let title_py = py_quote(fields.title);
    let det_py = py_quote(fields.detected);
    let why_py = py_quote(fields.why_deferred);
    let cmd_py = py_quote(fields.command_to_apply);
    let sev_py = py_quote(fields.severity);

    format!(
        "import sys\n\
         sys.path.insert(0, {root_py})\n\
         from pathlib import Path\n\
         from vco_lib.deferral_emit import DeferralEntry, emit\n\
         folder = Path({folder_py})\n\
         entry = DeferralEntry(\n\
         \x20\x20\x20\x20condition_id={cid_py},\n\
         \x20\x20\x20\x20title={title_py},\n\
         \x20\x20\x20\x20detected={det_py},\n\
         \x20\x20\x20\x20why_deferred={why_py},\n\
         \x20\x20\x20\x20command_to_apply={cmd_py},\n\
         \x20\x20\x20\x20severity={sev_py},\n\
         )\n\
         ok = emit(folder, entry)\n\
         sys.exit(0 if ok else 1)\n",
    )
}

// v0.2.91 WP-B — DISPOSITION lookup deliberately has NO wrapper here.
//
// The compiled registry mirror is `crate::deferral_registry`; its
// `disposition_for` / `is_actionable` are the launcher-side API and are already
// unit-tested + parity-locked against `vco_lib/deferral_conditions.toml`. A
// pass-through wrapper in this crate would be dead code until the ledger UI
// (WP-I) lands, and this repo consumes symbols rather than `#[allow(dead_code)]`
// them. Call the core functions directly:
//
//     crate::deferral_registry::disposition_for(cid)   // tier
//     crate::deferral_registry::is_actionable(cid)     // badge count
//
// `is_actionable` is the partition a badge must use (`action_required` +
// `auto_retryable`); it matches Python's `split_by_disposition`, so the
// launcher's count and the CLAUDE.md reminder's count cannot disagree.

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
    use std::time::{Duration, Instant};

    fn spawnable_python() -> Option<std::path::PathBuf> {
        let python = crate::python_resolve::resolve_python_for_vco_lib()?;
        std::process::Command::new(&python)
            .arg("-c")
            .arg("pass")
            .output()
            .ok()
            .filter(|o| o.status.success())
            .map(|_| python)
    }

    /// W4R-05 / F-W4-05: a payload that never finishes is killed at the
    /// bound and reported, instead of parking its caller (the hub's watchdog
    /// tick, the reaper pass, the launcher's quit path) forever.
    #[test]
    fn a_hung_payload_is_killed_at_the_bound_and_reported() {
        let Some(python) = spawnable_python() else {
            eprintln!("skipping: no spawnable python interpreter");
            return;
        };
        let started = Instant::now();
        let err = run_deferral_payload_within(
            &python,
            "import time\ntime.sleep(60)\n",
            "deferral emit",
            Duration::from_millis(500),
        )
        .expect_err("a hung payload must be an Err");
        assert!(started.elapsed() < Duration::from_secs(20), "the bound was not honoured");
        assert!(err.contains("timed out after"), "got: {err}");
        assert!(err.contains("deferral emit helper"), "got: {err}");
    }

    /// Leave-alone: a payload that finishes inside the bound is unaffected.
    #[test]
    fn a_quick_payload_is_ok() {
        let Some(python) = spawnable_python() else {
            eprintln!("skipping: no spawnable python interpreter");
            return;
        };
        run_deferral_payload_within(&python, "pass\n", "deferral emit", Duration::from_secs(20))
            .expect("a payload that exits 0 is Ok");
    }

    #[test]
    fn the_production_bound_is_finite_and_generous() {
        assert!(DEFERRAL_PAYLOAD_TIMEOUT >= Duration::from_secs(10));
        assert!(DEFERRAL_PAYLOAD_TIMEOUT <= Duration::from_secs(120));
    }
}

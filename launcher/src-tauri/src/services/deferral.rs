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
//! imports the Python emitter and appends one entry.
//!
//! ## v0.2.83 WP-B6 — routes through the LOCKED emitter
//!
//! The `-c` snippet now imports `DeferralEntry` + `emit` from
//! `vco_lib.deferral_emit` (NOT the raw `DeferralReport.read/add_entry/write`
//! triplet it used through v0.2.82). `deferral_emit.emit` holds an exclusive
//! `flock` on `<folder>/.claude/context/.update-deferred.lock` for the whole
//! read → mutate → write cycle, so ALL SIX delegating call-sites below now
//! serialize on the SAME lock as every other UPDATE_DEFERRED writer (the
//! Python install-flow / project-init writers, and — via
//! `vct_launcher_core::services::deferral_lock` — the launcher's DIRECT
//! `std::fs` deferral writers). Behaviour is otherwise identical: foreign
//! entries are preserved (last-write-wins per condition_id) and a failure maps
//! to a subprocess non-zero exit, which this writer surfaces as `Err`.
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
//!   * `binding_reconcile` (repair/phantom deferrals; the v0.2.75 rename
//!     emitter was retired by the v0.2.89 immutable-names ruling)
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
//! Best-effort is NOT the same as quiet. Every caller logs the `Err`, so the
//! `Err` has to be worth logging: through v0.2.92 it read
//! `deferral helper exited exit status: 1` — an exit code and nothing else —
//! while the interpreter's own diagnosis was written straight to the
//! LAUNCHER's stderr, because `.status()` inherits the child's streams. Two
//! bad halves: a log line that cannot explain the failure, and an explanation
//! that appears somewhere no caller controls, attached to no context. It
//! surfaced as bare `ModuleNotFoundError: No module named 'vco_lib.deferral_emit'`
//! tracebacks interleaved into CI's Rust test log (run 34118502499), reading
//! like a crash in a suite that was in fact passing.
//!
//! So [`run_deferral_payload`] CAPTURES the child's output and folds the tail
//! of it into the `Err`. The launcher's `tracing::warn!` then carries the
//! interpreter path AND the reason; nothing writes to an inherited stream.
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
//! The last rung of that ladder is a bare `python3`, which on a machine with
//! no VCO install cannot import `vco_lib` at all. That is a legitimate
//! outcome, not a bug to paper over: the deferral goes unwritten and the
//! caller logs why. What must NOT happen is the failure arriving as an
//! unexplained exit code — see the best-effort note above.
//!
//! ## `.silent()` note
//!
//! The single `Command::new` of this bridge — in `run_deferral_payload`,
//! which both public writers route through — carries `.silent()`; since
//! v0.2.100 it lives in `vct_launcher_core::services::deferral_bridge`, which
//! the `command_silent_gate` integration test scans (`vct-launcher-core/src`).
//!
//! ## v0.2.100 (WP-06) — the code lives in `vct_launcher_core`
//!
//! The helpers below moved VERBATIM to
//! `vct_launcher_core::services::deferral_bridge` so the detached hub (which
//! cannot depend on this crate) writes its rows through the same payload and
//! lock. This module keeps the launcher's import path and the history above;
//! the one `.silent()` spawn is now in the core file.

pub use vct_launcher_core::services::deferral_bridge::{
    emit_deferral_entry, resolve_deferral_conditions, DeferralEntryFields,
};

// The payload internals, visible to this module's own (unchanged) tests.
#[cfg(test)]
use std::path::Path;
#[cfg(test)]
use vct_launcher_core::services::deferral_bridge::{
    build_deferral_emit_script, build_deferral_resolve_script, py_quote, run_deferral_payload,
    summarise_child_output,
};

#[cfg(test)]
mod tests {
    use super::*;

    /// v0.2.92 CI-divergence fix: a payload whose `import vco_lib…` fails
    /// must come back as an `Err` that NAMES the import failure.
    ///
    /// This is the machine-with-no-VCO-install case, and it is not
    /// hypothetical: GitHub's `ubuntu-latest` is exactly that machine. The
    /// RT-4 ladder finds no venv there, falls to a bare PATH `python3`, and
    /// the payload's `sys.path.insert(0, <clone root>)` then resolves
    /// `vco_lib` as an implicit NAMESPACE package over whatever `vco_lib/`
    /// directory the caller's clone happens to contain — so `import
    /// vco_lib.deferral_emit` raises. Pre-fix the caller was handed
    /// `deferral helper exited exit status: 1` while the traceback went to an
    /// inherited stderr; the failure was simultaneously unexplained and
    /// unmissable, in the wrong place.
    ///
    /// Hermetic by construction: the module name cannot exist on any machine,
    /// so the assertion does not depend on whether THIS host has `vco_lib`
    /// importable (the maintainer's does — via `$VCT_INSTALL_ROOT`'s venv —
    /// and CI's does not; that gap is what made this defect CI-only).
    #[test]
    fn a_payload_that_cannot_import_reports_the_import_error_not_a_bare_exit_code() {
        let Some(python) = vct_launcher_core::python_resolve::resolve_python_for_vco_lib()
        else {
            eprintln!("skipping: no python interpreter resolved");
            return;
        };
        // Spawnability probe — the PATH-fallback rung may name a python that
        // isn't there, and "spawn failed" is a different assertion.
        if std::process::Command::new(&python)
            .arg("-c")
            .arg("pass")
            .output()
            .map(|o| !o.status.success())
            .unwrap_or(true)
        {
            eprintln!("skipping: {} is not spawnable", python.display());
            return;
        }

        let err = run_deferral_payload(
            &python,
            "import vco_lib_absent_on_every_machine_8f21c3\n",
            "deferral emit",
        )
        .expect_err("an unimportable payload must be an Err");

        assert!(
            err.contains("vco_lib_absent_on_every_machine_8f21c3"),
            "the Err must carry the interpreter's own diagnosis — the module \
             it could not import — not just an exit code; got: {err}"
        );
        assert!(
            err.contains("deferral emit helper"),
            "the Err must say which operation failed; got: {err}"
        );
        assert!(
            err.contains(&python.display().to_string()),
            "the Err must name the interpreter that failed — on a machine with \
             several pythons that IS the diagnosis; got: {err}"
        );
    }

    /// The success half, end to end, through a REAL python.
    ///
    /// Everything else in this module tests the payload as a string. Nothing
    /// tested that the payload, handed to an interpreter, actually writes a
    /// deferral — so a broken payload would have surfaced only as the same
    /// swallowed best-effort `Err` that a missing `vco_lib` produces, i.e. as
    /// nothing at all. (That gap is how the CI divergence stayed invisible: on
    /// the maintainer's machine `$VCT_INSTALL_ROOT`'s venv makes every emit
    /// succeed; on CI every emit fails; no test could tell the two apart.)
    ///
    /// Hermetic on both: `sys_path_root` is THIS repo's root, so `vco_lib`
    /// resolves as a regular package at `sys.path[0]` regardless of what the
    /// host has installed, and the whole import chain
    /// (`deferral_emit` → `deferral_report` → `atomic`) is pure stdlib.
    #[test]
    fn a_real_python_writes_the_deferral_through_the_bridge() {
        let Some(python) = vct_launcher_core::python_resolve::resolve_python_for_vco_lib()
        else {
            eprintln!("skipping: no python interpreter resolved");
            return;
        };
        if std::process::Command::new(&python)
            .arg("-c")
            .arg("pass")
            .output()
            .map(|o| !o.status.success())
            .unwrap_or(true)
        {
            eprintln!("skipping: {} is not spawnable", python.display());
            return;
        }

        // <repo>/launcher/src-tauri → <repo>
        let repo_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..");
        assert!(
            repo_root.join("vco_lib").join("deferral_emit.py").is_file(),
            "fixture precondition: vco_lib/deferral_emit.py must exist at {}",
            repo_root.display()
        );

        let folder = tempfile::tempdir().expect("tempdir");
        let fields = DeferralEntryFields {
            condition_id: "bridge_end_to_end_probe",
            title: "Bridge probe",
            detected: "written by a_real_python_writes_the_deferral_through_the_bridge",
            why_deferred: "test",
            command_to_apply: "```bash\necho probe\n```",
            severity: "info",
        };

        emit_deferral_entry(&repo_root, folder.path(), &fields)
            .expect("the bridge must WRITE, not merely not-crash");

        let report = folder.path().join(".claude/context/UPDATE_DEFERRED.md");
        let body = std::fs::read_to_string(&report)
            .unwrap_or_else(|e| panic!("no report at {}: {e}", report.display()));
        assert!(
            body.contains("bridge_end_to_end_probe") && body.contains("Bridge probe"),
            "the entry's own fields must reach the file; got:\n{body}"
        );

        // …and the settle half retires it, deleting the now-empty report.
        resolve_deferral_conditions(&repo_root, folder.path(), &["bridge_end_to_end_probe"])
            .expect("resolve must succeed through the same bridge");
        assert!(
            !report.is_file(),
            "resolving the only entry must delete the report, not leave a husk"
        );
    }

    /// The summariser keeps the END of a traceback (the exception line), not
    /// the beginning (the least informative frames), and stays log-sized.
    #[test]
    fn child_output_summary_keeps_the_exception_and_stays_bounded() {
        let traceback = b"Traceback (most recent call last):\n  \
            File \"<string>\", line 4, in <module>\n\
            ModuleNotFoundError: No module named 'vco_lib.deferral_emit'\n";
        let s = summarise_child_output(traceback, b"");
        assert!(
            s.ends_with("ModuleNotFoundError: No module named 'vco_lib.deferral_emit'"),
            "the exception line must survive to the end of the summary; got: {s}"
        );

        // stdout is the fallback, never the preference.
        assert_eq!(summarise_child_output(b"", b"on stdout\n"), "on stdout");
        assert_eq!(summarise_child_output(b"on stderr\n", b"on stdout\n"), "on stderr");
        // Blank-but-present output is treated as no output.
        assert_eq!(summarise_child_output(b"\n  \n", b""), "(no output)");

        // A pathological child cannot flood the log, and truncation eats the
        // FRONT so the tail (the reason) survives.
        let flood = format!("{}THE_REASON", "x".repeat(4000));
        let s = summarise_child_output(flood.as_bytes(), b"");
        assert!(s.chars().count() <= 501, "summary must stay bounded; got {}", s.chars().count());
        assert!(s.ends_with("THE_REASON"), "truncation must keep the tail; got: {s}");
    }

    /// WP-B6 (v0.2.83): the chokepoint payload MUST route through the LOCKED
    /// emitter `vco_lib.deferral_emit` (`emit`), NOT the raw
    /// `DeferralReport.read/add_entry/write` triplet — that is what serializes
    /// all six delegating call-sites on the shared file lock. Structural pin:
    /// if a refactor reverts the import, this fails.
    #[test]
    fn payload_routes_through_locked_deferral_emit() {
        let fields = DeferralEntryFields {
            condition_id: "some_cond",
            title: "T",
            detected: "D",
            why_deferred: "W",
            command_to_apply: "cmd",
            severity: "warning",
        };
        let script = build_deferral_emit_script(
            Path::new("/orch/root"),
            Path::new("/proj/folder"),
            &fields,
        );
        // Locked emitter import + call.
        assert!(
            script.contains("from vco_lib.deferral_emit import DeferralEntry, emit"),
            "payload must import from the LOCKED emitter vco_lib.deferral_emit; got:\n{script}"
        );
        assert!(
            script.contains("ok = emit(folder, entry)"),
            "payload must call emit(folder, entry); got:\n{script}"
        );
        // Soft-fail posture preserved: False ⇒ non-zero exit ⇒ caller Err.
        assert!(
            script.contains("sys.exit(0 if ok else 1)"),
            "payload must map emit()==False to a non-zero exit; got:\n{script}"
        );
        // The pre-WP-B6 raw triplet must be GONE (no unlocked read/write path).
        assert!(
            !script.contains("DeferralReport"),
            "payload must NOT reference the unlocked DeferralReport writer; got:\n{script}"
        );
        assert!(
            !script.contains("report.write"),
            "payload must NOT call the unlocked report.write; got:\n{script}"
        );
    }

    /// v0.2.88 (MAJOR-3): the settle payload MUST route through the LOCKED
    /// `resolve_conditions` and pass every condition id as a quoted literal.
    #[test]
    fn resolve_payload_routes_through_locked_resolve_conditions() {
        let script = build_deferral_resolve_script(
            Path::new("/orch/root"),
            Path::new("/proj/folder"),
            &["untracked_collision_divergent", "autostash_pop_conflict"],
        );
        assert!(
            script.contains("from vco_lib.deferral_emit import resolve_conditions"),
            "settle payload must import the LOCKED resolve_conditions; got:\n{script}"
        );
        assert!(
            script.contains("resolve_conditions(folder, ["),
            "settle payload must call resolve_conditions(folder, [...]); got:\n{script}"
        );
        assert!(
            script.contains("\"untracked_collision_divergent\"")
                && script.contains("\"autostash_pop_conflict\""),
            "settle payload must carry every condition id as a quoted literal; got:\n{script}"
        );
        // No unlocked writer path leaks in.
        assert!(
            !script.contains("DeferralReport"),
            "settle payload must NOT reference the unlocked DeferralReport; got:\n{script}"
        );
    }

    /// v0.2.91 WP-B: every cid this crate emits through the bridge must be
    /// CLASSIFIED, and classified the way the ledger UI will render it. Pinned
    /// from the emitting side (not only inside the registry module) so a future
    /// Rust emitter cannot ship a condition the table has never heard of.
    #[test]
    fn launcher_emitted_conditions_are_classified() {
        use vct_launcher_core::deferral_registry as reg;

        assert_eq!(reg::disposition_for("launcher_binary_stale"), "action_required");
        assert_eq!(
            reg::disposition_for("launcher_binary_clobber_averted"),
            "informational_record"
        );
        assert_eq!(reg::disposition_for("kg_access_phantom_repaired"), "informational_record");
        assert_eq!(reg::disposition_for("dual_ollama_detected"), "environmental");
        // Unregistered ⇒ conservative default, and it counts as work.
        assert_eq!(reg::disposition_for("no_such_condition_id"), "action_required");
        assert!(reg::is_actionable("no_such_condition_id"));
        // auto_retryable is owed work; a completed record is not.
        assert!(reg::is_actionable("kg_sync_no_embedding_backend"));
        assert!(!reg::is_actionable("launcher_binary_clobber_averted"));
    }

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

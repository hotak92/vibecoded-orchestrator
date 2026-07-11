"""v0.2.46 V47-G-final — Rust/Python detection-heuristic drift gate.

The adopt-mode detection heuristic is implemented twice:

  * Python: ``_detect_third_party_project`` in install.py (canonical — runs
    when install.py is invoked, drives the prompt + dry-run manifest).
  * Rust: ``detect_third_party_project_signals`` in
    launcher/src-tauri/src/commands/installer.rs (UI gate — runs when the
    launcher's Add-Project wizard wants to know whether to show the modal).

Both check the same 5 signals (.claude/, CLAUDE.md, .env, venv, knowledge/)
and the same .vco-manifest.json short-circuit. If a future change adds a 6th
signal to one side without adding it to the other, the launcher's modal will
silently miss the case (worst case is "user not warned in GUI", per Part 2
adversarial review S8).

This test is a STRUCTURAL drift gate: it counts the signal-emit sites in
each source file and asserts equality. It does NOT simulate the runtime
behaviour (that's covered by `tests/test_v0246_v47gfinal_detection_and_prompt.py`
on the Python side and the Rust unit tests on the Rust side). The gate's
purpose is to catch one-sided changes at code-review time.

When this test fails after an intentional signal addition, update BOTH
sides + bump the expected counts in this file.
"""
from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = PROJECT_ROOT / "install.py"
RUST_INSTALLER = PROJECT_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "installer.rs"


def _rust_installer_source() -> str:
    """installer.rs facade + its installer/*.rs submodules, concatenated.

    v0.2.77 Part 7d split installer.rs into a facade + `installer/`
    submodules; `detect_third_party_project_signals` (the Rust mirror of
    install.py::_detect_third_party_project) moved into
    `installer/inspect.rs`. Scan the whole submodule set so the drift
    check follows the mirror wherever it lands.
    """
    parts = [RUST_INSTALLER.read_text(encoding="utf-8")]
    submod_dir = RUST_INSTALLER.parent / "installer"
    if submod_dir.is_dir():
        for f in sorted(submod_dir.glob("*.rs")):
            parts.append(f.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _extract_python_detection_body() -> str:
    """Return the source body of `_detect_third_party_project` in install.py.

    Slices from the `def _detect_third_party_project(` line through the
    function's matching `return None` epilogue. The function ends at the next
    top-level `def` declaration.
    """
    text = INSTALL_PY.read_text(encoding="utf-8")
    start_m = re.search(
        r"^def _detect_third_party_project\(",
        text, flags=re.MULTILINE,
    )
    assert start_m, "Python: _detect_third_party_project not found in install.py"
    body_start = start_m.start()
    # Find the next top-level `def ` or `class ` after the body.
    end_m = re.search(
        r"\n(def |class )",
        text[start_m.end():],
    )
    body_end = start_m.end() + end_m.start() if end_m else len(text)
    return text[body_start:body_end]


def _extract_rust_detection_body() -> str:
    """Return the source body of `detect_third_party_project_signals` in installer.rs."""
    text = _rust_installer_source()
    start_m = re.search(
        r"pub fn detect_third_party_project_signals\(",
        text,
    )
    assert start_m, "Rust: detect_third_party_project_signals not found in installer.rs"
    body_start = start_m.start()
    # Find the next top-level `pub fn ` or `fn ` after the body. The Rust
    # function ends with its matching `}` then a blank line then either
    # another top-level item or the section divider. We slice to the next
    # `// ---` divider OR the next `pub fn`/`fn ` whichever comes first.
    rest = text[start_m.end():]
    end_m = re.search(
        r"\n(// ---|pub fn |fn )",
        rest,
    )
    body_end = start_m.end() + end_m.start() if end_m else len(text)
    return text[body_start:body_end]


def _count_signal_emits_python(body: str) -> int:
    """Count Python `signals.append(` calls — each one emits a detection signal."""
    return len(re.findall(r"\bsignals\.append\(", body))


def _count_signal_emits_rust(body: str) -> int:
    """Count Rust `out.signals.push(` calls — each one emits a detection signal."""
    return len(re.findall(r"\bout\.signals\.push\(", body))


# ---------------------------------------------------------------------------
# Drift gate tests
# ---------------------------------------------------------------------------

# v0.2.46 V47-G-final shipped with 5 signals on each side.
# v0.2.46 post-adversarial L2 added a 6th signal (broken-manifest).
# If you add a signal: bump this constant + update both detection functions +
# update the Svelte modal's "Show details" rendering. All three layers move
# together by design.
EXPECTED_SIGNAL_COUNT = 6


def test_python_detection_has_expected_signal_count():
    """Python side has the documented number of signal-emit sites."""
    body = _extract_python_detection_body()
    n = _count_signal_emits_python(body)
    assert n == EXPECTED_SIGNAL_COUNT, (
        f"Python _detect_third_party_project has {n} signals.append() calls, "
        f"expected {EXPECTED_SIGNAL_COUNT}. If this is an intentional addition, "
        f"bump EXPECTED_SIGNAL_COUNT in this test AND add the corresponding "
        f"signal to the Rust mirror in installer.rs."
    )


def test_rust_detection_has_expected_signal_count():
    """Rust side has the documented number of signal-emit sites."""
    body = _extract_rust_detection_body()
    n = _count_signal_emits_rust(body)
    assert n == EXPECTED_SIGNAL_COUNT, (
        f"Rust detect_third_party_project_signals has {n} out.signals.push() "
        f"calls, expected {EXPECTED_SIGNAL_COUNT}. If this is an intentional "
        f"addition, bump EXPECTED_SIGNAL_COUNT in this test AND add the "
        f"corresponding signal to the Python source in install.py."
    )


def test_python_and_rust_signal_counts_match():
    """The two implementations emit the same number of signals.

    This is the actual drift gate — if one side adds a 6th signal without
    the other, the GUI silently misses it. This test catches that one-sided
    change at code-review time before it ships.
    """
    py_body = _extract_python_detection_body()
    rs_body = _extract_rust_detection_body()
    py_n = _count_signal_emits_python(py_body)
    rs_n = _count_signal_emits_rust(rs_body)
    assert py_n == rs_n, (
        f"DRIFT: Python emits {py_n} signals, Rust emits {rs_n}. "
        f"The launcher's Add-Project modal mirrors the Python heuristic in "
        f"Rust for speed; if Python adds a signal, the Rust mirror must too "
        f"(or the GUI silently fails to warn the user about that signal). "
        f"Update {RUST_INSTALLER.relative_to(PROJECT_ROOT)} or "
        f"{INSTALL_PY.relative_to(PROJECT_ROOT)} so both sides agree."
    )


def test_both_check_vco_manifest_short_circuit():
    """Both implementations check `.vco-manifest.json` as a hard short-circuit.

    The contract is: when the manifest is present, the project is an EXISTING
    VCO project — never treat as 3rd-party, never prompt. Both sides must
    encode this. If either side drops the check, the modal will pop on
    legitimate update flows.
    """
    py_body = _extract_python_detection_body()
    rs_body = _extract_rust_detection_body()
    assert ".vco-manifest.json" in py_body, (
        "Python detection lost the .vco-manifest.json short-circuit"
    )
    assert ".vco-manifest.json" in rs_body, (
        "Rust detection lost the .vco-manifest.json short-circuit"
    )


def test_both_check_same_five_signal_targets():
    """Both implementations name the same 5 targets in their source.

    The targets are part-of-API for the launcher GUI's "Show details"
    rendering. If Python renames `.venv` to `python_env` in its detection,
    the Rust mirror must too — otherwise the modal labels diverge.

    This is a string-presence test, not a structural one — drift between
    target NAMES (not just counts) would still pass the count-only gates
    above. This catches "Python added `pyproject.toml` as signal #6 but
    Rust still uses `requirements.txt`".
    """
    py_body = _extract_python_detection_body()
    rs_body = _extract_rust_detection_body()
    expected_targets = [".claude", "CLAUDE.md", ".env", "knowledge"]
    # `.venv` shows up via the _V47G_VENV_DIR_NAMES tuple on the Python side,
    # which is defined OUTSIDE the function body — assert presence in
    # install.py overall rather than the body slice.
    install_py_text = INSTALL_PY.read_text(encoding="utf-8")
    assert "_V47G_VENV_DIR_NAMES" in install_py_text, (
        "Python venv-name list lost — _V47G_VENV_DIR_NAMES tuple missing"
    )
    for target in expected_targets:
        assert target in py_body, (
            f"Python detection lost reference to '{target}'"
        )
        assert target in rs_body, (
            f"Rust detection lost reference to '{target}'"
        )

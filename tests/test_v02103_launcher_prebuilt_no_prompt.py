# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Owner rule 2026-10-15 (plan item 29): when a prebuilt launcher exists for
this OS/arch/version it is downloaded WITHOUT asking; "build from source?" is
asked only when no prebuilt could be obtained.

Field report (v0.2.103 Windows verification): first-install.bat showed the
"Launcher binary not found. Choose how to get it: [1] Download prebuilt /
[2] Build from source / [3] Skip" menu although the release asset existed.
scripts/post-install-launcher.sh had the same menu. The rule now has one
home, ``vco_lib.launcher_acquire`` (api-url + next-step), called by both
shims.

Both arms run here on the REAL Step 3 → 4a region of the .sh (extracted and
executed with network/download stubs) and on the shared rule's CLI. The
.bat sibling cannot run on a Linux host; its wiring is checked through the
same CLI calls it makes and verified on the Windows notebook.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import launcher_acquire as la  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "post-install-launcher.sh"
BAT = REPO_ROOT / "first-install.bat"


# ── the shared rule ──────────────────────────────────────────────────────


def test_next_step_arms():
    assert la.next_step(la.OUTCOME_DOWNLOADED, yes=False) == la.STEP_LAUNCH
    assert la.next_step(la.OUTCOME_DOWNLOADED, yes=True) == la.STEP_LAUNCH
    assert la.next_step(la.OUTCOME_NO_PREBUILT, yes=True) == la.STEP_BUILD
    assert la.next_step(la.OUTCOME_NO_PREBUILT, yes=False) == la.STEP_ASK


def test_api_url_names_this_checkouts_version(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "9.8.7"\n')
    assert la.checkout_version(tmp_path) == "9.8.7"
    assert la.release_api_url("9.8.7").endswith("/releases/tags/v9.8.7")
    assert la.release_api_url(None).endswith("/releases/latest")
    assert la.checkout_version(tmp_path / "missing") is None


def test_cli_answers_both_questions(capsys):
    assert la.main(["next-step", "--outcome", "no-prebuilt"]) == 0
    assert capsys.readouterr().out.strip() == "ask"
    assert la.main(["next-step", "--outcome", "downloaded", "--yes"]) == 0
    assert capsys.readouterr().out.strip() == "launch"
    assert la.main(["api-url"]) == 0
    assert "/releases/tags/v" in capsys.readouterr().out


# ── the .sh: the real Step 3 → 4a region, both arms ──────────────────────


def _region() -> str:
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if "# ----- Step 3:" in ln)
    end = next(i for i, ln in enumerate(lines) if "# ----- Step 4b:" in ln)
    return "\n".join(lines[start:end])


def _function(name: str) -> str:
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(j for j in range(start + 1, len(lines)) if lines[j] == "}")
    return "\n".join(lines[start:end + 1])


LINUX_ZIP = "vibecoded-orchestrator-9.9.9-linux-x64.zip"


def _release(with_linux_zip: bool, *, with_sidecar: bool = True,
             padding: int = 0) -> str:
    assets = [{"name": "vibecoded-orchestrator-9.9.9-windows-x64.zip",
               "browser_download_url": "https://x/win.zip"}]
    if with_linux_zip:
        assets.append({"name": LINUX_ZIP,
                       "browser_download_url": "https://x/linux.zip"})
        if with_sidecar:
            assets.append({"name": LINUX_ZIP + ".sha256",
                           "browser_download_url": "https://x/linux.zip.sha256"})
    doc: dict = {"assets": assets}
    if padding:
        doc["body"] = "x" * padding  # a long release body (R20 L2)
    return json.dumps(doc)


def _zip_with_launcher(path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("vibecoded-orchestrator-9.9.9-linux-x64/vct-launcher")
        info.external_attr = 0o755 << 16
        zf.writestr(info, b"#!/bin/sh\nexit 0\n" + b"x" * 64)
    path.write_bytes(buf.getvalue())


def _run_region(tmp_path: Path, *, prebuilt: bool, yes: int, answer: str = "",
                sidecar: str = "match", padding: int = 0,
                raw_release: str | None = None) -> subprocess.CompletedProcess:
    """``sidecar``: ``match`` (the real digest), ``mismatch`` (another
    digest), ``missing`` (the release lists no .sha256 asset)."""
    release = tmp_path / "release.json"
    release.write_text(raw_release if raw_release is not None else _release(
        prebuilt, with_sidecar=sidecar != "missing", padding=padding))
    payload = tmp_path / "payload.zip"
    _zip_with_launcher(payload)
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    if sidecar == "mismatch":
        digest = "0" * 64
    side = tmp_path / "payload.zip.sha256"
    side.write_text(f"{digest}  {LINUX_ZIP}\n")
    home = tmp_path / "home"
    home.mkdir()
    harness = textwrap.dedent(f"""
        set -uo pipefail
        OS=linux; YES={yes}; HAS_CURL=1; HAS_WGET=0; HAS_PYTHON3=1
        REPO_ROOT={REPO_ROOT!s}
        HOME={home!s}
        _json_escape() {{ printf '%s' "$1"; }}
        _log_event() {{ :; }}
        _fetch() {{ cat {release!s}; }}
        _download() {{
            case "$1" in
                *.sha256) cp {side!s} "$2" ;;
                *) cp {payload!s} "$2" ;;
            esac
        }}
        _stage_built_binary_into_dist() {{ return 1; }}
    """) + _function("_launcher_acquire_python") + "\n" \
        + _function("_launcher_release_api_url") + "\n" \
        + _function("_launcher_after_download") + "\n" \
        + _function("_verify_launcher_download") + "\n" \
        + _region() + '\necho "FINAL_MODE=$MODE"\necho "FINAL_BIN=${LAUNCHER_BIN:-}"\n'
    script = tmp_path / "region.sh"
    script.write_text(harness)
    return subprocess.run(["bash", str(script)], input=answer, capture_output=True,
                          text=True, timeout=120, env={**os.environ, "HOME": str(home)})


def test_prebuilt_available_is_downloaded_without_any_prompt(tmp_path):
    """ACT arm. RED before the fix: the region opened with the three-way
    'Choose how to get it' menu and a `Your choice [1]:` prompt."""
    proc = _run_region(tmp_path, prebuilt=True, yes=0)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "Your choice" not in out and "Choose how" not in out, out
    assert "FINAL_MODE=download" in proc.stdout, out
    assert "FINAL_BIN=" in proc.stdout and "vct-launcher" in proc.stdout.split("FINAL_BIN=")[1]


def test_no_prebuilt_asks_and_honours_skip(tmp_path):
    """The other arm: no asset for this OS → the user is asked; 's' skips."""
    proc = _run_region(tmp_path, prebuilt=False, yes=0, answer="s\n")
    out = proc.stdout + proc.stderr
    assert "No prebuilt launcher is available" in out, out
    assert "Your choice [b]" in out
    assert "Skipped." in out and proc.returncode == 0
    assert "FINAL_MODE" not in proc.stdout  # exited at the skip


def test_no_prebuilt_unattended_builds_without_asking(tmp_path):
    proc = _run_region(tmp_path, prebuilt=False, yes=1)
    out = proc.stdout + proc.stderr
    assert "Your choice" not in out, out
    assert "FINAL_MODE=build" in proc.stdout


def test_rule_unreachable_falls_back_to_asking(tmp_path):
    """LEAVE-ALONE of the conservative default: no interpreter for the rule
    → a question, never a silent build."""
    fn = _function("_launcher_after_download")
    harness = (f"YES=1\nREPO_ROOT={tmp_path}\n"
               "_launcher_acquire_python() { :; }\n" + fn
               + '\nr="$(_launcher_after_download no-prebuilt)"\necho "R=$r"\n')
    proc = subprocess.run(["bash", "-c", harness], input="b\n", capture_output=True,
                          text=True, timeout=30)
    assert "Your choice [b]" in proc.stderr
    assert "R=build" in proc.stdout


# ── the .bat: same rule, same CLI ────────────────────────────────────────


def test_bat_has_no_upfront_menu_and_consults_the_rule_after_a_failed_download():
    """The .bat cannot run here; its two rule calls are the same CLI the
    tests above exercise, and the Windows notebook run verifies the flow."""
    text = BAT.read_text(encoding="utf-8")
    probe_end = text.index('call :_log_event "binary-probe" "skip"')
    download = text.index(":launch_download\n")
    upfront = text[probe_end:download]
    assert "set /p" not in upfront and "Choose how to get it" not in upfront
    assert "vco_lib.launcher_acquire api-url" in text
    no_prebuilt = text.index(":launch_no_prebuilt\n")
    assert "vco_lib.launcher_acquire next-step --outcome no-prebuilt" in text[no_prebuilt:]
    assert text.index("goto :launch_no_prebuilt") < no_prebuilt


def test_bat_hands_the_verifier_to_the_download_helper():
    """R20 M1: the .bat gives the helper the venv interpreter that runs
    verify-sha256 (only when one exists — PS 5.1 rejects an empty -File
    argument), and names the two refusal exits before falling to the
    question. The helper's own verification runs live in
    tests/test_download_launcher_asset_ps1.py."""
    text = BAT.read_text(encoding="utf-8")
    assert 'if defined ACQ_PY set PY_ARG=-PythonExe "%ACQ_PY%"' in text
    download = text[text.index(":launch_download\n"):text.index(":launch_no_prebuilt\n")]
    assert download.count("%PY_ARG%") == 2
    assert 'if "%DL_EXIT%"=="5"' in download and 'if "%DL_EXIT%"=="6"' in download


# ── R20 M1: the download is checked against the release's .sha256 ───────


def test_verified_download_is_used(tmp_path):
    """ACT arm: the sidecar's digest matches → the binary is landed."""
    proc = _run_region(tmp_path, prebuilt=True, yes=0, sidecar="match")
    out = proc.stdout + proc.stderr
    assert "Checksum verified" in out, out
    assert "FINAL_MODE=download" in proc.stdout, out
    assert (tmp_path / "home/.local/share/vct-launcher/vct-launcher").exists()


def test_checksum_mismatch_is_refused_and_falls_to_the_question(tmp_path):
    """REFUSE arm: a wrong digest → nothing unpacked or landed, the user is
    asked about compiling (the existing conservative arm)."""
    proc = _run_region(tmp_path, prebuilt=True, yes=0, sidecar="mismatch",
                       answer="s\n")
    out = proc.stdout + proc.stderr
    assert "Checksum verification FAILED" in out, out
    assert "Your choice [b]" in out and "Skipped." in out, out
    assert not (tmp_path / "home/.local/share/vct-launcher/vct-launcher").exists()


def test_missing_sidecar_is_refused(tmp_path):
    """REFUSE arm: the release lists no .sha256 → an unverified download is
    never run; unattended runs build from source instead."""
    proc = _run_region(tmp_path, prebuilt=True, yes=1, sidecar="missing")
    out = proc.stdout + proc.stderr
    assert "publishes no checksum" in out, out
    assert "FINAL_MODE=build" in proc.stdout, out
    assert "FINAL_BIN=\n" in proc.stdout or proc.stdout.rstrip().endswith("FINAL_BIN="), out
    assert not (tmp_path / "home/.local/share/vct-launcher/vct-launcher").exists()


# ── R20 L2: the release document never rides in an env var/argv ─────────


def test_oversized_release_document_is_still_read(tmp_path):
    """A release document over MAX_ARG_STRLEN (128 KiB) must still yield the
    asset. RED with the env-var hand-off: execve fails with E2BIG and the
    shim silently falls back to building."""
    proc = _run_region(tmp_path, prebuilt=True, yes=1, padding=300_000)
    out = proc.stdout + proc.stderr
    assert "FINAL_MODE=download" in proc.stdout, out
    assert "Argument list too long" not in out, out


def test_unreadable_release_document_is_reported_not_silent(tmp_path):
    """A release document that is not JSON (a proxy page) is SAID to be
    unreadable — never only the silent "no prebuilt" of before."""
    release = tmp_path / "bad.json"
    release.write_text("<html>proxy login</html>")
    rc = la.main(["pick-asset", "--os", "linux", "--release-json", str(release)])
    assert rc == la.PICK_UNREADABLE
    proc = _run_region(tmp_path, prebuilt=True, yes=1,
                       raw_release="<html>proxy login</html>")
    out = proc.stdout + proc.stderr
    assert "The release document could not be read (pick-asset exit 3)" in out, out
    assert "FINAL_MODE=build" in proc.stdout, out


# ── the shared verifier (vco_lib.launcher_acquire verify-sha256) ────────


def _archive(tmp_path: Path) -> tuple[Path, str]:
    f = tmp_path / LINUX_ZIP
    f.write_bytes(b"archive-bytes" * 100)
    return f, hashlib.sha256(f.read_bytes()).hexdigest()


def test_verify_sha256_match_mismatch_missing(tmp_path):
    f, digest = _archive(tmp_path)
    side = tmp_path / "s.sha256"
    side.write_text(f"{digest}  {LINUX_ZIP}\n")
    assert la.verify_sha256(f, side, LINUX_ZIP)[0] == la.VERIFY_OK
    side.write_text(f"{digest.upper()} *{LINUX_ZIP}\n")  # Git-Bash binary marker
    assert la.verify_sha256(f, side, LINUX_ZIP)[0] == la.VERIFY_OK
    side.write_text(f"{'1' * 64}  {LINUX_ZIP}\n")
    code, msg = la.verify_sha256(f, side, LINUX_ZIP)
    assert code == la.VERIFY_MISMATCH and "mismatch" in msg
    assert la.verify_sha256(f, tmp_path / "absent", LINUX_ZIP)[0] == la.VERIFY_UNVERIFIABLE
    side.write_text("<html>not a checksum</html>\n")
    assert la.verify_sha256(f, side, LINUX_ZIP)[0] == la.VERIFY_UNVERIFIABLE
    side.write_text(f"{digest}  some-other-archive.zip\n")
    assert la.verify_sha256(f, side, LINUX_ZIP)[0] == la.VERIFY_UNVERIFIABLE


def test_verify_sha256_cli_exit_codes(tmp_path):
    f, digest = _archive(tmp_path)
    side = tmp_path / "s.sha256"
    side.write_text(f"{digest}  {LINUX_ZIP}\n")
    argv = ["verify-sha256", "--file", str(f), "--sidecar", str(side), "--name", LINUX_ZIP]
    assert la.main(argv) == 0
    side.write_text(f"{'2' * 64}  {LINUX_ZIP}\n")
    # Never 1 (a crashed interpreter) nor 2 (argparse): a crash must not read
    # as a mismatch in download-launcher-asset.ps1 (review R22 N1).
    assert la.main(argv) == la.VERIFY_MISMATCH == 3
    side.unlink()
    assert la.main(argv) == 2

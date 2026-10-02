# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 owner Q5 / L1-F19 — install resume WIRED: a step is skipped only
when the last session completed it AND its side effect verifies.

Evidence the promise was never exercised: the four "Resume Update" runs of
2026-09-29 each started a new session at 1/10 and re-ran every step; the
successful one spent 110 s in 7/10 re-pulling models already present.

Fixture: an install.jsonl whose last session completed steps 1-4 and FAILED at
step 5 (the dogfood shape). No real pip, venv, Ollama or install run.
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from vco_lib import install_resume as ir  # noqa: E402
from vco_lib import ollama_pull as op  # noqa: E402
from vco_lib.embedding_pull_plan import PullPlan  # noqa: E402
from tests.test_v02100_ollama_pull import BASE, GEMMA, QWEN, FakeOllama  # noqa: E402

V = sys.version_info
PY = f"{V.major}.{V.minor}.{V.micro}"


def _ev(step, phase, detail="", data=None, actor="install.py"):
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "actor": actor, "step": step,
           "phase": phase, "detail": detail}
    if data is not None:
        rec["data"] = data
    return json.dumps(rec)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    for rel in ir.DEPS_FINGERPRINT_FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f"{rel}\n", encoding="utf-8")
    return root


def _failed_at_step_5_log(tmp_path: Path, root: Path) -> Path:
    log = tmp_path / "install.jsonl"
    log.write_text("\n".join([
        _ev("session", "start"),
        _ev("1/10", "start"), _ev("1/10", "ok", data={"version": PY}),
        _ev("2/10", "start"), _ev("2/10", "ok"),
        _ev("choices", "ok", "embedding_mode", data={"value": "cpu"}),
        _ev("3/10", "start"), _ev("3/10", "ok"),
        _ev("4/10", "start"), _ev("4/10", "ok", data=ir.deps_fingerprint(root, dev=False)),
        _ev("5/10", "start"), _ev("5/10", "error", "compose up failed (exit 1)"),
    ]) + "\n", encoding="utf-8")
    return log


def _ok_run(argv, **_k):
    return subprocess.CompletedProcess(argv, 0, "3.12.1\n", "")


def _skip(session, step, verify):
    events, buf = [], io.StringIO()
    with redirect_stdout(buf):
        skipped = ir.verified_skip(session, step, "Step", verify,
                                   log_event=lambda *a, **k: events.append((a, k)))
    return skipped, buf.getvalue(), events


# ── the session reader ──────────────────────────────────────────────────────


def test_a_step_5_failure_leaves_steps_1_to_4_completed_and_5_not(tmp_path):
    root = _root(tmp_path)
    s = ir.load_session(_failed_at_step_5_log(tmp_path, root))
    assert [st for st in ("1/10", "2/10", "3/10", "4/10", "5/10") if s.completed(st)] == \
        ["1/10", "2/10", "3/10", "4/10"]
    assert s.choices == {"embedding_mode": {"value": "cpu"}}


def test_no_resume_reads_nothing(tmp_path):
    root = _root(tmp_path)
    s = ir.load_session(_failed_at_step_5_log(tmp_path, root), enabled=False)
    assert not any(s.completed(st) for st in ("1/10", "3/10", "4/10")) and s.choices == {}


# ── step 1: same interpreter ────────────────────────────────────────────────


def test_step_1_verified_skip_and_the_session_marker_is_still_logged(tmp_path):
    s = ir.load_session(_failed_at_step_5_log(tmp_path, _root(tmp_path)))
    skipped, out, events = _skip(s, "1/10", ir.python_verifier())
    assert skipped and "verified, skipped" in out
    assert [e[0][1] for e in events] == ["start", "skip"]  # 1/10 start = next run's marker


def test_step_1_reruns_when_the_interpreter_changed(tmp_path):
    s = ir.load_session(_failed_at_step_5_log(tmp_path, _root(tmp_path)))
    skipped, out, events = _skip(s, "1/10", ir.python_verifier(version="3.99.0"))
    assert not skipped and "running it again" in out and events == []


# ── step 3: the venv interpreter runs ───────────────────────────────────────


def test_venv_verifier_needs_a_running_interpreter(tmp_path):
    py = tmp_path / "python"
    assert ir.venv_python_runs(py, run=_ok_run)[0] is False  # missing
    py.write_text("", encoding="utf-8")
    assert ir.venv_python_runs(py, run=_ok_run) == (True, "venv Python 3.12.1 runs")
    bad = ir.venv_python_runs(py, run=lambda a, **k: subprocess.CompletedProcess(a, 1, "", "x"))
    assert bad[0] is False


def test_create_venv_skips_a_verified_venv_and_repairs_a_broken_one(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    vpy = root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    vpy.parent.mkdir(parents=True)
    vpy.write_text("", encoding="utf-8")
    monkeypatch.setattr(install, "_log_install_event", lambda *a, **k: None)
    built = []
    monkeypatch.setattr(install.subprocess, "run",
                        lambda argv, **k: built.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(install._install_resume, "venv_python_runs", lambda p: (True, "venv Python 3.12.1 runs"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        install._create_venv(root)
    assert built == [] and "verified, skipped" in buf.getvalue()
    monkeypatch.setattr(install._install_resume, "venv_python_runs", lambda p: (False, "exited 1"))
    install._create_venv(root)
    assert built and built[0][1:3] == ["-m", "venv"]


# ── step 4: fingerprint + pip check + editable-import probe ─────────────────


def _report(**kw):
    rep = {"origin": None, "locations": [], "file": None, "error": None}
    rep.update(kw)
    return json.dumps(rep) + "\n"


def _checkout_report(root):
    pkg = root / "vco_lib"
    return _report(origin=str(pkg / "__init__.py"), locations=[str(pkg)],
                   file=str(pkg / "__init__.py"))


def _deps(root, calls, fail=None, report=None, cwds=None):
    def run(argv, **k):
        calls.append(argv)
        if cwds is not None:
            cwds.append(k.get("cwd"))
        rc = 1 if fail and fail in " ".join(argv) else 0
        out = ""
        if rc == 0 and ir._EDITABLE_PROBE in argv:
            out = report if report is not None else _checkout_report(root)
        return subprocess.CompletedProcess(argv, rc, out, "broken: x" if rc else "")
    return ir.deps_verifier(root, Path("/v/python"), dev=False, weaviate_mcp_probe="import x",
                            run=run)


def test_step_4_verified_skip_runs_real_verifiers(tmp_path):
    root = _root(tmp_path)
    s = ir.load_session(_failed_at_step_5_log(tmp_path, root))
    calls = []
    skipped, out, _ = _skip(s, "4/10", _deps(root, calls))
    assert skipped and "pip check OK" in out
    assert ["/v/python", "-I", "-m", "pip", "check"] in calls
    assert ["/v/python", "-I", "-c", ir._EDITABLE_PROBE] in calls  # the editable-import probe ran


def test_step_4_probes_run_isolated_from_a_cwd_outside_the_checkout(tmp_path):
    """W4R-02: with ``cwd=root`` and no ``-I``, ``sys.path[0]`` is the cwd and
    ``import vco_lib`` resolves from the checkout on every machine."""
    root = _root(tmp_path)
    calls, cwds = [], []
    ok, _ = _deps(root, calls, cwds=cwds)({"deps_fingerprint": ir.deps_fingerprint(
        root, dev=False)["deps_fingerprint"]})
    assert ok and len(calls) == 3
    for argv, cwd in zip(calls, cwds):
        assert argv[1] == "-I", argv
        assert cwd is not None
        here = Path(cwd).resolve()
        assert here != root.resolve() and root.resolve() not in here.parents


@pytest.mark.parametrize("report, expect", [
    ("site", "site-packages copy"),
    ("site_in_root", "site-packages copy"),
    ("dist_in_root", "site-packages copy"),
    ("copy_in_root", "not the checkout's own"),
    ("elsewhere", "outside the checkout"),
    ("missing", "not installed"),
    ("error", "does not import"),
    ("garbage", "no report"),
])
def test_step_4_reruns_when_vco_lib_is_not_served_by_the_checkout(tmp_path, report, expect):
    """ACT: the probe reports a shadow copy / no editable install → run step 4."""
    root = _root(tmp_path)
    shadow = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "vco_lib"
    # R18-02: the DEFAULT layout — install.py's venv lives inside the checkout.
    in_root = root / ".venv" / "lib" / "python3.12" / "site-packages" / "vco_lib"
    dist = root / ".venv" / "lib" / "python3" / "dist-packages" / "vco_lib"
    build = root / "build" / "lib" / "vco_lib"

    def _pkg(d):
        return _report(origin=str(d / "__init__.py"), locations=[str(d)],
                       file=str(d / "__init__.py"))
    rep = {
        "site": _pkg(shadow),
        "site_in_root": _pkg(in_root),
        "dist_in_root": _pkg(dist),
        "copy_in_root": _pkg(build),
        "elsewhere": _report(origin=str(tmp_path / "other" / "vco_lib" / "__init__.py"),
                             locations=[str(tmp_path / "other" / "vco_lib")]),
        "missing": _report(),
        "error": _report(origin=str(root / "vco_lib" / "__init__.py"),
                         error="ImportError: boom"),
        "garbage": "not json\n",
    }[report]
    s = ir.load_session(_failed_at_step_5_log(tmp_path, root))
    skipped, out, events = _skip(s, "4/10", _deps(root, [], report=rep))
    assert not skipped and expect in out and events == []


def test_step_4_skips_when_vco_lib_resolves_in_the_checkout(tmp_path):
    """LEAVE-ALONE: an editable install that serves the checkout verifies."""
    root = _root(tmp_path)
    s = ir.load_session(_failed_at_step_5_log(tmp_path, root))
    skipped, out, _ = _skip(s, "4/10", _deps(root, [], report=_checkout_report(root)))
    assert skipped and "editable imports OK" in out


def _real_venv(tmp_path: Path, env: "Path | None" = None) -> "tuple[Path, Path]":
    """A real (pip-less) venv at ``env`` (default: a sibling of the checkout);
    returns (interpreter, purelib)."""
    import os
    import venv
    env = env if env is not None else tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(env)
    py = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    purelib = subprocess.run(
        [str(py), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True, check=True, timeout=60).stdout.strip()
    return py, Path(purelib)


def _real_deps(root, py):
    """Real interpreter for the probes; ``pip check`` faked (the venv has no pip)."""
    def run(argv, **k):
        if argv[-3:] == ["-m", "pip", "check"]:
            return subprocess.CompletedProcess(argv, 0, "No broken requirements found.\n", "")
        return subprocess.run(argv, **k)
    return ir.deps_verifier(root, py, dev=False, weaviate_mcp_probe="import sys", run=run)


def _checkout_with_vco_lib(tmp_path):
    root = _root(tmp_path)
    (root / "vco_lib").mkdir()
    (root / "vco_lib" / "__init__.py").write_text("WHERE = 'checkout'\n", encoding="utf-8")
    return root


def test_step_4_real_interpreter_detects_a_site_packages_shadow_copy(tmp_path):
    """ACT, against a real interpreter: a frozen ``vco_lib`` in the venv's
    site-packages (no editable install) must fail the verifier even though the
    checkout has its own ``vco_lib`` — the case ``cwd=root`` could never see."""
    root = _checkout_with_vco_lib(tmp_path)
    py, purelib = _real_venv(tmp_path)
    (purelib / "vco_lib").mkdir()
    (purelib / "vco_lib" / "__init__.py").write_text("WHERE = 'shadow'\n", encoding="utf-8")
    ok, why = _real_deps(root, py)({"deps_fingerprint": ir.deps_fingerprint(
        root, dev=False)["deps_fingerprint"]})
    assert not ok and "site-packages copy" in why, why


def test_step_4_real_interpreter_accepts_an_editable_install(tmp_path):
    """LEAVE-ALONE, against a real interpreter: a ``.pth`` editable install
    pointing at the checkout verifies."""
    root = _checkout_with_vco_lib(tmp_path)
    py, purelib = _real_venv(tmp_path)
    (purelib / "__editable__.vco_test.pth").write_text(f"{root}\n", encoding="utf-8")
    ok, why = _real_deps(root, py)({"deps_fingerprint": ir.deps_fingerprint(
        root, dev=False)["deps_fingerprint"]})
    assert ok, why


def test_step_4_real_interpreter_detects_a_shadow_copy_in_an_in_root_venv(tmp_path):
    """ACT (R18-02), against a real interpreter, DEFAULT layout: the venv is
    ``<root>/.venv`` — inside the checkout — and holds a frozen ``vco_lib``.
    The copy is "under root", which the pre-fix verdict accepted."""
    root = _checkout_with_vco_lib(tmp_path)
    py, purelib = _real_venv(tmp_path, root / ".venv")
    assert root.resolve() in purelib.resolve().parents
    (purelib / "vco_lib").mkdir()
    (purelib / "vco_lib" / "__init__.py").write_text("WHERE = 'shadow'\n", encoding="utf-8")
    ok, why = _real_deps(root, py)({"deps_fingerprint": ir.deps_fingerprint(
        root, dev=False)["deps_fingerprint"]})
    assert not ok and "site-packages copy" in why, why


def test_step_4_real_interpreter_accepts_an_editable_install_in_an_in_root_venv(tmp_path):
    """LEAVE-ALONE (R18-02), DEFAULT layout: ``<root>/.venv`` with a ``.pth``
    editable install pointing at the checkout verifies."""
    root = _checkout_with_vco_lib(tmp_path)
    py, purelib = _real_venv(tmp_path, root / ".venv")
    (purelib / "__editable__.vco_test.pth").write_text(f"{root}\n", encoding="utf-8")
    ok, why = _real_deps(root, py)({"deps_fingerprint": ir.deps_fingerprint(
        root, dev=False)["deps_fingerprint"]})
    assert ok, why


def test_step_4_real_interpreter_detects_a_missing_editable_install(tmp_path):
    """ACT: no editable install and no copy — vco_lib is simply not in the venv."""
    root = _checkout_with_vco_lib(tmp_path)
    py, _ = _real_venv(tmp_path)
    ok, why = _real_deps(root, py)({"deps_fingerprint": ir.deps_fingerprint(
        root, dev=False)["deps_fingerprint"]})
    assert not ok and "not installed" in why, why


@pytest.mark.parametrize("why", ["pip check", "vco_lib", "requirements"])
def test_step_4_reruns_when_any_verifier_fails(tmp_path, why):
    root = _root(tmp_path)
    s = ir.load_session(_failed_at_step_5_log(tmp_path, root))
    if why == "requirements":
        (root / "requirements.txt").write_text("changed\n", encoding="utf-8")
        verify = _deps(root, [])
    else:
        verify = _deps(root, [], fail=why)
    skipped, out, events = _skip(s, "4/10", verify)
    assert not skipped and "running it again" in out and events == []


def test_install_requirements_is_wired_to_the_verifier(tmp_path, monkeypatch):
    """ACT: a verified step 4 runs no pip at all. LEAVE-ALONE: a failing
    verifier (or --no-resume) runs pip."""
    root = _root(tmp_path)
    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    monkeypatch.setattr(install, "_log_install_event", lambda *a, **k: None)
    session = ir.load_session(_failed_at_step_5_log(tmp_path, root))
    monkeypatch.setattr(install, "_RESUME_SESSION", session)

    class Pip(Exception):
        pass

    def pip(*a, **k):
        raise Pip()

    monkeypatch.setattr(install, "_run_logged_subprocess", pip)
    monkeypatch.setattr(install._install_resume, "deps_verifier",
                        lambda *a, **k: (lambda rec: (True, "dependencies unchanged")))
    install._install_requirements(Path("/v/python"), dev=False)  # no pip → no Pip raised

    monkeypatch.setattr(install._install_resume, "deps_verifier",
                        lambda *a, **k: (lambda rec: (False, "pip check failed")))
    with pytest.raises(Pip):
        install._install_requirements(Path("/v/python"), dev=False)

    monkeypatch.setattr(install, "_RESUME_SESSION", ir.Session(enabled=False, loaded=True))
    monkeypatch.setattr(install._install_resume, "deps_verifier",
                        lambda *a, **k: (lambda rec: (True, "would skip")))
    with pytest.raises(Pip):  # --no-resume: runs even though it would verify
        install._install_requirements(Path("/v/python"), dev=False)


def test_install_records_the_fingerprint_it_will_verify_against(tmp_path):
    root = _root(tmp_path)
    fp = ir.deps_fingerprint(root, dev=True)["deps_fingerprint"]
    assert fp["dev"] is True and set(fp["files"]) == set(ir.DEPS_FINGERPRINT_FILES)
    assert all(v for v in fp["files"].values())


# ── embedding profile: the recorded choice replays (snapshot BEFORE the marker)


def test_previous_choices_come_from_the_snapshot_not_this_runs_empty_session(tmp_path, monkeypatch):
    """The replay never fired on an --update before v0.2.100: read AFTER this
    run's `1/10 start`, "the latest session" was this run's (empty) one."""
    root = _root(tmp_path)
    log = _failed_at_step_5_log(tmp_path, root)
    snapshot = ir.load_session(log)
    with log.open("a", encoding="utf-8") as fh:  # this run's marker lands
        fh.write(_ev("1/10", "start") + "\n")
    assert ir.load_session(log).choices == {}  # the old read-after-marker answer
    monkeypatch.setattr(install, "_RESUME_SESSION", snapshot)
    assert install._load_previous_choices() == {"embedding_mode": {"value": "cpu"}}


def test_profile_replay_prints_verified_and_no_resume_redetects(monkeypatch):
    monkeypatch.setattr(install, "_record_install_choice", lambda *a, **k: None)
    args = argparse.Namespace(openai_key=None, low_resource=False, cpu_only=False)
    monkeypatch.setattr(install, "_RESUME_SESSION",
                        ir.Session(choices={"embedding_mode": {"value": "cpu"}}, loaded=True))
    buf = io.StringIO()
    with redirect_stdout(buf):
        cfg = install._choose_embedding_config(mock.Mock(vram_gb=0, ram_gb=0, has_gpu=False), args)
    assert "verified, skipped" in buf.getvalue() and cfg["code_model"]
    monkeypatch.setattr(install, "_RESUME_SESSION", ir.Session(enabled=False, loaded=True))
    buf = io.StringIO()
    with redirect_stdout(buf), mock.patch.object(install, "_probe_cpu_cores", return_value=2):
        install._choose_embedding_config(mock.Mock(vram_gb=0.0, ram_gb=4.0, has_gpu=False), args)
    assert "verified, skipped" not in buf.getvalue()


# ── step 7: /api/tags lists every planned model ─────────────────────────────


def test_step_7_verified_skipped_when_every_model_is_present():
    fake = FakeOllama(present={QWEN, GEMMA})
    buf = io.StringIO()
    with redirect_stdout(buf):
        res = op.ensure_plan_step(PullPlan(embedding=(QWEN,), inference=(GEMMA,)),
                                  {"ollama_url": BASE}, None, log_event=lambda *a, **k: None,
                                  http=fake)
    assert fake.pulled == [] and res.present == [QWEN, GEMMA]
    assert "verified, skipped (2 models present)" in buf.getvalue()


def test_step_7_no_resume_pulls_even_present_models():
    fake = FakeOllama(present={QWEN, GEMMA})
    buf = io.StringIO()
    with redirect_stdout(buf):
        op.ensure_plan_step(PullPlan(embedding=(QWEN,), inference=(GEMMA,)),
                            {"ollama_url": BASE}, None, log_event=lambda *a, **k: None,
                            resume=False, http=fake)
    assert fake.pulled == [QWEN, GEMMA] and "verified, skipped" not in buf.getvalue()

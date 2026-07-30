# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.89 BUG 3 — kg-sync project-root resolution (Fabio field audit).

Background
----------
Pre-fix, ``sync_knowledge_graph.py`` resolved its target root from the
inherited ``KG_BASE_DIR`` env (script-location fallback), and
``_resolve_collections()`` keyed the hub resolver off the same value. Every
Claude Code session exports ``KG_BASE_DIR`` via ``.claude/settings.json
env``, so a wrapper run from a session whose env belonged to ANOTHER project
inherited the foreign root and synced the WRONG project's tree into the
WRONG project's collections — coherently, i.e. with false success and zero
diagnostics ("118 succeeded" against the wrong project).

Fix (plan §1.3): layered precedence with a NEW, non-leaking env name::

    1. --project-root <path>   argv   (explicit intent — highest)
    2. KG_SYNC_PROJECT_ROOT    env    (launcher + wrappers only; Claude
                                       sessions never export it → can't leak)
    3. KG_BASE_DIR             env    (legacy; honored, logged as legacy)
    4. script location                (_PROJECT_HOME)

plus an unconditional resolution banner (root + winning channel) and a
validation leg: ``--all`` / ``--all-docs`` against a root with neither
``knowledge/`` nor a docs root → explicit error naming root + source,
``exit 2``. The wrappers (``kg-sync`` / ``kg-sync.ps1``) pin
``KG_SYNC_PROJECT_ROOT`` set-if-unset from their own location, so a
direct-CLI run wins over a poisoned session env while the launcher's
explicit value survives the v0.2.77 orchestrator-copy wrapper fallback.

Test strategy
-------------
* Pre-scan: pure-function unit tests on ``_extract_cli_project_root``
  (module loaded via importlib — no subprocess needed).
* Precedence matrix: subprocess ``--all`` runs against roots that lack
  ``knowledge/``/``docs/`` — the validation leg exits 2 BEFORE any
  service construction, so these are CI-safe (no Weaviate / Ollama), and
  the banner names exactly which root + channel won. WEAVIATE_URL /
  OLLAMA_URL additionally point at unroutable endpoints so a precedence
  REGRESSION (root resolving to a tree that HAS knowledge/) fails fast
  instead of touching live services.
* The Fabio repro + launcher-fallback leave-alone go through the REAL
  bash wrapper (subprocess), with a fake venv whose python execs the
  test interpreter.
* Wrapper parity + install.py seed pins: source-level assertions
  (established convention for cross-OS / large-file pins, see
  ``test_v0249_bug_k_kg_sync_venv_picker.py``).
"""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
KG_SYNC_SH = REPO_ROOT / "templates" / "scripts" / "kg-sync"
KG_SYNC_PS1 = REPO_ROOT / "templates" / "scripts" / "kg-sync.ps1"
INSTALL_PY = REPO_ROOT / "install.py"

#: Env keys that would leak a foreign root / collection into the runs below.
_LEAKY_KEYS = (
    "KG_BASE_DIR",
    "KG_SYNC_PROJECT_ROOT",
    "KG_COLLECTION",
    "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION",
    "DEV_DOCS_ROOT",
    "VCT_INSTALL_ROOT",
    "VIRTUAL_ENV",
    "VCT_ORCHESTRATOR_ROOT",
)


def _deps_available() -> bool:
    """True when the test interpreter can import the script's hard deps."""
    probe = (
        "import sys; sys.path.insert(0, %r); "
        "import weaviate, weaviate_mcp, yaml; "
        "import vco_lib.embedding_service" % str(REPO_ROOT)
    )
    try:
        r = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return False


_HAVE_DEPS = _deps_available()


def _base_env(**extra: str) -> dict:
    """Controlled subprocess env: leaky keys stripped, hub disabled,
    services unroutable (a regression must fail fast, never touch live
    infrastructure)."""
    env = {k: v for k, v in os.environ.items() if k not in _LEAKY_KEYS}
    env["VCT_DISABLE_HUB_RESOLVER"] = "1"
    env["KG_COLLECTION"] = "V0289RootTestKG"
    # Unroutable on purpose — see module docstring.
    env["WEAVIATE_URL"] = "http://127.0.0.1:9"
    env["OLLAMA_URL"] = "http://127.0.0.1:9"
    # vco_lib lives in the orchestrator clone; copied-script runs need this.
    env["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    env.update(extra)
    return env


def _run_script(args: list[str], env: dict, script: Path = SCRIPT_PATH,
                timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)] + args,
        env=env, capture_output=True, text=True, timeout=timeout,
    )


def _load_sync_module(project_root: Path):
    """Import the sync script as a module with a controlled env (the
    established importlib pattern — see test_v0270_shipped_embedding_ingest)."""
    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = "V0289RootTestKG"
    os.environ["DEVELOPMENT_COLLECTION"] = ""
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
    os.environ.pop("KG_SYNC_PROJECT_ROOT", None)

    mod_name = f"_sync_kg_root_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest(
            f"sync_knowledge_graph.py has runtime deps not installed ({exc})"
        )
    return mod


class PreScanUnitTests(unittest.TestCase):
    """Plan §1.4: the argv pre-scan extracts + REMOVES the flag so main()'s
    manual positional dispatch never sees it; absent flag → argv untouched."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self) -> None:
        for k in ("KG_BASE_DIR", "KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                  "VCT_DISABLE_HUB_RESOLVER", "KG_SYNC_PROJECT_ROOT"):
            os.environ.pop(k, None)

    def test_space_form_extracted_and_removed(self) -> None:
        mod = _load_sync_module(self.root)
        argv = ["prog", "--project-root", "/some/target", "--all"]
        got = mod._extract_cli_project_root(argv)
        self.assertEqual(got, Path(os.path.abspath("/some/target")))
        self.assertEqual(argv, ["prog", "--all"],
                         "flag + value must be REMOVED so --all dispatch is untouched")

    def test_equals_form_extracted_and_removed_file_list(self) -> None:
        mod = _load_sync_module(self.root)
        argv = ["prog", "--project-root=/some/target", "f1.md", "f2.md"]
        got = mod._extract_cli_project_root(argv)
        self.assertEqual(got, Path(os.path.abspath("/some/target")))
        self.assertEqual(argv, ["prog", "f1.md", "f2.md"],
                         "explicit file list must survive the pre-scan intact")

    def test_absent_flag_leaves_argv_untouched(self) -> None:
        mod = _load_sync_module(self.root)
        argv = ["prog", "--all"]
        got = mod._extract_cli_project_root(argv)
        self.assertIsNone(got)
        self.assertEqual(argv, ["prog", "--all"])

    def test_missing_value_is_usage_error(self) -> None:
        mod = _load_sync_module(self.root)
        with self.assertRaises(SystemExit) as ctx:
            mod._extract_cli_project_root(["prog", "--project-root"])
        self.assertEqual(ctx.exception.code, 2)

    def test_resolver_precedence_pure(self) -> None:
        """In-process precedence check on ``_resolve_project_root`` itself:
        KG_SYNC_PROJECT_ROOT beats KG_BASE_DIR beats script location."""
        mod = _load_sync_module(self.root)
        # Module was loaded with KG_BASE_DIR=self.root and no new-name env:
        self.assertEqual(mod.PROJECT_ROOT, self.root)
        self.assertIn("KG_BASE_DIR", mod._PROJECT_ROOT_SOURCE)
        self.assertIn("legacy", mod._PROJECT_ROOT_SOURCE)
        # New env name now outranks the (still-set) legacy one:
        os.environ["KG_SYNC_PROJECT_ROOT"] = "/new/channel/root"
        root, source = mod._resolve_project_root()
        self.assertEqual(root, Path("/new/channel/root"))
        self.assertEqual(source, "KG_SYNC_PROJECT_ROOT")
        # Empty new-name value is treated as unset → falls back to legacy:
        os.environ["KG_SYNC_PROJECT_ROOT"] = "   "
        root, source = mod._resolve_project_root()
        self.assertEqual(root, self.root)
        self.assertIn("KG_BASE_DIR", source)


@unittest.skipUnless(_HAVE_DEPS, "script runtime deps not installed")
class RootPrecedenceSubprocessTests(unittest.TestCase):
    """Precedence matrix through the real script (validation-leg exit 2 —
    fires BEFORE any service construction, so these runs are network-free)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # A root with NEITHER knowledge/ nor docs/ → validation exit 2.
        self.empty_a = self.tmp / "empty_a"
        self.empty_a.mkdir()
        self.empty_b = self.tmp / "empty_b"
        self.empty_b.mkdir()
        # A root WITH knowledge/ — the "foreign but plausible" tree a
        # precedence regression would silently sync.
        self.with_knowledge = self.tmp / "with_knowledge"
        (self.with_knowledge / "knowledge").mkdir(parents=True)

    def test_argv_beats_both_env_channels(self) -> None:
        p = _run_script(
            ["--project-root", str(self.empty_a), "--all"],
            _base_env(
                KG_SYNC_PROJECT_ROOT=str(self.with_knowledge),
                KG_BASE_DIR=str(self.with_knowledge),
            ),
        )
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("source: --project-root", p.stdout)
        self.assertIn(str(self.empty_a), p.stdout)
        self.assertIn(str(self.empty_a), p.stderr)  # error names the root

    def test_new_env_beats_legacy_env(self) -> None:
        p = _run_script(
            ["--all"],
            _base_env(
                KG_SYNC_PROJECT_ROOT=str(self.empty_a),
                KG_BASE_DIR=str(self.with_knowledge),
            ),
        )
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("source: KG_SYNC_PROJECT_ROOT", p.stdout)
        self.assertIn(str(self.empty_a), p.stdout)

    def test_legacy_env_beats_script_location(self) -> None:
        # The REPO script's location root HAS knowledge/ — if location won,
        # this run would NOT exit 2. KG_BASE_DIR pointing at an empty root
        # exiting 2 therefore proves legacy-env > location.
        p = _run_script(["--all"], _base_env(KG_BASE_DIR=str(self.empty_a)))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("KG_BASE_DIR (legacy env", p.stdout)
        self.assertIn(str(self.empty_a), p.stdout)

    def test_script_location_fallback(self) -> None:
        # Copy the script into a bare tmp project: with no env channels the
        # root must be the copy's OWN tree (which lacks knowledge/ → exit 2,
        # source "script location").
        proj = self.tmp / "bare_project"
        scripts = proj / ".claude" / "scripts"
        scripts.mkdir(parents=True)
        copy = scripts / "sync_knowledge_graph.py"
        copy.write_text(SCRIPT_PATH.read_text(encoding="utf-8"),
                        encoding="utf-8")
        p = _run_script(["--all"], _base_env(), script=copy)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("source: script location", p.stdout)
        self.assertIn(str(proj), p.stdout)

    def test_validation_error_names_root_and_source(self) -> None:
        p = _run_script(
            ["--all-docs"], _base_env(KG_SYNC_PROJECT_ROOT=str(self.empty_b))
        )
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn(str(self.empty_b), p.stderr)
        self.assertIn("KG_SYNC_PROJECT_ROOT", p.stderr)
        self.assertIn("--project-root", p.stderr)  # remediation hint


@unittest.skipUnless(_HAVE_DEPS, "script runtime deps not installed")
@unittest.skipIf(sys.platform == "win32", "bash wrapper path (POSIX only)")
class WrapperRootPinningTests(unittest.TestCase):
    """The Fabio repro + the launcher-fallback leave-alone leg, through the
    REAL bash wrapper."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        # Fake venv whose python execs THIS interpreter — passes the
        # wrapper's `import weaviate, weaviate_mcp` probe for real.
        self.install_root = self.tmp / "install_root"
        bin_dir = self.install_root / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        fake_py = bin_dir / "python"
        fake_py.write_text(
            f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n',
            encoding="utf-8",
        )
        fake_py.chmod(fake_py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

        # "Project B" — the tree the wrapper lives in. NO knowledge/ →
        # correct resolution exits 2 with projectB named.
        self.project_b = self.tmp / "project_b"
        scripts = self.project_b / ".claude" / "scripts"
        scripts.mkdir(parents=True)
        self.wrapper = scripts / "kg-sync"
        self.wrapper.write_text(KG_SYNC_SH.read_text(encoding="utf-8"),
                                encoding="utf-8")
        self.wrapper.chmod(self.wrapper.stat().st_mode | stat.S_IXUSR)
        (scripts / "sync_knowledge_graph.py").write_text(
            SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )

        # The FOREIGN tree a leaked session env points at (has knowledge/).
        self.foreign = self.tmp / "foreign_session_project"
        (self.foreign / "knowledge").mkdir(parents=True)

    def _run_wrapper(self, extra_env: dict) -> subprocess.CompletedProcess:
        env = _base_env(VCT_INSTALL_ROOT=str(self.install_root), **extra_env)
        return subprocess.run(
            ["bash", str(self.wrapper), "--all"],
            env=env, capture_output=True, text=True, timeout=90,
        )

    def test_fabio_repro_wrapper_tree_wins_over_leaked_kg_base_dir(self) -> None:
        """THE regression pin (fails at pre-fix HEAD): a project-local
        wrapper invoked with a foreign session's ``KG_BASE_DIR`` must target
        the WRAPPER's tree, not the leaked one. Post-fix the wrapper pins
        ``KG_SYNC_PROJECT_ROOT`` (which outranks the legacy env), the sync
        resolves project_b, finds no knowledge/ or docs/, and refuses with
        exit 2 — converting the silent wrong-tree run into a diagnosable
        failure."""
        p = self._run_wrapper({"KG_BASE_DIR": str(self.foreign)})
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn(str(self.project_b), p.stdout)
        self.assertIn("source: KG_SYNC_PROJECT_ROOT", p.stdout)
        self.assertNotIn(f"project root: {self.foreign}", p.stdout,
                         "the leaked KG_BASE_DIR tree must NOT be the target")

    def test_launcher_env_survives_wrapper_location(self) -> None:
        """Leave-alone leg: when ``KG_SYNC_PROJECT_ROOT`` is ALREADY set
        (the launcher's explicit pin, incl. the v0.2.77 orchestrator-copy
        wrapper fallback), the wrapper must NOT overwrite it with its own
        location — set-if-unset."""
        target = self.tmp / "launcher_target"
        target.mkdir()
        p = self._run_wrapper({"KG_SYNC_PROJECT_ROOT": str(target)})
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn(str(target), p.stdout)
        self.assertIn("source: KG_SYNC_PROJECT_ROOT", p.stdout)
        self.assertNotIn(f"project root: {self.project_b}", p.stdout,
                         "wrapper location must not clobber the launcher pin")


class WrapperParityAndSeedPinSourceTests(unittest.TestCase):
    """Source-level pins: wrapper OS-parity + install.py seed-site env pins
    (the established convention for cross-OS / large-file assertions)."""

    def test_bash_wrapper_pins_root_set_if_unset(self) -> None:
        text = KG_SYNC_SH.read_text(encoding="utf-8")
        self.assertIn('KG_SYNC_PROJECT_ROOT:-', text,
                      "bash wrapper must test-before-set (set-if-unset)")
        self.assertIn("export KG_SYNC_PROJECT_ROOT", text)

    def test_ps1_wrapper_pins_root_set_if_unset(self) -> None:
        text = KG_SYNC_PS1.read_text(encoding="utf-8")
        self.assertIn("if (-not $env:KG_SYNC_PROJECT_ROOT)", text,
                      "ps1 wrapper must test-before-set (set-if-unset)")
        self.assertIn("$env:KG_SYNC_PROJECT_ROOT =", text)

    def test_wrappers_never_assign_kg_base_dir(self) -> None:
        """Plan §1.3 B: 'Do NOT touch KG_BASE_DIR' — the legacy channel is
        read-only for the wrappers on BOTH OSes."""
        sh = KG_SYNC_SH.read_text(encoding="utf-8")
        ps1 = KG_SYNC_PS1.read_text(encoding="utf-8")
        self.assertNotIn("KG_BASE_DIR=", sh)
        self.assertNotIn("export KG_BASE_DIR", sh)
        self.assertNotIn("$env:KG_BASE_DIR =", ps1)

    def test_wrapper_parity_both_reference_sibling(self) -> None:
        """Each wrapper's new block must carry the PARITY marker naming its
        sibling, so a future single-sided edit is caught in review."""
        self.assertIn("PARITY: this block must match kg-sync.ps1",
                      KG_SYNC_SH.read_text(encoding="utf-8"))
        self.assertIn("PARITY: this block must match kg-sync",
                      KG_SYNC_PS1.read_text(encoding="utf-8"))

    def test_install_py_seed_sites_pin_root(self) -> None:
        """Plan §1.3 E: BOTH sync_kg subprocess sites (per-project seed +
        shared-KG seed) must pin KG_SYNC_PROJECT_ROOT in their env — a
        foreign KG_BASE_DIR in the invoking shell must never steer the
        seed. (The third grep hit at the helper-call site passes env
        through these two.)"""
        text = INSTALL_PY.read_text(encoding="utf-8")
        pin = 'seed_env["KG_SYNC_PROJECT_ROOT"] = str(PROJECT_ROOT)'
        self.assertGreaterEqual(
            text.count(pin), 2,
            "install.py must pin KG_SYNC_PROJECT_ROOT at BOTH sync_kg "
            "subprocess seed sites (per-project + shared-KG)",
        )
        # Both subprocess sites must pass the locally-built env (not a bare
        # _subprocess_env_with_embedding() inline).
        self.assertNotIn(
            "env=_subprocess_env_with_embedding(),\n            )", text,
            "a sync_kg subprocess still passes the unpinned env inline",
        )

    def test_script_banner_and_validation_markers_present(self) -> None:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("🧭 project root:", text)
        self.assertIn("sys.exit(2)", text)
        self.assertIn("KG_SYNC_PROJECT_ROOT", text)


if __name__ == "__main__":
    unittest.main()

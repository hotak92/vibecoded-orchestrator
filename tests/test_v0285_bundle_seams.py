# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.85 PLAN-v0285 WP-2 — bundle-engine seams.

Pins the two additive seams `install.py`'s root-delegation (WP-1) and the
launcher (WP-3) consume:

  D6  `--skip-kind` / `skip_kinds=` — the THREE-LEG orphan-safe skip:
      (1) excluded from enumeration, (2) excluded from orphan processing,
      (3) prior manifest entries carried forward VERBATIM. Leg 2+3 are the
      ones whose absence makes the orphan loop DELETE the user's installed
      files — PIN-S1 pins that hazard directly (act + leave-alone).
  D2  the ONE-home schema constants (`BUNDLE_ACTION_KEYS`,
      `BUNDLE_RESULT_TOP_KEYS`) + the extracted human renderer
      (`format_bundle_result_lines`).

Design discipline (D11 + v0.2.84 A4): reuse the shared fake-orchestrator
fixture (`tests/_v0284_bundle_fixtures.py`) — do NOT hand-roll a second
orchestrator tree. Main act path runs on a NON-ROOT project (folder ≠
orchestrator_root, the A3 discipline).

These tests must COMPOSE with — never duplicate or modify —
test_v0284_json_stdout_contract.py's structural `print(` guard and the
test_v0284_bundle_adoption*.py suites (those keep owning the adoption incident
pins). This file owns the v0.2.85 seams only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._v0284_bundle_fixtures import bundle_ext, make_fake_orchestrator  # noqa: E402
from vco_lib import project_init  # noqa: E402


# ---------------------------------------------------------------------------
# In-process fixture (fast path for act/leave-alone behaviour) — NON-ROOT.
# ---------------------------------------------------------------------------
class _BundleSeamCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v0285-seams-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        make_fake_orchestrator(self.orch)
        # A3: the main act path is a NON-ROOT project.
        assert self.proj.resolve() != self.orch.resolve()
        self.ext = bundle_ext()

    def tearDown(self) -> None:
        import shutil

        for p in self.tmp.rglob("*"):
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def _install(self, **kw):
        return project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, **kw
        )

    def _hook_rel(self) -> str:
        return str(Path(".claude") / "hooks" / f"foo.{self.ext}")

    def _hook_path(self) -> Path:
        return self.proj / ".claude" / "hooks" / f"foo.{self.ext}"

    def _agent_rel(self) -> str:
        return str(Path(".claude") / "agents" / "coder.md")

    def _script_rel(self) -> str:
        return str(Path(".claude") / "scripts" / "kg-search")

    def _read_manifest(self) -> dict:
        return json.loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
        )


# ---------------------------------------------------------------------------
# PIN-S1 — the orphan hazard (FAIL-WITHOUT-FIX for legs 2+3). Act + leave-alone.
# ---------------------------------------------------------------------------
class PinS1OrphanHazardTests(_BundleSeamCase):
    """v0.2.85 D6: skipping the `hooks` kind must NOT let the orphan loop
    delete the user's installed hook. Without legs 2-3 the prior manifest
    entry (no longer re-shipped, because leg-1 dropped it from enumeration)
    would be seen as an orphan and DELETED (case b) or retired (case c).
    """

    def test_skip_hooks_leaves_file_manifest_and_no_orphan_action(self):
        # First install materializes the hook + records it in the manifest.
        self._install(update_mode=False)
        hook_rel = self._hook_rel()
        prior_entry = self._read_manifest()["files"][hook_rel]
        self.assertIsNotNone(prior_entry, "premise: hook tracked in manifest")

        # User drifts the on-disk hook (so a normal run would ADOPT it —
        # proving the skip really prevents ANY classification, not just noop).
        self._hook_path().write_text("DRIFTED USER EDIT\n", encoding="utf-8")

        result = self._install(update_mode=True, skip_kinds=frozenset({"hooks"}))

        # LEG 1: file untouched on disk (never enumerated → never written).
        self.assertEqual(
            self._hook_path().read_text(encoding="utf-8"), "DRIFTED USER EDIT\n"
        )
        # LEG 1: no op-action of ANY kind references the skipped hook.
        for action, paths in result["actions"].items():
            self.assertNotIn(
                hook_rel, paths, f"skipped hook leaked into action '{action}'"
            )
        # LEG 2: specifically NOT orphan-processed.
        self.assertNotIn(hook_rel, result["actions"]["orphan-deleted"])
        self.assertNotIn(hook_rel, result["actions"]["orphan-preserved"])
        self.assertNotIn(hook_rel, result["actions"]["orphan-retired"])
        # LEG 3: manifest entry carried forward BYTE-IDENTICAL.
        carried = self._read_manifest()["files"].get(hook_rel)
        self.assertEqual(
            carried, prior_entry, "skipped-kind manifest entry not carried forward"
        )
        # Envelope surfaces the skip additively.
        self.assertEqual(result.get("skip_kinds"), ["hooks"])

    def test_skip_hooks_also_covers_lib_hooks(self):
        """`.claude/hooks/_lib/...` is part of the `hooks` kind: its prior
        manifest entries must ALSO be carried forward, not orphan-processed."""
        self._install(update_mode=False)
        lib_rel = str(Path(".claude") / "hooks" / "_lib" / f"find-python.{self.ext}")
        prior_entry = self._read_manifest()["files"][lib_rel]

        lib_path = self.proj / lib_rel
        self.assertTrue(lib_path.exists(), "premise: _lib hook installed")

        result = self._install(update_mode=True, skip_kinds=frozenset({"hooks"}))

        self.assertNotIn(lib_rel, result["actions"]["orphan-deleted"])
        self.assertNotIn(lib_rel, result["actions"]["orphan-retired"])
        self.assertEqual(self._read_manifest()["files"].get(lib_rel), prior_entry)
        # The _lib hook is STILL ON DISK — leg 2 (orphan exclusion) protected
        # it from the delete branch, not just the manifest bookkeeping.
        self.assertTrue(
            lib_path.exists(),
            "skip-hooks must not delete the _lib hook from disk (orphan hazard)",
        )

    def test_without_skip_normal_classification_resumes(self):
        """After a skip run, a NORMAL (no skip_kinds) run reclassifies the
        drifted hook — proving the skip is per-run, not sticky."""
        self._install(update_mode=False)
        hook_rel = self._hook_rel()
        self._hook_path().write_text("DRIFTED USER EDIT\n", encoding="utf-8")

        # skip run — leaves the drift in place.
        self._install(update_mode=True, skip_kinds=frozenset({"hooks"}))

        # normal run — the drifted hook is now adopted (backup + shipped bytes).
        result = self._install(update_mode=True)
        self.assertIn(hook_rel, result["actions"]["adopt"])
        self.assertTrue(result.get("adopt_backup_dir"))
        # Shipped bytes are back on disk.
        self.assertEqual(
            self._hook_path().read_text(encoding="utf-8"), "#!/bin/sh\necho v1\n"
        )
        # And the drift is preserved in the adoption backup.
        backup_root = self.proj / ".claude" / "backups" / "bundle-adoptions"
        backups = [
            p for p in backup_root.rglob("*") if p.name == f"foo.{self.ext}"
        ]
        self.assertEqual(len(backups), 1)
        self.assertEqual(
            backups[0].read_text(encoding="utf-8"), "DRIFTED USER EDIT\n"
        )

    def test_skip_holds_then_full_run_clears_deferral_end_to_end(self):
        """v0.2.85 NIT-3 (M-1 sequence closure): one test covering BOTH
        directions in sequence — a seeded `bundle_user_modified_preserved` entry
        for a drifted hook is HELD across a `--skip-kind hooks` run (M-1), then
        CLEARED by the subsequent FULL run (which adopts the hook → the file is
        no longer preserved → the reconciler honestly drops the entry)."""
        from vco_lib.deferral_report import DeferralEntry, DeferralReport

        self._install(update_mode=False)
        hook_rel = self._hook_rel()
        self._hook_path().write_text("DRIFTED USER EDIT\n", encoding="utf-8")
        seed = DeferralReport.read(self.proj)
        seed.add_entry(DeferralEntry(
            condition_id="bundle_user_modified_preserved",
            title="1 user-modified file preserved", detected=hook_rel,
            why_deferred="user edited a shipped hook",
            command_to_apply="--update --force", severity="info",
        ))
        seed.write(self.proj)

        # skip run — HELD (M-1: partial view must not clear it).
        self._install(update_mode=True, skip_kinds=frozenset({"hooks"}))
        self.assertTrue(
            DeferralReport.read(self.proj).has_condition(
                "bundle_user_modified_preserved"),
            "skip run must HOLD the deferral (M-1)",
        )

        # full run — the hook is adopted (backup + shipped bytes), so it is no
        # longer preserved → the reconciler honestly CLEARS the entry.
        result = self._install(update_mode=True)
        self.assertIn(hook_rel, result["actions"]["adopt"])
        self.assertFalse(
            DeferralReport.read(self.proj).has_condition(
                "bundle_user_modified_preserved"),
            "full run must CLEAR the deferral once the file is resolved",
        )

    def test_skip_hooks_does_not_falsely_autoresolve_preserved_deferral(self):
        """v0.2.85 M-1 (skip-kind reconciler honesty): a pre-existing
        `bundle_user_modified_preserved` deferral for a STILL-diverged hook must
        SURVIVE a `--skip-kind hooks` run — the reconciler must not read the
        (incomplete, skip-limited) `user_modified_paths` and falsely drop the
        entry with a "condition no longer applies" audit row for a file it never
        inspected. (This is the dishonest-auto-resolution class v0.2.83 killed;
        the skip-kind view is partial, so the reconciler holds the condition.)
        """
        from vco_lib.deferral_report import DeferralEntry, DeferralReport

        self._install(update_mode=False)
        hook_rel = self._hook_rel()
        # Drift the hook so it is genuinely still user-modified on disk.
        self._hook_path().write_text("DRIFTED USER EDIT\n", encoding="utf-8")

        # Seed a real preserved-deferral entry for this hook (as a prior update
        # that DID classify it would have).
        seed = DeferralReport.read(self.proj)
        seed.add_entry(DeferralEntry(
            condition_id="bundle_user_modified_preserved",
            title="1 user-modified file preserved",
            detected=hook_rel,
            why_deferred="user edited a shipped hook",
            command_to_apply="--update --force",
            severity="info",
        ))
        seed.write(self.proj)
        self.assertTrue(
            DeferralReport.read(self.proj).has_condition(
                "bundle_user_modified_preserved"),
            "premise: deferral seeded",
        )

        # Skip-hooks update: hooks are never classified, so user_modified_paths
        # is empty — but the entry must NOT be auto-resolved.
        self._install(update_mode=True, skip_kinds=frozenset({"hooks"}))

        self.assertTrue(
            DeferralReport.read(self.proj).has_condition(
                "bundle_user_modified_preserved"),
            "M-1: skip-hooks run falsely auto-resolved a still-applicable "
            "preserved deferral for an uninspected hook",
        )
        # And the drift is still on disk (leg 1 — untouched).
        self.assertEqual(
            self._hook_path().read_text(encoding="utf-8"), "DRIFTED USER EDIT\n"
        )

    def test_skip_hooks_windows_separator_manifest_key(self):
        """A `\\`-separator manifest key (Windows-shaped) is carried forward
        and NOT orphan-processed — `_bundle_op_kind` normalizes the separator
        before the prefix test, so a Windows non-root project's skip-hooks run
        is data-safe too."""
        self._install(update_mode=False)
        # Rewrite the manifest with a Windows-shaped hook key (the launcher on
        # Windows persists `.claude\hooks\foo.ps1`). The on-disk POSIX file
        # stays; only the manifest KEY carries backslashes.
        manifest = self._read_manifest()
        posix_key = self._hook_rel()
        win_key = posix_key.replace("/", "\\")
        entry = manifest["files"].pop(posix_key)
        manifest["files"][win_key] = entry
        project_init._write_manifest_atomic(self.proj, manifest)

        result = self._install(update_mode=True, skip_kinds=frozenset({"hooks"}))

        # Not orphan-processed under EITHER key shape.
        for bucket in ("orphan-deleted", "orphan-preserved", "orphan-retired"):
            self.assertNotIn(win_key, result["actions"][bucket])
            self.assertNotIn(posix_key, result["actions"][bucket])
        # Carried forward verbatim (still under the `\`-key it came in with).
        self.assertEqual(self._read_manifest()["files"].get(win_key), entry)


# ---------------------------------------------------------------------------
# PIN-S2 — default byte-parity (leave-alone). Envelope of a default run equals
# the envelope of an explicit `skip_kinds=frozenset()` run, deep-equal minus
# the additive `skip_kinds` key (which is absent in BOTH — proving the default
# path is unchanged).
# ---------------------------------------------------------------------------
class PinS2DefaultByteParityTests(_BundleSeamCase):
    @staticmethod
    def _normalize(obj, project_folder: str):
        """Recursively replace the per-project absolute folder with a stable
        placeholder so two runs of DIFFERENT projects can be deep-compared.

        The envelope legitimately embeds each project's own folder in several
        sub-blocks (`folder`, the env-backfill `path`s, the `templates` result,
        `adopt_backup_dir`'s timestamp). PIN-S2 asserts that turning
        `skip_kinds` from absent → `frozenset()` changes NOTHING about the
        classification envelope; the only difference between two projects is the
        folder string, which we normalize away here. (adopt_backup_dir carries a
        UTC timestamp — irrelevant on these fresh/noop runs where nothing is
        adopted, but dropped for robustness.)"""
        if isinstance(obj, dict):
            return {
                k: PinS2DefaultByteParityTests._normalize(v, project_folder)
                for k, v in obj.items()
                if k != "adopt_backup_dir"
            }
        if isinstance(obj, list):
            return [
                PinS2DefaultByteParityTests._normalize(v, project_folder)
                for v in obj
            ]
        if isinstance(obj, str):
            return obj.replace(project_folder, "<PROJECT>")
        return obj

    def test_default_run_equals_empty_skip_kinds_run(self):
        # Two independent NON-ROOT projects from the same orchestrator.
        proj_a = self.tmp / "proj_a"
        proj_b = self.tmp / "proj_b"
        proj_a.mkdir()
        proj_b.mkdir()

        r_default = project_init.install_project_bundle(
            proj_a, orchestrator_root=self.orch, update_mode=False,
        )
        r_empty = project_init.install_project_bundle(
            proj_b, orchestrator_root=self.orch, update_mode=False,
            skip_kinds=frozenset(),
        )

        # Neither carries the additive key (empty skip is byte-identical to no
        # skip) — the core of PIN-S2.
        self.assertNotIn("skip_kinds", r_default)
        self.assertNotIn("skip_kinds", r_empty)

        # Full-envelope deep equality, per-project folder normalized away.
        self.maxDiff = None
        a = self._normalize(r_default, str(proj_a.resolve()))
        b = self._normalize(r_empty, str(proj_b.resolve()))
        self.assertEqual(
            a, b, "empty skip_kinds must be byte-identical to the default path"
        )

    def test_empty_skip_kinds_update_envelope_matches_default_update(self):
        """Same equivalence on the UPDATE path (noop-heavy second run)."""
        for proj in ("proj_c", "proj_d"):
            p = self.tmp / proj
            p.mkdir()
            project_init.install_project_bundle(
                p, orchestrator_root=self.orch, update_mode=False,
            )

        proj_c = self.tmp / "proj_c"
        proj_d = self.tmp / "proj_d"
        r_default = project_init.install_project_bundle(
            proj_c, orchestrator_root=self.orch, update_mode=True,
        )
        r_empty = project_init.install_project_bundle(
            proj_d, orchestrator_root=self.orch, update_mode=True,
            skip_kinds=frozenset(),
        )
        self.assertNotIn("skip_kinds", r_default)
        self.assertNotIn("skip_kinds", r_empty)
        self.maxDiff = None
        a = self._normalize(r_default, str(proj_c.resolve()))
        b = self._normalize(r_empty, str(proj_d.resolve()))
        self.assertEqual(a, b)


# ---------------------------------------------------------------------------
# skip agents / skills / settings — act tests.
# ---------------------------------------------------------------------------
class SkipKindActTests(_BundleSeamCase):
    def test_skip_agents_no_agent_ops(self):
        result = self._install(update_mode=False, skip_kinds=frozenset({"agents"}))
        agent_rel = self._agent_rel()
        for action, paths in result["actions"].items():
            self.assertNotIn(agent_rel, paths)
        self.assertFalse((self.proj / ".claude" / "agents" / "coder.md").exists())
        self.assertEqual(result.get("skip_kinds"), ["agents"])
        # Other kinds still installed.
        self.assertIn(self._hook_rel(), result["actions"]["create"])

    def test_skip_skills_no_skill_ops(self):
        # Ship a skill so the kind is populated.
        skill_dir = self.orch / "templates" / "skills" / "tdd"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# TDD\n", encoding="utf-8")
        result = self._install(update_mode=False, skip_kinds=frozenset({"skills"}))
        skill_rel = str(Path(".claude") / "skills" / "tdd" / "SKILL.md")
        for action, paths in result["actions"].items():
            self.assertNotIn(skill_rel, paths)
        self.assertFalse((self.proj / ".claude" / "skills" / "tdd").exists())

    def test_skip_settings_leaves_settings_action_empty(self):
        result = self._install(update_mode=False, skip_kinds=frozenset({"settings"}))
        # `settings` is NOT a file-kind — it names the merge step.
        self.assertEqual(result["settings_action"], "")
        self.assertFalse((self.proj / ".claude" / "settings.json").exists())
        self.assertEqual(result.get("skip_kinds"), ["settings"])
        # File kinds still install.
        self.assertIn(self._hook_rel(), result["actions"]["create"])

    def test_multiple_skip_kinds_combine(self):
        result = self._install(
            update_mode=False,
            skip_kinds=frozenset({"hooks", "settings"}),
        )
        for action, paths in result["actions"].items():
            self.assertNotIn(self._hook_rel(), paths)
        self.assertEqual(result["settings_action"], "")
        # Agents/scripts still installed.
        self.assertIn(self._agent_rel(), result["actions"]["create"])
        self.assertIn(self._script_rel(), result["actions"]["create"])
        self.assertEqual(result.get("skip_kinds"), ["hooks", "settings"])

    def test_unknown_skip_kind_ignored_by_function(self):
        """A direct Python caller passing an out-of-vocab kind gets it dropped
        (the CLI's `choices` gates the surface; the function is defensive)."""
        result = self._install(
            update_mode=False, skip_kinds=frozenset({"bogus", "hooks"}),
        )
        # Only the valid kind takes effect + is surfaced.
        self.assertEqual(result.get("skip_kinds"), ["hooks"])
        self.assertIn(self._agent_rel(), result["actions"]["create"])


# ---------------------------------------------------------------------------
# Structural — the schema constants are the ONE home; both construction sites
# derive from them; the documented schema matches.
# ---------------------------------------------------------------------------
class SchemaConstantStructuralTests(unittest.TestCase):
    def test_action_keys_match_documented_schema(self):
        # The documented action buckets (v0.2.84 + v0.2.81 + v0.2.83 additions).
        expected = {
            "create", "overwrite", "always-overwrite", "noop", "preserve",
            "adopt", "skip-existing", "skip-disabled", "keep-regenerated",
            "orphan-deleted", "orphan-preserved", "orphan-retired",
            "knowledge-retired",
        }
        self.assertEqual(set(project_init.BUNDLE_ACTION_KEYS), expected)
        # Ordered tuple (byte-parity requirement) with no duplicates.
        self.assertEqual(
            len(project_init.BUNDLE_ACTION_KEYS),
            len(set(project_init.BUNDLE_ACTION_KEYS)),
        )

    def test_top_keys_are_the_always_present_envelope_keys(self):
        self.assertEqual(
            project_init.BUNDLE_RESULT_TOP_KEYS,
            frozenset({
                "folder", "orchestrator_root", "update_mode", "force",
                "dry_run", "actions", "settings_action", "manifest_written",
                "vco_version", "warnings", "errors",
            }),
        )

    def test_skip_kinds_vocabulary(self):
        self.assertEqual(
            project_init.BUNDLE_SKIP_KINDS,
            frozenset({"agents", "skills", "hooks", "scripts", "settings"}),
        )

    def test_both_result_dicts_derive_action_keys_from_constant(self):
        """Both `install_project_bundle` result-dict construction sites must
        derive their `actions` key set from `BUNDLE_ACTION_KEYS` — no drifting
        literal tuple. Structural guard over the source: the ONLY
        `{k: [] for k in ...}` action-dict comprehensions in the function body
        iterate `BUNDLE_ACTION_KEYS`, and there is no hand-written action tuple
        literal left."""
        src = (REPO_ROOT / "vco_lib" / "project_init.py").read_text(encoding="utf-8")
        start = src.index("def install_project_bundle(")
        end = src.index("\ndef ", start + 10)
        body = src[start:end]

        # Every `for k in <X>` inside an actions comprehension uses the constant.
        import re

        comps = re.findall(r"\{k: \[\] for k in ([^}]+)\}", body)
        self.assertEqual(
            len(comps), 2,
            f"expected exactly 2 action-dict comprehensions, found {len(comps)}: {comps}",
        )
        for expr in comps:
            self.assertIn(
                "BUNDLE_ACTION_KEYS", expr,
                f"action-dict comprehension does not derive from the constant: {expr!r}",
            )

        # And the pre-v0.2.85 literal tuple ("create", "overwrite", ...,
        # "knowledge-retired") is GONE from the function body.
        self.assertNotIn(
            '"orphan-retired", "knowledge-retired")', body,
            "a hand-written action-key literal tuple survived in the function body",
        )

    def test_early_return_and_main_return_share_the_key_set(self):
        """Behavioural corollary: the missing-folder early-return's `actions`
        dict has the SAME keys as a successful run's `actions` dict — both come
        from the ONE constant."""
        missing = project_init.install_project_bundle(
            Path(tempfile.gettempdir()) / "vct-v0285-does-not-exist-xyz",
            orchestrator_root=None,
        )
        self.assertEqual(
            set(missing["actions"].keys()),
            set(project_init.BUNDLE_ACTION_KEYS),
        )


# ---------------------------------------------------------------------------
# PIN-S3 — renderer extraction golden. The extracted
# `format_bundle_result_lines` + `print("\n".join(...))` must be byte-identical
# to the historical sequential-`print()` output. We reproduce the historical
# print sequence inline as the independent reference (the pre-extraction code),
# capturing its bytes, and assert the live CLI stdout matches it exactly.
# ---------------------------------------------------------------------------
class PinS3RendererGoldenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v0285-golden-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        make_fake_orchestrator(self.orch)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(str(self.tmp), ignore_errors=True)

    @staticmethod
    def _historical_render(result: dict) -> str:
        """The EXACT pre-v0.2.85 renderer, reproduced verbatim (sequential
        `print()` calls captured into one string). This is the golden
        expectation `format_bundle_result_lines` is pinned against. Copied from
        `_cmd_install_bundle`'s non-JSON branch at base SHA a4a07fa3 — see
        PLAN-v0285 D2 / PIN-S3. If the shared renderer ever drifts from this,
        the launcher and install.py human output silently diverge.
        """
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print(f"folder: {result['folder']}")
            print(f"orchestrator_root: {result['orchestrator_root']}")
            print(f"update_mode: {result['update_mode']}  dry_run: {result['dry_run']}")
            for category, paths in result["actions"].items():
                if not paths:
                    continue
                print(f"  {category} ({len(paths)}):")
                for p in paths[:8]:
                    print(f"    {p}")
                if len(paths) > 8:
                    print(f"    ... +{len(paths) - 8} more")
            if result["settings_action"]:
                print(f"  settings.json: {result['settings_action']}")
            if result["manifest_written"]:
                # noqa: F541 kept — VERBATIM copy of the historical renderer
                # (see docstring). The `f` prefix must match the pre-v0.2.85
                # source byte-for-byte so PIN-S3 pins the real historical bytes.
                print(f"  manifest written: .claude/.vco-manifest.json")  # noqa: F541
            for w in result["warnings"]:
                print(f"  WARNING {w}")
            for err in result["errors"]:
                print(f"  ERROR {err.get('path', '?')}: {err['error']}")
        return buf.getvalue()

    def _run_cli_nonjson(self, *extra: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.pop("VCT_DISABLE_HOOKS", None)
        return subprocess.run(
            [
                sys.executable, "-m", "vco_lib.project_init", "install-bundle",
                "--folder", str(self.proj),
                "--orchestrator-root", str(self.orch),
                *extra,
            ],
            capture_output=True, text=True, env=env, timeout=600,
        )

    def test_cli_nonjson_output_matches_historical_renderer(self):
        """Drive the REAL CLI (no --json). Its stdout must equal the historical
        sequential-print output byte-for-byte, computed against the SAME result
        envelope (which we recover from a parallel --json run so the reference
        renderer sees identical inputs)."""
        # First, the fresh-install run under --json to recover the exact
        # envelope the human run rendered.
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.pop("VCT_DISABLE_HOOKS", None)
        json_proc = subprocess.run(
            [
                sys.executable, "-m", "vco_lib.project_init", "install-bundle",
                "--folder", str(self.proj),
                "--orchestrator-root", str(self.orch),
                "--json",
            ],
            capture_output=True, text=True, env=env, timeout=600,
        )
        self.assertEqual(json_proc.returncode, 0, json_proc.stderr[-500:])
        envelope = json.loads(json_proc.stdout)

        # Fresh project for the non-JSON run (identical fixture → identical
        # classification, minus the absolute folder path which the renderer
        # echoes verbatim from the envelope).
        proj2 = self.tmp / "project2"
        proj2.mkdir()
        env2 = dict(os.environ)
        env2["PYTHONPATH"] = str(REPO_ROOT)
        env2.pop("VCT_DISABLE_HOOKS", None)
        human_proc = subprocess.run(
            [
                sys.executable, "-m", "vco_lib.project_init", "install-bundle",
                "--folder", str(proj2),
                "--orchestrator-root", str(self.orch),
            ],
            capture_output=True, text=True, env=env2, timeout=600,
        )
        self.assertEqual(human_proc.returncode, 0, human_proc.stderr[-500:])

        # Rebuild the golden with proj2's folder so paths line up (the envelope
        # differs only in the `folder` value).
        envelope2 = dict(envelope)
        envelope2["folder"] = str(proj2.resolve())
        expected_human2 = self._historical_render(envelope2)

        self.assertEqual(
            human_proc.stdout, expected_human2,
            "extracted format_bundle_result_lines drifted from the historical "
            "renderer (PLAN-v0285 D2 / PIN-S3)",
        )

    def test_format_bundle_result_lines_is_pure_and_matches_reference(self):
        """Unit-level golden: `format_bundle_result_lines` joined with newlines
        (+ trailing newline, as `print` adds) equals the historical render."""
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        lines = project_init.format_bundle_result_lines(result)
        rebuilt = "\n".join(lines) + "\n"
        self.assertEqual(rebuilt, self._historical_render(result))


# ---------------------------------------------------------------------------
# CLI parse — `--skip-kind` is repeatable and choice-enforced.
# ---------------------------------------------------------------------------
class SkipKindCliParseTests(unittest.TestCase):
    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.pop("VCT_DISABLE_HOOKS", None)
        return subprocess.run(
            [sys.executable, "-m", "vco_lib.project_init", "install-bundle", *extra],
            capture_output=True, text=True, env=env, timeout=120,
        )

    def test_repeatable_skip_kind_reaches_envelope(self):
        tmp = Path(tempfile.mkdtemp(prefix="vct-v0285-cli-"))
        orch = tmp / "orch"
        proj = tmp / "proj"
        orch.mkdir()
        proj.mkdir()
        make_fake_orchestrator(orch)
        try:
            proc = self._run(
                "--folder", str(proj),
                "--orchestrator-root", str(orch),
                "--skip-kind", "hooks",
                "--skip-kind", "settings",
                "--json",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
            result = json.loads(proc.stdout)
            self.assertEqual(result.get("skip_kinds"), ["hooks", "settings"])
            self.assertEqual(result["settings_action"], "")
        finally:
            import shutil

            shutil.rmtree(str(tmp), ignore_errors=True)

    def test_invalid_skip_kind_rejected(self):
        """`choices` enforcement: an unknown kind is rejected by argparse (exit
        2, error on stderr) — the surface never reaches the function."""
        proc = self._run(
            "--folder", "/nonexistent",
            "--skip-kind", "bogus",
            "--json",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--skip-kind", proc.stderr)
        self.assertIn("bogus", proc.stderr)

    def test_no_skip_kind_absent_key(self):
        tmp = Path(tempfile.mkdtemp(prefix="vct-v0285-cli2-"))
        orch = tmp / "orch"
        proj = tmp / "proj"
        orch.mkdir()
        proj.mkdir()
        make_fake_orchestrator(orch)
        try:
            proc = self._run(
                "--folder", str(proj),
                "--orchestrator-root", str(orch),
                "--json",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
            result = json.loads(proc.stdout)
            self.assertNotIn("skip_kinds", result)
        finally:
            import shutil

            shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

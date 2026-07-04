# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""W2-D (v0.2.73) — SessionStart + nudge + kg-sync hook behaviour.

Drives the four W2-D hook scripts as real subprocesses against synthetic
fixtures (no real project, no secrets):

  * IN-3 — session-start-deferral-surface.sh: surfaces UPDATE_DEFERRED
    entries JSON-first, Markdown-fallback, one line per entry, silent when
    both are absent, silent under VCT_DISABLE_HOOKS.
  * KG-3 — session-start-retrieval-health.sh: one-line retrieval-health
    status; graceful "unavailable" line when Weaviate is unreachable.
  * HK-3 — kg-sync-on-edit.sh: runs standalone, no-ops under
    VCT_DISABLE_HOOKS, no-ops on a non-knowledge path.
  * HK-5 — the kg-update-nudge PostToolUse(*) registration now carries the
    settings-level guard (asserted against the template).

All fixtures are synthetic — no project-identifying strings, no secrets.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"

DEFERRAL_HOOK = HOOKS / "session-start-deferral-surface.sh"
HEALTH_HOOK = HOOKS / "session-start-retrieval-health.sh"
KG_SYNC_HOOK = HOOKS / "kg-sync-on-edit.sh"


def _run(script: Path, *, env_extra=None, stdin="", project_dir=None):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": tempfile.gettempdir(),
    }
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(script)],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _write_json_sidecar(project: Path, entries):
    ctx = project / ".claude" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_at": "2026-07-04T00:00:00Z",
        "severity_max": "warning",
        "entries": entries,
    }
    (ctx / "UPDATE_DEFERRED.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _entry(cid, title, sev):
    return {
        "condition_id": cid,
        "title": title,
        "detected": "d",
        "why_deferred": "w",
        "command_to_apply": "cmd " + cid,
        "severity": sev,
        "kg_node_refs": [],
        "detected_at": "2026-07-04T00:00:00Z",
    }


class DeferralSurfaceTests(unittest.TestCase):
    """IN-3 — session-start-deferral-surface.sh."""

    def test_json_present_emits_one_line_per_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            _write_json_sidecar(
                project,
                [
                    _entry("schema_migration_required", "Rebuild", "warning"),
                    _entry("bundle_user_modified_preserved", "Preserved", "info"),
                ],
            )
            r = _run(DEFERRAL_HOOK, project_dir=project)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = r.stdout
            self.assertIn("2 deferred", out)
            self.assertIn("schema_migration_required", out)
            self.assertIn("bundle_user_modified_preserved", out)
            self.assertIn("[warning]", out)
            self.assertIn("[info]", out)
            # One summary line per entry (not the full bodies).
            self.assertNotIn("why_deferred", out)

    def test_markdown_fallback_when_json_absent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            ctx = project / ".claude" / "context"
            ctx.mkdir(parents=True)
            (ctx / "UPDATE_DEFERRED.md").write_text(
                "---\ncondition_ids: [foo_bar]\n---\n\n"
                "## foo_bar (critical)\n\n**Title**: Foo Bar\n",
                encoding="utf-8",
            )
            r = _run(DEFERRAL_HOOK, project_dir=project)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("foo_bar", r.stdout)
            self.assertIn("[critical]", r.stdout)
            # The frontmatter condition_ids line must NOT be parsed as an
            # entry (only the ## header is a real entry).
            self.assertIn("1 deferred", r.stdout)

    def test_both_absent_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / ".claude" / "context").mkdir(parents=True)
            r = _run(DEFERRAL_HOOK, project_dir=project)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "")

    def test_disabled_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            _write_json_sidecar(project, [_entry("x", "X", "warning")])
            r = _run(
                DEFERRAL_HOOK,
                project_dir=project,
                env_extra={"VCT_DISABLE_HOOKS": "1"},
            )
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")

    def test_corrupt_json_is_silent_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            ctx = project / ".claude" / "context"
            ctx.mkdir(parents=True)
            (ctx / "UPDATE_DEFERRED.json").write_text(
                "{not valid json", encoding="utf-8"
            )
            r = _run(DEFERRAL_HOOK, project_dir=project)
            self.assertEqual(r.returncode, 0, r.stderr)
            # No JSON, no MD → silent.
            self.assertEqual(r.stdout.strip(), "")


class RetrievalHealthTests(unittest.TestCase):
    """KG-3 — session-start-retrieval-health.sh."""

    def test_unreachable_weaviate_reports_unavailable(self) -> None:
        # Point at an almost-certainly-closed port so both probes fail at
        # the transport level → the graceful "unavailable" line.
        r = _run(
            HEALTH_HOOK,
            env_extra={
                "WEAVIATE_URL": "http://127.0.0.1:1",
                "KG_COLLECTION": "SyntheticKG",
            },
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Retrieval:", r.stdout)
        self.assertIn("unavailable", r.stdout)

    def test_disabled_is_silent(self) -> None:
        r = _run(
            HEALTH_HOOK,
            env_extra={
                "VCT_DISABLE_HOOKS": "1",
                "WEAVIATE_URL": "http://127.0.0.1:1",
            },
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_never_raises_regardless(self) -> None:
        # A malformed WEAVIATE_URL must not crash — soft-fail to a line or
        # silence, always exit 0.
        r = _run(HEALTH_HOOK, env_extra={"WEAVIATE_URL": "not-a-url"})
        self.assertEqual(r.returncode, 0, r.stderr)


class KgSyncOnEditTests(unittest.TestCase):
    """HK-3 — kg-sync-on-edit.sh runs standalone + guards."""

    def test_disabled_is_noop(self) -> None:
        payload = json.dumps(
            {"tool_input": {"file_path": "knowledge/concepts/x.md"}}
        )
        r = _run(
            KG_SYNC_HOOK,
            stdin=payload,
            env_extra={"VCT_DISABLE_HOOKS": "1"},
        )
        self.assertEqual(r.returncode, 0)
        # No kg-sync wrapper present in the synthetic env → no diagnostic.
        self.assertEqual(r.stdout.strip(), "")

    def test_non_knowledge_path_is_noop(self) -> None:
        payload = json.dumps({"tool_input": {"file_path": "src/main.py"}})
        with tempfile.TemporaryDirectory() as td:
            r = _run(KG_SYNC_HOOK, stdin=payload, project_dir=Path(td))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "")
            self.assertEqual(r.stderr.strip(), "")

    def test_empty_payload_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r = _run(KG_SYNC_HOOK, stdin="", project_dir=Path(td))
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")

    def test_knowledge_path_without_wrapper_is_clean(self) -> None:
        # Knowledge path but no .claude/scripts/kg-sync wrapper in the
        # synthetic project → clean no-op (the wrapper existence gates the
        # sync; absence is not an error).
        payload = json.dumps(
            {"tool_input": {"file_path": "knowledge/concepts/x.md"}}
        )
        with tempfile.TemporaryDirectory() as td:
            r = _run(KG_SYNC_HOOK, stdin=payload, project_dir=Path(td))
            self.assertEqual(r.returncode, 0, r.stderr)


class NudgeGuardRegistrationTests(unittest.TestCase):
    """HK-5 — the PostToolUse(*) nudge registration carries the guard."""

    def test_posttooluse_star_nudge_is_guarded(self) -> None:
        linux = REPO_ROOT / "templates" / "settings.json.linux.template"
        data = json.loads(linux.read_text(encoding="utf-8"))
        found = False
        for group in data["hooks"].get("PostToolUse", []):
            if group.get("matcher") == "*":
                for hook in group.get("hooks", []):
                    cmd = hook.get("command", "")
                    if "kg-update-nudge.sh" in cmd:
                        found = True
                        self.assertIn(
                            'VCT_DISABLE_HOOKS',
                            cmd,
                            "HK-5: PostToolUse(*) kg-update-nudge "
                            "registration must carry the settings-level "
                            "VCT_DISABLE_HOOKS guard (outer net for the "
                            "internal guard).",
                        )
        self.assertTrue(found, "PostToolUse(*) nudge registration not found")


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 BLOCKER-2 — ``--force-recreate`` must not drop a family it does not own.

THE DEFECT
----------

The chunker-revision deferral this release writes into EVERY pre-existing
project told the user to run, in a plain terminal::

    cd <folder>
    .claude/scripts/code-graph-analyze . --force-recreate

Chain (each link verified in source):

* ``templates/scripts/code-graph-analyze`` is a venv ladder that ends in
  ``"$VENV_PY" "$SCRIPT_DIR/analyze_code_graph.py" "$@"`` — a pure
  pass-through, so the analyzer sees exactly the flags above;
* ``analyze_code_graph.main`` resolves the collection prefix
  ``--from-resolver > --project > $CODE_GRAPH_PROJECT > repo_path.name``, and
  ``--from-resolver`` is OPT-IN. A terminal has not sourced ``.claude/env``
  (it is shell-sourced for hooks via ``BASH_ENV``), so the run lands on the
  FOLDER BASENAME rung, printing a warning and PROCEEDING;
* ``CodeGraphAnalyzer.create_collections(force=True)`` then
  ``collections.delete()``s ``<basename>_CodeModule`` and its four siblings.

So a moved/renamed project rebuilt the wrong family while its real one kept
the stale vectors, and a basename that happened to be ANOTHER registered
project's bound prefix dropped that project's five populated classes.

WHAT IS PINNED HERE
-------------------

Both arms of the destructive decision, always: the REFUSAL **and** the
legitimate rebuild that must still work. A guard that refuses everything is
not a fix, so the allow-arms are as load-bearing as the refuse-arms:

* registered folder + resolved prefix ≠ bound prefix → refuse, nothing deleted
* resolved prefix IS another registered project's bound prefix → refuse
* launcher.db present but UNREADABLE → refuse (cannot confirm ≠ permission)
* **no launcher.db at all → PROCEED** (free-tier / standalone; positively
  observed absence, not an unreadable registry)
* registered folder + its own bound prefix → PROCEED, all five dropped
* unregistered folder, family bound to nobody → PROCEED
* ``force=False`` → the guard never runs, nothing is ever deleted

Driven through the production entry points: ``analyze_code_graph.main()`` with
a real argv (the exact shape the remedy produces) for the CLI arms, and
``create_collections(force=True, repo_path=…)`` — the method that owns the
delete — for the rest. No live Weaviate: the client is a fake that records
every ``delete``. No ambient launcher.db: every test pins one via
``VCT_LAUNCHER_DB_PATH``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    add_project,
    create_corrupt_launcher_db,
    create_empty_launcher_db,
)
from vco_lib import codegraph_drop_guard as guard  # noqa: E402

_ANALYZER_PATH = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_b2_analyze_code_graph", str(_ANALYZER_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_MOD = _load_analyzer()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCollections:
    """Records every delete; every class 'exists' so `force` always drops."""

    def __init__(self, existing: set[str]) -> None:
        self.existing = set(existing)
        self.deleted: list[str] = []
        self.created: list[str] = []

    def exists(self, name: str) -> bool:
        return name in self.existing

    def delete(self, name: str) -> None:
        self.deleted.append(name)
        self.existing.discard(name)

    def create(self, **kwargs):
        name = kwargs.get("name")
        self.created.append(name)
        self.existing.add(name)

        class _C:
            pass

        return _C()

    def get(self, name: str):
        class _C:
            pass

        return _C()


class _FakeClient:
    def __init__(self, existing: set[str]) -> None:
        self.collections = _FakeCollections(existing)

    def close(self) -> None:
        pass


class _StopAfterCollections(SystemExit):
    """Sentinel: main() got past create_collections without a refusal.

    A ``SystemExit`` subclass on purpose — ``main()``'s ``except Exception``
    arms would swallow a plain exception and turn a PROVEN allow into a
    generic exit 1, which is exactly the false-green a mutation test would
    then not catch.
    """


def _five(prefix: str) -> set[str]:
    return {
        f"{prefix}_CodeModule",
        f"{prefix}_CodeClass",
        f"{prefix}_CodeFunction",
        f"{prefix}_CodeAPI",
        f"{prefix}_CodeInteraction",
    }


class _GuardTestBase(unittest.TestCase):
    """Temp project folders + a pinned launcher.db per test."""

    def setUp(self) -> None:
        self._td = TemporaryDirectory()
        self.root = Path(self._td.name).resolve()
        self.db = self.root / "launcher.db"
        # A folder whose BASENAME sanitizes to `Widget` — while the project
        # that owns it is registered as `ACME widget` bound to `ACME_widget`.
        self.folder = self.root / "widget"
        self.folder.mkdir()
        (self.folder / "mod.py").write_text("def f():\n    return 1\n")
        self._env = mock.patch.dict(
            os.environ,
            {
                "VCT_LAUNCHER_DB_PATH": str(self.db),
                "WEAVIATE_URL": "http://127.0.0.1:9",
            },
        )
        self._env.start()
        # No inherited identity from the shell that runs the suite.
        for key in ("CODE_GRAPH_PROJECT", "PROJECT_NAME"):
            os.environ.pop(key, None)
        self.addCleanup(self._env.stop)
        self.addCleanup(self._td.cleanup)

    # -- helpers ----------------------------------------------------------

    def make_registry(self) -> None:
        create_empty_launcher_db(self.db)

    def register(self, *, name: str, folder: Path, prefix: str | None,
                 pid: str, slug: str) -> None:
        add_project(
            self.db,
            project_id=pid,
            name=name,
            slug=slug,
            folder_path=str(folder),
            kg_primary=f"{slug}_KnowledgeGraph",
            codegraph_prefix=prefix,
        )

    def analyzer_for(self, project_name: str, existing: set[str]):
        a = _MOD.CodeGraphAnalyzer(project_name, named_vectors=False)
        a.client = _FakeClient(existing)
        return a

    def run_cli(self, argv: list[str], *, existing: set[str]):
        """Drive ``analyze_code_graph.main()`` with a real argv.

        Returns ``(returncode_or_None, fake_client)``. ``None`` means main()
        got PAST ``create_collections`` (the allow arm) — the walk itself is
        cut short by ``_StopAfterCollections``, since what is under test is
        the drop decision, not the analyzer.
        """
        client = _FakeClient(existing)

        def _fake_connect(self):
            self.client = client
            return True

        class _FakeEmbeddingService:
            code_vector_slot = "codesage_embed"
            code_model_id = "fake-code-model"

            @classmethod
            def for_project(cls, _root):
                return cls()

            def code_backend_ready(self):
                return True

            def close(self):
                pass

        def _boom(self, *a, **kw):
            raise _StopAfterCollections(99)

        with mock.patch.object(_MOD.CodeGraphAnalyzer, "connect", _fake_connect), \
                mock.patch.object(_MOD, "EmbeddingService", _FakeEmbeddingService), \
                mock.patch.object(
                    _MOD.CodeGraphAnalyzer, "analyze_repository", _boom), \
                mock.patch.object(sys, "argv", ["analyze_code_graph.py", *argv]):
            try:
                rc = _MOD.main()
            except _StopAfterCollections:
                return (None, client)
        return (rc, client)


# ---------------------------------------------------------------------------
# 1. The CLI arms — the exact shapes the printed remedy produces
# ---------------------------------------------------------------------------


class CliForceRecreateTests(_GuardTestBase):

    def test_registered_folder_basename_mismatch_refuses_and_deletes_nothing(self):
        """THE defect: the remedy pasted verbatim into a terminal.

        Folder ``widget/`` is registered as ``ACME widget`` bound to
        ``ACME_widget``; with no identity flag the analyzer resolves the
        basename ``Widget``. Before the guard this DROPPED
        ``Widget_Code*``; now it refuses, exits 5, and deletes nothing.
        """
        self.make_registry()
        self.register(name="ACME widget", folder=self.folder,
                      prefix="ACME_widget", pid="p1", slug="acme-widget")

        with mock.patch("sys.stderr", new=_Capture()) as err:
            rc, client = self.run_cli(
                [str(self.folder), "--force-recreate"],
                existing=_five("Widget") | _five("ACME_widget"),
            )

        self.assertEqual(rc, 5, "a refused drop must exit with the distinct code 5")
        self.assertEqual(client.collections.deleted, [],
                         "NOTHING may be deleted on a refusal")
        text = err.text
        self.assertIn("Widget", text)
        self.assertIn("ACME_widget", text)
        self.assertIn("--from-resolver", text)

    def test_registered_folder_with_its_bound_prefix_still_rebuilds(self):
        """The legitimate rebuild — the arm a blanket refusal would break."""
        self.make_registry()
        self.register(name="ACME widget", folder=self.folder,
                      prefix="ACME_widget", pid="p1", slug="acme-widget")

        rc, client = self.run_cli(
            [str(self.folder), "--project", "ACME_widget", "--force-recreate"],
            existing=_five("ACME_widget"),
        )

        self.assertIsNone(rc, "the allow arm must reach the walk")
        self.assertEqual(sorted(client.collections.deleted),
                         sorted(_five("ACME_widget")),
                         "all five of the project's own classes must be dropped")

    def test_basename_collision_with_another_project_refuses(self):
        """The unrecoverable shape: two registrations, one folder name.

        An UNREGISTERED clone at ``widget/`` whose basename resolves to
        ``Widget`` — which is another registered project's bound prefix.
        """
        self.make_registry()
        other = self.root / "elsewhere"
        other.mkdir()
        self.register(name="Widget", folder=other, prefix="Widget",
                      pid="p2", slug="widget-other")

        with mock.patch("sys.stderr", new=_Capture()) as err:
            rc, client = self.run_cli(
                [str(self.folder), "--force-recreate"],
                existing=_five("Widget"),
            )

        self.assertEqual(rc, 5)
        self.assertEqual(client.collections.deleted, [])
        self.assertIn("Widget", err.text)
        self.assertIn(str(other), err.text,
                      "the refusal must name the owning project's folder")

    def test_guard_checks_the_SANITIZED_prefix_not_the_raw_project_name(self):
        """``--project`` takes a raw NAME; the delete targets the SANITIZED
        prefix. The guard must compare the prefix.

        ``"ACME Widget"`` sanitizes to ``ACMEWidget`` — which is exactly this
        project's binding, so this is a legitimate rebuild. A guard that
        compared the raw name against the binding would refuse it (and, worse,
        on the mismatch arm would compare the wrong string in the other
        direction too). The launcher spent v0.2.82 fixing a dual-writer caused
        by display names reaching ``--project``; the guard must not re-import
        that confusion.
        """
        self.make_registry()
        self.register(name="ACME Widget", folder=self.folder,
                      prefix="ACMEWidget", pid="p1", slug="acme-widget")

        rc, client = self.run_cli(
            [str(self.folder), "--project", "ACME Widget", "--force-recreate"],
            existing=_five("ACMEWidget"),
        )

        self.assertIsNone(rc, "the sanitized prefix IS the bound one — allow")
        self.assertEqual(sorted(client.collections.deleted),
                         sorted(_five("ACMEWidget")))

    def test_no_force_recreate_never_deletes_and_never_refuses(self):
        """Leave-alone: without the flag the guard is not consulted at all."""
        self.make_registry()
        self.register(name="ACME widget", folder=self.folder,
                      prefix="ACME_widget", pid="p1", slug="acme-widget")

        rc, client = self.run_cli(
            [str(self.folder)], existing=_five("Widget"),
        )

        self.assertIsNone(rc)
        self.assertEqual(client.collections.deleted, [])


# ---------------------------------------------------------------------------
# 2. Registry tri-state, through create_collections (the delete's own method)
# ---------------------------------------------------------------------------


class RegistryStateTests(_GuardTestBase):

    def test_absent_launcher_db_proceeds(self):
        """Free-tier / standalone: no registry file at all → rebuild works.

        Positively observed absence. Collapsing this into "unreadable" would
        refuse every launcher-less install's legitimate ``--force-recreate``.
        """
        self.assertFalse(self.db.exists())
        a = self.analyzer_for("widget", _five("Widget"))
        a.create_collections(force=True, repo_path=self.folder)
        self.assertEqual(sorted(a.client.collections.deleted),
                         sorted(_five("Widget")))

    def test_unreadable_launcher_db_refuses(self):
        """A registry we cannot READ is not permission to delete."""
        create_corrupt_launcher_db(self.db)
        a = self.analyzer_for("widget", _five("Widget"))
        with self.assertRaises(_MOD.CodeGraphDropRefused) as ctx:
            a.create_collections(force=True, repo_path=self.folder)
        self.assertEqual(ctx.exception.verdict.reason,
                         guard.REASON_REGISTRY_UNREADABLE)
        self.assertEqual(a.client.collections.deleted, [])

    def test_readable_registry_with_no_owner_proceeds(self):
        """Registry read, nobody binds this family → historical behaviour."""
        self.make_registry()
        other = self.root / "elsewhere"
        other.mkdir()
        self.register(name="Something Else", folder=other, prefix="SomethingElse",
                      pid="p3", slug="something-else")
        a = self.analyzer_for("widget", _five("Widget"))
        a.create_collections(force=True, repo_path=self.folder)
        self.assertEqual(sorted(a.client.collections.deleted),
                         sorted(_five("Widget")))

    def test_registered_project_without_a_binding_row_proceeds(self):
        """No binding row = a SUCCESSFUL read that found nothing to own the
        family. A name-DERIVED prefix is a guess, and a guess must not refuse
        a first analyze."""
        self.make_registry()
        self.register(name="widget", folder=self.folder, prefix=None,
                      pid="p4", slug="widget")
        a = self.analyzer_for("widget", _five("Widget"))
        a.create_collections(force=True, repo_path=self.folder)
        self.assertEqual(sorted(a.client.collections.deleted),
                         sorted(_five("Widget")))

    def test_force_without_repo_path_refuses(self):
        """A caller that will not say WHICH project cannot be checked."""
        self.make_registry()
        a = self.analyzer_for("widget", _five("Widget"))
        with self.assertRaises(_MOD.CodeGraphDropRefused) as ctx:
            a.create_collections(force=True)
        self.assertEqual(ctx.exception.verdict.reason, guard.REASON_NO_REPO_PATH)
        self.assertEqual(a.client.collections.deleted, [])


# ---------------------------------------------------------------------------
# 3. The pure decision — the underscore axis, which normalisation would break
# ---------------------------------------------------------------------------


class PureDecisionTests(unittest.TestCase):

    class _Ident:
        def __init__(self, pid, name, folder, bound, registered=True):
            self.project_id = pid
            self.name = name
            self.folder_path = folder
            self._bound = bound
            self.registered = registered

        def authoritative_codegraph_prefix(self):
            return self._bound

    class _Snap:
        def __init__(self, resolvable, projects=()):
            self.resolvable = resolvable
            self.projects = tuple(projects)

        def identity_for_folder(self, folder):
            f = str(Path(folder).resolve())
            for p in self.projects:
                if p.folder_path and str(Path(p.folder_path).resolve()) == f:
                    return p
            return None

    def test_case_difference_alone_is_the_same_class(self):
        """Weaviate class names collide case-INsensitively, so `acme_Widget`
        and `ACME_widget` are ONE family — dropping it from its own folder is
        the legitimate rebuild, not a mismatch."""
        me = self._Ident("p1", "ACME widget", "/tmp/w", "ACME_widget")
        v = guard.decide(Path("/tmp/w"), "acme_Widget",
                         registry_exists=True, snapshot=self._Snap(True, [me]))
        self.assertTrue(v.allowed)
        self.assertEqual(v.reason, guard.REASON_OWN_FAMILY)

    def test_underscore_difference_is_a_different_family(self):
        """`ACMEWidget_Code*` and `ACME_widget_Code*` coexist in Weaviate.

        ``project_identity.normalise_for_match`` strips underscores, so using
        it here would (a) call this a rebuild of the project's own family and
        authorise the drop of a family that is NOT it, and (b) refuse an
        unregistered folder its own legitimate rebuild. The guard uses
        casefold equality precisely to avoid both.
        """
        me = self._Ident("p1", "ACME widget", "/tmp/w", "ACME_widget")
        v = guard.decide(Path("/tmp/w"), "ACMEWidget",
                         registry_exists=True, snapshot=self._Snap(True, [me]))
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, guard.REASON_REGISTERED_PREFIX_MISMATCH)
        self.assertIn("ACME_widget", v.message)

    def test_other_project_binding_outranks_own_absence(self):
        other = self._Ident("p2", "Widget", "/tmp/other", "Widget")
        v = guard.decide(Path("/tmp/w"), "Widget",
                         registry_exists=True,
                         snapshot=self._Snap(True, [other]))
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, guard.REASON_BOUND_TO_OTHER_PROJECT)
        self.assertEqual(v.owner_name, "Widget")

    def test_unresolvable_snapshot_with_registry_present_refuses(self):
        v = guard.decide(Path("/tmp/w"), "Widget",
                         registry_exists=True, snapshot=self._Snap(False))
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, guard.REASON_REGISTRY_UNREADABLE)

    def test_absent_registry_allows_even_with_no_snapshot(self):
        v = guard.decide(Path("/tmp/w"), "Widget",
                         registry_exists=False, snapshot=None)
        self.assertTrue(v.allowed)
        self.assertEqual(v.reason, guard.REASON_NO_REGISTRY)


class RegistryProbeTests(unittest.TestCase):
    """``registry_present`` decides which of the three registry states we are
    in, so both of its resolution branches are pinned.

    The default branch (``db_path=None``) is what production uses; the
    explicit-path branch is the seam ``evaluate(db_path=…)`` forwards, and it
    is exercised end-to-end below so it cannot rot into an untested rung.
    """

    def test_default_branch_follows_the_launcher_db_discovery(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "launcher.db"
            with mock.patch.dict(os.environ,
                                 {"VCT_LAUNCHER_DB_PATH": str(db)}):
                self.assertFalse(guard.registry_present())
                db.write_bytes(b"")
                self.assertTrue(guard.registry_present())

    def test_a_failing_path_probe_assumes_PRESENT(self):
        """Cannot confirm absence ⇒ assume a registry exists.

        The soft-fail must lean toward "present", which routes an
        unresolvable snapshot to a REFUSAL. Leaning toward "absent" would
        turn any probe error into a blanket allow — the failure mode this
        whole module exists to prevent.
        """
        with mock.patch("vco_lib.paths.launcher_db_path",
                        side_effect=OSError("boom")):
            self.assertTrue(guard.registry_present())

    def test_a_raising_snapshot_read_refuses_rather_than_reads_empty(self):
        """"I could not read the DB" must never become "no project is live"."""
        with TemporaryDirectory() as td:
            root = Path(td).resolve()
            db = root / "launcher.db"
            db.write_bytes(b"")
            folder = root / "widget"
            folder.mkdir()
            with mock.patch("vco_lib.project_identity.resolve_snapshot",
                            side_effect=RuntimeError("boom")):
                v = guard.evaluate(folder, "Widget", db_path=db)
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, guard.REASON_REGISTRY_UNREADABLE)

    def test_explicit_path_branch_and_its_evaluate_seam(self):
        with TemporaryDirectory() as td:
            root = Path(td).resolve()
            db = root / "elsewhere.db"
            folder = root / "widget"
            folder.mkdir()
            # Absent at the EXPLICIT path → allow, even though the ambient
            # discovery might find some other DB.
            self.assertFalse(guard.registry_present(db))
            v = guard.evaluate(folder, "Widget", db_path=db)
            self.assertTrue(v.allowed)
            self.assertEqual(v.reason, guard.REASON_NO_REGISTRY)
            # Present but unreadable at the EXPLICIT path → refuse.
            create_corrupt_launcher_db(db)
            self.assertTrue(guard.registry_present(db))
            v = guard.evaluate(folder, "Widget", db_path=db)
            self.assertFalse(v.allowed)
            self.assertEqual(v.reason, guard.REASON_REGISTRY_UNREADABLE)


# ---------------------------------------------------------------------------
# 4. The PRINTED remedy — a command VCO prints is a code path VCO ships
# ---------------------------------------------------------------------------


class PrintedRemedyTests(unittest.TestCase):
    """Both chunker emitters must print an identity-resolving invocation.

    The bare ``. --force-recreate`` shape is the defect itself; it must never
    come back on either emitter (they share a ``condition_id``, so a
    divergence would ship whichever ran first).
    """

    def _emit(self, which: str) -> str:
        from vco_lib import project_init

        with TemporaryDirectory() as td:
            folder = Path(td)
            if which == "semver":
                project_init._emit_chunker_resync_deferral(
                    folder, "0.2.45", "0.2.46")
            else:
                project_init._emit_chunker_revision_resync_deferral(
                    folder, "r-old", "r-new")
            return (folder / ".claude" / "context"
                    / "UPDATE_DEFERRED.md").read_text(encoding="utf-8")

    def test_both_emitters_carry_from_resolver(self):
        for which in ("semver", "revision"):
            with self.subTest(emitter=which):
                content = self._emit(which)
                self.assertIn("--from-resolver --force-recreate", content)
                self.assertNotIn(
                    "code-graph-analyze . --force-recreate", content,
                    "the basename-resolving shape must never come back",
                )

    def test_remedy_has_no_cd_precondition(self):
        """The wrapper is named absolutely and the folder is the analyzer's
        positional ``repo_path`` — so the line works from any cwd, on any OS."""
        for which in ("semver", "revision"):
            with self.subTest(emitter=which):
                content = self._emit(which)
                self.assertNotIn("\ncd ", content)

    def test_schema_regenerate_reingest_remedy_carries_from_resolver(self):
        from vco_lib import schema_regenerate

        cmd = schema_regenerate._reingest_remediation_command(
            "codegraph_collection", Path("/tmp/proj"), "Proj_CodeFunction",
        )
        self.assertIn("--from-resolver", cmd)
        self.assertIn("--force-recreate", cmd)

    def test_schema_regenerate_runner_passes_from_resolver(self):
        """The EXECUTED regenerate (env=os.environ from a plain CLI) must not
        rely on ``$CODE_GRAPH_PROJECT`` being present."""
        from vco_lib import schema_regenerate

        with TemporaryDirectory() as td:
            folder = Path(td)
            scripts = folder / ".claude" / "scripts"
            scripts.mkdir(parents=True)
            for name in ("code-graph-analyze", "code-graph-analyze.ps1"):
                (scripts / name).write_text("#!/bin/sh\nexit 0\n")
            seen: list[list[str]] = []

            def _run(cmd, **kwargs):
                seen.append(list(cmd))

                class _P:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return _P()

            res = schema_regenerate.RegenerateResult(
                artifact_type="codegraph_collection",
                artifact_name="Proj_CodeFunction",
            )
            schema_regenerate._regenerate_codegraph(
                res, folder=folder, env={}, run=_run,
            )
        self.assertEqual(len(seen), 1)
        self.assertIn("--from-resolver", seen[0])
        self.assertIn("--force-recreate", seen[0])


class _Capture:
    """Minimal stderr stand-in (StringIO plus the attrs print() may touch)."""

    def __init__(self) -> None:
        self.text = ""

    def write(self, s: str) -> int:
        self.text += s
        return len(s)

    def flush(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    unittest.main()

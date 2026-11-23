# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.108 HFBUILD — the two other code_embed image REBUILD paths get the
A-021 free-space check install step 5 has (``compose_image_prepull.
build_space_shortfall``: what the existing container's image adds above its
locally stored base — here no base is stored, so the whole image — x 1.5 + the
floor, against the engine's image store).

* ``service_lifecycle.migrate_managed_service`` (the launcher's service move,
  ``service_endpoints move``, and install's code_embed migration): a rebuild
  that does not fit is ``refused`` before anything is written or stopped —
  the container stays on its port, image and project, and the move ends
  ``NOT moved (refused)`` with the need / free / where line.
* ``python -m vco_lib.service_lifecycle up --build``: the service is left
  alone (never removed, never composed), the verb exits 3, prints the line
  and records ``service_image_no_disk_space``.

Both: fits -> builds; unknown size -> builds as before. Fakes only — no
runtime, no real disk read (``_free_bytes`` is patched).
"""
from __future__ import annotations

import functools
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from tests.common.launcher_db_fixture import make_launcher_db
from tests.test_v02100_data_identity import EMBED_DEST, TwoBox, _embed_row
from tests.test_v0297_managed_move import RecorderKwargs
from tests.test_v0297_service_lifecycle import REF, EmbedWorld, _row
from vco_lib import compose_image_prepull as pp
from vco_lib import containers
from vco_lib import service_endpoints as se
from vco_lib import service_lifecycle as sl
from vco_lib import service_reconcile as sr

GIB = 1024 ** 3
IMAGE_ID = "sha256builtce"
CE_SIZE = 8 * GIB
#: what the rebuild needs: 8 GB x 1.5 + the 2 GB default floor = 14 GB
NEED = int(CE_SIZE * pp.PULL_SPACE_FACTOR) + 2 * GIB
RESOLUTION = SimpleNamespace(compose=["podman", "compose"], compose_form="subcommand")


def _cp(argv, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)


def disk_probe(inner, graph_root: Path, size):
    """Wrap a fake runtime: answer the three disk-check reads (the container's
    image id, that image's size, the engine's graph root); pass the rest on.
    *size* ``None`` = the image size cannot be read (unknown)."""

    def run(argv, **kw):
        argv = [str(a) for a in argv]
        if argv[1] == "inspect" and "{{.Image}}" in argv:
            if argv[-1] != REF:
                return _cp(argv, 125, "", f"no such container {argv[-1]}")
            return _cp(argv, 0, IMAGE_ID + "\n")
        if argv[1:3] == ["image", "inspect"] and argv[-1] == IMAGE_ID:
            return _cp(argv, 0, f"{size}\n") if size is not None else _cp(argv, 125, "", "x")
        if argv[1] == "info":
            return _cp(argv, 0, str(graph_root) + "\n")
        return inner(argv, **kw)

    return run


def _ups(argv_log):
    return [a for a in argv_log if "up" in a and a[-1] != "config"]


# ─── migrate_managed_service (launcher move, install migration) ─────────


class MigrateRebuildDiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vco_hfbuild_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = self.tmp / "hf-cache"
        (self.cache / "models--codesage").mkdir(parents=True)
        self.store = self.tmp / "graphroot"
        self.store.mkdir()
        self.committed: list = []
        for p in (mock.patch.object(containers, "_resolve_runtime", return_value="podman"),):
            p.start()
            self.addCleanup(p.stop)

    def world(self) -> EmbedWorld:
        return EmbedWorld(self.tmp, cache={"kind": "bind", "source": str(self.cache)})

    def migrate(self, w: EmbedWorld, *, free: int, size=CE_SIZE, row=None, **kw):
        lines: list[str] = []
        kw.setdefault("commit", self.committed.extend)
        with mock.patch.object(pp, "_free_bytes", lambda _p: free), \
                mock.patch.object(containers, "find_existing_container",
                                  lambda s, r="podman": REF if s == "code_embed" and w.exists
                                  else None):
            result = sl.migrate_managed_service(
                w.root, row or _row("code_embed"), runtime="podman",
                run=disk_probe(w.run, self.store, size), fetch=w.fetch,
                resolution=RESOLUTION, log=lines.append, db_path=self.tmp / "no.db",
                health_timeout_s=0.0, health_interval_s=0.0, **kw)
        return result, lines

    def test_a_rebuild_that_does_not_fit_is_refused_before_anything_is_touched(self):
        w = self.world()
        result, lines = self.migrate(w, free=10 * GIB)
        self.assertEqual(result.status, "refused")
        self.assertEqual(_ups(w.all_argv), [])
        self.assertFalse([a for a in w.all_argv if a[1] in ("stop", "rm")])
        self.assertTrue(w.exists)
        self.assertEqual(w.project, "infrastructure")
        self.assertFalse((w.infra / ".env").exists(), "nothing may be written before the refusal")
        self.assertEqual(self.committed, [])
        for part in (f"{NEED / GIB:.1f} GB free on {self.store}", "10.0 GB is free",
                     "Not building it", "podman system df", "podman image prune"):
            self.assertIn(part, result.reason)
        self.assertTrue(any("nothing was stopped" in line for line in lines), lines)

    def test_a_rebuild_that_fits_builds(self):
        w = self.world()
        result, _ = self.migrate(w, free=100 * GIB)
        self.assertEqual(result.status, "migrated", result.reason)
        (up,) = _ups(w.all_argv)
        self.assertIn("--build", up)

    def test_an_unknown_image_size_builds_as_before(self):
        w = self.world()
        result, _ = self.migrate(w, free=1, size=None)
        self.assertEqual(result.status, "migrated", result.reason)
        (up,) = _ups(w.all_argv)
        self.assertIn("--build", up)

    def test_without_a_rebuild_the_space_is_not_checked(self):
        w = self.world()
        result, _ = self.migrate(w, free=1, build=False)
        self.assertEqual(result.status, "migrated", result.reason)
        (up,) = _ups(w.all_argv)
        self.assertNotIn("--build", up)

    def test_the_service_move_stays_on_its_old_endpoint(self):
        """The launcher's Services-page move (`service_endpoints move`): a
        refused rebuild leaves the row and the container where they were and
        the verb exits 1 with the refusal as its last line — the text the
        launcher shows for a failed move (vco_lib_bridge::child_failure)."""
        w = self.world()
        db = make_launcher_db(self.tmp / "launcher.db")
        se.write_rows([_row("code_embed", container_name=REF, data_mount={
            "kind": "bind", "source": str(self.cache), "destination": "/cache"})], db_path=db)
        rec = RecorderKwargs()
        lines: list[str] = []
        migrate = functools.partial(
            sl.migrate_managed_service, run=disk_probe(w.run, self.store, CE_SIZE),
            fetch=w.fetch, resolution=RESOLUTION, health_timeout_s=0.0, health_interval_s=0.0)
        with mock.patch.object(pp, "_free_bytes", lambda _p: 10 * GIB), \
                mock.patch.object(containers, "find_existing_container",
                                  lambda s, r="podman": REF if s == "code_embed" else None):
            rc = sr.move_endpoint("code_embed", orchestrator_root=w.root, port=11441, db_path=db,
                                  runtime="podman", fetch=lambda u, t: None,
                                  port_free=lambda p: True, migrate=migrate, out=lines.append,
                                  apply_kwargs=rec.kwargs())
        self.assertEqual(rc, 1)
        self.assertIn("code_embed NOT moved (refused)", lines[-1])
        self.assertIn("Not building it", lines[-1])
        self.assertIn("It stays on :11440", lines[-1])
        self.assertEqual(se.load_rows(db)["code_embed"].port, 11440)
        self.assertEqual(_ups(w.all_argv), [])
        self.assertFalse([a for a in w.all_argv if a[1] in ("stop", "rm")])


# ─── `service_lifecycle up --build` (the session hooks' verb) ───────────


@pytest.fixture()
def box(tmp_path):
    root = tmp_path / "orch"
    infra = root / "infrastructure"
    infra.mkdir(parents=True)
    (infra / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    cache = tmp_path / "hf-cache"
    (cache / "models--x").mkdir(parents=True)
    db = make_launcher_db(tmp_path / "launcher.db")
    se.write_rows([_embed_row({"kind": "bind", "source": str(cache),
                               "destination": EMBED_DEST})], db_path=db)
    store = tmp_path / "graphroot"
    store.mkdir()
    two = TwoBox(infra, {"code_embed": [{"Type": "bind", "Source": str(cache),
                                         "Destination": EMBED_DEST}]})
    return SimpleNamespace(infra=infra, db=db, store=store, two=two)


def _up(b, monkeypatch, *, free, size=CE_SIZE, build=True, recreate=()):
    monkeypatch.setattr(pp, "_free_bytes", lambda _p: free)
    out: list[str] = []
    res = sl.up_services(["code_embed"], compose_dir=b.infra, compose_argv=["podman", "compose"],
                         runtime="podman", db_path=b.db, build=build, recreate=list(recreate),
                         run=disk_probe(b.two.run, b.store, size), out=out.append)
    return res, out


def test_up_build_that_does_not_fit_leaves_the_service_alone(box, monkeypatch):
    res, out = _up(box, monkeypatch, free=10 * GIB, recreate=("code_embed",))
    assert res.refused == ["code_embed"] and res.cleared == [] and res.returncode == 3
    assert box.two.calls("up") == [] and box.two.calls("rm") == []
    assert box.two.calls("config") == []  # refused before the guard ran
    (line,) = [o for o in out if "Not building it" in o]
    assert f"{NEED / GIB:.1f} GB free on {box.store}" in line and "10.0 GB is free" in line
    assert "podman image prune" in line
    (entry,) = res.entries
    assert entry.condition_id == pp.CID_NO_DISK_SPACE


def test_up_build_that_fits_builds(box, monkeypatch):
    res, _ = _up(box, monkeypatch, free=100 * GIB)
    assert res.cleared == ["code_embed"] and res.returncode == 0
    (up,) = box.two.calls("up")
    assert "--build" in up and res.entries == []


def test_up_build_with_an_unknown_image_size_builds(box, monkeypatch):
    res, _ = _up(box, monkeypatch, free=1, size=None)
    assert res.cleared == ["code_embed"]
    (up,) = box.two.calls("up")
    assert "--build" in up


def test_up_without_build_is_not_checked(box, monkeypatch):
    res, _ = _up(box, monkeypatch, free=1, build=False)
    assert res.cleared == ["code_embed"] and res.returncode == 0
    (up,) = box.two.calls("up")
    assert "--build" not in up


def test_the_up_cli_exits_non_zero_and_records_the_row(box, monkeypatch, capsys):
    from vco_lib import tool_search_dirs

    monkeypatch.setattr(pp, "_free_bytes", lambda _p: 10 * GIB)
    monkeypatch.setattr(tool_search_dirs, "run", disk_probe(box.two.run, box.store, CE_SIZE))
    rc = sl._main(["up", "--services", "code_embed", "--build", "--compose-dir", str(box.infra),
                   "--compose-cmd", "podman compose", "--runtime", "podman",
                   "--db-path", str(box.db)])
    assert rc == 3
    printed = capsys.readouterr().out
    assert "Not building it" in printed and str(box.store) in printed
    assert box.two.calls("up") == []
    ledger = box.infra.parent / ".claude" / "context" / "UPDATE_DEFERRED.md"
    assert pp.CID_NO_DISK_SPACE in ledger.read_text(encoding="utf-8")

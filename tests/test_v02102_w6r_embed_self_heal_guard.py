# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-R (CI on PR #388) — the embedding self-heal never aborts a bundle.

``embed_slot_fill.reconcile_folder_embeddings`` promised "each half soft-fails
on its own", yet it imported both halves unguarded: under an interpreter
without ``requests`` (the Rust job's bare python3) the chain
``code_embed_model → embedding_service → import requests`` raised
``ModuleNotFoundError`` and aborted the whole ``install-bundle``.

Now each half (its import included) runs in its own guard, and a failure is
LOUD: one stderr line naming the missing module as a broken install with the
repair, plus an ``embed_self_heal_failed`` ledger row, resolved by the next
run in which both halves complete. The block below makes ``requests``
unimportable the way a bare interpreter does — through the real import chain.
"""

from __future__ import annotations

import importlib.abc
import json
import shutil
import sys
from pathlib import Path

import pytest

from tests.test_v02100_bundle_leftovers import _git, _make_templates
from vco_lib import embed_slot_fill as esf
from vco_lib import project_init
from vco_lib.deferral_report import DeferralReport

CID = "embed_self_heal_failed"


class _NoRequests(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "requests" or name.startswith("requests."):
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return None


_EVICT = ("vco_lib.code_embed_model", "vco_lib.embedding_service", "vco_lib.kg_embed_model")


@pytest.fixture
def no_requests():
    """`requests` unimportable, and every module that imported it evicted so
    the self-heal re-runs the real import chain. Teardown puts the ORIGINAL
    module objects back (sys.modules AND the `vco_lib` package attributes),
    so later tests never see a second copy."""
    import vco_lib

    saved = {m: sys.modules[m] for m in list(sys.modules)
             if m == "requests" or m.startswith("requests.") or m in _EVICT}
    attrs = {m: getattr(vco_lib, m.rsplit(".", 1)[1]) for m in _EVICT if hasattr(vco_lib, m.rsplit(".", 1)[1])}
    for m in saved:
        del sys.modules[m]
    finder = _NoRequests()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        for m in _EVICT:
            sys.modules.pop(m, None)
        sys.modules.update(saved)
        for m, mod in attrs.items():
            setattr(vco_lib, m.rsplit(".", 1)[1], mod)
        for m in _EVICT:
            if m not in attrs and hasattr(vco_lib, m.rsplit(".", 1)[1]):
                delattr(vco_lib, m.rsplit(".", 1)[1])


@pytest.fixture
def bundle(tmp_path: Path):
    orch, proj = tmp_path / "orchestrator", tmp_path / "project"
    orch.mkdir()
    proj.mkdir()
    _make_templates(orch)
    _git(orch, "init", "-q")
    _git(orch, "add", "-A")
    _git(orch, "commit", "-q", "-m", "v1")
    yield orch, proj
    shutil.rmtree(tmp_path, ignore_errors=True)


def _ledger(folder: Path) -> dict:
    return {e.condition_id: e for e in DeferralReport.read(folder).entries}


def test_missing_module_does_not_abort_the_bundle(no_requests, bundle, capfd) -> None:
    orch, proj = bundle
    result = project_init.install_project_bundle(proj, orchestrator_root=orch, update_mode=False)
    err = capfd.readouterr().err
    # The bundle completed: its files are there and its manifest is written.
    assert (proj / ".claude" / "hooks" / "foo.sh").is_file()
    assert json.loads((proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8"))["files"]
    assert isinstance(result, dict)
    # Loud: one stderr line naming the module as a broken install, with the repair.
    lines = [ln for ln in err.splitlines() if "broken install" in ln]
    assert lines and all("`requests`" in ln and "install.py --update" in ln for ln in lines), err
    assert any("code-graph" in ln for ln in lines)
    # Recorded through the deferral mechanism.
    row = _ledger(proj)[CID]
    assert "requests" in row.detected and "install.py --update" in row.command_to_apply


def test_a_clean_run_resolves_the_row(bundle, monkeypatch, capfd) -> None:
    orch, proj = bundle
    seen: list[str] = []

    def _boom(folder, dry_run=False):
        raise ModuleNotFoundError("No module named 'requests'", name="requests")

    import vco_lib.code_embed_model as cem
    import vco_lib.kg_embed_model as kem

    monkeypatch.setattr(cem, "reconcile_folder_code_embedding", _boom)
    monkeypatch.setattr(kem, "reconcile_folder_kg_embedding", lambda f, dry_run=False: seen.append("kg"))
    esf.reconcile_folder_embeddings(proj)
    # One half failing never stops the other.
    assert seen == ["kg"]
    assert CID in _ledger(proj)

    monkeypatch.setattr(cem, "reconcile_folder_code_embedding", lambda f, dry_run=False: seen.append("code"))
    esf.reconcile_folder_embeddings(proj)
    assert seen == ["kg", "code", "kg"]
    assert CID not in _ledger(proj)


def test_dry_run_reports_but_writes_no_row(bundle, monkeypatch, capfd) -> None:
    _orch, proj = bundle
    import vco_lib.code_embed_model as cem
    import vco_lib.kg_embed_model as kem

    def _boom(folder, dry_run=False):
        raise RuntimeError("weaviate exploded")

    monkeypatch.setattr(cem, "reconcile_folder_code_embedding", _boom)
    monkeypatch.setattr(kem, "reconcile_folder_kg_embedding", lambda f, dry_run=False: None)
    esf.reconcile_folder_embeddings(proj, dry_run=True)
    assert "RuntimeError: weaviate exploded" in capfd.readouterr().err
    assert CID not in _ledger(proj)

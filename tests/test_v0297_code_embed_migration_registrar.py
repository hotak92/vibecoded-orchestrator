# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""F6 (v0.2.97 review round 7): the code-embed migration spawns no registrar.

Step [5b] promises "no second ``--register-default-mcps`` here" — the
reconcile shim passes ``apply_kwargs={"register_mcps": ...}`` so the run
itself registers the MCPs from the rows (step 11). But the code-embed
migration it triggers right after used the DEFAULT commit
(``service_lifecycle._default_commit`` → ``commit_rows`` → ``apply_change``'s
``_default_registrar``, i.e. ``vct-launcher --register-default-mcps``, 60 s
timeout, twice per migration) — rewriting ``~/.claude.json`` mid-step-5 and
logging failures on a first install where no launcher binary exists yet.

This test drives install.py's real ``_migrate_code_embed_with_cache`` with
the registrar counter patched in, and asserts the commit the migration
receives (and runs, exactly where ``migrate_managed_service`` runs its
identity commit) reaches the real ``commit_rows`` follow-up chain without
ever calling the registrar.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import install  # type: ignore  # noqa: E402
from vco_lib import service_endpoints as se  # noqa: E402
from vco_lib import service_lifecycle as sl  # noqa: E402


def _code_embed_row() -> "se.EndpointRow":
    return se.EndpointRow(service="code_embed", mode="vco_managed", port=11440,
                          source="install_probe")


def test_code_embed_migration_commits_without_a_second_registrar(
    monkeypatch, tmp_path,
) -> None:
    calls = {"registrar": 0}

    def _counting_registrar(_root):
        calls["registrar"] += 1
        return True

    row = _code_embed_row()
    # The real commit_rows follow-up chain, with its default steps no-op'd
    # and the REGISTRAR counted: everything else in the chain runs for real.
    monkeypatch.setattr(se, "_default_registrar", _counting_registrar)
    monkeypatch.setattr(se, "_default_infra_env_writer",
                        lambda _infra, _rows: None)
    monkeypatch.setattr(se, "_default_reprojector", lambda _db: None)
    monkeypatch.setattr(se, "write_rows",
                        lambda rows, db_path=None, now_ms=None: types.SimpleNamespace(
                            propagating=[r.service for r in rows]))
    monkeypatch.setattr(se, "load_rows", lambda db_path=None: {"code_embed": row})

    captured: dict = {}

    def _fake_migrate(root, row_arg, **kwargs):
        captured.update(kwargs)
        commit = kwargs.get("commit")
        assert commit is not None, (
            "install.py must pass a commit: the default one "
            "(_default_commit) runs the MCP registrar"
        )
        # Exactly the identity commit migrate_managed_service performs
        # before any pre-check.
        commit([row_arg])
        return types.SimpleNamespace(ok=True, status="migrated", reason="")

    monkeypatch.setattr(sl, "migrate_code_embed", _fake_migrate)

    install._migrate_code_embed_with_cache(row, "podman", None)

    assert captured, "the migration entry point was not called"
    assert calls["registrar"] == 0, (
        "the migration's row commit spawned --register-default-mcps; "
        "step [5b] promises the run's own step-11 registration is the only one"
    )


def test_the_shims_commit_still_runs_the_rest_of_the_chain(
    monkeypatch, tmp_path,
) -> None:
    """The commit is not a `propagate=False` dodge: infra `.env` writing and
    the re-projection still run — only the MCP registration is the run's own
    step-11 business."""
    row = _code_embed_row()
    ran: list[str] = []
    monkeypatch.setattr(se, "_default_registrar", lambda _r: ran.append("registrar") or True)
    monkeypatch.setattr(se, "_default_infra_env_writer",
                        lambda _infra, _rows: ran.append("infra_env"))
    monkeypatch.setattr(se, "_default_reprojector", lambda _db: ran.append("reproject"))
    monkeypatch.setattr(se, "write_rows",
                        lambda rows, db_path=None, now_ms=None: types.SimpleNamespace(
                            propagating=["code_embed"]))
    monkeypatch.setattr(se, "load_rows", lambda db_path=None: {"code_embed": row})

    captured: dict = {}

    def _fake_migrate(root, row_arg, **kwargs):
        captured.update(kwargs)
        kwargs["commit"]([row_arg])
        return types.SimpleNamespace(ok=True, status="migrated", reason="")

    monkeypatch.setattr(sl, "migrate_code_embed", _fake_migrate)
    install._migrate_code_embed_with_cache(row, "podman", None)

    assert ran == ["infra_env", "reproject"], (
        "the commit must run the infra-env and re-projection legs of the "
        f"follow-up chain (got {ran})"
    )

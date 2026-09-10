# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — whose project is being resolved, and how a user says otherwise.

TWO DEFECTS, ONE FAMILY.

**A. The lying log / the library-import landmine.**
``weaviate_mcp.server`` resolves every collection constant at IMPORT time. Its
project-root ladder was ``CLAUDE_PROJECT_DIR`` → ``Path(__file__)``. Claude Code
sets the first for MCP subprocesses, so the MCP was fine; a project CLI that
merely IMPORTS the module (``templates/scripts/search_knowledge.py`` pulls the
tier helpers from it) runs in a plain shell with no ``CLAUDE_PROJECT_DIR``, hit
the ``Path(__file__)`` arm, and resolved **the orchestrator's** config. It then
let that outrank the project's own correct ``KG_COLLECTION`` env var and
announced the result affirmatively::

    INFO:weaviate_mcp.server:weaviate-kg: resolved collections
    (kg='VCODev_KnowledgeGraph' src=hub, ...)

…from inside a project whose ``KG_COLLECTION`` was ``OtherProj_KnowledgeGraph``.
The CLI's *effective query scope* was never wrong (it resolves its own
collections), so this was a lying log plus a landmine for the next consumer of
those constants — not a wrong query. The fix is therefore in the resolution and
the reporting, NOT in the query path.

The shape that makes it structurally impossible: name the CONTEXT the answer
was keyed on, and let a merely-GUESSED context lose to an explicit env var.
There is no longer a code path that answers "the orchestrator" to a question
about "here" while claiming authority for it.

**B. No hub≠env conflict warning.** When hub-first silently overrode a correct
env var, nothing said so — the pre-existing WARNING only covered falling back to
bundled defaults.

**C. Optional scope specification on `kg-search`.** Requirement, verbatim: "both
[CLI and MCP] should search on both shared and project's KG, unless (optionally)
specified." The default half was already right on both surfaces. The optional
half did not exist on the CLI: its entire flag surface was
``--limit/--type/--tags/--content/--files-only/--detail/--days``. These tests pin
the new ``--project/-p``, ``--collection/-c``, ``--shared-only``, ``--no-shared``
— names taken from ``code-graph-query`` rather than a third convention — and pin
that the DEFAULT scope is unchanged.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SEARCH_KNOWLEDGE = REPO_ROOT / "templates" / "scripts" / "search_knowledge.py"


def _srv():
    import claude_mcp_servers.weaviate_mcp.server as srv  # noqa: PLC0415
    return srv


def _fake_cfg(**fields) -> types.SimpleNamespace:
    base = {
        "kg_collection": "",
        "shared_kg_collection": "",
        "development_collection": "",
        "diagrams_collection": "",
        "code_graph_collection_prefix": "",
        "code_graph_project": "",
        "kg_access_list": [],
        "diagrams_access_list": [],
    }
    base.update(fields)
    return types.SimpleNamespace(**base)


def _make_project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    return root


# ══════════════════════════════════════════════════════════════════════
# A1. Which project root does the resolution key on?
# ══════════════════════════════════════════════════════════════════════

class TestResolutionContext:
    def test_claude_project_dir_is_authoritative(self, tmp_path, monkeypatch):
        srv = _srv()
        ws = _make_project(tmp_path, "ws")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ws))
        root, kind = srv._resolution_context()
        assert root == ws.resolve()
        assert kind == srv._CTX_WORKSPACE
        assert srv._hub_context_is_authoritative() is True

    def test_cwd_project_wins_when_no_workspace_env(self, tmp_path, monkeypatch):
        """THE library-import case: a project CLI in a plain shell.

        Pre-fix this fell straight through to the orchestrator's own root.
        """
        srv = _srv()
        proj = _make_project(tmp_path, "proj")
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(proj)
        root, kind = srv._resolution_context()
        assert root == proj.resolve()
        assert kind == srv._CTX_CWD
        assert srv._hub_context_is_authoritative() is True

    def test_cwd_walks_up_to_the_project_root(self, tmp_path, monkeypatch):
        srv = _srv()
        proj = _make_project(tmp_path, "proj")
        deep = proj / "a" / "b" / "c"
        deep.mkdir(parents=True)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(deep)
        root, kind = srv._resolution_context()
        assert root == proj.resolve()
        assert kind == srv._CTX_CWD

    def test_unmarked_cwd_falls_back_to_the_module_guess(self, tmp_path, monkeypatch):
        srv = _srv()
        plain = tmp_path / "nothing_here"
        plain.mkdir()
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(plain)
        root, kind = srv._resolution_context()
        assert root == srv._MODULE_OWN_ROOT
        assert kind == srv._CTX_MODULE
        # …and the guess is explicitly NOT authoritative. This is the whole
        # fix: a hub answer about the module's own directory must not be
        # allowed to overrule the caller's env.
        assert srv._hub_context_is_authoritative() is False

    # ── v0.2.94: the walk must not climb into the user's HOME ──────────
    #
    # EVERY Claude Code user has ~/.claude/settings.json — the GLOBAL config
    # file, not a project marker. The rung-2 walk probed for exactly that
    # filename, so from any cwd with no project above it the walk climbed all
    # the way to $HOME, matched the global config and returned the home
    # directory as "the project". Everything keyed on the project root (the
    # KG-collection fallback, KG_BASE_DIR-relative writes, doctor paths) then
    # pointed at the user's home. A project lives UNDER home, never at or
    # above it — so home is both rejected and the stopping point.

    def test_user_home_is_never_the_project_root(self, tmp_path, monkeypatch):
        """A cwd under home with no project above it must NOT resolve to home."""
        srv = _srv()
        fake_home = tmp_path / "home" / "someone"
        (fake_home / ".claude").mkdir(parents=True)
        # Claude Code's GLOBAL config — present for every real user.
        (fake_home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        scratch = fake_home / "scratch" / "deeper"
        scratch.mkdir(parents=True)

        monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(fake_home))
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(scratch)

        root, kind = srv._resolution_context()
        assert root != fake_home.resolve(), (
            "~/.claude/settings.json is Claude Code's GLOBAL config, not a "
            "project marker — the walk must never accept the user's home"
        )
        assert (root, kind) == (srv._MODULE_OWN_ROOT, srv._CTX_MODULE)
        # …and the documented degrade still marks the answer a guess, so an
        # explicit env var beats it.
        assert srv._hub_context_is_authoritative() is False

    def test_walk_stops_at_home_and_ignores_markers_above_it(
        self, tmp_path, monkeypatch
    ):
        """Nothing at or above home is a candidate, marked or not."""
        srv = _srv()
        above = tmp_path / "above"
        fake_home = above / "someone"
        (fake_home / "scratch").mkdir(parents=True)
        # A marker ABOVE home (shared mount, stray file) must stay invisible.
        (above / ".claude").mkdir()
        (above / ".claude" / "settings.json").write_text("{}", encoding="utf-8")

        monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(fake_home))
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(fake_home / "scratch")

        root, kind = srv._resolution_context()
        assert root != above.resolve(), "the walk climbed past the home boundary"
        assert (root, kind) == (srv._MODULE_OWN_ROOT, srv._CTX_MODULE)

    def test_real_project_under_home_still_resolves(self, tmp_path, monkeypatch):
        """LEAVE-ALONE: the home rule must not cost the normal case."""
        srv = _srv()
        fake_home = tmp_path / "home" / "someone"
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        proj = _make_project(fake_home, "realproj")
        sub = proj / "src" / "deep"
        sub.mkdir(parents=True)

        monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(fake_home))
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(sub)

        root, kind = srv._resolution_context()
        assert root == proj.resolve()
        assert kind == srv._CTX_CWD
        assert srv._hub_context_is_authoritative() is True

    def test_home_itself_as_cwd_is_not_a_project(self, tmp_path, monkeypatch):
        """The degenerate case the bug report hit: cwd IS the home directory."""
        srv = _srv()
        fake_home = tmp_path / "home" / "someone"
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")

        monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(fake_home))
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(fake_home)

        root, kind = srv._resolution_context()
        assert root != fake_home.resolve()
        assert (root, kind) == (srv._MODULE_OWN_ROOT, srv._CTX_MODULE)

    def test_home_override_cannot_defeat_the_boundary(self, tmp_path, monkeypatch):
        """``VCT_USER_HOME_OVERRIDE`` is a shipped knob — pointing it away from
        the real home must NOT re-open the bug. The OS home keeps holding the
        real ``~/.claude/settings.json`` whatever the override says, so it is a
        boundary too (``_home_boundaries`` returns both)."""
        srv = _srv()
        os_home = tmp_path / "os_home"
        (os_home / ".claude").mkdir(parents=True)
        (os_home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        scratch = os_home / "scratch"
        scratch.mkdir()
        elsewhere = tmp_path / "sandbox_home"      # the override points HERE
        elsewhere.mkdir()

        monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(elsewhere))
        monkeypatch.setenv("HOME", str(os_home))            # POSIX Path.home()
        monkeypatch.setenv("USERPROFILE", str(os_home))     # Windows Path.home()
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(scratch)

        root, kind = srv._resolution_context()
        assert root != os_home.resolve(), (
            "the OS home was accepted as a project because the config "
            "override pointed elsewhere — the boundary must cover both homes"
        )
        assert (root, kind) == (srv._MODULE_OWN_ROOT, srv._CTX_MODULE)

    def test_nonexistent_workspace_env_does_not_win(self, tmp_path, monkeypatch):
        srv = _srv()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "gone"))
        monkeypatch.chdir(tmp_path)
        _, kind = srv._resolution_context()
        assert kind != srv._CTX_WORKSPACE


# ══════════════════════════════════════════════════════════════════════
# A2. Precedence: hub-first when identified, env-first when guessing
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def guessing(monkeypatch):
    """Force the module-path GUESS context."""
    srv = _srv()
    monkeypatch.setattr(
        srv, "_resolution_context", lambda: (srv._MODULE_OWN_ROOT, srv._CTX_MODULE)
    )
    return srv


@pytest.fixture
def identified(monkeypatch):
    """Force an authoritative (workspace) context."""
    srv = _srv()
    monkeypatch.setattr(
        srv, "_resolution_context", lambda: (Path("/tmp/aproject"), srv._CTX_WORKSPACE)
    )
    return srv


class TestConfigFieldPrecedence:
    def test_identified_context_keeps_hub_first(self, identified, monkeypatch):
        srv = identified
        monkeypatch.setattr(
            srv, "_try_resolve_project_config",
            lambda: _fake_cfg(kg_collection="HubSaid_KnowledgeGraph"),
        )
        monkeypatch.setenv("KG_COLLECTION", "EnvSaid_KnowledgeGraph")
        value, source = srv._config_field_with_source(
            "kg_collection", "KG_COLLECTION", "Bundled", empty_means_unset=True
        )
        assert (value, source) == ("HubSaid_KnowledgeGraph", "hub")

    def test_guessed_context_lets_env_win(self, guessing, monkeypatch):
        """THE FIX. Pre-fix the hub's answer about the ORCHESTRATOR beat the
        caller's own KG_COLLECTION, and the label said ``src=hub``."""
        srv = guessing
        monkeypatch.setattr(
            srv, "_try_resolve_project_config",
            lambda: _fake_cfg(kg_collection="Orchestrator_KnowledgeGraph"),
        )
        monkeypatch.setenv("KG_COLLECTION", "MyProject_KnowledgeGraph")
        value, source = srv._config_field_with_source(
            "kg_collection", "KG_COLLECTION", "Bundled", empty_means_unset=True
        )
        assert value == "MyProject_KnowledgeGraph"
        assert source == "env"

    def test_guessed_context_still_beats_the_bundled_default(self, guessing, monkeypatch):
        """Leave-alone case: with env silent, a guess is better than nothing —
        but the label must say the context was unverified."""
        srv = guessing
        monkeypatch.setattr(
            srv, "_try_resolve_project_config",
            lambda: _fake_cfg(kg_collection="Orchestrator_KnowledgeGraph"),
        )
        monkeypatch.delenv("KG_COLLECTION", raising=False)
        value, source = srv._config_field_with_source(
            "kg_collection", "KG_COLLECTION", "Bundled", empty_means_unset=True
        )
        assert value == "Orchestrator_KnowledgeGraph"
        assert source == "hub(unverified-context)"

    def test_no_hub_uses_env(self, identified, monkeypatch):
        srv = identified
        monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: None)
        monkeypatch.setenv("KG_COLLECTION", "EnvOnly_KnowledgeGraph")
        assert srv._config_field_with_source(
            "kg_collection", "KG_COLLECTION", "Bundled", empty_means_unset=True
        ) == ("EnvOnly_KnowledgeGraph", "env")

    def test_no_hub_no_env_uses_default(self, identified, monkeypatch):
        srv = identified
        monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: None)
        monkeypatch.delenv("KG_COLLECTION", raising=False)
        assert srv._config_field_with_source(
            "kg_collection", "KG_COLLECTION", "Bundled", empty_means_unset=True
        ) == ("Bundled", "default")

    def test_empty_env_coercion_semantic_is_preserved(self, identified, monkeypatch):
        """v0.2.27 behaviour must survive the refactor."""
        srv = identified
        monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: None)
        monkeypatch.setenv("KG_COLLECTION", "   ")
        assert srv._config_field_with_source(
            "kg_collection", "KG_COLLECTION", "Bundled", empty_means_unset=True
        ) == ("Bundled", "default(empty-env-coerced)")

    def test_empty_env_is_literal_when_it_carries_meaning(self, identified, monkeypatch):
        srv = identified
        monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: None)
        monkeypatch.setenv("DEVELOPMENT_COLLECTION", "")
        assert srv._config_field_with_source(
            "development_collection", "DEVELOPMENT_COLLECTION", ""
        ) == ("", "env")

    def test_hub_empty_is_a_real_answer_for_shared_kg(self, identified, monkeypatch):
        """An identified project explicitly unbound from the shared KG stays
        unbound (the asymmetric-access design)."""
        srv = identified
        monkeypatch.setattr(
            srv, "_try_resolve_project_config", lambda: _fake_cfg(shared_kg_collection="")
        )
        monkeypatch.setenv("SHARED_KG_COLLECTION", "SomethingStale")
        value, source = srv._config_field_with_source(
            "shared_kg_collection", "SHARED_KG_COLLECTION", "BundledShared",
            hub_empty_is_meaningful=True,
        )
        assert (value, source) == ("", "hub")

    def test_guessed_context_does_not_unbind_the_callers_shared_kg(
        self, guessing, monkeypatch
    ):
        """The same "unbound" answer, but about the ORCHESTRATOR, must not
        blank the caller's shared KG."""
        srv = guessing
        monkeypatch.setattr(
            srv, "_try_resolve_project_config", lambda: _fake_cfg(shared_kg_collection="")
        )
        monkeypatch.setenv("SHARED_KG_COLLECTION", "VCODev_KnowledgeGraph")
        value, source = srv._config_field_with_source(
            "shared_kg_collection", "SHARED_KG_COLLECTION", "BundledShared",
            hub_empty_is_meaningful=True,
        )
        assert (value, source) == ("VCODev_KnowledgeGraph", "env")


# ══════════════════════════════════════════════════════════════════════
# B. Conflict surfacing + honest logging
# ══════════════════════════════════════════════════════════════════════

class TestHubEnvConflictIsSurfaced:
    def test_divergence_is_recorded_and_warned(self, identified, monkeypatch, caplog):
        srv = identified
        srv._HUB_ENV_CONFLICTS.clear()
        monkeypatch.setattr(
            srv, "_try_resolve_project_config",
            lambda: _fake_cfg(kg_collection="HubSaid_KnowledgeGraph"),
        )
        monkeypatch.setenv("KG_COLLECTION", "EnvSaid_KnowledgeGraph")
        srv._config_field_with_source(
            "kg_collection", "KG_COLLECTION", "Bundled", empty_means_unset=True
        )
        assert ("KG_COLLECTION", "HubSaid_KnowledgeGraph", "EnvSaid_KnowledgeGraph") \
            in srv._HUB_ENV_CONFLICTS

        with caplog.at_level(logging.WARNING, logger=srv.logger.name):
            srv._log_collection_resolution()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        text = " ".join(r.getMessage() for r in warnings)
        assert "disagreement" in text
        assert "HubSaid_KnowledgeGraph" in text and "EnvSaid_KnowledgeGraph" in text
        srv._HUB_ENV_CONFLICTS.clear()

    def test_agreement_produces_no_conflict(self, identified, monkeypatch):
        srv = identified
        srv._HUB_ENV_CONFLICTS.clear()
        monkeypatch.setattr(
            srv, "_try_resolve_project_config",
            lambda: _fake_cfg(kg_collection="Same_KnowledgeGraph"),
        )
        monkeypatch.setenv("KG_COLLECTION", "Same_KnowledgeGraph")
        srv._config_field_with_source(
            "kg_collection", "KG_COLLECTION", "Bundled", empty_means_unset=True
        )
        assert srv._HUB_ENV_CONFLICTS == []


class TestResolutionLogIsHonest:
    def test_log_names_the_project_root_and_how_it_was_chosen(
        self, tmp_path, monkeypatch, caplog
    ):
        srv = _srv()
        srv._HUB_ENV_CONFLICTS.clear()
        ws = _make_project(tmp_path, "declared")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ws))
        with caplog.at_level(logging.DEBUG, logger=srv.logger.name):
            srv._log_collection_resolution()
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "resolved collections" in text
        assert "project-root=" in text and "via=workspace" in text, (
            "the line must say WHOSE project it describes — a reader could not "
            "tell before, which is how it got read as 'my CLI is searching the "
            "orchestrator's KG'."
        )

    def test_library_import_logs_at_debug_not_info(self, caplog):
        """The documented diagnostic is the MCP SERVER's startup line. In a
        library import it is not a startup line at all and describes constants
        the surrounding program may never read — asserting it at INFO is what
        put a misleading claim in front of CLI users."""
        srv = _srv()
        srv._HUB_ENV_CONFLICTS.clear()
        assert srv._RUNNING_AS_MCP_SERVER is False, (
            "under import __name__ is not __main__ — that IS the discriminator"
        )
        with caplog.at_level(logging.DEBUG, logger=srv.logger.name):
            srv._log_collection_resolution()
        lines = [
            r for r in caplog.records if "resolved collections" in r.getMessage()
        ]
        assert lines, "the line must still be emitted, just quietly"
        assert all(r.levelno == logging.DEBUG for r in lines)

    def test_mcp_server_process_would_log_at_info(self):
        """Structural pin: the level is chosen by the entry-point check, so the
        documented INFO line survives for the process that actually serves MCP."""
        source = (
            REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
        ).read_text(encoding="utf-8")
        assert '_RUNNING_AS_MCP_SERVER = __name__ == "__main__"' in source
        assert "logging.INFO if _RUNNING_AS_MCP_SERVER else logging.DEBUG" in source


class TestNoOrchestratorGuessRemains:
    def test_resolver_no_longer_reaches_for_the_module_path_inline(self):
        """The ``Path(__file__).parent.parent.parent`` fallback must live in
        ONE place (``_resolution_context``) that also labels it a guess — not
        inline in the resolver where nothing marks it as unverified."""
        source = (
            REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
        ).read_text(encoding="utf-8")
        start = source.index("def _try_resolve_project_config()")
        end = source.index("def _config_field_with_source(")
        body = source[start:end]
        # Strip comments — the historical explainer legitimately NAMES the
        # retired fallback; what must be gone is the code.
        code = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        assert "Path(__file__)" not in code, (
            "the orchestrator-root guess must not be reconstructed inside "
            "_try_resolve_project_config; go through _resolution_context()."
        )
        assert "_resolution_context()" in body


# ══════════════════════════════════════════════════════════════════════
# C. kg-search scope flags
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def sk():
    """Import ``search_knowledge.py`` by path without running __main__."""

# v0.2.92 WP-1 — pin the orchestrator root to the REPO UNDER TEST while the
# script is exec'd.
#
# These CLIs resolve `$VCT_ORCHESTRATOR_ROOT` (that is the fix: it is the
# canonical channel, and without it an installed project can never find
# `kg_access`). `kg_access` then does `sys.path.insert(0, <that root>)`
# (claude_mcp_servers/scripts/kg_access.py:113). `claude_mcp_servers` is a
# NAMESPACE package, so on a machine that has a SECOND orchestrator clone and
# an ambient `$VCT_ORCHESTRATOR_ROOT` pointing at it, merely importing this
# script re-points `claude_mcp_servers.*` for the whole pytest process — and a
# later `import claude_mcp_servers.weaviate_mcp.server` gets the OTHER clone's
# code. That is a test whose meaning depends on the developer's shell.
#
# Pinning here is the same discipline as pinning PYTHONPATH: a repo's own
# suite tests THAT repo. Production behaviour is unchanged and correct — a real
# machine has one orchestrator and the env var names it.
    spec = importlib.util.spec_from_file_location(
        "_test_search_knowledge_v0292", SEARCH_KNOWLEDGE
    )
    mod = importlib.util.module_from_spec(spec)
    _saved = os.environ.get("VCT_ORCHESTRATOR_ROOT")
    os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    try:
        spec.loader.exec_module(mod)
    finally:
        if _saved is None:
            os.environ.pop("VCT_ORCHESTRATOR_ROOT", None)
        else:
            os.environ["VCT_ORCHESTRATOR_ROOT"] = _saved
    return mod


class TestKgAccessHelperIsActuallyImportable:
    def test_sanitizer_resolves_from_the_orchestrator_layout(self, sk):
        """``kg_access`` lives in ``claude_mcp_servers/scripts/``; the editable
        install only puts ``claude_mcp_servers/`` on sys.path, so a bare
        ``import kg_access`` could never resolve on an installed project and the
        launcher's KG access matrix (``VCT_KG_ACCESS_LIST``) was silently
        ignored by this CLI on EVERY install."""
        assert sk._sanitize_kg_prefix is not None, (
            "kg_access must be importable — otherwise --project cannot derive a "
            "collection name and the access-matrix fan-out stays dead."
        )
        # The KG rule DROPS underscores (it is NOT the code-graph rule).
        assert sk._sanitize_kg_prefix("SimRaceTest_AI") == "SimRaceTestAI"


class TestResolveKgScope:
    def test_default_is_project_plus_shared(self, sk, monkeypatch):
        monkeypatch.delenv("VCT_KG_ACCESS_LIST", raising=False)
        assert sk.resolve_kg_scope(
            self_kg="Mine_KnowledgeGraph", shared_kg="Shared_KnowledgeGraph"
        ) == ["Mine_KnowledgeGraph", "Shared_KnowledgeGraph"]

    def test_default_includes_access_matrix_peers(self, sk, monkeypatch):
        monkeypatch.setenv("VCT_KG_ACCESS_LIST", "Peer_One,PeerTwo")
        got = sk.resolve_kg_scope(
            self_kg="Mine_KnowledgeGraph", shared_kg="Shared_KnowledgeGraph"
        )
        assert got[:2] == ["Mine_KnowledgeGraph", "Shared_KnowledgeGraph"]
        assert "PeerOne_KnowledgeGraph" in got  # underscore-dropping KG rule
        assert "PeerTwo_KnowledgeGraph" in got

    def test_no_shared_drops_only_the_shared_collection(self, sk, monkeypatch):
        monkeypatch.delenv("VCT_KG_ACCESS_LIST", raising=False)
        assert sk.resolve_kg_scope(
            no_shared=True,
            self_kg="Mine_KnowledgeGraph", shared_kg="Shared_KnowledgeGraph",
        ) == ["Mine_KnowledgeGraph"]

    def test_shared_only(self, sk):
        assert sk.resolve_kg_scope(
            shared_only=True,
            self_kg="Mine_KnowledgeGraph", shared_kg="Shared_KnowledgeGraph",
        ) == ["Shared_KnowledgeGraph"]

    def test_shared_only_without_a_shared_kg_fails_clearly(self, sk):
        with pytest.raises(sk.ScopeError, match="no shared KG is configured"):
            sk.resolve_kg_scope(
                shared_only=True, self_kg="Mine_KnowledgeGraph", shared_kg=""
            )

    def test_project_selects_explicitly_and_suppresses_peers(self, sk, monkeypatch):
        monkeypatch.setenv("VCT_KG_ACCESS_LIST", "SomePeer")
        assert sk.resolve_kg_scope(
            projects=["Other"],
            self_kg="Mine_KnowledgeGraph", shared_kg="Shared_KnowledgeGraph",
        ) == ["Other_KnowledgeGraph", "Shared_KnowledgeGraph"]

    def test_project_is_repeatable(self, sk):
        assert sk.resolve_kg_scope(
            projects=["A", "B"], no_shared=True,
            self_kg="Mine_KnowledgeGraph", shared_kg="Shared_KnowledgeGraph",
        ) == ["A_KnowledgeGraph", "B_KnowledgeGraph"]

    def test_project_uses_the_kg_sanitizer_not_the_codegraph_one(self, sk):
        """KG names DROP underscores; code-graph class names PRESERVE them.
        Using the wrong one here would query a collection nobody created."""
        assert sk.resolve_kg_scope(
            projects=["SimRaceTest_AI"], no_shared=True, self_kg="X", shared_kg="Y"
        ) == ["SimRaceTestAI_KnowledgeGraph"]

    def test_project_accepts_a_full_collection_name(self, sk):
        assert sk.resolve_kg_scope(
            projects=["Pasted_KnowledgeGraph"], no_shared=True,
            self_kg="X", shared_kg="Y",
        ) == ["Pasted_KnowledgeGraph"]

    def test_collection_bypasses_resolution(self, sk, monkeypatch):
        monkeypatch.setenv("VCT_KG_ACCESS_LIST", "SomePeer")
        assert sk.resolve_kg_scope(
            collections=["Raw_A", "Raw_B"],
            self_kg="Mine_KnowledgeGraph", shared_kg="Shared_KnowledgeGraph",
        ) == ["Raw_A", "Raw_B"]

    @pytest.mark.parametrize(
        "kwargs,needle",
        [
            ({"collections": ["A"], "shared_only": True}, "cannot be combined"),
            ({"collections": ["A"], "projects": ["B"]}, "cannot be combined"),
            ({"collections": ["A"], "no_shared": True}, "cannot be combined"),
            ({"shared_only": True, "no_shared": True}, "contradictory"),
            ({"shared_only": True, "projects": ["B"]}, "drop it or drop --project"),
        ],
    )
    def test_invalid_combinations_fail_loudly(self, sk, kwargs, needle):
        """Silently picking a winner is how a user ends up searching something
        other than what they asked for."""
        with pytest.raises(sk.ScopeError, match=needle):
            sk.resolve_kg_scope(
                self_kg="Mine_KnowledgeGraph", shared_kg="Shared_KnowledgeGraph",
                **kwargs,
            )


class TestCliSurface:
    """Drive the real argparse surface — argv-shape assertions have missed
    live parser rejections before."""

    @staticmethod
    def _run(*args):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers"),
             env.get("PYTHONPATH", "")]
        ).strip(os.pathsep)
        return subprocess.run(
            [sys.executable, str(SEARCH_KNOWLEDGE), *args],
            capture_output=True, text=True, timeout=120, env=env,
        )

    def test_help_advertises_every_scope_flag(self):
        out = self._run("--help")
        assert out.returncode == 0, out.stderr
        for flag in ("--project", "--collection", "--shared-only", "--no-shared"):
            assert flag in out.stdout, f"{flag} missing from --help"

    def test_short_forms_match_code_graph_query(self):
        """``code-graph-query`` uses ``-p``/``-c``; one vocabulary, not two."""
        out = self._run("--help")
        assert "-p NAME" in out.stdout or "-p " in out.stdout
        assert "-c CLASS" in out.stdout or "-c " in out.stdout

    def test_contradictory_flags_exit_two(self):
        out = self._run("search", "x", "--shared-only", "--no-shared")
        assert out.returncode == 2, out.stdout + out.stderr
        assert "not allowed with" in out.stderr

    def test_collection_with_project_exits_two(self):
        out = self._run("search", "x", "--collection", "A", "--project", "B")
        assert out.returncode == 2, out.stdout + out.stderr
        assert "cannot be combined" in out.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))

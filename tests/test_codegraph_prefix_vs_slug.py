# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.23 regression — slug-vs-prefix field contract in the code-graph path.

Context
-------
The hub's ``GET /api/v1/projects/{id}/config`` returns TWO superficially
similar fields:

  * ``code_graph_project``           — legacy alias of ``project_slug``
                                       (returned for callers that key on
                                       the slug, e.g. codegraph-access
                                       matrix joins). May be
                                       hyphen/underscore-mixed.
  * ``code_graph_collection_prefix`` — canonical Weaviate prefix sourced
                                       from ``project_codegraph_bindings.collection_prefix``.

These DIVERGE when the project slug is not already a valid Weaviate
class prefix (e.g. ``orchestrator-root`` → analyzer sanitises to
``Orchestrator_root`` but the binding row says
``VibeCodedOrchestrator``).

Pre-v0.2.23 the analyzer + hooks read ``code_graph_project`` and then
ran it through ``_sanitize_collection_prefix``, producing a DIFFERENT
prefix from the binding row. Symptom: every incremental write since
the project rename landed in zombie collections
(``Orchestrator_root_Code*``) while consumers + the launcher GUI
queried the canonical prefix (``VibeCodedOrchestrator_Code*``) and
saw 0 results.

This test pins the v0.2.23 contract: the analyzer's ``--from-resolver``
branch MUST use ``code_graph_collection_prefix``, NOT
``code_graph_project``, for the write-target prefix.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class AnalyzerFromResolverPrefixContract(unittest.TestCase):
    """Pin the analyzer to read code_graph_collection_prefix, not the slug alias."""

    def test_template_analyzer_uses_collection_prefix_in_from_resolver_branch(self):
        """templates/scripts/analyze_code_graph.py is the shipped source of truth.

        The ``--from-resolver`` branch (the only one that calls the hub) must
        assign ``project_name`` from ``cfg.code_graph_collection_prefix``,
        not from ``cfg.code_graph_project``. Pre-v0.2.23 it did the latter
        — that bug silently routed incremental writes to zombie
        collections after any project rename.
        """
        path = PROJECT_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
        text = path.read_text(encoding="utf-8")

        # Locate the --from-resolver block. We anchor on the unique
        # ``if args.from_resolver:`` line, then scan the next ~40 lines.
        marker = "if args.from_resolver:"
        idx = text.find(marker)
        self.assertGreater(idx, 0, f"{path}: missing '--from-resolver' branch")
        block_end = text.find("\n    if not project_name:", idx)
        self.assertGreater(block_end, idx, f"{path}: malformed branch")
        block = text[idx:block_end]

        # The CORRECT assignment uses code_graph_collection_prefix.
        self.assertIn(
            "cfg.code_graph_collection_prefix",
            block,
            f"{path}: --from-resolver branch must assign from "
            "cfg.code_graph_collection_prefix (the binding-row truth), "
            "not cfg.code_graph_project (the slug alias).",
        )
        # The OLD bug — assigning from the slug alias — must not return.
        # An incidental docstring mention is fine; an actual `cfg.code_graph_project`
        # *assignment* is not.
        bad_assignment = re.search(
            r"project_name\s*=\s*cfg\.code_graph_project\b", block
        )
        self.assertIsNone(
            bad_assignment,
            f"{path}: --from-resolver branch must NOT assign project_name "
            "from cfg.code_graph_project (pre-v0.2.23 bug regression).",
        )


class WeaviateMcpServerPrefixContract(unittest.TestCase):
    """v0.2.23 W3 (2026-05-21) — pin the Weaviate MCP server's CODE_GRAPH_PROJECT
    initialization to read code_graph_collection_prefix, NOT the slug alias.

    The MCP server constructs `CODE_GRAPH_PROJECT` at module-import time from
    the hub-resolver response and uses it as the Weaviate collection prefix
    for code-graph reads/writes (`_code_collection(base)` returns
    `f"{prefix}_{base}"`). If sourced from the slug, post-rename projects
    silently query zombie collections.
    """

    def test_mcp_server_uses_collection_prefix_in_module_init(self):
        path = (
            PROJECT_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
        )
        text = path.read_text(encoding="utf-8")

        # Locate the module-init block. Anchor on the unique
        # `_cfg_for_cgp = _try_resolve_project_config()` line, then scan the
        # next ~8 lines (the assignment lives just below).
        marker = "_cfg_for_cgp = _try_resolve_project_config()"
        idx = text.find(marker)
        self.assertGreater(idx, 0, f"{path}: missing module-init marker")
        block = text[idx:idx + 600]

        # CORRECT assignment: from cfg.code_graph_collection_prefix.
        self.assertIn(
            "cfg_for_cgp.code_graph_collection_prefix",
            block,
            f"{path}: module-init MUST source CODE_GRAPH_PROJECT from "
            "cfg.code_graph_collection_prefix (the binding-row truth), not "
            "cfg.code_graph_project (the slug alias).",
        )
        # OLD bug — assigning CODE_GRAPH_PROJECT from the slug — must not
        # return. An incidental comment mentioning the legacy field is fine
        # (the docstring documents the v0.2.23 fix); an actual assignment
        # `CODE_GRAPH_PROJECT = _cfg_for_cgp.code_graph_project` is not.
        bad_assignment = re.search(
            r"CODE_GRAPH_PROJECT\s*=\s*_cfg_for_cgp\.code_graph_project\b",
            block,
        )
        self.assertIsNone(
            bad_assignment,
            f"{path}: module-init must NOT assign CODE_GRAPH_PROJECT from "
            "cfg.code_graph_project (pre-v0.2.23 W3 bug regression).",
        )


class HookResolverFieldContract(unittest.TestCase):
    """Pin the bash hooks to ask the resolver for code_graph_collection_prefix."""

    HOOKS = [
        "templates/hooks/post-file-edit.sh",
        "templates/hooks/code-graph-incremental.sh",
    ]

    @staticmethod
    def _strip_comments(text: str, marker: str) -> str:
        """Drop lines starting with ``marker`` (after optional whitespace).

        Both bash (``#``) and PowerShell (``#``) use ``#`` for comments.
        We strip them before the regex so an explanatory comment that
        mentions ``--field code_graph_project`` for context can't cause
        a false-positive match.
        """
        out = []
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith(marker):
                continue
            out.append(line)
        return "\n".join(out)

    # v0.2.47 (extras): the hooks now query a SECOND resolver field
    # (`code_graph_extra_paths`) in addition to the canonical
    # `code_graph_collection_prefix`. Both are legitimate. The slug-
    # alias `code_graph_project` remains banned — it routes writes to a
    # divergent zombie prefix after rename. The allowlist below names
    # every field the hook is allowed to ask for; new fields land here
    # explicitly so the test catches accidental regressions to slug
    # alias use.
    ALLOWED_HOOK_RESOLVER_FIELDS = frozenset({
        "code_graph_collection_prefix",   # canonical Weaviate prefix
        "code_graph_extra_paths",         # v0.2.47 (extras)
        # v0.2.72 (P5): per-project ".claude/ is first-party source" bool —
        # drives the --index-dot-claude / --no-index-dot-claude analyzer
        # flag. A resolver FIELD read for a CLI toggle, not a write-target
        # name; unrelated to the banned slug alias.
        "code_graph_index_dot_claude",
    })

    def test_hooks_ask_resolver_for_collection_prefix(self):
        """All hooks that resolve a project-name for the code-graph write
        target MUST ask the resolver for ``code_graph_collection_prefix``,
        never ``code_graph_project``. The slug alias diverges from the
        canonical Weaviate prefix when the slug contains characters that
        the analyzer's sanitiser would re-canonicalise differently from
        the binding row.

        v0.2.47: the hooks ALSO query ``code_graph_extra_paths`` to drive
        the extras-path detection. Both fields are in the allowlist
        ``ALLOWED_HOOK_RESOLVER_FIELDS``; the slug alias
        ``code_graph_project`` remains banned.

        Strategy: scan non-comment lines for ``--field <name>``. The
        hooks construct the resolver path in a variable (``$_RESOLVER``)
        rather than calling it by literal name, so we don't anchor on
        the script name.
        """
        for rel in self.HOOKS:
            path = PROJECT_ROOT / rel
            text = self._strip_comments(path.read_text(encoding="utf-8"), "#")
            fields = re.findall(r"--field\s+(\S+)", text)
            self.assertTrue(
                fields,
                f"{rel}: expected at least one '--field <name>' call",
            )
            # At least one of the fields must be the canonical prefix —
            # that's still the contract for the code-graph write target.
            self.assertIn(
                "code_graph_collection_prefix",
                fields,
                f"{rel}: at least one '--field code_graph_collection_prefix' "
                f"call required (the canonical Weaviate-prefix resolver path)",
            )
            for fld in fields:
                self.assertIn(
                    fld,
                    self.ALLOWED_HOOK_RESOLVER_FIELDS,
                    f"{rel}: '--field {fld!r}' is not in the allowed-fields "
                    "set. code_graph_project is the slug alias and routes "
                    "writes to a divergent prefix after rename. New "
                    "legitimate fields must be added to "
                    "ALLOWED_HOOK_RESOLVER_FIELDS in this test class.",
                )

    # v0.2.47 (extras): PowerShell sibling extras-check uses
    # ``-Field code_graph_extra_paths`` — same allowlist applies.
    ALLOWED_PS1_RESOLVER_FIELDS = frozenset({
        "code_graph_collection_prefix",
        "code_graph_extra_paths",
        # The PS1 sibling at templates/hooks/code-graph-incremental.ps1
        # uses literal "code_graph_extra_paths" but it's quoted on the
        # call site; the regex below matches unquoted tokens only.
    })

    def test_ps1_hook_uses_collection_prefix_field(self):
        """PowerShell sibling of post-file-edit.sh — cross-OS parity.

        v0.2.47: the PS1 sibling also queries ``code_graph_extra_paths``
        via the same allowlist pattern as the bash hook.
        """
        path = PROJECT_ROOT / "templates" / "hooks" / "post-file-edit.ps1"
        text = self._strip_comments(path.read_text(encoding="utf-8"), "#")
        fields = re.findall(r"-Field\s+(\S+)", text)
        self.assertTrue(fields, f"{path}: expected at least one '-Field <name>' call")
        for fld in fields:
            self.assertIn(
                fld,
                self.ALLOWED_PS1_RESOLVER_FIELDS,
                f"{path}: '-Field {fld!r}' not in allowlist "
                "(code_graph_project is the slug alias).",
            )


class DedupInsertUpsertContract(unittest.TestCase):
    """v0.2.23 — pin the upsert fallback that fixes cold-start writes."""

    def test_dedup_insert_falls_back_to_insert_when_object_missing(self):
        """The analyzer's ``_dedup_insert`` must fall back to ``insert()``
        when Weaviate's ``replace()`` reports "no object with id X".

        v0.2.16's docstring claimed ``replace()`` was upsert; the
        weaviate-client v4.21 runtime requires the object to pre-exist.
        Without the fallback, a full re-analysis against an empty
        collection produces 0 successful writes (every file ends up in
        ``insert_errors``). The fix is straightforward — try ``replace``
        first (the canonical hot path for incremental re-writes), then
        ``insert`` on the not-found branch.

        We pin the source-level pattern here so the regression cannot
        be re-introduced by a refactor that drops the fallback.
        """
        for rel in (
            "templates/scripts/analyze_code_graph.py",
            ".claude/scripts/analyze_code_graph.py",
        ):
            path = PROJECT_ROOT / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            # The `_dedup_insert` body must contain BOTH the replace()
            # call AND an insert() fallback gated on "no object with id".
            #
            # Anchor on the replace() call, then look for the fallback
            # within the next ~50 lines.
            anchor = text.find("collection.data.replace(uuid=det_uuid")
            self.assertGreater(anchor, 0, f"{rel}: missing replace() call")
            tail = text[anchor:anchor + 4000]
            self.assertIn(
                "no object with id",
                tail,
                f"{rel}: expected an upsert fallback gated on "
                "'no object with id'",
            )
            self.assertIn(
                "collection.data.insert(uuid=det_uuid",
                tail,
                f"{rel}: expected `collection.data.insert(uuid=det_uuid, **insert_params)` "
                "as the fallback for the 'object does not exist' branch",
            )


class HubAliasFieldDocumentation(unittest.TestCase):
    """Pin the hub-side docstring so future devs don't re-introduce the bug."""

    def test_config_api_warns_against_code_graph_project_as_write_target(self):
        """``config_api.rs`` must document that ``code_graph_project`` is
        the slug alias, and explicitly warn against using it as the
        Weaviate write-target prefix.
        """
        path = PROJECT_ROOT / "launcher" / "src-tauri" / "vct-hub" / "src" / "config_api.rs"
        if not path.exists():
            self.skipTest("config_api.rs not in this layout")
        text = path.read_text(encoding="utf-8")
        # The warning must mention the divergence risk between the slug
        # alias and the canonical prefix.
        self.assertIn("DO NOT use this as the Weaviate write-target prefix", text)
        self.assertIn("code_graph_collection_prefix", text)


if __name__ == "__main__":
    unittest.main()

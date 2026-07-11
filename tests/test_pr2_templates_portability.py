"""Tests for PR-2 (templates portability fixes, 2026-05-06).

Covers four invariants introduced in this PR:

1. The new `{{VCT_ORCHESTRATOR_ROOT}}` placeholder substitutes to the
   LITERAL string `${VCT_ORCHESTRATOR_ROOT}` (not an absolute path), so
   shell / Python consumers can resolve it at runtime instead of having
   the orchestrator-clone path baked into the file.

2. The legacy `{{ORCHESTRATOR_ROOT}}` placeholder still substitutes to
   an absolute path (necessary for Claude Code MCP `command:` YAML which
   does NOT shell-expand env vars).

3. The stale-orchestrator-root heal: when an installed agent .md was
   stamped with a now-stale orchestrator path AND the user has not
   modified it, `update_mode=True` re-stamps it with the current
   orchestrator root instead of falling into "preserve".

4. The 7 KG/code-graph Python scripts in `templates/scripts/` still parse
   (no syntax error) and honor `VCT_ORCHESTRATOR_ROOT` over in-tree
   resolution.

Pure unit tests; no Weaviate / Ollama / network dependencies.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Substitution map invariants
# ---------------------------------------------------------------------------


class AgentSubsTests(unittest.TestCase):
    def test_orchestrator_root_resolves_to_absolute_path(self):
        orch = Path("/opt/vco-clone")
        subs = project_init._agent_subs(orch)
        self.assertEqual(subs["{{ORCHESTRATOR_ROOT}}"], "/opt/vco-clone")

    def test_projects_root_is_orch_parent(self):
        orch = Path("/opt/vco-clone")
        subs = project_init._agent_subs(orch)
        self.assertEqual(subs["{{PROJECTS_ROOT}}"], "/opt")

    def test_home_resolves_to_actual_home(self):
        orch = Path("/opt/vco-clone")
        subs = project_init._agent_subs(orch)
        self.assertEqual(subs["{{HOME}}"], str(Path.home()))

    def test_vct_orchestrator_root_is_literal_shell_expansion(self):
        """PR-2: must remain the literal `${VCT_ORCHESTRATOR_ROOT}` string,
        not the resolved orchestrator path. Consumers (hooks, Python
        scripts) expand it at runtime via env."""
        orch = Path("/opt/vco-clone")
        subs = project_init._agent_subs(orch)
        self.assertEqual(
            subs["{{VCT_ORCHESTRATOR_ROOT}}"],
            "${VCT_ORCHESTRATOR_ROOT}",
        )
        # Hard guard: if a future change ever resolves this to an absolute
        # path, scripts that source `.claude/env` will silently fall back
        # to the in-tree resolution path and produce stale wiring.
        self.assertNotIn("/opt/vco-clone", subs["{{VCT_ORCHESTRATOR_ROOT}}"])

    def test_vct_orchestrator_root_substitution_round_trip(self):
        """Apply the substitution to a sample template body and confirm
        the literal `${VCT_ORCHESTRATOR_ROOT}` survives."""
        orch = Path("/opt/vco-clone")
        subs = project_init._agent_subs(orch)
        body = (
            "# Sample\n"
            "Path A: {{ORCHESTRATOR_ROOT}}/claude_mcp_servers/foo\n"
            "Path B: {{VCT_ORCHESTRATOR_ROOT}}/claude_mcp_servers/foo\n"
        )
        for k, v in subs.items():
            body = body.replace(k, v)
        # A: absolute (for YAML execvp consumers)
        self.assertIn("/opt/vco-clone/claude_mcp_servers/foo", body)
        # B: literal env var (for shell / Python consumers)
        self.assertIn("${VCT_ORCHESTRATOR_ROOT}/claude_mcp_servers/foo", body)

    # ── {{PROJECT_ROOT}} placeholder (follow-up #9, 2026-05-07) ──

    def test_project_root_resolves_to_install_target(self):
        """`project_root` arg = the project folder being installed into.
        `{{PROJECT_ROOT}}` resolves to its absolute path so agent .md
        bodies can reference project-relative paths cleanly."""
        orch = Path("/opt/vco-clone")
        proj = Path("/home/user/MyProject")
        subs = project_init._agent_subs(orch, project_root=proj)
        self.assertEqual(subs["{{PROJECT_ROOT}}"], "/home/user/MyProject")
        # Sanity: doesn't shadow {{ORCHESTRATOR_ROOT}}.
        self.assertEqual(subs["{{ORCHESTRATOR_ROOT}}"], "/opt/vco-clone")

    def test_project_root_falls_back_to_orchestrator_when_none(self):
        """Orchestrator self-install: `project_root` is None.
        `{{PROJECT_ROOT}}` resolves to the orchestrator root (the
        orchestrator IS its own project at install time). Lets agent
        templates be installed into the orchestrator without
        substitution failing."""
        orch = Path("/opt/vco-clone")
        subs = project_init._agent_subs(orch)
        self.assertEqual(subs["{{PROJECT_ROOT}}"], "/opt/vco-clone")
        # And explicit None has the same fallback.
        subs2 = project_init._agent_subs(orch, project_root=None)
        self.assertEqual(subs2["{{PROJECT_ROOT}}"], "/opt/vco-clone")

    def test_project_root_substitution_round_trip(self):
        """Apply substitutions to an agent body using all 4 placeholders,
        confirm they each resolve to the correct distinct value."""
        orch = Path("/opt/vco-clone")
        proj = Path("/home/user/MyProject")
        subs = project_init._agent_subs(orch, project_root=proj)
        body = (
            "# Agent body\n"
            "Orchestrator: {{ORCHESTRATOR_ROOT}}\n"
            "Project: {{PROJECT_ROOT}}\n"
            "Projects parent: {{PROJECTS_ROOT}}\n"
            "Env-var form: {{VCT_ORCHESTRATOR_ROOT}}\n"
            "Project file: {{PROJECT_ROOT}}/CLAUDE.md\n"
        )
        for k, v in subs.items():
            body = body.replace(k, v)
        self.assertIn("Orchestrator: /opt/vco-clone", body)
        self.assertIn("Project: /home/user/MyProject", body)
        self.assertIn("Projects parent: /opt", body)
        self.assertIn("Env-var form: ${VCT_ORCHESTRATOR_ROOT}", body)
        self.assertIn("Project file: /home/user/MyProject/CLAUDE.md", body)

    def test_project_root_distinct_from_orchestrator_root(self):
        """Most realistic case: project lives next to (not inside) the
        orchestrator clone. Both placeholders MUST resolve to distinct
        absolute paths so agents can reference each independently."""
        orch = Path("/home/user/code/orch")
        proj = Path("/home/user/code/proj")
        subs = project_init._agent_subs(orch, project_root=proj)
        self.assertNotEqual(subs["{{ORCHESTRATOR_ROOT}}"], subs["{{PROJECT_ROOT}}"])
        self.assertEqual(
            subs["{{ORCHESTRATOR_ROOT}}"],
            "/home/user/code/orch",
        )
        self.assertEqual(
            subs["{{PROJECT_ROOT}}"],
            "/home/user/code/proj",
        )


# ---------------------------------------------------------------------------
# 2. Stale-orchestrator-root heal
# ---------------------------------------------------------------------------


class StaleOrchRootHealTests(unittest.TestCase):
    """End-to-end heal scenario via _file_action.

    The installed agent .md was stamped against /old/clone. The user has
    not modified it; they only moved the clone to /new/clone. Running
    install_project_bundle in update_mode should re-stamp it cleanly.
    """

    def _make_fake_orch(self, root: Path) -> None:
        (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
        agents = root / "templates" / "agents" / "free"
        agents.mkdir(parents=True)
        (agents / "coder.md").write_text(
            "---\n"
            "name: coder\n"
            "mcpServers:\n"
            "  orchestrator-tools:\n"
            "    command: {{ORCHESTRATOR_ROOT}}/claude_mcp_servers/.venv/bin/python\n"
            "    args:\n"
            "      - {{ORCHESTRATOR_ROOT}}/claude_mcp_servers/server.py\n"
            "---\n"
            "# Coder\n",
            encoding="utf-8",
        )

    def test_heal_overwrites_stale_root(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            tmp = Path(tmp_root)
            old_orch = tmp / "old" / "vco-clone"
            new_orch = tmp / "new" / "vco-clone"
            project = tmp / "project"
            old_orch.mkdir(parents=True)
            new_orch.mkdir(parents=True)
            project.mkdir(parents=True)

            # Build identical fake orchestrators at both paths.
            self._make_fake_orch(old_orch)
            self._make_fake_orch(new_orch)

            # Step 1: install bundle from old_orch (stamps old paths into agent .md).
            project_init.install_project_bundle(
                project, orchestrator_root=old_orch, update_mode=False,
            )
            agent_path = project / ".claude" / "agents" / "coder.md"
            installed_text = agent_path.read_text(encoding="utf-8")
            self.assertIn(str(old_orch), installed_text)
            self.assertNotIn(str(new_orch), installed_text)

            # Step 2: simulate orchestrator clone moving — call install
            # bundle from new_orch in update mode. Heal should re-stamp.
            result = project_init.install_project_bundle(
                project, orchestrator_root=new_orch, update_mode=True,
            )
            healed = agent_path.read_text(encoding="utf-8")
            self.assertNotIn(str(old_orch), healed)
            self.assertIn(str(new_orch), healed)
            # Action should be `overwrite` (heal), not `preserve` (would
            # have skipped) and not `noop` (content actually changed).
            agent_rel = str(Path(".claude") / "agents" / "coder.md")
            self.assertIn(agent_rel, result["actions"]["overwrite"])
            self.assertNotIn(agent_rel, result["actions"]["preserve"])

    def test_heal_does_NOT_overwrite_user_modifications(self):
        """Counter-test: if the user actually edited the file (e.g. added
        a custom tool entry), heal must NOT fire — falls through to
        `preserve` so the user's edit isn't clobbered.
        """
        with tempfile.TemporaryDirectory() as tmp_root:
            tmp = Path(tmp_root)
            old_orch = tmp / "old" / "vco-clone"
            new_orch = tmp / "new" / "vco-clone"
            project = tmp / "project"
            old_orch.mkdir(parents=True)
            new_orch.mkdir(parents=True)
            project.mkdir(parents=True)
            self._make_fake_orch(old_orch)
            self._make_fake_orch(new_orch)

            project_init.install_project_bundle(
                project, orchestrator_root=old_orch, update_mode=False,
            )
            agent_path = project / ".claude" / "agents" / "coder.md"
            # User customisation
            tampered = agent_path.read_text(encoding="utf-8") + "\n# user note\n"
            agent_path.write_text(tampered, encoding="utf-8")

            result = project_init.install_project_bundle(
                project, orchestrator_root=new_orch, update_mode=True,
            )
            survived = agent_path.read_text(encoding="utf-8")
            # User edit is intact.
            self.assertIn("# user note\n", survived)
            agent_rel = str(Path(".claude") / "agents" / "coder.md")
            self.assertIn(agent_rel, result["actions"]["preserve"])
            self.assertNotIn(agent_rel, result["actions"]["overwrite"])


# ---------------------------------------------------------------------------
# 3. Templates carry no fresh `{{ORCHESTRATOR_ROOT}}` literal after install
# ---------------------------------------------------------------------------


class AgentMdInstallSubstitutesTests(unittest.TestCase):
    """The shipped templates/agents/free/*.md DO contain
    {{ORCHESTRATOR_ROOT}}; after install, the installed copy must NOT.
    Regression-guards the substitution pipeline against silent breakage.
    """

    def test_no_unsubstituted_placeholder_in_installed_agent(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            tmp = Path(tmp_root)
            project = tmp / "proj"
            project.mkdir()

            # Use the actual repo's templates as the orchestrator source.
            project_init.install_project_bundle(
                project, orchestrator_root=REPO_ROOT, update_mode=False,
            )
            installed_agents = project / ".claude" / "agents"
            if not installed_agents.exists():
                self.skipTest("templates/agents/free/ not present in repo checkout")
            for md in installed_agents.glob("*.md"):
                text = md.read_text(encoding="utf-8")
                self.assertNotIn("{{ORCHESTRATOR_ROOT}}", text,
                                 f"{md.name} still has {{ORCHESTRATOR_ROOT}}")
                self.assertNotIn("{{PROJECTS_ROOT}}", text,
                                 f"{md.name} still has {{PROJECTS_ROOT}}")
                self.assertNotIn("{{HOME}}", text,
                                 f"{md.name} still has {{HOME}}")
                # The new placeholder, if used, may legitimately appear
                # post-substitution as the LITERAL ${VCT_ORCHESTRATOR_ROOT}.
                self.assertNotIn("{{VCT_ORCHESTRATOR_ROOT}}", text,
                                 f"{md.name} still has unresolved {{VCT_ORCHESTRATOR_ROOT}}")


# ---------------------------------------------------------------------------
# 4. The 7 rewired Python scripts honor VCT_ORCHESTRATOR_ROOT
# ---------------------------------------------------------------------------


# Scripts that historically did
#     sys.path.insert(0, .../claude_mcp_servers)
# and have been retro-fitted to consult VCT_ORCHESTRATOR_ROOT.
#
# v0.2.38 A1: detect_duplicates.py uses the `weaviate` client directly
# (no `weaviate_mcp.*` imports) so its claude_mcp_servers/ sys.path entry
# was vestigial. A1 dropped it when introducing the pip-installable
# weaviate_mcp package; the script no longer needs to consult
# VCT_ORCHESTRATOR_ROOT for that purpose. Removed from this list so the
# contract reflects reality.
PR2_REWIRED_SCRIPTS = [
    "sync_knowledge_graph.py",
    "search_knowledge.py",
    "analyze_code_graph.py",
    "process_documents.py",
    "maintain_knowledge_graph.py",
    "generate-kg-summary.py",
]


class RewiredScriptsTests(unittest.TestCase):
    SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"

    def test_all_seven_scripts_present(self):
        for name in PR2_REWIRED_SCRIPTS:
            self.assertTrue(
                (self.SCRIPTS_DIR / name).exists(),
                f"missing {name} — PR-2 expected this file in templates/scripts/",
            )

    def test_all_reference_vct_orchestrator_root(self):
        """Each script must read VCT_ORCHESTRATOR_ROOT from os.environ.

        We grep instead of importing because the scripts have heavy
        third-party imports (weaviate, yaml, requests) that aren't in the
        test environment. Grep is sufficient: if the env-var name appears
        in source, the rewiring is in place.
        """
        for name in PR2_REWIRED_SCRIPTS:
            text = (self.SCRIPTS_DIR / name).read_text(encoding="utf-8")
            self.assertIn(
                "VCT_ORCHESTRATOR_ROOT", text,
                f"{name} does not consult VCT_ORCHESTRATOR_ROOT — see PR-2 brief D",
            )

    def test_all_parse_as_python(self):
        """Each script must parse as Python 3 (no syntax errors after
        the rewire). We use ast.parse because subprocess `python -m
        py_compile` would also try to import the heavy deps."""
        for name in PR2_REWIRED_SCRIPTS:
            text = (self.SCRIPTS_DIR / name).read_text(encoding="utf-8")
            try:
                ast.parse(text)
            except SyntaxError as e:
                self.fail(f"{name} failed to parse: {e}")

    def test_resolution_helper_works_in_isolation(self):
        """Build a 2-layer fake (tempdir/clone + tempdir/project), set
        VCT_ORCHESTRATOR_ROOT to the clone, and run a stripped helper
        that mirrors the rewired pattern. Confirms the env-var-first /
        in-tree-fallback chain.
        """
        helper_src = textwrap.dedent('''
            import os, sys
            from pathlib import Path

            def resolve(here):
                env = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
                if env:
                    c = Path(env) / "claude_mcp_servers"
                    if c.is_dir():
                        return str(c)
                c = here.parent.parent / "claude_mcp_servers"
                if c.is_dir():
                    return str(c)
                return ""

            here = Path(sys.argv[1]).resolve()
            print(resolve(here))
        ''')

        with tempfile.TemporaryDirectory() as tmp_root:
            tmp = Path(tmp_root)
            clone = tmp / "vco-clone"
            clone_mcp = clone / "claude_mcp_servers"
            clone_mcp.mkdir(parents=True)
            (clone_mcp / "__init__.py").write_text("", encoding="utf-8")

            proj = tmp / "proj" / ".claude" / "scripts"
            proj.mkdir(parents=True)
            here_file = proj / "fake_script.py"
            here_file.write_text("", encoding="utf-8")

            helper = tmp / "helper.py"
            helper.write_text(helper_src, encoding="utf-8")

            # Case A: VCT_ORCHESTRATOR_ROOT set → wins.
            env = {**os.environ, "VCT_ORCHESTRATOR_ROOT": str(clone)}
            out = subprocess.check_output(
                [sys.executable, str(helper), str(here_file)], env=env,
            ).decode().strip()
            self.assertEqual(out, str(clone_mcp))

            # Case B: VCT_ORCHESTRATOR_ROOT unset, in-tree fallback fails
            # (no claude_mcp_servers next to project) → empty string.
            env_no = {k: v for k, v in os.environ.items()
                      if k != "VCT_ORCHESTRATOR_ROOT"}
            out = subprocess.check_output(
                [sys.executable, str(helper), str(here_file)], env=env_no,
            ).decode().strip()
            self.assertEqual(out, "")


# ---------------------------------------------------------------------------
# 5. ensure-containers hook: container-name lib + compose-dir resolution
# ---------------------------------------------------------------------------


class EnsureContainersHookTests(unittest.TestCase):
    HOOK_SH = REPO_ROOT / "templates" / "hooks" / "ensure-containers.sh"
    HOOK_PS1 = REPO_ROOT / "templates" / "hooks" / "ensure-containers.ps1"
    LIB_SH = REPO_ROOT / "templates" / "hooks" / "_lib" / "container-names.sh"
    LIB_PS1 = REPO_ROOT / "templates" / "hooks" / "_lib" / "container-names.ps1"
    COMPOSE = REPO_ROOT / "infrastructure" / "docker-compose.yml"

    def test_hook_sources_container_names_lib(self):
        text = self.HOOK_SH.read_text(encoding="utf-8")
        self.assertIn("_lib/container-names.sh", text,
                      "ensure-containers.sh must source the canonical "
                      "container-name registry — see PR-2 brief A")
        self.assertIn("VCO_REQUIRED_CONTAINERS", text)

    def test_ps1_hook_sources_container_names_lib(self):
        text = self.HOOK_PS1.read_text(encoding="utf-8")
        self.assertIn("_lib\\container-names.ps1", text,
                      "ensure-containers.ps1 must dot-source the canonical "
                      "container-name registry")
        self.assertIn("VcoRequiredContainers", text)

    def test_lib_container_names_match_compose(self):
        """Single source of truth: the names declared in container-names.sh
        must equal the `container_name:` fields in
        infrastructure/docker-compose.yml. If these drift, the hook will
        try to start containers that don't exist.
        """
        lib_text = self.LIB_SH.read_text(encoding="utf-8")
        # Extract the canonical assignments.
        lib_names = set()
        for line in lib_text.splitlines():
            m = re.match(r'^\s*VCO_(\w+)_CONTAINER\s*=\s*"([^"]+)"', line)
            if m:
                lib_names.add(m.group(2))
        # v0.2.15 rename: vct_code_embed -> vco_code_embed for naming
        # consistency. Source the canonical set from
        # vco_lib.containers.CANONICAL_CONTAINERS so this test cannot
        # drift from the registry.
        from vco_lib.containers import CANONICAL_CONTAINERS
        expected_canonical = set(CANONICAL_CONTAINERS.values())
        self.assertEqual(
            lib_names,
            expected_canonical,
            f"container-names.sh drifted from CANONICAL_CONTAINERS "
            f"({expected_canonical})",
        )

        # Pull container_name fields from the compose file.
        compose_text = self.COMPOSE.read_text(encoding="utf-8")
        compose_names = set(re.findall(
            r"^\s*container_name:\s*(\S+)\s*$",
            compose_text,
            flags=re.MULTILINE,
        ))
        self.assertEqual(
            lib_names, compose_names,
            f"hook lib names {lib_names} != compose names {compose_names}",
        )

    def test_hook_no_longer_hardcodes_legacy_names(self):
        """Pre-PR-2 the hook defaulted to weaviate_claude / ollama_claude
        (the maintainer's own pre-VCO container names — NOT names VCO
        ever shipped). The bundled compose declares vco_weaviate /
        vco_ollama, so the legacy defaults guaranteed the hook would
        fail in user projects. v0.2.15 widened this regression guard:
        the legacy names come from
        `vco_lib.containers.HISTORICAL_ALIASES` (sans the canonical
        names themselves) so any future addition there is automatically
        regression-guarded in active hook code without code changes here.

        The test tokenises hook code (whitespace + a few shell
        delimiters) so a legacy name like `weaviate` cannot
        false-match the canonical `vco_weaviate` (substring containment
        would: `'weaviate' in 'vco_weaviate'`). Token equality is the
        right semantic check — we're asserting "no bare reference to
        the legacy container name", not "the legacy substring does
        not appear in any longer identifier".
        """
        from vco_lib.containers import (
            CANONICAL_CONTAINERS,
            HISTORICAL_ALIASES,
        )
        canonical_set = set(CANONICAL_CONTAINERS.values())
        legacy_names: list[str] = []
        for aliases in HISTORICAL_ALIASES.values():
            for name in aliases:
                if name not in canonical_set:
                    legacy_names.append(name)

        # Shell delimiters that separate identifiers from each other.
        # `=` for `VAR=value`, `:` for env defaults `${VAR:-...}`, `;`
        # for command separators, `(`/`)` for arrays/subshells, `"`
        # for quotes, `'` for single quotes, `,` is rare in shell but
        # harmless to include.
        _DELIMS = " \t\n=:;()'\","
        _TRANS = str.maketrans({c: " " for c in _DELIMS})

        text = self.HOOK_SH.read_text(encoding="utf-8")
        for legacy in legacy_names:
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                tokens = stripped.translate(_TRANS).split()
                self.assertNotIn(
                    legacy, tokens,
                    f"legacy container name {legacy!r} survives as a "
                    f"bare identifier in active hook code "
                    f"(pre-PR-2 / v0.2.15 regression). Offending line: "
                    f"{stripped!r}",
                )

    def test_hook_compose_dir_prefers_infrastructure_over_legacy(self):
        """`ensure-containers.sh` must look at the bundled
        `<project>/infrastructure/` (where install copies the compose
        file) BEFORE falling back to `<project>/claude_mcp_servers/`
        (which only exists in the orchestrator clone).
        """
        text = self.HOOK_SH.read_text(encoding="utf-8")
        # Inspect only non-comment lines so descriptive comments don't
        # poison the ordering check (the resolution chain is documented
        # in the file header before it's implemented).
        code_lines = [
            line for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        # Find the FIRST code-line index that assigns COMPOSE_DIR from
        # each of the two relative-to-REPO_ROOT roots.
        def _first_index(needle: str) -> int:
            for i, line in enumerate(code_lines):
                if needle in line and "COMPOSE_DIR=" in line:
                    return i
            return -1
        idx_infra = _first_index('REPO_ROOT/infrastructure"')
        idx_legacy = _first_index('REPO_ROOT/claude_mcp_servers"')
        self.assertGreaterEqual(idx_infra, 0,
                                "no $REPO_ROOT/infrastructure assignment in "
                                "ensure-containers.sh")
        self.assertGreaterEqual(idx_legacy, 0,
                                "no $REPO_ROOT/claude_mcp_servers fallback "
                                "in ensure-containers.sh")
        self.assertLess(idx_infra, idx_legacy,
                        "infrastructure assignment must come BEFORE the "
                        "claude_mcp_servers legacy fallback")

    def test_hook_honors_vct_orchestrator_root_env(self):
        text = self.HOOK_SH.read_text(encoding="utf-8")
        self.assertIn("VCT_ORCHESTRATOR_ROOT", text,
                      "ensure-containers.sh must consult "
                      "VCT_ORCHESTRATOR_ROOT — see PR-2 brief A")
        self.assertIn("VCT_INFRASTRUCTURE_DIR", text,
                      "ensure-containers.sh must consult "
                      "VCT_INFRASTRUCTURE_DIR — see PR-2 brief A")


# ---------------------------------------------------------------------------
# 6. infrastructure/docker-compose.yml: env-overridable build context
# ---------------------------------------------------------------------------


class ComposeBuildContextTests(unittest.TestCase):
    COMPOSE = REPO_ROOT / "infrastructure" / "docker-compose.yml"

    def test_code_embed_build_context_is_env_overridable(self):
        text = self.COMPOSE.read_text(encoding="utf-8")
        # Must use ${VCT_CODE_EMBED_BUILD_CONTEXT:-...} so user projects
        # can override (or skip) the build path that defaults to a path
        # that doesn't exist outside the orchestrator clone.
        self.assertIn(
            "${VCT_CODE_EMBED_BUILD_CONTEXT",
            text,
            "code_embed.build.context must be env-var-overridable — "
            "see PR-2 brief C",
        )

    def test_code_embed_remains_gpu_profile_gated(self):
        """The bundled service must stay behind the `gpu` profile so
        CPU-only `compose up -d` invocations don't try to build it.
        """
        text = self.COMPOSE.read_text(encoding="utf-8")
        block = self._code_embed_service_block(text)
        self.assertIn("profiles:", block)
        self.assertIn("- gpu", block)

    @staticmethod
    def _code_embed_service_block(text: str) -> str:
        """Return the EXACT `code_embed:` service block (header → next
        top-level 2-space-indented service key, or EOF).

        v0.2.77: replaces the previous brittle fixed 3500-char window, which
        false-failed when a legitimate service-body comment pushed the
        `profiles:` gate past the window (the gate was still present, just
        beyond 3500 chars). Delimiting on the next sibling service key makes
        the assertion robust to body edits of any length.
        """
        lines = text.splitlines(keepends=True)
        # Find the `  code_embed:` service header (2-space indent under
        # `services:`) — accept either the exact indented form or a bare find.
        start = None
        for i, ln in enumerate(lines):
            if ln.lstrip().startswith("code_embed:") and (
                ln.startswith("  code_embed:") or ln.strip() == "code_embed:"
            ):
                start = i
                break
        assert start is not None, "code_embed: service header not found"
        # The block ends at the next line that starts a sibling service:
        # exactly 2 leading spaces + a non-space (another `  <name>:`), and
        # not a comment.
        end = len(lines)
        for j in range(start + 1, len(lines)):
            ln = lines[j]
            if (
                ln.startswith("  ")
                and not ln.startswith("   ")
                and ln[2:3] not in (" ", "#")
                and ln.rstrip().endswith(":")
            ):
                end = j
                break
        return "".join(lines[start:end])


if __name__ == "__main__":
    unittest.main()

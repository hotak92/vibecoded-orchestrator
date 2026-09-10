# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-16 (N5): the `VCO-REWIRE` install-time rewriter.

Ten sentinel-marked regions in `templates/scripts/` served a template-drift
gate that was RETIRED in PR-39 / v0.2.12; since then they had no consumer at
all. Ruling R21 kept R4 and chose to CONCRETIZE the convention:
`vco_lib/rewire.py` is now that consumer, and it delivers a real user-facing
capability — an installed script resolves its orchestrator clone with
`$VCT_ORCHESTRATOR_ROOT` UNSET, which fails today.

The WP-15/WP-16 split is asserted here as well as described: a user edit
INSIDE a rewritten region must stay VISIBLE to the bundle's manifest compare
(strip-at-compare, WP-15's tool, would make it invisible and then silently
overwrite it).
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402
from vco_lib import rewire  # noqa: E402

SCRIPTS = REPO_ROOT / "templates" / "scripts"

MARKER_BEARING = (
    "query_code_graph.py",
    "get_node_info.py",
    "process_documents.py",
    "sync_knowledge_graph.py",
    "analyze_code_graph.py",
    "maintain_knowledge_graph.py",
    "vct_project_config.sh",
    "vct_project_config.ps1",
    "vct_secrets_resolve.sh",
    "vct_secrets_resolve.ps1",
)
# The six that carry a real baked value. The shell pair deliberately does not
# — see `TestTheShellPairBakesNothingOnPurpose`.
BAKING = MARKER_BEARING[:6]
SHELL_PAIR = MARKER_BEARING[6:]


# ══════════════════════════════════════════════════════════════════════
# The pure transform
# ══════════════════════════════════════════════════════════════════════
class TestTransformContract:
    def test_a_file_with_no_region_is_byte_identical(self):
        data = b"#!/usr/bin/env python3\nprint('{{ORCHESTRATOR_ROOT}}')\n"
        assert rewire.rewire_bytes(data, Path("/opt/vco"), filename="x.py") == data

    def test_substitution_is_span_scoped(self):
        src = (
            "OUTSIDE {{ORCHESTRATOR_ROOT}}\n"
            "# VCO-REWIRE-BEGIN: orchestrator-root-resolution\n"
            "INSIDE {{ORCHESTRATOR_ROOT}}\n"
            "# VCO-REWIRE-END: orchestrator-root-resolution\n"
            "ALSO OUTSIDE {{ORCHESTRATOR_ROOT}}\n"
        ).encode("utf-8")
        out = rewire.rewire_bytes(src, Path("/opt/vco"), filename="x.sh")
        text = out.decode("utf-8")
        assert text.splitlines()[0] == "OUTSIDE {{ORCHESTRATOR_ROOT}}"
        assert text.splitlines()[2] == "INSIDE /opt/vco"
        assert text.splitlines()[4] == "ALSO OUTSIDE {{ORCHESTRATOR_ROOT}}"

    def test_an_fstring_brace_pair_outside_the_span_is_untouched(self):
        """The `{{`/`}}` trap: Python f-strings escape braces that way. The
        substitution is token-exact AND span-scoped, so neither can collide."""
        src = (
            'msg = f"{{literal}} {{ORCHESTRATOR_ROOT}} {x}"\n'
            "# VCO-REWIRE-BEGIN: orchestrator-root-resolution\n"
            '_B = "{{ORCHESTRATOR_ROOT}}"\n'
            "# VCO-REWIRE-END: orchestrator-root-resolution\n"
        ).encode("utf-8")
        out = rewire.rewire_bytes(src, Path("/opt/vco"), filename="x.py")
        text = out.decode("utf-8")
        assert text.splitlines()[0] == 'msg = f"{{literal}} {{ORCHESTRATOR_ROOT}} {x}"'
        assert text.splitlines()[2] == '_B = "/opt/vco"'

    def test_crlf_is_preserved(self):
        """Tri-OS: a normalising rewrite would make every Windows checkout
        look user-modified to the manifest hash compare."""
        src = (
            "# VCO-REWIRE-BEGIN: orchestrator-root-resolution\r\n"
            '_B = "{{ORCHESTRATOR_ROOT}}"\r\n'
            "# VCO-REWIRE-END: orchestrator-root-resolution\r\n"
        ).encode("utf-8")
        out = rewire.rewire_bytes(src, Path("/opt/vco"), filename="x.py")
        assert out.count(b"\r\n") == 3
        assert b'_B = "/opt/vco"\r\n' in out

    @pytest.mark.parametrize("body,why", [
        ("# VCO-REWIRE-BEGIN: orchestrator-root-resolution\nx\n", "never closed"),
        ("# VCO-REWIRE-END: orchestrator-root-resolution\n", "no matching"),
        ("# VCO-REWIRE-BEGIN: orchestrator-root-resolution\n"
         "# VCO-REWIRE-BEGIN: orchestrator-root-resolution\n"
         "# VCO-REWIRE-END: orchestrator-root-resolution\n", "still unclosed"),
    ])
    def test_unbalanced_markers_raise_naming_the_file(self, body, why):
        with pytest.raises(ValueError) as exc:
            rewire.rewire_bytes(body.encode("utf-8"), Path("/opt/vco"),
                                filename="offender.py")
        assert "offender.py" in str(exc.value)
        assert why in str(exc.value)

    def test_the_runtime_expansion_placeholder_is_not_escaped(self):
        """`{{VCT_ORCHESTRATOR_ROOT}}` resolves to the LITERAL
        `${VCT_ORCHESTRATOR_ROOT}` for the consumer to expand at use time.
        Escaping its `$` for a shell would turn it into inert text."""
        src = (
            "# VCO-REWIRE-BEGIN: orchestrator-root-resolution\n"
            'R="{{VCT_ORCHESTRATOR_ROOT}}"\n'
            "# VCO-REWIRE-END: orchestrator-root-resolution\n"
        ).encode("utf-8")
        out = rewire.rewire_bytes(src, Path("/opt/vco"), filename="x.sh")
        assert 'R="${VCT_ORCHESTRATOR_ROOT}"' in out.decode("utf-8")

    def test_vocabulary_matches_the_stale_root_heal_map(self):
        """The moved-clone heal (`_stale_orchestrator_root_heal_match`)
        re-derives "what would this file have been under the OLD root?" with
        its own inline map. A placeholder present here but absent there would
        silently defeat that heal, so the key sets must stay equal — and
        `{{PROJECT_ROOT}}` is deliberately excluded from BOTH."""
        import inspect
        heal_src = inspect.getsource(
            project_init._stale_orchestrator_root_heal_match)
        keys = set(rewire.rewire_subs(Path("/opt/vco")))
        heal_keys = {
            k for k in (
                "{{ORCHESTRATOR_ROOT}}", "{{PROJECTS_ROOT}}", "{{HOME}}",
                "{{VCT_ORCHESTRATOR_ROOT}}", "{{PROJECT_ROOT}}",
            )
            if f'"{k}":' in heal_src
        }
        assert keys == heal_keys
        assert "{{PROJECT_ROOT}}" not in keys


class TestWindowsPathEscaping:
    """R12/R14 tri-OS. `C:\\Users\\alice` baked verbatim into a Python
    double-quoted literal is a SyntaxError (`\\U` opens a unicode escape) —
    every shipped Python script would be bricked on Windows."""

    WIN = Path(r"C:\Users\alice\My 'VCO' clone")

    @pytest.mark.parametrize("name", BAKING)
    def test_the_installed_python_still_parses_and_round_trips(self, name):
        out = rewire.rewire_bytes((SCRIPTS / name).read_bytes(), self.WIN,
                                  filename=name)
        text = out.decode("utf-8")
        ast.parse(text)  # SyntaxError here = a bricked Windows install
        line = next(ln for ln in text.splitlines()
                    if "_VCO_BAKED_ORCHESTRATOR_ROOT =" in ln)
        assert ast.literal_eval(line.split("=", 1)[1].strip()) == str(self.WIN)

    def test_powershell_uses_single_quote_doubling(self):
        src = (
            "# VCO-REWIRE-BEGIN: orchestrator-root-resolution\n"
            "$R = '{{ORCHESTRATOR_ROOT}}'\n"
            "# VCO-REWIRE-END: orchestrator-root-resolution\n"
        ).encode("utf-8")
        out = rewire.rewire_bytes(src, Path(r"C:\a'b"), filename="x.ps1")
        # Backslashes are literal inside PS single-quotes; the quote doubles.
        assert "$R = 'C:\\a''b'" in out.decode("utf-8")

    def test_posix_shell_neutralises_expansion_characters(self):
        src = (
            "# VCO-REWIRE-BEGIN: orchestrator-root-resolution\n"
            ': "${VCT_ORCHESTRATOR_ROOT:={{ORCHESTRATOR_ROOT}}}"\n'
            "# VCO-REWIRE-END: orchestrator-root-resolution\n"
        ).encode("utf-8")
        out = rewire.rewire_bytes(src, Path("/opt/$(rm -rf ~)/x"),
                                  filename="x.sh").decode("utf-8")
        assert "\\$(rm -rf ~)" in out


# ══════════════════════════════════════════════════════════════════════
# The ten regions themselves
# ══════════════════════════════════════════════════════════════════════
class TestTheTenRegions:
    def test_detection_is_by_content_and_finds_exactly_the_ten(self):
        found = tuple(sorted(
            p.name for p in SCRIPTS.iterdir()
            if p.is_file() and rewire.has_rewire_region(p.read_bytes())
        ))
        assert found == tuple(sorted(MARKER_BEARING))

    @pytest.mark.parametrize("name", BAKING)
    def test_each_python_region_bakes_the_install_root(self, name):
        out = rewire.rewire_bytes((SCRIPTS / name).read_bytes(),
                                  Path("/opt/vco-clone"), filename=name)
        assert b'_VCO_BAKED_ORCHESTRATOR_ROOT = "/opt/vco-clone"' in out

    @pytest.mark.parametrize("name", BAKING)
    def test_the_placeholder_is_inert_in_the_clone(self, name):
        """Delivery + safety: the SHIPPED bytes must be harmless. The
        `vco_lib/` validation IS the "was it substituted?" guard, so no
        separate check can drift from it."""
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        assert '_VCO_BAKED_ORCHESTRATOR_ROOT = "{{ORCHESTRATOR_ROOT}}"' in text
        assert (Path("{{ORCHESTRATOR_ROOT}}") / "vco_lib").is_dir() is False

    @pytest.mark.parametrize("name", BAKING)
    def test_the_baked_root_is_consulted_last(self, name):
        """A VALID env pin always wins; the baked root is reached only when
        NEITHER `$VCT_ORCHESTRATOR_ROOT` nor `$VCT_INSTALL_ROOT` names a real
        orchestrator root. Both shapes express that, differently:
        `analyze_code_graph.py` retries after the ladder helper has already
        failed; the other five test both pins directly."""
        region = _region_of(SCRIPTS / name)
        assert "_VCO_BAKED_ORCHESTRATOR_ROOT" in region
        if name == "analyze_code_graph.py":
            assert region.count("if not _ensure_vco_lib_on_path():") == 2
        else:
            assert '"VCT_ORCHESTRATOR_ROOT", "VCT_INSTALL_ROOT"' in region
            assert 'if (Path(_VCO_BAKED_ORCHESTRATOR_ROOT) / "vco_lib").is_dir()' in region

    # The four sentences that actually shipped, verbatim (whitespace-flattened).
    RETIRED_GATE_CLAIMS = (
        "The template-drift gate (`scripts/check_template_drift.py`) "
        "enforces that.",
        "Mirrors templates/scripts/vct_project_config.sh — kept in lockstep "
        "by the template-drift gate.",
        "Mirrors `templates/scripts/vct_secrets_resolve.sh` — kept in "
        "lockstep by the template-drift gate.",
        "This file is byte-identical between `templates/scripts/` (shipped "
        "to user projects) and `.claude/scripts/` (orchestrator's own copy).",
    )

    @pytest.mark.parametrize("name", MARKER_BEARING)
    def test_no_region_still_asserts_the_retired_drift_gate(self, name):
        """R23 register item 19. Four regions asserted that the template-drift
        gate `scripts/check_template_drift.py` "enforces" byte-identity with
        the orchestrator's own `.claude/scripts/` copy. That gate was removed
        in PR-39 / v0.2.12 and this repo tracks no `.claude/scripts/` at all,
        so the claim had been false for eighteen releases.

        Naming the gate is still allowed — the history is worth keeping — but
        only alongside the fact that it is gone."""
        flat = _flat_prose(_region_of(SCRIPTS / name))
        for claim in self.RETIRED_GATE_CLAIMS:
            assert claim not in flat, f"{name}: still asserts {claim!r}"
        if "check_template_drift" in flat or "template-drift gate" in flat:
            assert "REMOVED in PR-39 / v0.2.12" in flat, (
                f"{name}: names the retired gate without saying it is gone"
            )


class TestTheShellPairBakesNothingOnPurpose:
    """DEVIATION FROM THE WP-16 SPEC, asserted so it cannot be lost.

    The brief called for `: "${VCT_ORCHESTRATOR_ROOT:={{ORCHESTRATOR_ROOT}}}"`
    in these four regions "so no marker promises a no-op". Not done, for two
    reasons that outrank it:

      * these scripts resolve NOTHING from the orchestrator clone — the hub
        owns every lookup and is found via `$VCT_HUB_PORT` /
        `${VCT_STATE_DIR:-$HOME/.vct}/hub.{port,token}` — so a baked root
        would be a variable nothing reads, which is the exact R16/R24 defect
        one layer down ("a knob with a reader but no effect");
      * `tests/test_v0292_cli_root_resolution_and_prefix.py::
        TestTheShellPairDecision` (another lane's file) asserts the ABSENCE of
        any orchestrator-root ladder in this pair, with a correct rationale.

    The marker still has a live consumer: `rewire_bytes` walks these files and
    returns them byte-identical because they contain no placeholder — an
    honest no-op, not an unimplemented promise.
    """

    @pytest.mark.parametrize("name", SHELL_PAIR)
    def test_transform_is_a_byte_identical_no_op(self, name):
        raw = (SCRIPTS / name).read_bytes()
        assert rewire.has_rewire_region(raw)
        assert rewire.rewire_bytes(raw, Path("/opt/vco"), filename=name) == raw

    @pytest.mark.parametrize("name", SHELL_PAIR)
    def test_region_says_so_rather_than_leaving_it_unexplained(self, name):
        region = _region_of(SCRIPTS / name)
        assert "NOTHING IS BAKED HERE" in region
        assert "rewire.py" in region


def _region_of(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    begin = next(i for i, ln in enumerate(lines) if rewire.REWIRE_BEGIN in ln)
    end = next(i for i, ln in enumerate(lines) if rewire.REWIRE_END in ln)
    return "\n".join(lines[begin:end + 1])


def _flat_prose(region: str) -> str:
    """Region text as one line of prose: comment markers dropped, whitespace
    collapsed, so a claim can be matched regardless of where it wrapped."""
    words = []
    for line in region.splitlines():
        words.extend(line.strip().lstrip("#").split())
    return " ".join(words)


# ══════════════════════════════════════════════════════════════════════
# Delivery: through the real bundle engine
# ══════════════════════════════════════════════════════════════════════
def _fake_orch(root: Path) -> None:
    """A minimal orchestrator clone carrying the REAL marker-bearing scripts.

    `vco_lib/` and `claude_mcp_servers/scripts/kg_access.py` exist so the
    scripts' own validation arms can succeed against this root.
    """
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    (root / "vco_lib").mkdir(parents=True, exist_ok=True)
    (root / "vco_lib" / "__init__.py").write_text("", encoding="utf-8")
    kg = root / "claude_mcp_servers" / "scripts"
    kg.mkdir(parents=True, exist_ok=True)
    (kg / "kg_access.py").write_text(
        "def kg_collections_to_search(self_kg, shared_kg='', development='',\n"
        "                             include_dev=False):\n"
        "    return ['FROM_REAL_KG_ACCESS']\n",
        encoding="utf-8",
    )
    dst = root / "templates" / "scripts"
    dst.mkdir(parents=True, exist_ok=True)
    for name in MARKER_BEARING:
        (dst / name).write_bytes((SCRIPTS / name).read_bytes())


def _manifest(project: Path) -> dict:
    return json.loads(
        (project / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8"))


def _installed(project: Path, name: str) -> Path:
    return project / ".claude" / "scripts" / name


class TestDeliveryThroughTheBundleEngine:
    def test_fresh_install_lands_the_absolute_root(self, tmp_path):
        """Axis 1 — FRESH. And R17.4: the baked path is the INSTALL's own,
        computed at install time, never a build-machine path — the clone here
        sits at a deliberately odd location."""
        orch = tmp_path / "somewhere-odd" / "vco-clone"
        orch.mkdir(parents=True)
        _fake_orch(orch)
        project = tmp_path / "proj"
        project.mkdir()
        project_init.install_project_bundle(
            project, orchestrator_root=orch, update_mode=False)
        text = _installed(project, "get_node_info.py").read_text(encoding="utf-8")
        assert f'_VCO_BAKED_ORCHESTRATOR_ROOT = "{orch}"' in text

    def test_the_manifest_hash_is_the_post_transform_hash(self, tmp_path):
        """THE manifest interaction. If the manifest recorded the RAW template
        hash, every rewritten file would read as user-modified on the next
        update — the same defect WP-15 fixes from the other side."""
        from vco_lib.hashing import sha256_file
        orch = tmp_path / "orch"
        orch.mkdir()
        _fake_orch(orch)
        project = tmp_path / "proj"
        project.mkdir()
        project_init.install_project_bundle(
            project, orchestrator_root=orch, update_mode=False)
        rel = str(Path(".claude") / "scripts" / "get_node_info.py")
        recorded = _manifest(project)["files"][rel]["sha256"]
        assert recorded == sha256_file(_installed(project, "get_node_info.py"))

    def test_update_of_an_untouched_install_is_a_noop(self, tmp_path):
        """Axis 2 — UPDATE."""
        orch = tmp_path / "orch"
        orch.mkdir()
        _fake_orch(orch)
        project = tmp_path / "proj"
        project.mkdir()
        project_init.install_project_bundle(
            project, orchestrator_root=orch, update_mode=False)
        res = project_init.install_project_bundle(
            project, orchestrator_root=orch, update_mode=True)
        rel = str(Path(".claude") / "scripts" / "get_node_info.py")
        assert rel in res["actions"]["noop"]
        for bad in ("preserve", "adopt", "overwrite"):
            assert rel not in res["actions"].get(bad, [])

    def test_a_moved_clone_heals_to_the_new_root(self, tmp_path):
        """Axis 3 — ALREADY-DAMAGED, the moved-clone half. The manifest
        records the post-transform hash, so an untouched installed script
        matches `prior_hash` and classifies `overwrite` — no
        `bundle_user_modified_preserved`, no manual step."""
        old = tmp_path / "old" / "vco-clone"
        new = tmp_path / "new" / "vco-clone"
        for root in (old, new):
            root.mkdir(parents=True)
            _fake_orch(root)
        project = tmp_path / "proj"
        project.mkdir()
        project_init.install_project_bundle(
            project, orchestrator_root=old, update_mode=False)
        assert str(old) in _installed(project, "get_node_info.py").read_text(
            encoding="utf-8")
        res = project_init.install_project_bundle(
            project, orchestrator_root=new, update_mode=True)
        rel = str(Path(".claude") / "scripts" / "get_node_info.py")
        assert rel in res["actions"]["overwrite"]
        healed = _installed(project, "get_node_info.py").read_text(encoding="utf-8")
        assert str(new) in healed and str(old) not in healed

    def test_a_user_edit_inside_the_region_is_still_seen(self, tmp_path):
        """LEAVE-ALONE, and the WP-15/WP-16 split enforced: strip-at-compare
        would classify this `noop` and silently drop the user's line. The
        engine must classify it as a divergence.

        For `.claude/scripts/**` the v0.2.84 D7 terminal outcome is `adopt`
        (back up the user's bytes under `.claude/backups/bundle-adoptions/`,
        then write the shipped bytes) — NOT `preserve`. Asserted here because
        the WP-16 brief still described the pre-v0.2.84 `preserve` behaviour.
        """
        orch = tmp_path / "orch"
        orch.mkdir()
        _fake_orch(orch)
        project = tmp_path / "proj"
        project.mkdir()
        project_init.install_project_bundle(
            project, orchestrator_root=orch, update_mode=False)
        target = _installed(project, "get_node_info.py")
        edited = target.read_text(encoding="utf-8").replace(
            "    _kg_access_dir = (",
            "    _user_line = 1  # my edit inside the region\n    _kg_access_dir = (",
        )
        target.write_text(edited, encoding="utf-8")
        res = project_init.install_project_bundle(
            project, orchestrator_root=orch, update_mode=True)
        rel = str(Path(".claude") / "scripts" / "get_node_info.py")
        assert rel not in res["actions"]["noop"]
        assert rel in res["actions"]["adopt"]
        backups = list(
            (project / ".claude" / "backups" / "bundle-adoptions").rglob(
                "get_node_info.py"))
        assert backups, "the user's bytes must be captured before adoption"
        assert "_user_line = 1" in backups[0].read_text(encoding="utf-8")

    def test_every_marker_bearing_script_matches_a_bundle_glob(self):
        """Delivery check 1, VERIFIED rather than assumed."""
        import fnmatch
        from vco_lib.bundle_globs import script_patterns
        pats = script_patterns()
        for name in MARKER_BEARING:
            assert any(fnmatch.fnmatch(name, p) for p in pats), name

    def test_only_marker_bearing_scripts_get_the_transform(self, tmp_path):
        """One engine, no hand-kept list: the enumeration attaches the
        rewriter by CONTENT, and nothing else in the bundle gains it."""
        orch = tmp_path / "orch"
        orch.mkdir()
        _fake_orch(orch)
        # A byte-copy script with no sentinel.
        (orch / "templates" / "scripts" / "plain_helper.py").write_text(
            "print('{{ORCHESTRATOR_ROOT}}')\n", encoding="utf-8")
        ops = project_init._enumerate_bundle_files(orch, tmp_path / "proj")
        by_name = {Path(op.dest_rel).name: op for op in ops
                   if "/scripts/" in op.dest_rel.replace("\\", "/")}
        for name in MARKER_BEARING:
            assert by_name[name].transform is not None, name
        assert by_name["plain_helper.py"].transform is None

    def test_root_and_projects_go_through_the_same_engine(self, tmp_path):
        """Delivery check 2 — no `if folder == orchestrator_root` fork here:
        the root bakes its OWN path through the identical enumeration."""
        orch = tmp_path / "orch"
        orch.mkdir()
        _fake_orch(orch)
        project_init.install_project_bundle(
            orch, orchestrator_root=orch, update_mode=False)
        assert f'_VCO_BAKED_ORCHESTRATOR_ROOT = "{orch}"' in _installed(
            orch, "get_node_info.py").read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════
# The user-visible benefit, at runtime
# ══════════════════════════════════════════════════════════════════════
_RUNTIME_DRIVER = textwrap.dedent(
    '''
    """Execute the installed script's prologue up to the end of its
    VCO-REWIRE region and report whether `kg_access` became importable.

    Stubs only the third-party imports the prologue performs, so the ladder
    and the region under test are the REAL shipped bytes.
    """
    import ast, sys, types
    from pathlib import Path

    for name in ("weaviate", "weaviate.classes", "weaviate.classes.query"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["weaviate.classes.query"].Filter = object

    script = Path(sys.argv[1])
    text = script.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    end_line = 1 + next(
        i for i, ln in enumerate(lines)
        if "VCO-REWIRE-END: orchestrator-root-resolution" in ln
    )
    # Slice on TOP-LEVEL STATEMENT boundaries, not raw line offsets: the
    # region lives inside a try/except, and cutting mid-block is a
    # SyntaxError. Keep whole statements up to and including the one that
    # contains the region's end, so the `from kg_access import ...` that
    # follows it (and its except arm) are both present.
    tree = ast.parse(text)
    stop = None
    for node in tree.body:
        stop = node.end_lineno or node.lineno
        if node.lineno <= end_line <= (node.end_lineno or node.lineno):
            break
    prologue = "".join(lines[:stop])
    ns = {"__file__": str(script), "__name__": "__vco_probe__"}
    exec(compile(prologue, str(script), "exec"), ns)
    mod = sys.modules.get("kg_access")
    print("KG_ACCESS_IMPORTED" if mod is not None else "FALLBACK")
    print("SOURCE=%s" % (getattr(mod, "__file__", "") or ""))
    '''
)


class TestTheRuntimeBenefit:
    """The point of the whole package: with NOTHING in the environment and a
    neutral cwd, an installed script still reaches its orchestrator clone."""

    def _install(self, tmp_path: Path) -> tuple:
        orch = tmp_path / "orch"
        orch.mkdir()
        _fake_orch(orch)
        project = tmp_path / "proj"
        project.mkdir()
        project_init.install_project_bundle(
            project, orchestrator_root=orch, update_mode=False)
        driver = tmp_path / "driver.py"
        driver.write_text(_RUNTIME_DRIVER, encoding="utf-8")
        neutral = tmp_path / "neutral-cwd"
        neutral.mkdir()
        return orch, project, driver, neutral

    def _run(self, driver, script, cwd, env_root=None):
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("VCT_ORCHESTRATOR_ROOT", "VCT_INSTALL_ROOT",
                         "PYTHONPATH")
        }
        if env_root is not None:
            env["VCT_ORCHESTRATOR_ROOT"] = str(env_root)
        return subprocess.run(
            [sys.executable, str(driver), str(script)],
            cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120,
        )

    def test_resolves_kg_access_with_the_env_unset(self, tmp_path):
        """RED against today's script: with no env and a cwd outside both
        trees, rung 3 (`<script>/../..`) is the USER PROJECT root, which has
        no `claude_mcp_servers/`, so the import falls back and the access
        matrix is silently lost."""
        _orch, project, driver, neutral = self._install(tmp_path)
        res = self._run(driver, _installed(project, "get_node_info.py"), neutral)
        assert res.returncode == 0, res.stderr
        assert "KG_ACCESS_IMPORTED" in res.stdout, res.stdout + res.stderr

    def test_an_explicit_env_root_still_wins(self, tmp_path):
        """LEAVE-ALONE: the baked value only FILLS an unset variable."""
        orch, project, driver, neutral = self._install(tmp_path)
        res = self._run(driver, _installed(project, "get_node_info.py"),
                        neutral, env_root=orch)
        assert res.returncode == 0, res.stderr
        assert "KG_ACCESS_IMPORTED" in res.stdout

    def test_a_provably_stale_env_pin_is_healed(self, tmp_path):
        """The already-damaged shape this package MUST cover, and the reason
        the guard is "neither pin validates" rather than "the pin is unset":
        after a clone move, `.claude/env` still exports the OLD path. The
        shared resolver correctly SKIPS the invalid rung — and then lands on
        the user project root, losing the access matrix silently. With the
        bake, the install's own root answers instead.

        This test was written expecting the narrower "unset only" guard, went
        red, and the GUARD was widened — the narrow version would have left
        every moved-clone user exactly as broken as before."""
        _orch, project, driver, neutral = self._install(tmp_path)
        res = self._run(driver, _installed(project, "get_node_info.py"),
                        neutral, env_root=tmp_path / "does-not-exist")
        assert res.returncode == 0, res.stderr
        assert "KG_ACCESS_IMPORTED" in res.stdout, res.stdout

    def test_a_valid_env_pin_to_a_different_clone_still_wins(self, tmp_path):
        """LEAVE-ALONE: healing is scoped to a pin that PROVABLY does not
        resolve. A valid operator pin is never second-guessed."""
        _orch, project, driver, neutral = self._install(tmp_path)
        other = tmp_path / "other-clone"
        other.mkdir()
        _fake_orch(other)
        res = self._run(driver, _installed(project, "get_node_info.py"),
                        neutral, env_root=other)
        assert res.returncode == 0, res.stderr
        assert "KG_ACCESS_IMPORTED" in res.stdout
        # The module must come from the PINNED clone, not the baked one.
        source = next(ln.split("=", 1)[1] for ln in res.stdout.splitlines()
                      if ln.startswith("SOURCE="))
        assert source.startswith(str(other)), source


class TestTheRatchetHeld:
    def test_analyze_code_graph_did_not_grow(self):
        """The region edit REPLACED comment lines rather than adding to a
        ~7,226-line monolith; `tests/test_analyze_code_graph_ratchet.py` also
        fails if this drifts far below the pin.

        v0.2.94: re-pinned DOWNWARD 7227 -> 7226. Routing `connect()` and the
        migrate helper through `vco_lib.weaviate_helpers` (instead of two
        hand-rolled `connect_to_custom` calls that hardcoded localhost:8081)
        was net-negative even after adding the guarded create. Both ratchets
        move together, and only ever down.
        """
        n = len((SCRIPTS / "analyze_code_graph.py").read_text(
            encoding="utf-8").splitlines())
        assert n == 7226, n

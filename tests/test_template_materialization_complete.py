# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-18 — the RENDER-EVERY-TEMPLATE gate, plus the owner rules.

What ships is enumerated by CALLING THE SHIPPING CODE, never by globbing
``templates/``:

* the bundle enumerator (``project_init._enumerate_bundle_files``) — every op
  whose transform is a :class:`vco_lib.materialize.Transform` is re-rendered
  through ITS OWN spec under a synthetic install;
* the project-level templates (``project_init._PROJECT_LEVEL_TEMPLATES``),
  through the one pipeline both CLAUDE.md callers use;
* the orchestrator-root table (``rendered_root_files.entries()``);
* the boot units — the real ``register_linux`` / ``register_macos`` /
  ``register_windows`` against the real specs, with no init system reachable;
* the secrets bootstrap readme (``secrets_bootstrap.materialize_shared_readme``).

Synthetic installs: a POSIX-shaped root and a Windows-shaped root, both with a
path containing ``&``, a space and a quote. Asserted: no ``{{UPPER_SNAKE}}``
and no vct-hub ``__TOKEN__`` (the Rust renderer's table,
``tests/fixtures/hub_unit_placeholders.json``) survives; YAML frontmatter, plists and Task XML still
parse; every rendered path placeholder points at an existing path in the
synthetic install; every registry key is used.

This gate is STRICT by owner decision (2026-09-30): the field behaviour for an
unfillable placeholder is warn + write + deferral row (tested below as the
"act" and "leave-alone" halves), but no SHIPPED template may rely on it.
"""
from __future__ import annotations

import contextlib
import io
import json
import plistlib
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import boot_service, materialize, project_init  # noqa: E402
from vco_lib import rendered_root_files as rrf  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402
from vco_lib.project_templates import render_project_template  # noqa: E402
from tests._materialize_fixtures import mirror_repo_tree  # noqa: E402

#: Path-vocabulary keys that NO shipped template uses today. They are part of
#: the documented contract (templates/README.md) and of the moved-clone heal's
#: round-trip vocabulary, so they are kept — removing a documented capability is
#: an owner decision, not a gate side effect. The set is pinned EXACTLY, so a
#: NEW dead key still fails the gate and one of these gaining a user shrinks it.
_DOCUMENTED_BUT_UNUSED = frozenset({"HOME", "PROJECTS_ROOT", "VCT_ORCHESTRATOR_ROOT"})


@dataclass
class Synthetic:
    os_name: str
    root: Path
    project: Path
    home: Path
    state: Path

    @property
    def context(self) -> materialize.MaterializeContext:
        return materialize.MaterializeContext(
            self.root, self.project, project_name="Synthetic & 'Co'",
            os_name=self.os_name, home=self.home,
        )


def _make(tmp: Path, os_name: str) -> Synthetic:
    if os_name == "Windows":
        # A Windows-shaped root on any runner: one directory NAME carrying the
        # backslashes, so escaping of `\` is exercised end to end.
        root = tmp / "C:\\Users\\Ann & Bob's\\vco install"
    else:
        root = tmp / "Ann & Bob's: #1" / "vco install"
    project = tmp / "my proj & 'x'"
    home = tmp / "home dir"
    state = tmp / "state & dir"
    for d in (root, project, home, state, root / "infrastructure", root / "scripts"):
        d.mkdir(parents=True, exist_ok=True)
    # Review R18-03: composite paths (`{{ORCHESTRATOR_ROOT}}/tools/…`) are
    # checked, so the synthetic root is a real copy of the tree's layout.
    mirror_repo_tree(root)
    py = materialize.venv_python_path(root, os_name=os_name)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", encoding="utf-8")
    for name in ("launch-claude-mcp-stack.sh", "launch-claude-mcp-stack.ps1"):
        (root / "scripts" / name).write_text("", encoding="utf-8")
    return Synthetic(os_name, root, project, home, state)


@pytest.fixture(params=["Linux", "Windows"])
def synthetic(request, tmp_path) -> Synthetic:
    return _make(tmp_path, request.param)


# ---------------------------------------------------------------------------
# Enumeration through the shipping code
# ---------------------------------------------------------------------------

def _bundle_ops(monkeypatch) -> list:
    """Every op the bundle ships, gateway agents included (the gate is forced
    to DELIVER so the gated class is covered too)."""
    from vco_lib import module_gated_delivery as mgd

    monkeypatch.setattr(
        mgd, "gateway_agents_gate",
        lambda *_a, **_k: mgd.GateVerdict(mgd.GateState.DELIVER, "test", "forced"),
    )
    return project_init._enumerate_bundle_files(REPO_ROOT, project_root=None)


def _region_text(text: str) -> str:
    from vco_lib.rewire import _region_line_spans

    lines = text.splitlines(keepends=True)
    return "".join("".join(lines[a:b + 1]) for a, b in _region_line_spans(lines, "x"))


def _hub_tokens() -> Tuple["re.Pattern[str]", frozenset]:
    """The vct-hub (R7, Rust) token grammar and key set — read from its ONE
    table, never restated here (WP-18B, ``hub_unit_placeholders.json``)."""
    table = json.loads((REPO_ROOT / "tests" / "fixtures" / "hub_unit_placeholders.json")
                       .read_text(encoding="utf-8"))
    keys = frozenset(k for row in table["templates"].values() for k in row)
    return re.compile(table["token_regex"]), keys


_HUB_RE, _HUB_KEYS = _hub_tokens()


def _tokens(text: str) -> List[str]:
    return (materialize.PLACEHOLDER_RE.findall(text)
            + [t for t in _HUB_RE.findall(text) if t in _HUB_KEYS])


def _frontmatter(text: str):
    lines = text.splitlines(keepends=True)
    first, last = materialize._frontmatter_line_span(lines)
    if last < first:
        return None
    return yaml.safe_load("".join(lines[first:last + 1]))


Rendered = Dict[str, Tuple[str, materialize.RenderResult]]


def _render_bundle(syn: Synthetic, monkeypatch) -> Rendered:
    out: Rendered = {}
    ctx = materialize.LazyContext(syn.context)
    for op in _bundle_ops(monkeypatch):
        if not isinstance(op.transform, materialize.Transform):
            continue
        data, result = op.transform.render(op.source_abs.read_bytes(), ctx)
        out[op.dest_rel] = (data.decode("utf-8"), result)
    return out


def _render_project_templates(syn: Synthetic) -> Rendered:
    out: Rendered = {}
    templates = REPO_ROOT / "templates"
    for active in (set(), set(project_init._DEFAULT_ACTIVE_MODULES) | {"diagrams"}):
        for template_name, live_rel, _ref in project_init._PROJECT_LEVEL_TEMPLATES:
            sink = materialize.FindingsSink()
            text = render_project_template(
                (templates / template_name).read_text(encoding="utf-8"),
                label=str(live_rel), folder=syn.project, orchestrator_root=syn.root,
                project_name="Synthetic", active_modules=active, sink=sink,
                context=syn.context,
            )
            out[f"{live_rel} (modules={sorted(active)})"] = (text, sink.results[str(live_rel)])
    return out


def _render_root_table(syn: Synthetic) -> Rendered:
    out: Rendered = {}
    for entry in rrf.entries():
        result = rrf._render_template_text(
            syn.root, entry, (REPO_ROOT / entry.template).read_text(encoding="utf-8"),
            context=materialize.MaterializeContext(
                syn.root, syn.root, os_name=syn.os_name, home=syn.home),
        )
        out[entry.path] = (result.text, result)
    return out


def _render_boot_units(syn: Synthetic, monkeypatch) -> Rendered:
    """The REAL register_* functions, init systems unreachable.

    The unit specs format paths for their OS themselves (Windows: forward
    slashes via ``_windows_forward``), so a Windows-shaped root simulated on a
    POSIX runner (backslashes inside ONE directory name) cannot exist after
    that conversion. Units are therefore rendered for all three OSes from a
    POSIX-shaped root that still carries ``&``, a space and a quote; the
    Windows-shaped root covers the bundle, project and root-table renders."""
    if "\\" in str(syn.root) and sys.platform != "win32":
        syn = _make(syn.root.parent / "units", "Linux")
    spied: List[materialize.RenderResult] = []
    real_render = materialize.render

    def _spy(*a, **k):
        result = real_render(*a, **k)
        spied.append(result)
        return result

    monkeypatch.setattr(materialize, "render", _spy)
    monkeypatch.setattr(boot_service.shutil, "which", lambda _name: None)
    out: Rendered = {}
    for os_key in ("Linux", "Darwin", "Windows"):
        specs = [
            boot_service.container_stack_spec(
                syn.root, syn.root / "infrastructure", os_key=os_key),
            boot_service.model_gateway_spec(
                os_key=os_key,
                exec_argv=[str(syn.root / ".venv" / "bin" / "vct-model-gateway")],
                state_dir=syn.state, secret_project="",
            ),
        ]
        for spec in specs:
            spec = boot_service.replace(spec, deferral_folder=None)
            if os_key == "Linux":
                tpl = boot_service.read_template(REPO_ROOT, spec.template_linux)
                boot_service.register_linux(spec, tpl, home=syn.home)
                target = boot_service.systemd_unit_path(spec, syn.home)
            elif os_key == "Darwin":
                tpl = boot_service.read_template(REPO_ROOT, spec.template_macos)
                boot_service.register_macos(spec, tpl, home=syn.home)
                target = boot_service.launchd_plist_path(spec, syn.home)
            else:
                tpl = boot_service.read_template(REPO_ROOT, spec.template_windows)
                boot_service.register_windows(spec, tpl)
                target = spec.windows_task_xml_path
            assert tpl is not None and target is not None
            out[f"{os_key}:{spec.service_id}"] = (target.read_text(encoding="utf-8"), spied[-1])
    return out


def _render_everything(syn: Synthetic, monkeypatch) -> Rendered:
    out: Rendered = {}
    out.update(_render_bundle(syn, monkeypatch))
    out.update(_render_project_templates(syn))
    out.update(_render_root_table(syn))
    out.update(_render_boot_units(syn, monkeypatch))
    return out


def _violations(rendered: Rendered) -> List[str]:
    """The gate's one verdict function (the mutation proof feeds it too)."""
    bad: List[str] = []
    for label, (text, result) in rendered.items():
        for u in result.unresolved:
            bad.append(f"{label}: {{{{{u.name}}}}} line {u.line} ({u.reason})")
        for m in result.missing_paths:
            bad.append(f"{label}: {{{{{m.name}}}}} -> missing path {m.value!r}")
        region_only = label.startswith(".claude/scripts/")
        scanned = _region_text(text) if region_only else text
        for tok in _tokens(scanned):
            bad.append(f"{label}: surviving token {tok!r}")
    return bad


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

class TestEveryShippedTemplateRenders:
    def test_enumeration_is_not_vacuous(self, synthetic, monkeypatch):
        rendered = _render_bundle(synthetic, monkeypatch)
        assert any(k.startswith(".claude/agents/") for k in rendered)
        assert any(k.startswith(".claude/skills/") for k in rendered)
        assert sum(k.startswith(".claude/scripts/") for k in rendered) >= 10
        assert len(_render_boot_units(synthetic, monkeypatch)) == 6

    def test_no_placeholder_survives_and_no_path_is_missing(self, synthetic, monkeypatch):
        rendered = _render_everything(synthetic, monkeypatch)
        assert _violations(rendered) == []

    def test_frontmatter_plist_and_xml_still_parse(self, synthetic, monkeypatch):
        """Rendering must never BREAK a document. For frontmatter the claim is
        relative to the shipped source: a source whose own frontmatter is not
        valid YAML is a template defect this gate reports, not a render one."""
        sources = {op.dest_rel: op.source_abs.read_text(encoding="utf-8")
                   for op in _bundle_ops(monkeypatch)
                   if isinstance(op.transform, materialize.Transform)}
        rendered = _render_everything(synthetic, monkeypatch)
        parsed = 0
        for label, (text, _r) in rendered.items():
            if label.endswith(".md") and text.startswith("---"):
                try:
                    _frontmatter(sources.get(label, text))
                except yaml.YAMLError:
                    continue  # pre-existing source defect (see the report)
                assert isinstance(_frontmatter(text), dict), label
                parsed += 1
            elif label.startswith("Darwin:"):
                plistlib.loads(text.encode("utf-8"))
                parsed += 1
            elif label.startswith("Windows:"):
                ET.fromstring(text.encode("utf-8"))
                parsed += 1
        assert parsed > 50

    def test_rendered_values_are_this_installs(self, synthetic, monkeypatch):
        rendered = _render_root_table(synthetic)
        (text, _r), = rendered.values()
        assert str(synthetic.root) in text
        py = materialize.venv_python_path(synthetic.root, os_name=synthetic.os_name)
        assert str(py) in text and py.exists()
        if synthetic.os_name == "Windows":
            assert py.parts[-2:] == ("Scripts", "python.exe")
        assert str(synthetic.root / "claude_mcp_servers" / ".venv") not in text

    def test_every_registry_key_is_used(self, tmp_path, monkeypatch):
        used: set = set()
        for os_name in ("Linux", "Windows"):
            syn = _make(tmp_path / os_name, os_name)
            for _text, result in _render_everything(syn, monkeypatch).values():
                used |= result.used
        unused = set(materialize.REGISTRY) - used
        assert unused == set(_DOCUMENTED_BUT_UNUSED), (
            "registry keys no shipped template uses (a dead key is drift; a "
            f"key that gained a user must leave the exception set): {sorted(unused)}"
        )

    def test_secrets_bootstrap_readme_is_verbatim_and_token_free(self, tmp_path, monkeypatch):
        from vco_lib import secrets_bootstrap

        monkeypatch.setenv("VCT_SECRETS_DIR", str(tmp_path / "secrets"))
        res = secrets_bootstrap.materialize_shared_readme(REPO_ROOT)
        assert res.status in ("written", "created", "ok", "materialized"), res
        written = tmp_path / "secrets" / "shared" / "_README.md"
        assert _tokens(written.read_text(encoding="utf-8")) == []


class TestMutationProof:
    """RED proof of the gate itself: a template copy carrying ``{{BOGUS}}``,
    rendered through the very spec that ships it, is a violation."""

    def test_a_bogus_placeholder_turns_the_gate_red(self, tmp_path, monkeypatch):
        syn = _make(tmp_path, "Linux")
        op = next(o for o in _bundle_ops(monkeypatch)
                  if isinstance(o.transform, materialize.Transform)
                  and o.dest_rel.endswith("coder.md"))
        mutated = op.source_abs.read_bytes() + b"\nSee {{BOGUS}}.\n"
        data, result = op.transform.render(mutated, materialize.LazyContext(syn.context))
        violations = _violations({op.dest_rel: (data.decode("utf-8"), result)})
        assert any("BOGUS" in v for v in violations), violations
        assert b"{{BOGUS}}" in data, "owner rule: the token is left in place"


# ---------------------------------------------------------------------------
# Renderer unit contract
# ---------------------------------------------------------------------------

class TestRenderContract:
    def test_token_exact(self):
        ctx = {"ORCHESTRATOR_ROOT": "/r"}
        text = ("{{ORCHESTRATOR_ROOT}} {{first_name}} ${{ secrets.X }} ${{VAR}} "
                "{{#if_module_active diagrams}}x{{/if_module_active}}")
        res = materialize.render(text, ctx, allowed={"ORCHESTRATOR_ROOT"}, escape="none")
        assert res.text.startswith("/r {{first_name}} ${{ secrets.X }} ${{VAR}} {{#if")
        assert res.unresolved == ()

    @pytest.mark.parametrize("escape,expected", [
        ("py", 'C:\\\\a \\"b\\"'),
        ("ps1", "C:\\a \"b\""),
        ("sh", 'C:\\\\a \\"b\\"'),
        ("xml", 'C:\\a "b"'),
    ])
    def test_escapes(self, escape, expected):
        res = materialize.render("{{ORCHESTRATOR_ROOT}}", {"ORCHESTRATOR_ROOT": 'C:\\a "b"'},
                                 allowed={"ORCHESTRATOR_ROOT"}, escape=escape)
        assert res.text == expected

    def test_xml_escapes_ampersand_once(self):
        res = materialize.render("<s>{{WORKING_DIR}}</s>", {"WORKING_DIR": "/a & <b>"},
                                 allowed={"WORKING_DIR"}, escape="xml")
        assert res.text == "<s>/a &amp; &lt;b&gt;</s>"

    def test_yaml_quotes_a_scalar_the_value_would_break(self):
        text = "---\ncommand: {{ORCHESTRATOR_ROOT}}/bin/x\n---\nBody {{ORCHESTRATOR_ROOT}}\n"
        res = materialize.render(text, {"ORCHESTRATOR_ROOT": "/a: #b 'c'"},
                                 allowed={"ORCHESTRATOR_ROOT"}, escape="yaml")
        assert _frontmatter(res.text) == {"command": "/a: #b 'c'/bin/x"}
        assert res.text.endswith("Body /a: #b 'c'\n"), "the body is prose — unescaped"

    def test_windows_venv_python_is_scripts_python_exe(self, tmp_path):
        assert materialize.venv_python_path(tmp_path, os_name="Windows") == (
            tmp_path / ".venv" / "Scripts" / "python.exe")
        assert materialize.venv_python_path(tmp_path, os_name="Linux") == (
            tmp_path / ".venv" / "bin" / "python")

    def test_one_path_vocabulary(self, tmp_path):
        from vco_lib import rewire

        assert (project_init._agent_subs(tmp_path, tmp_path / "p")
                == rewire.rewire_subs(tmp_path, tmp_path / "p")
                == materialize.path_subs(tmp_path, tmp_path / "p"))


# ---------------------------------------------------------------------------
# Owner rules 1 + 2 — act and leave-alone
# ---------------------------------------------------------------------------

class TestOwnerRules:
    def _result(self, text: str, ctx: dict) -> materialize.RenderResult:
        return materialize.render(text, ctx, allowed=materialize.GLOBAL_KEYS, escape="none")

    def test_unknown_placeholder_warns_writes_and_records(self, tmp_path):
        folder = tmp_path / "proj"
        folder.mkdir()
        result = self._result("a {{ORCHESTRATOR_ROOT}}\nb {{NOPE}}\n",
                              {"ORCHESTRATOR_ROOT": str(folder)})
        assert "{{NOPE}}" in result.text and str(folder) in result.text
        sink = materialize.FindingsSink(rerender_command="python install.py --update")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sink.record(".claude/agents/x.md", result)
            sink.record(".claude/agents/x.md", result)  # re-render: no second warning
        assert err.getvalue().count("NOPE") == 1
        summary = sink.settle(folder)
        cid = materialize.unrendered_condition_id(".claude/agents/x.md")
        assert summary["emitted"] == [cid]
        entry = DeferralReport.read(folder).entry_for(cid)
        assert entry is not None and entry.resolved_disposition == "action_required"
        assert "{{NOPE}} on line 2" in entry.detected
        assert entry.dismiss_fields == {"file": ".claude/agents/x.md", "placeholders": "NOPE"}

    def test_missing_path_warns_and_records_the_sibling_row(self, tmp_path):
        folder = tmp_path / "proj"
        folder.mkdir()
        gone = str(tmp_path / "no-such-venv" / "python")
        result = self._result("py: {{VENV_PYTHON}}\n", {"VENV_PYTHON": gone})
        assert result.missing_paths == (materialize.MissingPath("VENV_PYTHON", gone, 1),)
        with contextlib.redirect_stderr(io.StringIO()):
            materialize.settle_deferrals(folder, {"CLAUDE.md": result})
        cid = materialize.path_missing_condition_id("CLAUDE.md")
        assert DeferralReport.read(folder).has_condition(cid)
        assert gone in DeferralReport.read(folder).entry_for(cid).detected

    def test_clean_rerender_clears_the_rows(self, tmp_path):
        folder = tmp_path / "proj"
        folder.mkdir()
        bad = self._result("{{NOPE}} {{VENV_PYTHON}}", {"VENV_PYTHON": "/nope"})
        materialize.settle_deferrals(folder, {"f.md": bad})
        report = DeferralReport.read(folder)
        assert report.has_condition(materialize.unrendered_condition_id("f.md"))
        assert report.has_condition(materialize.path_missing_condition_id("f.md"))
        good = self._result("{{ORCHESTRATOR_ROOT}}", {"ORCHESTRATOR_ROOT": str(folder)})
        summary = materialize.settle_deferrals(folder, {"f.md": good})
        assert sorted(summary["resolved"]) == sorted([
            materialize.unrendered_condition_id("f.md"),
            materialize.path_missing_condition_id("f.md")])
        assert not DeferralReport.read(folder).entries

    def test_leave_alone_clean_render_touches_nothing(self, tmp_path):
        """A clean render on a folder with no ledger creates no ledger, no lock."""
        folder = tmp_path / "proj"
        folder.mkdir()
        good = self._result("{{ORCHESTRATOR_ROOT}}", {"ORCHESTRATOR_ROOT": str(folder)})
        materialize.settle_deferrals(folder, {"f.md": good})
        assert not (folder / ".claude").exists()

    def test_leave_alone_other_files_rows_survive(self, tmp_path):
        folder = tmp_path / "proj"
        folder.mkdir()
        bad = self._result("{{NOPE}}", {})
        materialize.settle_deferrals(folder, {"a.md": bad})
        good = self._result("x", {})
        materialize.settle_deferrals(folder, {"b.md": good})
        assert DeferralReport.read(folder).has_condition(
            materialize.unrendered_condition_id("a.md"))

    def test_registered_in_the_deferral_registry(self):
        from vco_lib import deferral_registry as dr

        for cid in (materialize.unrendered_condition_id(".claude/agents/coder.md"),
                    materialize.path_missing_condition_id("launchd:com.x.plist")):
            assert dr.disposition_for(cid) == "action_required"


class TestReadmeTable:
    def test_readme_table_is_the_registry(self):
        text = (REPO_ROOT / "templates" / "README.md").read_text(encoding="utf-8")
        begin = text.index(materialize.README_TABLE_BEGIN) + len(materialize.README_TABLE_BEGIN)
        end = text.index(materialize.README_TABLE_END)
        assert text[begin:end].strip("\n") == materialize.readme_placeholder_table(), (
            "templates/README.md placeholder table drifted from the registry; "
            "regenerate: python -m vco_lib.materialize --readme-table"
        )


class TestYamlQuotingStyles:
    @pytest.mark.parametrize("line", [
        'command: "{{ORCHESTRATOR_ROOT}}/x"',
        "command: '{{ORCHESTRATOR_ROOT}}/x'",
        "command: {{ORCHESTRATOR_ROOT}}/x",
        "args:\n  - {{ORCHESTRATOR_ROOT}}/x",
    ])
    def test_every_scalar_style_round_trips(self, line):
        value = "/a: #b 'c' \"d\" \\e"
        text = f"---\n{line}\n---\n"
        res = materialize.render(text, {"ORCHESTRATOR_ROOT": value},
                                 allowed={"ORCHESTRATOR_ROOT"}, escape="yaml")
        data = _frontmatter(res.text)
        got = data["command"] if "command" in data else data["args"][0]
        assert got == value + "/x"


def test_the_hub_token_table_is_read_not_restated():
    """The hub key set includes the non-``__VCT_`` token ``__WIN_USER_SID__`` —
    the reason the check reads WP-18B's table instead of a hand regex."""
    assert "__WIN_USER_SID__" in _HUB_KEYS
    assert _tokens("x __WIN_USER_SID__ y __BOLD__") == ["__WIN_USER_SID__"]

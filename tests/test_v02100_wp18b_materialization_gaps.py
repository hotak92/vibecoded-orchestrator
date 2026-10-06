# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-18B — the template-materialization gap fixes outside the core
renderer (survey ``V02100-TEMPLATE-MATERIALIZATION-SURVEY`` gaps (b), (e)).

1. The v0.2.99 ``orchestrator-tools`` mcpServers block (a server that never
   existed, through ``claude_mcp_servers/.venv``) is gone — and an UPDATING
   user's installed copy of the old render is replaced by the bundle update.
   v0.2.101 narrowed the checked set to the three agent templates from that
   release that still ship as default agents (``tester``, ``planner``,
   ``expert-coder``): the other four were merged away or moved into packs, so
   their old copies are handled by the orphan/leftover path, not this gate.
2. The Python MCP-registration fallback carries the rows' URLs (host + scheme
   + port), pinned to the Rust registrar by a shared case table; a stale
   ``http://localhost:<port>`` entry is corrected by the next registration.
3. ``query_code_graph.py`` reads WEAVIATE_URL / GRPC_PORT / OLLAMA_URL and
   connects through them.
4. The pre-tool-use SSRF allowlist follows the projected env (both shells).
5. Printed container commands name the detected runtime.
6. The vct-hub unit renderer's key table + the manual launchd plist mirror.
7. Every changed hook / script / agent is a bundle-managed file.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import xml.dom.minidom
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
AGENTS = REPO / "templates" / "agents" / "free"
HOOKS = REPO / "templates" / "hooks"
SCRIPTS = REPO / "templates" / "scripts"

#: The v0.2.99 templates that carried the dead ``orchestrator-tools`` block
#: AND still ship as default agents in v0.2.101. The other four (``coder``,
#: ``project-architect``, ``ai-agentic-architect``,
#: ``consulting-cto-portfolio-coordinator``) were merged away or moved into
#: packs, so no default-agent file exists to check any more.
V0299_SHIPPED_AGENTS = (
    "tester", "planner", "expert-coder",
)


def _frontmatter(text: str) -> str:
    assert text.startswith("---\n")
    return text[: text.index("\n---\n", 4) + 5]


# ─── 1. agents ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", V0299_SHIPPED_AGENTS)
def test_agent_declares_no_nonexistent_mcp_server(name: str) -> None:
    text = (AGENTS / f"{name}.md").read_text(encoding="utf-8")
    fm = _frontmatter(text)
    assert "orchestrator-tools" not in fm
    assert "orchestrator_tools_mcp" not in text
    assert "claude_mcp_servers/.venv" not in text
    assert "mcpServers:" not in fm


def test_no_agent_frontmatter_renders_a_missing_path() -> None:
    """Every ``{{ORCHESTRATOR_ROOT}}/…`` path an agent's FRONTMATTER names (what
    Claude Code execs — ``mcpServers`` command/args) exists in the shipped tree
    (the render replaces only the root)."""
    offenders = []
    for md in sorted((REPO / "templates" / "agents").rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            continue
        for rel in re.findall(r"\{\{ORCHESTRATOR_ROOT\}\}/([^\s`'\"),]+)", _frontmatter(text)):
            if not (REPO / rel.rstrip(".:;")).exists():
                offenders.append(f"{md.relative_to(REPO)}: {rel}")
    assert offenders == []


def _write_orch(root: Path, *, old: bool) -> None:
    """A minimal orchestrator clone whose agents are the v0.2.99 render
    (``old``) or the shipped one."""
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    dest = root / "templates" / "agents" / "free"
    dest.mkdir(parents=True, exist_ok=True)
    old_fm = json.loads((FIXTURES / "wp18b_v0299_agent_frontmatter.json").read_text())["frontmatter"]
    for name in V0299_SHIPPED_AGENTS:
        cur = (AGENTS / f"{name}.md").read_text(encoding="utf-8")
        body = cur[len(_frontmatter(cur)):]
        text = (old_fm[name] + body) if old else cur
        (dest / f"{name}.md").write_text(text, encoding="utf-8")


def _deferral_text(project: Path) -> str:
    p = project / ".claude" / "context" / "UPDATE_DEFERRED.md"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def test_update_replaces_the_v0299_agent_render(tmp_path: Path) -> None:
    """An installed agent that is byte-for-byte the v0.2.99 render (dead
    block included) is OVERWRITTEN by the bundle update — no adoption backup,
    no user-modified deferral; a user-edited copy is backed up, then replaced."""
    from vco_lib import project_init

    orch = tmp_path / "vco-clone"
    project = tmp_path / "project"
    orch.mkdir()
    project.mkdir()
    _write_orch(orch, old=True)
    project_init.install_project_bundle(project, orchestrator_root=orch, update_mode=False)
    agents = project / ".claude" / "agents"
    for name in V0299_SHIPPED_AGENTS:
        assert "orchestrator-tools" in (agents / f"{name}.md").read_text(encoding="utf-8")
    # One copy the user edited after install.
    edited = agents / "tester.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\nmy local note\n", encoding="utf-8")

    _write_orch(orch, old=False)  # the clone is updated in place
    result = project_init.install_project_bundle(project, orchestrator_root=orch, update_mode=True)

    for name in V0299_SHIPPED_AGENTS:
        text = (agents / f"{name}.md").read_text(encoding="utf-8")
        assert "orchestrator-tools" not in text, name
        assert "claude_mcp_servers/.venv" not in text, name
        assert text == (orch / "templates" / "agents" / "free" / f"{name}.md").read_text(encoding="utf-8")
    actions = result["actions"]
    for name in V0299_SHIPPED_AGENTS:
        rel = str(Path(".claude") / "agents" / f"{name}.md")
        assert rel not in actions["preserve"], name
        if name == "tester":
            assert rel in actions["adopt"]
        else:
            assert rel in actions["overwrite"], (name, {k: v for k, v in actions.items() if v})
    backups = project / ".claude" / "backups" / "bundle-adoptions"
    backed_up = sorted(p.name for p in backups.rglob("*.md")) if backups.exists() else []
    assert backed_up == ["tester.md"]
    assert "my local note" in next(backups.rglob("tester.md")).read_text(encoding="utf-8")
    deferred = _deferral_text(project)
    assert "bundle_user_modified_preserved" not in deferred
    assert "claude_mcp_servers/.venv" not in deferred


# ─── 2. MCP registration URLs ───────────────────────────────────────────

_URL_TABLE = json.loads((FIXTURES / "mcp_registration_url_parity.json").read_text())


def _rows(spec: dict):
    from vco_lib import service_endpoints as se

    rows = {}
    for service, row in spec.items():
        if row is None:
            continue
        fields = dict(row)
        fields.setdefault("source", "install_probe")
        rows[service] = se.EndpointRow(service=service, **fields)
    return rows


def _pseudo_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "root"
    (root / "claude_mcp_servers" / "weaviate_mcp").mkdir(parents=True)
    (root / "claude_mcp_servers" / "search_mcp").mkdir(parents=True)
    return root, root / ".venv" / "bin" / "python"


@pytest.mark.parametrize("case", _URL_TABLE["cases"], ids=lambda c: c["name"])
def test_python_fallback_entries_follow_the_rows(case, tmp_path: Path) -> None:
    from vco_lib import install_mcp, service_endpoints as se

    root, py = _pseudo_root(tmp_path)
    urls = se.urls_from_rows(_rows(case["rows"]))
    entries = install_mcp._build_python_mcp_entries(root, py, urls)
    env = next(e for n, e, _ in entries if n == "weaviate-kg")["env"]
    for key, want in case["expect_weaviate_kg_env"].items():
        assert env[key] == want, key


def test_next_registration_corrects_a_stale_localhost_entry(tmp_path: Path) -> None:
    """A ``~/.claude.json`` entry the old fallback wrote with
    ``http://localhost:<port>`` is overwritten by the next ``_register_mcps``
    (every install / ``--update`` runs it) with the rows' URLs."""
    sys.path.insert(0, str(REPO))
    import install  # type: ignore

    from vco_lib import service_endpoints as se

    root = tmp_path / "root"
    (root / "claude_mcp_servers" / "weaviate_mcp").mkdir(parents=True)
    (root / "claude_mcp_servers" / "search_mcp").mkdir(parents=True)
    venv_py = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    stale = {"mcpServers": {"weaviate-kg": {"type": "stdio", "command": str(venv_py), "args": [],
                                            "env": {"OLLAMA_URL": "http://localhost:11434",
                                                    "WEAVIATE_URL": "http://localhost:8081"}},
                            "user-own": {"command": "x"}}}
    (home / ".claude.json").write_text(json.dumps(stale), encoding="utf-8")
    case = _URL_TABLE["cases"][2]
    urls = se.urls_from_rows(_rows(case["rows"]))
    from vco_lib.deferral_report import DeferralReport

    with mock.patch.dict(os.environ, {"VCT_USER_HOME_OVERRIDE": str(home)}), \
            mock.patch.object(install, "_service_endpoint_urls", return_value=urls), \
            mock.patch.object(install, "_try_bundled_launcher_binary", return_value=None), \
            mock.patch.object(install, "_try_download_launcher_binary", return_value=None), \
            mock.patch.object(install, "_try_cargo_tauri_build", return_value=None):
        install._register_mcps(root, DeferralReport())
    data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    env = data["mcpServers"]["weaviate-kg"]["env"]
    for key, want in case["expect_weaviate_kg_env"].items():
        assert env[key] == want, key
    assert data["mcpServers"]["user-own"] == {"command": "x"}


# ─── 3. code-graph query endpoints ──────────────────────────────────────


def _import_qcg(monkeypatch, tmp_path: Path, env: dict, config: dict | None = None):
    for extra in (str(SCRIPTS), str(REPO / "claude_mcp_servers")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    claude_dir = tmp_path / "claude"
    if config is not None:
        cfg = claude_dir / "workflow" / "config" / "mcp-config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(claude_dir))
    for key in ("WEAVIATE_URL", "WEAVIATE_PORT", "GRPC_PORT", "OLLAMA_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("query_code_graph", None)
    return importlib.import_module("query_code_graph")


def test_query_code_graph_reads_the_env_and_connects_through_it(monkeypatch, tmp_path: Path) -> None:
    mod = _import_qcg(monkeypatch, tmp_path, {
        "WEAVIATE_URL": "http://127.0.0.1:18081", "GRPC_PORT": "50099",
        "OLLAMA_URL": "http://gpu.lan:21435"})
    try:
        assert (mod.WEAVIATE_URL, mod.GRPC_PORT, mod.OLLAMA_URL) == (
            "http://127.0.0.1:18081", 50099, "http://gpu.lan:21435")
        seen: dict = {}

        class _FakeWeaviate:
            @staticmethod
            def connect_to_custom(**kw):
                seen.update(kw)
                return object()

        with mock.patch.dict(sys.modules, {"weaviate": _FakeWeaviate}):
            assert mod.CodeGraphQuery().connect() is True
        assert (seen["http_host"], seen["http_port"], seen["grpc_host"], seen["grpc_port"]) == (
            "127.0.0.1", 18081, "127.0.0.1", 50099)
    finally:
        sys.modules.pop("query_code_graph", None)


def test_query_code_graph_env_outranks_config_per_knob(monkeypatch, tmp_path: Path) -> None:
    config = {"weaviate": {"url": "http://cfg-host:9000", "grpc_port": 51000},
              "ollama": {"url": "http://cfg-host:9001"}}
    mod = _import_qcg(monkeypatch, tmp_path, {"GRPC_PORT": "50111"}, config)
    try:
        assert mod.WEAVIATE_URL == "http://cfg-host:9000"
        assert mod.GRPC_PORT == 50111
        assert mod.OLLAMA_URL == "http://cfg-host:9001"
    finally:
        sys.modules.pop("query_code_graph", None)
    mod = _import_qcg(monkeypatch, tmp_path / "b", {})
    try:
        assert (mod.GRPC_PORT, mod.OLLAMA_URL) == (50052, "http://localhost:11435")
    finally:
        sys.modules.pop("query_code_graph", None)


@pytest.mark.parametrize("url,want", [
    ("http://localhost:8081", ("localhost", 8081)),
    ("https://weaviate.example.com", ("weaviate.example.com", 443)),
    ("http://gpu.lan", ("gpu.lan", 80)),
    ("http://[::1]:8090", ("::1", 8090)),
    ("localhost:18081", ("localhost", 18081)),
    ("http://host:notaport", ("host", 8081)),
    ("HTTPS://weaviate.example.com", ("weaviate.example.com", 443)),  # R18-11 scheme case
    ("HTTP://[::1]", ("::1", 80)),
])
def test_split_http_url(url: str, want) -> None:
    from vco_lib.weaviate_helpers import split_http_url

    assert split_http_url(url) == want


@pytest.mark.parametrize("url,host,port,secure", [
    ("http://localhost:8081", "localhost", 8081, False),
    ("https://weaviate.example.com", "weaviate.example.com", 443, True),
    ("HTTPS://weaviate.example.com", "weaviate.example.com", 443, True),   # R18-11
    ("Https://h:9443", "h", 9443, True),
    ("http://[::1]:8090", "[::1]", 8090, False),                           # R18-11 re-bracketed
    ("https://[2001:db8::5]", "[2001:db8::5]", 443, True),
    ("gpu.lan:18081", "gpu.lan", 18081, False),
])
def test_connect_v4_scheme_case_and_ipv6_host(monkeypatch, url, host, port, secure) -> None:
    """R18-11: ``http_secure`` follows the PARSED scheme (``HTTPS://`` is TLS
    on the 443 split chose), and an IPv6 host reaches the client bracketed —
    weaviate-client formats ``f"{host}:{port}"`` itself."""
    import types

    from vco_lib import weaviate_helpers as wh

    seen: dict = {}
    fake = types.ModuleType("weaviate")
    fake.connect_to_custom = lambda **kw: seen.update(kw) or "client"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weaviate", fake)
    monkeypatch.delenv("GRPC_PORT", raising=False)
    assert wh.connect_v4(url) == "client"
    assert (seen["http_host"], seen["http_port"], seen["http_secure"]) == (host, port, secure)
    assert seen["grpc_host"] == host


# ─── 4. SSRF allowlist ──────────────────────────────────────────────────


def _hook_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "scripts").mkdir(parents=True)
    for name in ("vct_project_config.sh", "vct_project_config.ps1"):
        shutil.copy2(SCRIPTS / name, proj / ".claude" / "scripts" / name)
    return proj


def _hook_env(proj: Path, tmp_path: Path, extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("WEAVIATE_URL", "WEAVIATE_PORT", "OLLAMA_URL", "CODE_EMBED_SERVICE_URL",
                        "VCT_HUB_PORT", "VCT_DISABLE_HOOKS")}
    env.update({"CLAUDE_PROJECT_DIR": str(proj), "VCT_STATE_DIR": str(tmp_path / "state"),
                "HOME": str(tmp_path / "home")})
    env.update(extra)
    return env


def _run_hook(argv: list[str], proj: Path, tmp_path: Path, url: str, extra: dict):
    payload = json.dumps({"tool_name": "WebFetch", "tool_input": {"url": url, "prompt": "x"},
                          "session_id": "wp18b"})
    return subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=60,
                          env=_hook_env(proj, tmp_path, extra), cwd=str(proj))


_SHELLS = [pytest.param(["bash", str(HOOKS / "pre-tool-use.sh")], id="sh")]
if shutil.which("pwsh"):
    _SHELLS.append(pytest.param(["pwsh", "-NoProfile", "-File", str(HOOKS / "pre-tool-use.ps1")], id="ps1"))

_MOVED = {"WEAVIATE_URL": "http://127.0.0.1:18081", "OLLAMA_URL": "http://localhost:21435",
          "VCT_HUB_PORT": "17777"}


@pytest.mark.parametrize("argv", _SHELLS)
@pytest.mark.parametrize("url,allowed", [
    ("http://localhost:18081/v1/meta", True),      # moved Weaviate (row → env)
    ("http://127.0.0.1:21435/api/tags", True),     # moved Ollama, loopback spelling
    ("http://localhost:11440/health", True),       # code-embed default (env silent)
    ("http://127.0.0.1:17777/api/v1/health", True),  # hub port through the resolver
    ("http://localhost:7860/", True),              # Gradio literal
    ("http://127.0.0.1:9999/", False),             # a random local port
    ("http://localhost:8081/v1/meta", False),      # Weaviate's OLD port once it moved
    ("http://localhost:18081@10.0.0.5:22/", False),  # userinfo trick
])
def test_ssrf_allowlist_follows_the_env(argv, url: str, allowed: bool, tmp_path: Path) -> None:
    proj = _hook_project(tmp_path)
    res = _run_hook(argv, proj, tmp_path, url, _MOVED)
    if allowed:
        assert res.returncode == 0, res.stderr
    else:
        assert res.returncode == 2, (res.returncode, res.stderr)
        assert "SSRF guard" in res.stderr
        assert "localhost:18081" in res.stderr  # the derived set is shown
        assert "hand-edit" not in res.stderr and "add to whitelist" not in res.stderr


@pytest.mark.parametrize("argv", _SHELLS)
def test_ssrf_defaults_when_env_is_silent(argv, tmp_path: Path) -> None:
    proj = _hook_project(tmp_path)
    assert _run_hook(argv, proj, tmp_path, "http://localhost:8081/v1/meta", {}).returncode == 0
    assert _run_hook(argv, proj, tmp_path, "http://localhost:7700/api/v1/health", {}).returncode == 0
    assert _run_hook(argv, proj, tmp_path, "http://localhost:18081/", {}).returncode == 2


@pytest.mark.parametrize("argv", [
    pytest.param(["bash", str(SCRIPTS / "vct_project_config.sh"), "hub-port"], id="sh"),
    *([pytest.param(["pwsh", "-NoProfile", "-File", str(SCRIPTS / "vct_project_config.ps1"), "-HubPort"],
                    id="ps1")] if shutil.which("pwsh") else []),
])
@pytest.mark.parametrize("env_pin,file_body,want", [
    ("17701", None, "17701"),
    (None, "17702\n", "17702"),
    ("notaport", "17703", "17703"),
    (None, None, "7700"),
])
def test_resolver_hub_port_subcommand(argv, env_pin, file_body, want, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    if file_body is not None:
        (state / "hub.port").write_text(file_body, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "VCT_HUB_PORT"}
    env.update({"VCT_STATE_DIR": str(state), "HOME": str(tmp_path)})
    if env_pin is not None:
        env["VCT_HUB_PORT"] = env_pin
    res = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == want


# ─── 5. printed container commands ──────────────────────────────────────


@pytest.fixture
def docker_machine(monkeypatch):
    """A machine where only docker is installed (and nothing pins podman)."""
    from vco_lib import containers

    monkeypatch.delenv("VCT_CONTAINER_RUNTIME", raising=False)
    monkeypatch.setattr(containers, "read_runtime_txt", lambda _root: None)
    monkeypatch.setattr(containers._tsd, "which",
                        lambda name, *a, **k: "/usr/bin/docker" if name == "docker" else None)
    return containers


def test_runtime_command_hint_names_the_detected_runtime(docker_machine) -> None:
    assert docker_machine.runtime_command_hint("start vco_ollama") == "docker start vco_ollama"
    assert docker_machine.runtime_command_hint("start x", runtime="podman") == "podman start x"


def test_runtime_hint_with_no_runtime_installed(monkeypatch) -> None:
    from vco_lib import containers

    monkeypatch.delenv("VCT_CONTAINER_RUNTIME", raising=False)
    monkeypatch.setattr(containers, "read_runtime_txt", lambda _root: None)
    monkeypatch.setattr(containers._tsd, "which", lambda *a, **k: None)
    assert containers.hint_runtime() == containers.RUNTIME_CANDIDATES[0]


def test_printed_remedies_follow_the_runtime(docker_machine, tmp_path: Path) -> None:
    from vco_lib import codegraph_deferrals as cd
    from vco_lib.deferral_report import DeferralReport
    from vco_lib.embedding_pull_plan import code_embed_unavailable_entry

    cd.emit_code_backend_down(tmp_path, "codesage_embed", "m")
    cd.emit_no_backend(tmp_path, RuntimeError("down"))
    commands = " ".join(e.command_to_apply for e in DeferralReport.read(tmp_path).entries)
    entry = code_embed_unavailable_entry("detail")
    for text in (commands, entry.command_to_apply):
        assert "docker start vco_code_embed" in text
        assert "podman" not in text


# ─── 6. vct-hub unit renderer key table + manual plist mirror ───────────

_HUB = json.loads((FIXTURES / "hub_unit_placeholders.json").read_text())


def test_hub_unit_templates_match_the_key_table() -> None:
    token = re.compile(_HUB["token_regex"])
    tdir = REPO / _HUB["template_dir"]
    on_disk = {p.name for p in tdir.iterdir() if p.is_file()}
    assert on_disk == set(_HUB["templates"])
    for name, keys in _HUB["templates"].items():
        assert sorted(set(token.findall((tdir / name).read_text(encoding="utf-8")))) == sorted(keys), name


def test_manual_launchctl_plist_is_the_canonical_body() -> None:
    tdir = REPO / _HUB["template_dir"]
    for manual_rel, spec in _HUB["manual_mirrors"].items():
        manual = (REPO / manual_rel).read_text(encoding="utf-8")
        canonical = (tdir / spec["canonical"]).read_text(encoding="utf-8")
        decl, _, rest = manual.partition("\n")
        assert rest.startswith("<!--")
        notes_end = rest.index("-->\n") + len("-->\n")
        assert decl + "\n" + rest[notes_end:] == canonical, manual_rel
        for text in (manual, canonical):
            filled = text.replace("__VCT_HUB_BIN__", "/opt/vct-hub").replace("__VCT_STATE_DIR__", "/Users/u/.vct")
            xml.dom.minidom.parseString(filled)  # XML 1.0: no `--` inside a comment


# ─── 7. bundle-managed ──────────────────────────────────────────────────


def test_changed_files_are_bundle_managed() -> None:
    from vco_lib import project_init

    dests = {op.dest_rel.replace("\\", "/") for op in project_init._enumerate_bundle_files(REPO)}
    for rel in (
        ".claude/hooks/pre-tool-use.sh", ".claude/hooks/pre-tool-use.ps1",
        ".claude/hooks/_lib/ssrf-allowlist.sh", ".claude/hooks/_lib/ssrf-allowlist.ps1",
        ".claude/scripts/query_code_graph.py", ".claude/scripts/vct_project_config.sh",
        ".claude/scripts/vct_project_config.ps1", ".claude/scripts/sync_knowledge_graph.py",
        ".claude/scripts/analyze_code_graph.py",
        *(f".claude/agents/{n}.md" for n in V0299_SHIPPED_AGENTS),
    ):
        assert rel in dests, rel

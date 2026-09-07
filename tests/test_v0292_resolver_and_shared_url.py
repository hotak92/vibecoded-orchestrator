# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Two resolvers that must not answer with something unusable (v0.2.92 R2).

**A directory is executable.** ``resolve-vco-venv.sh``'s ``$VCT_VENV`` tier
ends with ``$VCT_VENV`` itself, for a user who pointed the variable at the
interpreter directly.  ``[ -x ]`` is true for a DIRECTORY (that is the search
permission bit), so a ``$VCT_VENV`` naming a venv whose ``bin/python`` is
missing used to "resolve" to the venv directory — and every spawn built on it
died with exit 126, a message that says nothing about the misconfiguration.
The ``.ps1`` sibling already tested ``-PathType Leaf``; the ``.sh`` now tests
``-f`` before ``-x``.

**One home for the code-embed base URL.**  The three-step order (explicit →
``CODE_EMBED_SERVICE_URL`` → ``http://localhost:<CODE_EMBED_PORT|11440>``) was
inlined in ``vco_lib/embedding_service.py`` twice and in
``vco_lib/codegraph_resync.py`` once, beside the shared
``vco_lib.code_embed_image.service_base_url``.  The copies had already
diverged within one release: both embedding_service copies ignored
``CODE_EMBED_PORT`` entirely, so moving the service with the knob compose and
install.py both honour left them probing the old port and silently demoting to
an Ollama code tier.  The tests below drive each migrated call site with the
port moved, which is exactly what a re-inlined copy would fail.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
VENV_LIB_SH = REPO / "templates" / "hooks" / "_lib" / "resolve-vco-venv.sh"


# ---------------------------------------------------------------------------
# Item 3 — a directory must not resolve as an interpreter
# ---------------------------------------------------------------------------


def _resolve_sh(vct_venv: Path, script_dir: Path | None = None) -> str:
    """Run the real shell resolver and return VCO_VENV_PYTHON."""
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover — POSIX CI always has bash
        pytest.skip("no bash on this machine")
    script = (
        f'. "{VENV_LIB_SH}"\n'
        f'resolve_vco_venv_python "{script_dir or ""}"\n'
        'printf "%s" "$VCO_VENV_PYTHON"\n'
    )
    env = os.environ.copy()
    env["VCT_VENV"] = str(vct_venv)
    env.pop("VCT_INSTALL_ROOT", None)
    return subprocess.run(
        [bash, "-c", script], capture_output=True, text=True, env=env, timeout=60
    ).stdout


def test_a_venv_directory_is_not_accepted_as_the_interpreter(tmp_path):
    """The defect: ``[ -x <dir> ]`` is true, so the resolver returned the dir.

    Staged as the real misconfiguration — ``$VCT_VENV`` names a directory that
    exists and is traversable but holds no ``bin/python``.
    """
    venv = tmp_path / "brokenvenv"
    (venv / "bin").mkdir(parents=True)          # no python inside
    assert os.access(venv, os.X_OK), "the premise (a dir passes -x) does not hold here"

    resolved = _resolve_sh(venv)
    assert resolved == "", (
        f"the resolver handed back {resolved!r}; a spawn on it exits 126"
    )


def test_the_directory_that_would_have_been_returned_really_is_unusable(tmp_path):
    """Why it matters, measured rather than asserted: exec'ing a dir is 126."""
    venv = tmp_path / "brokenvenv"
    (venv / "bin").mkdir(parents=True)
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("no bash")
    rc = subprocess.run(
        [bash, "-c", f'"{venv}" --version'], capture_output=True, timeout=60
    ).returncode
    assert rc == 126, f"expected exit 126 from exec'ing a directory, got {rc}"


def test_a_real_interpreter_still_resolves(tmp_path):
    """LEAVE-ALONE side: the fix must not reject a genuine venv python."""
    venv = tmp_path / "goodvenv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    assert _resolve_sh(venv) == str(python)


def test_vct_venv_pointing_straight_at_an_interpreter_still_resolves(tmp_path):
    """LEAVE-ALONE: the tier exists for `$VCT_VENV=/usr/bin/python3`."""
    direct = tmp_path / "python3"
    direct.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    direct.chmod(0o755)
    assert _resolve_sh(direct) == str(direct)


def test_a_symlinked_interpreter_still_resolves(tmp_path):
    """LEAVE-ALONE: venv pythons are usually symlinks; `-f` follows them.

    A naive tightening to ``-f`` semantics that did not follow links would
    break every real venv, so pin the shape that actually ships.
    """
    real = tmp_path / "real-python"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    venv = tmp_path / "linkvenv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(real)
    assert _resolve_sh(venv) == str(venv / "bin" / "python")


def _resolve_sh_install_root(install_root: Path) -> str:
    """Drive the ``$VCT_INSTALL_ROOT`` tiers, which go through _probe_venv_python."""
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("no bash on this machine")
    script = (
        f'. "{VENV_LIB_SH}"\n'
        'resolve_vco_venv_python ""\n'
        'printf "%s" "$VCO_VENV_PYTHON"\n'
    )
    env = os.environ.copy()
    env.pop("VCT_VENV", None)
    env["VCT_INSTALL_ROOT"] = str(install_root)
    return subprocess.run(
        [bash, "-c", script], capture_output=True, text=True, env=env, timeout=60
    ).stdout


def test_the_install_root_probe_applies_the_same_is_it_a_file_rule(tmp_path):
    """One rule for "is this an interpreter?", not two.

    ``_probe_venv_python`` serves the ``$VCT_INSTALL_ROOT`` tiers. A directory
    at ``<venv>/bin/python`` is rarer than the tier-1 case, but a resolver
    that answers differently depending on WHICH tier found the candidate is a
    second rule waiting to diverge.
    """
    root = tmp_path / "root"
    (root / ".venv" / "bin" / "python").mkdir(parents=True)      # a DIRECTORY
    good = root / "claude_mcp_servers" / ".venv" / "bin"
    good.mkdir(parents=True)
    (good / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (good / "python").chmod(0o755)

    # Tier 2's directory must be rejected so tier 3's real interpreter wins.
    assert _resolve_sh_install_root(root) == str(good / "python")


# ---------------------------------------------------------------------------
# Item 5 — every call site goes through the ONE resolver
# ---------------------------------------------------------------------------


@pytest.fixture
def moved_port(monkeypatch):
    """The service moved off 11440 via CODE_EMBED_PORT, with no URL override."""
    monkeypatch.delenv("CODE_EMBED_SERVICE_URL", raising=False)
    monkeypatch.setenv("CODE_EMBED_PORT", "12345")
    return "http://localhost:12345"


def test_shared_resolver_is_the_reference(moved_port):
    from vco_lib.code_embed_image import service_base_url

    assert service_base_url() == moved_port
    assert service_base_url("http://explicit:1/") == "http://explicit:1"


def test_codegraph_resync_health_probe_uses_the_shared_resolver(moved_port, monkeypatch):
    """Driven: the URL the health probe actually opens.

    Intercepting ``urlopen`` in the module under test means a re-inlined copy
    (which would still say 11440 when only CODE_EMBED_PORT moved) fails here.
    """
    from vco_lib import codegraph_resync

    seen = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(url, timeout=None):
        seen.append(url)
        return _Resp()

    monkeypatch.setattr(codegraph_resync.urllib.request, "urlopen", _fake_urlopen)
    assert codegraph_resync.code_embed_service_healthy() is True
    assert seen == [f"{moved_port}/health"], seen


def test_embedding_service_for_project_uses_the_shared_resolver(moved_port, monkeypatch):
    """Driven: the code-embed URL ``for_project`` hands to the service."""
    from vco_lib import embedding_service as es

    captured = {}
    real_init = es.EmbeddingService.__init__

    def _spy(self, *args, **kwargs):
        captured.update(kwargs)
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(es.EmbeddingService, "__init__", _spy)
    try:
        es.EmbeddingService.for_project()
    except Exception:  # noqa: BLE001 — no backend is reachable in a test env
        pass
    assert captured.get("code_embed_url") == moved_port, captured


def test_embedding_service_code_model_discovery_uses_the_shared_resolver(
        moved_port, monkeypatch):
    """Driven: the URL ``discover_code_models`` probes."""
    from vco_lib import embedding_service as es

    seen = {}

    def _fake_discover(cls, sess, ollama_url, code_embed_url, openai_api_key):
        seen["code_embed_url"] = code_embed_url
        return []

    monkeypatch.setattr(
        es.EmbeddingService, "_discover_code_choices", classmethod(_fake_discover)
    )
    es.EmbeddingService.discover_code_models()
    assert seen.get("code_embed_url") == moved_port, seen


def test_an_explicit_url_still_wins_at_every_site(monkeypatch):
    """LEAVE-ALONE: the explicit argument outranks both env vars."""
    from vco_lib import codegraph_resync
    from vco_lib.code_embed_image import service_base_url

    monkeypatch.setenv("CODE_EMBED_SERVICE_URL", "http://env:1")
    monkeypatch.setenv("CODE_EMBED_PORT", "12345")
    assert service_base_url("http://explicit:2") == "http://explicit:2"

    seen = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        codegraph_resync.urllib.request, "urlopen",
        lambda url, timeout=None: (seen.append(url), _Resp())[1],
    )
    codegraph_resync.code_embed_service_healthy("http://explicit:2")
    assert seen == ["http://explicit:2/health"], seen


def test_no_module_re_inlines_the_resolution_order():
    """Delivery-layer backstop for the ONE home — not the wiring proof.

    The driven tests above are the wiring proof. This only stops a FOURTH copy
    appearing: outside ``code_embed_image`` itself, no module may build the
    localhost URL from ``CODE_EMBED_PORT`` by hand.
    """
    import ast

    offenders = []
    for path in sorted((REPO / "vco_lib").glob("*.py")):
        if path.name == "code_embed_image.py":
            continue
        source = path.read_text(encoding="utf-8")
        # Prose lines (comments AND docstrings) must not trip this — a
        # docstring DESCRIBING the shared order is the correct thing to have.
        prose = set()
        try:
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    prose.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        except SyntaxError:  # pragma: no cover
            pass
        for num, line in enumerate(source.splitlines(), 1):
            if num in prose or line.lstrip().startswith("#"):
                continue
            if "CODE_EMBED_PORT" in line and "localhost" in line:
                offenders.append(f"{path.relative_to(REPO)}:{num}: {line.strip()}")
    assert not offenders, (
        "these build the code-embed base URL inline instead of calling "
        "vco_lib.code_embed_image.service_base_url:\n  " + "\n  ".join(offenders)
    )

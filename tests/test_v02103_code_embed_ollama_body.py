# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 F1 (Windows field run): the code_embed container's ollama backend
builds its Ollama embed body WITHOUT vco_lib.

The image ships exactly requirements.txt + image_source.py + server.py — no
vco_lib, by design. v0.2.102 W4-E made ``_embed_ollama`` import
``vco_lib.embedding_providers.ollama.embed_request_body``, so in the container
every embed and every ``/health`` (``get_dim`` → a probe embed) died with
``No module named 'vco_lib'``; VCO then judged the endpoint "not VCO's
code-embed" and no code graph could be built.

Fix (A>B>C option B): install resolves the body from the one builder on the
host (``vco_lib.ollama_embed_body.container_body_template``) and writes it to
``infrastructure/.env`` as ``CODE_EMBED_OLLAMA_BODY``; compose passes it into
the container; the service merges the prompt in. These tests run the service
with vco_lib BLOCKED (a meta-path finder that raises ModuleNotFoundError, the
exact container failure) from a copy of the image's three files.

RED-PROOF: restoring the bare ``from vco_lib.embedding_providers.ollama import
embed_request_body`` in ``_embed_ollama`` fails the parity + /health tests
(ModuleNotFoundError in the subprocess); dropping the compose_env write fails
``test_install_writes_the_template_for_an_ollama_backend``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SERVICE_DIR = REPO / "claude_mcp_servers" / "code_embedding_service"
COMPOSE = REPO / "infrastructure" / "docker-compose.yml"

# Runs inside a subprocess whose import system refuses vco_lib, from a temp
# dir holding only the files the Dockerfile COPYs.
_CHILD = r"""
import importlib.abc, json, os, sys

class _NoVcoLib(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "vco_lib" or name.startswith("vco_lib."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return None

sys.meta_path.insert(0, _NoVcoLib())
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != os.environ["REPO_ROOT"]]
sys.path.insert(0, os.environ["IMAGE_DIR"])
job = json.loads(sys.stdin.read())

import server  # the image's server.py
assert server.VCO_LIB_AVAILABLE is False, "vco_lib must be unimportable here"

out = {"bodies": {}, "errors": {}}
for model, template in job["templates"].items():
    if template is None:
        os.environ.pop(server.OLLAMA_BODY_ENV_KEY, None)
    else:
        os.environ[server.OLLAMA_BODY_ENV_KEY] = template
    try:
        out["bodies"][model] = server._ollama_embed_request_body(model, job["text"])
    except Exception as exc:
        out["errors"][model] = f"{type(exc).__name__}: {exc}"

if job.get("health"):
    import asyncio
    import requests

    class _Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"embedding": [0.0] * 768}

    sent = []
    def _post(url, json=None, timeout=None):
        sent.append(json)
        return _Resp()

    requests.post = _post
    os.environ[server.OLLAMA_BODY_ENV_KEY] = job["health"]["template"]
    server.BACKEND = "ollama"
    server.MODEL_NAME = job["health"]["model"]
    out["health"] = asyncio.run(server.health())
    out["health_sent"] = sent
print(json.dumps(out))
"""


def _image_dir(tmp_path: Path) -> Path:
    d = tmp_path / "image"
    d.mkdir()
    for name in ("requirements.txt", "image_source.py", "server.py"):
        shutil.copy2(SERVICE_DIR / name, d / name)
    return d


def _run_child(tmp_path: Path, job: dict) -> dict:
    # DELIBERATELY UNPINNED (no `child_env()`): the child must NOT have the
    # checkout on its path — it simulates the image, where vco_lib is absent
    # (allowlisted in tests/test_v0292_fixround_child_env_lint.py).
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update({
        "REPO_ROOT": str(REPO),
        "IMAGE_DIR": str(_image_dir(tmp_path)),
        "CODE_EMBED_BACKEND": "ollama",
        "WEAVIATE_URL": "http://127.0.0.1:9",
    })
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD], input=json.dumps(job), capture_output=True,
        text=True, env=env, cwd=str(tmp_path), timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _table_models() -> list[str]:
    from claude_mcp_servers.weaviate_mcp.chunking import MODEL_TOKEN_LIMITS

    return sorted(MODEL_TOKEN_LIMITS)


@pytest.fixture(autouse=True)
def _default_keep_alive(monkeypatch):
    monkeypatch.delenv("VCO_OLLAMA_KEEP_ALIVE", raising=False)


def test_container_body_equals_the_builder_for_every_table_model(tmp_path):
    """The container's body, built with vco_lib unimportable from the
    template install writes, is EXACTLY ``embed_request_body`` — for every
    model in chunking.MODEL_TOKEN_LIMITS (so num_ctx can never differ)."""
    from vco_lib.ollama_embed_body import container_body_template, embed_request_body

    models = _table_models()
    out = _run_child(tmp_path, {
        "text": "def f(x): return x",
        "templates": {m: container_body_template(m) for m in models},
    })
    assert out["errors"] == {}
    for m in models:
        assert out["bodies"][m] == embed_request_body(m, prompt="def f(x): return x"), m


def test_container_body_follows_a_keep_alive_override(tmp_path, monkeypatch):
    """The keep_alive policy is resolved on the host (VCO_OLLAMA_KEEP_ALIVE),
    including the explicit empty-string opt-out — the container just carries
    it."""
    from vco_lib.ollama_embed_body import container_body_template, embed_request_body

    m = "unclemusclez/jina-embeddings-v2-base-code:latest"
    for value in ("2h", ""):
        monkeypatch.setenv("VCO_OLLAMA_KEEP_ALIVE", value)
        sub = tmp_path / f"ka-{value or 'empty'}"
        sub.mkdir()
        out = _run_child(sub, {"text": "t", "templates": {m: container_body_template(m)}})
        assert out["bodies"][m] == embed_request_body(m, prompt="t")
        assert ("keep_alive" in out["bodies"][m]) is bool(value)


def test_missing_or_other_model_template_is_refused_not_guessed(tmp_path):
    """No template / a template for another model → a refusal naming the key
    and the remedy, never a body with a guessed num_ctx."""
    from vco_lib.ollama_embed_body import container_body_template

    out = _run_child(tmp_path, {
        "text": "t",
        "templates": {
            "unclemusclez/jina-embeddings-v2-base-code:latest": None,
            "qwen3-embedding:0.6b": container_body_template("snowflake-arctic-embed2:latest"),
            "nomic-embed-text": "{not json",
        },
    })
    assert out["bodies"] == {}
    assert set(out["errors"]) == {
        "unclemusclez/jina-embeddings-v2-base-code:latest", "qwen3-embedding:0.6b",
        "nomic-embed-text",
    }
    for err in out["errors"].values():
        assert "CODE_EMBED_OLLAMA_BODY" in err and "install.py --update" in err, err
    assert "not set" in out["errors"]["unclemusclez/jina-embeddings-v2-base-code:latest"]
    assert "snowflake-arctic-embed2:latest" in out["errors"]["qwen3-embedding:0.6b"]


def test_health_on_the_ollama_backend_is_ok_without_vco_lib(tmp_path):
    """The field symptom: /health on the ollama backend returned
    status=error ("No module named 'vco_lib'"). With the template it is ok,
    reports the probed dim, and the probe body is the builder's."""
    from vco_lib.ollama_embed_body import container_body_template, embed_request_body

    m = "unclemusclez/jina-embeddings-v2-base-code:latest"
    out = _run_child(tmp_path, {
        "text": "t", "templates": {},
        "health": {"model": m, "template": container_body_template(m)},
    })
    health = out["health"]
    assert health["status"] == "ok", health
    assert health["backend"] == "ollama" and health["dim"] == 768
    assert health["vco_lib_available"] is False
    assert out["health_sent"] == [embed_request_body(m, prompt="dim probe")]


def test_install_writes_the_template_for_an_ollama_backend():
    """compose_substitution_env (→ infrastructure/.env) carries the template
    for an ollama-backend container, and nothing for the gpu backend."""
    from vco_lib.compose_env import compose_substitution_env
    from vco_lib.ollama_embed_body import CONTAINER_BODY_ENV_KEY, container_body_template

    m = "unclemusclez/jina-embeddings-v2-base-code:latest"
    env = compose_substitution_env({"code_backend": "ollama", "code_model": m})
    assert env["CODE_EMBED_MODEL"] == m
    assert env[CONTAINER_BODY_ENV_KEY] == container_body_template(m)
    assert json.loads(env[CONTAINER_BODY_ENV_KEY])["options"] == {"num_ctx": 2048}
    gpu = compose_substitution_env({"code_backend": "gpu", "gpu_vendor": "nvidia"})
    assert CONTAINER_BODY_ENV_KEY not in gpu


def test_template_value_is_one_literal_dotenv_token():
    """The value survives the .env round trip through VCO's own parser and
    carries nothing a compose/dotenv reader would interpolate or cut."""
    from vco_lib.envfile import parse_env_line
    from vco_lib.ollama_embed_body import CONTAINER_BODY_ENV_KEY, container_body_template

    for m in _table_models():
        value = container_body_template(m)
        assert not any(ch in value for ch in " #$'\n\r\t"), (m, value)
        parsed = parse_env_line(f"{CONTAINER_BODY_ENV_KEY}={value}")
        assert parsed is not None and parsed[1] == value, (m, parsed)


def test_env_key_name_parity_compose_service_and_builder():
    """One key name across the builder, the service and the compose file;
    the compose entry passes the .env value through unchanged."""
    import importlib

    from vco_lib.ollama_embed_body import CONTAINER_BODY_ENV_KEY

    server = importlib.import_module("claude_mcp_servers.code_embedding_service.server")
    assert server.OLLAMA_BODY_ENV_KEY == CONTAINER_BODY_ENV_KEY
    env = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["code_embed"]["environment"]
    assert env[CONTAINER_BODY_ENV_KEY] == "${" + CONTAINER_BODY_ENV_KEY + ":-}"
    from vco_lib.service_adoption import BEHAVIOR_CRITICAL_ENV_KEYS

    assert CONTAINER_BODY_ENV_KEY in BEHAVIOR_CRITICAL_ENV_KEYS


def test_the_service_has_no_unguarded_vco_lib_import():
    """Every vco_lib import in the image's server.py sits behind the
    VCO_LIB_AVAILABLE guard or the ImportError fallback — the container must
    never reach one (the F1 failure shape, on any backend)."""
    import ast

    tree = ast.parse((SERVICE_DIR / "server.py").read_text(encoding="utf-8"))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "VCO_LIB_AVAILABLE" in ast.unparse(node.test):
            guarded |= {getattr(n, "lineno") for b in node.body for n in ast.walk(b) if hasattr(n, "lineno")}
        if isinstance(node, ast.Try) and any(
            h.type is not None and "ImportError" in ast.unparse(h.type) for h in node.handlers
        ):
            guarded |= {getattr(n, "lineno") for b in node.body for n in ast.walk(b) if hasattr(n, "lineno")}
    bare = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "vco_lib"
        and n.lineno not in guarded
    ]
    assert bare == [], f"unguarded vco_lib imports in server.py at lines {bare}"

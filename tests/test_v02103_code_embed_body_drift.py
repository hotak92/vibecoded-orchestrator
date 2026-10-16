# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 R42-L1: an existing ollama-backend code_embed converges onto the
body template this checkout resolves, even when install has nothing else to
start or recreate.

``CODE_EMBED_OLLAMA_BODY`` is built from HOST code (``MODEL_TOKEN_LIMITS``,
the keep_alive default) that is not in the code_embed image. ``infrastructure/
.env`` used to be rewritten only inside the compose tail, which install skips
("All required services already running"), so a release editing the table for
an existing model left the running container on the old num_ctx while host
callers sent the new one. ``vco_lib.compose_env.code_embed_body_template_drifted``
decides; ``vco_lib.code_embed_image.plan_body_template_recreate`` rewrites the
file and names the service for recreate BEFORE install's recreate /
data-identity guards, so a foreign-identity container is still never touched.

Recreate is gated on the ONE in-stack decision (coordinator ruling): a
container VCO does not want only gets its ``.env`` refreshed.

RED-PROOF: dropping the ``plan_body_template_recreate`` call from
``install._start_services`` fails ``test_install_rewrites_env_and_recreates_on_drift``
(no compose run, stale template left in .env); making the drift decision
return False fails the unit drift arms; dropping the ``in_stack`` gate fails
``test_install_never_recreates_a_container_not_in_the_stack`` and the
``plan_rebuild(in_stack=False)`` ownership arm.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from claude_mcp_servers.weaviate_mcp import chunking  # noqa: E402
from vco_lib import code_embed_image, compose_env, containers  # noqa: E402
from vco_lib.ollama_embed_body import (  # noqa: E402
    CONTAINER_BODY_ENV_KEY,
    container_body_template,
)

MODEL = "unclemusclez/jina-embeddings-v2-base-code:latest"
OLLAMA_CFG = {"code_backend": "ollama", "code_model": MODEL}
GPU_CFG = {"code_backend": "gpu", "gpu_vendor": "nvidia"}


def _env_value(infra: Path, key: str):
    return compose_env._read_env_keys(infra).get(key)


@pytest.fixture
def stale_infra(tmp_path, monkeypatch):
    """An ``infrastructure/.env`` written by the PREVIOUS release (table entry
    4096), then the release under test edits the table to the shipped value."""
    infra = tmp_path / "infrastructure"
    infra.mkdir()
    shipped = chunking.MODEL_TOKEN_LIMITS[MODEL]
    monkeypatch.setitem(chunking.MODEL_TOKEN_LIMITS, MODEL, 4096)
    ok, msg = compose_env.write_infrastructure_env(infra, OLLAMA_CFG)
    assert ok, msg
    old = _env_value(infra, CONTAINER_BODY_ENV_KEY)
    monkeypatch.setitem(chunking.MODEL_TOKEN_LIMITS, MODEL, shipped)
    assert old != container_body_template(MODEL)
    return infra


# ── the drift decision ──────────────────────────────────────────────────────
def test_a_table_edit_for_the_running_model_is_drift(stale_infra):
    assert compose_env.code_embed_body_template_drifted(stale_infra, OLLAMA_CFG)


def test_a_missing_template_is_drift(tmp_path):
    (tmp_path / ".env").write_text("CODE_EMBED_BACKEND=ollama\n", encoding="utf-8")
    assert compose_env.code_embed_body_template_drifted(tmp_path, OLLAMA_CFG)


def test_the_same_template_is_not_drift(tmp_path):
    compose_env.write_infrastructure_env(tmp_path, OLLAMA_CFG)
    assert not compose_env.code_embed_body_template_drifted(tmp_path, OLLAMA_CFG)


def test_a_gpu_backend_is_never_drifted(stale_infra):
    """The gpu backend never reads the key — even a stale value is no drift."""
    assert not compose_env.code_embed_body_template_drifted(stale_infra, GPU_CFG)


# ── the recreate plan ───────────────────────────────────────────────────────
def _plan(infra, cfg, *, owned=True, url="http://localhost:11440", services=(),
          force_separate=False):
    writes = []

    def write_env(c):
        writes.append(c)
        compose_env.write_infrastructure_env(infra, c)

    out = code_embed_image.plan_body_template_recreate(
        code_embed_image.RebuildPlan(build=False, owned=owned),
        force_separate=force_separate, url=url, infra_dir=infra, embed_config=cfg,
        services=list(services), write_env=write_env)
    return out, writes


def test_drift_rewrites_env_and_requests_recreate(stale_infra):
    out, writes = _plan(stale_infra, OLLAMA_CFG)
    assert out == ("code_embed",)
    assert writes == [OLLAMA_CFG]
    assert _env_value(stale_infra, CONTAINER_BODY_ENV_KEY) == container_body_template(MODEL)


def test_the_same_template_requests_nothing_and_writes_nothing(tmp_path):
    compose_env.write_infrastructure_env(tmp_path, OLLAMA_CFG)
    assert _plan(tmp_path, OLLAMA_CFG) == ((), [])


def test_a_gpu_backend_requests_nothing(stale_infra):
    assert _plan(stale_infra, GPU_CFG) == ((), [])


@pytest.mark.parametrize("kw", [
    {"owned": False},                       # not in stack / CPU tier / foreign adopt
    {"url": None},                          # no running service to recreate
    {"force_separate": True},               # a separate stack brings up its own
    {"services": ("code_embed",)},          # already named — never twice
])
def test_drift_without_a_recreatable_container_only_refreshes_env(stale_infra, kw):
    out, writes = _plan(stale_infra, OLLAMA_CFG, **kw)
    assert out == ()
    assert writes == [OLLAMA_CFG]
    assert _env_value(stale_infra, CONTAINER_BODY_ENV_KEY) == container_body_template(MODEL)


def test_rebuild_plan_carries_the_ownership_verdict():
    """``owned`` is rebuild_allowed's verdict — the same rule for both."""
    plan = code_embed_image.plan_rebuild(
        decisions={"code_embed": {"action": "adopt", "probe": "foreign"}},
        has_gpu=True, force_separate=False, install_root=REPO_ROOT, url=None,
        services_to_start=[], services_to_recreate=[], adopt_action="adopt",
        managed_probe="vct-managed")
    assert plan.owned is False
    with mock.patch.object(code_embed_image, "image_state",
                           return_value=code_embed_image.ImageState(code_embed_image.CURRENT, "s")):
        plan = code_embed_image.plan_rebuild(
            decisions=None, has_gpu=True, force_separate=False, install_root=REPO_ROOT,
            url=None, services_to_start=[], services_to_recreate=[], adopt_action="adopt",
            managed_probe="vct-managed")
    assert plan.owned is True
    plan = code_embed_image.plan_rebuild(
        decisions=None, has_gpu=True, force_separate=False, install_root=REPO_ROOT, url=None,
        services_to_start=[], services_to_recreate=[], adopt_action="adopt",
        managed_probe="vct-managed", in_stack=False,
        state=code_embed_image.ImageState(code_embed_image.STALE, "s"))
    assert plan.owned is False  # a container VCO does not want is never ours to recreate


# ── end to end through install._start_services ─────────────────────────────
def _install_module():
    spec = importlib.util.spec_from_file_location("_test_install_r42_l1", REPO_ROOT / "install.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_test_install_r42_l1"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def install():
    return _install_module()


_DEFAULT = object()


def _start(install, root: Path, *, identity="infrastructure", in_stack=True, decisions=_DEFAULT,
           code_embed_url: "str | None" = "http://localhost:11440", embed_config=None):
    recorded: dict = {}

    def fake_run(cmd, **kwargs):
        if "compose" in cmd or "-f" in cmd:
            recorded["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    sysinfo = install.SystemInfo(
        os_name="Linux", has_gpu=True, has_metal=False, container_cmd="docker",
        gpu_name="RTX 4090", vram_gb=24.0, ram_gb=64.0, gpu_vendor="nvidia")
    # No code_embed row: running and ours, disposition "skip" (the field shape
    # of test_v0292_code_embed_image_rebuild) — so the v0.2.61 vct-managed
    # adopt recreate never fires and only this plan can name the service.
    decisions = decisions if decisions is not _DEFAULT else {
        "weaviate": {"action": install.ACTION_ADOPT, "probe": install.PROBE_FOREIGN},
        "ollama": {"action": install.ACTION_ADOPT, "probe": install.PROBE_FOREIGN},
    }
    current = code_embed_image.ImageState(code_embed_image.CURRENT, "summary(current)")
    # The ONE in-stack decision reads launcher.db; pinned so the host's own
    # bindings never decide these tests.
    with mock.patch.object(compose_env, "code_embed_container_in_stack", return_value=in_stack), \
         mock.patch.object(install, "PROJECT_ROOT", root), \
         mock.patch.object(install, "_detect_existing_volume_paths", return_value={}), \
         mock.patch.object(install, "_detect_existing_services", return_value={
             "weaviate_url": "http://localhost:8081",
             "ollama_url": "http://localhost:11435",
             "code_embed_url": code_embed_url}), \
         mock.patch.object(install, "_container_runtime_reachable", return_value=True), \
         mock.patch.object(install, "_get_compose_command", return_value=["docker", "compose"]), \
         mock.patch.object(install, "_log_install_event"), \
         mock.patch.object(install, "_should_check_weaviate_reclaim_drift", return_value=False), \
         mock.patch.object(code_embed_image, "image_state", return_value=current), \
         mock.patch.object(install._containers, "find_existing_container",
                           side_effect=lambda svc, runtime="podman": f"vco_{svc}"), \
         mock.patch.object(install._containers, "compose_identity_of",
                           return_value=containers.ComposeIdentity(identity)), \
         mock.patch.object(subprocess, "run", side_effect=fake_run):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            install._start_services(sysinfo, mock.Mock(), dict(embed_config or OLLAMA_CFG),
                                    decisions=decisions, deferral_report=None)
    return recorded.get("cmd", []), buf.getvalue()


@pytest.fixture
def root_with(tmp_path):
    def make(infra_src: Path) -> Path:
        root = tmp_path / "root"
        shutil.copytree(infra_src, root / "infrastructure")
        shutil.copy(REPO_ROOT / "infrastructure" / "docker-compose.yml",
                    root / "infrastructure" / "docker-compose.yml")
        return root
    return make


def test_install_rewrites_env_and_recreates_on_drift(install, stale_infra, root_with):
    root = root_with(stale_infra)
    cmd, out = _start(install, root)
    assert "--force-recreate" in cmd and "code_embed" in cmd, out
    assert "All required services already running" not in out
    assert _env_value(root / "infrastructure", CONTAINER_BODY_ENV_KEY) == \
        container_body_template(MODEL)


def test_install_never_recreates_a_container_not_in_the_stack(install, stale_infra, root_with):
    """Coordinator ruling: not in the stack → .env refreshed, no recreate."""
    root = root_with(stale_infra)
    cmd, out = _start(install, root, in_stack=False)
    assert cmd == []
    assert "All required services already running" in out
    assert _env_value(root / "infrastructure", CONTAINER_BODY_ENV_KEY) == \
        container_body_template(MODEL)


def test_install_with_the_same_template_reuses_everything(install, tmp_path, root_with):
    infra = tmp_path / "infra_src"
    infra.mkdir()
    compose_env.write_infrastructure_env(infra, OLLAMA_CFG)
    cmd, out = _start(install, root_with(infra))
    assert cmd == []
    assert "All required services already running" in out


def test_a_foreign_identity_container_is_never_recreated(install, stale_infra, root_with):
    """The plan runs BEFORE the identity guard: the guard strips the recreate,
    and the .env (VCO's own file) is still converged."""
    root = root_with(stale_infra)
    cmd, out = _start(install, root, identity="someone_elses_project")
    assert "code_embed" not in cmd
    assert _env_value(root / "infrastructure", CONTAINER_BODY_ENV_KEY) == \
        container_body_template(MODEL)


def test_the_legacy_branch_follows_the_one_in_stack_decision(install, tmp_path, root_with):
    """v0.2.103 (diag 1b): ``decisions=None`` used the pre-W6-B2 rule (any GPU
    host starts code_embed). It now follows ``code_embed_wanted``."""
    infra = tmp_path / "infra_src"
    infra.mkdir()
    compose_env.write_infrastructure_env(infra, OLLAMA_CFG)
    root = root_with(infra)
    cmd, _out = _start(install, root, in_stack=False, decisions=None, code_embed_url=None)
    assert "code_embed" not in cmd
    cmd, _out = _start(install, root, in_stack=True, embed_config=GPU_CFG, decisions=None,
                       code_embed_url=None)
    assert "code_embed" in cmd

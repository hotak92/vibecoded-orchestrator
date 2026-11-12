# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The bundled RL reranker keeps what it learns across container recreates.

Every module update (and every recreate the hub does on start) replaces the
container, so whatever the server writes must live on a HOST mount, never in
the container's writable layer: the per-project online models and learner
state (``/data/state/projects/<project_id>/``, ``rl_server.py``), the event
logs (``/data/logs``) and the global model (``/data/global``). This pins:

* the shipped manifest mounts every root the server writes
  (``rl_server._SAFE_PATH_ROOTS``) and the paths the image's env points at;
* the mounts are ONE global host directory set (the container is
  ``install.scope = global``) and never the paid module's per-slug
  ``<data>/vct-rl-reranker/<slug>/state`` dirs, which stay the user's;
* ``runtime.image_user`` equals the Dockerfile's ``APP_UID`` / ``APP_GID``
  (the launcher maps the host user onto it so the mounts are writable).

The run-spec half (the manifest's mounts and user mapping reach the
``podman run`` argv) is pinned on the Rust side, in
``container_runtime.rs`` (``the_shipped_rl_manifest_runs_with_its_mounts``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = ROOT / "claude_mcp_servers" / "rl_reranker"


def _manifest() -> dict:
    return json.loads((MODULE_DIR / "vct-module.json").read_text(encoding="utf-8"))


def _dockerfile() -> str:
    return (MODULE_DIR / "Dockerfile").read_text(encoding="utf-8")


def _mounted(path: str, mounts: "list[str]") -> bool:
    p = path.rstrip("/") + "/"
    return any(p.startswith(m.rstrip("/") + "/") for m in mounts)


def test_every_root_the_server_writes_is_a_host_mount():
    pytest.importorskip("torch")
    from rl_reranker import rl_server

    mounts = [v["container"] for v in _manifest()["runtime"].get("volumes", [])]
    assert mounts, "the manifest mounts nothing: every recreate loses the learnt models"
    for root in rl_server._SAFE_PATH_ROOTS:
        assert _mounted(root, mounts), f"{root} lives in the container's writable layer"


def test_the_image_env_points_inside_the_mounts():
    env = dict(re.findall(r"^\s*(RL_[A-Z_]+)=(\S+)", _dockerfile(), flags=re.M))
    mounts = [v["container"] for v in _manifest()["runtime"]["volumes"]]
    # RLServer: state_dir = RL_PROJECT_ROOT / "state"; logs at RL_LOG_PATH;
    # the global model under RL_GLOBAL_WEIGHTS_DIR (default /data/global).
    assert _mounted(env["RL_PROJECT_ROOT"].rstrip("/") + "/state", mounts)
    assert _mounted(env["RL_LOG_PATH"], mounts)
    assert _mounted(env.get("RL_GLOBAL_WEIGHTS_DIR", "/data/global"), mounts)


def test_the_mounts_are_one_global_set_beside_the_paid_modules_dirs():
    m = _manifest()
    assert m["install"]["scope"] == "global"
    for v in m["runtime"]["volumes"]:
        host = v["host"]
        assert host.startswith("{VCT_DATA}/"), host
        assert "{project_slug}" not in host and "{project_id}" not in host, host
        # The paid module's dirs (<data>/vct-rl-reranker/<slug>/...) are
        # user data this release leaves untouched, never re-mounted.
        assert not host.startswith("{VCT_DATA}/vct-rl-reranker/"), host
        assert not host.startswith("{VCT_DATA}/{MODULE_ID}/"), host


def test_image_user_matches_the_dockerfile_user():
    df = _dockerfile()
    uid = re.search(r"^ARG APP_UID=(\d+)$", df, flags=re.M)
    gid = re.search(r"^ARG APP_GID=(\d+)$", df, flags=re.M)
    assert uid and gid
    assert re.search(r"^USER app:app$", df, flags=re.M)
    assert _manifest()["runtime"].get("image_user") == {"uid": int(uid[1]), "gid": int(gid[1])}

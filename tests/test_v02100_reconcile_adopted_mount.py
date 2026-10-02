# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 W2R-11 — ``--service S=vco`` after an adoption clears ONLY a mount
observed on the adopted container; VCO's own recorded bind (kept on the
adopted row by ``keep_recorded_mount`` when the adopted container had no data
mount) goes back to VCO's copy."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import service_endpoints as se  # noqa: E402
from vco_lib import service_reconcile as sr  # noqa: E402
from vco_lib.service_detection import ContainerInfo, Mount  # noqa: E402

VCO_BIND = {"kind": "bind", "source": "/data/ollama-models",
            "destination": sr.DATA_MOUNT_TARGETS["ollama"]}


def _inp(container_mounts):
    existing = se.EndpointRow(service="ollama", mode="adopted_container", port=11434,
                              source="user_cli", host="localhost", container_name="their_ollama",
                              data_mount=VCO_BIND)
    containers = () if container_mounts is None else (
        ContainerInfo(name="their_ollama", image="ollama/ollama", state="running",
                      mounts=tuple(container_mounts)),)
    return sr.ServiceInputs(service="ollama", existing=existing,
                            choice=sr.Choice("ollama", "vco"), containers=containers,
                            port_free=lambda p: True)


def test_vcos_own_bind_on_an_adopted_row_returns_to_vcos_copy():
    """ACT: the adopted container mounts nothing at the data target → the
    recorded bind is VCO's → kept (RED before W2R-11: cleared)."""
    out = sr._decide_choice(_inp([]))
    assert out.row is not None and out.row.mode == "vco_managed"
    assert dict(out.row.data_mount or {}) == VCO_BIND and out.clear_mount is False


def test_the_adopted_containers_own_mount_is_still_cleared():
    """LEAVE-ALONE: the adopted container really mounts that directory → it is
    that container's data, never handed to VCO's copy."""
    live = Mount(kind="bind", source=VCO_BIND["source"], destination=VCO_BIND["destination"])
    out = sr._decide_choice(_inp([live]))
    assert out.clear_mount is True and out.row is not None and out.row.data_mount is None


def test_an_unlisted_container_keeps_the_conservative_clear():
    out = sr._decide_choice(_inp(None))
    assert out.clear_mount is True

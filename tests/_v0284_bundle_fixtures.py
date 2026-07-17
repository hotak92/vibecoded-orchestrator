# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared bundle-install test fixtures for v0.2.84 WP-4 (PLAN-v0284 AMENDMENTS A4:
one-concern-one-home — a single fake-orchestrator + non-root helper instead of a
per-file copy).

`make_fake_orchestrator(root)` writes a minimal orchestrator tree (hooks with
both .sh/.ps1 flavours, _lib, a script, agents, docker+podman compose files,
settings templates). `bundle_ext()` returns the OS-correct hook extension.
"""
from __future__ import annotations

import json
import platform
from pathlib import Path


def bundle_ext() -> str:
    """OS-correct hook/script flavour extension."""
    return "ps1" if platform.system() == "Windows" else "sh"


def make_fake_orchestrator(root: Path, *, with_compose_pair: bool = True) -> None:
    """Write a minimal fake orchestrator tree for bundle tests.

    Ships BOTH docker- and podman- compose flavours (A4 container-runtime
    coverage) so tests can assert adoption/overwrite parity across runtimes and
    pin that the launcher-owned `.override.yml` mirror is NOT in the op set.
    """
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")

    hooks = root / "templates" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "foo.sh").write_text("#!/bin/sh\necho v1\n", encoding="utf-8")
    (hooks / "foo.ps1").write_text("Write-Host 'v1'\n", encoding="utf-8")

    lib = hooks / "_lib"
    lib.mkdir()
    (lib / "find-python.sh").write_text("# find-python v1\n", encoding="utf-8")
    (lib / "find-python.ps1").write_text("# find-python.ps1 v1\n", encoding="utf-8")

    scripts = root / "templates" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "kg-search").write_text(
        "#!/usr/bin/env python3\nprint('s')\n", encoding="utf-8",
    )

    agents = root / "templates" / "agents" / "free"
    agents.mkdir(parents=True)
    (agents / "coder.md").write_text(
        "# Coder\nOrchestrator at {{ORCHESTRATOR_ROOT}}\n", encoding="utf-8",
    )

    settings = {"permissions": {"allow": ["Bash"]}, "hooks": {}}
    (root / "templates" / "settings.json.linux.template").write_text(
        json.dumps(settings, indent=2), encoding="utf-8",
    )
    (root / "templates" / "settings.json.windows.template").write_text(
        json.dumps(settings, indent=2), encoding="utf-8",
    )

    infra = root / "infrastructure"
    infra.mkdir()
    if with_compose_pair:
        # A4: both container-runtime compose flavours ship (docker + podman).
        # The `.override.yml` dual-name MIRROR is deliberately NOT shipped —
        # it is written at runtime by the launcher's C-RT-5 mirror path, so it
        # is not in `_enumerate_bundle_files` and adoption can never touch it.
        (infra / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (infra / "podman-compose.gpu.yml").write_text("services: {gpu: 1}\n", encoding="utf-8")
    else:
        (infra / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

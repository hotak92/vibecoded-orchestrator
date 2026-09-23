# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""infrastructure/docker-compose.yml — volume SOURCE env overrides.

A machine whose services use BIND mounts (e.g. Ollama models at a host
path shared with other containers) could not adopt the installer compose
without copying data; a blind recreate silently re-pointed the service at
an EMPTY named volume (field evidence 2026-09-23: 110 GB Ollama bind +
7.4 GB code-embed cache vs the empty ``vco_ollama_data`` /
``vco_code_embed_cache`` volumes).

The compose now parameterises, per service (the
``VCT_CODE_EMBED_BUILD_CONTEXT`` pattern):

  * ``VCT_<SERVICE>_DATA_SOURCE`` — the service-stanza mount SOURCE.
    Default: the volume key (``weaviate_data`` …). Set it to a HOST PATH
    for a bind mount.
  * ``VCT_<SERVICE>_VOLUME_NAME`` — the top-level ``volumes:`` ``name:``.
    Default: today's resolved names (``vco_weaviate_data`` …). Set it to
    an EXISTING volume name to reuse that volume.

Why two knobs and not one: compose classifies a short-syntax source as a
bind when it starts with ``/`` / ``./`` / ``~`` and as a named volume
otherwise — and a named volume that is not declared under top-level
``volumes:`` is a HARD ERROR ("service refers to undefined volume",
verified on docker compose v2.40.3; podman-compose 1.5.0 likewise fails
to parse). So "an existing volume name" can only enter through the
declared volume's ``name:`` field. Both runtimes were verified to honour
``${VAR:-default}`` in the source position and in ``name:`` (docker
compose v2.40.3, podman-compose 1.5.0, 2026-09-23; ``config`` renders —
side-effect free).

These tests parse the REAL compose file through the substitution engine
VCO itself uses to read it
(:func:`vco_lib.service_adoption.load_compose_doc` + ``config_mounts``),
so they pin what the adoption/guard readers see, not a reimplementation.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "infrastructure" / "docker-compose.yml"

from vco_lib.service_adoption import (  # noqa: E402
    config_mounts,
    load_compose_doc,
)

#: Today's defaults — the exact values the file must render with when no
#: override is set (nothing changes for anyone who sets nothing).
#: service -> (container destination, volume key, resolved volume name)
DEFAULTS = {
    "weaviate": ("/var/lib/weaviate", "weaviate_data", "vco_weaviate_data"),
    "ollama": ("/root/.ollama", "ollama_data", "vco_ollama_data"),
    "code_embed": ("/cache", "code_embed_cache", "vco_code_embed_cache"),
}

#: (service, source-override var, volume-name-override var) per service.
OVERRIDE_VARS = {
    "weaviate": ("VCT_WEAVIATE_DATA_SOURCE", "VCT_WEAVIATE_VOLUME_NAME"),
    "ollama": ("VCT_OLLAMA_DATA_SOURCE", "VCT_OLLAMA_VOLUME_NAME"),
    "code_embed": ("VCT_CODE_EMBED_CACHE_SOURCE", "VCT_CODE_EMBED_VOLUME_NAME"),
}


def _effective(env: dict) -> dict:
    doc = load_compose_doc(COMPOSE_FILE, env)
    assert doc is not None, f"compose file unparseable: {COMPOSE_FILE}"
    return doc


class ComposeVolumeOverrideTests(unittest.TestCase):
    def test_defaults_render_exactly_todays_sources_and_names(self) -> None:
        doc = _effective({})
        services = doc["services"]
        for service, (dest, key, resolved) in DEFAULTS.items():
            mounts = config_mounts(services[service], doc.get("volumes") or {})
            self.assertEqual(
                set(mounts), {dest},
                f"{service}: default mount destinations changed",
            )
            m = mounts[dest]
            self.assertEqual(m.kind, "volume")
            self.assertEqual(m.source, resolved)
        for service, (_, vol_key, resolved) in DEFAULTS.items():
            self.assertEqual(
                (doc["volumes"].get(vol_key) or {}).get("name"), resolved,
                f"{service}: volume {vol_key} default resolved name changed",
            )

    def test_source_override_makes_the_mount_a_bind_at_that_path(self) -> None:
        doc = _effective({"VCT_OLLAMA_DATA_SOURCE": "/srv/ollama/models"})
        mounts = config_mounts(
            doc["services"]["ollama"], doc.get("volumes") or {}
        )
        m = mounts["/root/.ollama"]
        self.assertEqual(m.kind, "bind")
        self.assertEqual(m.source, "/srv/ollama/models")
        # The other services keep their defaults (per-service overrides).
        w = config_mounts(doc["services"]["weaviate"], doc.get("volumes") or {})
        self.assertEqual(w["/var/lib/weaviate"].kind, "volume")
        self.assertEqual(w["/var/lib/weaviate"].source, "vco_weaviate_data")

    def test_volume_name_override_resolves_through_the_top_level_name(self) -> None:
        doc = _effective({"VCT_WEAVIATE_VOLUME_NAME": "existing_weaviate_vol"})
        mounts = config_mounts(
            doc["services"]["weaviate"], doc.get("volumes") or {}
        )
        m = mounts["/var/lib/weaviate"]
        self.assertEqual(m.kind, "volume")
        self.assertEqual(
            m.source, "existing_weaviate_vol",
            "a name override must flow into the mount the reader resolves",
        )

    def test_override_vars_present_in_the_file_for_every_service(self) -> None:
        """The knobs must actually be wired in the compose text — a var
        documented in docs/ but absent from the file is a broken promise."""
        text = COMPOSE_FILE.read_text(encoding="utf-8")
        for service, (src_var, name_var) in OVERRIDE_VARS.items():
            self.assertIn(
                f"${{{src_var}:", text,
                f"{service}: {src_var} is not interpolated in the compose file",
            )
            self.assertIn(
                f"${{{name_var}:", text,
                f"{service}: {name_var} is not interpolated in the compose file",
            )


class LiveComposeRenderTests(unittest.TestCase):
    """Optional live confirmation through the real compose binaries.

    ``<compose> config`` only renders the interpolated document — no
    container, volume or network is touched. Skipped when neither
    docker compose nor podman-compose is on PATH (CI images without
    container tooling still get the pure-parse tests above).
    """

    #: The rendered config — populated by setUpClass, which SKIPS the whole
    #: class when no compose binary can produce one (so the tests below may
    #: treat it as always-set).
    render: str

    @classmethod
    def setUpClass(cls) -> None:
        candidates: list[list[str]] = []
        if shutil.which("docker-compose") or shutil.which("docker"):
            candidates.append(["docker-compose", "-f", str(COMPOSE_FILE), "config"])
        if shutil.which("podman-compose"):
            candidates.append(
                ["podman-compose", "-f", str(COMPOSE_FILE), "config"]
            )
        for argv in candidates:
            try:
                proc = subprocess.run(
                    argv, capture_output=True, text=True, timeout=60,
                    env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                         "HOME": str(Path.home())},
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if proc.returncode == 0 and proc.stdout.strip():
                cls.render = proc.stdout
                return
        # (fall through to the SkipTest below)
        raise unittest.SkipTest(
            "no usable compose binary for the live render check"
        )

    def test_default_render_keeps_named_volumes(self) -> None:
        self.assertIn("vco_weaviate_data", self.render)
        self.assertNotIn("${VCT_WEAVIATE_DATA_SOURCE", self.render)

    def test_bind_override_renders_as_a_bind(self) -> None:
        proc = subprocess.run(
            ["docker-compose", "-f", str(COMPOSE_FILE), "config"],
            capture_output=True, text=True, timeout=60,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                 "HOME": str(Path.home()),
                 "VCT_OLLAMA_DATA_SOURCE": "/srv/ollama/models"},
        )
        if proc.returncode != 0:
            self.skipTest(f"docker-compose unavailable: {proc.stderr[:200]}")
        self.assertIn("/srv/ollama/models", proc.stdout)
        self.assertIn("type: bind", proc.stdout)


if __name__ == "__main__":
    unittest.main()

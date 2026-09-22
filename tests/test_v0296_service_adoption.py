# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 WP-4 — the guarded, mount-reconciling adoption of foreign-owned
compose services (``vco_lib.service_adoption``), the owning-name rebuild
derivation, and the deferral remedy that points at them.

Field shape this models (2026-09-20 survey): weaviate / ollama / code_embed
run healthy but were created by the LEGACY compose home (project
``vibecoded``, ``claude_mcp_servers/compose.yaml`` + a gitignored override
carrying the 110 GB ollama models BIND and ``OLLAMA_KEEP_ALIVE: 30s``),
while the installer drives ``infrastructure/docker-compose.yml`` (project
``infrastructure``, named volumes ``vco_*``).  The adoption must move the
containers WITHOUT changing what they mount or how they behave.

Test matrix required by the plan (WP-4) — all FULLY MOCKED, no podman/docker
ever executes (a fake ``run`` seam answers every inspect / ps / compose /
stop / rm / network call; ``find_existing_container`` and the GPU host
probe are patched; roots live in tmp dirs; ``VCT_STATE_DIR`` redirects
services.toml):

* mount/env identity matrix + the m-5 pins — the ollama BIND survives
  byte-for-byte as a bind (never a named volume), ``OLLAMA_KEEP_ALIVE``
  reconciles to the canonical ``24h`` (NOT the foreign home's ``30s``), the
  live-only ``OLLAMA_FLASH_ATTENTION`` is carried, healthchecks are
  reconstructed, the model-router network leg + alias is preserved;
* named-volume replacement attempt = hard refusal, on BOTH gates: the
  fragments simulation (:func:`service_adoption._verify_final_mounts`) and
  the RENDERED override re-merged with the base chain — the red-proof
  targets;
* never-touch-volumes / never-``down`` / ``rm`` only for adopted containers;
* rollback: a failed service N leaves N+1 untouched and is re-created under
  the OWNING invocation;
* the mixed-provider stale-network-label refusal on the first recreate is
  handled (empty network → rm → ONE retry; attached network → never
  removed);
* unreconcilable services stay foreign, named per-service, nothing stopped;
* a user-authored override is never clobbered;
* services.toml rows drop for the ADOPTED services only;
* GPU overlay selection by compose FORM (+ parse-verify refusal);
* the deferral remedy leads with the adoption command and derives the
  OWNING-name rebuild (Task 2 / Task 3a);
* post-adopt clear: the owned record expires via owned-drop-when-absent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import (  # noqa: E402
    code_embed_image,
    containers,
    install_services_guard,
    service_adoption,
)
from vco_lib.deferral_probes import owned_record_is_expirable  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402

CID_FOREIGN = "services_foreign_compose_identity"

OWNING_PROJECT = "vibecoded"
OWN_PROJECT = "infrastructure"
OLLAMA_BIND = "/home/martino/podman_volumes/ollama/models"

# ---------------------------------------------------------------------------
# The compose files the two homes contribute (mirrors of the field shape).
# ---------------------------------------------------------------------------

BASE_COMPOSE = f"""\
name: {OWN_PROJECT}

services:
  weaviate:
    image: cr.weaviate.io/semitechnologies/weaviate:1.27.0
    ports:
      - "8081:8080"
    environment:
      QUERY_DEFAULTS_LIMIT: "25"
      PERSISTENCE_LSM_MAX_SEGMENT_SIZE: "268435456"
      TOMBSTONE_DELETION_MIN_PER_CYCLE: "500"
      TOMBSTONE_DELETION_MAX_PER_CYCLE: "1000"
      TOMBSTONE_DELETION_CONCURRENCY: "8"
    volumes:
      - weaviate_data:/var/lib/weaviate
  ollama:
    image: docker.io/ollama/ollama:0.5.7
    ports:
      - "11435:11434"
    volumes:
      - ollama_data:/root/.ollama
  code_embed:
    build:
      context: ../claude_mcp_servers/code_embedding_service
    ports:
      - "11440:8000"
    environment:
      CODE_EMBED_BACKEND: gpu
      CODE_EMBED_MODEL: Qwen/Qwen2.5-Coder-7B-Instruct
    volumes:
      - code_embed_cache:/cache

volumes:
  weaviate_data:
    name: vco_weaviate_data
  ollama_data:
    name: vco_ollama
  code_embed_cache:
    name: vco_code_embed_cache
"""

#: The legacy home's base: named volume for weaviate (SAME resolved name as
#: the installer's — the "safest" case), the 110 GB BIND for ollama, and a
#: differently-named external volume for code_embed.
OWNING_COMPOSE = f"""\
name: {OWNING_PROJECT}

services:
  weaviate:
    image: cr.weaviate.io/semitechnologies/weaviate:1.27.0
    volumes:
      - vco_weaviate_data:/var/lib/weaviate
  ollama:
    image: docker.io/ollama/ollama:0.5.7
    volumes:
      - {OLLAMA_BIND}:/root/.ollama:Z
  code_embed:
    build:
      context: ../code_embedding_service
    volumes:
      - code_embed_data:/cache

volumes:
  vco_weaviate_data:
    external: true
  code_embed_data:
    external: true
"""

#: Same, with the CDI device declarations the legacy podman-compose home
#: carries for its GPU services (the second runtime-evidence signal).
OWNING_COMPOSE_GPU = OWNING_COMPOSE.replace(
    "  ollama:\n    image: docker.io/ollama/ollama:0.5.7\n",
    "  ollama:\n    image: docker.io/ollama/ollama:0.5.7\n"
    "    devices:\n      - nvidia.com/gpu=all\n",
).replace(
    "      context: ../code_embedding_service\n",
    "      context: ../code_embedding_service\n"
    "    devices:\n      - nvidia.com/gpu=all\n",
)

#: The legacy home's override: the 0.2.77 hook-latency fix TRAP — 30s, where
#: VCO's own fix (and CANONICAL_ENV_TARGETS) says 24h.
OWNING_OVERRIDE = """\
services:
  ollama:
    environment:
      OLLAMA_KEEP_ALIVE: 30s
"""

#: The subcommand-form GPU overlay (docker-compose v2 shape — deploy
#: resources, NOT the CDI ``devices:`` podman-compose cannot-parse-here).
GPU_SUBCOMMAND_OVERLAY = """\
services:
  ollama:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
  code_embed:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
"""

# ---------------------------------------------------------------------------
# The live "machine": inspect payloads per container.
# ---------------------------------------------------------------------------

HEALTHCHECKS = {
    "vco_weaviate": {
        "Test": ["CMD-SHELL", "curl -f http://localhost:8080/v1/.well-known/ready || exit 1"],
        "Interval": 30000000000, "Timeout": 5000000000, "Retries": 5,
    },
    "vco_ollama": {
        "Test": ["CMD-SHELL", "curl -f http://localhost:11434/api/tags || exit 1"],
        "Interval": 10000000000, "Timeout": 5000000000, "Retries": 5,
    },
    "vco_code_embed": {
        "Test": ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"],
        "Interval": 30000000000, "Timeout": 3000000000, "Retries": 3,
    },
}

LIVE = {
    "vco_weaviate": {
        "mounts": [{"Type": "volume", "Name": "vco_weaviate_data",
                    "Source": "/var/lib/containers/storage/volumes/x/_data",
                    "Destination": "/var/lib/weaviate"}],
        "env": [
            "QUERY_DEFAULTS_LIMIT=25",
            "PERSISTENCE_LSM_MAX_SEGMENT_SIZE=268435456",
            "TOMBSTONE_DELETION_MIN_PER_CYCLE=500",
            "TOMBSTONE_DELETION_MAX_PER_CYCLE=1000",
            "TOMBSTONE_DELETION_CONCURRENCY=8",
        ],
        "ports": {"8080/tcp": [{"HostIp": "", "HostPort": "8081"}]},
    },
    "vco_ollama": {
        "mounts": [{"Type": "bind", "Source": OLLAMA_BIND,
                    "Destination": "/root/.ollama", "Mode": "Z"}],
        "env": ["OLLAMA_KEEP_ALIVE=30s", "OLLAMA_FLASH_ATTENTION=1"],
        "ports": {"11434/tcp": [{"HostIp": "", "HostPort": "11435"}]},
    },
    "vco_code_embed": {
        "mounts": [{"Type": "volume", "Name": "code_embed_data",
                    "Source": "/var/lib/containers/storage/volumes/y/_data",
                    "Destination": "/cache"}],
        "env": ["CODE_EMBED_BACKEND=gpu",
                "CODE_EMBED_MODEL=Qwen/Qwen2.5-Coder-7B-Instruct",
                "CODE_EMBED_MAX_CONCURRENT=4"],
        "ports": {"8000/tcp": [{"HostIp": "", "HostPort": "11440"}]},
    },
}

SERVICE_TO_CONTAINER = {"weaviate": "vco_weaviate", "ollama": "vco_ollama",
                        "code_embed": "vco_code_embed"}
#: every container in the owning project, including the ones that STAY
#: (model_router's DNS dependency is the reason the project is never downed).
OWNING_PROJECT_CONTAINERS = ["vco_model_router", "vco_neo4j",
                             "vco_weaviate", "vco_ollama", "vco_code_embed"]


def _cp(argv, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)


class World:
    """A fake machine + fake ``run`` seam.  NOTHING here executes podman."""

    def __init__(self, tmp, *, gpu=False, owning_exists=True,
                 user_override=False, drop_gpu_overlays=False):
        self.root = Path(tmp) / "root"
        self.infra = self.root / "infrastructure"
        self.infra.mkdir(parents=True, exist_ok=True)
        (self.infra / "docker-compose.yml").write_text(BASE_COMPOSE, encoding="utf-8")
        if gpu and not drop_gpu_overlays:
            (self.infra / "docker-compose.gpu.yml").write_text(
                GPU_SUBCOMMAND_OVERLAY, encoding="utf-8")
        if user_override:
            (self.infra / "compose.override.yaml").write_text(
                "# my own hand-written override\nservices:\n  weaviate:\n"
                "    environment:\n      QUERY_DEFAULTS_LIMIT: 100\n",
                encoding="utf-8")

        self.owning_dir = Path(tmp) / "owning"
        if owning_exists:
            self.owning_dir.mkdir(parents=True, exist_ok=True)
            (self.owning_dir / "compose.yaml").write_text(
                OWNING_COMPOSE_GPU if gpu else OWNING_COMPOSE, encoding="utf-8")
            (self.owning_dir / "compose.override.yaml").write_text(
                OWNING_OVERRIDE, encoding="utf-8")

        # container → compose project (flips when an `up` re-creates it)
        self.projects = {name: OWNING_PROJECT
                         for name in OWNING_PROJECT_CONTAINERS}
        self.working_dirs = {name: str(self.owning_dir)
                             for name in OWNING_PROJECT_CONTAINERS}
        self.config_files = {name: "compose.yaml,compose.override.yaml"
                             for name in OWNING_PROJECT_CONTAINERS}

        # failure injection (all default-off)
        self.fail_up_stderr = {}        # service → stderr (up fails, no retry)
        self.label_refuse_services = set()  # first up → network-label refusal
        self.fail_mounts_for = set()    # containers whose Mounts probe fails
        self.config_rc = 0              # `compose config` parse probe

        self.fetched = []               # health URLs probed
        self.all_argv = []              # EVERY runtime call, in order
        self._label_refused = set()

    # -- the run seam ------------------------------------------------------

    def run(self, argv, **kwargs):
        argv = list(argv)
        self.all_argv.append(list(argv))
        rest = argv[1:]
        if rest[0] == "inspect":
            i = rest.index("--format")
            return self._inspect(rest[i + 1], rest[i + 2])
        if rest[0] == "ps":
            return _cp(argv, 0, "\n".join(OWNING_PROJECT_CONTAINERS) + "\n")
        if rest[0] == "stop":
            return _cp(argv)
        if rest[0] == "rm":
            return _cp(argv)
        if rest[0] == "network" and rest[1] == "inspect":
            return _cp(argv, 0, json.dumps(
                [{"Name": rest[2], "Containers": {}}]))
        if rest[0] == "network" and rest[1] == "rm":
            return _cp(argv)
        if rest[0] == "compose":
            return self._compose(argv)
        raise AssertionError(f"unexpected runtime argv in tests: {argv}")

    def _inspect(self, fmt, ref):
        if "com.docker.compose.project" in fmt:
            return _cp([], 0, "{}\t{}\t{}\n".format(
                self.projects.get(ref, ""),
                self.working_dirs.get(ref, ""),
                self.config_files.get(ref, "")))
        if fmt == "{{json .Mounts}}":
            if ref in self.fail_mounts_for:
                return _cp([], 125, "", "no such container")
            return _cp([], 0, json.dumps(LIVE[ref]["mounts"]))
        if fmt == "{{json .Config.Env}}":
            return _cp([], 0, json.dumps(LIVE[ref]["env"]))
        if fmt == "{{json .NetworkSettings.Networks}}":
            return _cp([], 0, json.dumps(
                {"vibecoded-network": {"NetworkID": "abc"}}))
        if fmt == "{{json .Config.Healthcheck}}":
            return _cp([], 0, json.dumps(HEALTHCHECKS[ref]))
        if fmt == "{{json .HostConfig.RestartPolicy}}":
            return _cp([], 0, json.dumps({"Name": "always"}))
        if fmt == "{{json .HostConfig.PortBindings}}":
            return _cp([], 0, json.dumps(LIVE[ref]["ports"]))
        if fmt == "{{json .HostConfig.Devices}}":
            return _cp([], 0, json.dumps([]))
        raise AssertionError(f"unexpected inspect format in tests: {fmt}")

    def _compose(self, argv):
        if argv[-1] == "config":
            return _cp(argv, self.config_rc, "", "" if self.config_rc == 0
                       else "services.ollama.devices must be a list")
        service = argv[-1]
        project = argv[argv.index("-p") + 1]
        if service in self.label_refuse_services and service not in self._label_refused:
            self._label_refused.add(service)
            return _cp(argv, 1, "",
                       f'network {OWN_PROJECT}_default was found but has '
                       'incorrect label com.docker.compose.network set to '
                       '"" (expected: "default")')
        if service in self.fail_up_stderr:
            return _cp(argv, 1, "", self.fail_up_stderr[service])
        # a successful up re-creates the service's container under `project`
        self.projects[SERVICE_TO_CONTAINER[service]] = project
        return _cp(argv)

    # -- the other seams ---------------------------------------------------

    def find_existing_container(self, service, runtime="podman"):
        return SERVICE_TO_CONTAINER.get(service)

    def fetch(self, url, timeout):
        self.fetched.append(url)
        return 200

    def resolution(self):
        return SimpleNamespace(compose=["podman", "compose"],
                               compose_form="subcommand")

    def adopt(self, run=None, **kw):
        kw.setdefault("resolution", self.resolution())
        kw.setdefault("fetch", self.fetch)
        lines: list = []
        kw.setdefault("log", lines.append)
        with mock.patch.object(containers, "find_existing_container",
                               self.find_existing_container), \
                mock.patch.object(containers, "_resolve_runtime",
                                  return_value="podman"), \
                mock.patch.object(service_adoption, "_derive_embed_config",
                                  return_value={"gpu_vendor": "nvidia",
                                                "code_backend": "gpu"}):
            result = service_adoption.adopt_services(
                self.root, run=run or self.run, **kw)
        return result, lines


class _TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="vco_v0296_adopt_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        # Redirect ~/.vct so services.toml IO can never touch the real one.
        patcher = mock.patch.dict(os.environ,
                                  {"VCT_STATE_DIR": str(Path(self._tmp) / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_world(self, **kw):
        return World(self._tmp, **kw)


# ===========================================================================
# Module-level helpers (Task 1 + install.py wrappers)
# ===========================================================================

class OverrideFChainTests(unittest.TestCase):
    def test_stock_install_has_empty_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            infra = Path(tmp)
            (infra / "docker-compose.yml").write_text("name: infrastructure\n")
            self.assertEqual(install_services_guard.override_f_chain(infra), [])

    def test_present_overrides_become_f_fragments_in_canonical_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            infra = Path(tmp)
            one = infra / "compose.override.yaml"
            two = infra / "docker-compose.override.yml"
            one.write_text("# managed\n")
            two.write_text("# managed\n")
            self.assertEqual(
                install_services_guard.override_f_chain(infra),
                ["-f", str(one), "-f", str(two)],
            )


class ServicesTomlIOTests(_TempCase):
    def test_drop_rows_removes_only_named_and_keeps_the_rest(self):
        path = Path(self._tmp) / "services.toml"
        service_adoption.write_services_toml({"services": [
            {"name": "weaviate", "mode": "vco-managed"},
            {"name": "ollama", "mode": "vco-managed"},
            {"name": "model_router", "mode": "external",
             "external_url": "http://localhost:11436"},
        ]}, path=path)
        dropped = service_adoption.drop_services_toml_rows(
            ["weaviate", "ollama"], path=path)
        self.assertEqual(dropped, 2)
        rows = service_adoption.read_services_toml(path)["services"]
        self.assertEqual([r["name"] for r in rows], ["model_router"])
        self.assertEqual(rows[0]["external_url"], "http://localhost:11436")

    def test_missing_file_is_empty_and_drop_is_a_noop(self):
        path = Path(self._tmp) / "services.toml"
        self.assertEqual(service_adoption.read_services_toml(path),
                         {"services": []})
        self.assertEqual(
            service_adoption.drop_services_toml_rows(["weaviate"], path=path), 0)


class MergeComposeSemanticsTests(unittest.TestCase):
    """The axes adoption compares observe compose's REAL merge rules:
    service volume/ports lists REPLACE; environment/networks maps merge."""

    def test_volume_list_replaces(self):
        base = {"services": {"ollama": {
            "volumes": ["ollama_data:/root/.ollama"],
            "environment": {"A": "1"}}}}
        over = {"services": {"ollama": {
            "volumes": ["/bind/path:/root/.ollama:Z"]}}}
        merged = service_adoption.merge_compose(base, over)
        self.assertEqual(merged["services"]["ollama"]["volumes"],
                         ["/bind/path:/root/.ollama:Z"])

    def test_environment_map_merges_per_key(self):
        base = {"services": {"ollama": {"environment": {"A": "1", "B": "2"}}}}
        over = {"services": {"ollama": {"environment": {"B": "3", "C": "4"}}}}
        merged = service_adoption.merge_compose(base, over)
        self.assertEqual(merged["services"]["ollama"]["environment"],
                         {"A": "1", "B": "3", "C": "4"})

    def test_top_level_volumes_block_merges(self):
        base = {"volumes": {"weaviate_data": {"name": "vco_weaviate_data"}}}
        over = {"volumes": {"code_embed_cache": {"external": True,
                                                 "name": "code_embed_data"}}}
        merged = service_adoption.merge_compose(base, over)
        self.assertIn("weaviate_data", merged["volumes"])
        self.assertIn("code_embed_cache", merged["volumes"])


class GpuOverlayFormTests(unittest.TestCase):
    def test_file_selected_by_compose_form_not_runtime_name(self):
        self.assertEqual(
            service_adoption.gpu_overlay_for_form("subcommand"),
            "docker-compose.gpu.yml")
        self.assertEqual(
            service_adoption.gpu_overlay_for_form("standalone"),
            "podman-compose.gpu.yml")

    def test_unknown_form_has_no_overlay(self):
        self.assertIsNone(service_adoption.gpu_overlay_for_form(None))
        self.assertIsNone(service_adoption.gpu_overlay_for_form("weird"))


# ===========================================================================
# Reconciliation units (the identity matrix)
# ===========================================================================

class _LiveCase(_TempCase):
    def plan_for(self, world):
        with mock.patch.object(containers, "find_existing_container",
                               world.find_existing_container), \
                mock.patch.object(containers, "_resolve_runtime",
                                  return_value="podman"):
            plans, files = service_adoption._plan_all(
                world.root, "podman", world.run, lambda m: None,
                resolution=world.resolution())
        return {p.service: p for p in plans}, files


class ReconcileFragmentsTests(_LiveCase):
    """The m-5 pins: mounts stay live-authoritative, env is fix-authoritative
    with the live value as the floor."""

    def test_identity_matrix_fragments(self):
        world = self.make_world()
        plans, _ = self.plan_for(world)
        # weaviate: identical resolved volume name → NO mounts fragment
        wea = plans["weaviate"].override_fragments
        self.assertEqual(wea["mounts"], [])
        self.assertEqual(wea["volume_aliases"], {})
        self.assertEqual(wea["environment"], {})
        # ollama: the bind, carried byte-for-byte
        oll = plans["ollama"].override_fragments
        self.assertIn(f"{OLLAMA_BIND}:/root/.ollama:Z", oll["mounts"])
        self.assertEqual(oll["volume_aliases"], {})
        # KEEP_ALIVE canonical 24h — NOT the foreign home's 30s
        self.assertEqual(oll["environment"].get("OLLAMA_KEEP_ALIVE"), "24h")
        # live-only key the installer does not set is carried (floor)
        self.assertEqual(oll["environment"].get("OLLAMA_FLASH_ATTENTION"), "1")
        # code_embed: differently-named external volume → alias, no bind
        cod = plans["code_embed"].override_fragments
        self.assertEqual(cod["mounts"], ["code_embed_cache:/cache"])
        self.assertEqual(cod["volume_aliases"],
                         {"code_embed_cache": "code_embed_data"})
        # equal effective/live values produce NO env fragment
        self.assertNotIn("CODE_EMBED_MODEL", cod["environment"])
        self.assertNotIn("CODE_EMBED_BACKEND", cod["environment"])

    def test_healthcheck_is_reconstructed_from_live_ns_to_s(self):
        world = self.make_world()
        plans, _ = self.plan_for(world)
        hc = plans["weaviate"].override_fragments["healthcheck"]
        self.assertEqual(hc["interval"], "30s")
        self.assertEqual(hc["timeout"], "5s")
        self.assertEqual(hc["retries"], 5)
        self.assertEqual(hc["test"][0], "CMD-SHELL")

    def test_restart_and_network_leg_are_carried(self):
        world = self.make_world()
        plans, _ = self.plan_for(world)
        for svc in ("weaviate", "ollama", "code_embed"):
            frag = plans[svc].override_fragments
            self.assertEqual(frag["restart"], "always")
            self.assertEqual(frag["networks"], {"vibecoded-network": None})


class VerifyFinalMountsTests(_LiveCase):
    """The hard data-plane gate — the plan's red-proof target: neutralize
    :func:`service_adoption._verify_final_mounts` (or the shared
    :func:`service_adoption._mount_problems` core) and these go red."""

    def _base_doc(self):
        world = self.make_world()
        doc = service_adoption.load_compose_doc(
            world.infra / "docker-compose.yml", {})
        assert doc is not None
        return world, doc["services"], doc["volumes"]

    def _live(self, world, ref):
        live = service_adoption.live_service_state(ref, "podman", world.run)
        assert live is not None
        return live

    def test_correct_alias_passes(self):
        world, services, top = self._base_doc()
        live = self._live(world, "vco_code_embed")
        self.assertEqual(
            service_adoption._verify_final_mounts(
                {"volume_aliases": {"code_embed_cache": "code_embed_data"}},
                services["code_embed"], top, live, "code_embed"),
            [])

    def test_missing_alias_is_a_named_volume_name_refusal(self):
        world, services, top = self._base_doc()
        live = self._live(world, "vco_code_embed")
        problems = service_adoption._verify_final_mounts(
            {"volume_aliases": {}}, services["code_embed"], top, live,
            "code_embed")
        self.assertTrue(problems)
        self.assertIn("would become vco_code_embed_cache", problems[0])
        self.assertIn("code_embed_data", problems[0])

    def test_wrong_alias_name_is_refused(self):
        world, services, top = self._base_doc()
        live = self._live(world, "vco_code_embed")
        problems = service_adoption._verify_final_mounts(
            {"volume_aliases": {"code_embed_cache": "some_other_volume"}},
            services["code_embed"], top, live, "code_embed")
        self.assertTrue(problems)
        self.assertIn("would become some_other_volume", problems[0])

    def test_named_volume_replacement_of_a_live_bind_is_a_hard_refusal(self):
        """A final config that turns the 110 GB bind into a named volume is
        refused BY NAME — the exact catastrophic replacement the gate
        exists to stop."""
        world, services, top = self._base_doc()
        live = self._live(world, "vco_ollama")
        problems = service_adoption._mount_problems(
            services["ollama"], top, live, "ollama")
        self.assertEqual(len(problems), 1)
        self.assertIn("live bind would become volume", problems[0])
        self.assertIn("named-volume replacement attempt", problems[0])
        self.assertIn("/root/.ollama", problems[0])

    def test_mount_the_live_container_lacks_is_flagged_as_added(self):
        world, services, top = self._base_doc()
        live = self._live(world, "vco_ollama")
        cfg = dict(services["ollama"])
        cfg["volumes"] = ["ollama_data:/root/.ollama", "ollama_data:/extra"]
        problems = service_adoption._mount_problems(cfg, top, live, "ollama")
        self.assertTrue(any("would be ADDED" in p for p in problems))


# ===========================================================================
# The adoption flow (end-to-end, fully mocked)
# ===========================================================================

class AdoptionFlowTests(_TempCase):
    # -- happy path --------------------------------------------------------

    def test_happy_path_adopts_in_order_and_writes_both_overrides(self):
        world = self.make_world()
        result, _ = world.adopt()
        self.assertEqual(result.adopted, ["weaviate", "ollama", "code_embed"])
        self.assertEqual(result.failed, {})
        self.assertEqual(result.refused, {})
        for name in ("compose.override.yaml", "docker-compose.override.yml"):
            target = world.infra / name
            self.assertTrue(target.is_file(), name)
            text = target.read_text(encoding="utf-8")
            self.assertIn(service_adoption._OVERRIDE_MANAGED_MARKER, text)
        # atomic write left no temp files behind
        self.assertEqual(list(world.infra.glob("*.tmp")), [])

    def test_written_override_carries_the_reconciliation(self):
        world = self.make_world()
        result, _ = world.adopt()
        self.assertEqual(result.adopted, ["weaviate", "ollama", "code_embed"])
        doc = yaml.safe_load(
            (world.infra / "compose.override.yaml").read_text(encoding="utf-8"))
        svcs = doc["services"]
        # the 110 GB bind, pinned byte-for-byte, as a BIND (never a volume)
        self.assertEqual(svcs["ollama"]["volumes"],
                         [f"{OLLAMA_BIND}:/root/.ollama:Z"])
        # the canonical fix value, not the foreign home's 30s
        self.assertEqual(svcs["ollama"]["environment"]["OLLAMA_KEEP_ALIVE"],
                         "24h")
        self.assertEqual(
            svcs["ollama"]["environment"]["OLLAMA_FLASH_ATTENTION"], "1")
        # identical-volume service gets NO volumes stanza
        self.assertNotIn("volumes", svcs["weaviate"])
        # differently-named volume → external alias in the top-level block
        self.assertEqual(svcs["code_embed"]["volumes"],
                         ["code_embed_cache:/cache"])
        self.assertEqual(doc["volumes"]["code_embed_cache"],
                         {"external": True, "name": "code_embed_data"})
        # the model-router network leg: external network + service alias
        self.assertEqual(doc["networks"],
                         {"vibecoded-network": {"external": True}})
        for svc in ("weaviate", "ollama", "code_embed"):
            nets = svcs[svc]["networks"]
            self.assertIn("default", nets)
            self.assertEqual(nets["vibecoded-network"]["aliases"], [svc])

    def test_merged_final_config_mounts_ollama_bind_byte_for_byte(self):
        """End-to-end: re-merge the WRITTEN override with the base file the
        way compose would, and require the ollama mount to still BE the
        bind.  This pins the rendered artifact itself (a reconcile bug that
        drops the bind fragment cannot pass here even if the verify gates
        were neutralized)."""
        world = self.make_world()
        result, _ = world.adopt()
        self.assertEqual(len(result.adopted), 3)
        env = service_adoption.infrastructure_env_for_substitution(world.infra)
        base = service_adoption.load_compose_doc(
            world.infra / "docker-compose.yml", env)
        over = service_adoption.load_compose_doc(
            world.infra / "compose.override.yaml", env)
        assert base is not None and over is not None
        merged = service_adoption.merge_compose(base, over)
        mounts = service_adoption.config_mounts(
            merged["services"]["ollama"], merged.get("volumes") or {})
        spec = mounts["/root/.ollama"]
        self.assertEqual(spec.kind, "bind")
        self.assertEqual(spec.source, OLLAMA_BIND)
        self.assertEqual(spec.options, "Z")
        # code_embed keeps its live volume NAME through the alias
        cod = service_adoption.config_mounts(
            merged["services"]["code_embed"], merged.get("volumes") or {})
        self.assertEqual(cod["/cache"].kind, "volume")
        self.assertEqual(cod["/cache"].source, "code_embed_data")
        # weaviate is unchanged
        wea = service_adoption.config_mounts(
            merged["services"]["weaviate"], merged.get("volumes") or {})
        self.assertEqual(wea["/var/lib/weaviate"].source, "vco_weaviate_data")

    def test_rendered_config_verification_refuses_a_dropped_reconciliation(self):
        """The RENDERED-side gate: a renderer bug that loses the code_embed
        volume reconciliation (a named-volume replacement in the making)
        refuses the service before anything is written or stopped."""
        world = self.make_world()
        real_render = service_adoption.render_adoption_override

        def buggy_render(plans):
            doc = yaml.safe_load(real_render(plans))
            doc["services"]["code_embed"].pop("volumes", None)
            doc["volumes"].pop("code_embed_cache", None)
            return yaml.safe_dump(doc, default_flow_style=False, sort_keys=True)

        with mock.patch.object(service_adoption, "render_adoption_override",
                               buggy_render):
            result, _ = world.adopt()
        self.assertEqual(result.adopted, ["weaviate", "ollama"])
        self.assertIn("would become vco_code_embed_cache",
                      result.refused["code_embed"])
        # the refused service's container was never touched
        for argv in result.argv_log:
            if len(argv) > 2:
                self.assertNotEqual(argv[2], "vco_code_embed")
            if argv[-1] == "code_embed":
                self.fail(f"refused service was brought up: {argv}")

    def test_up_runs_under_installer_project_with_the_override_in_the_chain(self):
        world = self.make_world()
        result, _ = world.adopt()
        self.assertEqual(len(result.adopted), 3)
        ups = [a for a in result.argv_log
               if "up" in a and "-p" in a and a[-1] in SERVICE_TO_CONTAINER]
        self.assertEqual(len(ups), 3)
        for up in ups:
            self.assertEqual(up[up.index("-p") + 1], OWN_PROJECT)
            self.assertIn("-f", up)
            files = [up[i + 1] for i, tok in enumerate(up) if tok == "-f"]
            self.assertIn(str(world.infra / "docker-compose.yml"), files)
            # the freshly generated override IS in the explicit -f chain
            self.assertIn(str(world.infra / "compose.override.yaml"), files)
            self.assertIn(str(world.infra / "docker-compose.override.yml"),
                          files)
            self.assertIn("--no-deps", up)

    def test_never_touches_volumes_or_downs_the_project(self):
        world = self.make_world()
        result, _ = world.adopt()
        adopted_containers = set(SERVICE_TO_CONTAINER.values())
        for argv in result.argv_log:
            self.assertNotIn("down", argv)
            self.assertNotEqual(argv[1:2], ["volume"])
            self.assertNotEqual(argv[1:2], ["network"])
            if argv[1:2] == ["rm"]:
                self.assertIn(argv[2], adopted_containers)
            if argv[1:2] == ["stop"]:
                self.assertIn(argv[2], adopted_containers)
        # every adopted container was stopped and removed exactly once
        stops = {a[2] for a in result.argv_log if a[1:2] == ["stop"]}
        rms = {a[2] for a in result.argv_log if a[1:2] == ["rm"]}
        self.assertEqual(stops, adopted_containers)
        self.assertEqual(rms, adopted_containers)

    def test_health_probes_hit_the_live_host_ports(self):
        world = self.make_world()
        result, _ = world.adopt()
        self.assertEqual(len(result.adopted), 3)
        self.assertIn("http://localhost:8081/v1/.well-known/ready",
                      world.fetched)
        self.assertIn("http://localhost:11435/api/tags", world.fetched)
        self.assertIn("http://localhost:11440/health", world.fetched)

    def test_collateral_containers_are_named_in_the_warning(self):
        world = self.make_world()
        _, lines = world.adopt()
        joined = "\n".join(lines)
        self.assertIn("[collateral]", joined)
        self.assertIn("vco_model_router", joined)
        self.assertIn("aliases", joined)

    def test_post_adoption_container_labels_read_our_project(self):
        world = self.make_world()
        result, _ = world.adopt()
        self.assertEqual(len(result.adopted), 3)
        for name in SERVICE_TO_CONTAINER.values():
            self.assertEqual(world.projects[name], OWN_PROJECT)
        # the services that STAY keep the owning project
        self.assertEqual(world.projects["vco_model_router"], OWNING_PROJECT)

    def test_services_toml_rows_drop_for_adopted_only(self):
        world = self.make_world()
        state = service_adoption.services_toml_path()
        state.parent.mkdir(parents=True, exist_ok=True)
        service_adoption.write_services_toml({"services": [
            {"name": "weaviate", "mode": "vco-managed"},
            {"name": "ollama", "mode": "vco-managed"},
            {"name": "code_embed", "mode": "vco-managed"},
            {"name": "model_router", "mode": "external",
             "external_url": "http://localhost:11436"},
        ]})
        result, _ = world.adopt()
        self.assertEqual(len(result.adopted), 3)
        rows = service_adoption.read_services_toml()["services"]
        self.assertEqual([r["name"] for r in rows], ["model_router"])

    def test_dry_run_touches_nothing(self):
        world = self.make_world()
        result, _ = world.adopt(dry_run=True)
        self.assertEqual(result.adopted, [])
        self.assertEqual(result.argv_log, [])
        self.assertFalse((world.infra / "compose.override.yaml").exists())
        self.assertFalse((world.infra / "docker-compose.override.yml").exists())

    # -- rollback (constraints #9) ----------------------------------------

    def test_failed_service_rolls_back_under_owner_and_leaves_the_rest(self):
        world = self.make_world()
        world.fail_up_stderr = {
            "weaviate": "Error: sandbox setup failed: invalid mount config"}
        result, _ = world.adopt()
        self.assertEqual(result.adopted, [])
        self.assertEqual(list(result.failed), ["weaviate"])
        self.assertIn("sandbox setup failed", result.failed["weaviate"])
        # rollback re-created weaviate under the OWNING invocation
        rollback = [a for a in result.argv_log
                    if "up" in a and a[-1] == "weaviate"
                    and a[a.index("-p") + 1] == OWNING_PROJECT]
        self.assertEqual(len(rollback), 1)
        argv = rollback[0]
        files = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-f"]
        self.assertEqual(files, [str(world.owning_dir / "compose.yaml"),
                                 str(world.owning_dir / "compose.override.yaml")])
        self.assertEqual(world.projects["vco_weaviate"], OWNING_PROJECT)
        # N+1 untouched: no stop/rm/up for ollama or code_embed
        for argv in result.argv_log:
            if len(argv) > 2 and argv[2] in ("vco_ollama", "vco_code_embed"):
                self.fail(f"later service touched after failure: {argv}")
            if argv[-1] in ("ollama", "code_embed") and "up" in argv:
                self.fail(f"later service brought up after failure: {argv}")

    def test_failure_at_ollama_leaves_code_embed_untouched(self):
        world = self.make_world()
        world.fail_up_stderr = {"ollama": "Error: port 11435 already bound"}
        result, _ = world.adopt()
        self.assertEqual(result.adopted, ["weaviate"])
        self.assertEqual(list(result.failed), ["ollama"])
        self.assertEqual(world.projects["vco_code_embed"], OWNING_PROJECT)
        for argv in result.argv_log:
            self.assertNotEqual(argv[-1], "code_embed")

    # -- the mixed-provider stale-network-label refusal --------------------

    def test_network_label_refusal_is_retried_once_after_network_rm(self):
        world = self.make_world()
        world.label_refuse_services = {"weaviate"}
        result, _ = world.adopt()
        self.assertEqual(result.adopted, ["weaviate", "ollama", "code_embed"])
        ups = [a for a in result.argv_log
               if "up" in a and a[-1] == "weaviate"]
        self.assertEqual(len(ups), 2)  # refused once, retried once
        insp = [a for a in result.argv_log if a[1:3] == ["network", "inspect"]]
        rm = [a for a in result.argv_log if a[1:3] == ["network", "rm"]]
        self.assertEqual(len(insp), 1)
        self.assertEqual(insp[0][3], f"{OWN_PROJECT}_default")
        self.assertEqual(len(rm), 1)
        self.assertEqual(rm[0][3], f"{OWN_PROJECT}_default")

    def test_network_with_attached_containers_is_never_removed(self):
        world = self.make_world()
        world.label_refuse_services = {"weaviate"}
        calls = []
        real_run = world.run

        def guarded(argv, **kw):
            if argv[1:3] == ["network", "inspect"]:
                calls.append(list(argv))
                return _cp(argv, 0, json.dumps(
                    [{"Name": argv[3],
                      "Containers": {"abc": {"Name": "vco_model_router"}}}]))
            return real_run(argv, **kw)

        result, _ = world.adopt(run=guarded)
        # no retry rescue: the up failed, the service rolled back, the
        # ATTACHED network was inspected but NOT removed
        self.assertEqual(list(result.failed), ["weaviate"])
        self.assertEqual(result.adopted, [])
        self.assertEqual(world.projects["vco_ollama"], OWNING_PROJECT)
        self.assertEqual(calls, [["podman", "network", "inspect",
                                  f"{OWN_PROJECT}_default"]])
        self.assertNotIn(["podman", "network", "rm", f"{OWN_PROJECT}_default"],
                         result.argv_log)

    # -- refusals: unreconcilable stays foreign ----------------------------

    def test_owning_files_missing_means_nothing_is_touched(self):
        world = self.make_world(owning_exists=False)
        result, _ = world.adopt()
        self.assertEqual(result.adopted, [])
        self.assertEqual(result.failed, {})
        self.assertEqual(set(result.refused),
                         {"weaviate", "ollama", "code_embed"})
        for why in result.refused.values():
            self.assertIn("owning config files missing", why)
        self.assertEqual(result.argv_log, [])
        self.assertFalse((world.infra / "compose.override.yaml").exists())

    def test_unreadable_live_state_refuses_only_that_service(self):
        world = self.make_world()
        world.fail_mounts_for = {"vco_ollama"}
        state = service_adoption.services_toml_path()
        state.parent.mkdir(parents=True, exist_ok=True)
        service_adoption.write_services_toml({"services": [
            {"name": "weaviate", "mode": "vco-managed"},
            {"name": "ollama", "mode": "vco-managed"},
            {"name": "model_router", "mode": "external"},
        ]})
        result, _ = world.adopt()
        self.assertEqual(result.adopted, ["weaviate", "code_embed"])
        self.assertIn("could not positively read", result.refused["ollama"])
        # the refused service's container is never touched
        for argv in result.argv_log:
            if len(argv) > 2:
                self.assertNotEqual(argv[2], "vco_ollama")
            if argv[-1] == "ollama":
                self.fail(f"refused service was brought up: {argv}")
        # its services.toml row SURVIVES (it is still foreign)
        rows = service_adoption.read_services_toml()["services"]
        self.assertEqual(sorted(r["name"] for r in rows),
                         ["model_router", "ollama"])

    def test_user_authored_override_is_never_clobbered(self):
        world = self.make_world(user_override=True)
        before = (world.infra / "compose.override.yaml").read_text(
            encoding="utf-8")
        result, _ = world.adopt()
        self.assertEqual(result.adopted, [])
        self.assertEqual(result.failed, {})
        self.assertEqual(len(result.refused), 3)
        for why in result.refused.values():
            self.assertIn("user-authored override", why)
        self.assertEqual(
            (world.infra / "compose.override.yaml").read_text(encoding="utf-8"),
            before)
        self.assertFalse((world.infra / "docker-compose.override.yml").exists())
        self.assertEqual(result.argv_log, [])

    # -- GPU overlay selection (constraints #11: by compose FORM) ----------

    def test_gpu_overlay_by_form_enters_the_chain_when_it_parses(self):
        world = self.make_world(gpu=True)
        result, _ = world.adopt()
        self.assertEqual(result.adopted, ["weaviate", "ollama", "code_embed"])
        ups = [a for a in result.argv_log
               if "up" in a and a[-1] in ("ollama", "code_embed")]
        self.assertEqual(len(ups), 2)
        for up in ups:
            files = [up[i + 1] for i, tok in enumerate(up) if tok == "-f"]
            self.assertIn(str(world.infra / "docker-compose.gpu.yml"), files)
        # the parse probe ran BEFORE anything was stopped
        all_argv = [a for a in world.all_argv]
        first_config = next(i for i, a in enumerate(all_argv)
                            if a[-1] == "config")
        first_stop = next(i for i, a in enumerate(all_argv)
                          if a[1:2] == ["stop"])
        self.assertLess(first_config, first_stop)

    def test_unparseable_gpu_overlay_refuses_the_gpu_services(self):
        world = self.make_world(gpu=True)
        world.config_rc = 1
        result, _ = world.adopt()
        self.assertEqual(result.adopted, ["weaviate"])
        self.assertEqual(set(result.refused), {"ollama", "code_embed"})
        for why in result.refused.values():
            self.assertIn("does not parse", why)
        for argv in result.argv_log:
            self.assertNotEqual(argv[-1], "ollama")
            self.assertNotEqual(argv[-1], "code_embed")

    def test_missing_overlay_for_the_form_refuses_the_gpu_services(self):
        world = self.make_world(gpu=True, drop_gpu_overlays=True)
        result, _ = world.adopt()
        self.assertEqual(result.adopted, ["weaviate"])
        self.assertEqual(set(result.refused), {"ollama", "code_embed"})
        for why in result.refused.values():
            self.assertIn("no overlay matches the compose form", why)
        # the refused GPU services were never touched (weaviate was)
        for argv in result.argv_log:
            if len(argv) > 2:
                self.assertNotIn(argv[2], ("vco_ollama", "vco_code_embed"))
            if argv[-1] in ("ollama", "code_embed"):
                self.fail(f"refused GPU service was brought up: {argv}")


# ===========================================================================
# Task 2 + 3a — the deferral remedy and the owning-name rebuild
# ===========================================================================

class GuardEntryTests(_TempCase):
    FOREIGN = {
        "weaviate": ("container 'vco_weaviate' was created by compose project "
                     "'vibecoded' in /home/u/claude_mcp_servers "
                     "(compose.yaml,compose.override.yaml), not by project "
                     "'infrastructure'"),
        "code_embed": ("container 'vco_code_embed' was created by compose "
                       "project 'vibecoded' in /home/u/claude_mcp_servers "
                       "(compose.yaml,compose.override.yaml), not by project "
                       "'infrastructure'"),
    }
    IDENTITY = containers.ComposeIdentity(
        "vibecoded", "/home/u/claude_mcp_servers",
        "compose.yaml,compose.override.yaml")

    def _infra(self):
        infra = Path(self._tmp) / "root" / "infrastructure"
        infra.mkdir(parents=True, exist_ok=True)
        (infra / "docker-compose.yml").write_text("name: infrastructure\n",
                                                  encoding="utf-8")
        return infra

    def test_same_condition_id_and_adoption_command_first(self):
        entry = install_services_guard.build_foreign_compose_identity_entry(
            self.FOREIGN, "podman", self._infra(),
            identities={"weaviate": self.IDENTITY,
                        "code_embed": self.IDENTITY})
        self.assertEqual(entry.condition_id, CID_FOREIGN)
        cmd = entry.command_to_apply
        self.assertIn("RECOMMENDED", cmd)
        self.assertIn(
            f"python -m vco_lib.service_adoption adopt-services --root "
            f"{Path(self._tmp) / 'root'}", cmd)
        # the manual fallback is still documented
        self.assertIn("Option B", cmd)
        self.assertIn("python install.py --update", cmd)
        # never a delete command in a remedy
        self.assertNotIn(" rm ", cmd)

    def test_option_a_derives_the_owning_name_rebuild(self):
        entry = install_services_guard.build_foreign_compose_identity_entry(
            self.FOREIGN, "podman", self._infra(),
            identities={"weaviate": self.IDENTITY,
                        "code_embed": self.IDENTITY})
        cmd = entry.command_to_apply
        self.assertIn("-p vibecoded", cmd)
        self.assertIn("-f /home/u/claude_mcp_servers/compose.yaml", cmd)
        self.assertIn(
            "-f /home/u/claude_mcp_servers/compose.override.yaml", cmd)
        self.assertIn("--force-recreate", cmd)
        # ALL the foreign services in one owning-side rebuild
        self.assertIn("--profile gpu up -d --build --force-recreate "
                      "code_embed weaviate", cmd)

    def test_without_identities_the_v0293_fallback_shape_is_kept(self):
        entry = install_services_guard.build_foreign_compose_identity_entry(
            self.FOREIGN, "podman", self._infra())
        cmd = entry.command_to_apply
        self.assertIn("cd <working dir shown above>", cmd)
        # the adoption command still leads
        self.assertIn("adopt-services", cmd)

    def test_foreign_owned_services_fills_the_out_param(self):
        infra = self._infra()
        compose_file = infra / "docker-compose.yml"
        with mock.patch.object(containers, "find_existing_container",
                               return_value="vco_code_embed"), \
                mock.patch.object(containers, "_resolve_runtime",
                                  return_value="podman"), \
                mock.patch.object(containers, "compose_identity_of",
                                  return_value=self.IDENTITY):
            identities = {}
            foreign = install_services_guard.foreign_owned_services(
                ["code_embed"], "podman", infra, compose_file,
                identities=identities)
            foreign_noparam = install_services_guard.foreign_owned_services(
                ["code_embed"], "podman", infra, compose_file)
        self.assertEqual(set(foreign), {"code_embed"})
        self.assertEqual(foreign_noparam, foreign)  # backward compatible
        self.assertEqual(identities["code_embed"], self.IDENTITY)


class RebuildCommandOwningNameTests(unittest.TestCase):
    IDENTITY = containers.ComposeIdentity(
        "vibecoded", "/home/u/claude_mcp_servers",
        "compose.yaml,compose.override.yaml")

    def test_services_parameter_extends_the_recreate_list(self):
        cmd = code_embed_image.rebuild_command(
            "/any/root", "podman compose", self.IDENTITY,
            services=["code_embed", "weaviate"])
        self.assertTrue(
            cmd.endswith("--profile gpu up -d --build --force-recreate "
                         "code_embed weaviate"), cmd)
        self.assertIn("-p vibecoded", cmd)
        self.assertIn("-f /home/u/claude_mcp_servers/compose.yaml", cmd)

    def test_default_shape_stays_byte_identical_single_service(self):
        cmd = code_embed_image.rebuild_command(
            "/any/root", "podman compose", self.IDENTITY)
        self.assertTrue(cmd.endswith(
            "--profile gpu up -d --build --force-recreate code_embed"), cmd)
        self.assertNotIn("weaviate", cmd)

    def test_no_identity_still_uses_the_installers_infrastructure(self):
        cmd = code_embed_image.rebuild_command(
            "/any/root", "podman compose", None,
            services=["code_embed", "weaviate"])
        self.assertIn("/any/root/infrastructure", cmd)
        self.assertTrue(cmd.endswith("--force-recreate code_embed weaviate"),
                        cmd)


class PostAdoptDeferralClearTests(_TempCase):
    """Constraint: the adoption adds NO new clearing wiring — the existing
    owned-drop-when-absent contract self-clears the row on the next install
    run that does not re-detect it."""

    def _entry(self):
        return install_services_guard.build_foreign_compose_identity_entry(
            {"code_embed": "container 'x' was created by compose project "
                           "'vibecoded', not by project 'infrastructure'"},
            "podman", Path(self._tmp) / "infra")

    def test_owned_and_absent_from_the_run_report_expires(self):
        report = DeferralReport()  # adoption succeeded → step 5 re-detects nothing
        self.assertTrue(
            owned_record_is_expirable(CID_FOREIGN, report, {CID_FOREIGN}))

    def test_re_detected_this_run_is_kept(self):
        report = DeferralReport()
        report.add_entry(self._entry())  # still foreign → re-emitted
        self.assertFalse(
            owned_record_is_expirable(CID_FOREIGN, report, {CID_FOREIGN}))

    def test_condition_owned_by_another_writer_is_never_expired(self):
        report = DeferralReport()
        self.assertFalse(owned_record_is_expirable(
            "kg_access_phantom_repaired", report, {CID_FOREIGN}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

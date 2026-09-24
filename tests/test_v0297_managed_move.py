# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 lane Z — VCO's OWN Weaviate / Ollama move to a new port with their data.

Plan: ``.claude/context/plans/PLAN-V0297-SERVICE-ENDPOINTS-SSOT-2026-09-24.md``
§4g (the port "move" story) and invariant I2 (every recreate of a
``vco_managed`` service checks the data mount before anything is stopped and
again after the up, and rolls back on a mismatch). Orchestrator decision
(2026-09-24): the owner's "never moved" is about ADOPTED services; VCO's own
Weaviate / Ollama are moved exactly like code-embed —
``service_lifecycle.migrate_managed_service``, reached through
``service_reconcile.move_endpoint``.

Everything runs against a fake machine: one ``run`` that plays the container
runtime AND the compose provider (``compose config`` and ``compose up`` read
``infrastructure/.env`` FROM DISK at call time, the way compose does, so what
the migration wrote is what the provider sees), a fake ``fetch`` that answers
only on the port the container currently publishes, and a fake TCP probe for
Weaviate's gRPC port. No runtime, no real port, no real ``~/.vct``.
"""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest import mock

import yaml

from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib import compose_env, containers, service_adoption
from vco_lib import service_endpoints as se
from vco_lib import service_lifecycle as sl
from vco_lib import service_reconcile as sr
from vco_lib.envfile import parse_env_lines

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_COMPOSE = REPO_ROOT / "infrastructure" / "docker-compose.yml"
INSTALLER = "infrastructure"
OWNER = "vibecoded"
RESOLUTION = SimpleNamespace(compose=["podman", "compose"], compose_form="subcommand")

SPECS = {
    "weaviate": {"container": "vco_weaviate", "dest": "/var/lib/weaviate", "http": "8080",
                 "port_key": "WEAVIATE_PORT", "port": 8081, "grpc_key": "WEAVIATE_GRPC_PORT",
                 "grpc": 50052, "volume_key": "weaviate_data", "volume": "vco_weaviate_data"},
    "ollama": {"container": "vco_ollama", "dest": "/root/.ollama", "http": "11434",
               "port_key": "OLLAMA_PORT", "port": 11435, "volume_key": "ollama_data",
               "volume": "vco_ollama_data"},
}


def _cp(argv, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)


class ServiceWorld:
    """ONE Weaviate or Ollama container and the compose provider around it.

    ``lose_classes_once``: the first ``up`` comes back with an EMPTY class
    list (a recreate that did not bring the data along); later ups — the
    rollback — see the original classes again. ``up_mount_once``: the first
    ``up`` mounts this instead of what the ``.env`` knobs say (a provider
    bug); later ups follow the knobs."""

    def __init__(self, tmp: Path, service: str, *, mount: dict, project: str = INSTALLER,
                 classes: tuple[str, ...] = ()):
        self.service = service
        self.spec = SPECS[service]
        self.root = tmp / "root"
        self.infra = self.root / "infrastructure"
        self.infra.mkdir(parents=True)
        shutil.copy(REAL_COMPOSE, self.infra / "docker-compose.yml")
        self.owning_dir = tmp / "owning"
        self.owning_dir.mkdir()
        self.exists = True
        self.project = project
        self.mount = dict(mount)
        self.port = self.spec["port"]
        self.grpc: Optional[int] = self.spec.get("grpc")
        self.classes = list(classes)
        self.visible_classes = list(classes)
        self.lose_classes_once = False
        self.models = ["qwen3-embedding:0.6b"]
        self.visible_models = list(self.models)
        self.lose_models_once = False
        self.up_mount_once: Optional[dict] = None
        self.all_argv: list[list[str]] = []

    # -- what compose would substitute ----------------------------------------
    def _env(self) -> dict:
        return service_adoption.infrastructure_env_for_substitution(self.infra)

    def _knob_mount(self) -> dict:
        env = self._env()
        source_key, volume_key = compose_env.DATA_KNOBS[self.service]
        if env.get(source_key, ""):
            return {"kind": "bind", "source": env[source_key]}
        return {"kind": "volume", "source": env.get(volume_key, "") or self.spec["volume"]}

    def _env_port(self, key: str, default: int) -> int:
        value = self._env().get(key, "")
        return int(value) if value.isdecimal() else default

    # -- the runtime + provider ------------------------------------------------
    def run(self, argv, **_kw):
        argv = list(argv)
        self.all_argv.append(argv)
        dest, spec = self.spec["dest"], self.spec
        if argv[-1] == "config":
            m = self._knob_mount()
            vol = ({"type": "bind", "source": m["source"], "target": dest} if m["kind"] == "bind"
                   else {"type": "volume", "source": spec["volume_key"], "target": dest})
            doc = {"services": {self.service: {"volumes": [vol]}},
                   "volumes": {spec["volume_key"]: {"name": m["source"] if m["kind"] == "volume"
                                                    else spec["volume"]}}}
            return _cp(argv, 0, yaml.safe_dump(doc))
        if "up" in argv:
            if argv[-1] != self.service:
                raise AssertionError(f"compose up named more than {self.service}: {argv}")
            self.exists = True
            self.project = argv[argv.index("-p") + 1]
            self.mount = self.up_mount_once or self._knob_mount()
            self.up_mount_once = None
            self.port = self._env_port(spec["port_key"], spec["port"])
            if "grpc_key" in spec:
                self.grpc = self._env_port(spec["grpc_key"], spec["grpc"])
            self.visible_classes = [] if self.lose_classes_once else list(self.classes)
            self.lose_classes_once = False
            self.visible_models = [] if self.lose_models_once else list(self.models)
            self.lose_models_once = False
            return _cp(argv)
        verb = argv[1]
        if verb == "inspect":
            ref = argv[-1]
            if ref != spec["container"] or not self.exists:
                return _cp(argv, 125, "", "no such container")
            fmt = argv[argv.index("--format") + 1]
            if fmt == "{{.Id}}":
                return _cp(argv, 0, "abc123\n")
            if "com.docker.compose.project" in fmt:
                wd = str(self.owning_dir) if self.project == OWNER else str(self.infra)
                return _cp(argv, 0, f"{self.project}\t{wd}\tdocker-compose.yml\n")
            if self.mount["kind"] == "bind":
                live_mount = {"Type": "bind", "Source": self.mount["source"], "Destination": dest}
            else:
                live_mount = {"Type": "volume", "Name": self.mount["source"], "Source": "/var/v",
                              "Destination": dest}
            ports = {f"{spec['http']}/tcp": [{"HostPort": str(self.port)}]}
            if self.grpc is not None:
                ports["50051/tcp"] = [{"HostPort": str(self.grpc)}]
            payloads = {
                "{{json .Mounts}}": [live_mount],
                "{{json .Config.Env}}": [],
                "{{json .NetworkSettings.Networks}}": {f"{self.project}_default": {}},
                "{{json .Config.Healthcheck}}": None,
                "{{json .HostConfig.RestartPolicy}}": {"Name": "unless-stopped"},
                "{{json .HostConfig.PortBindings}}": ports,
                "{{json .HostConfig.Devices}}": [],
            }
            return _cp(argv, 0, json.dumps(payloads[fmt]))
        if verb in ("stop", "rm"):
            if verb == "rm":
                self.exists = False
            return _cp(argv)
        raise AssertionError(f"unexpected argv: {argv}")

    # -- the network -------------------------------------------------------------
    def fetch_json(self, url: str, _timeout: float) -> Optional[dict]:
        hostport, _, path = url.split("://", 1)[1].partition("/")
        if not self.exists or int(hostport.rsplit(":", 1)[1]) != self.port:
            return None
        path = "/" + path
        if self.service == "weaviate":
            if path == "/v1/.well-known/ready":
                return {}
            if path == "/v1/schema":
                return {"classes": [{"class": c} for c in self.visible_classes]}
        if self.service == "ollama" and path == "/api/tags":
            return {"models": [{"name": m} for m in self.visible_models]}
        return None

    def tcp_open(self, _host: str, port: int, _timeout: float) -> bool:
        return self.exists and port == self.grpc

    # -- assertions helpers ------------------------------------------------------
    def ups(self) -> list[list[str]]:
        return [a for a in self.all_argv if "up" in a]

    def stops(self) -> list[list[str]]:
        return [a for a in self.all_argv if a[1] in ("stop", "rm")]

    def env_keys(self) -> dict:
        path = self.infra / ".env"
        return dict(parse_env_lines(path.read_text(encoding="utf-8"))) if path.is_file() else {}


def _row(service: str, **kw) -> se.EndpointRow:
    spec = SPECS[service]
    kw.setdefault("port", spec["port"])
    kw.setdefault("source", "install_probe")
    kw.setdefault("container_name", spec["container"])
    if service == "weaviate":
        kw.setdefault("grpc_port", spec["grpc"])
    return se.EndpointRow(service=service, mode="vco_managed", **kw)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vco_laneZ_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.committed: list[se.EndpointRow] = []
        patches = (
            mock.patch.object(containers, "_resolve_runtime", return_value="podman"),
            mock.patch.object(containers, "find_existing_container", return_value=None),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def migrate(self, world: ServiceWorld, row: se.EndpointRow, **kw):
        kw.setdefault("commit", self.committed.extend)
        kw.setdefault("db_path", self.tmp / "no-launcher.db")
        return sl.migrate_managed_service(
            world.root, row, runtime="podman", run=world.run, fetch=world.fetch_json,
            tcp_open=world.tcp_open, resolution=RESOLUTION, log=lambda _m: None,
            health_timeout_s=0.0, health_interval_s=0.0, **kw)


WEAVIATE_VOLUME = {"kind": "volume", "source": "legacy_weaviate_volume"}
CLASSES = ("Proj_KnowledgeGraph", "Proj_Development", "CodeFunction")


class WeaviateMoveTests(_Base):
    def world(self, **kw) -> ServiceWorld:
        kw.setdefault("mount", WEAVIATE_VOLUME)
        kw.setdefault("classes", CLASSES)
        return ServiceWorld(self.tmp, "weaviate", **kw)

    def test_moves_to_the_new_ports_on_the_same_volume_with_the_same_classes(self):
        w = self.world()
        result = self.migrate(w, _row("weaviate", port=18081, grpc_port=60052))
        self.assertEqual(result.status, "migrated", result.reason)
        self.assertEqual((w.port, w.grpc), (18081, 60052))
        self.assertEqual(w.mount, WEAVIATE_VOLUME)
        keys = w.env_keys()
        self.assertEqual((keys["WEAVIATE_PORT"], keys["WEAVIATE_GRPC_PORT"]), ("18081", "60052"))
        self.assertEqual(keys["VCT_WEAVIATE_VOLUME_NAME"], "legacy_weaviate_volume")
        [up] = w.ups()
        for flag in ("--force-recreate", "--no-deps"):
            self.assertIn(flag, up)
        self.assertNotIn("--build", up)  # an image, not a build context
        self.assertEqual(up[up.index("-p") + 1], INSTALLER)
        final = self.committed[-1]
        self.assertEqual((final.port, final.grpc_port), (18081, 60052))
        self.assertIsNotNone(final.verified_at)
        self.assertEqual(dict(final.data_mount or {})["source"], "legacy_weaviate_volume")

    def test_a_mount_the_effective_config_would_not_keep_refuses_before_stopping(self):
        """Without the knob on disk the installer's config mounts the EMPTY
        default volume — nothing may be stopped, the port stays."""
        w = self.world()
        with mock.patch.object(compose_env, "write_service_keys"):
            result = self.migrate(w, _row("weaviate", port=18081, grpc_port=60052))
        self.assertEqual(result.status, "refused", result.reason)
        self.assertIn("vco_weaviate_data", result.reason)
        self.assertEqual((w.ups(), w.stops()), ([], []))
        self.assertFalse([r for r in self.committed if r.port == 18081])

    def test_a_provider_that_renders_another_mount_refuses_and_restores_the_env(self):
        w = self.world()
        real = w.run

        def lying_config(argv, **kw):
            if argv[-1] == "config":
                doc = {"services": {"weaviate": {"volumes": ["weaviate_data:/var/lib/weaviate"]}},
                       "volumes": {"weaviate_data": {"name": "vco_weaviate_data"}}}
                return _cp(argv, 0, yaml.safe_dump(doc))
            return real(argv, **kw)

        w.run = lying_config  # type: ignore[method-assign]
        result = self.migrate(w, _row("weaviate", port=18081, grpc_port=60052))
        self.assertEqual(result.status, "refused", result.reason)
        self.assertIn("compose config", result.reason)
        self.assertEqual(w.ups(), [])
        self.assertEqual(w.env_keys()["WEAVIATE_PORT"], "8081")  # the move's port did not stay

    def test_a_class_list_mismatch_after_the_up_rolls_back_to_the_old_ports(self):
        w = self.world()
        w.lose_classes_once = True
        result = self.migrate(w, _row("weaviate", port=18081, grpc_port=60052))
        self.assertEqual(result.status, "failed", result.reason)
        self.assertIn("class list changed", result.reason)
        self.assertIn("Proj_KnowledgeGraph", result.reason)
        ups = w.ups()
        self.assertEqual(len(ups), 2, ups)
        self.assertIn("--force-recreate", ups[1])  # the ports moved: come back to them
        self.assertEqual(w.env_keys()["WEAVIATE_PORT"], "8081")
        self.assertEqual((w.port, w.grpc, w.mount), (8081, 50052, WEAVIATE_VOLUME))
        self.assertEqual(w.visible_classes, list(CLASSES))
        self.assertFalse([r for r in self.committed if r.port == 18081])

    def test_an_unreadable_class_list_refuses_before_stopping(self):
        w = self.world()
        w.fetch_json = lambda url, t: None  # type: ignore[method-assign]
        result = self.migrate(w, _row("weaviate", port=18081, grpc_port=60052))
        self.assertEqual(result.status, "refused")
        self.assertIn("class list", result.reason)
        self.assertEqual((w.ups(), w.stops()), ([], []))

    def test_an_unreachable_grpc_port_after_the_up_fails(self):
        w = self.world()
        w.tcp_open = lambda host, port, t: False  # type: ignore[method-assign]
        result = self.migrate(w, _row("weaviate", port=18081, grpc_port=60052))
        self.assertEqual(result.status, "failed")
        self.assertIn("gRPC", result.reason)
        self.assertEqual(w.port, 8081)

    def test_a_weaviate_under_another_compose_project_is_not_taken_over(self):
        w = self.world(project=OWNER)
        result = self.migrate(w, _row("weaviate", port=18081, grpc_port=60052))
        self.assertEqual(result.status, "refused")
        self.assertIn("hand-to-vco", result.reason)
        self.assertEqual((w.ups(), w.stops()), ([], []))
        self.assertEqual(w.env_keys()["WEAVIATE_PORT"], "8081")


class OllamaMoveTests(_Base):
    def setUp(self):
        super().setUp()
        self.models = self.tmp / "ollama-models"
        (self.models / "blobs").mkdir(parents=True)
        (self.models / "blobs" / "sha256-x").write_text("m", encoding="utf-8")

    def world(self, **kw) -> ServiceWorld:
        kw.setdefault("mount", {"kind": "bind", "source": str(self.models)})
        return ServiceWorld(self.tmp, "ollama", **kw)

    def test_moves_to_the_new_port_on_the_same_bind(self):
        w = self.world()
        result = self.migrate(w, _row("ollama", port=11500))
        self.assertEqual(result.status, "migrated", result.reason)
        self.assertEqual((w.port, w.mount), (11500, {"kind": "bind", "source": str(self.models)}))
        self.assertEqual(w.env_keys()["VCT_OLLAMA_DATA_SOURCE"], str(self.models))
        self.assertEqual(self.committed[-1].port, 11500)
        self.assertEqual(w.visible_models, ["qwen3-embedding:0.6b"])  # the inventory came along

    def test_a_model_list_mismatch_after_the_up_rolls_back_to_the_old_port(self):
        """The Ollama twin of the Weaviate class check: a recreate whose
        model list is not the pre-move set fails and rolls back."""
        w = self.world()
        w.lose_models_once = True
        result = self.migrate(w, _row("ollama", port=11500))
        self.assertEqual(result.status, "failed", result.reason)
        self.assertIn("model list changed", result.reason)
        self.assertIn("qwen3-embedding:0.6b", result.reason)
        ups = w.ups()
        self.assertEqual(len(ups), 2, ups)
        self.assertIn("--force-recreate", ups[1])  # the port moved: come back to it
        self.assertEqual(w.env_keys()["OLLAMA_PORT"], "11435")
        self.assertEqual(w.port, 11435)
        self.assertEqual(w.visible_models, ["qwen3-embedding:0.6b"])  # the rollback restored them
        self.assertFalse([r for r in self.committed if r.port == 11500])

    def test_an_unreadable_model_list_refuses_before_stopping(self):
        w = self.world()
        w.fetch_json = lambda url, t: None  # type: ignore[method-assign]
        result = self.migrate(w, _row("ollama", port=11500))
        self.assertEqual(result.status, "refused")
        self.assertIn("model list", result.reason)
        self.assertEqual((w.ups(), w.stops()), ([], []))

    def test_a_post_check_mount_mismatch_rolls_back_on_the_restored_env(self):
        w = self.world()
        w.up_mount_once = {"kind": "volume", "source": "vco_ollama_data"}  # the EMPTY default
        result = self.migrate(w, _row("ollama", port=11500))
        self.assertEqual(result.status, "failed", result.reason)
        self.assertIn("mounts", result.reason)
        ups = w.ups()
        self.assertEqual(len(ups), 2, ups)
        self.assertIn("--force-recreate", ups[1])
        self.assertEqual(w.env_keys()["OLLAMA_PORT"], "11435")
        self.assertEqual((w.port, w.mount), (11435, {"kind": "bind", "source": str(self.models)}))
        self.assertFalse([r for r in self.committed if r.port == 11500])

    def test_an_empty_bind_refuses(self):
        empty = self.tmp / "empty-models"
        empty.mkdir()
        w = self.world(mount={"kind": "bind", "source": str(empty)})
        result = self.migrate(w, _row("ollama", port=11500))
        self.assertEqual(result.status, "refused")
        self.assertEqual((w.ups(), w.stops()), ([], []))


class RecorderKwargs:
    def __init__(self):
        self.infra: list[dict] = []
        self.reprojected = 0
        self.registered = 0

    def kwargs(self) -> dict:
        return {
            "write_infra_env": lambda _d, rows: self.infra.append(dict(rows)),
            "reproject": lambda _db: setattr(self, "reprojected", self.reprojected + 1),
            "register_mcps": lambda _root: bool(setattr(self, "registered", self.registered + 1)) or True,
        }


class MoveEndpointTests(_Base):
    """``python -m vco_lib.service_endpoints move`` on VCO's own Weaviate —
    the real migration, the real launcher.db row, the real follow-up chain
    (its three steps recorded)."""

    def test_success_commits_the_row_and_runs_apply_change(self):
        w = ServiceWorld(self.tmp, "weaviate", mount=WEAVIATE_VOLUME, classes=CLASSES)
        db = make_launcher_db(self.tmp / "launcher.db")
        se.write_rows([_row("weaviate", data_mount={**WEAVIATE_VOLUME,
                                                    "destination": "/var/lib/weaviate"})],
                      db_path=db)
        rec = RecorderKwargs()
        lines: list[str] = []
        migrate = functools.partial(sl.migrate_managed_service, run=w.run, fetch=w.fetch_json,
                                    tcp_open=w.tcp_open, resolution=RESOLUTION,
                                    health_timeout_s=0.0, health_interval_s=0.0)
        rc = sr.move_endpoint("weaviate", orchestrator_root=w.root, port=18081, db_path=db,
                              runtime="podman", fetch=lambda u, t: None,
                              port_free=lambda p: True, migrate=migrate, out=lines.append,
                              apply_kwargs=rec.kwargs())
        self.assertEqual(rc, 0, lines)
        row = se.load_rows(db)["weaviate"]
        self.assertEqual((row.mode, row.port, row.grpc_port, row.source),
                         ("vco_managed", 18081, 60052, "user_cli"))  # gRPC keeps its offset
        self.assertIsNotNone(row.verified_at)
        self.assertEqual((rec.reprojected, rec.registered), (1, 1))
        self.assertEqual(rec.infra[-1]["weaviate"].port, 18081)
        self.assertEqual((w.port, w.grpc), (18081, 60052))

    def test_a_failed_move_leaves_the_row_where_the_service_answers(self):
        w = ServiceWorld(self.tmp, "weaviate", mount=WEAVIATE_VOLUME, classes=CLASSES)
        w.lose_classes_once = True
        db = make_launcher_db(self.tmp / "launcher.db")
        se.write_rows([_row("weaviate", data_mount={**WEAVIATE_VOLUME,
                                                    "destination": "/var/lib/weaviate"})],
                      db_path=db)
        rec = RecorderKwargs()
        lines: list[str] = []
        migrate = functools.partial(sl.migrate_managed_service, run=w.run, fetch=w.fetch_json,
                                    tcp_open=w.tcp_open, resolution=RESOLUTION,
                                    health_timeout_s=0.0, health_interval_s=0.0)
        rc = sr.move_endpoint("weaviate", orchestrator_root=w.root, port=18081, db_path=db,
                              runtime="podman", fetch=lambda u, t: None,
                              port_free=lambda p: True, migrate=migrate, out=lines.append,
                              apply_kwargs=rec.kwargs())
        self.assertEqual(rc, 1)
        self.assertIn("class list changed", lines[-1])
        row = se.load_rows(db)["weaviate"]
        self.assertEqual((row.port, row.grpc_port), (8081, 50052))
        self.assertEqual((w.port, w.grpc), (8081, 50052))


if __name__ == "__main__":
    unittest.main()

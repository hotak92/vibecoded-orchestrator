# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 SE-3 — lifecycle safety + code-embed recreate-with-cache.

Plan: ``.claude/context/plans/PLAN-V0297-SERVICE-ENDPOINTS-SSOT-2026-09-24.md``
§4c / §6 SE-3, invariants I1 (every compose invocation names only
``vco_managed`` services; an adopted service is never ``rm``'d, composed or
re-created) and I2 (a code_embed recreate keeps its cache: verified before
anything is stopped and again after the up).

Everything here is driven through injected seams — a fake ``run`` that plays
the container runtime AND the compose provider (it renders ``compose config``
and creates containers from the ``infrastructure/.env`` it finds ON DISK at
call time, the way compose does), a fake ``fetch``, temp roots and a temp
launcher.db. No container runtime is ever executed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional
from unittest import mock

import yaml

from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib import compose_env, containers, service_adoption, service_endpoints as se
from vco_lib import service_lifecycle as sl

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_COMPOSE = REPO_ROOT / "infrastructure" / "docker-compose.yml"


def _row(service: str, mode: str = "vco_managed", **kw) -> se.EndpointRow:
    defaults = {"weaviate": 8081, "ollama": 11435, "code_embed": 11440}
    kw.setdefault("port", defaults[service])
    kw.setdefault("source", "install_probe")
    if service == "weaviate":
        kw.setdefault("grpc_port", 50052)
    return se.EndpointRow(service=service, mode=mode, **kw)


#: The dogfood shape (plan §2): Weaviate and Ollama adopted from the legacy
#: compose home under their vco_* names, code_embed VCO-managed.
ADOPTED_ROWS = {
    "weaviate": _row("weaviate", "adopted_container", container_name="vco_weaviate",
                     source="migrated:legacy_compose"),
    "ollama": _row("ollama", "adopted_container", container_name="vco_ollama",
                   source="migrated:legacy_compose"),
    "code_embed": _row("code_embed"),
}


def _cp(argv, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)


# ===========================================================================
# I1 — the compose service list and the `up` argv
# ===========================================================================

class ComposeServicesTests(unittest.TestCase):
    def test_adopted_services_are_never_in_the_list(self):
        self.assertEqual(sl.compose_services(ADOPTED_ROWS), ["code_embed"])

    def test_no_rows_means_vcos_own_stack(self):
        self.assertEqual(sl.compose_services({}), ["weaviate", "ollama", "code_embed"])

    def test_a_disabled_managed_row_is_not_composed(self):
        rows = {"code_embed": _row("code_embed", enabled=False)}
        self.assertEqual(sl.compose_services(rows), ["weaviate", "ollama"])

    def test_external_ollama_is_neither_composed_nor_started(self):
        rows = {"ollama": _row("ollama", "adopted_external", host="localhost", port=11434)}
        self.assertNotIn("ollama", sl.compose_services(rows))
        self.assertEqual([p.service for p in sl.container_policies(rows)],
                         ["weaviate", "code_embed"])


class ComposeUpArgsTests(unittest.TestCase):
    def test_explicit_list_with_no_deps(self):
        args, dropped = sl.compose_up_args(["weaviate", "ollama"])
        self.assertEqual(args, ["up", "-d", "--no-deps", "weaviate", "ollama"])
        self.assertEqual(dropped, [])

    def test_code_embed_carries_the_gpu_profile_and_build(self):
        args, _ = sl.compose_up_args(["code_embed"], build=True)
        self.assertEqual(args, ["--profile", "gpu", "up", "-d", "--build", "--no-deps", "code_embed"])

    def test_cpu_mode_drops_code_embed(self):
        args, dropped = sl.compose_up_args(["ollama", "code_embed"], gpu_mode="cpu")
        self.assertEqual(args, ["up", "-d", "--no-deps", "ollama"])
        self.assertEqual(dropped, ["code_embed"])

    def test_an_empty_list_is_no_command_never_a_bare_up(self):
        self.assertEqual(sl.compose_up_args([]), ([], []))
        self.assertEqual(sl.compose_up_args(["code_embed"], gpu_mode="cpu")[0], [])

    def test_unknown_service_names_are_refused(self):
        with self.assertRaises(ValueError):
            sl.compose_up_args(["neo4j"])

    def test_gpu_profile_appears_once_when_the_prefix_enables_it(self):
        """install.py adds `-f <gpu overlay> --profile gpu` itself; the up
        args appended to it must not repeat the profile."""
        prefix = ["podman", "compose", "-f", "docker-compose.yml",
                  "-f", "podman-compose.gpu.yml", "--profile", "gpu"]
        args, _ = sl.compose_up_args(["weaviate", "code_embed"], build=True, gpu_mode="gpu",
                                     prefix=prefix)
        full = [*prefix, *args]
        self.assertEqual(full.count("--profile"), 1, full)
        self.assertEqual(args, ["up", "-d", "--build", "--no-deps", "weaviate", "code_embed"])
        args, _ = sl.compose_up_args(["code_embed"], prefix=["docker", "compose", "--profile=gpu"])
        self.assertNotIn("--profile", args)

    def test_gpu_profile_is_added_once_when_the_prefix_lacks_it(self):
        prefix = ["podman-compose", "-f", "docker-compose.yml", "--profile", "cpu-extras"]
        args, _ = sl.compose_up_args(["code_embed"], prefix=prefix)
        self.assertEqual([*prefix, *args].count("gpu"), 1)
        self.assertEqual(args[:2], ["--profile", "gpu"])

    def test_cli_prints_nothing_for_an_empty_list(self):
        with mock.patch("sys.stdout") as out:
            rc = sl._main(["compose-args", "--shell", "--services", ""])
        self.assertEqual(rc, 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertEqual(printed.strip(), "")


# ===========================================================================
# The zombie-recovery gate
# ===========================================================================

class ZombiePolicyTests(unittest.TestCase):
    def test_adopted_container_is_started_never_recreated(self):
        self.assertEqual(sl.zombie_action(ADOPTED_ROWS, "weaviate"), "start")
        self.assertEqual(sl.zombie_action(ADOPTED_ROWS, "ollama"), "start")
        self.assertEqual(sl.zombie_action(ADOPTED_ROWS, "code_embed"), "recreate")

    def test_adopted_without_autostart_is_left_alone(self):
        rows = {"ollama": _row("ollama", "adopted_container", container_name="theirs",
                               autostart=False)}
        self.assertEqual(sl.zombie_action(rows, "ollama"), "ignore")

    def test_policies_carry_the_rows_container_names(self):
        rows = {"weaviate": _row("weaviate", "adopted_container", container_name="my-weaviate")}
        pol = {p.service: p for p in sl.container_policies(rows)}
        self.assertEqual(pol["weaviate"].container, "my-weaviate")
        self.assertEqual((pol["weaviate"].on_missing, pol["weaviate"].on_stopped), ("report", "start"))
        self.assertEqual(pol["ollama"].container, "vco_ollama")
        self.assertEqual(pol["ollama"].on_missing, "compose")

    def test_user_required_names_unknown_to_rows_are_start_only(self):
        pol = sl.container_policies(ADOPTED_ROWS, required=["vco_code_embed", "neo4j_x"])
        self.assertEqual([p.container for p in pol], ["vco_code_embed", "neo4j_x"])
        self.assertEqual(pol[1].mode, "unlisted")
        self.assertEqual((pol[1].on_missing, pol[1].on_zombie), ("report", "start"))

    def test_shell_lines_are_index_aligned_arrays(self):
        lines = sl.lifecycle_shell_lines(sl.lifecycle_plan(ADOPTED_ROWS))
        text = "\n".join(lines)
        self.assertIn("VCO_COMPOSE_SERVICES=code_embed", text)
        self.assertIn("VCO_LC_CONTAINER=(vco_weaviate vco_ollama vco_code_embed)", text)
        self.assertIn("VCO_LC_ON_ZOMBIE=(start start recreate)", text)


# ===========================================================================
# The session reconcile runner (the session hooks' emit site)
# ===========================================================================

class SessionReconcileRunnerTests(unittest.TestCase):
    def fake(self, rc=0, stdout="", stderr=""):
        calls: list = []

        def run(argv, **kw):
            calls.append((list(argv), kw))
            return _cp(argv, rc, stdout, stderr)
        return run, calls

    def test_the_real_command_is_the_session_reconcile_verb(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(sl.SESSION_RECONCILE_ARGV_ENV, None)
            argv = sl.session_reconcile_argv()
        self.assertEqual(argv[1:], ["-m", "vco_lib.service_endpoints", "reconcile",
                                    "--phase", "session", "--json"])

    def test_entries_become_one_session_line_and_the_call_is_bounded(self):
        out = json.dumps({"schema": 1, "entries": ["service_endpoint_unreachable"]})
        run, calls = self.fake(stdout=out)
        lines = sl.run_session_reconcile(argv=["x"], run=run, timeout_s=3.0)
        self.assertEqual(len(lines), 1)
        self.assertIn("service_endpoint_unreachable", lines[0])
        self.assertEqual(calls[0][1]["timeout"], 3.0)
        self.assertIs(calls[0][1]["stdin"], subprocess.DEVNULL)

    def test_nothing_to_report_prints_nothing(self):
        run, _ = self.fake(stdout=json.dumps({"schema": 1, "entries": []}))
        self.assertEqual(sl.run_session_reconcile(argv=["x"], run=run), [])

    def test_every_failure_is_one_line_never_an_exception(self):
        run, _ = self.fake(rc=1, stderr="boom\nTraceback: nope")
        self.assertIn("exited 1", sl.run_session_reconcile(argv=["x"], run=run)[0])
        run, _ = self.fake(stdout="not json")
        self.assertIn("no readable JSON", sl.run_session_reconcile(argv=["x"], run=run)[0])

        def missing(argv, **kw):
            raise OSError("no such file")
        self.assertIn("could not run", sl.run_session_reconcile(argv=["x"], run=missing)[0])
        with mock.patch.dict(os.environ, {sl.SESSION_RECONCILE_ARGV_ENV: "{not a list"}):
            with self.assertRaises(ValueError):
                sl.session_reconcile_argv()

    def test_a_hung_reconcile_is_killed_at_the_bound(self):
        import sys
        import time

        started = time.monotonic()
        lines = sl.run_session_reconcile(
            argv=[sys.executable, "-c", "import time; time.sleep(60)"], timeout_s=0.5)
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("did not finish within 0.5 s", lines[0])


# ===========================================================================
# compose_env.write_service_keys — the managed infrastructure/.env keys
# ===========================================================================

class WriteServiceKeysTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vco_se3_env_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = self.tmp / ".env"

    def keys(self) -> dict:
        from vco_lib.envfile import parse_env_lines

        return dict(parse_env_lines(self.env.read_text(encoding="utf-8")))

    def test_managed_rows_project_ports_and_the_cache_bind(self):
        rows = {
            "weaviate": _row("weaviate", port=18081, grpc_port=15052),
            "code_embed": _row("code_embed", data_mount={
                "kind": "bind", "source": "/srv/hf cache", "destination": "/cache"}),
        }
        compose_env.write_service_keys(self.tmp, rows, runtime="podman")
        got = self.keys()
        self.assertEqual(got["WEAVIATE_PORT"], "18081")
        self.assertEqual(got["WEAVIATE_GRPC_PORT"], "15052")
        self.assertEqual(got["CODE_EMBED_PORT"], "11440")
        self.assertEqual(got["VCT_CODE_EMBED_CACHE_SOURCE"], "/srv/hf cache")
        self.assertNotIn("VCT_CODE_EMBED_VOLUME_NAME", got)
        self.assertNotIn("OLLAMA_PORT", got)  # no ollama row: no statement

    def test_a_named_volume_cache_projects_the_volume_name(self):
        rows = {"code_embed": _row("code_embed", data_mount={
            "kind": "volume", "source": "legacy_embed_cache", "destination": "/cache"})}
        compose_env.write_service_keys(self.tmp, rows, runtime="podman")
        got = self.keys()
        self.assertEqual(got["VCT_CODE_EMBED_VOLUME_NAME"], "legacy_embed_cache")
        self.assertNotIn("VCT_CODE_EMBED_CACHE_SOURCE", got)

    def test_adopted_rows_state_no_port_or_data_keys(self):
        compose_env.write_service_keys(self.tmp, ADOPTED_ROWS, runtime="podman")
        got = self.keys()
        for key in ("WEAVIATE_PORT", "WEAVIATE_GRPC_PORT", "OLLAMA_PORT",
                    "VCT_WEAVIATE_DATA_SOURCE", "VCT_OLLAMA_DATA_SOURCE"):
            self.assertNotIn(key, got)

    def test_adopted_ollama_url_uses_the_runtime_host_alias(self):
        compose_env.write_service_keys(self.tmp, ADOPTED_ROWS, runtime="podman")
        self.assertEqual(self.keys()["CODE_EMBED_OLLAMA_URL"],
                         "http://host.containers.internal:11435")
        self.assertNotIn("VCT_CODE_EMBED_HOST_GATEWAY", self.keys())
        compose_env.write_service_keys(self.tmp, ADOPTED_ROWS, runtime="docker")
        got = self.keys()
        self.assertEqual(got["CODE_EMBED_OLLAMA_URL"], "http://vco-host-gateway:11435")
        self.assertEqual(got["VCT_CODE_EMBED_HOST_GATEWAY"], "host-gateway")

    def test_an_unknown_runtime_writes_no_guess(self):
        with mock.patch.object(containers, "resolve",
                               return_value=SimpleNamespace(runtime=None)):
            result = compose_env.write_service_keys(self.tmp, ADOPTED_ROWS)
        self.assertNotIn("CODE_EMBED_OLLAMA_URL", result.values)
        self.assertTrue(result.notes)

    def test_remote_external_ollama_url_is_its_own(self):
        rows = {"ollama": _row("ollama", "adopted_external", host="gpu-box.lan", port=11434)}
        compose_env.write_service_keys(self.tmp, rows, runtime="podman")
        self.assertEqual(self.keys()["CODE_EMBED_OLLAMA_URL"], "http://gpu-box.lan:11434")

    def test_user_lines_survive_and_a_stated_key_supersedes_its_user_line(self):
        self.env.write_text(
            "# mine\nVCT_VOLUMES_PATH=/data/vols\nWEAVIATE_PORT=9999\n"
            "CODE_EMBED_OLLAMA_URL=http://my-host:1\n"
            "# Managed-by-install.py: header\nCODE_EMBED_BACKEND=gpu\n",
            encoding="utf-8")
        result = compose_env.write_service_keys(
            self.tmp, {"weaviate": _row("weaviate"), "ollama": _row("ollama")}, runtime="podman")
        got = self.keys()
        self.assertEqual(got["WEAVIATE_PORT"], "8081")
        self.assertEqual(result.superseded, {"WEAVIATE_PORT": "9999"})
        self.assertEqual(got["VCT_VOLUMES_PATH"], "/data/vols")
        self.assertEqual(got["CODE_EMBED_BACKEND"], "gpu")
        # Ollama is VCO's: the documented power-user override is not ours to touch.
        self.assertEqual(got["CODE_EMBED_OLLAMA_URL"], "http://my-host:1")
        self.assertEqual(self.env.read_text(encoding="utf-8").count("WEAVIATE_PORT="), 1)

    def test_a_no_longer_stated_block_key_leaves_the_block(self):
        compose_env.write_service_keys(self.tmp, ADOPTED_ROWS, runtime="podman")
        self.assertIn("CODE_EMBED_OLLAMA_URL", self.keys())
        rows = dict(ADOPTED_ROWS, ollama=_row("ollama"))
        compose_env.write_service_keys(self.tmp, rows, runtime="podman")
        self.assertNotIn("CODE_EMBED_OLLAMA_URL", self.keys())
        self.assertEqual(self.keys()["OLLAMA_PORT"], "11435")

    def test_a_managed_row_without_an_observed_mount_leaves_a_user_knob(self):
        self.env.write_text("VCT_OLLAMA_DATA_SOURCE=/srv/models\n", encoding="utf-8")
        compose_env.write_service_keys(self.tmp, {"ollama": _row("ollama")}, runtime="podman")
        self.assertEqual(self.keys()["VCT_OLLAMA_DATA_SOURCE"], "/srv/models")

    def test_rerun_is_byte_identical(self):
        rows = dict(ADOPTED_ROWS)
        first = compose_env.write_service_keys(self.tmp, rows, runtime="podman")
        before = self.env.read_bytes()
        again = compose_env.write_service_keys(self.tmp, rows, runtime="podman")
        self.assertEqual((first.action, again.action), ("set", "unchanged"))
        self.assertEqual(self.env.read_bytes(), before)

    def test_install_env_and_service_block_survive_each_other_in_either_order(self):
        rows = {"weaviate": _row("weaviate", port=18081, grpc_port=15052),
                "code_embed": _row("code_embed", data_mount={
                    "kind": "bind", "source": "/srv/hf", "destination": "/cache"})}
        embed = {"code_backend": "gpu", "gpu_vendor": "nvidia", "code_embed_max_concurrent": 6}
        want = {"WEAVIATE_PORT": "18081", "WEAVIATE_GRPC_PORT": "15052",
                "VCT_CODE_EMBED_CACHE_SOURCE": "/srv/hf", "CODE_EMBED_BACKEND": "gpu",
                "CODE_EMBED_DOCKERFILE": "Dockerfile.cuda", "VCT_VOLUMES_PATH": "/data/vols"}
        orders = {
            "service keys first": ("svc", "install", "svc", "install"),
            "install env first": ("install", "svc", "install", "svc"),
        }
        for label, order in orders.items():
            with self.subTest(order=label):
                self.env.write_text("VCT_VOLUMES_PATH=/data/vols\n", encoding="utf-8")
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("CODE_EMBED_MAX_CONCURRENT", None)
                    for step in order:
                        if step == "svc":
                            compose_env.write_service_keys(self.tmp, rows, runtime="podman")
                        else:
                            ok, msg = compose_env.write_infrastructure_env(self.tmp, embed)
                            self.assertTrue(ok, msg)
                got = self.keys()
                for key, value in want.items():
                    self.assertEqual(got.get(key), value, f"{label}: {key}\n{self.env.read_text()}")
                text = self.env.read_text(encoding="utf-8")
                self.assertEqual(text.count(compose_env._SERVICE_BLOCK_BEGIN), 1, text)
                self.assertEqual(text.count("WEAVIATE_PORT="), 1, text)
                self.assertEqual(text.count("CODE_EMBED_BACKEND="), 1, text)
                # a final service-keys pass finds its block intact: nothing to rewrite
                again = compose_env.write_service_keys(self.tmp, rows, runtime="podman")
                self.assertEqual(again.action, "unchanged", text)

    def test_no_rows_writes_no_file(self):
        compose_env.write_service_keys(self.tmp, {})
        self.assertFalse(self.env.exists())

    def test_apply_change_default_infra_step_is_this_writer(self):
        """SE-1's apply_change calls `compose_env.write_service_keys` by name."""
        db = self.tmp / "launcher.db"
        make_launcher_db(db)
        se.write_rows([_row("weaviate", port=18081, grpc_port=15052)], db_path=db)
        root = self.tmp / "root"
        (root / "infrastructure").mkdir(parents=True)
        report = se.apply_change(["weaviate"], orchestrator_root=root, db_path=db,
                                 reproject=lambda _db: None, register_mcps=lambda _r: True,
                                 out=lambda _l: None)
        self.assertEqual(report.steps["infra_env"], "ok", report.errors)
        text = (root / "infrastructure" / ".env").read_text(encoding="utf-8")
        self.assertIn("WEAVIATE_PORT=18081", text)


# ===========================================================================
# I2 — migrate_code_embed
# ===========================================================================

INSTALLER = "infrastructure"
OWNER = "vibecoded"
REF = "vco_code_embed"


class EmbedWorld:
    """A fake machine + compose provider for ONE code_embed container.

    ``compose ... config`` and ``compose ... up`` read ``infrastructure/.env``
    FROM DISK at call time and render the /cache mount exactly as the base
    file's knobs would, so what the migration wrote is what the provider
    sees. ``up`` re-creates the container under the ``-p`` project with that
    mount (unless ``up_mount_override`` forces another — a provider bug)."""

    def __init__(self, tmp: Path, *, cache: dict, project: str = INSTALLER,
                 owning_files: bool = True):
        self.root = tmp / "root"
        self.infra = self.root / "infrastructure"
        self.infra.mkdir(parents=True)
        shutil.copy(REAL_COMPOSE, self.infra / "docker-compose.yml")
        self.owning_dir = tmp / "owning"
        if owning_files:
            self.owning_dir.mkdir()
            (self.owning_dir / "compose.yaml").write_text(
                "name: vibecoded\nservices:\n  code_embed:\n    image: x\n"
                "    volumes:\n      - legacy_cache:/cache\n", encoding="utf-8")
        self.exists = True
        self.project = project
        self.mount = dict(cache)
        self.up_mount_override: Optional[dict] = None
        self.fetch: Callable[[str, float], Optional[dict]] = self._healthy
        self.config_rc = 0
        self.all_argv: list[list[str]] = []

    # -- provider render from the .env on disk --------------------------------
    def _knob_mount(self) -> dict:
        env = service_adoption.infrastructure_env_for_substitution(self.infra)
        source = env.get("VCT_CODE_EMBED_CACHE_SOURCE", "")
        if source:
            return {"Type": "bind", "Source": source, "Destination": "/cache"}
        name = env.get("VCT_CODE_EMBED_VOLUME_NAME", "") or "vco_code_embed_cache"
        return {"Type": "volume", "Name": name, "Source": "/var/x", "Destination": "/cache"}

    def _live_mount(self) -> dict:
        if self.mount["kind"] == "bind":
            return {"Type": "bind", "Source": self.mount["source"], "Destination": "/cache"}
        return {"Type": "volume", "Name": self.mount["source"], "Source": "/var/y",
                "Destination": "/cache"}

    def run(self, argv, **kw):
        argv = list(argv)
        self.all_argv.append(argv)
        rest = argv[1:]
        if "config" in argv and argv[-1] == "config":
            m = self._knob_mount()
            vol = ({"type": "bind", "source": m["Source"], "target": "/cache"}
                   if m["Type"] == "bind" else
                   {"type": "volume", "source": "code_embed_cache", "target": "/cache"})
            doc = {"services": {"code_embed": {"volumes": [vol]}},
                   "volumes": {"code_embed_cache": {"name": m.get("Name", "vco_code_embed_cache")}}}
            return _cp(argv, self.config_rc, yaml.safe_dump(doc), "" if not self.config_rc else "bad")
        if "up" in argv:
            if argv[-1] != "code_embed":
                raise AssertionError(f"compose up named more than code_embed: {argv}")
            m = self.up_mount_override or self._knob_mount()
            self.exists = True
            self.project = argv[argv.index("-p") + 1]
            self.mount = ({"kind": "bind", "source": m["Source"]} if m["Type"] == "bind"
                          else {"kind": "volume", "source": m["Name"]})
            return _cp(argv)
        if rest[0] == "inspect":
            ref = rest[-1]
            if ref != REF or not self.exists:
                return _cp(argv, 125, "", "no such container")
            fmt = rest[rest.index("--format") + 1]
            if fmt == "{{.Id}}":
                return _cp(argv, 0, "abc123\n")
            if "com.docker.compose.project" in fmt:
                wd = str(self.owning_dir) if self.project == OWNER else str(self.infra)
                files = "compose.yaml" if self.project == OWNER else "docker-compose.yml"
                return _cp(argv, 0, f"{self.project}\t{wd}\t{files}\n")
            payloads = {
                "{{json .Mounts}}": [self._live_mount()],
                "{{json .Config.Env}}": ["CODE_EMBED_BACKEND=gpu"],
                "{{json .NetworkSettings.Networks}}": {f"{self.project}_default": {}},
                "{{json .Config.Healthcheck}}": None,
                "{{json .HostConfig.RestartPolicy}}": {"Name": "unless-stopped"},
                "{{json .HostConfig.PortBindings}}": {"11440/tcp": [{"HostPort": "11440"}]},
                "{{json .HostConfig.Devices}}": [],
            }
            return _cp(argv, 0, json.dumps(payloads[fmt]))
        if rest[0] in ("stop", "rm"):
            if rest[0] == "rm":
                self.exists = False
            return _cp(argv)
        if rest[0] == "ps":
            return _cp(argv, 0, f"{REF}\n")
        raise AssertionError(f"unexpected argv: {argv}")

    @staticmethod
    def _healthy(url: str, timeout: float) -> Optional[dict]:
        return {"status": "ok", "model_loaded": True}


class MigrateCodeEmbedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vco_se3_mig_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = self.tmp / "hf-cache"
        (self.cache / "models--codesage").mkdir(parents=True)
        self.committed: list = []

    def world(self, **kw) -> EmbedWorld:
        kw.setdefault("cache", {"kind": "bind", "source": str(self.cache)})
        return EmbedWorld(self.tmp, **kw)

    def migrate(self, world: EmbedWorld, row=None, **kw):
        kw.setdefault("commit", lambda rows: self.committed.extend(rows))
        kw.setdefault("health_timeout_s", 0.0)
        kw.setdefault("health_interval_s", 0.0)
        kw.setdefault("log", lambda _m: None)
        kw.setdefault("db_path", self.tmp / "no-launcher.db")
        with mock.patch.object(containers, "_resolve_runtime", return_value="podman"), \
                mock.patch.object(containers, "find_existing_container",
                                  lambda s, r="podman": REF if s == "code_embed" and world.exists else None):
            return sl.migrate_code_embed(
                world.root, row or _row("code_embed"), runtime="podman", run=world.run,
                fetch=world.fetch,
                resolution=SimpleNamespace(compose=["podman", "compose"], compose_form="subcommand"),
                **kw)

    def env_keys(self, world) -> dict:
        from vco_lib.envfile import parse_env_lines

        return dict(parse_env_lines((world.infra / ".env").read_text(encoding="utf-8")))

    def ups(self, world):
        return [a for a in world.all_argv if "up" in a]

    # (3) the live bind cache survives, verified before and after ----------------

    def test_bind_cache_is_projected_verified_and_kept(self):
        w = self.world()
        result = self.migrate(w)
        self.assertEqual(result.status, "migrated", result.reason)
        self.assertEqual(self.env_keys(w)["VCT_CODE_EMBED_CACHE_SOURCE"], str(self.cache))
        self.assertEqual(result.mount, {"kind": "bind", "source": str(self.cache),
                                        "destination": "/cache"})
        self.assertEqual(self.committed[0].data_mount["source"], str(self.cache))
        cfg = [a for a in w.all_argv if a[-1] == "config"]
        up = self.ups(w)
        self.assertEqual(len(up), 1)
        self.assertLess(w.all_argv.index(cfg[0]), w.all_argv.index(up[0]))
        for flag in ("--force-recreate", "--build", "--no-deps"):
            self.assertIn(flag, up[0])
        self.assertEqual(up[0][up[0].index("-p") + 1], INSTALLER)
        self.assertEqual(w.mount, {"kind": "bind", "source": str(self.cache)})

    def test_without_the_knob_on_disk_it_refuses_before_stopping_anything(self):
        """The knob write suppressed: the effective config would mount the
        EMPTY default volume, so nothing may be stopped."""
        w = self.world()
        with mock.patch.object(compose_env, "write_service_keys"):
            result = self.migrate(w)
        self.assertEqual(result.status, "refused")
        self.assertIn("vco_code_embed_cache", result.reason)
        self.assertEqual(self.ups(w), [])
        self.assertFalse([a for a in w.all_argv if a[1] in ("stop", "rm")])

    def test_a_provider_that_renders_another_mount_refuses_too(self):
        w = self.world()
        real = w.run

        def lying_config(argv, **kw):
            if argv[-1] == "config":
                doc = {"services": {"code_embed": {"volumes": ["code_embed_cache:/cache"]}},
                       "volumes": {"code_embed_cache": {"name": "vco_code_embed_cache"}}}
                return _cp(argv, 0, yaml.safe_dump(doc))
            return real(argv, **kw)

        w.run = lying_config
        result = self.migrate(w)
        self.assertEqual(result.status, "refused")
        self.assertIn("compose config", result.reason)
        self.assertEqual(self.ups(w), [])

    # (4) a named-volume cache ---------------------------------------------------

    def test_named_volume_cache_projects_the_volume_name(self):
        w = self.world(cache={"kind": "volume", "source": "legacy_embed_cache"})
        result = self.migrate(w)
        self.assertEqual(result.status, "migrated", result.reason)
        keys = self.env_keys(w)
        self.assertEqual(keys["VCT_CODE_EMBED_VOLUME_NAME"], "legacy_embed_cache")
        self.assertNotIn("VCT_CODE_EMBED_CACHE_SOURCE", keys)
        self.assertEqual(w.mount, {"kind": "volume", "source": "legacy_embed_cache"})

    # (5) post-verify mismatch → rollback under the previous owner -------------

    def test_installer_owned_post_check_mismatch_fails_and_re_ups(self):
        w = self.world()
        w.up_mount_override = {"Type": "volume", "Name": "vco_code_embed_cache"}
        result = self.migrate(w)
        self.assertEqual(result.status, "failed")
        self.assertIn("mounts", result.reason)
        ups = self.ups(w)
        self.assertEqual(len(ups), 2)
        self.assertNotIn("--build", ups[1])
        self.assertNotIn("--force-recreate", ups[1])

    def test_foreign_owned_post_check_mismatch_rolls_back_under_the_owner(self):
        w = self.world(project=OWNER)
        w.up_mount_override = {"Type": "volume", "Name": "vco_code_embed_cache"}
        result = self.migrate(w)
        self.assertEqual(result.status, "failed", result.reason)
        rollback = [a for a in self.ups(w) if a[a.index("-p") + 1] == OWNER]
        self.assertEqual(len(rollback), 1, self.ups(w))
        self.assertEqual(w.project, OWNER)

    def test_foreign_owned_happy_path_moves_it_under_the_installer(self):
        w = self.world(project=OWNER)
        result = self.migrate(w)
        self.assertEqual(result.status, "migrated", result.reason)
        self.assertEqual(w.project, INSTALLER)
        up = [a for a in self.ups(w) if a[a.index("-p") + 1] == INSTALLER]
        self.assertIn("--build", up[0])
        self.assertIn("--no-deps", up[0])
        # the knob, not an override mount fragment, carries the cache
        override = w.infra / "compose.override.yaml"
        doc = yaml.safe_load(override.read_text(encoding="utf-8"))
        self.assertNotIn("volumes", (doc.get("services") or {}).get("code_embed") or {})

    def test_model_not_loaded_in_the_window_fails_and_re_ups(self):
        w = self.world()
        w.fetch = lambda url, t: {"status": "ok", "model_loaded": False}
        result = self.migrate(w)
        self.assertEqual(result.status, "failed")
        self.assertIn("model loaded", result.reason)

    # refusals that touch nothing -------------------------------------------------

    def test_no_container_is_not_needed(self):
        w = self.world()
        w.exists = False
        result = self.migrate(w)
        self.assertEqual(result.status, "not_needed")
        self.assertEqual(self.ups(w), [])

    def test_an_empty_bind_refuses(self):
        empty = self.tmp / "empty-cache"
        empty.mkdir()
        w = self.world(cache={"kind": "bind", "source": str(empty)})
        result = self.migrate(w)
        self.assertEqual(result.status, "refused")
        self.assertEqual(self.ups(w), [])

    def test_the_wrong_row_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            sl.migrate_code_embed(self.tmp, _row("ollama"))


# ===========================================================================
# service_adoption — services=, knob over fragment, the HTTP health port
# ===========================================================================

class AdoptionScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vco_se3_adopt_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = self.tmp / "hf-cache"
        (self.cache / "m").mkdir(parents=True)

    def test_services_limits_the_adoption_and_preserves_other_stanzas(self):
        w = EmbedWorld(self.tmp, cache={"kind": "bind", "source": str(self.cache)}, project=OWNER)
        (w.infra / "compose.override.yaml").write_text(
            "# Auto-generated by VCT (v0.2.96 service adoption).\n"
            "services:\n  ollama:\n    environment:\n      OLLAMA_KEEP_ALIVE: 24h\n",
            encoding="utf-8")
        compose_env.write_service_keys(w.infra, {"code_embed": _row("code_embed", data_mount={
            "kind": "bind", "source": str(self.cache), "destination": "/cache"})}, runtime="podman")
        seen: list[str] = []

        def find(service, runtime="podman"):
            seen.append(service)
            return REF if service == "code_embed" else None

        with mock.patch.object(containers, "_resolve_runtime", return_value="podman"), \
                mock.patch.object(containers, "find_existing_container", find), \
                mock.patch.object(service_adoption, "_derive_embed_config", return_value={}):
            result = service_adoption.adopt_services(
                w.root, services=("code_embed",), run=w.run, fetch=lambda u, t: 200,
                resolution=SimpleNamespace(compose=["podman", "compose"], compose_form="subcommand"),
                log=lambda _m: None, commit_rows=lambda rows: None)
        self.assertEqual(result.adopted, ["code_embed"])
        self.assertEqual(seen, ["code_embed"])
        doc = yaml.safe_load((w.infra / "compose.override.yaml").read_text(encoding="utf-8"))
        self.assertEqual(doc["services"]["ollama"]["environment"]["OLLAMA_KEEP_ALIVE"], "24h")

    def test_a_knob_that_disagrees_with_the_live_mount_is_refused_not_papered_over(self):
        w = EmbedWorld(self.tmp, cache={"kind": "bind", "source": str(self.cache)}, project=OWNER)
        (w.infra / ".env").write_text("VCT_CODE_EMBED_VOLUME_NAME=some_other_volume\n",
                                      encoding="utf-8")
        with mock.patch.object(containers, "_resolve_runtime", return_value="podman"), \
                mock.patch.object(containers, "find_existing_container",
                                  lambda s, r="podman": REF if s == "code_embed" else None):
            result = service_adoption.adopt_services(
                w.root, services=("code_embed",), run=w.run, fetch=lambda u, t: 200,
                resolution=SimpleNamespace(compose=["podman", "compose"], compose_form="subcommand"),
                log=lambda _m: None, commit_rows=lambda rows: None)
        self.assertEqual(result.adopted, [])
        self.assertIn("code_embed", result.refused)
        self.assertFalse([a for a in w.all_argv if "up" in a])

    def test_weaviate_health_is_probed_on_its_http_port_not_grpc(self):
        live = service_adoption.LiveServiceState(
            host_ports={"50051": "50052", "8080": "8081"})
        self.assertEqual(service_adoption.live_http_host_port("weaviate", live), "8081")
        live_only_first = service_adoption.LiveServiceState(host_ports={"50051": "50052",
                                                                        "9999": "18081"})
        self.assertEqual(service_adoption.live_http_host_port("weaviate", live_only_first), "18081")

    def test_the_adopted_row_records_http_and_grpc_ports(self):
        plan = service_adoption.ServicePlan(
            service="weaviate", container="vco_weaviate",
            live=service_adoption.LiveServiceState(
                mounts=(service_adoption.MountSpec("volume", "vco_weaviate_data",
                                                   "/var/lib/weaviate", ""),),
                host_ports={"50051": "50052", "8080": "8081"}))
        row = service_adoption.adopted_row("weaviate", plan, INSTALLER, prior=None,
                                           source="user_cli", now_ms=1)
        se.validate_row(row)
        self.assertEqual((row.port, row.grpc_port), (8081, 50052))
        self.assertEqual(row.data_mount["source"], "vco_weaviate_data")


# ===========================================================================
# The real compose file renders under both providers (read-only `config`)
# ===========================================================================

def _provider_config(argv: list[str], env_extra: dict) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="vco_se3_cfg_"))
    try:
        infra = tmp / "infrastructure"
        infra.mkdir()
        shutil.copy(REAL_COMPOSE, infra / "docker-compose.yml")
        ctx = tmp / "claude_mcp_servers" / "code_embedding_service"
        ctx.mkdir(parents=True)
        (ctx / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("VCT_", "WEAVIATE_", "OLLAMA_", "CODE_EMBED_"))}
        env.update(env_extra)
        res = subprocess.run([*argv, "-f", "docker-compose.yml", "--profile", "gpu", "config"],
                             cwd=infra, env=env, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise AssertionError(f"{argv} config failed: {res.stderr}")
        return yaml.safe_load(res.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _providers() -> list[list[str]]:
    out = []
    if shutil.which("podman-compose"):
        out.append(["podman-compose"])
    if shutil.which("docker"):
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30)
        if probe.returncode == 0:
            out.append(["docker", "compose"])
    return out


@unittest.skipUnless(_providers(), "no compose provider on PATH")
class ComposeRenderTests(unittest.TestCase):
    """`compose config` is read-only (no container touched). Every knob this
    lane projects must render as intended under every provider installed."""

    def cache_mount(self, doc: dict) -> tuple:
        svc = doc["services"]["code_embed"]
        m = service_adoption.config_mounts(svc, doc.get("volumes") or {})["/cache"]
        return m.kind, m.source

    def test_knobs_set_and_unset(self):
        for argv in _providers():
            with self.subTest(provider=argv):
                self.assertEqual(self.cache_mount(_provider_config(argv, {})),
                                 ("volume", "vco_code_embed_cache"))
                self.assertEqual(
                    self.cache_mount(_provider_config(argv, {"VCT_CODE_EMBED_CACHE_SOURCE": "/srv/hf"})),
                    ("bind", "/srv/hf"))
                self.assertEqual(
                    self.cache_mount(_provider_config(argv, {"VCT_CODE_EMBED_VOLUME_NAME": "old_cache"})),
                    ("volume", "old_cache"))

    def test_extra_hosts_default_is_inert_and_docker_gets_the_gateway(self):
        for argv in _providers():
            with self.subTest(provider=argv):
                hosts = _provider_config(argv, {})["services"]["code_embed"].get("extra_hosts")
                self.assertIn("127.0.0.1", json.dumps(hosts))
                hosts = _provider_config(argv, {"VCT_CODE_EMBED_HOST_GATEWAY": "host-gateway"})[
                    "services"]["code_embed"].get("extra_hosts")
                self.assertIn("host-gateway", json.dumps(hosts))

    def test_projected_ports_render(self):
        for argv in _providers():
            with self.subTest(provider=argv):
                doc = _provider_config(argv, {"WEAVIATE_PORT": "18081", "WEAVIATE_GRPC_PORT": "15052"})
                self.assertIn("18081", json.dumps(doc["services"]["weaviate"]["ports"]))
                self.assertIn("15052", json.dumps(doc["services"]["weaviate"]["ports"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

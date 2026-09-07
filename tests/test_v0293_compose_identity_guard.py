# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.93 — step 5 must not recreate containers another compose project owns,
and a failed `compose up` on --update must not kill a run nothing depends on.

Field 2026-09-07 (dogfood update VCO_dev → v0.2.92): every service was healthy
and adopted, but the containers had been created in July from the legacy
``claude_mcp_servers/compose.yaml`` (compose project ``vibecoded``). v0.2.92's
adopt-recreate + image-rebuild path was the FIRST to drive compose against them
under project ``infrastructure``: compose refused ("network infrastructure_default
was found but has incorrect label"), install.py printed FAIL and ``sys.exit(1)``
at step 5/10 — after the manifest/bundle steps had run and before hooks, hub,
MCP registration, KG seed and schema migrations. Half-updated tree, no ledger row.

A second defect hid under it: the code_embed staleness probe got the detector's
``.../health`` URL and appended ``/health`` again → 404 → "service is not
answering /health" for a service that answered fine. The rebuild fired anyway,
for the wrong reason; the digest comparison never ran.

Every test drives a production entry point (``vco_lib.containers``,
``vco_lib.code_embed_image``, ``install._start_services``). Red-proofs: revert
any one of the three hunks and the matching test class goes red.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import code_embed_image, containers, install_services_guard  # noqa: E402


def _install_module():
    spec = importlib.util.spec_from_file_location(
        "_test_install_v0293_identity", REPO_ROOT / "install.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["_test_install_v0293_identity"] = module
    spec.loader.exec_module(module)
    return module


def _fake_inspect(stdout: str, rc: int = 0):
    def run(argv, **kwargs):
        run.argv = list(argv)
        return subprocess.CompletedProcess(argv, rc, stdout, "")
    return run


class ComposeProjectNameTests(unittest.TestCase):
    def test_top_level_name_key_wins(self):
        text = "name: vibecoded\n\nservices:\n  weaviate:\n    image: x\n"
        self.assertEqual(containers.compose_project_name(Path("/x/infrastructure"), text), "vibecoded")

    def test_indented_name_is_not_the_project_key(self):
        text = "services:\n  weaviate:\n    name: nope\n"
        self.assertEqual(containers.compose_project_name(Path("/x/infrastructure"), text), "infrastructure")

    def test_basename_is_normalised_like_compose(self):
        self.assertEqual(containers.compose_project_name(Path("/x/Infra Structure.v2")), "infrastructurev2")
        self.assertEqual(containers.compose_project_name(Path("/x/_-my-dir")), "my-dir")


class ComposeIdentityOfTests(unittest.TestCase):
    def test_reads_the_three_labels(self):
        run = _fake_inspect("vibecoded\t/home/u/claude_mcp_servers\tcompose.yaml,compose.override.yaml\n")
        with mock.patch.object(containers, "_resolve_runtime", return_value="podman"):
            ident = containers.compose_identity_of("vco_weaviate", "podman", run=run)
        self.assertEqual(ident, containers.ComposeIdentity(
            "vibecoded", "/home/u/claude_mcp_servers", "compose.yaml,compose.override.yaml"))
        self.assertEqual(run.argv[:3], ["podman", "inspect", "--type"])
        self.assertEqual(run.argv[-1], "vco_weaviate")
        self.assertIn("com.docker.compose.project", run.argv[-2])

    def test_no_project_label_is_none(self):
        run = _fake_inspect("\t\t\n")
        with mock.patch.object(containers, "_resolve_runtime", return_value="docker"):
            self.assertIsNone(containers.compose_identity_of("c", "docker", run=run))

    def test_inspect_failure_is_none(self):
        with mock.patch.object(containers, "_resolve_runtime", return_value="podman"):
            self.assertIsNone(containers.compose_identity_of("c", run=_fake_inspect("", rc=125)))

            def boom(argv, **kw):
                raise OSError("no runtime")
            self.assertIsNone(containers.compose_identity_of("c", run=boom))

    def test_missing_runtime_binary_is_none(self):
        with mock.patch.object(containers, "_resolve_runtime", return_value=None):
            self.assertIsNone(containers.compose_identity_of("c", run=_fake_inspect("x\t\t")))


class ForeignComposeIdentityTests(unittest.TestCase):
    def test_same_project_is_ours(self):
        self.assertIsNone(containers.foreign_compose_identity(
            containers.ComposeIdentity("infrastructure", "/x/infrastructure"), "infrastructure"))

    def test_other_project_is_foreign_and_names_it(self):
        why = containers.foreign_compose_identity(
            containers.ComposeIdentity("vibecoded", "/x/claude_mcp_servers", "compose.yaml"),
            "infrastructure")
        self.assertIsNotNone(why)
        for needle in ("vibecoded", "/x/claude_mcp_servers", "compose.yaml", "infrastructure"):
            self.assertIn(needle, why)

    def test_not_compose_created_is_foreign(self):
        why = containers.foreign_compose_identity(None, "infrastructure")
        self.assertIsNotNone(why)
        self.assertIn("not created by compose", why)

    def test_path_difference_alone_is_not_foreign(self):
        """A moved checkout keeps its project name; paths are reported, not judged."""
        self.assertIsNone(containers.foreign_compose_identity(
            containers.ComposeIdentity("infrastructure", "/old/place/infrastructure"), "infrastructure"))


class HealthUrlTests(unittest.TestCase):
    """The detector hands over ``.../health``; the probe must not double it."""

    def test_explicit_health_url_is_a_base_url(self):
        self.assertEqual(code_embed_image.service_base_url("http://localhost:11440/health"),
                         "http://localhost:11440")
        self.assertEqual(code_embed_image.service_base_url("http://localhost:11440/health/"),
                         "http://localhost:11440")

    def test_plain_base_untouched(self):
        self.assertEqual(code_embed_image.service_base_url("http://h:1/"), "http://h:1")

    def test_env_health_url_is_a_base_url(self):
        with mock.patch.dict("os.environ", {"CODE_EMBED_SERVICE_URL": "http://svc:9/health"}):
            self.assertEqual(code_embed_image.service_base_url(), "http://svc:9")

    def test_probe_health_requests_health_once(self):
        """Wiring: probe_health(detector url) GETs ``/health``, not ``/health/health``."""
        seen = {}

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"status": "ok", "source_sha": "abc"}'

        def fake_urlopen(url, timeout=0):
            seen["url"] = url
            return _Resp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            payload = code_embed_image.probe_health("http://localhost:11440/health")
        self.assertEqual(seen["url"], "http://localhost:11440/health")
        self.assertEqual(payload, {"status": "ok", "source_sha": "abc"})


class SurvivableDecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = _install_module()

    def _args(self, update):
        import argparse
        return argparse.Namespace(update=update)

    def test_update_with_every_required_service_up_survives(self):
        detected = {"weaviate_url": "u", "ollama_url": "u", "code_embed_url": "u"}
        self.assertTrue(install_services_guard.compose_failure_is_survivable(self._args(True), detected, True))

    def test_fresh_install_never_survives(self):
        detected = {"weaviate_url": "u", "ollama_url": "u", "code_embed_url": "u"}
        self.assertFalse(install_services_guard.compose_failure_is_survivable(self._args(False), detected, True))

    def test_required_service_down_does_not_survive(self):
        self.assertFalse(install_services_guard.compose_failure_is_survivable(
            self._args(True), {"weaviate_url": "u", "ollama_url": None, "code_embed_url": "u"}, True))
        self.assertFalse(install_services_guard.compose_failure_is_survivable(
            self._args(True), {"weaviate_url": "u", "ollama_url": "u", "code_embed_url": None}, True))

    def test_code_embed_not_required_without_gpu(self):
        self.assertTrue(install_services_guard.compose_failure_is_survivable(
            self._args(True), {"weaviate_url": "u", "ollama_url": "u", "code_embed_url": None}, False))

    def test_mock_args_do_not_read_as_update(self):
        """A truthy non-bool attribute (mock.Mock) must not unlock the survivable path."""
        detected = {"weaviate_url": "u", "ollama_url": "u", "code_embed_url": "u"}
        self.assertFalse(install_services_guard.compose_failure_is_survivable(mock.Mock(), detected, True))


class _Ledger:
    def __init__(self):
        self.entries = []

    def add_entry(self, entry):
        self.entries.append(entry)


class StartServicesIdentityGuardTests(unittest.TestCase):
    """Drives ``install._start_services`` — the production entry point."""

    @classmethod
    def setUpClass(cls):
        cls.install = _install_module()

    def _drive(self, identity, *, rc=0, stderr="", update=True, decisions=None):
        install = self.install
        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"] = list(cmd)
            return subprocess.CompletedProcess(cmd, rc, "", stderr)

        state = code_embed_image.ImageState(code_embed_image.STALE, "summary(stale)")
        sysinfo = install.SystemInfo(
            os_name="Linux", has_gpu=True, has_metal=False, container_cmd="podman",
            gpu_name="RTX", vram_gb=24.0, ram_gb=64.0, gpu_vendor="nvidia",
        )
        decisions = decisions if decisions is not None else {
            "code_embed": {"action": install.ACTION_ADOPT, "probe": install.PROBE_VCT_MANAGED},
            "weaviate": {"action": install.ACTION_ADOPT, "probe": install.PROBE_VCT_MANAGED},
            "ollama": {"action": install.ACTION_ADOPT, "probe": install.PROBE_VCT_MANAGED},
        }
        import argparse
        args = argparse.Namespace(update=update)
        ledger = _Ledger()
        with mock.patch.object(install, "_detect_existing_volume_paths", return_value={}), \
             mock.patch.object(install, "_detect_existing_services", return_value={
                 "weaviate_url": "http://localhost:8081/v1/.well-known/ready",
                 "ollama_url": "http://localhost:11435/api/tags",
                 "code_embed_url": "http://localhost:11440/health"}), \
             mock.patch.object(install, "_container_runtime_reachable", return_value=True), \
             mock.patch.object(install, "_get_compose_command", return_value=["podman", "compose"]), \
             mock.patch.object(install, "_write_infrastructure_env"), \
             mock.patch.object(install, "_compose_substitution_env", return_value={}), \
             mock.patch.object(install, "_log_install_event"), \
             mock.patch.object(install, "_should_check_weaviate_reclaim_drift", return_value=False), \
             mock.patch.object(install._containers, "find_existing_container",
                               side_effect=lambda svc, runtime="podman": f"vco_{svc}"), \
             mock.patch.object(install._containers, "compose_identity_of", return_value=identity), \
             mock.patch.object(code_embed_image, "image_state", return_value=state), \
             mock.patch.object(subprocess, "run", side_effect=fake_run):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                install._start_services(sysinfo, args, {}, decisions=decisions, deferral_report=ledger)
        return recorded.get("cmd", []), buf.getvalue(), ledger.entries

    def test_foreign_owned_services_are_left_alone_and_ledgered(self):
        foreign = containers.ComposeIdentity(
            "vibecoded", "/home/u/claude_mcp_servers", "compose.yaml,compose.override.yaml")
        cmd, out, entries = self._drive(foreign)
        # Every adopt was foreign → nothing to start or recreate → compose never runs.
        self.assertEqual(cmd, [])
        self.assertIn("[skip-recreate] code_embed", out)
        self.assertIn("[skip-recreate] weaviate", out)
        self.assertIn("vibecoded", out)
        self.assertIn("All required services already running", out)
        cids = [e.condition_id for e in entries]
        self.assertEqual(cids, ["services_foreign_compose_identity"])
        entry = entries[0]
        self.assertIn("/home/u/claude_mcp_servers", entry.detected)
        self.assertIn("--force-recreate", entry.command_to_apply)
        self.assertNotIn(" rm ", entry.command_to_apply)  # never a delete command in a remedy

    def test_owned_services_still_get_the_recreate_and_build(self):
        """Red-proof of the guard's other side: same drive, our own project → unchanged v0.2.92 behaviour."""
        ours = containers.ComposeIdentity("infrastructure", str(REPO_ROOT / "infrastructure"))
        cmd, out, entries = self._drive(ours)
        self.assertIn("--force-recreate", cmd)
        self.assertIn("--build", cmd)
        self.assertIn("code_embed", cmd)
        self.assertNotIn("[skip-recreate]", out)
        self.assertEqual(entries, [])

    def test_compose_failure_on_update_with_services_up_continues_and_ledgers(self):
        ours = containers.ComposeIdentity("infrastructure", str(REPO_ROOT / "infrastructure"))
        stderr = ('network infrastructure_default was found but has incorrect label '
                  'com.docker.compose.network set to "" (expected: "default")\n')
        cmd, out, entries = self._drive(ours, rc=1, stderr=stderr, update=True)
        self.assertIn("FAIL", out)
        self.assertIn("Continuing:", out)
        self.assertIn("label mismatch", out)  # the targeted hint fired
        self.assertEqual([e.condition_id for e in entries], ["services_compose_up_failed"])
        self.assertIn("incorrect label", entries[0].detected)
        self.assertIn("up -d", entries[0].command_to_apply)

    def test_compose_failure_on_fresh_install_still_exits(self):
        ours = containers.ComposeIdentity("infrastructure", str(REPO_ROOT / "infrastructure"))
        with self.assertRaises(SystemExit):
            self._drive(ours, rc=1, stderr="boom", update=False)


if __name__ == "__main__":
    unittest.main()


class HardStopPersistsForeignRowTests(unittest.TestCase):
    """Review R1 finding 4: the run report is only written by finalize() at
    the end of a COMPLETED run, so on the hard stop the foreign-identity row
    must go through the locked on-disk writer."""

    def test_followup_persists_rows_through_the_locked_writer_on_hard_stop(self):
        import argparse
        import tempfile
        root = Path(tempfile.mkdtemp(prefix="_tmp_v0293_hardstop_"))
        try:
            entry = install_services_guard.build_foreign_compose_identity_entry(
                {"code_embed": "container 'vco_code_embed' was created by compose project 'vibecoded'"},
                "podman", root / "infrastructure",
            )
            cont = install_services_guard.compose_failure_followup(
                args=argparse.Namespace(update=False), detected={}, has_gpu=True,
                deferral_report=None, exit_code=1, stderr="boom",
                manual_cmd="cd x && podman compose up -d", log_event=lambda *a, **k: None,
                install_root=root, persist_on_hard_stop=(entry,),
            )
            self.assertFalse(cont)
            ledger = root / ".claude" / "context" / "UPDATE_DEFERRED.md"
            self.assertTrue(ledger.is_file(), "hard stop must persist the owed row")
            self.assertIn("services_foreign_compose_identity", ledger.read_text(encoding="utf-8"))
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_start_services_hands_the_guard_rows_to_the_followup(self):
        """Wiring: on a foreign+FAIL run, _start_services passes the guard's
        rows and the install root to the follow-up (spy, no disk write)."""
        install = _install_module()
        seen = {}

        def spy(**kw):
            seen.update(kw)
            return False

        foreign = containers.ComposeIdentity("vibecoded", "/home/u/claude_mcp_servers")
        harness = StartServicesIdentityGuardTests()
        harness.install = install
        # Only weaviate is foreign (others start fresh) so compose still runs and fails.
        decisions = {
            "code_embed": {"action": install.ACTION_START, "probe": install.PROBE_NOT_RUNNING},
            "weaviate": {"action": install.ACTION_ADOPT, "probe": install.PROBE_VCT_MANAGED},
            "ollama": {"action": install.ACTION_START, "probe": install.PROBE_NOT_RUNNING},
        }
        with mock.patch.object(install._svc_guard, "compose_failure_followup", side_effect=spy):
            with self.assertRaises(SystemExit):
                harness._drive(foreign, rc=1, stderr="boom", update=False, decisions=decisions)
        self.assertEqual(seen["install_root"], install.PROJECT_ROOT)
        self.assertEqual([e.condition_id for e in seen["persist_on_hard_stop"]],
                         ["services_foreign_compose_identity"])

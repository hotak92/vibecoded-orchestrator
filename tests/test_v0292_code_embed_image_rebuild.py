# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 BLOCKER-1 — the code_embed image is REBUILT, and staleness is CHECKABLE.

The defect these tests pin is a delivery defect, not a logic one: ``code_embed``
is the only VCO service that ships as an image BUILT from the checkout, and
``compose up`` builds an image only when it is MISSING. So v0.2.92's fix (the
service REFUSES over-window input at HTTP 400 instead of silently truncating it
at HTTP 200) was correct in ``server.py``, pinned by its own tests, green in CI,
and running nowhere. Measured on the maintainer's machine before the fix: image
built 2026-05-16, container ``--force-recreate``d 2026-07-12, and a live 13 450
-char POST answered HTTP 200 with ``cosine(text, text+tail) == 1.000000``.

A second defect was hiding INSIDE it, and only became reachable once the first
was fixed: the shipped ``server.py`` could not START in a freshly built image
(``from vco_lib.log_setup import ...`` at module level, plus ``_lib.bootstrap``
in ``__main__`` — neither is COPYed into the image). Proven by running the
current source inside the current image: ``ModuleNotFoundError: No module named
'vco_lib'`` at line 76, i.e. a crash loop under ``restart: unless-stopped``.
Nothing rebuilt the image, so nobody ever saw it — the two defects hid each
other. :class:`ImageRunsWithoutVcoLibTests` is the regression that keeps the
image startable.

Every test here drives a production entry point; the mutation red-proofs are
recorded in the lane report.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO_ROOT / "claude_mcp_servers" / "code_embedding_service"

sys.path.insert(0, str(REPO_ROOT))

from vco_lib import code_embed_image, containers  # noqa: E402
from vco_lib import deferral_probes, doctor  # noqa: E402


def _load_image_source():
    spec = importlib.util.spec_from_file_location(
        "_test_image_source", SERVICE_DIR / "image_source.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


image_source = _load_image_source()


# ---------------------------------------------------------------------------
# The shared rule: one digest definition, used by the image AND the host
# ---------------------------------------------------------------------------
class SourceShaTests(unittest.TestCase):
    def test_digest_is_stable_for_the_shipped_tree(self):
        first = image_source.source_sha(SERVICE_DIR)
        self.assertIsInstance(first, str)
        self.assertEqual(first, image_source.source_sha(SERVICE_DIR))

    def test_every_hashed_file_actually_moves_the_digest(self):
        """A file listed but not folded in would under-cover the image silently."""
        import tempfile

        base = image_source.source_sha(SERVICE_DIR)
        for name in image_source.IMAGE_SOURCE_FILES:
            with tempfile.TemporaryDirectory() as tmp:
                copy = Path(tmp) / "svc"
                shutil.copytree(SERVICE_DIR, copy)
                target = copy / name
                target.write_bytes(target.read_bytes() + b"\n# drift\n")
                self.assertNotEqual(
                    image_source.source_sha(copy), base,
                    f"changing {name} did not change the digest",
                )

    def test_a_missing_file_is_unknown_not_a_partial_digest(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "svc"
            shutil.copytree(SERVICE_DIR, copy)
            (copy / "server.py").unlink()
            self.assertIsNone(image_source.source_sha(copy))

    def test_dockerfiles_copy_exactly_the_files_the_digest_covers(self):
        """The two declarative files must agree, or the digest under-covers.

        Not a wiring guard (those are red-proofed by mutation elsewhere) — this
        is a CONSISTENCY gate between two declarations that cannot call each
        other: a ``COPY`` added without extending ``IMAGE_SOURCE_FILES`` would
        put a file in the image that ``/health.source_sha`` does not describe.
        """
        expected = set(image_source.IMAGE_SOURCE_FILES)
        for dockerfile in ("Dockerfile", "Dockerfile.cuda"):
            text = (SERVICE_DIR / dockerfile).read_text()
            copied = set(re.findall(r"^COPY\s+(\S+)\s+\.$", text, re.MULTILINE))
            self.assertEqual(
                copied, expected,
                f"{dockerfile} COPYs {sorted(copied)} but the digest covers "
                f"{sorted(expected)}",
            )


# ---------------------------------------------------------------------------
# The verdict table — every arm, including the two that must NOT read as "fine"
# ---------------------------------------------------------------------------
class ServedStateTests(unittest.TestCase):
    SHA = "a" * 64

    def test_matching_digest_is_current(self):
        state = code_embed_image.served_state(
            self.SHA, {"status": "ok", "source_sha": self.SHA}
        )
        self.assertEqual(state.verdict, code_embed_image.CURRENT)

    def test_different_digest_is_stale(self):
        state = code_embed_image.served_state(
            self.SHA, {"status": "ok", "source_sha": "b" * 64}
        )
        self.assertEqual(state.verdict, code_embed_image.STALE)
        self.assertTrue(state.is_stale)

    def test_health_without_the_field_is_stale_not_unknown(self):
        """The FIELD-ABSENT arm is the whole pre-v0.2.92 population.

        Every image that ships v0.2.92's server.py reports ``source_sha``
        unconditionally, so its absence is positive evidence of an old image —
        the one still truncating silently. Reading it as ``unknown`` would let
        exactly the affected installs slip through as "cannot say".
        """
        state = code_embed_image.served_state(self.SHA, {"status": "ok", "dim": 2048})
        self.assertEqual(state.verdict, code_embed_image.STALE)
        self.assertIn("predates", state.summary)

    def test_null_digest_is_unknown_not_stale(self):
        state = code_embed_image.served_state(
            self.SHA, {"status": "ok", "source_sha": None}
        )
        self.assertEqual(state.verdict, code_embed_image.UNKNOWN)

    def test_no_service_is_unknown(self):
        self.assertEqual(
            code_embed_image.served_state(self.SHA, None).verdict,
            code_embed_image.UNKNOWN,
        )

    def test_error_health_is_unknown(self):
        self.assertEqual(
            code_embed_image.served_state(self.SHA, {"status": "error"}).verdict,
            code_embed_image.UNKNOWN,
        )

    def test_no_source_in_tree_is_unknown(self):
        """A per-project install has no service source; that is not a problem."""
        self.assertEqual(
            code_embed_image.served_state(
                None, {"status": "ok", "source_sha": self.SHA}
            ).verdict,
            code_embed_image.UNKNOWN,
        )

    def test_checkout_digest_matches_the_shared_rule(self):
        self.assertEqual(
            code_embed_image.checkout_source_sha(REPO_ROOT),
            image_source.source_sha(SERVICE_DIR),
        )


# ---------------------------------------------------------------------------
# The service reports its own digest — through the real endpoint
# ---------------------------------------------------------------------------
class HealthReportsSourceShaTests(unittest.TestCase):
    """Drives ``server.health()``, the function FastAPI routes ``GET /health`` to."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("CODE_EMBED_BACKEND", "gpu")
        spec = importlib.util.spec_from_file_location(
            "_test_code_embed_server", SERVICE_DIR / "server.py"
        )
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def _health(self):
        import asyncio

        # Pre-seed the cached dim so /health does not load 7.25 GB of weights
        # (the v0.2.79 §C path the endpoint already takes for a liveness probe).
        self.server._st_model_dim = 2048
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.server.health())
        finally:
            loop.close()

    def test_health_carries_the_digest_of_the_files_it_is_running(self):
        payload = self._health()
        self.assertEqual(payload["status"], "ok")
        self.assertIn(
            "source_sha", payload,
            "the field's ABSENCE is the pre-v0.2.92 signal — it must always be present",
        )
        self.assertEqual(payload["source_sha"], image_source.source_sha(SERVICE_DIR))

    def test_the_host_reads_the_service_as_current(self):
        """End-to-end through the pure comparator: no false 'stale' on a fresh tree."""
        state = code_embed_image.served_state(
            code_embed_image.checkout_source_sha(REPO_ROOT), self._health()
        )
        self.assertEqual(state.verdict, code_embed_image.CURRENT)


# ---------------------------------------------------------------------------
# The image must be able to START — the regression that unblocks the rebuild
# ---------------------------------------------------------------------------
class ImageRunsWithoutVcoLibTests(unittest.TestCase):
    """``python server.py`` must reach uvicorn with NO vco_lib and NO _lib.

    Simulates the shipped image exactly: a directory holding ONLY the files the
    Dockerfiles COPY, with ``vco_lib`` and ``_lib`` made unimportable. Before
    the fix this died at import (``ModuleNotFoundError``) — i.e. every rebuilt
    image would have crash-looped under ``restart: unless-stopped``.
    """

    SENTINEL_EXIT = 42

    def test_module_and_main_both_survive_the_minimal_image(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "app"
            app.mkdir()
            for name in image_source.IMAGE_SOURCE_FILES:
                shutil.copy2(SERVICE_DIR / name, app / name)

            # sitecustomize runs at interpreter startup: block the two packages
            # the image genuinely does not contain.
            (app / "sitecustomize.py").write_text(textwrap.dedent(
                """
                import sys
                class _Block:
                    def find_module(self, name, path=None):
                        return self.find_spec(name, path)
                    def find_spec(self, name, path=None, target=None):
                        root = name.split(".")[0]
                        if root in ("vco_lib", "_lib"):
                            raise ImportError("no module named " + name)
                        return None
                sys.meta_path.insert(0, _Block())
                """
            ))
            # uvicorn.run() is the LAST line of __main__: reaching it proves
            # the whole import path survived. Exiting there keeps the test fast
            # and portless.
            (app / "uvicorn.py").write_text(
                f"def run(*a, **k):\n    raise SystemExit({self.SENTINEL_EXIT})\n"
            )

            env = {
                k: v for k, v in os.environ.items()
                if k not in ("PYTHONPATH", "PYTHONHOME")
            }
            env["PYTHONPATH"] = str(app)
            env["CODE_EMBED_BACKEND"] = "ollama"  # no model load on import
            proc = subprocess.run(
                [sys.executable, "server.py"],
                cwd=str(app), env=env, capture_output=True, text=True, timeout=120,
            )

        combined = proc.stdout + proc.stderr
        self.assertNotIn("ModuleNotFoundError", combined, combined[-2000:])
        self.assertEqual(
            proc.returncode, self.SENTINEL_EXIT,
            f"server.py did not reach uvicorn.run in a minimal image:\n{combined[-2000:]}",
        )
        # Announced, not silent — both degrades name themselves on the log.
        self.assertIn("vco_lib is not importable", combined)
        self.assertIn("_lib is not importable", combined)


# ---------------------------------------------------------------------------
# install.py — the ownership rule and the argv it produces
# ---------------------------------------------------------------------------
def _install_module():
    spec = importlib.util.spec_from_file_location(
        "_test_install_blocker1", REPO_ROOT / "install.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["_test_install_blocker1"] = module
    spec.loader.exec_module(module)
    return module


class RebuildOwnershipTests(unittest.TestCase):
    """The ownership gate. Its END-TO-END effect is covered by ComposeArgvTests,
    which drives ``install._start_services``; this class pins the rule itself.

    The decision vocabulary (``ACTION_ADOPT`` / ``PROBE_*``) is read from
    install.py, which OWNS it — passing the real constants keeps the test
    honest if they are ever renamed.
    """

    @classmethod
    def setUpClass(cls):
        cls.install = _install_module()

    def _allowed(self, decisions, has_gpu, force_separate):
        return code_embed_image.rebuild_allowed(
            decisions, has_gpu, force_separate,
            adopt_action=self.install.ACTION_ADOPT,
            managed_probe=self.install.PROBE_VCT_MANAGED,
        )

    def test_cpu_tier_never_builds(self):
        self.assertFalse(self._allowed({}, False, False))

    def test_foreign_adopt_is_never_touched(self):
        decisions = {"code_embed": {
            "action": self.install.ACTION_ADOPT,
            "probe": self.install.PROBE_FOREIGN,
        }}
        self.assertFalse(self._allowed(decisions, True, False))

    def test_our_own_adopt_may_be_rebuilt(self):
        decisions = {"code_embed": {
            "action": self.install.ACTION_ADOPT,
            "probe": self.install.PROBE_VCT_MANAGED,
        }}
        self.assertTrue(self._allowed(decisions, True, False))

    def test_not_running_may_be_rebuilt(self):
        decisions = {"code_embed": {
            "action": self.install.ACTION_START,
            "probe": self.install.PROBE_NOT_RUNNING,
        }}
        self.assertTrue(self._allowed(decisions, True, False))

    def test_legacy_and_force_separate_paths_may_be_rebuilt(self):
        self.assertTrue(self._allowed(None, True, False))
        self.assertTrue(self._allowed({}, True, True))

    def test_a_probe_that_raises_never_fails_the_install(self):
        """A freshness check must degrade to "no rebuild + a warning line"."""
        boom = mock.patch.object(
            code_embed_image, "image_state", side_effect=RuntimeError("probe blew up"))
        with boom:
            plan = code_embed_image.plan_rebuild(
                decisions=None, has_gpu=True, force_separate=False,
                install_root=REPO_ROOT, url=None,
                services_to_start=[], services_to_recreate=[],
                adopt_action=self.install.ACTION_ADOPT,
                managed_probe=self.install.PROBE_VCT_MANAGED,
            )
        self.assertFalse(plan.build)
        self.assertTrue(any("could not check" in line for line in plan.lines))


class ComposeArgvTests(unittest.TestCase):
    """Drives ``install._start_services`` — the production entry point for the argv."""

    @classmethod
    def setUpClass(cls):
        cls.install = _install_module()

    def _run(self, verdict, decisions=None):
        cmd, _out = self._run_capturing(verdict, decisions)
        return cmd

    def _run_capturing(self, verdict, decisions=None):
        import contextlib
        import io

        install = self.install
        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"] = list(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        state = code_embed_image.ImageState(verdict, f"summary({verdict})")
        sysinfo = install.SystemInfo(
            os_name="Linux", has_gpu=True, has_metal=False, container_cmd="docker",
            gpu_name="RTX 4090", vram_gb=24.0, ram_gb=64.0, gpu_vendor="nvidia",
        )
        decisions = decisions if decisions is not None else {
            "code_embed": {"action": install.ACTION_ADOPT,
                           "probe": install.PROBE_VCT_MANAGED},
            "weaviate": {"action": install.ACTION_ADOPT, "probe": install.PROBE_FOREIGN},
            "ollama": {"action": install.ACTION_ADOPT, "probe": install.PROBE_FOREIGN},
        }
        with mock.patch.object(install, "_detect_existing_volume_paths", return_value={}), \
             mock.patch.object(install, "_detect_existing_services", return_value={
                 "weaviate_url": "http://localhost:8081",
                 "ollama_url": "http://localhost:11435",
                 "code_embed_url": "http://localhost:11440"}), \
             mock.patch.object(install, "_container_runtime_reachable", return_value=True), \
             mock.patch.object(install, "_get_compose_command", return_value=["docker", "compose"]), \
             mock.patch.object(install, "_write_infrastructure_env"), \
             mock.patch.object(install, "_compose_substitution_env", return_value={}), \
             mock.patch.object(install, "_log_install_event"), \
             mock.patch.object(install, "_should_check_weaviate_reclaim_drift", return_value=False), \
             mock.patch.object(code_embed_image, "image_state", return_value=state), \
             mock.patch.object(install._containers, "find_existing_container",
                               side_effect=lambda svc, runtime="podman": f"vco_{svc}"), \
             mock.patch.object(install._containers, "compose_identity_of",
                               return_value=containers.ComposeIdentity("infrastructure")), \
             mock.patch.object(subprocess, "run", side_effect=fake_run):
            # v0.2.93: the identity guard (test_v0293_compose_identity_guard)
            # would otherwise probe the HOST's containers here — stub it as
            # "ours" so this suite stays hermetic and keeps pinning the argv.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                install._start_services(
                    sysinfo, mock.Mock(), {}, decisions=decisions, deferral_report=None,
                )
        return recorded.get("cmd", []), buf.getvalue()

    def test_stale_image_gets_build_and_names_the_service(self):
        cmd, out = self._run_capturing(code_embed_image.STALE)
        self.assertIn("[rebuild]", out)
        self.assertIn("--build", cmd)
        self.assertIn("--force-recreate", cmd)
        self.assertIn("code_embed", cmd)
        # Surgical: no other service is dragged into the recreate.
        self.assertNotIn("weaviate", cmd)

    def test_a_reused_running_service_is_named_so_the_new_image_reaches_it(self):
        """The FIELD shape: code_embed running-and-ours, with no decision row.

        `_classify_service_compose_action` returns "skip" for it, so before
        this fix the service was never named and never recreated — which is
        exactly how the maintainer's machine kept a 2026-05-16 image through a
        2026-07-12 `--force-recreate`. `--build` alone is not enough here: a
        rebuilt image does not reach a container compose was not asked to
        touch, so the service must ALSO be named for `--force-recreate`.
        """
        install = self.install
        decisions = {
            "weaviate": {"action": install.ACTION_ADOPT, "probe": install.PROBE_FOREIGN},
            "ollama": {"action": install.ACTION_ADOPT, "probe": install.PROBE_FOREIGN},
            # no code_embed row at all → disposition "skip"
        }
        cmd, out = self._run_capturing(code_embed_image.STALE, decisions=decisions)
        self.assertIn("[rebuild]", out)
        self.assertIn("--build", cmd)
        self.assertIn("--force-recreate", cmd)
        self.assertIn("code_embed", cmd)

    def test_unknown_image_state_also_builds(self):
        """Could-not-look must never read as 'current' — build rather than assume."""
        self.assertIn("--build", self._run(code_embed_image.UNKNOWN))

    def test_current_image_does_not_build(self):
        install = self.install
        decisions = {
            "code_embed": {"action": install.ACTION_ADOPT,
                           "probe": install.PROBE_VCT_MANAGED},
            "weaviate": {"action": install.ACTION_START,
                         "probe": install.PROBE_NOT_RUNNING},
            "ollama": {"action": install.ACTION_ADOPT, "probe": install.PROBE_FOREIGN},
        }
        cmd, out = self._run_capturing(code_embed_image.CURRENT, decisions=decisions)
        self.assertTrue(cmd, "compose should still have run for weaviate")
        self.assertNotIn("--build", cmd)
        # The escalation did not fire at all. (`code_embed` may still appear in
        # the argv: an ADOPT we own is force-recreated for CONFIG drift by the
        # pre-existing v0.2.61 rule — a different reason, tested elsewhere.)
        self.assertNotIn("[rebuild]", out)

    def test_foreign_adopt_is_not_rebuilt_even_when_stale(self):
        install = self.install
        decisions = {
            "code_embed": {"action": install.ACTION_ADOPT,
                           "probe": install.PROBE_FOREIGN},
            "weaviate": {"action": install.ACTION_START,
                         "probe": install.PROBE_NOT_RUNNING},
            "ollama": {"action": install.ACTION_ADOPT, "probe": install.PROBE_FOREIGN},
        }
        cmd, out = self._run_capturing(code_embed_image.STALE, decisions=decisions)
        self.assertNotIn("--build", cmd)
        self.assertNotIn("code_embed", cmd)
        self.assertNotIn("[rebuild]", out)


# ---------------------------------------------------------------------------
# doctor — the reported, deferred, and self-clearing halves
# ---------------------------------------------------------------------------
class DoctorProbeTests(unittest.TestCase):
    def _report(self, state):
        res = doctor.DoctorResolvers(code_embed_state=lambda root: state)
        return doctor.run_doctor(REPO_ROOT, scope=doctor.SCOPE_FULL, resolvers=res)

    def _finding(self, state):
        report = self._report(state)
        return next(f for f in report.findings if f.probe == "code_embed_image")

    def test_stale_is_a_problem_naming_the_registered_condition(self):
        f = self._finding(code_embed_image.ImageState(
            code_embed_image.STALE, "old image"))
        self.assertEqual(f.status, doctor.STATUS_PROBLEM)
        self.assertEqual(f.condition_id, doctor.CID_CODE_EMBED_IMAGE_STALE)
        self.assertIn("install.py --update", f.command)

    def test_current_is_ok(self):
        f = self._finding(code_embed_image.ImageState(
            code_embed_image.CURRENT, "fresh"))
        self.assertEqual(f.status, doctor.STATUS_OK)

    def test_unknown_is_unknown_not_ok(self):
        f = self._finding(code_embed_image.ImageState(
            code_embed_image.UNKNOWN, "service down"))
        self.assertEqual(f.status, doctor.STATUS_UNKNOWN)

    def test_stale_emits_the_registered_deferral(self):
        entries = doctor.deferral_entries_for(self._report(
            code_embed_image.ImageState(code_embed_image.STALE, "old image")))
        cids = [e.condition_id for e in entries]
        self.assertIn("code_embed_image_stale", cids)
        entry = next(e for e in entries if e.condition_id == "code_embed_image_stale")
        self.assertIn("BEFORE", entry.why_deferred)

    def test_a_user_project_is_not_probed(self):
        """No vco_lib/ in the folder ⇒ not an install root ⇒ no finding at all."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            res = doctor.DoctorResolvers(
                code_embed_state=lambda root: code_embed_image.ImageState(
                    code_embed_image.STALE, "old"))
            report = doctor.run_doctor(Path(tmp), scope=doctor.SCOPE_FULL, resolvers=res)
            self.assertEqual(
                [f for f in report.findings if f.probe == "code_embed_image"], []
            )

    def test_the_condition_is_registered_with_its_clear_probe(self):
        from vco_lib.deferral_registry import condition

        spec = condition("code_embed_image_stale")
        self.assertIsNotNone(spec, "condition must be declared in the registry")
        self.assertEqual(spec.clear_probe, "probe:py:code_embed_image_still_stale")
        self.assertEqual(
            deferral_probes.registry_probe_name("code_embed_image_stale"),
            "code_embed_image_still_stale",
        )


class ClearProbeTests(unittest.TestCase):
    def _probe(self, verdict):
        state = code_embed_image.ImageState(verdict, "x")
        with mock.patch.object(code_embed_image, "image_state", return_value=state):
            return deferral_probes.run_probe(
                "code_embed_image_still_stale",
                deferral_probes.ProbeContext(folder=REPO_ROOT),
            )

    def test_still_stale_keeps_the_entry(self):
        self.assertIs(self._probe(code_embed_image.STALE), True)

    def test_current_clears_the_entry(self):
        self.assertIs(self._probe(code_embed_image.CURRENT), False)

    def test_unknown_neither_clears_nor_confirms(self):
        self.assertIsNone(self._probe(code_embed_image.UNKNOWN))


if __name__ == "__main__":
    unittest.main()

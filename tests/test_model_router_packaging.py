# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Delivery: does this package actually reach a third-party machine?

The defect this file exists for was found before a line of the gateway was
written and would have made every other test worthless:
``claude_mcp_servers/pyproject.toml`` declared
``packages = ["weaviate_mcp"]``, so a new ``model_router/`` directory would
have been built, tested and reviewed — and then silently omitted from the
install. Correct code that never reaches a user is not delivered.

Proof, not assertion. The wheel is BUILT from the real ``pyproject.toml`` and
then INSTALLED into a throwaway virtualenv with ``--no-deps``, and the
assertions run against the installed copy: the modules import, the shipped
JSON data files are real files inside site-packages, and the console script
exists as an executable. ``--no-deps`` keeps it hermetic — no network, and no
dependency of the distribution needs to be present for the packaging facts to
be checkable.

Both heavy phases skip — with a stated reason — only when the environment
genuinely CANNOT do the work: ``build`` or ``venv`` is absent, or ``python -m
venv`` will not create one. ``build`` is declared in ``requirements-dev.txt``
precisely so that arm does not fire on CI; if it ever does, the packaging
proof has silently stopped running and the skip reason says so.

Everything past that point is a RESULT, and a bad result is a FAILURE, never a
skip (v0.2.92 MAJOR-11). A wheel build that returns non-zero, a build that
emits no wheel or several, and an install that returns non-zero are all
``AssertionError`` with the captured output attached. They used to be
``SkipTest``, which made a broken wheel indistinguishable from a minimal
runner — and CI, which had no ``build``, reported "7 skipped" for the only
evidence that this package reaches a user at all.

``setUpClass`` cannot call ``self.fail`` (there is no instance yet), so these
raise ``AssertionError`` directly — the exact exception ``TestCase.fail``
raises, and unittest reports it against every test in the class.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_ROOT = REPO_ROOT / "claude_mcp_servers"
PYPROJECT = MCP_ROOT / "pyproject.toml"

_BUILD_TIMEOUT_S = 300
_INSTALL_TIMEOUT_S = 300
_RUN_TIMEOUT_S = 90

#: Files that must be INSIDE the wheel, not merely beside the source.
REQUIRED_WHEEL_MEMBERS = (
    "model_router/__init__.py",
    "model_router/__main__.py",
    "model_router/server.py",
    "model_router/routing.py",
    "model_router/vendors.py",
    "model_router/catalog.py",
    "model_router/context_table.py",
    "model_router/secrets.py",
    "model_router/auth.py",
    "model_router/config.py",
    "model_router/fileperms.py",
    "model_router/quota.py",
    "model_router/tool_ids.py",
    "model_router/chat_model_context.seed.json",
    "model_router/static_catalog.json",
)

CONSOLE_SCRIPT = "vct-model-gateway"


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


class PyprojectDeclarationTests(unittest.TestCase):
    """Static checks — fast, and they name the defect if it comes back."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

    def test_model_router_is_in_the_wheel_package_list(self) -> None:
        packages = self.data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        self.assertIn(
            "model_router", packages,
            "model_router is not in the wheel package list, so `pip install -e "
            "claude_mcp_servers/` would not install it and neither the console "
            "script nor `python -m model_router` would resolve.",
        )

    def test_weaviate_mcp_is_still_in_the_wheel_package_list(self) -> None:
        """Regression guard on the pre-existing package while amending it."""
        packages = self.data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        self.assertIn("weaviate_mcp", packages)

    def test_the_console_script_is_declared(self) -> None:
        scripts = self.data.get("project", {}).get("scripts", {})
        self.assertEqual(scripts.get(CONSOLE_SCRIPT), "model_router.__main__:main")

    def test_the_console_script_target_exists_and_is_callable(self) -> None:
        """A declared entry point pointing at nothing is a promise, not a CLI."""
        module_name, _, attr = self.data["project"]["scripts"][CONSOLE_SCRIPT].partition(":")
        module = __import__(module_name, fromlist=[attr])
        self.assertTrue(callable(getattr(module, attr)))

    def test_every_declared_package_directory_exists(self) -> None:
        packages = self.data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        for package in packages:
            with self.subTest(package=package):
                self.assertTrue(
                    (MCP_ROOT / package / "__init__.py").is_file(),
                    f"{package} is declared for the wheel but has no "
                    f"__init__.py under {MCP_ROOT}",
                )

    def test_aiohttp_is_declared_so_the_gateway_has_its_server(self) -> None:
        deps = " ".join(self.data["project"]["dependencies"])
        self.assertIn("aiohttp", deps)

    def test_no_dependency_was_added_for_the_gateway(self) -> None:
        """The gateway needs aiohttp (already there) and vco_lib (installed by
        the root distribution). A new third-party dependency on a build host is
        a delivery risk, so this pins that none was introduced."""
        deps = {
            d.split(">")[0].split("=")[0].split("<")[0].strip()
            for d in self.data["project"]["dependencies"]
        }
        self.assertEqual(
            deps,
            {"mcp", "weaviate-client", "aiohttp", "httpx", "pydantic", "pyyaml"},
        )


class ImportSurfaceTests(unittest.TestCase):
    """The import path the console script and the service unit rely on."""

    def test_the_package_imports_under_its_installed_name(self) -> None:
        import model_router

        self.assertTrue(model_router.__file__)

    def test_the_package_has_no_import_time_dependency_on_anything(self) -> None:
        """``import model_router`` must work before deps are present, so a
        post-install smoke check can use it."""
        completed = subprocess.run(
            [sys.executable, "-c", "import model_router; print(model_router.__version__)"],
            capture_output=True, text=True, timeout=_RUN_TIMEOUT_S,
            env={**os.environ, "PYTHONPATH": str(MCP_ROOT)},
            cwd=tempfile.gettempdir(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_pure_modules_import_without_aiohttp_or_vco_lib(self) -> None:
        """``routing`` and ``vendors`` are stdlib-only by design; the packaging
        smoke check leans on that."""
        script = (
            "import sys\n"
            "for blocked in ('aiohttp', 'vco_lib'):\n"
            "    sys.modules[blocked] = None\n"
            "import model_router.routing, model_router.vendors\n"
            "print(model_router.vendors.VENDORS.keys())\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=_RUN_TIMEOUT_S,
            env={**os.environ, "PYTHONPATH": str(MCP_ROOT)},
            cwd=tempfile.gettempdir(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_shipped_data_files_sit_beside_the_module(self) -> None:
        """Resolved relative to ``__file__``, never to a checkout root, so the
        install location does not matter."""
        from model_router.catalog import STATIC_CATALOG_PATH
        from model_router.context_table import SEED_PATH

        import model_router

        package_dir = Path(model_router.__file__).resolve().parent
        for path in (SEED_PATH, STATIC_CATALOG_PATH):
            with self.subTest(path=path.name):
                self.assertEqual(path.parent, package_dir)
                self.assertTrue(path.is_file())


class WheelContentTests(unittest.TestCase):
    """Build the real wheel and look inside it."""

    wheel: Path | None = None
    build_dir: Path | None = None

    @classmethod
    def setUpClass(cls) -> None:
        if not _has_module("build"):
            raise unittest.SkipTest(
                "`build` is not installed in this environment — skipping the "
                "wheel-content checks. `pip install build` enables them.",
            )
        cls.build_dir = Path(tempfile.mkdtemp(prefix="wp9-wheel-"))
        args = [sys.executable, "-m", "build", "--wheel",
                "--outdir", str(cls.build_dir), str(MCP_ROOT)]
        if _has_module("hatchling"):
            args.insert(4, "--no-isolation")
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=_BUILD_TIMEOUT_S,
        )
        if result.returncode != 0:
            shutil.rmtree(cls.build_dir, ignore_errors=True)
            raise AssertionError(
                f"wheel build FAILED (rc={result.returncode}) — the package "
                "cannot be delivered. This is a failure, not a skip: a broken "
                "wheel and a minimal runner must not look alike.\n"
                f"argv: {args}\n"
                f"stdout tail: {result.stdout.strip()[-600:]}\n"
                f"stderr tail: {result.stderr.strip()[-600:]}",
            )
        wheels = list(cls.build_dir.glob("*.whl"))
        if len(wheels) != 1:
            shutil.rmtree(cls.build_dir, ignore_errors=True)
            raise AssertionError(
                f"the build returned rc=0 but produced {len(wheels)} wheels "
                f"({wheels!r}); exactly one is required to check its contents. "
                "A build that emits nothing is a delivery failure, not an "
                "environment limitation.",
            )
        cls.wheel = wheels[0]

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.build_dir is not None:
            shutil.rmtree(cls.build_dir, ignore_errors=True)

    def _names(self) -> list[str]:
        assert self.wheel is not None
        with zipfile.ZipFile(self.wheel) as archive:
            return archive.namelist()

    def test_wheel_contains_every_module_and_data_file(self) -> None:
        names = self._names()
        for member in REQUIRED_WHEEL_MEMBERS:
            with self.subTest(member=member):
                self.assertIn(
                    member, names,
                    f"{member} is missing from the wheel. Without it the "
                    "gateway cannot start after a real install, however well "
                    "it runs from the checkout.",
                )

    def test_wheel_declares_the_console_script(self) -> None:
        assert self.wheel is not None
        with zipfile.ZipFile(self.wheel) as archive:
            entry_points = [n for n in archive.namelist() if n.endswith("entry_points.txt")]
            self.assertTrue(entry_points, "no entry_points.txt in the wheel")
            body = archive.read(entry_points[0]).decode("utf-8")
        self.assertIn("[console_scripts]", body)
        self.assertIn(f"{CONSOLE_SCRIPT} = model_router.__main__:main", body)

    def test_wheel_still_contains_the_pre_existing_package(self) -> None:
        self.assertIn("weaviate_mcp/__init__.py", self._names())


class FreshVenvInstallTests(unittest.TestCase):
    """Install the wheel into a throwaway venv and use it from there.

    This is the check that would have caught the packaging defect: everything
    above can pass against a source checkout, and only an install proves the
    files travel.
    """

    venv_dir: Path | None = None
    build_dir: Path | None = None
    python: Path | None = None

    @classmethod
    def setUpClass(cls) -> None:
        if not _has_module("build"):
            raise unittest.SkipTest("`build` is not installed — skipping install proof")
        if not _has_module("venv"):
            raise unittest.SkipTest("`venv` is unavailable — skipping install proof")

        cls.build_dir = Path(tempfile.mkdtemp(prefix="wp9-install-build-"))
        args = [sys.executable, "-m", "build", "--wheel",
                "--outdir", str(cls.build_dir), str(MCP_ROOT)]
        if _has_module("hatchling"):
            args.insert(4, "--no-isolation")
        built = subprocess.run(
            args, capture_output=True, text=True, timeout=_BUILD_TIMEOUT_S,
        )
        if built.returncode != 0:
            shutil.rmtree(cls.build_dir, ignore_errors=True)
            raise AssertionError(
                f"wheel build FAILED (rc={built.returncode}) — the install "
                "proof cannot run because there is nothing to install. This is "
                "a failure, not a skip.\n"
                f"argv: {args}\n"
                f"stdout tail: {built.stdout.strip()[-600:]}\n"
                f"stderr tail: {built.stderr.strip()[-600:]}",
            )
        wheels = list(cls.build_dir.glob("*.whl"))
        if len(wheels) != 1:
            shutil.rmtree(cls.build_dir, ignore_errors=True)
            raise AssertionError(
                f"the build returned rc=0 but produced {len(wheels)} wheels "
                f"({wheels!r}); exactly one is required to install.",
            )
        wheel = wheels[0]

        cls.venv_dir = Path(tempfile.mkdtemp(prefix="wp9-install-venv-"))
        made = subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(cls.venv_dir)],
            capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S,
        )
        if made.returncode != 0:
            cls._cleanup()
            raise unittest.SkipTest(f"venv creation failed: {made.stderr[-400:]}")

        scripts = "Scripts" if os.name == "nt" else "bin"
        exe = "python.exe" if os.name == "nt" else "python"
        cls.python = cls.venv_dir / scripts / exe

        # --no-deps + --target-less install into the fresh venv, driven by the
        # CALLER's pip (the venv is created without pip so nothing is fetched).
        purelib = sysconfig.get_path(
            "purelib",
            vars={
                "base": str(cls.venv_dir),
                "platbase": str(cls.venv_dir),
                "installed_base": str(cls.venv_dir),
                "installed_platbase": str(cls.venv_dir),
            },
        )
        installed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
             "--no-compile", "--target", purelib,
             "--python-version", f"{sys.version_info.major}.{sys.version_info.minor}",
             "--only-binary", ":all:", str(wheel)],
            capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S,
        )
        if installed.returncode != 0:
            cls._cleanup()
            raise AssertionError(
                f"wheel install FAILED (rc={installed.returncode}) — the wheel "
                "built but does not install, which is exactly the delivery "
                "defect this file exists to catch. This is a failure, not a "
                "skip.\n"
                f"stdout tail: {installed.stdout.strip()[-600:]}\n"
                f"stderr tail: {installed.stderr.strip()[-600:]}",
            )
        cls.purelib = Path(purelib)

    @classmethod
    def _cleanup(cls) -> None:
        for directory in (cls.build_dir, cls.venv_dir):
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._cleanup()

    def test_the_installed_package_imports_from_site_packages(self) -> None:
        assert self.python is not None
        script = (
            "import model_router, pathlib\n"
            "print(pathlib.Path(model_router.__file__).resolve())\n"
        )
        completed = subprocess.run(
            [str(self.python), "-c", script],
            capture_output=True, text=True, timeout=_RUN_TIMEOUT_S,
            cwd=tempfile.gettempdir(),
            env={**os.environ, "PYTHONPATH": str(self.purelib)},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        resolved = Path(completed.stdout.strip())
        self.assertTrue(
            str(resolved).startswith(str(self.purelib)),
            f"imported {resolved}, which is NOT the installed copy under "
            f"{self.purelib} — the test would be validating the checkout",
        )

    def test_the_installed_copy_carries_its_data_files(self) -> None:
        assert self.python is not None
        script = (
            "from model_router.context_table import load_seed, SEED_PATH\n"
            "from model_router.catalog import STATIC_CATALOG_PATH\n"
            "assert SEED_PATH.is_file(), SEED_PATH\n"
            "assert STATIC_CATALOG_PATH.is_file(), STATIC_CATALOG_PATH\n"
            "print(len(load_seed().rows))\n"
        )
        completed = subprocess.run(
            [str(self.python), "-c", script],
            capture_output=True, text=True, timeout=_RUN_TIMEOUT_S,
            cwd=tempfile.gettempdir(),
            env={**os.environ, "PYTHONPATH": str(self.purelib)},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # 10 GLM rows + the 4 Claude 5 family rows added in v0.2.93 (the
        # installed copy must carry the SAME seed the checkout ships).
        self.assertEqual(completed.stdout.strip(), "14")

    def test_the_console_script_is_installed_and_executable(self) -> None:
        """A declared entry point that produces no runnable file is a promise."""
        candidates = [
            self.purelib.parent.parent / "bin" / CONSOLE_SCRIPT,
            self.purelib.parent.parent / "Scripts" / f"{CONSOLE_SCRIPT}.exe",
            self.purelib / "bin" / CONSOLE_SCRIPT,
            self.purelib / f"{CONSOLE_SCRIPT}.exe",
            self.purelib.parent / "Scripts" / f"{CONSOLE_SCRIPT}.exe",
        ]
        # `pip install --target` puts scripts in <target>/bin on POSIX and
        # <target>/../Scripts on Windows depending on version; accept any and
        # report all when none match, so a layout change is diagnosable.
        found = [path for path in candidates if path.exists()]
        self.assertTrue(
            found,
            "the console script was not produced by the install. Looked at: "
            + ", ".join(str(c) for c in candidates),
        )

    def test_running_the_entry_point_module_works_from_an_unrelated_cwd(self) -> None:
        """``python -m model_router --version`` from a directory that is not
        the checkout — the situation a service unit actually runs in."""
        assert self.python is not None
        completed = subprocess.run(
            [str(self.python), "-m", "model_router", "--version"],
            capture_output=True, text=True, timeout=_RUN_TIMEOUT_S,
            cwd=tempfile.gettempdir(),
            env={**os.environ, "PYTHONPATH": str(self.purelib)},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        import model_router

        self.assertEqual(completed.stdout.strip(), model_router.__version__)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

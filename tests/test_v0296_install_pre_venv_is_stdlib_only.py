# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""install.py's PRE-VENV phase must run on the standard library alone.

The install flow's own ordering is the reason: ``main()`` creates the venv
(``_create_venv``) and then installs the dependencies into it, so everything it
calls BEFORE that runs on whatever interpreter launched ``install.py`` — on a
fresh clone, the only state a first install is ever in, the system Python with
no third-party packages at all.

The boundary is named by FUNCTION rather than step number throughout this file,
because the numbers are not reliable: ``main()``'s comments call the embedding
reconcile "Step 3" and venv creation "Step 4", ``docs/INSTALL_ARCHITECTURE_v2.md``
§4 numbers the same two 2 and 3, and the logged step id for both is ``3/10``.

So an in-process import of ``requests`` / ``yaml`` / ``weaviate`` reached before
the venv exists is not a degraded path, it is a crashed install. v0.2.96 shipped
exactly that: a consolidation pointed install.py's embedding reconcile at
``vco_lib.embedding_service``, which imports ``requests`` at module scope, and
install-smoke went red on all five platforms with
``ModuleNotFoundError: No module named 'requests'``.

**Why a developer machine cannot see this and CI can**: every checkout that has
ever run the test suite has ``requests`` installed, so the import resolves
locally no matter which interpreter runs it. The defect is invisible until a
machine without it runs the code — which, before this test, meant waiting an
hour for install-smoke. The blocker below manufactures that machine in-process.

Once the venv exists the rule RELAXES rather than disappears: the process is still on the
system interpreter (it does not re-exec), so a later in-process third-party
import must carry a working fallback. ``install.py::_enrich_slot_change`` is the
reference shape — it catches the ImportError at step 7c and re-embeds the
expensive way. That is why this test pins the pre-venv callables by name instead
of asserting "install.py never touches a third-party module".
"""

from __future__ import annotations

import ast
import builtins
import sys

import pytest

from tests.common.import_chain import (
    REPO_ROOT as REPO,
    requirements_import_roots,
    third_party_reachable_from,
)

#: Third-party packages the install's dependency step puts in the venv. A fresh
#: clone's system Python has none of them, so anything reached before that step
#: must not import any of them, directly or transitively.
#:
#: DERIVED from requirements.txt, not hand-listed: the first draft listed seven
#: names from memory and missed nine, including `pydantic`, `torch` and
#: `ollama`. A gate whose coverage is a hand-maintained guess reports on the
#: part of the problem its author already thought of.
POST_VENV_PACKAGES = requirements_import_roots()


class _FreshClonePython:
    """Context manager that makes the post-venv packages unimportable.

    Patches ``builtins.__import__`` rather than ``sys.meta_path`` so an
    ALREADY-IMPORTED module is caught too: the suite imports ``requests`` long
    before this test runs, so a meta-path finder would never be consulted and
    the test would pass vacuously while the real fresh clone still failed.

    ``evict`` is load-bearing for the same reason, one level up. A blocked
    package is only re-imported if the vco_lib module that imports it is
    itself re-imported — with ``vco_lib.embedding_service`` still in
    ``sys.modules``, ``from vco_lib.embedding_service import ...`` is a cache
    hit and the ``import requests`` at its module scope never re-runs. The
    first draft of this file omitted the eviction, and its runtime test passed
    against the very code that was red on all five CI platforms.

    **Known limit**: ``importlib.import_module`` does not route through
    ``builtins.__import__``, so an import spelled that way would slip past the
    guard. ``install.py`` contains no occurrence of it at all (grepped, not
    assumed), and the static sweep below reads the AST rather than executing
    it, so the two together still cover the class — but do not read a green
    runtime test as proof on its own if that spelling ever appears.
    """

    def __init__(self, blocked: frozenset[str], evict: tuple[str, ...] = ()) -> None:
        self._blocked = blocked
        self._evict = evict
        self._real = builtins.__import__

    def __enter__(self) -> _FreshClonePython:
        blocked = self._blocked
        real = self._real

        def guarded(name, globals=None, locals=None, fromlist=(), level=0):
            root = name.split(".", 1)[0]
            if level == 0 and root in blocked:
                raise ModuleNotFoundError(f"No module named {root!r}")
            return real(name, globals, locals, fromlist, level)

        builtins.__import__ = guarded
        # Drop cached copies so a module-scope `import requests` in a module
        # imported DURING the block actually re-enters __import__.
        self._evicted = {
            mod: sys.modules.pop(mod)
            for mod in list(sys.modules)
            if mod.split(".", 1)[0] in blocked
            or mod in self._evict
            or any(mod.startswith(f"{prefix}.") for prefix in self._evict)
        }
        return self

    def __exit__(self, *exc: object) -> None:
        builtins.__import__ = self._real
        # Whatever the block imported under the guard is discarded in favour of
        # the real modules captured on entry, so a partially-initialised module
        # cannot leak into the rest of the suite.
        for mod in list(sys.modules):
            if mod in self._evicted:
                del sys.modules[mod]
        sys.modules.update(self._evicted)
        # Restoring sys.modules is NOT enough: a package holds its submodules
        # as ATTRIBUTES, and `import vco_lib.x` inside the block rebound
        # `vco_lib.x` on the parent to the block-time copy. Leave that and
        # `from vco_lib.embedding_selection import C` (sys.modules) and
        # `vco_lib.embedding_selection.C` (attribute) return two different
        # class objects for the rest of the session — an isinstance/setattr
        # failure in some later test, ordering-dependent and hostile to debug.
        for name, module in self._evicted.items():
            parent_name, _, child = name.rpartition(".")
            parent = sys.modules.get(parent_name) if parent_name else None
            if parent is not None and getattr(parent, child, None) is not module:
                setattr(parent, child, module)


#: vco_lib modules that must be re-imported inside the block for the guard to
#: reach their module-scope third-party imports.
EVICT_FOR_FRESH_IMPORT = (
    "vco_lib.embedding_service",
    "vco_lib.embedding_providers",
    "vco_lib.embedding_enrichment",
    "vco_lib.embedding_selection",
)


def test_the_blocker_actually_blocks():
    """Red-proof the harness itself, so a passing test below means something."""
    with _FreshClonePython(POST_VENV_PACKAGES):
        with pytest.raises(ModuleNotFoundError):
            __import__("requests")
    # ...and the real import works again once the block lifts.
    __import__("requests")


def test_the_embedding_reconcile_survives_a_fresh_clone():
    """The exact call that went red on all five platforms in install-smoke.

    ``main()`` calls ``_reconcile_install_active_embedding`` before
    ``_create_venv``, and that calls ``_model_id_for_active``. Under the pre-fix code this raised
    ``ModuleNotFoundError: requests`` through
    ``vco_lib.embedding_service``'s module-scope provider-stack import.
    """
    import install as install_mod

    with _FreshClonePython(POST_VENV_PACKAGES, EVICT_FOR_FRESH_IMPORT):
        assert install_mod._model_id_for_active("qwen3") == "qwen3-embedding:0.6b"
        assert (
            install_mod._model_id_for_active("arctic")
            == "snowflake-arctic-embed2:latest"
        )
        assert install_mod._model_id_for_active("openai") == "text-embedding-3-small"
        assert install_mod._model_id_for_active("nosuchprofile") == (
            "qwen3-embedding:0.6b"
        )


def test_the_shared_home_is_importable_on_a_fresh_clone():
    """``vco_lib.embedding_selection`` is the home BECAUSE it imports cleanly.

    Its module docstring has claimed "pure leaf — stdlib only" since v0.2.68.
    This is that claim's enforcement: a future import added to it would move
    the profile table back out of reach of the pre-venv phase, which is the
    regression this cycle already shipped once.
    """
    with _FreshClonePython(POST_VENV_PACKAGES, EVICT_FOR_FRESH_IMPORT):
        import vco_lib.embedding_selection as sel

        assert sel.model_id_for_active("arctic") == "snowflake-arctic-embed2:latest"


def test_the_parent_package_attribute_is_restored_too():
    """The eviction must not leave two live copies of an evicted module.

    ``import vco_lib.x`` rebinds ``x`` as an ATTRIBUTE of the ``vco_lib``
    package object, and that binding survives a ``sys.modules`` restore. A
    block that imported a module under the guard therefore used to leave
    ``vco_lib.embedding_selection`` (attribute) and
    ``sys.modules["vco_lib.embedding_selection"]`` pointing at two different
    module objects, with two different copies of every class in them —
    ordering-dependent breakage for any later ``isinstance`` or
    ``monkeypatch.setattr`` on the attribute path.
    """
    import vco_lib

    with _FreshClonePython(POST_VENV_PACKAGES, EVICT_FOR_FRESH_IMPORT):
        import vco_lib.embedding_selection  # noqa: F401  # pyright: ignore[reportUnusedImport] — the import IS the action (it rebinds the parent attribute)

    for dotted in EVICT_FOR_FRESH_IMPORT:
        cached = sys.modules.get(dotted)
        if cached is None:
            continue
        child = dotted.rpartition(".")[2]
        assert getattr(vco_lib, child, None) is cached, (
            f"{dotted}: the package attribute and sys.modules disagree — the "
            "guard leaked a second copy of the module into the session"
        )


def test_no_third_party_import_is_reachable_before_the_venv():
    """Static sweep of install.py's function-local imports.

    The runtime tests above pin the ONE call that broke. This pins the class:
    for every import written inside an install.py function, flag any
    third-party package it reaches — directly, or transitively through
    ``vco_lib`` module-scope imports.

    A flagged entry is not automatically a bug. It is a bug unless the
    importing function SOFT-FAILS, so each deliberate one is listed in
    ``SOFT_FAILING`` with the fallback that makes it survivable, and a new one
    fails this test until someone makes that judgement explicitly.

    Scope note, learned the hard way: an earlier version of this sweep looked
    only at ``vco_lib`` imports, so a bare ``import psutil`` in an install.py
    function was invisible to it. Both are now collected — the question is
    "does this reach a package the venv has not installed yet", and how the
    import is spelled does not change the answer.
    """
    stdlib = set(sys.stdlib_module_names)

    #: function name -> why an ImportError inside it is survivable.
    SOFT_FAILING = {
        "_enrich_slot_change": (
            "step 7c, cost optimisation only: catches the ImportError, prints "
            "the reason and returns False, and the caller then re-embeds the "
            "expensive way (the behaviour of every release before v0.2.95)."
        ),
        # Both of these run from _detect_system — genuinely pre-venv — and are safe for
        # the reason the rule allows: psutil only REFINES an answer the stdlib
        # can already give, and `except ImportError: pass` falls through to it.
        # Verified by reading both bodies, not by their names.
        "_probe_system_ram_gb": (
            "`import psutil` under `except ImportError: pass`, falling through "
            "to /proc/meminfo (Linux), `sysctl hw.memsize` (macOS) and wmic "
            "(Windows). The stdlib path is the real implementation."
        ),
        "_probe_cpu_cores": (
            "same shape: `except ImportError: pass` falls through to "
            "`os.cpu_count() // 2`, deliberately conservative. psutil only "
            "improves physical-vs-logical accuracy."
        ),
    }

    local_imports: dict[str, set[str]] = {}

    class Collector(ast.NodeVisitor):
        """Record every import written inside a top-level install.py function.

        Attribution is to the OUTERMOST enclosing function, which is what
        SOFT_FAILING is keyed on; a nested helper's import is charged to the
        function that contains it, and that is the right granularity because
        the enclosing function is where the try/except would live.
        """

        def __init__(self) -> None:
            self.func: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.func.append(node.name)
            self.generic_visit(node)
            self.func.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def _record(self, target: str) -> None:
            if self.func:
                local_imports.setdefault(self.func[0], set()).add(target)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            module = node.module or ""
            if node.level:  # relative import inside install.py: not a package
                return
            if module == "vco_lib":
                for alias in node.names:
                    self._record(f"vco_lib.{alias.name}")
            elif module.startswith("vco_lib."):
                self._record(module)
            elif module:
                self._record(module.split(".")[0])

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                if alias.name.startswith("vco_lib."):
                    self._record(alias.name)
                else:
                    self._record(alias.name.split(".")[0])

    Collector().visit(ast.parse((REPO / "install.py").read_text("utf-8")))
    assert len(local_imports) > 10, (
        f"the walk collapsed ({len(local_imports)} functions) — it proves nothing"
    )

    violations = []
    for func, targets in sorted(local_imports.items()):
        if func in SOFT_FAILING:
            continue
        for target in sorted(targets):
            if target.startswith("vco_lib."):
                reached = third_party_reachable_from(target.split(".", 1)[1])
                for package, via in sorted(reached.items()):
                    violations.append(
                        f"{func}() imports {target}, which reaches "
                        f"{package!r} at the module scope of vco_lib.{via}"
                    )
            elif target not in stdlib:
                violations.append(f"{func}() imports {target!r} directly")

    assert not violations, (
        "install.py may reach a third-party dependency in-process only where an "
        "ImportError is survivable — before the venv exists there is nothing to "
        "import, and after it the process still never re-execs into it. Either "
        "move the logic to a stdlib-only vco_lib leaf (as "
        "vco_lib.embedding_selection holds the embedding-profile table), or "
        "soft-fail the call and add it to SOFT_FAILING with the fallback that "
        "makes it safe:\n  " + "\n  ".join(violations)
    )


def test_the_sweep_sees_the_subpackages_it_used_to_skip():
    """Regression guard on the walker itself, not on install.py.

    The first version resolved a dotted target as a FILE
    (``vco_lib/embedding_providers.openai.py``), found nothing, and skipped it
    — so the five ``import requests`` in ``vco_lib/embedding_providers/*.py``
    were invisible and the real defect was caught only by a coincidence
    (``embedding_service`` imports ``requests`` directly as well). A walker
    that silently skips what it cannot resolve reports clean for the wrong
    reason.
    """
    reached = third_party_reachable_from("embedding_providers.openai")
    assert "requests" in reached, (
        "the walker no longer descends into vco_lib subpackages — it would "
        "report a clean install.py for a chain that is not clean"
    )

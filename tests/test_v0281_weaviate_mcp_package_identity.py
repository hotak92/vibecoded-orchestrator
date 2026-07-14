# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.81 regression: weaviate_mcp/server.py must establish REAL package
identity when launched as a BARE SCRIPT, so its relative imports (and the
submodules' own ``from . import server``) resolve under BOTH entry points.

Background (the bug this pins):
- The launcher starts the weaviate-kg MCP as a bare script:
  ``<venv>/python .../claude_mcp_servers/weaviate_mcp/server.py`` (NOT
  ``python -m weaviate_mcp.server``). Run that way server.py is ``__main__``
  with an EMPTY ``__package__``.
- Before the fix, server.py's relative imports (``from .chunking``,
  ``from . import embeddings`` …) only survived via a ``try: from .X
  except ImportError: from X`` fallback that imported the submodules as
  TOP-LEVEL modules (``__package__ == ''``). Those submodules then did their
  OWN ``from . import server`` INSIDE functions → ``ImportError: attempted
  relative import with no known parent package`` at QUERY TIME (the fatal
  ``hybrid_search`` failure). Plus 6 ``ImportWarning: can't resolve package
  from __spec__ or __package__`` on the bare-script load.
- The fix: a package-identity bootstrap at the top of server.py detects the
  bare-script case (``__package__ in (None, "")``), puts ``claude_mcp_servers``
  on sys.path, imports ``weaviate_mcp``, sets ``__package__ = "weaviate_mcp"``,
  and reconciles ``sys.modules["weaviate_mcp.server"] = sys.modules["__main__"]``
  so there is ONE canonical server object. The dual-import fallbacks were then
  REMOVED (a broken relative import now = broken install → loud-fail).

These tests run server.py EXACTLY as the launcher does (a real subprocess /
faithful ``__main__`` exec) so they exercise the launch path the 6000+ package-
import tests never touch.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
PKG_PARENT = REPO_ROOT / "claude_mcp_servers"


def _run_driver(driver: str, timeout: int = 90) -> subprocess.CompletedProcess:
    """Run a driver script in a clean subprocess with -W all::ImportWarning so
    any ImportWarning shows up on stderr."""
    return subprocess.run(
        [sys.executable, "-W", "all::ImportWarning", "-c", driver],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(SERVER_PY.parent),
    )


def test_bare_script_load_emits_no_importwarning_and_no_import_error():
    """Run server.py exactly as the launcher does (``python server.py``) and
    assert the import phase is CLEAN: no ImportWarning, no relative-import
    ImportError. Before the fix this printed 6 ImportWarnings; the query-time
    relative import failed later.

    server.py logs "Starting Claude Orchestrator Weaviate MCP Server" only AFTER
    every import has succeeded, then blocks on the stdio serve loop — so seeing
    that line (via timeout) proves the import phase completed."""
    proc = subprocess.Popen(
        [sys.executable, "-W", "all::ImportWarning", str(SERVER_PY)],
        cwd=str(SERVER_PY.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    try:
        out, _ = proc.communicate(timeout=45)
        combined = out or ""
    except subprocess.TimeoutExpired:
        # Blocked on the serve loop = imports succeeded.
        proc.kill()
        out, _ = proc.communicate()
        combined = out or ""

    assert "ImportWarning" not in combined, (
        "server.py emitted an ImportWarning on the bare-script launch — the "
        "package-identity bootstrap must be established BEFORE any relative "
        f"import so __package__/__spec__ resolve.\noutput:\n{combined[-3000:]}"
    )
    assert "attempted relative import" not in combined, (
        "server.py's relative imports failed on the bare-script launch — the "
        f"package-identity bootstrap regressed.\noutput:\n{combined[-3000:]}"
    )
    assert "can't resolve package from __spec__ or __package__" not in combined, (
        f"stale __package__ warning on bare-script load.\noutput:\n{combined[-3000:]}"
    )
    assert "Starting Claude Orchestrator Weaviate MCP Server" in combined, (
        "server.py did NOT reach its post-import 'Starting …' log line when run "
        f"as a bare script — the import phase failed.\noutput:\n{combined[-3000:]}"
    )


def test_bare_script_makes_server_the_one_canonical_object():
    """After a faithful bare-script load (``__main__`` IS the running server),
    ``sys.modules['weaviate_mcp.server']`` must be that SAME object — one
    canonical server, not a second uninitialised copy. A second object would
    desync the re-exported functions (config read twice, test patches missed)."""
    driver = textwrap.dedent(
        f"""
        import sys, asyncio
        SERVER = {str(SERVER_PY)!r}
        # Neuter the blocking serve loop.
        def _noop(coro, *a, **k):
            try: coro.close()
            except Exception: pass
        asyncio.run = _noop
        # Exec server.py source into THIS __main__ (faithful bare-script: __name__
        # == '__main__', sys.modules['__main__'] IS us — exactly `python server.py`).
        main = sys.modules["__main__"]
        main.__file__ = SERVER
        code = compile(open(SERVER).read(), SERVER, "exec")
        exec(code, main.__dict__)
        assert sys.modules.get("weaviate_mcp.server") is main, (
            "weaviate_mcp.server must be reconciled to the running __main__ object"
        )
        # embeddings was imported under its PACKAGE name (not top-level).
        emb = sys.modules["weaviate_mcp.embeddings"]
        assert emb.__name__ == "weaviate_mcp.embeddings", emb.__name__
        assert emb.__package__ == "weaviate_mcp", repr(emb.__package__)
        # `from weaviate_mcp import server` must reach the SAME running object.
        from weaviate_mcp import server as s2
        assert s2 is main, "from-import resolved a SECOND server object"
        # rl_enrichment's lazy proxy must also resolve the running server.
        rl = sys.modules["weaviate_mcp.rl_enrichment"]
        assert rl.server._live() is main, "rl_enrichment proxy missed __main__"
        print("CANONICAL_OK")
        """
    )
    r = _run_driver(driver)
    assert r.returncode == 0 and "CANONICAL_OK" in r.stdout, (
        f"canonical-object property failed.\nrc={r.returncode}\n"
        f"stdout:\n{r.stdout[-2000:]}\nstderr:\n{r.stderr[-2000:]}"
    )
    # Belt-and-braces: no ImportWarning leaked on the faithful exec either.
    assert "ImportWarning" not in r.stderr, (
        f"ImportWarning on faithful bare-script exec:\n{r.stderr[-2000:]}"
    )


def test_bare_script_embeddings_query_time_relative_import_resolves():
    """THE FATAL QUERY-TIME PATH: after bare-script load, an ``embeddings.py``
    function whose body does ``from . import server`` (e.g.
    ``_get_embedding_service``) must resolve — before the fix this raised
    ``ImportError: attempted relative import with no known parent package``
    because embeddings had been imported as a top-level module with no parent."""
    driver = textwrap.dedent(
        f"""
        import sys, asyncio
        SERVER = {str(SERVER_PY)!r}
        def _noop(coro, *a, **k):
            try: coro.close()
            except Exception: pass
        asyncio.run = _noop
        main = sys.modules["__main__"]
        main.__file__ = SERVER
        exec(compile(open(SERVER).read(), SERVER, "exec"), main.__dict__)
        emb = sys.modules["weaviate_mcp.embeddings"]
        # This calls `from . import server` inside the function body. It must NOT
        # raise "attempted relative import with no known parent package". The
        # EmbeddingService may or may not construct (no live Ollama in CI) — we
        # only assert the RELATIVE IMPORT resolves, i.e. no ImportError of that
        # shape escapes.
        try:
            emb._get_embedding_service()
        except ImportError as e:
            raise AssertionError("query-time relative import failed: %s" % e)
        except Exception:
            # Any NON-ImportError (e.g. NoEmbeddingBackendError, network) is fine
            # here — the relative import already resolved to reach that point.
            pass
        # Also via the server re-export surface (what real callers hit):
        try:
            main._get_embedding_service()
        except ImportError as e:
            raise AssertionError("server re-export query-time import failed: %s" % e)
        except Exception:
            pass
        print("QUERY_IMPORT_OK")
        """
    )
    r = _run_driver(driver)
    assert r.returncode == 0 and "QUERY_IMPORT_OK" in r.stdout, (
        f"query-time relative import path failed.\nrc={r.returncode}\n"
        f"stdout:\n{r.stdout[-2000:]}\nstderr:\n{r.stderr[-2000:]}"
    )


def test_package_import_path_is_unaffected_and_bootstrap_is_noop():
    """When server.py is imported AS A PACKAGE (both ``weaviate_mcp.server`` and
    the repo-root ``claude_mcp_servers.weaviate_mcp.server`` keys), the bootstrap
    must be a pure no-op: __package__ stays correct, no ImportWarning, and the
    two keys remain DISTINCT objects (the dual-import-path the test suite uses)."""
    driver = textwrap.dedent(
        f"""
        import sys, importlib
        sys.path.insert(0, {str(REPO_ROOT)!r})
        sys.path.insert(0, {str(PKG_PARENT)!r})
        s1 = importlib.import_module("weaviate_mcp.server")
        assert s1.__package__ == "weaviate_mcp", repr(s1.__package__)
        e1 = importlib.import_module("weaviate_mcp.embeddings")
        assert e1.__package__ == "weaviate_mcp", repr(e1.__package__)
        # query-time relative import works in package mode too
        try:
            e1._get_embedding_service()
        except ImportError as ex:
            raise AssertionError("pkg-mode relative import failed: %s" % ex)
        except Exception:
            pass
        s2 = importlib.import_module("claude_mcp_servers.weaviate_mcp.server")
        assert s2.__package__ == "claude_mcp_servers.weaviate_mcp", repr(s2.__package__)
        assert s1 is not s2, "the two package-key imports must be distinct objects"
        print("PKG_PATH_OK")
        """
    )
    r = _run_driver(driver)
    assert r.returncode == 0 and "PKG_PATH_OK" in r.stdout, (
        f"package import path regressed.\nrc={r.returncode}\n"
        f"stdout:\n{r.stdout[-2000:]}\nstderr:\n{r.stderr[-2000:]}"
    )
    assert "ImportWarning" not in r.stderr, (
        f"ImportWarning on package import (bootstrap not a no-op):\n{r.stderr[-2000:]}"
    )


def test_no_dual_import_fallback_remains_for_shipped_submodules():
    """Ruling: REQUIRED shipped-submodule relative imports must be PLAIN
    ``from .X import Y`` with NO ``try/except ImportError`` fallback (the
    fallback masked broken package identity → silent query-time fail). This
    source-level guard fails if someone reintroduces a
    ``except ImportError: from X`` band-aid for the extracted submodules."""
    src = SERVER_PY.read_text()
    # The pre-fix band-aids imported the submodules as TOP-LEVEL names (no
    # leading dot). Match the NON-relative forms only — the legitimate relative
    # imports (``from . import rl_state as _rl_state`` etc.) are a superset
    # string of some of these, so anchor on the leading-whitespace + top-level
    # ``import``/``from`` keyword to avoid matching the relative form.
    import re as _re
    forbidden_patterns = {
        r"^\s*from chunking import Chunker": "top-level `from chunking`",
        r"^\s*from code_ranking import \(": "top-level `from code_ranking`",
        r"^\s*import rl_state as _rl_state": "top-level `import rl_state`",
        r"^\s*import embeddings as _embeddings": "top-level `import embeddings`",
        r"^\s*from embeddings import \(": "top-level `from embeddings`",
        r"^\s*import rl_enrichment as _rl_enrichment": "top-level `import rl_enrichment`",
    }
    hits = [
        label
        for pat, label in forbidden_patterns.items()
        if _re.search(pat, src, _re.MULTILINE)
    ]
    assert not hits, (
        "server.py still contains a top-level-import fallback for an extracted "
        "shipped submodule — the package-identity bootstrap makes the plain "
        f"relative import work for both entry points, so remove these: {hits}"
    )

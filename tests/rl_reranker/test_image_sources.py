# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The RL reranker image carries exactly the code the package imports — with
ONE copy of every file.

Until v0.2.106 the reranker lived in its own repository and carried VENDORED
duplicates of two orchestrator files (``rl_client/rl_logger.py`` and
``vco_lib/rl_training_targets.py``), kept in step by a sync script and a drift
test that SKIPPED whenever the module checkout was absent — the logger copy
fell a full schema version behind that way. Now the package imports the
originals (``rl_client.rl_logger``, ``vco_lib.rl_training_targets``) and the
Dockerfile, built from the checkout root, COPYs them from where they live.

These tests pin the replacement contract statically (no image build — that
needs the network for the PyTorch wheel):

* every runtime module of the package, and every module it imports from
  ``rl_client`` / ``vco_lib``, is COPYed by the Dockerfile to the path its
  import name resolves to inside ``/app``;
* the root ``.dockerignore`` whitelist lets each of those files into the
  build context;
* no vendored duplicate exists any more.
"""
from __future__ import annotations

import ast
import fnmatch
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "claude_mcp_servers" / "rl_reranker"
DOCKERFILE = PKG / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"

# Top-level packages the reranker may import from the rest of the repo, and
# where their sources live in the checkout.
_EXTERNAL_ROOTS = {
    "rl_client": REPO / "claude_mcp_servers" / "rl_client",
    "vco_lib": REPO / "vco_lib",
}


def _runtime_modules() -> list[Path]:
    """The package's runtime modules (the maintainer ``scripts/`` stay out of
    the image)."""
    return sorted(p for p in PKG.glob("*.py"))


def _external_imports() -> set[str]:
    """Dotted names of every ``rl_client.*`` / ``vco_lib.*`` module imported
    anywhere in the runtime modules (module scope or function scope)."""
    names: set[str] = set()
    for path in _runtime_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module.split(".")[0] in _EXTERNAL_ROOTS:
                    names.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in _EXTERNAL_ROOTS:
                        names.add(alias.name)
    return names


def _copies() -> dict[str, str]:
    """``{context-relative source: image destination}`` for every plain COPY."""
    out: dict[str, str] = {}
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^COPY\s+(?:--\S+\s+)*(\S+)\s+(\S+)\s*$", line)
        if m and "--from=" not in line:
            out[m.group(1)] = m.group(2)
    return out


def _dockerignore_admits(rel: str) -> bool:
    """Evaluate the root ``.dockerignore`` the way docker/podman do: patterns
    in order, last match wins, ``!`` re-includes; a directory pattern covers
    everything beneath it."""
    included = True
    for raw in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = line[1:] if negate else line
        pattern = pattern.rstrip("/")
        hit = (
            fnmatch.fnmatch(rel, pattern)
            or rel.startswith(pattern + "/")
            or fnmatch.fnmatch(rel, pattern + "/*")
            or ("**" in pattern and fnmatch.fnmatch(rel, pattern.replace("**/", "*")))
        )
        if hit:
            included = negate
    return included


def test_the_external_imports_are_the_two_shared_modules():
    """A new cross-package import must be added to the image on purpose."""
    assert _external_imports() == {
        "rl_client.rl_logger",
        "rl_client.rerank_wire",
        "vco_lib.rl_training_targets",
        "vco_lib.atomic",
        "vco_lib.log_setup",
        "vco_lib.rl_weights_ownership",
    }


def test_the_ownership_table_travels_with_its_reader():
    """``vco_lib/rl_weights_ownership.py`` reads the table beside it."""
    rel = "vco_lib/rl_weights_ownership.toml"
    assert _copies().get(rel) == "/app/vco_lib/rl_weights_ownership.toml"
    assert _dockerignore_admits(rel)


def test_every_imported_external_module_is_copied_to_its_import_path():
    copies = _copies()
    for dotted in sorted(_external_imports()):
        root, _, rest = dotted.partition(".")
        src_file = _EXTERNAL_ROOTS[root] / (rest.replace(".", "/") + ".py")
        assert src_file.is_file(), src_file
        rel = src_file.relative_to(REPO).as_posix()
        assert copies.get(rel) == f"/app/{root}/{rest.replace('.', '/')}.py", (rel, copies)
        assert _dockerignore_admits(rel), f".dockerignore keeps {rel} out of the context"


def test_every_runtime_module_of_the_package_is_copied():
    copies = _copies()
    for path in _runtime_modules():
        rel = path.relative_to(REPO).as_posix()
        assert copies.get(rel) == f"/app/rl_reranker/{path.name}", (rel, copies)
        assert _dockerignore_admits(rel), rel


def test_the_build_context_admits_what_the_image_needs_and_nothing_else():
    for rel in (
        "claude_mcp_servers/rl_reranker/requirements.txt",
        "claude_mcp_servers/rl_reranker/vct-module.json",
        "claude_mcp_servers/rl_reranker/db/0001_rl_state.sql",
        "claude_mcp_servers/rl_reranker/weights/manifest.json",
    ):
        assert _dockerignore_admits(rel), rel
    for rel in (
        "launcher/package.json",
        "install.py",
        "vco_lib/paths.py",
        "claude_mcp_servers/rl_client/client.py",
        "claude_mcp_servers/rl_reranker/scripts/train_and_eval.py",
        "claude_mcp_servers/rl_reranker/training/export.py",
        "claude_mcp_servers/weaviate_mcp/server.py",
    ):
        assert not _dockerignore_admits(rel), rel


def test_no_vendored_duplicate_remains():
    for stale in ("rl_logger.py", "_training_targets.py"):
        assert not (PKG / stale).exists(), f"vendored duplicate {stale} is back"
    assert not (REPO / "scripts" / "sync-vendored-files.sh").exists()

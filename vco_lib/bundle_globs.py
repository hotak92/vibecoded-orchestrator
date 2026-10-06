# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Single source of truth for which template files ship into `.claude/`
(v0.2.54 Track G, G-4).

History: by v0.2.54 there were THREE divergent answers to "which hook
flavours does an install ship?":

1. ``install.py::_hook_glob_for_os`` — native flavour only (``*.sh`` on
   POSIX, ``*.ps1`` on Windows; the original audit-F1 policy).
2. ``vco_lib/project_init.py::_hook_globs_for_os`` — BOTH flavours on
   every OS (the v0.2.14 Concern-#2 fix: dual-boot / WSL-crossover /
   network-mounted projects invoke hooks from BOTH shells, so the
   missing flavour lingered stale or simply errored).
3. ``install.py::_materialize_orchestrator_self_claude_dir`` — everything
   in ``templates/hooks/`` via ``iterdir()`` (both flavours by accident).

The both-flavours policy (#2) is the correct one — hooks are small text
files and shipping both makes the same ``.claude/`` tree work regardless
of which shell invokes it — so all call-sites now route through
:func:`hook_globs`.

The per-project script-copy pattern list had the same disease in two
places (``install.py`` Step 9b and ``project_init`` bundle ops) and the
copies HAD already drifted: install.py gained the extension-less
``detect-workflow-needs`` / ``generate-workflow`` bash wrappers in
v0.2.54 but project_init didn't, so per-project bundles silently missed
them. :func:`script_patterns` is now the only list.
"""

from __future__ import annotations


def hook_globs() -> tuple[str, ...]:
    """All hook flavours to ship — both ``.sh`` and ``.ps1``, on every OS.

    Shipping the host-native flavour only (the pre-v0.2.14 policy) broke
    cross-OS workflows where one project folder is opened from both POSIX
    and Windows shells (dual-boot, WSL crossover, network mounts): stale
    orphan hooks of the OTHER flavour lingered and could be invoked by
    the unexpected shell. Both flavours are a few KB each; the runtime
    settings.json picks which extension matches its shell.
    """
    return ("*.sh", "*.ps1")


def hook_lib_data_globs() -> tuple[str, ...]:
    """Data-file globs shipped from ``templates/hooks/_lib/`` alongside the
    ``.sh``/``.ps1`` helper flavours.

    v0.2.101: the lean-ctx allow-list inversion made
    ``templates/hooks/_lib/lean-ctx-allowlist.txt`` a load-bearing SHARED
    config that both ``lean-ctx-rewrite.sh`` and ``.ps1`` PARSE at runtime
    (A>B>C tier B — one committed rule table, not per-language mirrors). If
    that ``.txt`` doesn't ship into ``<project>/.claude/hooks/_lib/``, both
    hooks silently no-op (allow-list absent → every command runs raw), so
    compression would quietly never activate on any install. The ``_lib``
    copy loop in ``project_init._enumerate_bundle_files`` globs
    ``hook_globs() + hook_lib_data_globs()``; the top-level ``hooks/`` loop
    is unaffected (it ships registered hooks, which are always ``.sh``/``.ps1``).
    """
    return ("*.txt",)


def script_patterns() -> tuple[str, ...]:
    """Glob patterns for `templates/scripts/` files shipped to `.claude/scripts/`.

    Python modules, shell wrappers (extension-less or ``.sh``), PowerShell
    wrappers, and the named extension-less CLI wrappers. Callers must
    de-duplicate matches across patterns (several overlap by design).

    v0.2.100 (WP-15, L5-F08): the patterns apply RECURSIVELY — a match in a
    subdirectory of ``templates/scripts/`` ships to the same subpath under
    ``.claude/scripts/`` (``__pycache__`` excluded), for the orchestrator root
    and every project alike (one engine, ``project_init._enumerate_bundle_files``).
    """
    return (
        "*.py", "*.sh", "*.ps1", "kg-*", "code-graph-*",
        # v0.2.54: extension-less bash wrappers for the workflow tooling.
        "detect-workflow-needs", "generate-workflow",
    )

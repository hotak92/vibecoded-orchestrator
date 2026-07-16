# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""MCP-registration primitives for install.py (vco_lib.install_mcp — v0.2.73).

IN-1 (v0.2.73) extraction: the pure, side-effect-free MCP-registration
layer that install.py used to host inline. This module owns:

* the env-key allowlist + secret-shaped-key denylist that gate what may be
  written into ``~/.claude.json mcpServers.*.env`` (mirrors
  ``launcher/src-tauri/src/mcp_registration.rs``);
* the default-MCP entry builder + the pure-Python JSON writer (the Tier-4
  fallback used when the launcher binary is unavailable);
* the stale-entry + deprecated-entry scan / detect helpers + the interactive
  consent driver.

Everything here is a pure function of its inputs (plus the module-level
constants) — no ``PROJECT_ROOT`` reads, no install-time logging, no
launcher-binary orchestration. The orchestration layer (``_register_mcps``,
``_rewrite_stale_mcp_entries``, ``_remove_deprecated_mcp_entries``) stays in
install.py because it depends on install.py's launcher-binary resolution +
the pervasive ``_log_install_event`` / ``_user_home_for_install`` helpers.

This module does NOT import install.py — install.py imports FROM it. That
keeps the dependency edge one-directional (install.py → vco_lib.install_mcp)
and avoids the import cycle a back-import would create (install.py runs
top-level configuration code at import).
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from vco_lib import mcp_scan_rules
from vco_lib.deferral_report import DeferralEntry, DeferralReport


# ---------------------------------------------------------------------------
# PR-23 (v0.2.12, 2026-05-16): default-MCP registration into ~/.claude.json
#
# Audit reference: .claude/context/mcp-install-pipeline-audit-2026-05-16.md
#
# Pre-PR-23 install.py performed zero MCP registration. Result: fresh
# v0.2.11 installs left ~/.claude.json with no bundled MCP servers wired
# at all → Claude Code couldn't see the orchestrator. The Rust
# `mcp_registration::register_mcp` helper existed and was tested, but no
# install code path invoked it.
#
# Architecture (FINALIZED 2026-05-16):
#   - The launcher binary is the single writer of both ~/.claude.json
#     AND the project_mcp_servers DB table.
#   - install.py shells out to a CLI subcommand on the launcher:
#       `<binary> --register-default-mcps <install_root>`
#   - 4-tier launcher-binary resolution:
#       1. Bundled binary at `launcher/dist/<os>-<arch>/vct-launcher`.
#       2. Download from GitHub Releases matching the current version.
#       3. Rebuild via `cargo tauri build` (slow LAST resort).
#       4. Pure-Python JSON merge (always succeeds at writing JSON;
#          launcher DB stays empty until the user opens the GUI and
#          project_state_populate picks up the JSON entries).
#
# Security boundary: `~/.claude.json` is readable by every process running
# as the user. Therefore secret-shaped env keys (TOKEN, SECRET, PAT,
# PASSWORD, AUTH, *_KEY) are silently dropped from any written entry, AND
# per-project keys (KG_COLLECTION, PROJECT_NAME, etc.) are NEVER written —
# those live in each project's .claude/settings.json env (launcher-managed)
# instead. Empirical verification 2026-05-16: a long-lived project's MCP
# subprocess picked up its KG_COLLECTION from .claude/settings.json env,
# confirming the per-project env channel is sufficient.
# ---------------------------------------------------------------------------

# Env-key allowlist for ~/.claude.json mcpServers.*.env.
#
# v0.2.83 WP-B4: this list is now SOURCED from the cross-language rule table
# ``vco_lib/mcp_scan_rules.toml`` ([env].allowed_global_keys) — the SAME file
# ``launcher/src-tauri/src/mcp_registration.rs`` embeds via ``include_str!``.
# One committed table, both languages parse it (A>B>C tier B). It is no longer
# a hand-maintained literal that must "stay in sync" with the Rust copy — a
# parity test (tests/test_mcp_scan_rules_parity.py) locks both sides to the
# table. See the .toml header for the full rationale.
#
# CRITICAL CONTRACT (see Issue H.1 from mcp-instability audit 2026-05-16):
# Anthropic semantics say "project scope overrides user scope" for env
# vars, but Claude Code applies ~/.claude.json mcpServers.*.env keys LAST
# to MCP subprocesses — so they WIN against .claude/settings.json env.
# This is the wrong direction for any per-project-varying value. The
# allowlist is therefore restricted to keys that are TRULY machine-invariant
# (service URLs/ports, PYTHONPATH, ACTIVE_EMBEDDING). Removed in PR-43:
# RL_SERVER_URL, EMBEDDING_MODEL (per-project overrides). Edit the .toml to
# change this set, never here.
_ALLOWED_GLOBAL_ENV_KEYS = mcp_scan_rules.allowed_global_env_keys()

# Credential-shaped needle segments that MUST be dropped (secrets).
# v0.2.83 WP-B4: sourced from ``vco_lib/mcp_scan_rules.toml``
# ([env].secret_shaped_needles) — the same table the Rust ``needles`` array
# reads. The segment-split + ``KEY``/``*_KEY`` suffix PREDICATE below stays
# language-local (it is control-flow, not data); only its needle DATA is
# shared. tests/test_secret_shaped_needles_parity.py anchors every impl on
# the table.
_SECRET_SHAPED_SUBSTRINGS = mcp_scan_rules.secret_shaped_needles()

# The MCP ids whose ~/.claude.json entries are composed by the entry builder
# below (the registrar-owned / rewritable set). v0.2.83 WP-B4: sourced from
# ``vco_lib/mcp_scan_rules.toml`` ([entries].default_names) — the SAME set
# ``mcp_registration.rs::DEFAULT_MCP_ENTRY_NAMES`` reads. Callers that used to
# hand-type ``{"weaviate-kg", "search"}`` (a drifted subset — see the
# install.py rewrite-partition) MUST use this instead. ``_build_python_mcp_
# entries`` asserts its emit order equals this list so the two cannot drift.
_DEFAULT_MCP_ENTRY_NAMES: tuple[str, ...] = mcp_scan_rules.default_mcp_entry_names()


#: FN-5b (2026-07-14): weaviate_mcp submodules the code-graph analyzer + the
#: weaviate MCP import directly. After the editable install, these MUST all be
#: importable in the resolved venv; a silent import failure here surfaces later
#: as an opaque runtime crash, so install.py runs the verify script below and
#: LOUD-FAILS the install with the missing-module list on error. There is NO
#: sys.path shim that makes these importable from a bare checkout — a failed
#: editable install is a BROKEN install, not a degraded one.
WEAVIATE_MCP_REQUIRED_SUBMODULES: tuple[str, ...] = (
    "weaviate_mcp.chunking",
    "weaviate_mcp.code_ranking",
    "weaviate_mcp.rl_state",
    "weaviate_mcp.embeddings",
    "weaviate_mcp.rl_enrichment",
)


def build_weaviate_mcp_import_verify_script() -> str:
    """Return a ``python -c`` script that imports each required weaviate_mcp
    submodule and exits non-zero (with a stderr list) if any fail.

    Pure builder — install.py runs the returned string in the resolved venv
    via ``_run_logged_subprocess(..., on_failure="exit")`` so a broken
    editable install aborts loudly instead of degrading silently (FN-5b).
    """
    mods = ", ".join(repr(m) for m in WEAVIATE_MCP_REQUIRED_SUBMODULES)
    return (
        "import importlib, sys\n"
        f"mods = [{mods}]\n"
        "missing = []\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception as exc:\n"
        "        missing.append(f'{m}: {type(exc).__name__}: {exc}')\n"
        "if missing:\n"
        "    sys.stderr.write('unimportable weaviate_mcp submodules:\\n')\n"
        "    for line in missing:\n"
        "        sys.stderr.write('  - ' + line + '\\n')\n"
        "    sys.exit(1)\n"
    )


def _is_secret_shaped_env_key(key: str) -> bool:
    """True iff `key` looks like a credential. See module docstring.

    Matches secret substrings as TOKENS within ``[_\\-]``-delimited
    env-key parts. Avoids false positives like ``PYTHONPATH`` matching
    ``PAT`` or ``COMPASS`` matching ``PASS``. The keys we care about
    (``GITHUB_TOKEN``, ``DB_PASS``, ``MY_PAT``, ``AUTH_HEADER``, etc.)
    all have the secret token as a distinct segment between underscores
    or at the boundary of the string.
    """
    upper = key.upper()
    # Split on `_` and `-` (the two common env-key segment separators).
    parts = re.split(r"[_\-]+", upper)
    for needle in _SECRET_SHAPED_SUBSTRINGS:
        if needle in parts:
            return True
    # Trailing `_KEY` and exact `KEY` rules (catch STRIPE_KEY etc.).
    if upper == "KEY" or upper.endswith("_KEY"):
        return True
    return False


def _filter_env_for_global_json(candidate: dict) -> tuple[dict, list[str]]:
    """Return (safe_env, dropped_keys). Mirrors Rust filter_env_for_global_json."""
    safe = {}
    dropped = []
    for k, v in candidate.items():
        if _is_secret_shaped_env_key(k):
            dropped.append(k)
            continue
        if k not in _ALLOWED_GLOBAL_ENV_KEYS:
            dropped.append(k)
            continue
        safe[k] = v
    return safe, dropped


def _build_python_mcp_entries(
    install_root: Path,
    venv_python: Path,
    weaviate_port: int,
    ollama_port: int,
    grpc_port: int,
    code_embed_port: int,
) -> list[tuple[str, dict, list[str]]]:
    """Pure-Python mirror of mcp_registration.rs::build_default_mcp_entries.

    Returns a list of (name, entry_dict, dropped_keys). Each entry's `env`
    field has already been filtered through the allowlist + secret-shape
    denylist. The Rust path is the authoritative writer; this exists for
    Tier 4 (pure-Python fallback).
    """
    weaviate_url = f"http://localhost:{weaviate_port}"
    ollama_url = f"http://localhost:{ollama_port}"
    code_embed_url = f"http://localhost:{code_embed_port}"
    mcp_root = install_root / "claude_mcp_servers"
    pythonpath = str(mcp_root)
    venv_python_str = str(venv_python)

    # weaviate-kg
    weaviate_server = mcp_root / "weaviate_mcp" / "server.py"
    # PR-43 (post-PR-23): EMBEDDING_MODEL + RL_SERVER_URL are intentionally
    # omitted here. They were originally written as "global defaults that
    # per-project may override" but Claude Code's actual env precedence
    # makes ~/.claude.json mcpServers.*.env WIN against .claude/settings.json
    # env — so the override goes the wrong direction. The launcher's
    # write_project_env_files puts these in .claude/settings.json env where
    # they reach MCP subprocesses correctly. Don't shadow them here.
    weaviate_env_raw = {
        "WEAVIATE_URL": weaviate_url,
        "OLLAMA_URL": ollama_url,
        "GRPC_PORT": str(grpc_port),
        "PYTHONPATH": pythonpath,
        "ACTIVE_EMBEDDING": "qwen3",
        "CODE_EMBED_SERVICE_URL": code_embed_url,
    }
    weaviate_env, weaviate_dropped = _filter_env_for_global_json(weaviate_env_raw)
    weaviate_entry = {
        "type": "stdio",
        "command": venv_python_str,
        "args": [str(weaviate_server)],
        "env": weaviate_env,
    }

    # search (v0.2.11+: needs no secrets; uses wrapper.sh on Unix)
    search_server = mcp_root / "search_mcp" / "server.py"
    search_wrapper = mcp_root / "search_mcp" / "wrapper.sh"
    if platform.system().lower().startswith("win"):
        search_cmd, search_args = venv_python_str, [str(search_server)]
    else:
        search_cmd, search_args = str(search_wrapper), []
    search_env_raw = {"PYTHONPATH": pythonpath}
    search_env, search_dropped = _filter_env_for_global_json(search_env_raw)
    search_entry = {
        "type": "stdio",
        "command": search_cmd,
        "args": search_args,
        "env": search_env,
    }

    # playwright (F-1, v0.2.73)
    # Browser automation via Microsoft's `@playwright/mcp`. The entry
    # mirrors EXACTLY how the MCP is launched everywhere else in the
    # stack: bare `npx -y @playwright/mcp@latest` — the same invocation
    # the GUI catalog ships (vct-launcher-core/src/types.rs::
    # default_mcp_servers) and `_install_playwright_browsers` pre-caches.
    # `npx` resolves from PATH cross-OS; no venv-python and no env vars.
    # Default-enabled per project. MUST stay in sync with the Rust builder
    # mcp_registration.rs::build_default_mcp_entries.
    #
    # Pre-v0.2.73 this entry was MISSING from both builders (audit finding
    # F-1): the GUI catalog shipped enabled=True so the toggle-ON write
    # never fired on a fresh install, and no install path wrote the entry —
    # the Chromium pre-cache was spent on an MCP that never reached
    # ~/.claude.json.
    playwright_entry = {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
        "env": {},
    }
    playwright_dropped: list[str] = []

    # mermaid (Phase 1.2 — diagrams plan)
    # Wrapper MCP that proxies the pinned `claude-mermaid` npm package.
    # Spawned as `<venv-python> -m claude_mcp_servers.wrappers.mermaid_proxy`
    # — the wrapper itself spawns `npx` as a child once it's resolved the
    # per-project tool allowlist. Mirrors the Rust path's mermaid entry
    # in mcp_registration.rs::build_default_mcp_entries.
    mermaid_env_raw = {"PYTHONPATH": pythonpath}
    mermaid_env, mermaid_dropped = _filter_env_for_global_json(mermaid_env_raw)
    mermaid_entry = {
        "type": "stdio",
        "command": venv_python_str,
        "args": [
            "-m",
            "claude_mcp_servers.wrappers.mermaid_proxy",
        ],
        "env": mermaid_env,
    }

    # excalidraw (Phase 2 — diagrams plan)
    # Wrapper MCP that proxies the in-tree-vendored
    # `excalidraw-mcp-server` (see
    # vco_lib/excalidraw_mcp_fork/VENDORED.md — moved here from
    # claude_mcp_servers/excalidraw_mcp_fork/ in v0.2.34). Spawned as
    # `<venv-python> -m claude_mcp_servers.wrappers.excalidraw_proxy`
    # — the wrapper itself spawns Node on the vendored entry point
    # once it's resolved the per-project tool allowlist. Mirrors the
    # Rust path's excalidraw entry in
    # mcp_registration.rs::build_default_mcp_entries.
    excalidraw_env_raw = {"PYTHONPATH": pythonpath}
    excalidraw_env, excalidraw_dropped = _filter_env_for_global_json(excalidraw_env_raw)
    excalidraw_entry = {
        "type": "stdio",
        "command": venv_python_str,
        "args": [
            "-m",
            "claude_mcp_servers.wrappers.excalidraw_proxy",
        ],
        "env": excalidraw_env,
    }

    entries = [
        ("weaviate-kg", weaviate_entry, weaviate_dropped),
        ("search", search_entry, search_dropped),
        ("playwright", playwright_entry, playwright_dropped),
        ("mermaid", mermaid_entry, mermaid_dropped),
        ("excalidraw", excalidraw_entry, excalidraw_dropped),
    ]
    # WP-B4 drift guard: the builder's emit ORDER must equal the table's
    # [entries].default_names. The entry SHAPES (command/args/env, OS
    # branching) stay language-local build logic — only the name list +
    # ordering is sourced from the table. A new bundled MCP added to the
    # builder but not the table (or vice-versa) trips this immediately.
    emitted_names = tuple(name for name, _, _ in entries)
    if emitted_names != _DEFAULT_MCP_ENTRY_NAMES:
        raise RuntimeError(
            "MCP entry builder drifted from mcp_scan_rules.toml "
            f"[entries].default_names: builder emits {emitted_names}, table "
            f"says {_DEFAULT_MCP_ENTRY_NAMES}. Update both in one commit."
        )
    return entries


def _python_fallback_write_mcp_entries(
    claude_json_path: Path,
    entries: list[tuple[str, dict, list[str]]],
) -> tuple[int, list[str]]:
    """Pure-Python JSON merge mirroring mcp_registration.rs discipline.

    Same contract as the Rust register_mcp:
      - advisory file lock at <path>.lock (create_new)
      - read existing JSON (or empty {})
      - mutate ONLY mcpServers.<name>
      - write to .tmp + atomic rename
      - backup existing file to <path>.bak before overwrite

    The launcher.db is NOT touched here — `project_state_populate` will
    pick up the JSON entries when the user opens the launcher GUI.

    Returns (success_count, error_messages). Soft-fail per entry.
    """
    # Ensure parent dir exists before lock + write. The fake_home pattern
    # in tests creates a path like tmp/fake_home/.claude.json where
    # `fake_home` doesn't exist yet; without this mkdir, os.open() on the
    # .lock file raises FileNotFoundError and the write returns (0, ...).
    try:
        claude_json_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (0, [f"create parent {claude_json_path.parent}: {exc}"])
    # Acquire lock.
    lock_path = claude_json_path.with_suffix(claude_json_path.suffix + ".lock")
    locked = False
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            locked = True
            break
        except FileExistsError:
            time.sleep(0.05)
        except OSError:
            break
    if not locked:
        return (0, [f"could not acquire lock {lock_path}"])

    errors: list[str] = []
    success = 0
    try:
        # Read existing (or empty).
        try:
            if claude_json_path.is_file():
                raw = claude_json_path.read_text(encoding="utf-8")
                data = json.loads(raw) if raw.strip() else {}
            else:
                data = {}
        except (OSError, json.JSONDecodeError) as exc:
            return (0, [f"read {claude_json_path}: {exc}"])
        if not isinstance(data, dict):
            return (0, [f"{claude_json_path} root is not a JSON object"])
        if "mcpServers" not in data or not isinstance(data.get("mcpServers"), dict):
            data["mcpServers"] = {}
        # Merge entries.
        for name, entry, _dropped in entries:
            data["mcpServers"][name] = entry
            success += 1
        # Backup + atomic write.
        # v0.2.53 DEDUP-5 / CORRECT-1: route through
        # vco_lib.env_template._atomic_write_text which uses
        # tempfile.mkstemp + os.replace AND unlinks the tempfile on any
        # exception. The inline pre-v0.2.53 recipe (tmp.write_text +
        # os.replace) left behind <path>.tmp on partial-write failures
        # (disk-full, write-mid-flush, sigterm). The new helper makes
        # cleanup atomic.
        try:
            if claude_json_path.is_file():
                bak = claude_json_path.with_suffix(claude_json_path.suffix + ".bak")
                shutil.copy2(claude_json_path, bak)
            from vco_lib.env_template import _atomic_write_text
            _atomic_write_text(claude_json_path, json.dumps(data, indent=2))
        except OSError as exc:
            return (0, [f"write {claude_json_path}: {exc}"])
        return (success, errors)
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _scan_stale_mcp_entries(
    install_root: Path,
    claude_json: Path,
) -> list[tuple[str, str, dict]]:
    """Return a list of ``(mcp_name, stale_path, entry_dict)`` for every
    ``~/.claude.json mcpServers`` entry whose ``command`` or ``args[0]``
    points at a vco-install-shaped path OUTSIDE the current install_root.

    Pure function (no deferral side effects, no writes). Used by both
    :func:`_detect_stale_mcp_entries` (reports only) and
    :func:`_rewrite_stale_mcp_entries` (PR-33 consent-prompted rewrite).
    The triple includes the full entry dict so the rewrite path can
    inspect existing ``env`` keys for the secret-leak warning.

    Cross-OS path detection: absolute paths only (``/``, ``C:\\``,
    ``c:\\``, ``\\\\``-prefixed UNC). Anchored on the ``claude_mcp_servers``
    or ``.venv`` directory tokens so user-added MCPs at ``/usr/bin/foo``
    are NOT misclassified as orchestrator-stale.
    """
    if not claude_json.is_file():
        return []
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    mcp_servers = data.get("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        return []

    install_root_str = str(install_root.resolve())
    stale: list[tuple[str, str, dict]] = []
    for name, entry in mcp_servers.items():
        if not isinstance(entry, dict):
            continue
        cmd = entry.get("command", "") if isinstance(entry.get("command"), str) else ""
        first_arg = ""
        args = entry.get("args", [])
        if isinstance(args, list) and args and isinstance(args[0], str):
            first_arg = args[0]
        for candidate in (cmd, first_arg):
            if not candidate or not candidate.startswith(("/", "C:\\", "c:\\", "\\\\")):
                continue
            # Anchor: only flag paths that look like vco install layouts
            # (claude_mcp_servers/ or .venv/). Otherwise we'd flag every
            # user-added MCP that lives in /usr/bin/foo.
            if "claude_mcp_servers" not in candidate and ".venv" not in candidate:
                continue
            if not candidate.startswith(install_root_str):
                stale.append((name, candidate, entry))
                break
    return stale


def _detect_stale_mcp_entries(
    install_root: Path,
    claude_json: Path,
    deferral_report: "DeferralReport",
) -> None:
    """On --update, emit a deferral when ~/.claude.json mcpServers entries
    point at directories outside the current install_root.

    Detection-only path (no rewrite). The companion
    :func:`_rewrite_stale_mcp_entries` (PR-33) consumes the same
    :func:`_scan_stale_mcp_entries` data and performs consent-prompted
    rewrites when ``--rewrite-stale-mcps`` is passed.
    """
    stale = _scan_stale_mcp_entries(install_root, claude_json)
    if not stale:
        return

    install_root_str = str(install_root.resolve())
    detected_lines = [f"  - `{name}`: {path}" for name, path, _ in stale]
    deferral_report.add_entry(
        DeferralEntry(
            condition_id="stale_mcp_entry",
            title="Stale ~/.claude.json MCP entries from a previous install",
            detected=(
                f"~/.claude.json contains MCP entries that point at directories "
                f"outside the current install_root ({install_root_str}):\n\n"
                + "\n".join(detected_lines)
                + "\n\nThese were left behind by a previous orchestrator install "
                "at a different path. Claude Code may spawn duplicate MCP "
                "subprocesses against the same Weaviate container if both "
                "installs are still active."
            ),
            why_deferred=(
                "Auto-rewriting global MCP entries is destructive (user may "
                "have intentional dual-install setups). v0.2.12 detects and "
                "reports; pass `--rewrite-stale-mcps` for the consent-prompted "
                "rewrite path (PR-33)."
            ),
            command_to_apply=(
                "# Re-run with the consent-prompted rewrite flag (PR-33):\n"
                "python install.py --update --rewrite-stale-mcps\n"
                "# Or, for CI / scripted contexts that want to accept all:\n"
                "#   VCT_REWRITE_STALE_MCPS=all python install.py --update --rewrite-stale-mcps"
            ),
            severity="warning",
            kg_node_refs=[
                ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
            ],
        )
    )


# ---------------------------------------------------------------------------
# PR-33 (v0.2.12, 2026-05-16): consent-prompted rewrite of stale MCP entries
# ---------------------------------------------------------------------------
#
# Detection (PR-23, above) is unconditional on --update. Rewrite (PR-33,
# the install.py orchestration block) is OFF by default — it only runs when
# the user passes ``--rewrite-stale-mcps``. Even then, every stale entry is
# prompted individually with y/n/all/skip-all choices. ``--quiet`` cannot
# prompt; it emits a clarifying deferral and writes nothing.
# ``VCT_REWRITE_STALE_MCPS=all`` env override exists for CI / scripted
# contexts that explicitly want auto-acceptance.
#
# The consent driver below is the pure decision layer; the writing /
# backup / re-registration orchestration stays in install.py
# (``_rewrite_stale_mcp_entries``) because it depends on the launcher-binary
# registrar + install-time logging + home-dir resolution.


def _consent_for_stale_entries(
    stale: list[tuple[str, str, dict]],
    install_root: Path,
    quiet: bool,
    env_override: str,
    input_fn=input,
    output_fn=print,
) -> dict[str, bool]:
    """Drive the per-entry consent prompt and return a ``{name: accept}`` map.

    Decision tree (mirrors the PR-33 spec):

    * ``quiet=True`` and ``env_override != "all"`` → return all-False
      (caller emits a clarifying deferral; no prompt possible).
    * ``env_override == "all"`` → return all-True (CI fast-path).
    * Otherwise → walk each entry once, accept ``y`` / ``yes``, default
      reject on empty / ``n`` / ``no``; ``a`` / ``all`` short-circuits
      remaining entries to True; ``s`` / ``skip-all`` short-circuits
      to False.

    Soft-fail: an EOF / KeyboardInterrupt on the prompt is treated as
    skip-all so install does not crash mid-prompt.
    """
    # Fast-path: explicit env override for CI / scripted runs.
    if env_override.lower() in ("all", "yes", "y", "true", "1"):
        return {name: True for name, _, _ in stale}
    # Quiet mode cannot prompt — caller handles the deferral.
    if quiet:
        return {name: False for name, _, _ in stale}

    install_root_str = str(install_root.resolve())
    output_fn("")
    output_fn(
        f"Found {len(stale)} ~/.claude.json mcpServers entr"
        f"{'y' if len(stale) == 1 else 'ies'} "
        "pointing outside this install_root:"
    )
    for name, stale_path, _entry in stale:
        output_fn(f"  - {name}: {stale_path}")
    output_fn("")
    output_fn(
        "These were registered by a different orchestrator install. "
        "Rewriting will point them at the CURRENT install:"
    )
    output_fn(f"  {install_root_str}")
    output_fn(
        "Existing config (env block, args extras) is preserved; only "
        "path components change. Per-entry choices: "
        "[y]es, [n]o (default), [a]ll, [s]kip-all"
    )
    output_fn("")

    choices: dict[str, bool] = {}
    blanket: Optional[bool] = None
    for name, _stale_path, entry in stale:
        if blanket is not None:
            choices[name] = blanket
            output_fn(f"  {name} → {'rewrite' if blanket else 'skip'} (from blanket choice)")
            continue
        # Secret-leak warning: highlight env keys that will be dropped.
        env_block = entry.get("env", {}) if isinstance(entry.get("env"), dict) else {}
        will_drop = [
            k for k in env_block.keys()
            if _is_secret_shaped_env_key(k) or k not in _ALLOWED_GLOBAL_ENV_KEYS
        ]
        if will_drop:
            output_fn(
                f"  WARNING: rewriting `{name}` will drop env keys: {will_drop}. "
                "These are not in the global-JSON allowlist (secrets go to the "
                "OS keychain via the launcher; per-project keys live in "
                ".claude/settings.json env). Lost on rewrite."
            )
        try:
            answer = input_fn(f"  {name} → rewrite? [y/N/a/s]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # Treat any prompt-failure as skip-all (safest default).
            output_fn("  (prompt interrupted — treating as skip-all)")
            for n, _, _ in stale:
                choices.setdefault(n, False)
            return choices
        if answer in ("a", "all"):
            blanket = True
            choices[name] = True
        elif answer in ("s", "skip-all"):
            blanket = False
            choices[name] = False
        elif answer in ("y", "yes"):
            choices[name] = True
        else:
            # Empty / "n" / "no" / unrecognised → skip (safest default).
            choices[name] = False
    return choices


# ---------------------------------------------------------------------------
# PR-34 (v0.2.13, 2026-05-16): deprecated-MCP-entry detection + removal
# ---------------------------------------------------------------------------
#
# When install.py drops an MCP from the default set (e.g. Ollama MCP in
# v0.2.11) it leaves behind an entry in ~/.claude.json for users who
# installed before that release.  The old _check_ollama_mcp_remnants
# function (PR-14b) fires unconditionally on --update and emits an
# informational notice, but:
#
#   a) it does not distinguish "our" entry (command inside install_root)
#      from a user's own custom Ollama MCP at a different path;
#   b) it cannot auto-remove even with consent.
#
# PR-34 replaces that with a structured deprecation registry and a
# consent-prompted removal path.  Three-step design (mirrors PR-33):
#
#   1. _DEPRECATED_DEFAULT_MCPS — registry of MCPs dropped from the
#      default set, with the release version, human-readable reason, and
#      the opt-in manifest path where the feature moved.
#   2. _scan_deprecated_mcp_entries — pure function that reads
#      ~/.claude.json and returns entries whose (a) name is in the
#      registry AND (b) command path is inside the current install_root
#      (i.e. "our" entry, not user-customised).
#   3. _detect_deprecated_mcp_entries — detection-only path called
#      unconditionally from _register_mcps; emits a deferral for each
#      match (no rewrite).
#   4. _remove_deprecated_mcp_entries — consent-prompted removal (stays in
#      install.py). Only runs when --remove-deprecated-mcps is passed.
#      VCT_REMOVE_DEPRECATED_MCPS=all env override for CI.
#
# Composition with PR-33 (--rewrite-stale-mcps):
#   When --rewrite-stale-mcps is passed, deprecated-MCP detection is
#   ALSO run (deprecation is a form of staleness).  The removal itself
#   still requires the explicit --remove-deprecated-mcps flag.


#: Registry of MCPs that used to be in the default install set but were
#: later removed.  Any entry in this dict will be scanned for in
#: ~/.claude.json on every --update run.
#:
#: v0.2.83 WP-B4: sourced from ``vco_lib/mcp_scan_rules.toml`` ([deprecated.*])
#: — one committed table both Rust and Python parse. Add a future deprecation
#: as a ``[deprecated.<name>]`` table there, NOT here. Each value carries
#: ``removed_in`` / ``reason`` / ``opt_in_manifest`` (empty string when a
#: field is absent). Shape is identical to the pre-WP-B4 literal so every
#: consumer (``_scan_deprecated_mcp_entries`` / ``_detect_deprecated_mcp_
#: entries``) is unchanged.
_DEPRECATED_DEFAULT_MCPS: dict[str, dict] = mcp_scan_rules.deprecated_default_mcps()


def _scan_deprecated_mcp_entries(
    install_root: Path,
    claude_json: Path,
) -> list[tuple[str, str, dict, dict]]:
    """Return a list of ``(mcp_name, cmd_path, entry_dict, dep_info)`` for
    every ``~/.claude.json mcpServers`` entry that:

    a) has a name present in :data:`_DEPRECATED_DEFAULT_MCPS`, AND
    b) whose ``command`` or ``args[0]`` path lives INSIDE the current
       install_root (i.e. it was registered by THIS orchestrator install,
       not a user-added entry at an unrelated path).

    Entries that match the name but whose command is NOT inside
    install_root are assumed to be user-customised and are left alone.

    Pure function (no deferral side effects, no writes).

    Returns:
        List of 4-tuples: (name, path_inside_root, entry_dict, dep_info).
        ``dep_info`` is the value from :data:`_DEPRECATED_DEFAULT_MCPS`.
    """
    if not claude_json.is_file():
        return []
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    mcp_servers = data.get("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        return []

    install_root_str = str(install_root.resolve())
    results: list[tuple[str, str, dict, dict]] = []
    for name, dep_info in _DEPRECATED_DEFAULT_MCPS.items():
        entry = mcp_servers.get(name)
        if not isinstance(entry, dict):
            continue
        # Determine the path candidates (command + first arg).
        cmd = entry.get("command", "") if isinstance(entry.get("command"), str) else ""
        first_arg = ""
        args = entry.get("args", [])
        if isinstance(args, list) and args and isinstance(args[0], str):
            first_arg = args[0]
        # Check whether ANY path candidate is inside install_root.
        # If none of the candidates are absolute paths inside install_root,
        # the entry is user-customised → leave it alone.
        matched_path = ""
        for candidate in (cmd, first_arg):
            if not candidate:
                continue
            # Only consider absolute paths (cross-OS).
            if not candidate.startswith(("/", "C:\\", "c:\\", "\\\\")):
                continue
            if candidate.startswith(install_root_str):
                matched_path = candidate
                break
        if not matched_path:
            # Either no absolute path candidates, or the path is outside
            # install_root → user-customised entry; skip.
            continue
        results.append((name, matched_path, entry, dep_info))
    return results


def _detect_deprecated_mcp_entries(
    install_root: Path,
    claude_json: Path,
    deferral_report: "DeferralReport",
) -> None:
    """Detection-only path: emit a deferral for each deprecated-MCP entry
    whose path lives inside the current install_root.

    Called unconditionally from :func:`_register_mcps` (after every
    successful write via Path A or B).  The companion
    :func:`_remove_deprecated_mcp_entries` performs the actual removal
    when ``--remove-deprecated-mcps`` is passed.

    User-customised entries (command outside install_root) are silently
    skipped — they are the user's concern, not ours.
    """
    deprecated = _scan_deprecated_mcp_entries(install_root, claude_json)
    if not deprecated:
        return

    install_root_str = str(install_root.resolve())
    for name, matched_path, _entry, dep_info in deprecated:
        removed_in = dep_info.get("removed_in", "unknown release")
        reason = dep_info.get("reason", "")
        opt_in = dep_info.get("opt_in_manifest", "")
        opt_in_note = (
            f"\nOpt-in: if you still want these tools, install the module "
            f"via the launcher → Modules, or inspect {opt_in}."
        ) if opt_in else ""

        deferral_report.add_entry(
            DeferralEntry(
                condition_id=f"deprecated_mcp_{name}",
                title=(
                    f"Deprecated MCP entry `{name}` still in ~/.claude.json "
                    f"(removed {removed_in})"
                ),
                detected=(
                    f"~/.claude.json contains a `{name}` block under "
                    f"`mcpServers` whose command path ({matched_path}) points "
                    f"inside the current install_root ({install_root_str}). "
                    f"This entry was registered by a previous version of this "
                    f"orchestrator install and is no longer part of the default "
                    f"install set.\n\nReason: {reason}{opt_in_note}"
                ),
                why_deferred=(
                    "Auto-removal of ~/.claude.json entries requires user "
                    "consent. Pass `--remove-deprecated-mcps` (with "
                    "`--update`) for the consent-prompted removal path. "
                    "The existing entry is preserved and functional until "
                    "you remove it."
                ),
                command_to_apply=(
                    f"# Consent-prompted removal (PR-34):\n"
                    f"python install.py --update --remove-deprecated-mcps\n"
                    f"# Or, for CI / scripted contexts:\n"
                    f"#   VCT_REMOVE_DEPRECATED_MCPS=all python install.py "
                    f"--update --remove-deprecated-mcps\n"
                    f"# Or, remove manually:\n"
                    f"#   Edit {claude_json} and delete the `\"{name}\": {{...}}` "
                    f"entry under `mcpServers`."
                ),
                severity="info",
                kg_node_refs=[
                    "knowledge/concepts/orchestrator-mcp-servers.md",
                    ".claude/context/mcp-install-pipeline-audit-2026-05-16.md",
                ],
            )
        )

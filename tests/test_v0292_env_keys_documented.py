# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Ratchet for MAJOR-13 (v0.2.92 review): every ``VCT_*`` / ``VCO_*`` env key
READ by shipped code must appear in at least one file under ``docs/``.

Why this gate exists
--------------------
The review found keys that real code reads — ``VCT_MODEL_GATEWAY_PORT`` among
them, a named acceptance criterion of the model-gateway work package —
documented nowhere under ``docs/``. ``tests/test_model_router_auth.py`` only
enforces that the *module docstring* of ``model_router/config.py`` lists the
gateway keys; a docstring is developer documentation, not user documentation.
This test fails the NEXT such key at CI time, before it ships.

What counts as a READ (how the scan finds keys)
-----------------------------------------------
Keys are found by the syntactic form that actually reads the environment, per
language — a mention of ``VCT_FOO`` in a comment does NOT count:

* **Python** (``.py`` files, plus Python embedded in ``.sh`` heredocs and
  ``.ps1`` here-strings):
  - direct literal reads: ``os.environ.get("K")``, ``os.getenv("K")``,
    ``os.environ.pop("K")``, ``os.environ["K"]`` (the last only when NOT the
    target of ``=``, i.e. a read, not a write);
  - identifier reads: ``os.environ.get(CONST)`` / ``<any>.get(CONST)`` where
    ``CONST = "VCT_..."`` is a constant defined in the same file;
  - param-forwarding wrappers: a module-level ``def f(name, ...)`` whose body
    reads ``os.environ.get(name)``, called as ``f("VCT_...")`` (the
    ``_env_int("VCT_MODEL_GATEWAY_CATALOG_TTL", ...)`` shape).
* **Shell** (``.sh``): ``$KEY`` / ``${KEY...}`` expansions after full-line
  ``#`` comments are stripped. ``export KEY=...`` (no ``$``) is a write and
  does not count; ``KEY="$OTHER"`` on the right-hand side IS a read of OTHER.
* **PowerShell** (``.ps1``): ``$env:KEY`` except when directly followed by
  ``=`` (assignment), after full-line ``#`` comments are stripped.
* **Rust** (``.rs``): ``env::var("K")`` / ``env::var_os("K")``.
* **TypeScript/Svelte** (``.ts``/``.svelte``/``.js``): ``process.env.K`` /
  ``import.meta.env.K``.

Surfaces scanned (shipped code): ``vco_lib/``, ``claude_mcp_servers/``,
``templates/``, the launcher's Rust workspace (``launcher/src-tauri/src/``,
``launcher/src-tauri/vct-launcher-core/``, ``launcher/src-tauri/vct-hub/`` —
including ``build.rs`` compile-time reads), the launcher front-end
(``launcher/src/``), ``scripts/``, ``tools/``, ``infrastructure/``, and
repo-root ``*.py``/``*.sh``/``*.ps1``.

NOT covered (known limitations — do not rely on these holes):
* f-string / concatenated / computed key names in any language;
* env files consumed by containers or CI workflows (``.github/workflows``,
  compose files) — those are not Python/JS/Rust reads;
* keys without the ``VCT_``/``VCO_`` prefix (``KG_COLLECTION``, ``WEAVIATE_URL``,
  ...) — a different naming convention, deliberately out of scope here;
* test trees (any ``tests``/``test`` path component, ``test_*`` basenames) and
  build output (``node_modules``, ``target``, ``dist``, ``.venv``) are excluded.

What counts as DOCUMENTED: the key string appears in ANY file under ``docs/``
with a word-style boundary (a ``VCO_HOOK_DEBUG`` mention does not document
``VCO_HOOK_TRACE``).

The ALLOWLIST below registers every key that was already read-and-undocumented
when the ratchet landed (v0.2.92), each with a one-line reason. Shrinking it
by documenting a key is always welcome; GROWING it requires documenting the
new key instead — that is the point of the gate.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"

#: Shipped-code surfaces. ``tests/`` and ``docs/`` are deliberately absent.
SCAN_DIRS = (
    "vco_lib",
    "claude_mcp_servers",
    "templates",
    "launcher/src-tauri/src",
    "launcher/src-tauri/vct-launcher-core",
    "launcher/src-tauri/vct-hub",
    "launcher/src",
    "scripts",
    "tools",
    "infrastructure",
)
#: Repo-root installer entry points (install.py, install.sh, first-install.sh, ...).
ROOT_GLOBS = ("*.py", "*.sh", "*.ps1")
SUFFIXES = frozenset({".py", ".sh", ".ps1", ".rs", ".ts", ".svelte", ".js"})

EXCLUDED_PARTS = frozenset({
    "tests", "test", "docs", "site", "knowledge", "publisher-ci",
    "node_modules", "target", "dist", ".venv", ".claude", ".git",
    ".svelte-kit", "fixtures",
})

KEY = r"(?:VCT|VCO)_[A-Z0-9]+(?:_[A-Z0-9]+)*"
#: Word-style boundary so VCO_HOOK_DEBUG never matches VCO_HOOK_TRAC[E].
BOUNDARY = r"(?![A-Z0-9_])"

PY_DIRECT = re.compile(
    r"os\.environ\.get\(\s*[\"'](" + KEY + r")[\"']" + BOUNDARY
    + r"|os\.getenv\(\s*[\"'](" + KEY + r")[\"']" + BOUNDARY
    + r"|os\.environ\.pop\(\s*[\"'](" + KEY + r")[\"']" + BOUNDARY
    # os.environ["K"] is a READ only when not the target of an assignment.
    # BOUNDARY before the (?!=) lookahead also blocks regex backtracking
    # from producing a truncated key on the write form.
    + r"|os\.environ\[\s*[\"'](" + KEY + r")[\"']\s*\]" + BOUNDARY + r"(?!\s*=(?!=))"
)
#: Identifier reads: os.environ.get(IDENT) or any "<mapping>.get(IDENT)"
#: (covers a Mapping param defaulting to os.environ, e.g. ``env.get(ENV_X)``).
PY_IDENT = re.compile(
    r"(?:os\.environ\.get|os\.getenv|os\.environ\.pop)\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,)]"
    r"|\w+\.get\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,)]"
)
#: ``CONST = "VCT_..."`` — the constant is only counted when PY_IDENT reads it.
PY_CONST = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[\"'](" + KEY + r")[\"']" + BOUNDARY, re.M,
)
#: ``def f(params...`` — no trailing ``:`` so ``-> Type`` annotations match.
PY_FUNC = re.compile(r"^[ \t]*def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", re.M)
FUNC_SPLIT = re.compile(r"(?=^[ \t]*def\s)", re.M)

SHELL_READ = re.compile(r"\$\{?(" + KEY + ")" + BOUNDARY)
#: $env:KEY is a READ unless directly followed by ``=`` (assignment).
PS_READ = re.compile(r"\$env:(" + KEY + ")" + BOUNDARY + r"(?!\s*=(?!=))")
RS_READ = re.compile(r"env::var(?:_os)?\(\s*[\"'](" + KEY + r")[\"']" + BOUNDARY)
TS_READ = re.compile(r"(?:process\.env|import\.meta\.env)\.(" + KEY + ")" + BOUNDARY)


def _strip_comment_lines(text: str) -> str:
    """Drop full-line ``#`` comments so a prose mention is never a read."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _is_test_or_build_path(path: Path) -> bool:
    parts = path.relative_to(ROOT).parts
    if EXCLUDED_PARTS & set(parts):
        return True
    return path.name.startswith("test_") or path.name.endswith("_test.py")


def _iter_scan_files() -> list[Path]:
    files: list[Path] = []
    for directory in SCAN_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in SUFFIXES and not _is_test_or_build_path(path):
                files.append(path)
    for pattern in ROOT_GLOBS:
        for path in ROOT.glob(pattern):
            if path.is_file() and not _is_test_or_build_path(path):
                files.append(path)
    return sorted(set(files))


def _scan_python_text(text: str, origin: str, reads: dict[str, set[str]]) -> None:
    """Record env keys read by Python source (incl. embedded in sh/ps1)."""
    for match in PY_DIRECT.finditer(text):
        key = next(g for g in match.groups() if g)
        reads.setdefault(key, set()).add(origin)

    constants = {m.group(1): m.group(2) for m in PY_CONST.finditer(text)}
    if constants:
        identifiers = {
            m.group(1) or m.group(2) for m in PY_IDENT.finditer(text)
        }
        for identifier in identifiers & set(constants):
            reads.setdefault(constants[identifier], set()).add(origin)

    # Param-forwarding wrappers: def f(name, ...) reading os.environ.get(name),
    # called as f("VCT_..."). The literal at the CALL SITE is the read.
    for block in FUNC_SPLIT.split(text):
        func = PY_FUNC.match(block)
        if not func:
            continue
        func_name, raw_params = func.group(1), func.group(2)
        param_names = {
            p.split("=")[0].split(":")[0].strip()
            for p in raw_params.split(",")
            if p.strip()
        }
        body_reads = {
            m.group(1) or m.group(2)
            for m in PY_IDENT.finditer(block)
            if (m.group(1) or m.group(2)) in param_names
        }
        if not body_reads:
            continue
        call_pattern = re.compile(
            r"\b" + re.escape(func_name) + r"\(\s*[\"'](" + KEY + r")[\"']",
        )
        for call in call_pattern.finditer(text):
            reads.setdefault(call.group(1), set()).add(origin)


def collect_env_key_reads() -> dict[str, set[str]]:
    """Every VCT_/VCO_ key read by shipped code, with its read-site files."""
    reads: dict[str, set[str]] = {}
    for path in _iter_scan_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        origin = str(path.relative_to(ROOT))
        if path.suffix == ".py":
            _scan_python_text(text, origin, reads)
        elif path.suffix == ".sh":
            stripped = _strip_comment_lines(text)
            for match in SHELL_READ.finditer(stripped):
                reads.setdefault(match.group(1), set()).add(origin)
            _scan_python_text(stripped, origin, reads)  # embedded python
        elif path.suffix == ".ps1":
            stripped = _strip_comment_lines(text)
            for match in PS_READ.finditer(stripped):
                reads.setdefault(match.group(1), set()).add(origin)
            _scan_python_text(stripped, origin, reads)  # embedded python
        elif path.suffix == ".rs":
            for match in RS_READ.finditer(text):
                reads.setdefault(match.group(1), set()).add(origin)
        elif path.suffix in {".ts", ".svelte", ".js"}:
            for match in TS_READ.finditer(text):
                reads.setdefault(match.group(1), set()).add(origin)
    return reads


def documented_keys(reads: dict[str, set[str]]) -> set[str]:
    """Keys whose string appears (boundary-guarded) in some file under docs/."""
    found: set[str] = set()
    if not DOCS_DIR.is_dir():
        return found
    patterns = {
        key: re.compile(re.escape(key) + r"(?![A-Z0-9_])") for key in reads
    }
    for path in DOCS_DIR.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for key, pattern in patterns.items():
            if key not in found and pattern.search(text):
                found.add(key)
    return found


#: Keys already read-and-undocumented when this ratchet landed (v0.2.92).
#: One-line reason each. Remove an entry as soon as the key is documented.
ALLOWED_UNDOCUMENTED: dict[str, str] = {
    # --- internal plumbing: a VCO parent process sets these, a VCO child
    # --- reads them. A user has no reason to set any of them.
    "VCO_COMPOSE_ARGV": "verify-container-ports hook plumbing",
    "VCO_COMPOSE_CMD": "container-hook plumbing (compose command handoff)",
    "VCO_DEFERRAL_PROJECT_DIR": "deferral-surface hook plumbing (project-dir handoff)",
    "VCO_HUB_BINARY": "session-start-ensure-hub hook plumbing",
    "VCO_HUB_PID": "session-start-ensure-hub hook plumbing",
    "VCO_HUB_REASON": "session-start-ensure-hub hook plumbing",
    "VCO_HUB_STATE": "session-start-ensure-hub hook plumbing",
    "VCO_PROGRESS_STREAM": "install.py machine-readable progress channel for the launcher GUI",
    "VCO_RUNTIME": "container-hook plumbing (resolved runtime handoff)",
    "VCO_RUNTIME_REASON": "container-hook plumbing (runtime decision provenance)",
    "VCO_RUNTIME_REQUESTED": "container-hook plumbing (requested-runtime handoff)",
    "VCO_RUNTIME_STATE": "container-hook plumbing (runtime state handoff)",
    "VCO_VENV_PYTHON": "set by resolve-vco-venv, read by sibling bundled hooks",
    "VCT_ANALYZER_SCRIPT": "code-graph hooks hand the analyzer path to their child",
    "VCT_AUTO_RESTART_LAUNCHER": "post-install launcher relaunch loop guard",
    "VCT_CODE_GRAPH_ACCESS_LIST": "per-project access snapshot the launcher writes into .claude/env; the launcher GUI is the user surface",
    "VCT_COMPOSE_CMD": "container-hook plumbing (compose command handoff)",
    "VCT_DIAGRAMS_ACCESS_LIST": "per-project access snapshot the launcher writes into .claude/env; the launcher GUI is the user surface",
    "VCT_FIELD": "hook-script -> embedded-python value channel (vct_project_config.sh)",
    "VCT_FORCE_RESTART_DEFERRAL": "install.py internal escape hatch (support tooling)",
    "VCT_INSTALL_RELAUNCHED": "install.py self-relaunch loop guard",
    "VCT_INSTALL_ROOT": "install-root handoff consumed by venv resolution in bundled hooks",
    "VCT_JSON_PATH": "hook-script -> embedded-python value channel",
    "VCT_KG_ACCESS_LIST": "per-project access snapshot the launcher writes into .claude/env; the launcher GUI is the user surface",
    "VCT_LAUNCHER_PID": "install.py launcher-restart guard",
    "VCT_ORCHESTRATOR_ROOT_KG_COLLECTION": "install.py internal (root-clone KG binding)",
    "VCT_PREBASH_CMD_LEN": "pre-bash-context-inject hook -> embedded-python channel",
    "VCT_PREBASH_QUERY": "pre-bash-context-inject hook -> embedded-python channel",
    "VCT_PREBASH_SESSION": "pre-bash-context-inject hook -> embedded-python channel",
    "VCT_PREBASH_TASK_ID": "pre-bash-context-inject hook -> embedded-python channel",
    "VCT_PREBASH_TS_MS": "pre-bash-context-inject hook -> embedded-python channel",
    "VCT_PROJECT_ID": "per-project identity handed to MCP wrappers by VCO",
    "VCT_PROJECT_PATH": "per-project identity handed to the search MCP wrapper by VCO",
    "VCT_PROJECT_ROOT": "per-project root handed to embedded python by the pre-bash hook",
    "VCT_PYTHON": "python-interpreter handoff between bundled hooks and children",
    "VCT_REMOVE_DEPRECATED_MCPS": "install.py internal escape hatch (support tooling)",
    "VCT_RENDER_FIELD": "hook-script -> embedded-python value channel (vct_project_config.sh)",
    "VCT_REWRITE_STALE_MCPS": "install.py internal escape hatch (support tooling)",
    "VCT_RL_MODULE_DEPRECATED": "compiled-in deprecation notice, env-overridable; launcher-internal",
    "VCT_RL_MODULE_DEPRECATION_DATE": "compiled-in deprecation notice, env-overridable; launcher-internal",
    "VCT_RL_MODULE_DEPRECATION_MESSAGE": "compiled-in deprecation notice, env-overridable; launcher-internal",
    "VCT_RL_MODULE_DEPRECATION_URL": "compiled-in deprecation notice, env-overridable; launcher-internal",
    "VCT_RUNC_ROOT": "container-hook plumbing (runc state dir)",
    "VCT_SESSION_ID": "per-session id handed to hooks/MCP by the harness and VCO",
    "VCT_STACK_WORKING_DIR": "container-stack working-dir handoff read by ensure-containers",
    "VCT_TOML_FIELD": "hook-script -> embedded-python value channel",
    "VCT_TOML_PATH": "hook-script -> embedded-python value channel",
    "VCT_TUNING_PATH": "hook-script -> embedded-python value channel",
    "VCT_TUNING_TARGET": "hook-script -> embedded-python value channel",
    "VCT_TUNING_VALUES": "hook-script -> embedded-python value channel",
    "VCT_VENV": "venv-path handoff between bundled hooks and children",
    # --- shell variables ASSIGNED (unconditionally, before any read) by
    # --- templates/hooks/_lib/container-names.sh — a user-set env value is
    # --- clobbered by the assignment, so these are not env knobs at all.
    # --- The user-facing override is VCT_REQUIRED_CONTAINERS (documented in
    # --- docs/features/03-agents-skills-hooks.md).
    "VCO_CODE_EMBED_CONTAINER": "library-assigned shell var (container-names.sh:40); user env is overwritten before any read",
    "VCO_OLLAMA_CONTAINER": "library-assigned shell var (container-names.sh:39); user env is overwritten before any read",
    "VCO_REQUIRED_CONTAINERS": "array set by container-names.sh:51-60 from VCT_REQUIRED_CONTAINERS or the canonical names; never read from user env as an override",
    "VCO_WEAVIATE_CONTAINER": "library-assigned shell var (container-names.sh:38); user env is overwritten before any read",
    # --- test-only / diagnostic sentinels
    "VCO_HOOK_TRACE": "debug trace flag for pre-edit-context-inject",
    "VCO_LOG_LEVEL": "internal logging verbosity (vco_lib.log_setup)",
    "VCT_DISABLE_HUB_RESOLVER": "diagnostic kill-switch bypassing the hub config resolver",
    "VCT_HOOK_LEAK_PROBE": "leak-probe sentinel in post-tool-security",
    "VCT_HUB_ALLOW_TEST_POST": "test-only sentinel in the rl hub writer",
    "VCT_MANIFEST_SANITIZER_BYPASS": "test/CI bypass for the module-manifest sanitizer",
    "VCT_PORT_WATCHDOG_VERBOSE": "verbosity flag for the port-watchdog hook",
    "VCT_SKIP_PORT_WATCHDOG": "CI kill-switch for the port-watchdog hook",
    "VCT_TEST_CLEANUP_COUNTER_FILE": "test-only spy hook in module-manifest extraction",
    "VCT_WEBKIT_PREFLIGHT_OFF": "diagnostic kill-switch for the Windows WebKit preflight",
    "VCT_KEYWORD_DEDUP_DIR": "test-only override pointing agent-skill-keyword dedup state at a tmp_path (docstring: TEST OVERRIDE)",
    "VCT_USER_HOME_OVERRIDE": "test-suite home sandbox: install.py::_user_home_for_install and vco_lib.paths.user_home honour it so pytest can redirect boot-service/config writes to tmp_path",
    "VCT_WORKTREE_GUARD_STRICT": "log annotation only — populates the `strict` field of worktree-guard's JSONL rows (worktree-guard.sh:138); gates no behaviour",
    # --- launcher-internal overrides (Rust side; code comments document them)
    "VCT_EMBED_MODEL_FOOTPRINT_MB": "RAM-admission footprint override for exotic hosts (launcher-internal)",
    "VCT_GPU_VENDOR": "GPU-detection override used while probing hardware profiles",
    "VCT_LAUNCHER_SCRIPTS_DIR": "launcher-internal scripts-dir lookup override",
    "VCT_MODULE_CATALOG_URL": "staging/endpoint override for paid-module infrastructure; operator-only",
    "VCT_REBIND_ADMIN_TOKEN_URL": "staging/endpoint override for paid-module infrastructure; operator-only",
    "VCT_RL_LATEST_VERSION_URL": "staging/endpoint override for paid-module infrastructure; operator-only",
    "VCT_RL_LATEST_WEIGHTS_URL": "staging/endpoint override for paid-module infrastructure; operator-only",
    "VCT_WEAVIATE_URL": "launcher-side Weaviate URL probe; the documented user channel is WEAVIATE_URL",
    "VCT_GRPC_PORT": "hub-side gRPC port probe (vct-hub config_api); the documented user channel is GRPC_PORT",
    "VCT_OLLAMA_URL": "hub-side Ollama URL probe (vct-hub config_api); the documented user channel is OLLAMA_URL",
    "VCT_HUB_BUILD_FINGERPRINT": "compile-time build fingerprint injected by release CI and read via option_env!; build plumbing",
    "VCT_TEST_LIVE_KEYCHAIN": "test-only gate for the live-keychain smoke (default cargo test no-ops without it)",
    # --- repo-maintainer tooling (scripts/, not a shipped user surface)
    "VCO_SHARED_KG_MIGRATE_CONSENT": "one-shot migration-script consent gate (scripts/)",
    "VCT_ASSET_REF_MIN": "repo-maintainer script knob (scripts/lib/asset-ref-count)",
    "VCT_STACK_CDI_TIMEOUT": "repo-maintainer stack-launch script knob (scripts/)",
    "VCT_STACK_COMPOSE_FILE": "repo-maintainer stack-launch script knob (scripts/)",
    "VCT_STACK_COMPOSE_OVERRIDE": "repo-maintainer stack-launch script knob (scripts/)",
    "VCT_STACK_GPU_OVERLAY": "repo-maintainer stack-launch script knob (scripts/)",
    "VCT_STACK_GPU_OVERLAY_DOCKER": "repo-maintainer stack-launch script knob (scripts/)",
    "VCT_STACK_LOG_FILE": "repo-maintainer stack-launch script knob (scripts/)",
    "VCT_STACK_RUNTIME_FILE": "repo-maintainer stack-launch script knob (scripts/)",
    # --- former "registered debt" bucket (v0.2.92): CLOSED before the
    # --- v0.2.92 tag. The genuinely user-tunable knobs among them are now
    # --- documented (docs/CONFIGURATION.md, docs/TROUBLESHOOTING.md,
    # --- docs/features/); the rest moved to the buckets above with the
    # --- read-site evidence that they are NOT user-facing env knobs.
}


def test_every_read_vct_vco_env_key_is_documented() -> None:
    reads = collect_env_key_reads()
    documented = documented_keys(reads)
    missing = {
        key: sites
        for key, sites in reads.items()
        if key not in documented and key not in ALLOWED_UNDOCUMENTED
    }
    if missing:
        lines = [
            "VCT_/VCO_ env keys READ by shipped code but absent from every file "
            "under docs/ (MAJOR-13 ratchet):",
        ]
        for key in sorted(missing):
            sites = ", ".join(sorted(missing[key])[:3])
            lines.append(f"  {key}  (read in: {sites})")
        lines.append(
            "Document the key in docs/ (docs/CONFIGURATION.md unless a better "
            "home exists) — or, if it is genuinely internal, say \"internal, do "
            "not set\" there. Do not extend ALLOWED_UNDOCUMENTED silently."
        )
        raise AssertionError("\n".join(lines))


def test_allowlist_holds_no_stale_entries() -> None:
    """Every allowlisted key must still be READ and still UNDOCUMENTED.

    Keeps the register honest in both directions: documenting a key (or
    deleting its read) makes its entry stale, and stale entries hide real
    signal about how large the debt still is.
    """
    reads = collect_env_key_reads()
    documented = documented_keys(reads)
    stale_documented = sorted(set(ALLOWED_UNDOCUMENTED) & documented)
    stale_unread = sorted(set(ALLOWED_UNDOCUMENTED) - set(reads))
    problems = []
    if stale_documented:
        problems.append(
            "documented in docs/ — remove from ALLOWED_UNDOCUMENTED: "
            + ", ".join(stale_documented),
        )
    if stale_unread:
        problems.append(
            "no longer read by shipped code — remove from ALLOWED_UNDOCUMENTED: "
            + ", ".join(stale_unread),
        )
    assert not problems, "\n".join(problems)

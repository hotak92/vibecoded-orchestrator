"""V52-D.3: manifest sanitizer for 3rd-party `vct-module.json` files.

This module validates extracted module manifests against a set of
defense-in-depth rules that catch the pre-v0.2.49 Bug E pattern AND
deliberately-malicious / authoring-mistake shapes from 3rd-party
publishers.

The Rust side has its own runtime defense (`is_runtime_pathological`
in `vct-launcher-core/src/services/container_runtime.rs`, V52-D.1)
that drops the CMD override before podman sees it. This Python
sanitizer is the FIRST line: it runs at install time, BEFORE the
manifest is committed to `~/.vct/modules/<id>/vct-module.json`, so a
malformed manifest never reaches disk.

## Validation layers

Layer 1 (JSON schema): the manifest parses as JSON and has the
required top-level keys (`id`, `version`, `install`, `runtime`).
Missing-key failures are LOUD — the manifest is rejected with a
specific reason.

Layer 2 (runtime semantics): the `runtime.command` + `runtime.args`
block is checked for the V52-D.1 indicator set:
    * `command` is "podman" / "docker" → reject (pre-v0.2.49 Bug E)
    * `command` is "sh" / "bash" without "-c" → reject
    * `args` contains an unsubstituted `{module_image}` placeholder
      → reject
    * `args` contains other launcher-side placeholders that aren't
      part of the known-good set → reject

Layer 3 (install.scope coherence): if `install.scope = "global"`,
the runtime block must NOT include `{project_slug}` in
`container_name_template` (the v0.2.49 contract). Conversely, a
manifest with `install.scope` missing AND a `container_name_template`
that lacks `{project_slug}` is suspicious (looks global but isn't
declared).

## Usage

Python API:
    from vco_lib.manifest_validation import validate_manifest_file
    result = validate_manifest_file(Path("vct-module.json"))
    if not result.is_valid:
        print(f"rejected: {result.error}")

CLI (for Rust `extract_manifest_from_image` to invoke as a
subprocess):
    python -m vco_lib.manifest_validation /path/to/vct-module.json

Exit codes:
    0  — valid
    1  — invalid (reason on stderr, JSON details on stdout)
    2  — invocation error (file missing, no path arg, etc.)
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Placeholders the launcher's substitution layer KNOWS about. Any
# `{...}` token in `runtime.args` that isn't in this set is treated
# as unsubstituted → manifest is rejected.
#
# Source of truth: `vct-launcher-core/src/services/container_runtime.rs`
# helpers `rl_placeholders` + `rl_placeholders_global` + the manifest
# resolver's substitution map. Keep this set in sync when adding new
# placeholder names on the Rust side.
_KNOWN_PLACEHOLDERS: frozenset[str] = frozenset({
    "project_slug",
    "RL_SERVER_PORT",
    "ollama_port",
    "VCT_DATA",
    "VCT_LOGS",
    "VCT_MODULES",
    "HOME",
    "install.container.image",
    "install.container.tag",
})

# `runtime.command` values that strongly suggest the pre-v0.2.49
# Bug E manifest pattern (launcher-side podman invocation embedded
# as the container CMD).
_DANGEROUS_RUNTIME_COMMANDS: frozenset[str] = frozenset({
    "podman",
    "docker",
})

# Shell binaries that REQUIRE a `-c` arg to be useful as a container
# CMD. Without `-c`, the shell exits immediately under detached mode
# — almost certainly an authoring mistake.
_SHELL_COMMANDS_REQUIRING_DASH_C: frozenset[str] = frozenset({
    "sh",
    "bash",
    "zsh",
    "dash",
})

# Top-level keys every valid manifest MUST carry. Anything missing
# means the file isn't a recognisable module manifest.
_REQUIRED_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "id",
    "version",
    "install",
    "runtime",
)


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of `validate_manifest_*`. Carries the full reason on
    failure so the caller can surface it via `module_installs.last_error`
    or refuse the install with a clear operator-facing message."""

    is_valid: bool
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    """Non-fatal advisories — surfaced in logs but do NOT block install."""

    @classmethod
    def ok(cls, warnings: list[str] | None = None) -> "ValidationResult":
        return cls(is_valid=True, error=None, warnings=warnings or [])

    @classmethod
    def fail(cls, reason: str, warnings: list[str] | None = None) -> "ValidationResult":
        return cls(is_valid=False, error=reason, warnings=warnings or [])


def _extract_placeholders(value: str) -> list[str]:
    """Return every `{...}` token in `value`. Naive scan (doesn't
    handle escaped braces) — sufficient for our validation needs."""
    tokens: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "{":
            end = value.find("}", i)
            if end == -1:
                break
            tokens.append(value[i + 1 : end])
            i = end + 1
        else:
            i += 1
    return tokens


def validate_runtime_block(runtime: dict[str, Any]) -> ValidationResult:
    """Layer 2 validation: check `runtime.command` + `runtime.args`
    for the V52-D.1 indicator set + unsubstituted-placeholder leaks.
    """
    command = runtime.get("command")
    args = runtime.get("args", [])

    # Soft-fail: missing args field → treat as []. Missing command
    # field → treat as "" (= declarative; use image ENTRYPOINT).
    if command is None:
        command = ""
    if args is None:
        args = []

    if not isinstance(command, str):
        return ValidationResult.fail(
            f"runtime.command must be a string, got {type(command).__name__}"
        )
    if not isinstance(args, list):
        return ValidationResult.fail(
            f"runtime.args must be a list, got {type(args).__name__}"
        )
    for i, a in enumerate(args):
        if not isinstance(a, str):
            return ValidationResult.fail(
                f"runtime.args[{i}] must be a string, got {type(a).__name__}"
            )

    cmd_trim = command.strip()

    # Indicator 1: command is a container-runtime binary name.
    if cmd_trim in _DANGEROUS_RUNTIME_COMMANDS:
        return ValidationResult.fail(
            f"runtime.command='{cmd_trim}' is a container-runtime binary name. "
            "This is the pre-v0.2.49 Bug E manifest pattern (launcher-side podman "
            "invocation embedded as the container CMD). Set runtime.command='' to "
            "use the image's ENTRYPOINT, OR set it to the actual in-container "
            "binary (e.g. 'python', 'node')."
        )

    # Indicator 2: shell without -c arg.
    if cmd_trim in _SHELL_COMMANDS_REQUIRING_DASH_C:
        has_dash_c = any(a == "-c" for a in args)
        if not has_dash_c:
            return ValidationResult.fail(
                f"runtime.command='{cmd_trim}' requires a '-c <cmd>' arg pair. "
                f"A detached shell with no -c exits immediately, which is almost "
                f"certainly an authoring mistake. args={args!r}"
            )

    # Indicator 3: unsubstituted {module_image} placeholder anywhere
    # in args. The launcher never substitutes {module_image} — it's
    # the launcher-side variable for the positional image arg, NOT
    # the in-container CMD.
    for i, a in enumerate(args):
        if "{module_image}" in a:
            return ValidationResult.fail(
                f"runtime.args[{i}]={a!r} contains unsubstituted "
                f"'{{module_image}}' placeholder. This is the pre-v0.2.49 Bug E "
                f"pattern. Set runtime.command='' to use the image's ENTRYPOINT, "
                f"or remove the placeholder."
            )

    # Indicator 3b: unknown placeholders in args. Catches typos like
    # `{project-slug}` (with dash) or invented variables the launcher
    # doesn't substitute.
    for i, a in enumerate(args):
        tokens = _extract_placeholders(a)
        for tok in tokens:
            if tok not in _KNOWN_PLACEHOLDERS:
                return ValidationResult.fail(
                    f"runtime.args[{i}]={a!r} contains unknown placeholder "
                    f"'{{{tok}}}'. Known placeholders: "
                    f"{sorted(_KNOWN_PLACEHOLDERS)}"
                )

    # Indicator 3c: same check on `runtime.command` itself.
    cmd_tokens = _extract_placeholders(command)
    if "module_image" in cmd_tokens:
        return ValidationResult.fail(
            f"runtime.command={command!r} contains unsubstituted "
            f"'{{module_image}}' placeholder (V52-D.3 reject)."
        )
    for tok in cmd_tokens:
        if tok not in _KNOWN_PLACEHOLDERS:
            return ValidationResult.fail(
                f"runtime.command={command!r} contains unknown placeholder "
                f"'{{{tok}}}'. Known placeholders: {sorted(_KNOWN_PLACEHOLDERS)}"
            )

    return ValidationResult.ok()


def validate_install_scope_coherence(manifest: dict[str, Any]) -> ValidationResult:
    """Layer 3 validation: install.scope vs container_name_template
    coherence. A `scope='global'` manifest with a per-project template
    is an authoring slip — the v0.2.49 contract is that global
    containers have names without `{project_slug}`.
    """
    install = manifest.get("install", {})
    runtime = manifest.get("runtime", {})
    scope_raw = install.get("scope")
    if scope_raw is None:
        # No scope declaration → defaults to per_project on the
        # launcher side. Soft warning if the template ALSO lacks
        # {project_slug} (looks global but isn't declared).
        cnt = runtime.get("container_name_template") or ""
        if cnt and "{project_slug}" not in cnt:
            return ValidationResult.ok(warnings=[
                f"install.scope unset and container_name_template={cnt!r} "
                "lacks {project_slug}. This will produce a single container "
                "shared across all projects, which is most likely NOT what "
                "you want. Declare install.scope='global' if intentional, "
                "or add '-{project_slug}' to the template."
            ])
        return ValidationResult.ok()

    scope = scope_raw.lower() if isinstance(scope_raw, str) else scope_raw
    if scope == "global":
        # container_name_template must either be unset OR end with
        # `-{project_slug}` (the launcher's resolve_global_container_
        # name strips this for global modules), OR contain NO
        # {project_slug} at all. ANY non-trailing {project_slug}
        # is an authoring error.
        cnt = runtime.get("container_name_template") or ""
        if "{project_slug}" in cnt:
            # Trailing form: `<base>-{project_slug}` or
            # `<base>_{project_slug}`. Anything else is rejected.
            trailing_ok = (
                cnt.endswith("-{project_slug}")
                or cnt.endswith("_{project_slug}")
            )
            if not trailing_ok:
                return ValidationResult.fail(
                    f"install.scope='global' but container_name_template="
                    f"{cnt!r} contains {{project_slug}} in a non-trailing "
                    f"position. The launcher's resolve_global_container_name "
                    f"can only safely strip a trailing -{{project_slug}}; "
                    f"non-trailing placeholders produce an undefined "
                    f"container name for global scope."
                )
    elif scope == "per_project":
        # No additional constraints — the default behavior.
        pass
    else:
        return ValidationResult.fail(
            f"install.scope={scope_raw!r} must be 'global' or 'per_project' "
            f"(case-insensitive)."
        )

    return ValidationResult.ok()


def validate_manifest_dict(manifest: dict[str, Any]) -> ValidationResult:
    """Top-level validator for a parsed manifest dict. Combines all
    layers; first-failure-wins."""
    if not isinstance(manifest, dict):
        return ValidationResult.fail(
            f"manifest must be a JSON object at top level, got "
            f"{type(manifest).__name__}"
        )

    # Layer 1: required keys.
    for key in _REQUIRED_TOP_LEVEL_KEYS:
        if key not in manifest:
            return ValidationResult.fail(
                f"manifest missing required top-level key '{key}'"
            )

    # Layer 1b: id must be non-empty string.
    mid = manifest.get("id")
    if not isinstance(mid, str) or not mid.strip():
        return ValidationResult.fail(
            f"manifest.id must be a non-empty string, got {mid!r}"
        )

    # Layer 1c: install/runtime must be dicts.
    install = manifest.get("install")
    runtime = manifest.get("runtime")
    if not isinstance(install, dict):
        return ValidationResult.fail(
            f"manifest.install must be an object, got {type(install).__name__}"
        )
    if not isinstance(runtime, dict):
        return ValidationResult.fail(
            f"manifest.runtime must be an object, got {type(runtime).__name__}"
        )

    # Layer 2: runtime block.
    runtime_result = validate_runtime_block(runtime)
    if not runtime_result.is_valid:
        return runtime_result

    # Layer 3: install.scope coherence.
    scope_result = validate_install_scope_coherence(manifest)
    if not scope_result.is_valid:
        return scope_result

    # Combine warnings from all layers.
    all_warnings = list(runtime_result.warnings) + list(scope_result.warnings)
    return ValidationResult.ok(warnings=all_warnings)


def validate_manifest_file(path: Path) -> ValidationResult:
    """Read + parse + validate a manifest at `path`. Wraps
    `validate_manifest_dict` with file-IO and JSON-parse error
    surfacing.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ValidationResult.fail(f"manifest file not found: {path}")
    except OSError as e:
        return ValidationResult.fail(f"read manifest {path}: {e}")

    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as e:
        return ValidationResult.fail(f"manifest {path} is not valid JSON: {e}")

    return validate_manifest_dict(manifest)


def _cli_main(argv: list[str]) -> int:
    """CLI entry point. Argv shape: `<prog> <manifest_path>`.

    Exit codes:
        0 — valid
        1 — invalid (reason on stderr, JSON details on stdout)
        2 — invocation error
    """
    if len(argv) != 2:
        print(
            "usage: python -m vco_lib.manifest_validation <manifest_path>",
            file=sys.stderr,
        )
        return 2
    path = Path(argv[1])
    result = validate_manifest_file(path)

    # Emit machine-readable JSON on stdout always — the Rust caller
    # can parse it without re-running.
    out = {
        "is_valid": result.is_valid,
        "error": result.error,
        "warnings": result.warnings,
    }
    print(json.dumps(out, indent=2))

    if result.is_valid:
        for w in result.warnings:
            print(f"[manifest_validation] WARN: {w}", file=sys.stderr)
        return 0
    print(f"[manifest_validation] REJECTED: {result.error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli_main(sys.argv))

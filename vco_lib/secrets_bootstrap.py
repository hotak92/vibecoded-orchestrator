# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Bootstrap-time helpers for the vct-secrets primitive (v0.2.54).

Extracted from install.py to keep install.py modular (per the
search-before-add / extract-before-duplicate discipline). Two pure
helpers + one templated-file materializer:

- :func:`detect_secrets_envelope` — read-only probe that builds the
  ``secrets`` block embedded in the bootstrap envelope (S-8).
- :func:`materialize_shared_readme` — write the agent-facing key-schema
  README at ``~/.vct-secrets/shared/_README.md`` (S-3), idempotently.

Neither helper logs directly; callers handle logging so install.py
can keep its phase-prefixed install-event stream.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Soft probe timeout for the git-credential-helper detection.
# Matches install.py's BOOTSTRAP_PROBE_TIMEOUT_S default.
DEFAULT_PROBE_TIMEOUT_S = 10


def _secrets_root_from_env() -> Path:
    return Path(
        os.environ.get("VCT_SECRETS_DIR") or (Path.home() / ".vct-secrets")
    )


def detect_secrets_envelope(
    install_root: Path,
    *,
    probe_timeout_s: int = DEFAULT_PROBE_TIMEOUT_S,
) -> dict:
    """Build the ``secrets`` envelope block (v0.2.54 S-8).

    One-stop machine-readable answer to "what secrets exist, how do I
    use them, are they wired up?" for agents that prepass-read the
    envelope. Lists key NAMES only — never values. All probes soft-fail.
    """
    secrets_root = _secrets_root_from_env()
    shared_dir = secrets_root / "shared"

    shared_keys: list[str] = []
    try:
        if shared_dir.is_dir():
            shared_keys = sorted(
                p.name for p in shared_dir.iterdir()
                if p.is_file() and p.name != "_README.md"
                and not p.name.startswith(".")
            )
    except OSError:
        shared_keys = []

    # git-credential-vct registration probe (names only, no secret I/O).
    helper_registered = False
    try:
        r = subprocess.run(
            ["git", "config", "--global", "--get-all",
             "credential.https://github.com.helper"],
            capture_output=True, text=True,
            timeout=probe_timeout_s,
        )
        helper_registered = (
            r.returncode == 0 and "git-credential-vct" in (r.stdout or "")
        )
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        helper_registered = False

    shared_readme = shared_dir / "_README.md"
    return {
        "primitive": "vct-secrets",
        "cli": str(install_root / "tools" / "vct-secrets" / "vct"),
        "store_dir": str(secrets_root),
        "store_dir_exists": secrets_root.is_dir(),
        "shared_readme": str(shared_readme),
        "shared_readme_exists": shared_readme.is_file(),
        "shared_keys_available": shared_keys,
        "credential_helper": str(
            install_root / "tools" / "vct-secrets" / "git-credential-vct"
        ),
        "credential_helper_registered": helper_registered,
        # Launcher-managed slots resolve via vct-hub, not the file store.
        "hub_env_endpoint": "http://127.0.0.1:7700/api/v1/projects/{id}/env",
        "hub_resolver_clients": [
            "templates/scripts/vct_secrets_resolve.sh",
            "templates/scripts/vct_secrets_resolve.ps1",
            "vco_lib/agent_secrets.py",
        ],
        "docs": "docs/VCT_SECRETS_PRIMITIVE.md",
    }


@dataclass(frozen=True)
class MaterializeReadmeResult:
    """Result of :func:`materialize_shared_readme`.

    ``status`` is one of:
    - ``"materialized"`` — README written for the first time.
    - ``"preserved"`` — README already existed; user copy untouched.
    - ``"template_missing"`` — bundled template not found; skipped.
    - ``"error"`` — OSError during write; ``message`` carries the cause.
    """

    status: str
    target: Path | None = None
    message: str = ""


def materialize_shared_readme(install_root: Path) -> MaterializeReadmeResult:
    """v0.2.54 S-3: materialize the agent-facing key-schema doc at
    ``~/.vct-secrets/shared/_README.md`` (honoring ``$VCT_SECRETS_DIR``).

    Idempotent + user-respecting: ONLY written when the file does not
    already exist (users edit it to document their own keys; we never
    clobber). Creates the store skeleton (700 dirs) when absent so the
    doc has a place to land even before the first ``vct set``.

    Returns a :class:`MaterializeReadmeResult` so the caller can render
    its own log line (install.py wants phase-prefixed events; other
    callers can stay silent).
    """
    template_path = (
        install_root / "templates" / "vct-secrets-shared-readme.template"
    )
    if not template_path.is_file():
        return MaterializeReadmeResult(
            status="template_missing",
            message=f"template missing at {template_path}",
        )
    try:
        secrets_root = _secrets_root_from_env()
        shared_dir = secrets_root / "shared"
        target = shared_dir / "_README.md"
        if target.is_file():
            return MaterializeReadmeResult(
                status="preserved",
                target=target,
                message="user copy preserved",
            )
        shared_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(secrets_root, 0o700)
            os.chmod(shared_dir, 0o700)
        target.write_text(
            template_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        if os.name == "posix":
            os.chmod(target, 0o600)
        return MaterializeReadmeResult(status="materialized", target=target)
    except OSError as e:
        return MaterializeReadmeResult(
            status="error",
            message=f"failed to materialize: {e}",
        )

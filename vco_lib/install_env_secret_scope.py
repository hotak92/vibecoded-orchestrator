# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V47-C env-secret migration SCOPE resolution for install.py (GAP-1, 2026-07-14).

Extracted from ``install.py::_audit_and_offer_env_secret_migration`` so the
per-project scope decision + its deferral-entry construction + the post-migrate
scope messaging live in ONE pure module instead of inline in the 25k-line
monolith (CLAUDE.md modularity rule: >50 contiguous lines into a >5k-line file
→ extract).

The scope DECISION itself is NOT re-implemented here — the hub owns it
(``secret_scope_policy::decide_env_migration_scope``, the A-level of the A>B>C
rule). This module only resolves the launcher.db project id to SEND to the hub
(via the shared by-path resolver) and decides, for the CLI specifically,
whether to migrate-with-id / migrate-as-root / DEFER (an unregistered non-root
project must never be back-doored to Shared through the CLI — that recreates
the cross-tenant leak GAP-1 fixes).

install.py imports FROM this module; this module never imports install.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vco_lib.deferral_report import DeferralEntry


@dataclass(frozen=True)
class EnvSecretMigrationTarget:
    """Resolved CLI action for a project's ``.env`` migration.

    Exactly one of the branches applies:

    * ``action == "migrate"`` → POST to the hub with ``project_id`` (may be
      ``None`` for the orchestrator root or a hub-down fall-through; the hub's
      scope policy then decides Shared vs PerProject).
    * ``action == "defer"`` → do NOT migrate; ``deferral`` carries the entry to
      record (unregistered non-root — no CLI back-door to Shared).
    """

    action: str  # "migrate" | "defer"
    project_id: str | None = None
    deferral: DeferralEntry | None = None


def resolve_env_secret_migration_target(
    project_root: Path,
    clone_root: Path,
    env_path: Path,
    sorted_keys: list[str],
) -> EnvSecretMigrationTarget:
    """Decide the CLI migration action for ``project_root``'s ``.env``.

    Resolves the launcher.db project id via the shared by-path resolver
    (``vco_lib.project_config._resolve_project_id``) and maps the outcome:

    * resolved id → migrate with that id (hub scopes per-project / Shared-for-root).
    * ProjectNotFound + project IS the clone root → migrate with ``None``
      (V47-C contract; hub writes Shared, correct for root).
    * ProjectNotFound + project is NOT the clone root → DEFER (unregistered
      non-root; migrating to Shared via the CLI would leak).
    * HubUnreachable / Forbidden → migrate with ``None`` (the caller's
      ``_post_secrets_to_hub`` will itself raise on a down hub and route to the
      existing hub-migration-failed deferral).
    """
    from vco_lib.project_config import (
        _resolve_project_id,
        ProjectNotFound,
        HubUnreachable,
        Forbidden,
    )

    try:
        pid = _resolve_project_id(str(project_root))
        return EnvSecretMigrationTarget(action="migrate", project_id=pid)
    except ProjectNotFound:
        if project_root.resolve() == clone_root.resolve():
            # Orchestrator clone root, not yet registered (launcher never ran).
            # Preserve the V47-C contract: migrate with id=None → hub Shared.
            return EnvSecretMigrationTarget(action="migrate", project_id=None)
        # Fresh, UNREGISTERED non-root adopt → defer (no CLI back-door).
        n = len(sorted_keys)
        plural = "" if n == 1 else "s"
        entry = DeferralEntry(
            condition_id="env_secrets_project_not_registered",
            title=(
                f"{n} secret-shaped key{plural} not migrated — "
                f"project not registered with the launcher"
            ),
            detected=(
                f"V47-C detected secret-shaped key{plural} in `{env_path}`: "
                f"{', '.join(f'`{k}`' for k in sorted_keys)}. The project at "
                f"`{project_root}` is not registered with the launcher, so the "
                f"CLI cannot scope the secrets to it — and migrating to the "
                f"SHARED bucket would leak per-project credentials into every "
                f"other registered project."
            ),
            why_deferred=(
                "Per-project secret scoping (GAP-1) requires a launcher.db "
                "project id, which only exists after the project is added in "
                "the launcher. Migrating to the shared bucket instead would be "
                "a cross-tenant leak, so the conservative-defaults rule says "
                "defer."
            ),
            command_to_apply=(
                "# 1. Add this project in the launcher GUI (Projects → Add), OR:\n"
                "#      python install.py --update  # from the project root\n"
                "# 2. Then migrate its .env from the launcher's per-project\n"
                "#    Secrets tab ('Migrate from .env'), OR re-run:\n"
                "#      python install.py --update --apply-deferred"
            ),
            severity="info",
            kg_node_refs=["knowledge/concepts/secret-management.md"],
        )
        return EnvSecretMigrationTarget(action="defer", deferral=entry)
    except (HubUnreachable, Forbidden):
        # Hub down / anomalous 403: fall through with id=None; the hub POST
        # will raise → the existing hub-migration-failed deferral handles it.
        return EnvSecretMigrationTarget(action="migrate", project_id=None)


def post_secrets_to_hub(
    secrets_payload: list[dict],
    project_id: str | None = None,
) -> tuple[list[str], list[dict], str | None]:
    """POST to ``/api/v1/secrets/migrate`` and return ``(migrated, failed, scope)``.

    When ``project_id`` is supplied it is forwarded so the hub's scope policy
    (S1) routes the keys into THAT project's scope (or the shared bucket for
    the orchestrator root). ``None`` preserves the original V47-C contract
    (shared bucket). ``scope`` is ``"shared"`` | ``"per_project"`` from the
    hub, or ``None`` when the hub response omits the field (an OLD hub that
    predates per-project migration → keys went to Shared).

    Raises ``RuntimeError`` when the hub is unreachable / returns a non-2xx
    response. Discovery reuses ``vco_lib.project_config._discover_hub`` (same
    port + token the resolver clients use).
    """
    import json

    try:
        import requests
    except ImportError as exc:
        raise RuntimeError(
            "V47-C: cannot migrate secrets — `requests` not importable; "
            "is the MCP venv active?"
        ) from exc

    from vco_lib.project_config import _discover_hub, HubUnreachable

    try:
        port, token = _discover_hub()
    except HubUnreachable as exc:
        raise RuntimeError(f"V47-C: hub unreachable: {exc}") from exc

    url = f"http://127.0.0.1:{port}/api/v1/secrets/migrate"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    request_body: dict = {"secrets": secrets_payload}
    if project_id is not None:
        # GAP-1: forward the owning project id so the hub routes per-project.
        request_body["project_id"] = project_id
    body = json.dumps(request_body).encode("utf-8")
    try:
        resp = requests.post(url, data=body, headers=headers, timeout=(2.0, 30.0))
    except requests.RequestException as exc:
        raise RuntimeError(f"V47-C: POST {url} failed: {exc}") from exc

    if resp.status_code >= 400:
        raise RuntimeError(
            f"V47-C: hub returned HTTP {resp.status_code}: {resp.text[:300]!r}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"V47-C: hub response not JSON: {resp.text[:300]!r}"
        ) from exc

    migrated = list(data.get("migrated") or [])
    failed = list(data.get("failed") or [])
    # `scope` absent on an OLD hub (pre-GAP-1) → None signals the caller to
    # warn the keys landed in Shared regardless of the sent id.
    scope = data.get("scope") if isinstance(data, dict) else None
    return migrated, failed, scope


def print_secrets_migration_details() -> None:
    """Print the long-form explanation shown on the ``details`` prompt option
    of install.py's V47-C env-secret migration flow."""
    print()
    print("  What this does:")
    print("    1. Reads each secret-shaped key/value from your `.env`")
    print("    2. POSTs the values to your local vct-hub on 127.0.0.1")
    print("    3. The hub writes them to the OS keychain (libsecret on")
    print("       Linux, Keychain on macOS, Credential Manager on Windows)")
    print("       under THIS project's scope (or the shared scope for the")
    print("       orchestrator root)")
    print("    4. The .env values are replaced with `__vco_keychain__`")
    print("       so bundled tooling knows to ask the hub at runtime")
    print()
    print("  What this does NOT do:")
    print("    * No values leave your machine. The hub binds 127.0.0.1.")
    print("    * Your `.env` file is NOT deleted — the keys stay there")
    print("      with a sentinel value so .env-aware tools keep working.")
    print("    * Already-migrated entries (value == `__vco_keychain__`)")
    print("      are silently skipped.")
    print()
    print("  Why migrate:")
    print("    * Keychain encrypts secrets at rest (libsecret/Keychain).")
    print("    * Plaintext .env is readable by ANY process running as you,")
    print("      including a rogue `pip install` or browser extension.")
    print("    * Launcher GUI can rotate/pause/revoke keys without you")
    print("      having to grep through .env files.")
    print()


def format_scope_notice(sent_project_id: str | None, hub_scope: str | None) -> str | None:
    """Return the one-line post-migrate notice for the CLI, or ``None``.

    * sent an id but hub omitted ``scope`` (old hub) → warn keys went Shared +
      to re-scope.
    * ``per_project`` / ``shared`` → confirm the destination.
    """
    if sent_project_id is not None and hub_scope is None:
        return (
            "Note: your vct-hub predates per-project secret migration, so "
            "these keys landed in the SHARED keychain bucket (visible to every "
            "registered project). Update the orchestrator, then re-scope from "
            "the launcher's per-project Secrets tab."
        )
    if hub_scope == "per_project":
        return "Secrets scoped to THIS project's keychain."
    if hub_scope == "shared":
        return "Secrets written to the SHARED keychain bucket."
    return None

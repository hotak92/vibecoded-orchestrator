# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The OpenAI API key: one name, one store, one resolver (v0.2.97).

VCO never writes a secret VALUE into the project tree; resolvers read it at
need. Before v0.2.97 ``install.py --openai-key KEY`` wrote the key into the
orchestrator root's ``.env`` — and nothing read it back from there (no VCO
code loads a ``.env`` into the environment), while the launcher's slot for
it went unread by the Python consumers. This module closes both halves:

* **Name** — :data:`OPENAI_SECRET_NAME` = ``openai_api_key``, the slot
  ``vct-module.json`` ``bundled_secrets`` declares (scope ``shared``,
  module ``user``), which the launcher's ``register_openai_api_key`` writes
  and the hub's ``/env`` serves. No second name.
* **Store** — :func:`store_openai_api_key`: the launcher keychain through
  the hub (``POST /api/v1/secrets/migrate``, Shared scope — the same row the
  GUI writes) when the hub answers, else the file store
  ``$VCT_SECRETS_DIR/shared/openai_api_key`` through the ``vct`` CLI (its
  write guards apply).
* **Resolve** — :func:`resolve_openai_api_key`: VCO's OWN slot only — the
  shared ``openai_api_key`` (launcher keychain through the hub, else
  ``~/.vct-secrets/shared/openai_api_key``), through
  :func:`vco_lib.agent_secrets.get` in its shared-only mode. NOT
  ``$OPENAI_API_KEY``, NOT a per-project binding, NOT the project's
  ``.env``: a project's key is the project's (owner ruling 2026-09-26).
  Every Python reader goes through it.
* **Migrate** — :func:`migrate_dotenv_openai_key`: a pre-v0.2.97 root
  ``.env`` line VCO wrote (under its ``# OpenAI (for embeddings)`` header)
  is removed ONLY on value evidence — it equals what the store holds,
  after copying it into the file store when nothing is stored yet. A line
  the user wrote anywhere else is theirs and is not touched.

No function here logs, prints or returns a VALUE. (The ``__main__`` CLI
prints the migration's JSON verdict — status and detail, which carry no
value by the contract above.)
"""

from __future__ import annotations

import hmac
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

#: The one name of the slot (``vct-module.json`` ``bundled_secrets``).
OPENAI_SECRET_NAME = "openai_api_key"
#: The variable VCO no longer reads for its own key (v0.2.98). Kept for the
#: two places it still means something: the legacy ``.env`` LINE key
#: :func:`migrate_dotenv_openai_key` removes, and the once-per-process warning
#: :func:`_warn_env_var_ignored` emits when a pre-v0.2.98 env-var user would
#: otherwise lose the key silently. A project shell may export its OWN key
#: under this name — that is exactly why it must not answer for VCO.
OPENAI_ENV_VAR = "OPENAI_API_KEY"
#: The comment line ``install.py`` wrote right above the key before v0.2.97
#: — the provenance of a VCO-written ``.env`` line.
LEGACY_DOTENV_HEADER = "# OpenAI (for embeddings)"

_resolved: dict[str, str] = {}


class StoreFailed(RuntimeError):
    """Neither store accepted the key. The message names the stores and the
    reason, never the value."""


class MigrationUnavailable(RuntimeError):
    """The venv-side migration subprocess could not run, failed, or did not
    answer. The message names the interpreter and the reason, never a value."""


#: One warning per process: the env var is a process-wide fact, and a per-call
#: repeat would be noise in an embedding loop.
_warned_env_var = False


def _warn_env_var_ignored(unreadable: str = "") -> None:
    """Say ONCE, on stderr, that ``$OPENAI_API_KEY`` is set while VCO's own
    slot did not answer — the one way a pre-v0.2.98 env-var user can lose the
    key without being told.

    Names the variable and, on a clean miss, the remedy; never a value. The
    two cases are DIFFERENT claims and must not share a sentence:

    * ``unreadable != ""`` — the slot provably could NOT be read (a locked
      keychain, a refusal, an OS error). The key may be sitting there right
      now, so asserting it is absent would be false, and the "store the key"
      remedy would talk the user into a SECOND copy — the very divergence
      this module exists to prevent. Name the failure and stop there.
    * ``unreadable == ""`` — everything else. The sentence therefore claims
      only what is ALWAYS true (no key was RESOLVED from the slot), never
      that the slot is empty: an unreachable hub with file fallback on also
      arrives here as a plain ``SecretNotFound`` (see
      :func:`~vco_lib.agent_secrets.get`'s tail), and "hub down" is not
      "empty". The remedy carries its own precondition for the same reason.

    Only fires on a MISS (when the slot answered there is nothing to report),
    and never raises: a diagnostic must not be the thing that breaks an
    embedding run."""
    global _warned_env_var
    if _warned_env_var or not os.environ.get(OPENAI_ENV_VAR, "").strip():
        return
    _warned_env_var = True
    import sys

    shared = (
        f"{OPENAI_ENV_VAR} is set — VCO no longer reads that variable, because "
        f"a project shell may export ITS OWN key there and a project's key must "
        f"never be spent on VCO. "
    )
    if unreadable:
        print(
            f"VCO: could not READ VCO's own OpenAI slot "
            f"({OPENAI_SECRET_NAME}): {unreadable}. That is a read failure, "
            f"not an absence — the key may still be stored, so do NOT save a "
            f"second copy. " + shared + "Unlock the keychain / restart the "
            "launcher (vct-hub), then retry.",
            file=sys.stderr,
        )
        return
    print(
        f"VCO: no OpenAI key was resolved from VCO's own slot "
        f"({OPENAI_SECRET_NAME}). " + shared
        + f"Store the key VCO should use in VCO's slot: "
        f"`vct set --shared --key {OPENAI_SECRET_NAME}` or the launcher's "
        f"Preferences → Secrets. If you already stored one, the launcher "
        f"(vct-hub) must be running for the keychain to be readable — check "
        f"that before saving another copy.",
        file=sys.stderr,
    )


def resolve_openai_api_key(project: Optional[str] = None) -> str:
    """VCO's OpenAI key for ``project`` (``None`` → the current directory), or
    ``""`` when VCO has none configured.

    SCOPE RULE (owner ruling 2026-09-26): VCO's consumers use VCO's own slot
    and nothing else. The ONE source is the shared ``openai_api_key`` slot —
    the launcher keychain row through the hub, else
    ``~/.vct-secrets/shared/openai_api_key``. ``$OPENAI_API_KEY`` is NOT read
    (a project's key exported in that project's shell must not pay for VCO's
    embeddings), and neither is a per-project secret binding nor the project's
    own ``.env``.

    ``project`` is only the REQUESTER identity: the hub's per-(secret ×
    requester) pause matrix gates the keychain leg with it, and the
    requester's ``.no-shared-fallback`` marker gates the file-store leg. It
    never widens the lookup to that project's buckets.

    Answers once per project per process — a miss costs one bounded
    localhost request, not one per embedding call."""
    cache_key = project or ""
    if cache_key not in _resolved:
        from vco_lib import agent_secrets

        unreadable = ""
        try:
            _resolved[cache_key] = agent_secrets.get(
                OPENAI_SECRET_NAME, project=project, shared_only=True,
            ).strip()
        except agent_secrets.SecretNotFound:
            # The slot answered and holds no such key: a PROVABLE absence.
            _resolved[cache_key] = ""
        except (agent_secrets.ResolverError, OSError, ValueError) as exc:
            # KeychainLocked / HubUnreachable / Forbidden / OSError — we could
            # NOT read the slot. Never reported as "no key in the slot".
            _resolved[cache_key] = ""
            unreadable = type(exc).__name__
        if not _resolved[cache_key]:
            _warn_env_var_ignored(unreadable)
    return _resolved[cache_key]


def env_var_no_longer_read() -> str:
    """Why the pre-v0.2.98 env-var cohort owes an action, or ``""``.

    VCO used to take its OpenAI key from ``$OPENAI_API_KEY``. v0.2.98 reads
    VCO's own slot and nothing else, so a user who configured VCO that way can
    lose a working setup without being told: the variable is still exported,
    the slot is empty, and the embeddings that answered yesterday now cannot.
    This predicate is what the install-time UPDATE_DEFERRED entry
    (``openai_key_env_var_no_longer_read``) is built from — the durable
    channel the owner's "never a silent loss" requires.

    Holds when reading the variable WOULD have supplied a key VCO's slot does
    not have: ``$OPENAI_API_KEY`` is non-empty in this process's environment
    AND the shared ``openai_api_key`` slot resolves empty. Returns a one-line
    description of exactly those two facts — never a value, not even a length.

    Deliberately does NOT go through :func:`resolve_openai_api_key`: that
    answers once per project per process, and a cached miss from earlier in
    the same run would make this decision stale. It probes the same one store
    (``agent_secrets.get`` in shared-only mode) without the cache.

    Only ONE resolver outcome counts as proof, and the two that do not are
    named rather than swallowed by a broad ``except``:

    * ``SecretNotFound`` — every tier was consulted and none held the key.
      Proven: this is the claim.
    * ``AccessDenied`` — the hub says the key is not ACTIVE for this requester
      (paused in the launcher). That is the user's own choice and NOT an empty
      slot, so it makes no claim.
    * ``KeychainLocked`` — the keychain could not be read at all, so the slot
      may well hold the key. A claim here would invent a finding from a failed
      probe.

    (A hub that is DOWN arrives as ``SecretNotFound`` once the file store has
    also missed — the chain reports "consulted everything, found nothing". The
    entry is worded for that: it says the slot resolves empty, which is true,
    and it clears itself on the next update.)

    Callers gate on the embedding backend: the cohort owes this action where
    an OpenAI key is what VCO is meant to embed with. A machine on the default
    ``qwen3`` backend never read the variable for VCO's benefit, so an
    exported key there is noise, not a finding.
    """
    if not os.environ.get(OPENAI_ENV_VAR, "").strip():
        return ""
    from vco_lib import agent_secrets

    try:
        present = agent_secrets.get(OPENAI_SECRET_NAME, shared_only=True).strip()
    except agent_secrets.SecretNotFound:
        present = ""
    except (agent_secrets.ResolverError, OSError, ValueError):
        return ""
    if present:
        return ""
    return (
        f"{OPENAI_ENV_VAR} is set in this process's environment and VCO's own "
        f"slot ({OPENAI_SECRET_NAME}) resolves empty"
    )


#: The condition id of the pre-v0.2.98 migration notice (registered in
#: ``vco_lib/deferral_conditions.toml``: ``action_required``, owner
#: ``install.py``, ``clear_probe = "owned-drop-when-absent"``).
OPENAI_ENV_VAR_CID = "openai_key_env_var_no_longer_read"


def emit_env_var_deferral(
    report: Any,
    active_embedding: Optional[str],
    *,
    log_event: Optional[Callable[..., None]] = None,
) -> bool:
    """Emit :data:`OPENAI_ENV_VAR_CID` for the pre-v0.2.98 env-var cohort.

    The durable half of the owner's "never a silent loss" (2026-09-26): a
    machine whose VCO embeddings were paid for by an exported
    ``$OPENAI_API_KEY`` stops embedding the moment it updates, and this entry
    in the ledger is the only channel that reaches the user. The verdict comes
    from :func:`env_var_no_longer_read`, which proves the gap or says nothing.

    install.py calls this AFTER its step-8 ``.env`` writers, so a key the
    user passed with ``--openai-key`` in this same run is already in the
    slot and the probe cannot report a gap that this run just closed.

    ``active_embedding`` is taken as a PRIMITIVE rather than install.py's
    ``embed_config`` dict (same reason
    :func:`vco_lib.codegraph_deferrals.emit_code_backend_down` takes slot and
    model): this module then has no dependency on the installer's config
    shape. The GATE lives here, not at the call-site, because it is part of
    the condition's meaning — an OpenAI key is only VCO's problem where VCO is
    configured to embed with OpenAI. A machine on the default ``qwen3``
    backend that happens to export the variable for unrelated tools owes
    nothing and is not nagged; the runtime warning
    (:func:`_warn_env_var_ignored`) covers the moment such a machine switches
    backends.

    Data-only above the probe: the entry construction, the ``None``-guard and
    the emit soft-fail are :func:`vco_lib.deferral_report.safe_emit_entry`'s
    (v0.2.77 Part 7a convergence). Returns ``True`` when the entry landed.

    Nothing here reads the key, and nothing it writes carries a value.
    """
    if str(active_embedding or "") != "openai":
        return False
    try:
        evidence = env_var_no_longer_read()
    except Exception as exc:  # noqa: BLE001 — a diagnostic never breaks a run
        if log_event is not None:
            try:
                log_event(
                    "9/10", "warn",
                    f"could not emit {OPENAI_ENV_VAR_CID} deferral: {exc}",
                )
            except Exception:
                pass
        return False
    if not evidence:
        return False

    from vco_lib.deferral_report import safe_emit_entry

    return safe_emit_entry(
        report,
        condition_id=OPENAI_ENV_VAR_CID,
        title="VCO no longer reads OPENAI_API_KEY for its own embeddings",
        detected=evidence,
        why_deferred=(
            "VCO used to take its OpenAI key from $OPENAI_API_KEY. Since "
            "v0.2.98 it reads VCO's own shared slot and nothing else, because "
            "a project shell may export ITS OWN key under that name and a "
            "project's key must never be spent on VCO. This machine is on the "
            "OpenAI embedding backend and its slot is empty, so embeddings "
            "that used to work will not until the key VCO should use is "
            "stored in VCO's slot."
        ),
        command_to_apply=(
            "Store the key VCO should use in VCO's own slot: "
            "`vct set --shared --key openai_api_key` (value on stdin, never in "
            "argv or shell history), or in the launcher,\n"
            "Preferences → Secrets → Shared (this user)\n"
            "Nothing else needs changing, and this entry clears itself on the "
            "next update. If you deliberately paused the slot, leave it: "
            "dismiss this entry with `python -m vco_lib.project_init "
            "dismiss-deferral --condition-id " + OPENAI_ENV_VAR_CID + "`."
        ),
        log_event=log_event,
        log_step="9/10",
    )

def _store_in_keychain(value: str) -> bool:
    """``True`` when the hub stored the value in the launcher keychain's
    shared ``openai_api_key`` row; ``False`` when the hub could not be asked
    or refused the item (an older hub accepts only UPPER_CASE names)."""
    from vco_lib.install_env_secret_scope import post_secrets_to_hub

    try:
        migrated, _failed, _scope = post_secrets_to_hub(
            [{"key": OPENAI_SECRET_NAME, "value": value}], project_id=None,
        )
    except RuntimeError:
        return False
    return OPENAI_SECRET_NAME in migrated


#: The file-store CLI, shipped in the same checkout as this package.
_VCT_CLI = Path(__file__).resolve().parents[1] / "tools" / "vct-secrets" / "vct"


def _vct_cli() -> Optional[list[str]]:
    bash = shutil.which("bash")
    return [bash, str(_VCT_CLI)] if bash and _VCT_CLI.is_file() else None


def _store_in_file_store(value: str) -> None:
    """``vct set --shared --key openai_api_key`` with the value on stdin (the
    CLI refuses a value on argv, and applies its write-time guards)."""
    argv = _vct_cli()
    if argv is None:
        raise StoreFailed(
            f"the hub did not answer and the file-store CLI ({_VCT_CLI}) "
            f"cannot run here (it needs bash)"
        )
    done = subprocess.run(
        [*argv, "set", "--shared", "--key", OPENAI_SECRET_NAME],
        input=value, capture_output=True, text=True, timeout=30,
    )
    if done.returncode != 0:
        # The CLI's own messages name keys and paths only.
        raise StoreFailed(f"vct set failed: {done.stderr.strip()[:300]}")


def store_openai_api_key(value: str, *, keychain: bool = True) -> str:
    """Store ``value`` under :data:`OPENAI_SECRET_NAME`; returns where it
    landed (``"keychain"`` / ``"file_store"``). ``keychain=False`` skips the
    keychain (a migration that must not overwrite or un-pause a keychain row
    it could not read). Raises :class:`StoreFailed`."""
    value = value.strip()
    if not value:
        raise StoreFailed("empty value — nothing to store")
    _resolved.clear()
    if keychain and _store_in_keychain(value):
        return "keychain"
    _store_in_file_store(value)
    return "file_store"


def describe_store(where: str) -> str:
    """Where a stored key lives, for a user-facing line (no value)."""
    if where == "keychain":
        return (
            "the launcher keychain (shared slot `openai_api_key` — "
            "Preferences → Secrets)"
        )
    root = os.environ.get("VCT_SECRETS_DIR", "").strip() or "~/.vct-secrets"
    return f"the file store ({root}/shared/{OPENAI_SECRET_NAME})"


def _is_clean_key(value: str) -> bool:
    """A parsed ``.env`` value that is one token an API key can be."""
    return bool(value) and not any(ch.isspace() or ch in "#'\"" for ch in value)


def migrate_dotenv_openai_key(root: Path) -> dict[str, str]:
    """Move a pre-v0.2.97 VCO-written ``OPENAI_API_KEY`` line out of
    ``<root>/.env``.

    Only the line directly under :data:`LEGACY_DOTENV_HEADER` is VCO's. It
    is removed when its value EQUALS what VCO's stores hold (keychain or
    file store; constant-time compare). When nothing is stored yet, the value
    is first copied into the FILE STORE — never the keychain, whose row may
    be paused rather than empty (the hub answers both the same way) — and
    removed once the copy reads back equal. Otherwise it stays and the
    status says why.

    The value is read with the ONE line grammar (``envfile.parse_env_line``:
    ``export``, one quote pair, CRLF) — what a reader of the line gets.

    Returns ``{"status": ..., "detail": ...}`` — status ``absent`` (no such
    line), ``migrated`` (removed; detail = where the value now lives),
    ``left_differs`` (the store holds another value), ``left_unverified``
    (it could not be stored or read back), ``left_unparsed`` (the value is
    not one clean token). Never a value.
    """
    from vco_lib import agent_secrets
    from vco_lib.env_template import remove_line_under

    outcome: dict[str, str] = {"status": "absent", "detail": ""}

    def proven(value: str) -> bool:
        # The line grammar already stripped `export` and ONE quote pair. What
        # is left must be a single token: an API key never holds whitespace,
        # a `#` or a quote, so anything that does (a trailing comment, a stray
        # or mismatched quote) did not parse CLEANLY — storing it would plant
        # a broken key. Leave the line and say why (review R5 F34).
        if not _is_clean_key(value):
            outcome.update(
                status="left_unparsed",
                detail=(
                    "its value is not one clean token (a trailing comment, a stray "
                    "quote or whitespace) — move it into the store by hand"
                ),
            )
            return False
        state, stored = agent_secrets.lookup_stored(OPENAI_SECRET_NAME, project=str(root))
        if stored is None:
            try:
                store_openai_api_key(value, keychain=False)
            except (StoreFailed, OSError, subprocess.SubprocessError) as exc:
                outcome.update(status="left_unverified", detail=f"could not store it: {exc}")
                return False
            state, stored = agent_secrets.lookup_stored(OPENAI_SECRET_NAME, project=str(root))
        if stored is not None and hmac.compare_digest(
            value.encode("utf-8"), stored.strip().encode("utf-8"),
        ):
            outcome.update(status="migrated", detail=describe_store(state))
            return True
        if stored is None:
            outcome.update(status="left_unverified", detail="the stored copy could not be read back")
        else:
            outcome.update(
                status="left_differs",
                detail=f"{describe_store(state)} holds a different key",
            )
        return False

    removed = remove_line_under(root, LEGACY_DOTENV_HEADER, OPENAI_ENV_VAR, remove_if=proven)
    if removed is None:
        return {"status": "absent", "detail": ""}
    _resolved.clear()
    return outcome


def migrate_dotenv_openai_key_via(
    python_exe: Optional[Path],
    root: Path,
    *,
    say: Callable[..., None],
    note: Callable[..., None],
) -> None:
    """install.py's step-9 / ``--update`` migration entry: run
    :func:`migrate_dotenv_openai_key` as a child of ``python_exe`` and report.

    v0.2.97 install-smoke fix: install.py runs on the SYSTEM interpreter for
    its whole run (the venv is created mid-run but the process never re-execs
    into it), so migrating in-process imported ``vco_lib.agent_secrets`` →
    ``vco_lib.project_config`` → ``requests`` and every fresh install crashed
    at step 9 with ``ModuleNotFoundError``. The venv HAS the packages, so the
    step runs there — tier A, ``python -m vco_lib.openai_key
    migrate-dotenv``. The value never crosses argv, env or exception text:
    the CHILD reads ``<root>/.env``, argv carries only the root path, and the
    child's JSON verdict is status+detail by :func:`migrate_dotenv_openai_key`'s
    contract. A subprocess that cannot run is REPORTED (a left-in-place
    warning through ``note``), never a silent skip, never a crash."""
    if python_exe is None:
        result = {
            "status": "left_unverified",
            "detail": (
                f"the install venv has no python under {root} — re-run "
                "install.py to recreate it, then move the key by hand"
            ),
        }
    else:
        try:
            result = run_dotenv_migration_under(python_exe, root)
        except MigrationUnavailable as exc:
            result = {"status": "left_unverified", "detail": str(exc)}
    if result["status"] == "absent":
        return
    if result["status"] == "migrated":
        say(f"  .env: moved the OpenAI key VCO wrote there into {result['detail']}.")
        note("ok", "openai key moved out of .env", {"status": result["status"]})
        return
    say(
        f"  .env: the OpenAI key VCO wrote there was LEFT in place — "
        f"{result['detail']}. Remove the `OPENAI_API_KEY=` line under "
        "`# OpenAI (for embeddings)` once the key is stored (launcher "
        "Preferences → Secrets)."
    )
    note("warn", f".env openai key left: {result['detail']}", {"status": result["status"]})


def run_dotenv_migration_under(
    python_exe: Path, root: Path, *, timeout: float = 120.0,
) -> "dict[str, str]":
    """Run the migration as ``python -m vco_lib.openai_key migrate-dotenv``
    and return the child's ``{"status", "detail"}``.

    Raises :class:`MigrationUnavailable` when the child could not start,
    failed, or did not print one JSON verdict — the caller reports, the
    install never crashes. The key value never appears in argv (only the
    root path rides it), in the captured output that failure messages quote,
    or in the exception text."""
    argv = [
        str(python_exe), "-m", "vco_lib.openai_key",
        "migrate-dotenv", "--root", str(root),
    ]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise MigrationUnavailable(
            f"the venv migration timed out after {timeout:.0f}s"
        ) from exc
    except OSError as exc:
        raise MigrationUnavailable(f"could not run {python_exe}: {exc}") from exc
    if done.returncode != 0:
        tail = (done.stderr or "").strip().splitlines()
        reason = tail[-1][:300] if tail else f"exit code {done.returncode}"
        raise MigrationUnavailable(f"the venv migration failed: {reason}")
    stdout = (done.stdout or "").strip()
    line = stdout.splitlines()[-1] if stdout else ""
    try:
        verdict = json.loads(line)
    except ValueError as exc:
        raise MigrationUnavailable(
            "the venv migration printed no JSON verdict"
        ) from exc
    if not isinstance(verdict, dict) or not verdict.get("status"):
        raise MigrationUnavailable("the venv migration's verdict has no status")
    return {
        "status": str(verdict.get("status", "")),
        "detail": str(verdict.get("detail", "")),
    }


def _main(argv: Optional[list[str]] = None) -> int:
    """``python -m vco_lib.openai_key`` — the venv-side CLI install.py
    subprocesses to. Prints the migration's value-free JSON verdict as the
    LAST stdout line (what :func:`run_dotenv_migration_under` parses)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.openai_key",
        description="The OpenAI key's one-store tooling (install.py's venv side).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    mig = sub.add_parser(
        "migrate-dotenv",
        help="move a legacy VCO-written OPENAI_API_KEY .env line into the store",
    )
    mig.add_argument("--root", required=True, help="the install root holding the .env")
    parsed = parser.parse_args(argv)
    print(json.dumps(migrate_dotenv_openai_key(Path(parsed.root))))
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised as a real child below
    import sys

    sys.exit(_main())


# The ONE argv redactor lives with the install companions (stdlib only, so
# install.py can use it before its venv exists); re-exported here.
from vco_lib.install_companions import (  # noqa: E402
    SECRET_ARGV_FLAGS,
    redact_secret_argv,
)


__all__ = [
    "LEGACY_DOTENV_HEADER",
    "OPENAI_ENV_VAR",
    "OPENAI_SECRET_NAME",
    "MigrationUnavailable",
    "StoreFailed",
    "SECRET_ARGV_FLAGS",
    "describe_store",
    "OPENAI_ENV_VAR_CID",
    "emit_env_var_deferral",
    "env_var_no_longer_read",
    "migrate_dotenv_openai_key",
    "migrate_dotenv_openai_key_via",
    "redact_secret_argv",
    "resolve_openai_api_key",
    "run_dotenv_migration_under",
    "store_openai_api_key",
]

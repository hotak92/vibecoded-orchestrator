# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Claude accounts the model gateway can serve — the ONE home (v0.2.105).

A user can sign in to more than one Claude subscription (for example a
personal plan and a seat on a second organisation account). Each login lives
in its own Claude Code configuration directory; this module keeps the list of
those directories, chooses one per project, and tells the gateway about it.
It stores NAMES AND PATHS ONLY: no token, no email, no organisation name ever
enters a VCO file, the launcher DB, a log or a project tree.

Three stores, one writer each:

* **Truth** — the launcher DB, ``app_state`` key :data:`APP_STATE_KEY`: one
  JSON document ``{"schema", "default", "accounts", "projects"}``. Read and
  written ONLY here (Rust and the GUI call ``python -m vco_lib.gateway_accounts``,
  rule A of the cross-language policy). ``app_state`` is a plain key/value
  table, so no schema migration is needed.
* **Gateway contract** — ``<vct_root>/model-gateway/accounts.json``
  (:func:`accounts_path`), the same document minus ``projects``. Written by
  :func:`export` after every change; parsed by the gateway through
  :func:`parse_registry` (this module), so writer and reader share one parser.
* **Per project** — the project's ``.claude/settings.local.json``
  ``env.ANTHROPIC_CUSTOM_HEADERS = "x-vco-account: <id>"``. Written by the
  ordinary env projection (:mod:`vco_lib.config_projection`, a machine-local
  key) from :func:`project_header_value`; this module only re-runs that
  projection for the project it changed.

The account named ``primary`` is the login the gateway has always used
(``model_router.config.credentials_path``). It always exists, cannot be
removed, and is the account every request falls back to when the one it asked
for cannot be used (owner ruling 2026-11-03: never fail the chat, but say so
loudly — see :mod:`model_router.accounts`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

#: The reserved id of the gateway's original login.
PRIMARY_ID = "primary"
#: The request header that names the account (a project sets it through
#: ``ANTHROPIC_CUSTOM_HEADERS``). The gateway never forwards it upstream.
SELECTOR_HEADER = "x-vco-account"
#: Lower-case letters, digits and dashes, 1-32 characters, starting with a
#: letter or digit. The same grammar is enforced on the header value.
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
#: Where the truth lives in the launcher DB.
APP_STATE_KEY = "model_gateway.accounts"
SCHEMA_VERSION = 1
#: The gateway contract file, under the launcher's state root.
_STATE_SUBDIR = "model-gateway"
_EXPORT_BASENAME = "accounts.json"
ACCOUNTS_PATH_ENV = "VCT_MODEL_GATEWAY_ACCOUNTS"
#: The env key a project's settings carry the selector in.
CUSTOM_HEADERS_ENV = "ANTHROPIC_CUSTOM_HEADERS"
#: The Claude Code release that first honours ``ANTHROPIC_CUSTOM_HEADERS``.
MIN_CLAUDE_CODE_VERSION = "2.1.227"
_DEFAULT_PRIMARY_LABEL = "Personal (default login)"


class AccountsError(ValueError):
    """A request this module refuses, with a message a user can act on."""


@dataclass(frozen=True)
class Account:
    id: str
    label: str
    config_dir: str

    def credentials_file(self) -> Path:
        return Path(self.config_dir) / ".credentials.json"

    def to_json(self) -> dict:
        return {"id": self.id, "label": self.label, "config_dir": self.config_dir}


@dataclass(frozen=True)
class Registry:
    default: str = PRIMARY_ID
    primary_label: str = _DEFAULT_PRIMARY_LABEL
    accounts: tuple[Account, ...] = ()
    #: project id -> account id. Only in the launcher DB, never exported.
    projects: Mapping[str, str] = field(default_factory=dict)

    def ids(self) -> list[str]:
        return [PRIMARY_ID] + [a.id for a in self.accounts]

    def get(self, account_id: str) -> Optional[Account]:
        for acct in self.accounts:
            if acct.id == account_id:
                return acct
        return None

    def label_of(self, account_id: str) -> str:
        if account_id == PRIMARY_ID:
            return self.primary_label
        acct = self.get(account_id)
        return acct.label if acct is not None else account_id


# ── the contract: one parser, one renderer ───────────────────────────────


def valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(ID_PATTERN.match(value))


def parse_registry(doc: object) -> Registry:
    """Parse the DB document or the exported file.

    Raises :class:`AccountsError` for a document that is not an object or
    carries an unknown schema. An INDIVIDUAL bad account entry is skipped
    rather than poisoning the rest: the gateway falls back to the default
    account for any id it does not know, so a skipped entry degrades to the
    loud fallback instead of to an unreadable registry.
    """
    if not isinstance(doc, dict):
        raise AccountsError("the accounts registry is not a JSON object")
    schema = doc.get("schema", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise AccountsError(f"unknown accounts registry schema {schema!r}")
    accounts: list[Account] = []
    seen: set[str] = set()
    for raw in doc.get("accounts") or []:
        if not isinstance(raw, dict):
            continue
        acct_id = raw.get("id")
        config_dir = raw.get("config_dir")
        label = raw.get("label")
        if not valid_id(acct_id) or acct_id == PRIMARY_ID or acct_id in seen:
            continue
        if not isinstance(config_dir, str) or not os.path.isabs(config_dir):
            continue
        assert isinstance(acct_id, str)  # noqa: S101 — narrowed by valid_id
        seen.add(acct_id)
        accounts.append(Account(
            id=acct_id,
            label=label.strip() if isinstance(label, str) and label.strip() else acct_id,
            config_dir=config_dir,
        ))
    default = doc.get("default")
    if not (default == PRIMARY_ID or default in seen):
        default = PRIMARY_ID
    primary_label = doc.get("primary_label")
    projects_raw = doc.get("projects") or {}
    projects = {
        str(pid): aid for pid, aid in projects_raw.items()
        if isinstance(aid, str) and (aid == PRIMARY_ID or aid in seen)
    } if isinstance(projects_raw, dict) else {}
    return Registry(
        default=str(default),
        primary_label=(
            primary_label.strip()
            if isinstance(primary_label, str) and primary_label.strip()
            else _DEFAULT_PRIMARY_LABEL
        ),
        accounts=tuple(accounts),
        projects=projects,
    )


def render_registry(reg: Registry, *, with_projects: bool) -> dict:
    doc: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "default": reg.default,
        "primary_label": reg.primary_label,
        "accounts": [a.to_json() for a in reg.accounts],
    }
    if with_projects:
        doc["projects"] = dict(sorted(reg.projects.items()))
    return doc


def header_value(account_id: str) -> str:
    """The ``ANTHROPIC_CUSTOM_HEADERS`` value that selects ``account_id``."""
    return f"{SELECTOR_HEADER}: {account_id}"


def read_selector(raw_headers_value: Optional[str]) -> Optional[str]:
    """The account id an ``ANTHROPIC_CUSTOM_HEADERS`` value selects, if any."""
    if not raw_headers_value:
        return None
    for line in raw_headers_value.splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == SELECTOR_HEADER:
            return value.strip() or None
    return None


# ── paths ────────────────────────────────────────────────────────────────


def accounts_path() -> Path:
    custom = (os.environ.get(ACCOUNTS_PATH_ENV) or "").strip()
    if custom:
        return Path(custom)
    from vco_lib.paths import vct_root_dir
    return vct_root_dir() / _STATE_SUBDIR / _EXPORT_BASENAME


def primary_config_dir() -> Path:
    """The configuration directory of the gateway's original login."""
    from vco_lib.gateway_package import ensure_importable
    ensure_importable()
    from model_router.config import credentials_path  # pyright: ignore[reportMissingImports]
    return credentials_path().parent


def _db_path(db_path: Optional[Path]) -> Path:
    if db_path is not None:
        return db_path
    from vco_lib.paths import launcher_db_path
    return launcher_db_path()


# ── the launcher DB (truth) ──────────────────────────────────────────────


def load(db_path: Optional[Path] = None) -> Registry:
    """The registry from the launcher DB; the empty registry when absent."""
    path = _db_path(db_path)
    if not path.is_file():
        return Registry()
    from vco_lib.launcher_db_reader import sqlite_ro_uri
    conn = sqlite3.connect(sqlite_ro_uri(path), uri=True, timeout=5.0)
    try:
        return registry_from_conn(conn)
    finally:
        conn.close()


def registry_from_conn(conn: sqlite3.Connection) -> Registry:
    """The registry read through an already-open launcher DB connection
    (the env projection reads it inside its own read-only transaction)."""
    try:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (APP_STATE_KEY,),
        ).fetchone()
    except sqlite3.Error:
        return Registry()
    if row is None or not isinstance(row[0], str) or not row[0].strip():
        return Registry()
    try:
        return parse_registry(json.loads(row[0]))
    except (ValueError, AccountsError):
        return Registry()


def save(reg: Registry, db_path: Optional[Path] = None) -> None:
    """Write the registry to the launcher DB. Raises on failure — a user's
    change that did not land must not be reported as done."""
    path = _db_path(db_path)
    if not path.is_file():
        raise AccountsError(
            f"the launcher database {path} does not exist; start the launcher once first"
        )
    value = json.dumps(render_registry(reg, with_projects=True), separators=(",", ":"))
    try:
        conn = sqlite3.connect(str(path), timeout=10.0)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (APP_STATE_KEY, value, int(time.time() * 1000)),
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise AccountsError(f"could not write the launcher database {path}: {exc}") from exc


def export(reg: Registry, path: Optional[Path] = None) -> Path:
    """Write the gateway contract file atomically, owner-only."""
    target = path or accounts_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(render_registry(reg, with_projects=False), indent=2) + "\n"
    # One home for tmp+rename (vco_lib.atomic): the tempfile is created 0600,
    # so the file is never briefly world-readable; the mode is a no-op on NT.
    from vco_lib.atomic import atomic_write_text

    atomic_write_text(target, payload, mode=0o600)
    return target


# ── login state: names and expiry only ───────────────────────────────────


def login_state(config_dir: Path, *, now_ms: Optional[int] = None) -> dict:
    """``{"state", "expires_at_ms", "expires_in_s", "problem"}`` for one
    config dir, read through the gateway's own credentials reader (one
    parser). Never returns any part of a token."""
    from vco_lib.gateway_package import ensure_importable
    ensure_importable()
    from model_router.auth import OAuthReader  # pyright: ignore[reportMissingImports]
    state = OAuthReader(Path(config_dir) / ".credentials.json").read(now_ms=now_ms)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    expires_in = (
        int((state.expires_at_ms - now) / 1000) if state.expires_at_ms else None
    )
    return {
        "state": state.state,
        "expires_at_ms": state.expires_at_ms or None,
        "expires_in_s": expires_in,
        "problem": state.problem,
    }


def login_commands(config_dir: str) -> dict:
    """The commands the USER runs to sign this account in (VCO never runs an
    OAuth login itself). Printed by the GUI with a copy button."""
    return {
        "posix": f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude auth login",
        "powershell": (
            "$env:CLAUDE_CONFIG_DIR='" + config_dir.replace("'", "''") + "'; claude auth login"
        ),
        "check_posix": f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude auth status",
    }


# ── mutations ────────────────────────────────────────────────────────────


def _replace(reg: Registry, **changes: Any) -> Registry:
    values = {
        "default": reg.default,
        "primary_label": reg.primary_label,
        "accounts": reg.accounts,
        "projects": dict(reg.projects),
    }
    values.update(changes)
    return Registry(**values)


def add_account(reg: Registry, *, account_id: str, label: str, config_dir: str,
                primary_dir: Optional[Path] = None) -> Registry:
    account_id = (account_id or "").strip()
    if not valid_id(account_id):
        raise AccountsError(
            f"account id {account_id!r} is not valid: use 1-32 lower-case letters, "
            "digits or dashes, starting with a letter or digit"
        )
    if account_id == PRIMARY_ID or reg.get(account_id) is not None:
        raise AccountsError(f"an account with id {account_id!r} already exists")
    config_dir = os.path.expanduser((config_dir or "").strip())
    if not config_dir or not os.path.isabs(config_dir):
        raise AccountsError("the configuration directory must be an absolute path")
    norm = os.path.normpath(config_dir)
    taken = [os.path.normpath(a.config_dir) for a in reg.accounts]
    if primary_dir is not None:
        taken.append(os.path.normpath(str(primary_dir)))
    if norm in taken:
        raise AccountsError(
            f"{norm} is already registered (one configuration directory per account)"
        )
    label = (label or "").strip() or account_id
    return _replace(reg, accounts=reg.accounts + (Account(account_id, label, norm),))


def remove_account(reg: Registry, account_id: str) -> Registry:
    if account_id == PRIMARY_ID:
        raise AccountsError("the primary account cannot be removed")
    if reg.get(account_id) is None:
        raise AccountsError(f"no account with id {account_id!r}")
    bound = sorted(pid for pid, aid in reg.projects.items() if aid == account_id)
    if bound:
        raise AccountsError(
            f"account {account_id!r} is still selected by {len(bound)} project(s) "
            f"({', '.join(bound)}); switch them first"
        )
    if reg.default == account_id:
        raise AccountsError(
            f"account {account_id!r} is the default; make another account the default first"
        )
    return _replace(reg, accounts=tuple(a for a in reg.accounts if a.id != account_id))


def set_default(reg: Registry, account_id: str) -> Registry:
    if account_id not in reg.ids():
        raise AccountsError(f"no account with id {account_id!r}")
    return _replace(reg, default=account_id)


def set_project(reg: Registry, project_id: str, account_id: Optional[str]) -> Registry:
    """``account_id=None`` means "use the default account"."""
    projects = dict(reg.projects)
    if account_id is None:
        projects.pop(project_id, None)
    else:
        if account_id not in reg.ids():
            raise AccountsError(f"no account with id {account_id!r}")
        projects[project_id] = account_id
    return _replace(reg, projects=projects)


def project_header_value(reg: Registry, project_id: str) -> Optional[str]:
    """What the env projection writes for ``project_id``: the selector for
    an explicitly chosen account, ``None`` (key omitted, so a value VCO wrote
    earlier is removed) for "use the default" — the gateway applies the
    default to a request without the header."""
    account_id = reg.projects.get(project_id)
    return header_value(account_id) if account_id else None


# ── project-side status ──────────────────────────────────────────────────


def _settings_local_header(folder: Path) -> Optional[str]:
    """The ``ANTHROPIC_CUSTOM_HEADERS`` value the project's
    ``settings.local.json`` carries right now (JSONC-tolerant), or ``None``."""
    from vco_lib.jsonc_edit import load_object
    try:
        loaded = load_object(folder / ".claude" / "settings.local.json")
    except (OSError, ValueError):
        return None
    if loaded is None:
        return None
    env = loaded[0].get("env")
    value = env.get(CUSTOM_HEADERS_ENV) if isinstance(env, dict) else None
    return value if isinstance(value, str) else None


#: Why a project's ``settings.local.json`` does not carry its account choice
#: (``project_status`` ``drift_cause``). Only ``not_applied`` is fixed by
#: re-running the projection; the others need the user, and the GUI says how.
DRIFT_NOT_APPLIED = "not_applied"
DRIFT_LOCAL_TRACKED = "local_tracked"
DRIFT_LOCAL_UNKNOWN = "local_unknown"
DRIFT_USER_VALUE = "user_value"


def _drift_cause(folder: Path, raw: Optional[str]) -> str:
    """Why the selector is not in force (``drift`` is True). Never reads out
    a header VALUE: a user's own headers may carry anything."""
    from vco_lib import settings_local_env as sle
    try:
        state = sle.layer_state(folder)
    except Exception:  # noqa: BLE001 — unprovable layer = not usable
        state = sle.UNKNOWN
    if state == sle.TRACKED:
        return DRIFT_LOCAL_TRACKED
    if state != sle.USABLE:
        return DRIFT_LOCAL_UNKNOWN
    if raw and read_selector(raw) is None:
        # A value VCO writes is a pure selector; anything else is the user's
        # own, which the projection keeps (settings_local_user_value_kept).
        return DRIFT_USER_VALUE
    return DRIFT_NOT_APPLIED


def project_status(reg: Registry, project_id: str, folder: Optional[Path],
                   gateway_accounts: Optional[Mapping[str, Any]] = None) -> dict:
    """Which account a project's requests are billed to, and why.

    ``effective`` is the account the gateway will actually use: the
    assigned one when its login is usable, else the fallback (the default,
    else ``primary``) — the same rule the gateway applies, stated so the GUI
    can show it before any request is sent. ``drift_cause`` (``None`` when
    there is no drift) says why the file does not carry the choice, so the
    GUI offers only a remedy that helps (``DRIFT_*``).
    """
    assigned = reg.projects.get(project_id)
    selected = assigned or reg.default
    raw = _settings_local_header(folder) if folder else None
    file_says = read_selector(raw)
    drift = folder is not None and file_says != assigned
    cause = _drift_cause(folder, raw) if (drift and folder is not None) else None
    usable = _account_usable(reg, selected, gateway_accounts)
    effective = selected if usable else fallback_target(reg, selected, gateway_accounts)
    return {
        "project_id": project_id,
        "assigned": assigned,
        "selected": selected,
        "source": "project" if assigned else "default",
        "effective": effective,
        "falling_back": effective != selected,
        "file_says": file_says,
        "drift": drift,
        "drift_cause": cause,
        "selector_line": header_value(assigned) if assigned else None,
    }


def _account_usable(reg: Registry, account_id: str,
                    gateway_accounts: Optional[Mapping[str, Any]]) -> bool:
    if gateway_accounts and account_id in gateway_accounts:
        return bool(gateway_accounts[account_id].get("usable"))
    if account_id == PRIMARY_ID:
        return True
    acct = reg.get(account_id)
    if acct is None:
        return False
    return login_state(Path(acct.config_dir))["state"] == "present"


def fallback_target(reg: Registry, requested: str,
                    gateway_accounts: Optional[Mapping[str, Any]] = None) -> str:
    """The account a request for an unusable ``requested`` is served by: the
    default when it is a different, usable account, else ``primary``."""
    if reg.default != requested and reg.default != PRIMARY_ID and _account_usable(
        reg, reg.default, gateway_accounts,
    ):
        return reg.default
    return PRIMARY_ID


# ── CLI (the Rust/GUI entry point) ───────────────────────────────────────


def _gateway_view() -> dict:
    """The running gateway's per-account block from ``/health``, or a
    reason it is not available. Never raises."""
    try:
        from vco_lib.gateway_client import get_json
    except ImportError:  # pragma: no cover — shipped module
        return {"ok": False, "reason": "broken_install"}
    answer = get_json("/health", timeout_s=2.0)
    if not answer.get("ok"):
        return {"ok": False, "reason": answer.get("reason"), "message": answer.get("message")}
    payload = answer.get("payload") or {}
    block = payload.get("accounts")
    if not isinstance(block, dict):
        return {"ok": False, "reason": "outdated_gateway",
                "message": "the running gateway predates Claude accounts; restart it after the update"}
    return {"ok": True, **block}


def describe(reg: Registry, *, with_gateway: bool = True) -> dict:
    gateway = _gateway_view() if with_gateway else {"ok": False, "reason": "not_asked"}
    gw_accounts = {
        a.get("id"): a for a in (gateway.get("accounts") or []) if isinstance(a, dict)
    } if gateway.get("ok") else {}
    rows = []
    try:
        primary_dir = str(primary_config_dir())
    except ImportError:
        primary_dir = ""
    for acct_id in reg.ids():
        acct = reg.get(acct_id)
        config_dir = acct.config_dir if acct else primary_dir
        row: dict[str, Any] = {
            "id": acct_id,
            "label": reg.label_of(acct_id),
            "config_dir": config_dir,
            "is_default": acct_id == reg.default,
            "is_primary": acct_id == PRIMARY_ID,
            "projects": sorted(p for p, a in reg.projects.items() if a == acct_id),
            "login": login_state(Path(config_dir)) if config_dir else None,
            "login_commands": login_commands(config_dir) if config_dir else None,
            "gateway": gw_accounts.get(acct_id),
        }
        rows.append(row)
    return {
        "ok": True,
        "default": reg.default,
        "accounts": rows,
        "projects": dict(reg.projects),
        "gateway": {k: v for k, v in gateway.items() if k != "accounts"},
        "selector_header": SELECTOR_HEADER,
        "min_claude_code_version": MIN_CLAUDE_CODE_VERSION,
    }


def _reproject(project_id: str, db_path: Optional[Path]) -> dict:
    """Re-run the ordinary env projection for one project so its
    ``settings.local.json`` carries (or drops) the selector."""
    from vco_lib import config_projection as cp
    bundle = cp.project_env_from_db(project_id, db_path=db_path)
    written = cp.apply_project_env(bundle)
    return {"surfaces": sorted(written), "folder": str(bundle["project_root"])}


def _project_folder(project_id: str, db_path: Optional[Path]) -> Optional[Path]:
    from vco_lib import config_projection as cp
    try:
        return Path(cp.resolve_project_folder(project_id, db_path=db_path))
    except Exception:  # noqa: BLE001 — status is best effort, never fatal
        return None


def _emit(obj: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, indent=2) + "\n")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m vco_lib.gateway_accounts",
                                description="Claude accounts served by the model gateway")
    p.add_argument("--db", type=Path, default=None, help="launcher DB (tests)")
    sub = p.add_subparsers(dest="cmd", required=True)
    lst = sub.add_parser("list", help="accounts, login state and the gateway's view (JSON)")
    lst.add_argument("--no-gateway", action="store_true")
    add = sub.add_parser("add", help="register an account by its config dir")
    add.add_argument("--id", required=True)
    add.add_argument("--label", default="")
    add.add_argument("--config-dir", required=True)
    rm = sub.add_parser("remove")
    rm.add_argument("--id", required=True)
    sd = sub.add_parser("set-default")
    sd.add_argument("--id", required=True)
    sp = sub.add_parser("set-project", help="choose the account for one project")
    sp.add_argument("--project-id", required=True)
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--account")
    grp.add_argument("--default", action="store_true")
    ps = sub.add_parser("project-status")
    ps.add_argument("--project-id", required=True)
    sub.add_parser("export", help="(re)write the gateway contract file")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    db = args.db
    try:
        reg = load(db)
        if args.cmd == "list":
            _emit(describe(reg, with_gateway=not args.no_gateway))
            return 0
        if args.cmd == "export":
            _emit({"ok": True, "path": str(export(reg))})
            return 0
        if args.cmd == "project-status":
            gw = _gateway_view()
            gw_accounts = {a.get("id"): a for a in gw.get("accounts") or []} if gw.get("ok") else None
            status = project_status(reg, args.project_id, _project_folder(args.project_id, db), gw_accounts)
            status["label"] = reg.label_of(status["effective"])
            _emit({"ok": True, **status})
            return 0
        if args.cmd == "add":
            try:
                primary_dir: Optional[Path] = primary_config_dir()
            except ImportError:
                primary_dir = None
            new = add_account(reg, account_id=args.id, label=args.label,
                              config_dir=args.config_dir, primary_dir=primary_dir)
        elif args.cmd == "remove":
            new = remove_account(reg, args.id)
        elif args.cmd == "set-default":
            new = set_default(reg, args.id)
        elif args.cmd == "set-project":
            new = set_project(reg, args.project_id, None if args.default else args.account)
        else:  # pragma: no cover — argparse enforces the choices
            raise AccountsError(f"unknown command {args.cmd}")
        save(new, db)
        exported = export(new)
        out: dict[str, Any] = {"ok": True, "exported": str(exported)}
        if args.cmd == "set-project":
            # The choice is SAVED and exported at this point; a projection
            # that fails (a settings file VCO may not edit) must not read
            # back as "not saved" (review A10). It is reported beside ok.
            try:
                out["projection"] = _reproject(args.project_id, db)
            except Exception as exc:  # noqa: BLE001 — reported, never a traceback
                out["projection_error"] = f"{type(exc).__name__}: {exc}"
            folder = _project_folder(args.project_id, db)
            out["status"] = project_status(new, args.project_id, folder)
        _emit(out)
        return 0
    except AccountsError as exc:
        _emit({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

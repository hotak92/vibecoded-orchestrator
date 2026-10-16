# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The model-guidance refresh (v0.2.103, plan item 12).

WHY THIS MODULE EXISTS
----------------------
The ``{{MODEL_SELECTION_GRID}}`` and the provider-conditional sections of the
shipped CLAUDE.md templates render with only the providers whose key
resolves. Until v0.2.103 that render happened ONLY at install/update, so a
user who added a vendor key in the Secrets panel kept stale guidance until
the next release (owner request 2026-10-14: "all guidelines about which
models to use should only include models the user actually has a token/key
for … auto-update the non user editable portions of CLAUDE.md in each
project … starting from the template and materializing them from it
depending on the models availability").

This module is the refresh. It runs at LAUNCHER STARTUP and on every
Secrets-panel key change (the Rust triggers call
``python -m vco_lib.model_guidance refresh`` — rung A of the cross-language
ladder, no Rust mirror), and it re-materializes every provider-dependent
surface through the SAME code the update uses:

* the orchestrator ROOT's rendered files —
  ``vco_lib.rendered_root_files.render_all(only_marked=True)`` (the
  marker-splice of install.py's step 4c, never a full rewrite);
* every registered project's ``CLAUDE.md`` managed region —
  ``vco_lib.project_templates.render_claude_md`` (the bundle update's ONE
  render pipeline, user section untouched).

THE RULES THE OWNER SET
-----------------------
* **Probe once per run.** :func:`refresh_guidance` resolves the provider set
  once and pins it as the process-wide override
  (``model_selection.set_provider_override``), so the grid and every
  provider-conditional section of every file agree and the run costs one
  probe round.
* **Write only when the output changes.** Both render paths already avoid
  no-op writes; this module reports ``changed`` per file by comparing bytes,
  and logs ONE line per refreshed project ("model guidance refreshed for
  <name>: zai rows added") plus records a ``last_refresh`` block the GUI
  reads (``status --json``).
* **Soft-fail everywhere.** A failing project is a log line and a
  ``"failed"`` entry — the run continues and never blocks the launcher's
  startup. This module never raises out of ``refresh_guidance`` /
  ``status_payload``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from vco_lib import model_selection

__all__ = [
    "VENDOR_DISPLAY",
    "probe_vendor_states",
    "refresh_guidance",
    "status_payload",
    "main",
]

#: Display names for the per-project notice and the GUI ("zai rows added"
#: reads as "z.ai (GLM) rows added").
VENDOR_DISPLAY: Dict[str, str] = {
    "zai": "z.ai (GLM)",
    "qwen": "QwenCloud (Qwen/DeepSeek/GLM)",
    "kimi": "Kimi",
}


def _log_default(message: str) -> None:
    try:
        print(f"[vco] model-guidance: {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — a log line must never raise
        pass


def _grid_vendors() -> List[str]:
    """Every vendor the grid can render rows for, sorted (the probe set)."""
    grid = model_selection.load_grid()
    return sorted({p for row in grid.rows for p in row.providers
                   if p not in model_selection.ALWAYS_PROVIDERS})


def probe_vendor_states(*, state_probe=None) -> Dict[str, str]:
    """The LIVE tri-state per vendor: ``"present"`` / ``"absent"`` /
    ``"unavailable"`` (no retry, no record fallback — this is what the GUI
    shows as "detected now"). ``"unknown"`` when the gateway package cannot
    be imported at all. Never raises.
    """
    probe = state_probe
    if probe is None:
        try:
            probe = model_selection.vendor_key_state_probe()
        except Exception:  # noqa: BLE001 — answered as "unknown", never raised
            return {v: "unknown" for v in _grid_vendors()}
    states: Dict[str, str] = {}
    for vendor_id in _grid_vendors():
        try:
            states[vendor_id] = probe(vendor_id).value
        except Exception:  # noqa: BLE001 — one vendor, one answer
            states[vendor_id] = "unavailable"
    return states


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _refresh_diff(previous: Dict[str, bool],
                  providers) -> tuple[List[str], List[str]]:
    """``(added, removed)`` vendor ids between the recorded set and now."""
    current = {v: v in providers for v in _grid_vendors()}
    added = sorted(v for v in current if current[v] and not previous.get(v, False))
    removed = sorted(v for v in previous if previous.get(v) and not current[v])
    return added, removed


def _diff_suffix(previous: Dict[str, bool], providers) -> str:
    if not previous:
        return ""
    added, removed = _refresh_diff(previous, providers)
    parts = []
    if added:
        parts.append(", ".join(f"{VENDOR_DISPLAY.get(v, v)} rows added"
                               for v in added))
    if removed:
        parts.append(", ".join(f"{VENDOR_DISPLAY.get(v, v)} rows removed"
                               for v in removed))
    return (": " + "; ".join(parts)) if parts else ""


def refresh_guidance(
    orchestrator_root: Path,
    *,
    db_path: Optional[Path] = None,
    state_path: Optional[Path] = None,
    state_probe=None,
    sleeper=None,
    trigger: str = "manual",
    log: Optional[Callable[[str], None]] = None,
) -> dict:
    """Re-render every provider-dependent guidance surface. Never raises.

    Args:
        orchestrator_root: the install this refresh renders from (its
            ``templates/`` are the source).
        db_path: launcher.db override (tests); ``None`` = the machine's.
        state_path: provider-record override (tests).
        state_probe / sleeper: probe injection (tests).
        trigger: who asked (``startup`` / ``secrets`` / ``manual``) —
            recorded in ``last_refresh`` for the GUI.
        log: the notice sink (default: one stderr line per event).

    Returns the run summary (also the ``refresh --json`` payload).
    """
    say = log or _log_default
    root = Path(orchestrator_root)
    kwargs: dict = {"state_path": state_path}
    if state_probe is not None:
        kwargs["state_probe"] = state_probe
    if sleeper is not None:
        kwargs["sleeper"] = sleeper

    record_before = model_selection.load_record(state_path)
    previous = model_selection.recorded_providers(record_before) or {}
    providers = model_selection.reachable_providers(**kwargs)

    summary: dict = {
        "providers": sorted(providers),
        "trigger": trigger,
        "root": {"changed": [], "outcomes": []},
        "projects": [],
        "errors": [],
    }

    model_selection.set_provider_override(providers)
    try:
        # ── the orchestrator root, through the update's own renderer ──────
        from vco_lib import rendered_root_files

        try:
            entries = rendered_root_files.entries()
            before = {e.path: _read_bytes(root / Path(e.path)) for e in entries}
            outcomes = rendered_root_files.render_all(
                root, db_path=db_path, only_marked=True)
            for entry in entries:
                after = _read_bytes(root / Path(entry.path))
                if after != before[entry.path] and after is not None:
                    summary["root"]["changed"].append(entry.path)
            summary["root"]["outcomes"] = [
                {"path": o.path, "status": o.status} for o in outcomes
            ]
            for o in outcomes:
                if o.status == "failed":
                    say(f"root render failed for {o.path}: {o.detail}")
                    summary["errors"].append(f"{o.path}: {o.detail}")
        except Exception as exc:  # noqa: BLE001 — one leg's failure stops no other
            say(f"root refresh failed: {type(exc).__name__}: {exc}")
            summary["errors"].append(f"root: {type(exc).__name__}: {exc}")

        # ── every registered project, through the bundle's own pipeline ────
        from vco_lib import project_init as _pi
        from vco_lib.project_move import registered_projects
        from vco_lib.project_templates import render_claude_md

        suffix = _diff_suffix(previous, providers)
        try:
            root_resolved = root.resolve()
        except OSError:
            root_resolved = root
        for row in registered_projects(db_path):
            name = str(row.get("name") or row["folder_path"])
            folder = Path(str(row["folder_path"]))
            try:
                if folder.resolve() == root_resolved:
                    continue  # the root rendered above
            except OSError:
                pass
            claude_md = folder / "CLAUDE.md"
            if not folder.is_dir() or not claude_md.is_file():
                continue
            head = _read_bytes(claude_md)
            if head is None or _pi.MANAGED_REGION_OPEN.encode("utf-8") not in head:
                # No managed region: creating or adopting one is the install
                # path's job, never a refresh's.
                continue
            try:
                render_claude_md(
                    folder, orchestrator_root=root, project_name=name,
                    project_id=str(row.get("id") or "") or None,
                    db_path=db_path)
            except Exception as exc:  # noqa: BLE001 — soft-fail, next project
                say(f"refresh failed for {name}: {type(exc).__name__}: {exc}")
                summary["projects"].append({
                    "project": name, "folder": str(folder),
                    "changed": False, "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                summary["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            after = _read_bytes(claude_md)
            changed = after is not None and after != head
            summary["projects"].append({
                "project": name, "folder": str(folder),
                "changed": changed, "status": "refreshed" if changed else "unchanged",
            })
            if changed:
                say(f"model guidance refreshed for {name}{suffix}")
    finally:
        model_selection.set_provider_override(None)

    # ── the last-refresh record the GUI reads ──────────────────────────────
    added, removed = _refresh_diff(previous, providers)
    changed_count = sum(1 for p in summary["projects"] if p["changed"])
    record = model_selection.load_record(state_path)
    record.update({
        "version": model_selection.RECORD_VERSION,
        "last_refresh": {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "trigger": trigger,
            "added": added,
            "removed": removed,
            "projects_changed": changed_count,
            "root_changed": bool(summary["root"]["changed"]),
        },
    })
    # `providers` is written ONLY by a definitive probe round, inside
    # `reachable_providers` — never from here: a round with an unavailable
    # vendor is not a truth about the machine and must not become the
    # last-known record.
    model_selection.save_record(record, state_path)
    summary["last_refresh"] = record["last_refresh"]
    if changed_count or summary["root"]["changed"]:
        say(f"refresh done ({trigger}): {changed_count} project(s) and "
            f"{len(summary['root']['changed'])} root file(s) re-rendered{suffix}")
    else:
        say(f"refresh done ({trigger}): guidance already current")
    return summary


def status_payload(*, db_path: Optional[Path] = None,
                   state_path: Optional[Path] = None,
                   state_probe=None) -> dict:
    """What the launcher GUI shows: live detection, last-known record, and
    the last refresh. Never raises."""
    record = model_selection.load_record(state_path)
    known = model_selection.recorded_providers(record)
    live = probe_vendor_states(state_probe=state_probe)
    vendors = {
        v: {
            "state": live[v],
            "last_known": (known.get(v) if known is not None else None),
            "display": VENDOR_DISPLAY.get(v, v),
        }
        for v in _grid_vendors()
    }
    try:
        from vco_lib.project_move import registered_projects

        project_count = len(registered_projects(db_path))
    except Exception:  # noqa: BLE001 — the count is informational
        project_count = None
    return {
        "vendors": vendors,
        "last_refresh": record.get("last_refresh"),
        "recorded_at": record.get("recorded_at"),
        "registered_projects": project_count,
    }


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m vco_lib.model_guidance refresh|status [--json] …``"""
    parser = argparse.ArgumentParser(prog="vco_lib.model_guidance")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("refresh", "status"):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true",
                       help="print the payload as one JSON line")
        p.add_argument("--orchestrator-root", default=None,
                       help="install root to render from (default: the cwd)")
        p.add_argument("--db-path", default=None,
                       help="launcher.db override")
        p.add_argument("--trigger", default="manual",
                       help="who asked (startup / secrets / manual)")
    args = parser.parse_args(argv)
    root = Path(args.orchestrator_root).resolve() if args.orchestrator_root \
        else Path.cwd()
    db_path = Path(args.db_path) if args.db_path else None

    if args.command == "refresh":
        if not (root / "templates" / "CLAUDE.md.template").is_file():
            print(f"[vco] model-guidance: {root} is not an orchestrator "
                  "install root (templates/CLAUDE.md.template missing) — "
                  "nothing to refresh", file=sys.stderr)
            return 1
        payload = refresh_guidance(root, db_path=db_path,
                                   trigger=args.trigger)
    else:
        payload = status_payload(db_path=db_path)
    if args.json:
        print(json.dumps(payload))
    else:
        if args.command == "refresh":
            changed = [p["project"] for p in payload["projects"] if p["changed"]]
            print(f"providers: {', '.join(payload['providers'])}")
            print(f"refreshed projects: {len(changed)} "
                  f"({', '.join(changed) if changed else 'none'})")
            print(f"root files changed: "
                  f"{', '.join(payload['root']['changed']) or 'none'}")
            for error in payload["errors"]:
                print(f"ERROR: {error}", file=sys.stderr)
        else:
            for vendor_id, info in payload["vendors"].items():
                print(f"{info['display']}: {info['state']} "
                      f"(last known: {info['last_known']})")
            refresh = payload["last_refresh"] or {}
            print(f"last refresh: {refresh.get('at', 'never')}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

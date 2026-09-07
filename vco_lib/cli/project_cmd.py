# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco project`` — the project-lifecycle subcommand family (v0.2.92 W3+W14).

TWO verbs: ``move`` (change the folder — W3) and ``rename-collections``
(change the name AND carry the Weaviate collection family to the class names
it derives — W14). The family exists because both change what a registered
project IS rather than what it contains, and those belong under a common noun
rather than as loose top-level verbs.

Both verbs share ONE sequence shape, for one reason: the safety property IS
the ordering. Claim before copying, commit in a single transaction, and only
report success once the post-commit reconciliation actually ran — so an
interrupted operation is VISIBLE rather than indistinguishable from a
completed one.

WHY THE CLI ORCHESTRATES AND THE ENGINE DOES NOT
------------------------------------------------
:mod:`vco_lib.project_move` owns the FILE phases and knows nothing about how
the database gets written. This module owns the sequence, because the sequence
is where the safety property lives:

    hub: begin  →  engine: pre-flip  →  hub: commit  →  engine: post-flip
                                                      →  hub: finish

``begin`` claims single-flight BEFORE a byte is copied. ``commit`` is one SQL
transaction — flip, re-point, enqueue — so there is no window in which the
row moved but its dependent rows did not. ``finish`` only marks the move
complete once the post-commit reconciliation actually ran, which is what makes
an interrupted move visible instead of silently half-done.

The launcher's ``change_project_path_v2`` drives the SAME four steps against
its own ``Db`` handle. Two surfaces, one order, one engine.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

__all__ = ["add_subparsers", "cmd_move", "cmd_rename_collections"]


# ───────────────────────────────────────────────────────────────────────────
# Hub client
# ───────────────────────────────────────────────────────────────────────────


class HubError(RuntimeError):
    """The hub could not be reached, or refused the request."""


def _hub_post(path: str, payload: Mapping[str, Any]) -> dict:
    """POST ``payload`` to the hub and return the parsed body.

    Discovery and authentication reuse :mod:`vco_lib.project_config`'s
    resolver internals rather than re-deriving them. That module already owns
    the ``$VCT_HUB_PORT`` → ``<vct_root>/hub.port`` → default ladder and the
    ``hub.token`` read, including the stale-``$VCT_HUB_TOKEN`` retry; adding a
    fourth discovery path here is precisely the duplication the house rules
    forbid, and the one that would drift first.
    """
    try:
        from vco_lib.project_config import (  # noqa: PLC0415
            _discover_hub,
            _http_session,
            _on_disk_hub_token,
        )
    except Exception as exc:  # noqa: BLE001
        raise HubError(f"resolver unavailable: {exc}") from exc

    try:
        port, token = _discover_hub()
    except Exception as exc:  # noqa: BLE001
        raise HubError(f"hub not discoverable: {exc}") from exc
    if not token:
        token = _on_disk_hub_token()
    if not token:
        raise HubError("no hub token on disk")

    url = f"http://127.0.0.1:{port}/api/v1{path}"
    try:
        resp = _http_session().post(
            url,
            json=dict(payload),
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        raise HubError(f"POST {path} failed: {exc}") from exc
    if resp.status_code >= 400:
        raise HubError(
            f"hub refused {path} ({resp.status_code}): {resp.text[:400]}"
        )
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _hub_alive() -> bool:
    """Liveness probe for the preflight refusal.

    Asked BEFORE anything is copied. ``/health`` is the unauthenticated
    liveness route, so a token problem does not masquerade as a dead hub.
    """
    try:
        from vco_lib.project_config import _discover_hub, _http_session

        try:
            port, _ = _discover_hub()
        except Exception:  # noqa: BLE001 — no token yet is still a live port
            from vco_lib.project_config import DEFAULT_HUB_PORT

            port = DEFAULT_HUB_PORT
        resp = _http_session().get(
            f"http://127.0.0.1:{port}/api/v1/health", timeout=5
        )
        return resp.status_code < 500
    except Exception:  # noqa: BLE001
        return False


# ───────────────────────────────────────────────────────────────────────────
# `vco project move`
# ───────────────────────────────────────────────────────────────────────────


def cmd_move(args: argparse.Namespace) -> int:
    from vco_lib import project_move as pm

    emit_json = bool(getattr(args, "json", False))

    def _out(payload: Mapping[str, Any], human: str = "") -> None:
        if emit_json:
            print(json.dumps(dict(payload), indent=2))
        elif human:
            print(human)

    # ── verify mode ──────────────────────────────────────────────────────
    if args.verify:
        folder = Path(args.folder or ".").resolve()
        report = pm.verify_move(folder, old_path=args.old_path)
        if not report.get("old_path"):
            _out(
                {"ok": False, "error": "no_previous_path"},
                "Nothing to verify: no move sentinel here and no --old-path given.",
            )
            return 2
        clean = not report["stale_file_hits"] and not [
            h
            for h in report["stale_db_hits"]
            if h["policy"] in _pbk_actionable()
        ]
        _out(
            {"ok": True, "verify": report},
            _render_verify(report, clean),
        )
        return 0 if clean else 1

    # ── plan (the ONE planner; the launcher drives it via --phase plan) ──
    try:
        plan = pm.plan_for_selector(
            args.project,
            args.to,
            into_existing=args.into_existing,
            from_missing=args.from_missing,
        )
    except pm.MoveRefused as exc:
        _out(
            {"ok": False, "refused": exc.reason, "error": str(exc)},
            f"Refused ({exc.reason}): {exc}",
        )
        return 3

    if args.dry_run:
        _out({"ok": True, "dry_run": True, "plan": plan.to_json()}, _render_plan(plan))
        return 0

    # The writer must be reachable BEFORE anything is copied. Refusing here
    # costs the user a message; discovering it after Phase 2 would leave a
    # destination full of files for a move that cannot commit.
    if not _hub_alive():
        _out(
            {"ok": False, "refused": "writer_unavailable"},
            "Refused (writer_unavailable): "
            + pm.REFUSAL_REASONS["writer_unavailable"],
        )
        return 3

    move_id = str(uuid.uuid4())
    try:
        begin = _hub_post(
            f"/cli/projects/{plan.project_id}/move",
            {"action": "begin", "move_id": move_id, "new_path": plan.dst},
        )
    except HubError as exc:
        _out({"ok": False, "refused": "writer_unavailable", "error": str(exc)},
             f"Refused: {exc}")
        return 3
    if begin.get("refused"):
        reason = str(begin["refused"])
        _out(
            {"ok": False, "refused": reason, "error": begin.get("error", "")},
            f"Refused ({reason}): {begin.get('error') or pm.REFUSAL_REASONS.get(reason, '')}",
        )
        return 3
    move_id = str(begin.get("move_id") or move_id)

    # ── pre-flip (nothing committed yet; any failure is a clean refusal) ──
    try:
        pre = pm.execute_pre_flip(plan, move_id=move_id, safe_add=args.safe_add)
    except Exception as exc:  # noqa: BLE001
        _abort(plan.project_id, move_id, f"{type(exc).__name__}: {exc}")
        _out(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}", "phase": "pre-flip"},
            f"Move aborted before any database change: {exc}\n"
            f"The project is unchanged and still registered at {plan.src}.\n"
            f"Files already copied to {plan.dst} were only ADDED — nothing "
            f"there was overwritten.",
        )
        return 1

    # ── commit (one transaction) ─────────────────────────────────────────
    try:
        commit = _hub_post(
            f"/cli/projects/{plan.project_id}/move",
            {"action": "commit", "move_id": move_id, "new_path": plan.dst},
        )
    except HubError as exc:
        _abort(plan.project_id, move_id, str(exc))
        _out(
            {"ok": False, "error": str(exc), "phase": "commit"},
            f"The database change did not happen: {exc}\n"
            f"The project is unchanged and still registered at {plan.src}.",
        )
        return 1
    if commit.get("refused"):
        reason = str(commit["refused"])
        _abort(plan.project_id, move_id, reason)
        _out(
            {"ok": False, "refused": reason, "error": commit.get("error", "")},
            f"Refused at commit ({reason}): {commit.get('error', '')}\n"
            f"The project is unchanged and still registered at {plan.src}.",
        )
        return 3

    # ── post-flip (the flip is durable; this is reconciliation) ──────────
    post = None
    try:
        post = pm.execute_post_flip(
            plan,
            move_id=move_id,
            codegraph_enqueued=bool(commit.get("codegraph_enqueued", False)),
        )
    except Exception as exc:  # noqa: BLE001
        _out(
            {
                "ok": False,
                "phase": "post-flip",
                "error": f"{type(exc).__name__}: {exc}",
                "committed": True,
            },
            f"The project now lives at {plan.dst} and the database is "
            f"consistent, but the follow-up reconciliation did not finish: "
            f"{exc}\nRe-run it with:  "
            f"vco project move --verify --folder '{plan.dst}'",
        )
        return 1

    try:
        _hub_post(
            f"/cli/projects/{plan.project_id}/move",
            {"action": "finish", "move_id": move_id, "new_path": plan.dst},
        )
    except HubError as exc:
        post.warnings.append(f"could not mark the move complete: {exc}")

    _out(
        {
            "ok": True,
            "plan": plan.to_json(),
            "pre_flip": pre.to_json(),
            "post_flip": post.to_json(),
            "commit": commit,
        },
        _render_summary(plan, pre, post),
    )
    return 0


def _abort(project_id: str, move_id: str, reason: str) -> None:
    """Best-effort release of the single-flight claim."""
    try:
        _hub_post(
            f"/cli/projects/{project_id}/move",
            {"action": "abort", "move_id": move_id, "reason": reason[:400]},
        )
    except HubError:
        pass


def _pbk_actionable() -> frozenset:
    from vco_lib.path_bearing_keys import ACTIONABLE_POLICIES

    return ACTIONABLE_POLICIES


# ───────────────────────────────────────────────────────────────────────────
# Rendering
# ───────────────────────────────────────────────────────────────────────────


def _render_plan(plan: Any) -> str:
    j = plan.to_json()
    c = j["counts"]
    lines = [
        f"Move preview: {j['project_name'] or j['project_id']}",
        f"  from: {j['src']}",
        f"    to: {j['dst']}",
        "",
        f"  {c['to_copy']} file(s) copied "
        f"({c['user_modified']} user-modified, {c['user_adjacent']} VCO-adjacent)",
        f"  {c['bundle_clean']} unmodified bundle file(s) re-created at the "
        f"destination instead of copied",
        f"  {c['conflicts_identical']} already identical there (nothing to do)",
        f"  {c['conflicts_divergent']} differ there — those land beside the "
        f"existing file as .vco-moved siblings; NOTHING is overwritten",
    ]
    if j["extra_codegraph_paths_under_src"]:
        lines.append(
            f"  {len(j['extra_codegraph_paths_under_src'])} extra code-graph "
            f"path(s) point inside the old folder and are left alone"
        )
    lines += [
        "",
        "  The old folder is KEPT. Nothing is deleted, there or here.",
        "  Not copied (stays in the old folder): "
        + ", ".join(j["stays_in_old_folder"]),
    ]
    lines += [f"  ! {w}" for w in j["warnings"]]
    return "\n".join(lines)


def _render_summary(plan: Any, pre: Any, post: Any) -> str:
    lines = [
        f"Moved: {plan.project_name or plan.project_id}",
        f"  from: {plan.src}",
        f"    to: {plan.dst}",
        "",
        f"  copied           {len(pre.copied)}",
        f"  already identical {len(pre.skipped_identical)}",
        f"  .vco-moved siblings {len(pre.siblings)}",
    ]
    if pre.git_exclude.get("action") == "appended":
        lines.append(
            f"  .git/info/exclude entries added {len(pre.git_exclude.get('added', []))}"
        )
    if post.stale_file_hits:
        lines.append(f"  ! files still naming the old path: {', '.join(post.stale_file_hits)}")
    actionable = [h for h in post.stale_db_hits if not h.get("expected")]
    if actionable:
        lines.append(
            "  ! database rows still naming the old path: "
            + ", ".join(f"{h['table']}.{h['column']}({h['rows']})" for h in actionable)
        )
    else:
        lines.append("  database sweep: no actionable leftovers")
    if post.harness.get("old_present"):
        lines += [
            "",
            "  Claude Code session state does NOT follow a move.",
            "  Copy your project memory across with:",
            # Indent EVERY line: the command became multi-line in v0.2.92
            # (`a && b` is a PowerShell 5.1 syntax error, so the POSIX
            # rendering is two steps), and a single f-string prefix would
            # indent only the first, leaving the second flush-left and
            # reading like prose rather than a command to paste.
            *(
                f"    {line}"
                for line in str(
                    post.harness["memory_copy_command"]
                ).splitlines()
            ),
        ]
    if post.deferrals:
        lines += [
            "",
            f"  {len(post.deferrals)} ledger entr(ies) written at the new "
            f"folder for your project's Claude to pick up.",
        ]
    lines += ["", f"  The old folder is still at {plan.src}. Nothing was deleted."]
    lines += [f"  ! {w}" for w in (list(pre.warnings) + list(post.warnings))]
    return "\n".join(lines)


def _render_verify(report: Mapping[str, Any], clean: bool) -> str:
    lines = [f"Verify: {report['folder']} (previous folder {report['old_path']})"]
    if report["stale_file_hits"]:
        lines.append("  files still naming the old path: " + ", ".join(report["stale_file_hits"]))
    else:
        lines.append("  managed files: clean")
    actionable = [
        h for h in report["stale_db_hits"] if h["policy"] in _pbk_actionable()
    ]
    if actionable:
        lines.append(
            "  database rows still naming the old path: "
            + ", ".join(f"{h['table']}.{h['column']}({h['rows']})" for h in actionable)
        )
    else:
        lines.append("  database sweep: clean")
    if report.get("resolved"):
        lines.append("  cleared ledger entries: " + ", ".join(report["resolved"]))
    if not clean:
        lines.append("  Some references remain — see the ledger entries above.")
    return "\n".join(lines)


# ───────────────────────────────────────────────────────────────────────────
# Parser registration
# ───────────────────────────────────────────────────────────────────────────


def add_subparsers(sub: argparse._SubParsersAction) -> None:
    """Mount ``vco project`` onto the top-level parser."""
    project = sub.add_parser(
        "project",
        help="Project lifecycle operations (move, rename-collections).",
        description=(
            "Operations that change what a registered project IS — where it "
            "lives, what it is called — as opposed to what it contains."
        ),
    )
    verbs = project.add_subparsers(dest="project_command", required=True)
    _add_rename_collections(verbs)

    move = verbs.add_parser(
        "move",
        help="Change a registered project's folder.",
        description=(
            "Re-point a registered project at a new folder and re-materialize "
            "VCO's files there. NOTHING at the destination is overwritten and "
            "the old folder is never deleted. Run with --dry-run first: the "
            "preview lists every file that would be copied and every conflict."
        ),
    )
    move.add_argument(
        "project",
        nargs="?",
        default="",
        help="Project id or slug (omit with --verify).",
    )
    move.add_argument("--to", help="Absolute path of the new folder.")
    move.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and change nothing.",
    )
    move.add_argument("--json", action="store_true", help="Machine-readable output.")
    move.add_argument(
        "--into-existing",
        action="store_true",
        help=(
            "Allow a destination that already has files in it. Existing files "
            "are still never overwritten."
        ),
    )
    move.add_argument(
        "--from-missing",
        action="store_true",
        help=(
            "Allow the move when the currently-registered folder no longer "
            "exists on disk (re-points the registration only)."
        ),
    )
    move.add_argument(
        "--safe-add",
        action="store_true",
        help="Run the destination's bundle step in safe-add mode.",
    )
    move.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Re-run a completed move's verification at --folder, clearing the "
            "ledger entries that no longer apply."
        ),
    )
    move.add_argument("--folder", help="With --verify: the project folder.")
    move.add_argument(
        "--old-path",
        help="With --verify: the previous folder, when no sentinel remains.",
    )
    move.set_defaults(func=_dispatch_move)


def _dispatch_move(args: argparse.Namespace) -> int:
    if args.verify:
        return cmd_move(args)
    if not args.project or not args.to:
        print(
            "vco project move: PROJECT and --to are required "
            "(or use --verify --folder PATH).",
            file=sys.stderr,
        )
        return 2
    return cmd_move(args)


# ───────────────────────────────────────────────────────────────────────────
# `vco project rename-collections` (v0.2.92 WP-18 / W14)
# ───────────────────────────────────────────────────────────────────────────
#
# SAME four-step shape as `move`, for the same reason: the safety property is
# the ordering, and the ordering has to be observable from outside.
#
#   claim (sentinel, O_EXCL)  →  copy  →  verify  →  hub: flip  →  reconcile
#   └──── nothing committed; every failure is a clean refusal ────┘
#
# The one structural difference from `move`: there is no `begin` hub action.
# `move`'s single-flight claim must live in launcher.db because a move races
# the GUI's own move button over `projects.folder_path`. A rename's claim can
# live in the project folder, because the operation is gated on state that IS
# in the folder (the sentinel records which destination classes VCO created —
# the fact that makes a resume safe rather than a collision), and adding a
# second ledger table would duplicate a state machine rather than reuse one.
# The trade is stated in `collection_rename.claim_sentinel`: an O_EXCL claim
# is atomic between processes but does not survive a killed one, which is what
# the staleness window and the takeover branch are for.


def cmd_rename_collections(args: argparse.Namespace) -> int:
    from vco_lib import collection_rename as cr

    emit_json = bool(getattr(args, "json", False))

    def _out(payload: Mapping[str, Any], human: str = "") -> None:
        if emit_json:
            print(json.dumps(dict(payload), indent=2))
        elif human:
            print(human)

    # ── status / drop-retired: folder-scoped, no project selector needed ──
    if args.status:
        folder = Path(args.folder or ".").resolve()
        report = cr.rename_status(folder)
        _out({"ok": True, "status": report}, report["summary"])
        return 0

    if args.drop_retired:
        folder = Path(args.folder or ".").resolve()
        report = cr.drop_retired(folder, confirm=args.confirm)
        _out({"ok": report["ok"], "drop": report}, _render_drop(report))
        if report["errors"]:
            return 1
        # A pure refusal (nothing dropped, nothing attempted) is exit 2 — the
        # same code every other guarded destructive command in this repo uses
        # when it declines to act.
        return 0 if report["dropped"] else 2

    # ── plan (the ONE planner; --dry-run and the live run share it) ───────
    try:
        plan = cr.plan_rename(args.project, args.to)
    except cr.RenameRefused as exc:
        _out({"ok": False, "refused": exc.reason, "error": str(exc)},
             f"Refused ({exc.reason}): {exc}")
        return 3

    if args.dry_run:
        _out({"ok": True, "dry_run": True, "plan": plan.to_json()},
             _render_rename_plan(plan))
        return 0

    # The writer must be reachable BEFORE a byte is copied. Discovering it
    # after the copy would leave a populated destination for a rename that
    # cannot commit — files at a destination are recoverable, a half-flipped
    # identity is what this package exists to prevent.
    if not _hub_alive():
        _out({"ok": False, "refused": "writer_unavailable"},
             "Refused (writer_unavailable): "
             + cr.REFUSAL_REASONS["writer_unavailable"])
        return 3

    resuming = plan.resuming_from is not None
    if not resuming:
        try:
            cr.claim_sentinel(plan.folder, {
                "schema": "vco.collection_rename.v1",
                "project_id": plan.project_id,
                "project_name": plan.project_name,
                "new_name": plan.new_name,
                "phase": cr.PHASE_COPYING,
                "created_classes": [],
                "started_at": time.time(),
            })
        except cr.RenameRefused as exc:
            _out({"ok": False, "refused": exc.reason, "error": str(exc)},
                 f"Refused ({exc.reason}): {exc}")
            return 3

    say = (lambda m: None) if emit_json else (lambda m: print(f"  {m}"))

    # ── copy + verify (pre-flip) ─────────────────────────────────────────
    try:
        copy_report = cr.execute_copy(plan, progress=say)
        verify_report = cr.verify_copy(plan)
    except cr.RenameRefused as exc:
        _out({"ok": False, "refused": exc.reason, "error": str(exc),
              "phase": "verify"},
             f"Refused ({exc.reason}): {exc}\n"
             f"The project is unchanged and still uses "
             f"{plan.moves[0].src if plan.moves else 'its previous collections'}"
             f". Nothing was deleted. Re-run the same command to resume.")
        return 3
    except Exception as exc:  # noqa: BLE001
        _out({"ok": False, "error": f"{type(exc).__name__}: {exc}",
              "phase": "copy"},
             f"The copy did not finish: {exc}\n"
             f"NOTHING was committed — the project still uses its previous "
             f"collections and still works. Re-run the same command to "
             f"resume; the copy preserves UUIDs, so it re-writes rather than "
             f"duplicates.")
        return 1

    # ── flip: ONE transaction, the durable commit point ──────────────────
    try:
        flip = _hub_post(
            f"/cli/projects/{plan.project_id}/rename-collections",
            {"new_name": plan.new_name,
             "kg_bindings": plan.kg_flip_payload(),
             "codegraph_prefix": plan.new_code_prefix})
    except HubError as exc:
        _out({"ok": False, "error": str(exc), "phase": "flip"},
             f"The database change did not happen: {exc}\n"
             f"The project is unchanged and still uses its previous "
             f"collections. The copied classes were left in place; re-run to "
             f"resume, or run --status to see what exists.")
        return 1
    if flip.get("refused"):
        reason = str(flip["refused"])
        _out({"ok": False, "refused": reason, "error": flip.get("error", "")},
             f"Refused at the flip ({reason}): {flip.get('error', '')}\n"
             f"The project is unchanged.")
        return 3

    _mark_flipped(cr, plan)
    cr.write_completed_record(plan)

    # ── reconcile (the flip is durable; this is idempotent follow-up) ─────
    reconcile = cr.execute_reconcile(plan)
    _out({"ok": True, "plan": plan.to_json(), "copy": copy_report,
          "verify": verify_report, "flip": flip, "reconcile": reconcile},
         _render_rename_summary(plan, verify_report, reconcile))
    return 0


def _mark_flipped(cr: Any, plan: Any) -> None:
    """Record the durable commit in the sentinel BEFORE reconciling.

    This is the ordering that makes an interrupted rename honest: a crash
    during reconciliation leaves `phase=flipped`, which `--status` reports as
    "committed, follow-up owed" rather than as a failure. Writing it after the
    reconciliation would make that crash indistinguishable from a copy that
    never committed at all.
    """
    s = cr.read_sentinel(plan.folder) or {}
    s["phase"] = cr.PHASE_FLIPPED
    s["retired_classes"] = plan.retired_classes
    s["replacements"] = plan.replacements
    cr.write_sentinel(plan.folder, s)


def _render_rename_plan(plan: Any) -> str:
    j = plan.to_json()
    lines = [
        f"Rename preview: {j['project_name']} -> {j['new_name']}",
        f"  folder: {j['folder']}",
        "",
    ]
    for m in j["moves"]:
        if m["action"] == "noop":
            continue
        n = m["src_count"]
        detail = {
            "copy": f"{n} object(s) copied WITH vectors",
            "create-empty": "source is empty; destination created empty",
            "source-absent": "source class does not exist — no data carried",
            "resume": f"resuming an interrupted copy ({m['dst_count']}/{n} there)",
        }.get(m["action"], m["action"])
        lines.append(f"  {m['src']}  ->  {m['dst']}   [{detail}]")
    lines += [
        "",
        f"  {j['carried_objects']} object(s) carried in total. Vectors are "
        f"copied verbatim — nothing is re-embedded.",
        "  The previous classes are KEPT. This command never drops a "
        "collection.",
    ]
    lines += [f"  ! {w}" for w in j["warnings"]]
    return "\n".join(lines)


def _render_rename_summary(plan: Any, verify: Mapping[str, Any],
                           reconcile: Mapping[str, Any]) -> str:
    from vco_lib.collection_rename import drop_retired_command

    lines = [
        f"Renamed collections: {plan.project_name} -> {plan.new_name}",
        f"  {len(verify.get('classes', {}))} class(es) verified equal "
        f"(counts + {verify.get('vectors_sampled', 0)} sampled vectors)",
        f"  bindings flipped to {plan.new_code_prefix}* and the new KG family",
    ]
    remint = reconcile.get("remint") or {}
    if remint:
        lines.append(
            f"  code-graph identity re-mint: {remint.get('line', '')}")
    for w in reconcile.get("warnings") or []:
        lines.append(f"  ! {w}")
    lines += [
        "",
        "  The previous classes were NOT dropped:",
        "    " + ", ".join(plan.retired_classes),
        "  When you are satisfied, retire them with:",
        "    " + drop_retired_command(plan.folder),
    ]
    return "\n".join(lines)


def _render_drop(report: Mapping[str, Any]) -> str:
    lines = []
    for name in report.get("dropped", []):
        lines.append(f"dropped: {name}")
    for reason in report.get("refused", []):
        lines.append(f"REFUSED  {reason}")
    for err in report.get("errors", []):
        lines.append(f"  ERROR {err['collection']}: {err['error']}")
    if not lines:
        lines.append("nothing to do")
    return "\n".join(lines)


def _add_rename_collections(verbs: argparse._SubParsersAction) -> None:
    """Mount ``vco project rename-collections``.

    Four modes on ONE verb rather than four verbs, because they are phases of
    one operation and the user who needs ``--status`` or ``--drop-retired`` got
    there from ``rename-collections``. A separate ``vco project drop-retired``
    would also be a second place to keep the guard correct.
    """
    p = verbs.add_parser(
        "rename-collections",
        help="Rename a project AND carry its Weaviate collections.",
        description=(
            "Give a registered project a new name and carry its collection "
            "family (KG + Development + Diagrams + the 5 code-graph classes) "
            "to the class names that name derives. Objects are copied WITH "
            "their vectors — nothing is re-embedded — and the previous "
            "classes are never dropped. Run with --dry-run first: the preview "
            "lists every class, its object count and what would happen to it."
        ),
    )
    p.add_argument("project", nargs="?", default="",
                   help="Project id or slug (omit with --status/--drop-retired).")
    p.add_argument("--to", dest="to", default="",
                   help="The project's new name.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan and change nothing.")
    p.add_argument("--json", action="store_true",
                   help="Machine-readable output.")
    p.add_argument("--resume", action="store_true",
                   help=("Continue an interrupted rename at --folder. Safe to "
                         "repeat: the copy preserves UUIDs."))
    p.add_argument("--status", action="store_true",
                   help="Report what an interrupted rename left at --folder.")
    p.add_argument("--drop-retired", action="store_true",
                   help=("Drop the classes a completed rename retired. "
                         "Re-validates at run time and refuses unless the old "
                         "class is bound nowhere and its replacement still "
                         "holds the data. Requires --confirm."))
    p.add_argument("--confirm", action="store_true",
                   help="Required by --drop-retired. Nothing is deleted "
                        "without it.")
    p.add_argument("--folder", default="",
                   help="Project folder for --status / --drop-retired / "
                        "--resume (default: the current directory).")
    p.set_defaults(func=_dispatch_rename_collections)


def _dispatch_rename_collections(args: argparse.Namespace) -> int:
    if args.status or args.drop_retired:
        return cmd_rename_collections(args)
    if args.resume:
        # A resume re-derives the target from the sentinel, so the user does
        # not have to retype a name they already committed to. Refusing to
        # guess is the point: without a sentinel there is nothing to resume.
        from vco_lib import collection_rename as cr

        folder = Path(args.folder or ".").resolve()
        sentinel = cr.read_sentinel(folder)
        if sentinel is None:
            print(f"Nothing to resume: no rename sentinel at {folder}.",
                  file=sys.stderr)
            return 2
        args.project = str(sentinel.get("project_id") or "")
        args.to = str(sentinel.get("new_name") or "")
        if not args.project or not args.to:
            print("The rename sentinel is incomplete; re-run the full command.",
                  file=sys.stderr)
            return 2
        return cmd_rename_collections(args)
    if not args.project or not args.to:
        print("vco project rename-collections: PROJECT and --to are required "
              "(or use --status / --resume / --drop-retired with --folder).",
              file=sys.stderr)
        return 2
    return cmd_rename_collections(args)

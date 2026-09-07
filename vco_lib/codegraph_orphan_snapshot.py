# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The detect-time LIVE-PREFIX SNAPSHOT that guards the orphan fs-reclaim.

``<project>/.claude/state/codegraph-orphan-live-prefixes.json`` is the handshake
between two commands that cannot both see Weaviate:

* ``detect-orphan-code-collections`` runs with Weaviate UP and records which
  normalised code-graph prefixes had ANY live class at that moment;
* ``reclaim-stranded-code-segments`` runs with Weaviate DOWN by construction
  (deleting a segment dir under a running Weaviate can corrupt the volume), so
  it cannot re-fetch the schema and consults this file instead.

Extracted from ``vco_lib.project_init`` in v0.2.92 W18 (that module is >16,000
lines; CLAUDE.md forbids growing it further). ``project_init`` keeps thin
aliases under the historical names and owns the two commands.

WHY THE READ IS TRI-STATE
-------------------------
The pre-v0.2.92 detector wrote this file from a class list that was ``[]``
whenever Weaviate was unreachable. A snapshot captured during an outage
therefore said "no prefix was live" — and the reclaim, whose whole job is to
trust this file because it cannot check for itself, read that as licence to
remove every code segment dir without a launcher.db binding row. Fixing the
detector stops new such files; it does nothing for one already on disk, and the
reclaim command is already sitting in some users' ``UPDATE_DEFERRED.md`` ready
to paste. So the file now RECORDS that the schema was read
(``live_schema_resolvable``), and a file that cannot make that claim reads as
``unknown`` — never as an empty set. A field a writer forgot cannot read as
healthy.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from vco_lib import weaviate_helpers as _wh

__all__ = [
    "SNAPSHOT_FILENAME",
    "SNAPSHOT_SCHEMA",
    "WHAT_SNAPSHOT",
    "snapshot_path",
    "write_snapshot",
    "read_snapshot",
    "render_cleanup_command",
]

SNAPSHOT_FILENAME = "codegraph-orphan-live-prefixes.json"
SNAPSHOT_SCHEMA = "vco.codegraph_orphan_live_prefixes.v1"

#: The question the snapshot answers, used in every ``ProbeResult`` message.
WHAT_SNAPSHOT = (
    "which code-graph prefixes were live when the orphans were detected"
)


def snapshot_path(folder: Path) -> Path:
    """`<folder>/.claude/state/codegraph-orphan-live-prefixes.json`."""
    return Path(folder) / ".claude" / "state" / SNAPSHOT_FILENAME


def write_snapshot(
    folder: Path,
    live_prefixes_normalised: list[str],
    *,
    live_schema_resolvable: bool = True,
) -> bool:
    """Persist the detect-time normalised live-prefix set. Best-effort:
    returns False on I/O failure rather than raising.

    ``live_schema_resolvable`` records that the schema this snapshot summarises
    was actually READ — see the module docstring. It is never written as False
    by the shipped path (a detect run that could not read the schema does not
    reach the emitter at all); the parameter exists so a future caller cannot
    write an unverified snapshot without saying so.
    """
    p = snapshot_path(folder)
    payload = {
        "schema": SNAPSHOT_SCHEMA,
        "live_schema_resolvable": bool(live_schema_resolvable),
        "live_prefixes_normalised": sorted(
            {s for s in (live_prefixes_normalised or []) if s}
        ),
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    body = json.dumps(payload, indent=2) + "\n"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        from vco_lib.atomic import atomic_write_text
        atomic_write_text(p, body)
        return True
    except Exception:  # noqa: BLE001 — fall back to a plain write
        try:
            p.write_text(body, encoding="utf-8")
            return True
        except Exception:  # noqa: BLE001 — best-effort by contract
            return False


def read_snapshot(folder: Path) -> "_wh.ProbeResult[set[str]]":
    """Read the detect-time live-prefix snapshot. TRI-STATE.

    * ``present(prefixes)`` — written from a READ schema, names at least one
      live prefix.
    * ``absent(set())`` — written from a READ schema that names no live prefix.
      A real answer: every code class had already been dropped, so every code
      segment dir on disk is genuinely stranded.
    * ``unknown(reason)`` — no snapshot, a malformed one, or one written
      WITHOUT the ``live_schema_resolvable`` marker (i.e. by a pre-v0.2.92 run,
      which may have captured it during an outage). The reclaim refuses.
    """
    p = snapshot_path(folder)
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return _wh.ProbeResult.unknown(
            f"no detect-time snapshot at {p}", what=WHAT_SNAPSHOT,
        )
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return _wh.ProbeResult.unknown(
            f"snapshot at {p} is not valid JSON", what=WHAT_SNAPSHOT,
        )
    if not isinstance(data, dict):
        return _wh.ProbeResult.unknown(
            f"snapshot at {p} is not an object", what=WHAT_SNAPSHOT,
        )
    vals = data.get("live_prefixes_normalised")
    if not isinstance(vals, list):
        return _wh.ProbeResult.unknown(
            f"snapshot at {p} carries no live-prefix list", what=WHAT_SNAPSHOT,
        )
    if data.get("live_schema_resolvable") is not True:
        return _wh.ProbeResult.unknown(
            f"snapshot at {p} was written before v0.2.92 and does not record "
            f"whether the live schema was actually READ — a snapshot captured "
            f"during a Weaviate outage is an EMPTY set that reads as 'nothing "
            f"was live'. Re-run `detect-orphan-code-collections` with Weaviate "
            f"UP to replace it",
            what=WHAT_SNAPSHOT,
        )
    prefixes = {str(v) for v in vals if isinstance(v, str) and v}
    if not prefixes:
        return _wh.ProbeResult.absent(what=WHAT_SNAPSHOT, value=set())
    return _wh.ProbeResult.present(prefixes, what=WHAT_SNAPSHOT)


# ---------------------------------------------------------------------------
# The printed CONSENTED cleanup commands — pure text over the detection
# ---------------------------------------------------------------------------

def render_cleanup_command(
    weaviate_url: str,
    live_orphans: list[dict],
    ondisk_orphans: list[dict],
    volume_dir: Optional[str],
    project_folder: Optional[str] = None,
) -> str:
    """Render the two CONSENTED, RE-VALIDATING cleanup commands for the
    orphan-code deferral. Never renders a drop that isn't re-validated at run
    time against the live binding table + live schema.
    """
    lines = [
        "# ORPHAN CODE-COLLECTION CLEANUP — CONSENTED, re-validated at run time.",
        "# Both commands re-probe the LIVE launcher.db bindings + live Weaviate",
        "# schema BEFORE any drop (re-probe-before-acting) — a case-only variant",
        "# of a live binding is NEVER dropped. Nothing here auto-runs.",
        "",
    ]
    if live_orphans:
        lines.append("# (a) LIVE-schema orphan classes (no current binding):")
        for o in live_orphans:
            cnt = o.get("object_count")
            cnt_txt = f"{cnt} objects" if isinstance(cnt, int) else "count unknown"
            lines.append(f"#     - {o['class_name']}  ({cnt_txt})")
        lines.append(
            "python -m vco_lib.project_init drop-orphan-code-collections "
            f"--weaviate-url {weaviate_url!r} --confirm"
        )
        lines.append("")
    if ondisk_orphans:
        lines.append(
            "# (b) ON-DISK stranded segment dirs (class ALREADY gone from the"
        )
        lines.append(
            "#     live schema — Weaviate's schema DELETE cannot reclaim these)."
        )
        lines.append(
            "#     FILESYSTEM-LEVEL reclaim. INVARIANT: Weaviate MUST be STOPPED"
        )
        lines.append(
            "#     first — deleting a segment dir under a RUNNING Weaviate can"
        )
        lines.append(
            "#     CORRUPT the volume (Weaviate holds the dir in its shard map)."
        )
        lines.append(
            "#     The command REFUSES to run while Weaviate answers /v1/meta;"
        )
        lines.append(
            "#     stop the weaviate container (launcher Services tab, or"
        )
        lines.append(
            "#     `podman stop weaviate_claude` / `docker compose stop weaviate`),"
        )
        lines.append(
            "#     run it, then restart Weaviate. GUARD (with Weaviate DOWN it"
        )
        lines.append(
            "#     CANNOT re-fetch the schema): it removes ONLY dirs whose"
        )
        lines.append(
            "#     normalised prefix is (a) NOT a live launcher.db code-graph"
        )
        lines.append(
            "#     binding AND (b) NOT in the DETECT-TIME live-prefix snapshot"
        )
        lines.append(
            "#     (captured while Weaviate was UP). It REFUSES everything when"
        )
        lines.append(
            "#     the keep-set is unresolvable OR the snapshot is missing,"
        )
        lines.append(
            "#     malformed, or written by a pre-v0.2.92 run that could not"
        )
        lines.append(
            "#     prove it had read the live schema."
        )
        for o in ondisk_orphans:
            mb = o["size_bytes"] / (1024 * 1024)
            lines.append(f"#     - {o['dir']}  ({mb:.1f} MB)")
        vd = volume_dir or "<weaviate-volume-dir>"
        recl = (
            "python -m vco_lib.project_init reclaim-stranded-code-segments "
            f"--volume-dir {vd!r} --weaviate-url {weaviate_url!r} "
            "--confirm --i-understand-filesystem-level"
        )
        # SEV-3 #1: pass the project folder so the reclaim can locate the
        # detect-time live-prefix snapshot in `.claude/state/`.
        if project_folder:
            recl += f" --project-folder {str(project_folder)!r}"
        lines.append(recl)
        lines.append("")
    return "\n".join(lines)

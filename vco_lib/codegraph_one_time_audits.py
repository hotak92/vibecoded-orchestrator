# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One-time, per-project code-graph audits: the "done for THIS key" ledger
(v0.2.102 W1-C S4/S5).

Two repairs must each run ONCE per project, and again only when the condition
they depend on changes:

* ``vectorless_current_rows`` (S4) — find rows at the CURRENT embed revision
  whose active slot holds no vector, and demote them to revision 0 so the
  ordinary census and the walk re-embed them. Key:
  ``"<active slot>@<CODEGRAPH_EMBED_REVISION>"``. A new revision or a slot
  switch makes the audit owed again.
* ``ignored_path_rows`` (S5) — purge rows whose stored path is in the CURRENT
  ignore set (the walk never re-stamps them, and rows at the current revision
  were invisible to every cleanup). Key: a fingerprint of the ignore set plus
  the project's ``index_dot_claude`` choice. An ignore-set change, or a flip of
  that choice, makes it owed again.

This follows the same pattern as the exposure-heal marker
(``code_embed_exposure``): a JSON file under ``vct_root_dir()/state/``, with
read-merge-write under ``vco_lib.atomic.exclusive_file_lock``. The difference
is that each entry here is just a per-project string key with no evidence
state machine, so it is a small separate file rather than more blocks in that
marker.

Soft-fail by contract:
* an unreadable or absent ledger reads as "not recorded" — the audit is owed;
  both audits are idempotent and resumable, so re-running is only a cost;
* a failed write returns ``False`` — the next resync retries.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

LEDGER_REL = Path("state") / "codegraph_one_time_audits.json"

AUDIT_VECTORLESS = "vectorless_current_rows"
AUDIT_IGNORED_PATHS = "ignored_path_rows"


def _ledger_path() -> Path:
    from vco_lib.paths import vct_root_dir

    return vct_root_dir() / LEDGER_REL


def _read() -> dict:
    try:
        data = json.loads(_ledger_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — absent / corrupt → nothing recorded
        return {}
    return data if isinstance(data, dict) else {}


def recorded_key(audit: str, project: str) -> Optional[str]:
    """The key ``audit`` last completed under for ``project``, or ``None``."""
    entry = (_read().get(audit) or {}).get(str(project))
    if isinstance(entry, dict) and isinstance(entry.get("key"), str):
        return entry["key"]
    return None


def audit_owed(audit: str, project: str, key: str) -> bool:
    """True unless ``audit`` already completed for ``project`` under ``key``."""
    return recorded_key(audit, project) != key


def record_audit(audit: str, project: str, key: str) -> bool:
    """Record that ``audit`` completed for ``project`` under ``key``.

    Call this ONLY after a run with zero per-row failures. A partial run has
    not done the work, and the next resync must retry it.
    """
    if not project or not key:
        return False
    from vco_lib.atomic import atomic_write_json, exclusive_file_lock

    path = _ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(path.with_name(path.name + ".lock")):
            data = _read()
            block = dict(data.get(audit) or {})
            block[str(project)] = {
                "key": key,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            data[audit] = block
            atomic_write_json(path, data)
        return True
    except Exception as exc:  # noqa: BLE001 — bookkeeping never crashes a repair
        logger.warning("codegraph audits: cannot record %s for %s: %s", audit, project, exc)
        return False

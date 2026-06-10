# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""UTC ISO-8601 timestamp helpers (vco_lib.timeutil — v0.2.53).

Consolidates 4 sites with 2 different timestamp formats identified in
:file:`.claude/context/audits/vco-lib-python-dedup-2026-06-10.md`
finding 7:
* install.py::_utc_iso_now → "%Y-%m-%dT%H:%M:%SZ" (Z-suffixed)
* embedding_service.py → microsecond-precision with +00:00 offset
* project_init.py + diagram_indexer.py → various inline strftime

Per docs/INSTALL_ARCHITECTURE_v2.md §7.5.

v0.2.53 lands the module with both canonical formats; migrating
install.py + project_init.py onto it is a v0.2.54 organisational
refactor (install.py's ``_utc_iso_now`` already implements the
``utc_iso_now`` form below).
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_iso_now() -> str:
    """ISO-8601 UTC timestamp with second precision + ``Z`` suffix.

    Format: ``YYYY-MM-DDTHH:MM:SSZ``.

    This is the format used by install.py's JSONL log writer and the
    UPDATE_DEFERRED.md frontmatter. Matches the Rust side's
    ``chrono::Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)``.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_iso_now_us() -> str:
    """ISO-8601 UTC timestamp with microsecond precision + ``+00:00`` offset.

    Format: ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``.

    Used by embedding_service.py for telemetry where microsecond
    precision matters. Kept separate from :func:`utc_iso_now` because
    not all consumers can parse the microsecond form (some Rust
    chrono ts parsers reject the .ffffff suffix).
    """
    return datetime.now(timezone.utc).isoformat()

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The legacy/canonical compose-override PAIR decisions (moved out of
``vco_lib.project_init`` in v0.2.100, which is past its line ratchet).

``infrastructure/docker-compose.override.yml`` (Docker Compose v1's auto-load
name) and ``compose.override.yaml`` (podman-compose's) can coexist. The rename
pass in ``project_init._detect_and_rename_legacy_compose_override`` asks these
two questions about a coexisting pair:

* :func:`classify_pair` (v0.2.83 B-F2) — byte-identical / YAML-equal /
  divergent;
* :func:`reconcile_vco_generated_pair` (v0.2.100 U16) — for a divergent pair
  whose BOTH halves VCO generated, reconcile them instead of asking the user.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def classify_pair(legacy_path: Path, canonical_path: Path) -> str:
    """v0.2.83 PLAN-v0283 B-F2: classify a coexisting legacy+canonical compose
    override pair for auto-resolution.

    Returns one of:
      * ``"identical"``     — byte-for-byte identical. The v0.2.54 C-RT-5 mirror
        (``volumes.rs`` writes the SAME body to BOTH names by design) so this is
        the SANCTIONED pair. B-F2(i): suppress the deferral, KEEP BOTH FILES.
      * ``"semantic_equal"`` — bytes differ but ``yaml.safe_load`` of each parses
        cleanly AND compares equal (comment/whitespace drift only). B-F2(ii):
        re-mirror the legacy file to the canonical bytes (canonical wins per the
        user's "update to use the new one" ruling), NO deferral.
      * ``"divergent"``     — genuinely different (parse failure on either side,
        yaml unavailable, or parsed-unequal). B-F2(iii): keep today's
        ``compose_override_filename_conflict`` deferral verbatim.

    Conservative on every uncertainty: unreadable file, import-yaml failure, or
    a parse error anywhere ⇒ ``"divergent"`` (defer to human judgement).
    """
    try:
        legacy_bytes = legacy_path.read_bytes()
        canonical_bytes = canonical_path.read_bytes()
    except OSError:
        return "divergent"
    if legacy_bytes == canonical_bytes:
        return "identical"
    # Byte-different → try a semantic (YAML-structure) comparison.
    try:
        import yaml  # PyYAML — a hard dep of the orchestrator venv.
    except ImportError:
        # yaml unavailable → cannot prove semantic equality → conservative.
        return "divergent"
    try:
        legacy_doc = yaml.safe_load(legacy_bytes.decode("utf-8"))
        canonical_doc = yaml.safe_load(canonical_bytes.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError, ValueError):
        return "divergent"
    # Only claim semantic equality for a MEANINGFUL parsed structure. A
    # comment-only / empty override parses to ``None`` (or a bare scalar), and
    # two byte-different comment-only files must NOT be read as "identical
    # config" (that would re-mirror away genuine hand-edits with no config to
    # prove equivalence). Require BOTH sides to be a non-empty mapping/sequence
    # (a real compose document is a mapping) before treating them as equal.
    if not isinstance(legacy_doc, (dict, list)) or not legacy_doc:
        return "divergent"
    if not isinstance(canonical_doc, (dict, list)) or not canonical_doc:
        return "divergent"
    if legacy_doc == canonical_doc:
        return "semantic_equal"
    return "divergent"


def managed_override_header(path: Path) -> Optional[str]:
    """The first line of ``path`` when it is a VCO-GENERATED override (it
    carries the marker every VCO override generator writes —
    ``service_adoption._OVERRIDE_MANAGED_MARKER``, the Rust ``storage_ux.rs`` /
    ``volumes.rs`` headers), else ``None``. Unreadable → ``None``."""
    from vco_lib.service_adoption import _OVERRIDE_MANAGED_MARKER

    try:
        with open(path, encoding="utf-8") as fh:
            first = fh.readline().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return first if _OVERRIDE_MANAGED_MARKER in first else None


def reconcile_vco_generated_pair(
    install_root: Path, legacy_path: Path, canonical_path: Path, ts: str,
) -> Optional[str]:
    """U16 (v0.2.100): a divergent pair that VCO GENERATED BOTH halves of is
    not a question for the user. Every current generator writes one body to
    BOTH names (the C-RT-5 mirror), so a divergent pair means one half is an
    older generator run's output: the half written LAST is the current
    generator's (a tie → the canonical name). Its bytes become the canonical
    ``compose.override.yaml`` and are re-mirrored to the legacy name (the
    sanctioned identical pair); every half whose bytes change is backed up
    first under ``.claude/backups/bundle-adoptions/<ts>/``.

    ``None`` (the conflict deferral stays) unless BOTH halves carry the SAME
    generator's header — a pair from two different generators (volume binds vs
    adoption), or any hand-written half, is a real choice — or when a backup or
    write fails. Returns a one-line summary on success."""
    legacy_hdr = managed_override_header(legacy_path)
    if legacy_hdr is None or legacy_hdr != managed_override_header(canonical_path):
        return None
    from vco_lib.atomic import atomic_write_bytes
    from vco_lib.project_init import _ADOPT_BACKUPS_REL, _backup_bytes_for_adoption, _log_auto

    try:
        newest = (legacy_path if legacy_path.stat().st_mtime > canonical_path.stat().st_mtime
                  else canonical_path)
        body = newest.read_bytes()
        for half in (legacy_path, canonical_path):
            current = half.read_bytes()
            if current != body:
                _backup_bytes_for_adoption(
                    install_root, str(half.relative_to(install_root)), ts, current)
        for half in (canonical_path, legacy_path):
            atomic_write_bytes(half, body)
    except (OSError, ValueError) as exc:
        _log_auto(f"VCO-generated override pair not reconciled ({type(exc).__name__}: {exc}) "
                  "— kept the conflict deferral")
        return None
    return (f"reconciled VCO's own generated override pair: `{canonical_path}` = the newest "
            f"generator output (`{newest.name}`), `{legacy_path.name}` re-mirrored; the replaced "
            f"bytes are in {_ADOPT_BACKUPS_REL.as_posix()}/{ts}/")

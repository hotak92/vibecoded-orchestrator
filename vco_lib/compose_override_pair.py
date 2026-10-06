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
        (the Rust override writer writes the SAME body to BOTH names by
        design — ``storage_ux.rs`` since the v0.2.101 Q4b merge) so this is
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
    ``service_adoption._OVERRIDE_MANAGED_MARKER``, the Rust ``storage_ux.rs``
    headers), else ``None``. Unreadable → ``None``."""
    from vco_lib.service_adoption import _OVERRIDE_MANAGED_MARKER

    try:
        with open(path, encoding="utf-8") as fh:
            first = fh.readline().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return first if _OVERRIDE_MANAGED_MARKER in first else None


#: Two halves whose modification times are this close were written by the
#: same event — a checkout, a copy, an extract — not by two generator runs, so
#: their mtimes are no evidence of which is newer (W4R-11).
MTIME_EVIDENCE_S = 2.0


def newest_generated_half(legacy_path: Path, canonical_path: Path) -> tuple[Path, str]:
    """``(half, rule)``: which half of a VCO-generated pair is the current
    generator's output, and the rule that decided it.

    The half written LAST — but only when the two modification times are more
    than :data:`MTIME_EVIDENCE_S` apart. A ``git checkout`` / copy / archive
    extract writes both halves in one go and resets their times together,
    which would otherwise crown whichever it happened to write second; within
    the window the CANONICAL name wins (the owner's "use the new one" ruling,
    the same tie rule as B-F2)."""
    legacy_m, canonical_m = legacy_path.stat().st_mtime, canonical_path.stat().st_mtime
    if abs(legacy_m - canonical_m) <= MTIME_EVIDENCE_S:
        return canonical_path, (f"written within {MTIME_EVIDENCE_S:.0f} s of each other "
                                "(one checkout/copy), so the canonical name wins")
    if legacy_m > canonical_m:
        return legacy_path, "written later than the canonical half"
    return canonical_path, "written later than the legacy half"


def reconcile_vco_generated_pair(
    install_root: Path, legacy_path: Path, canonical_path: Path, ts: str,
) -> Optional[str]:
    """U16 (v0.2.100): a divergent pair that VCO GENERATED BOTH halves of is
    not a question for the user. Every current generator writes one body to
    BOTH names (the C-RT-5 mirror), so a divergent pair means one half is an
    older generator run's output: :func:`newest_generated_half` picks the
    current generator's (written last, when the times are evidence; else the
    canonical name). Its bytes become the canonical
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
        newest, rule = newest_generated_half(legacy_path, canonical_path)
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
            f"generator output (`{newest.name}`, {rule}), `{legacy_path.name}` re-mirrored; the "
            f"replaced bytes are in {_ADOPT_BACKUPS_REL.as_posix()}/{ts}/")

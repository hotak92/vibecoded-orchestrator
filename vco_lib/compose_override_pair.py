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
  whose BOTH halves VCO generated, reconcile them instead of asking the user;
* :func:`pair_state` (v0.2.102 W5-A, P299-B2) — the general case: are the two
  auto-load halves in an install's ``infrastructure/`` the same config? A
  divergent pair means docker compose and podman-compose run DIFFERENT
  configurations (the 2026-09 outage: one twin hand-fixed, the other still
  poisoned). The doctor and the install-time rename pass both report it as
  ``compose_override_pair_diverged`` (one entry builder:
  :func:`divergent_pairs_entry`) and
  every Python override writer refuses to overwrite such a pair
  (``service_adoption.write_adoption_override``) — a divergent half may be a
  user's edit, and only the user can say which half is right.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

#: Docker Compose v1's auto-load name for the override.
LEGACY_NAME = "docker-compose.override.yml"
#: podman-compose's (and Compose v2's preferred) auto-load name.
CANONICAL_NAME = "compose.override.yaml"

#: The directories under an install root that may hold an override pair: the
#: compose home, and the hand-edited legacy location some users have. ONE home
#: — the install-time rename pass (``project_init``), the doctor and the clear
#: probe all walk exactly these.
SEARCH_SUBDIRS: tuple[str, ...] = ("infrastructure", "claude_mcp_servers")

#: The ONE condition for "the two halves hold different configurations"
#: (v0.2.102 W5-A). It SUPERSEDES the install-time pass's former
#: ``compose_override_filename_conflict`` (retired in the registry; existing
#: entries of the old id drain through the rename pass's reconcile).
CID_PAIR_DIVERGED = "compose_override_pair_diverged"

PAIR_ABSENT = "absent"        # neither half exists
PAIR_SINGLE = "single"        # exactly one half exists — nothing to diverge
PAIR_EQUAL = "equal"          # both exist; byte-identical or YAML-equal
PAIR_DIVERGENT = "divergent"  # both exist and differ (or cannot be compared)


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
        divergent-pair deferral (``compose_override_pair_diverged`` since
        v0.2.102; ``compose_override_filename_conflict`` before).

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


def pair_state(infra_dir: Path) -> str:
    """The state of the override pair in ``infra_dir`` (v0.2.102 P299-B2).

    One of :data:`PAIR_ABSENT`, :data:`PAIR_SINGLE`, :data:`PAIR_EQUAL`,
    :data:`PAIR_DIVERGENT`. Equality is :func:`classify_pair`'s — the rename
    pass's rule, so the doctor and the install pass cannot disagree about what
    "the same config" means; its conservative arm (unreadable, unparsable,
    PyYAML missing) reads as divergent, never as equal.
    """
    legacy = Path(infra_dir) / LEGACY_NAME
    canonical = Path(infra_dir) / CANONICAL_NAME
    present = [p for p in (legacy, canonical) if p.is_file()]
    if not present:
        return PAIR_ABSENT
    if len(present) == 1:
        return PAIR_SINGLE
    if classify_pair(legacy, canonical) in ("identical", "semantic_equal"):
        return PAIR_EQUAL
    return PAIR_DIVERGENT


def divergent_pair_refusal(infra_dir: Path) -> Optional[str]:
    """The refusal an override WRITER returns for a divergent pair, else None.

    Writing one body to both names would silently replace whichever half the
    user edited; the writer must leave both alone and say why.
    """
    if pair_state(infra_dir) != PAIR_DIVERGENT:
        return None
    return (
        f"{Path(infra_dir) / CANONICAL_NAME} and {Path(infra_dir) / LEGACY_NAME} hold "
        "DIFFERENT configurations (one may be a hand edit) — refusing to overwrite "
        "either; make them agree (or delete the stale one), then re-run"
    )


def divergent_pairs(install_root: Path) -> list[tuple[Path, Path]]:
    """Every ``(legacy, canonical)`` pair under :data:`SEARCH_SUBDIRS` of
    ``install_root`` whose halves diverge (:func:`pair_state`)."""
    out: list[tuple[Path, Path]] = []
    for sub in SEARCH_SUBDIRS:
        d = Path(install_root) / sub
        if pair_state(d) == PAIR_DIVERGENT:
            out.append((d / LEGACY_NAME, d / CANONICAL_NAME))
    return out


def pair_remediation(pairs: "list[tuple[Path, Path]]") -> str:
    """The read-only remedy every surface prints for divergent pairs."""
    from vco_lib import remedy_shell

    compare = "fc" if remedy_shell.is_windows() else "diff"
    lines = [
        "# The two override auto-load names hold DIFFERENT configurations:",
        "#   podman-compose reads compose.override.yaml, docker compose v1 reads",
        "#   docker-compose.override.yml, so the two runtimes start different",
        "#   stacks. Compare each pair (read-only):",
    ]
    for legacy, canonical in pairs:
        lines.append(f"{compare} {remedy_shell.quote(legacy)} {remedy_shell.quote(canonical)}")
    lines += [
        "# Keep the half that is right, make the other identical to it (or",
        "# delete the stale one). VCO overwrites neither while they differ.",
        "# Then confirm: vco doctor   (this entry clears itself once they agree)",
    ]
    return remedy_shell.steps(*lines)


def divergent_pairs_summary(pairs: "list[tuple[Path, Path]]") -> str:
    names = "; ".join(f"{canonical} vs {legacy}" for legacy, canonical in pairs)
    return (
        f"{len(pairs)} compose override pair(s) hold different configurations — "
        f"podman-compose and docker compose would start different stacks: {names}"
    )


def divergent_pairs_entry(pairs: "list[tuple[Path, Path]]", *, detected: Optional[str] = None):
    """The ONE ``compose_override_pair_diverged`` deferral entry — built here
    for both emitters (the doctor's probe and the install-time rename pass), so
    the two can never word the same state differently."""
    from vco_lib.deferral_report import DeferralEntry

    return DeferralEntry(
        condition_id=CID_PAIR_DIVERGED,
        title="The two compose override files disagree",
        detected=detected or divergent_pairs_summary(pairs),
        why_deferred=(
            "Only you can say which half is right: either may be a hand edit (the "
            "2026-09 outage left one twin fixed by hand and the other still broken), "
            "and auto-merging override YAML is unsafe — the halves may encode "
            "different volume sources, ports or services. VCO overwrites neither "
            "while they differ: its override writers refuse the pair and the "
            "install-time rename pass does not rename it. The entry clears itself "
            "as soon as the two files agree or one is removed — at the next doctor "
            "run, install or update."
        ),
        command_to_apply=pair_remediation(pairs),
        severity="warning",
        kg_node_refs=["docs/TROUBLESHOOTING.md"],
    )

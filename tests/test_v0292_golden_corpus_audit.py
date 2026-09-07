# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — audit every stored golden row against its OWN fixture source.

WHY THIS EXISTS
---------------
``tests/test_codegraph_golden.py`` compares the analyzer's output against
``expected/*.json``. That answers "did the output change", which is a
DIFFERENT question from "is the output right" — and the corpus has twice been
found ratifying a real defect as expected behaviour, because the snapshot WAS
the defect. A snapshot comparison cannot see:

  * a class stored starting on ``package golden;`` or on ``namespace shapes {``,
    taking the whole namespace as its body;
  * a method stored starting on the PREVIOUS member's closing ``}``, with a
    body running to end-of-file;
  * a ``end_line: 45`` on a 44-line file;
  * every Ruby entity in a 40-line file sharing ``end_line: 40``.

All of those were committed, green, and wrong. The discriminator that found
them was not comparing snapshots — it was reading ``repo/src/<file>`` and
checking each row's claims against the SOURCE. That audit found NINE distinct
defects across FIVE languages at v0.2.92 (WP-5), and six more at WP-5b.

It was a scratch script both times. This file is that property, shipped, so
it holds for whoever regenerates the corpus next — and so the next
regeneration cannot quietly convert a new loss into "expected".

THE ALLOW-LIST IS THE DESIGN
----------------------------
Three findings are the auditor's own heuristic meeting DELIBERATE extractor
semantics. They are enumerated below with a reason each, keyed precisely
enough that a fourth cannot hide behind them. A bare count assertion
("6 findings") would let a new loss take a retired one's place, which is the
same shape as the ``fn reset`` test that encoded a lost entity as an
invariant. :func:`test_no_allow_listed_finding_has_gone_stale` closes the
other direction: an entry that stops firing must be DELETED, not left as
cover.

WHAT IS PINNED
--------------
* the ACT: every non-allow-listed row satisfies the source-fidelity property;
* the LEAVE-ALONE: the three known-deliberate divergences stay allowed, and
  each is still real;
* the auditor's own teeth: :func:`test_the_auditor_catches_a_perturbed_row`
  corrupts a snapshot row six ways and asserts each is caught. An auditor
  nobody has watched fail is not an auditor.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Dict, List, NamedTuple, Tuple

import pytest

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "codegraph_golden"
_EXPECTED_DIR = _FIXTURE_ROOT / "expected"
_REPO_DIR = _FIXTURE_ROOT / "repo"

#: (collection, body field) pairs the audit covers. ``CodeModule`` carries no
#: line range, and ``CodeAPI`` / ``CodeInteraction`` store a route rather than
#: a source slice, so neither has a source-fidelity property to check.
_AUDITED = (("CodeClass", "class_body"), ("CodeFunction", "function_body"))


class Finding(NamedTuple):
    collection: str
    full_name: str
    file_path: str
    code: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - diagnostic only
        d = f" ({self.detail})" if self.detail else ""
        return f"{self.collection} {self.full_name} [{self.file_path}] {self.code}{d}"


#: key → why this divergence is CORRECT. Keyed on the exact row and the exact
#: detail, so a different method name or a different row produces a NEW,
#: unallowed finding rather than landing in an existing bucket.
_ALLOWED: Dict[Tuple[str, str, str, str], str] = {
    **{
        ("CodeClass", "engine.Counter", "method_not_in_class_body", m): (
            "Rust methods live in `impl` blocks, OUTSIDE the struct body, so "
            "`engine.Counter`'s methods list names functions that are "
            "legitimately not inside its stored class_body. Containment is the "
            "wrong test for Rust, not a defect in the row."
        )
        for m in ("new", "increment", "value", "reset")
    },
    ("CodeClass", "vector.Vector", "body_not_source_slice", ""): (
        "Lua class bodies are SYNTHESISED rather than sliced: `vector.Vector` "
        "stores `Vector = {}` plus one generated `function Vector.<m>(...) end` "
        "line per method, and `start_line == end_line` by construction. A "
        "body-equals-its-slice check must flag it and should not be believed."
    ),
    ("CodeFunction", "big_module.enormous_computation", "body_not_source_slice", ""): (
        "The over-budget function is CHUNKED: chunk 0 stores the first chunk's "
        "text, not the whole 5..213 slice. The chunk rows are the analyzer's "
        "codesage budget working as designed."
    ),
}


def audit_corpus(expected_dir: Path, repo_dir: Path) -> List[Finding]:
    """Check every stored row against the fixture source it claims to describe.

    Deliberately takes its two directories as ARGUMENTS rather than reading the
    module-level constants: that is what lets the fail-proof test below run it
    against a perturbed scratch copy without touching the committed corpus.
    """
    findings: List[Finding] = []
    sources: Dict[str, List[str]] = {}

    def source_lines(rel: str) -> List[str]:
        if rel not in sources:
            sources[rel] = (repo_dir / rel).read_text(encoding="utf-8").split("\n")
        return sources[rel]

    for base, body_field in _AUDITED:
        rows = json.loads((expected_dir / f"{base}.json").read_text(encoding="utf-8"))

        # A type may be declared more than once in ONE file (a reopened Ruby
        # class, a C# partial class). Its `methods` list is deliberately the
        # UNION over those declarations, so the containment check below runs
        # against the union of their bodies rather than each row's own slice.
        union: Dict[Tuple[str, str], List[str]] = {}
        for r in rows:
            union.setdefault(
                (r.get("file_path", ""), r.get("full_name", "")), []
            ).append(r.get(body_field) or "")

        for r in rows:
            fp = r.get("file_path", "")
            name = r.get("name") or ""
            full_name = r.get("full_name") or ""
            # Only the canonical chunk carries the entity's own range.
            if r.get("chunk_num", 0) not in (0, None):
                continue

            def add(code: str, detail: str = "") -> None:
                findings.append(Finding(base, full_name, fp, code, detail))

            lines = source_lines(fp)
            sl, el = r.get("start_line"), r.get("end_line")

            if sl is None or not (1 <= sl <= len(lines)):
                add("start_line_out_of_range", f"{sl} vs 1..{len(lines)}")
                continue

            decl = lines[sl - 1]
            if not decl.strip():
                add("start_line_blank", str(sl))
            elif not decl.strip().strip("{}();,"):
                # Punctuation only — a neighbouring construct's brace, which is
                # exactly how the Java and C++ skews presented.
                add("start_line_punctuation_only", repr(decl))

            window = "\n".join(lines[sl - 1:sl + 1])
            if name and name not in window:
                add("name_not_at_start_line", repr(decl))

            if el is None or not (sl <= el <= len(lines)):
                add("end_line_out_of_range", f"{el} vs {sl}..{len(lines)}")
                continue

            body = r.get(body_field) or ""
            if body != "\n".join(lines[sl - 1:el]):
                add("body_not_source_slice", "")
            if el == len(lines) and sl < el - 1 and not lines[el - 1].strip():
                # The runaway-scan signature: a body that ran to EOF because no
                # closing token was found. Every Ruby row looked like this.
                add("end_line_eof_blank_runaway", str(el))

            if base == "CodeClass":
                own = "\n".join(union[(fp, full_name)])
                for method in r.get("methods") or []:
                    if method not in own:
                        add("method_not_in_class_body", method)

    return findings


def _unallowed(findings: List[Finding]) -> List[Finding]:
    return [f for f in findings if (f.collection, f.full_name, f.code, f.detail)
            not in _ALLOWED]


# ═══════════════════════════════════════════════════════════════════════════
# THE ACT
# ═══════════════════════════════════════════════════════════════════════════
def test_every_stored_row_matches_its_own_fixture_source() -> None:
    """The property nine defects across five languages violated while the
    snapshot suite stayed green."""
    unallowed = _unallowed(audit_corpus(_EXPECTED_DIR, _REPO_DIR))
    assert not unallowed, (
        "the golden corpus stores rows its own fixture source contradicts:\n"
        + "\n".join(f"  - {f}" for f in unallowed)
        + "\n\nThis is what a snapshot comparison CANNOT see. Fix the "
        "extractor, or — if the divergence is deliberate — add it to "
        "_ALLOWED with the reason it is correct. Do NOT regenerate to make "
        "this pass."
    )


def test_the_audit_actually_inspected_the_corpus() -> None:
    """A zero-row audit would make the assertion above vacuous — the same
    trap the WP-5c anti-fixture needed a positive control for."""
    rows = 0
    for base, _ in _AUDITED:
        rows += len(json.loads(
            (_EXPECTED_DIR / f"{base}.json").read_text(encoding="utf-8")
        ))
    assert rows >= 80, f"only {rows} rows audited — the corpus shrank"
    languages = {
        r.get("language")
        for base, _ in _AUDITED
        for r in json.loads(
            (_EXPECTED_DIR / f"{base}.json").read_text(encoding="utf-8")
        )
    }
    assert len(languages) >= 10, f"only {languages} — a fixture stopped parsing"


def test_no_allow_listed_finding_has_gone_stale() -> None:
    """Every entry must still FIRE. A retired entry left in place is cover for
    the next loss to hide behind — the failure mode a bare count assertion has
    by construction."""
    live = {(f.collection, f.full_name, f.code, f.detail)
            for f in audit_corpus(_EXPECTED_DIR, _REPO_DIR)}
    stale = [k for k in _ALLOWED if k not in live]
    assert not stale, (
        "these allow-list entries no longer describe anything — DELETE them "
        f"rather than leaving them as cover: {stale}"
    )


def test_every_allow_list_entry_carries_a_reason() -> None:
    for key, reason in _ALLOWED.items():
        assert len(reason) > 80, f"{key}: a reason, not a label — got {reason!r}"


# ═══════════════════════════════════════════════════════════════════════════
# THE AUDITOR'S OWN TEETH — prove it can fail
# ═══════════════════════════════════════════════════════════════════════════
def _scratch_corpus(tmp_path: Path) -> Tuple[Path, Path]:
    """A writable copy of the corpus. The committed one is never touched."""
    exp = tmp_path / "expected"
    shutil.copytree(_EXPECTED_DIR, exp)
    return exp, _REPO_DIR


def _perturb(expected_dir: Path, collection: str, full_name: str, **fields) -> None:
    path = expected_dir / f"{collection}.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    hits = [r for r in rows if r.get("full_name") == full_name
            and r.get("chunk_num", 0) in (0, None)]
    assert hits, f"no row {full_name!r} to perturb — fixture drifted"
    for key, value in fields.items():
        hits[0][key] = value
    path.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")


@pytest.mark.parametrize(
    "full_name,fields,expected_code",
    [
        pytest.param(
            "geometry.distance", {"start_line": 47},
            "body_not_source_slice", id="start-line-shifted-into-the-body",
        ),
        pytest.param(
            "geometry.Circle", {"start_line": 8},
            "name_not_at_start_line", id="class-stored-on-the-namespace-line",
        ),
        pytest.param(
            "geometry.summarize", {"end_line": 400},
            "end_line_out_of_range", id="end-line-past-eof",
        ),
        pytest.param(
            "geometry.quadrant", {"function_body": "int quadrant(const Point& p) {"},
            "body_not_source_slice", id="body-truncated",
        ),
        pytest.param(
            "geometry.Tally", {"methods": ["add", "total", "ghost"]},
            "method_not_in_class_body", id="methods-lists-a-nonexistent-member",
        ),
        pytest.param(
            "ledger.Account", {"start_line": 13},
            "start_line_blank", id="row-stored-on-a-blank-line",
        ),
    ],
)
def test_the_auditor_catches_a_perturbed_row(
    full_name: str, fields: dict, expected_code: str, tmp_path: Path
) -> None:
    """Corrupt one stored row in a scratch copy; the audit must object.

    Each perturbation reproduces a defect shape the corpus REALLY carried:
    a start_line shifted off its declaration (C#, six rows), a class stored on
    the enclosing `namespace` line (C++ `Circle`), a runaway end_line (Ruby),
    a body that is not its slice (Java), and a methods list naming something
    absent from the body (the pre-scoping whole-file lists).
    """
    exp, repo = _scratch_corpus(tmp_path)
    assert not _unallowed(audit_corpus(exp, repo)), "scratch copy started dirty"

    collection = "CodeClass" if full_name in (
        "geometry.Circle", "geometry.Tally", "ledger.Account",
    ) else "CodeFunction"
    _perturb(exp, collection, full_name, **fields)

    codes = {f.code for f in _unallowed(audit_corpus(exp, repo))}
    assert expected_code in codes, (
        f"perturbing {full_name} with {fields} produced {codes or 'NOTHING'} — "
        "the auditor cannot see this defect class"
    )


def test_a_perturbation_inside_an_allow_listed_row_is_still_caught(
    tmp_path: Path,
) -> None:
    """The allow-list must not become a blanket exemption for its rows.

    ``engine.Counter`` is allowed to list four methods absent from its body
    (Rust `impl` blocks). A FIFTH, invented name is a different finding with a
    different detail, so it is NOT covered — which is the whole reason the key
    includes the method name."""
    exp, repo = _scratch_corpus(tmp_path)
    _perturb(exp, "CodeClass", "engine.Counter",
             methods=["new", "increment", "value", "reset", "ghost"])
    unallowed = _unallowed(audit_corpus(exp, repo))
    assert [f.detail for f in unallowed] == ["ghost"], (
        f"expected only the invented method to be unallowed, got {unallowed}"
    )

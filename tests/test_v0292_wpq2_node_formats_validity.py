# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-Q2: the SECOND writer of `knowledge/.node_formats.json`.

WHAT THIS CLOSES
----------------
WP-Q root-caused a field report of "43% of cached summaries are non-answers"
to a Windows argv defect (`claude` is an npm `.cmd` shim, `cmd.exe` re-parsed
the arguments, and the newline inside the prompt terminated the command, so
the model saw only the system prompt and replied *"Ready. What do you need
summarized?"*). It fixed the transport (prompt over stdin) and taught the
RUNTIME generator two things: never cache a non-answer, and never treat a
STORED one as satisfied — so poisoned rows heal on the next ordinary run.

`claude_mcp_servers/scripts/generate_node_formats.py` writes THE SAME FILE,
through its own Ollama/Haiku router, and on the orchestrator-root layout
`kg-sync --all` reaches it via
`sync_knowledge_graph._regen_node_formats_after_full_sync()`. Without the
same gate on both sides the healing is real and defeatable: one path repairs
a row, the other reports it satisfied (read side) or writes a fresh refusal
into it (write side).

WHAT IS PINNED HERE
-------------------
* the predicate is the ladder's own `is_non_answer`, by IDENTITY — a copy
  would be a mirror, and mirrors drift (project rule A > B > C);
* READ side: a stored non-answer description / summary / chunk summary makes
  the entry not-satisfied, so it regenerates;
* the two LEAVE-ALONE cases that keep this from becoming churn — a healthy
  entry with a matching hash is still skipped, and a MISSING chunk summary
  does not invalidate anything (absent is not poisoned);
* the predicate stays PREFIX-ANCHORED: a real summary may *mention* a
  refusal, it just must not *begin* with one;
* WRITE side: a freshly generated non-answer is reported as an error and
  never stored; a non-answer chunk summary is dropped while its healthy
  siblings are kept;
* the `auth` cooldown is SHORTER than the rate-limit one (coordinator
  ruling: the user is the fix for an auth failure and expects the next node
  to retry; a rate limit they cannot fix, so its long cooldown stands);
* every `VCO_SUMMARY_BREAKER*` knob the ladder reads is documented, and
  every one documented is read (R24, in both directions).

RED-PROOF
---------
Every assertion here was run against `/tmp/wpq2/pre/generate_node_formats.py`
(the pre-fix file) and FAILED there before passing against the tree. A test
that is green both ways proves nothing.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "claude_mcp_servers" / "scripts" / "generate_node_formats.py"
LADDER_DIR = REPO_ROOT / "templates" / "scripts"
CONFIG_DOC = REPO_ROOT / "docs" / "CONFIGURATION.md"


def _load_generator():
    """Import the generator by path (it is a script, not a package member)."""
    if str(LADDER_DIR) not in sys.path:
        sys.path.insert(0, str(LADDER_DIR))
    spec = importlib.util.spec_from_file_location(
        "_wpq2_generate_node_formats", GENERATOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gnf():
    return _load_generator()


@pytest.fixture(scope="module")
def ladder():
    if str(LADDER_DIR) not in sys.path:
        sys.path.insert(0, str(LADDER_DIR))
    import summary_backends  # noqa: PLC0415  # pyright: ignore[reportMissingImports]

    return summary_backends


# ═══════════════════════════════════════════════════════════════════════════
# ONE implementation, not a mirror
# ═══════════════════════════════════════════════════════════════════════════

def test_the_predicate_is_the_ladders_own_function_not_a_copy(gnf, ladder):
    """A > B > C: shared CODE, not a duplicated predicate with a parity test.

    Identity, not equality of behaviour: a copy that happens to agree today
    is exactly the thing that drifts. If a future edit re-inlines the
    predicate here, this fails immediately rather than three releases later
    when the two lists diverge.
    """
    assert gnf.is_non_answer is ladder.is_non_answer


def test_the_ladder_import_is_hard_so_a_broken_checkout_cannot_degrade_quietly():
    """No `except ImportError: is_non_answer = lambda _: False` fallback.

    That fallback would silently restore the re-poisoning bug this package
    closes — the failure mode VCO's no-silent-fallback rule exists for.
    """
    source = GENERATOR.read_text(encoding="utf-8")
    head = source.split("def has_formats", 1)[0]
    assert "import summary_backends" in head
    assert not re.search(
        r"except\s+(ImportError|ModuleNotFoundError|Exception)[^\n]*:\s*\n"
        r"(\s+#[^\n]*\n)*\s*is_non_answer\s*=",
        head,
    ), "the ladder import must not degrade to a permissive stub"


# ═══════════════════════════════════════════════════════════════════════════
# READ side — a stored non-answer is NOT a satisfied entry
# ═══════════════════════════════════════════════════════════════════════════

GOOD_DESC = "Line one about the thing.\nLine two about the thing."
GOOD_SUMMARY = "This node documents the KG summary ladder and its breaker."

# The exact string the field report found, plus two other shapes.
POISON = [
    "Ready. What do you need summarized?",
    "I cannot summarize this content.",
    "",
]


@pytest.mark.parametrize("bad", POISON)
def test_a_stored_non_answer_description_is_not_satisfied(gnf, bad):
    db = {"knowledge/a.md": {
        "description": bad, "summary": GOOD_SUMMARY, "content_hash": "h",
    }}
    assert gnf.has_formats("knowledge/a.md", db, "h") is False


@pytest.mark.parametrize("bad", POISON)
def test_a_stored_non_answer_summary_is_not_satisfied(gnf, bad):
    db = {"knowledge/a.md": {
        "description": GOOD_DESC, "summary": bad, "content_hash": "h",
    }}
    assert gnf.has_formats("knowledge/a.md", db, "h") is False


def test_a_stored_non_answer_chunk_summary_invalidates_the_entry(gnf):
    db = {"knowledge/a.md": {
        "description": GOOD_DESC,
        "summary": GOOD_SUMMARY,
        "content_hash": "h",
        "chunk_summaries": {"1": "Covers the breaker.", "2": "I cannot help with that."},
        "total_chunks": 2,
    }}
    assert gnf.has_formats("knowledge/a.md", db, "h") is False


# ── the LEAVE-ALONE half: this must not become churn ──────────────────────

def test_a_missing_chunk_summary_does_not_invalidate_the_entry(gnf):
    """Absent is not poisoned (coordinator ruling, CONFIRMED).

    Treating a missing chunk summary as poisoned would re-run the two
    whole-node LLM calls on every sync for any node whose Weaviate chunk
    fetch keeps failing — churn bought for a retrieval-tier nicety.
    """
    db = {"knowledge/a.md": {
        "description": GOOD_DESC, "summary": GOOD_SUMMARY,
        "content_hash": "h", "total_chunks": 3,
    }}
    assert gnf.has_formats("knowledge/a.md", db, "h") is True

    db["knowledge/a.md"]["chunk_summaries"] = {}
    assert gnf.has_formats("knowledge/a.md", db, "h") is True


def test_a_healthy_entry_with_a_matching_hash_is_still_skipped(gnf):
    """The content-hash gate is why an unchanged node costs 0 s. It stays."""
    db = {"knowledge/a.md": {
        "description": GOOD_DESC, "summary": GOOD_SUMMARY, "content_hash": "h",
    }}
    assert gnf.has_formats("knowledge/a.md", db, "h") is True


def test_a_changed_node_still_regenerates(gnf):
    db = {"knowledge/a.md": {
        "description": GOOD_DESC, "summary": GOOD_SUMMARY, "content_hash": "old",
    }}
    assert gnf.has_formats("knowledge/a.md", db, "new") is False


def test_a_summary_that_MENTIONS_a_refusal_is_still_a_summary(gnf):
    """Prefix-anchored, deliberately — preserve that.

    A false positive costs one regeneration; a false negative freezes a
    poisoned row forever. But a predicate that fired on any occurrence
    would reject real summaries of the refusal-handling code itself.
    """
    db = {"knowledge/a.md": {
        "description": GOOD_DESC,
        "summary": (
            "The ladder rejects replies that begin with 'I cannot' or "
            "'Ready. What do you need summarized?' and never caches them."
        ),
        "content_hash": "h",
    }}
    assert gnf.has_formats("knowledge/a.md", db, "h") is True


def test_a_non_dict_entry_is_not_satisfied(gnf):
    assert gnf.has_formats("knowledge/a.md", {"knowledge/a.md": "junk"}, None) is False
    assert gnf.has_formats("knowledge/missing.md", {}, None) is False


# ═══════════════════════════════════════════════════════════════════════════
# WRITE side — a freshly generated non-answer is never cached
# ═══════════════════════════════════════════════════════════════════════════

NODE_TEXT = (
    "---\ntitle: Some Node\ntype: concept\ntags: [x]\n"
    "created: 2026-01-01T00:00:00Z\nupdated: 2026-01-01T00:00:00Z\n"
    "status: active\n---\n\nBody text of the node.\n"
)


@pytest.fixture
def node_file(tmp_path):
    p = tmp_path / "a.md"
    p.write_text(NODE_TEXT, encoding="utf-8")
    return p


def _stub_generation(monkeypatch, gnf, *, description, summary, chunks=None,
                     chunk_summary=None):
    monkeypatch.setattr(gnf, "generate_description", lambda *a, **k: description)
    monkeypatch.setattr(gnf, "generate_summary", lambda *a, **k: summary)
    # W7: mirrors the REAL signature (title, file_path) — the production
    # caller now scopes the chunk fetch by the node's relative path, and a
    # 1-arg stub turns that wiring change into a TypeError three frames away.
    monkeypatch.setattr(
        gnf, "get_chunks_from_weaviate", lambda _t, _fp="": chunks or [])
    if chunk_summary is not None:
        monkeypatch.setattr(
            gnf, "generate_chunk_summary",
            lambda _t, cn, _total, _c: chunk_summary(cn),
        )


@pytest.mark.parametrize("bad", POISON)
def test_process_node_refuses_to_cache_a_generated_non_answer(
    gnf, monkeypatch, node_file, bad
):
    """The re-poisoning vector: this script's `call_llm` is its OWN router,
    so unlike the shared ladder's it does not raise on a non-answer."""
    _stub_generation(monkeypatch, gnf, description=bad, summary=GOOD_SUMMARY)
    db: dict = {}
    status = gnf.process_node(node_file, "m", False, False, db)
    assert status.startswith("error:"), status
    assert "non-answer" in status
    assert db == {}, "a non-answer must never reach the sidecar"


def test_process_node_refuses_a_non_answer_summary_too(
    gnf, monkeypatch, node_file
):
    _stub_generation(
        monkeypatch, gnf, description=GOOD_DESC,
        summary="Ready. What do you need summarized?",
    )
    db: dict = {}
    assert gnf.process_node(node_file, "m", False, False, db).startswith("error:")
    assert db == {}


def test_process_node_stores_a_good_pair(gnf, monkeypatch, node_file):
    """The ACT side: nothing above may block a healthy generation."""
    _stub_generation(monkeypatch, gnf, description=GOOD_DESC, summary=GOOD_SUMMARY)
    db: dict = {}
    assert gnf.process_node(node_file, "m", False, False, db) == "generated"
    (entry,) = db.values()
    assert entry["summary"] == GOOD_SUMMARY


def test_process_node_drops_a_non_answer_chunk_but_keeps_its_siblings(
    gnf, monkeypatch, node_file
):
    """Dropped, not stored — because `has_formats` treats a STORED
    non-answer chunk as invalid and a MISSING one as fine. Storing it would
    invalidate the whole entry on the very next pass."""
    _stub_generation(
        monkeypatch, gnf, description=GOOD_DESC, summary=GOOD_SUMMARY,
        chunks=[(1, "c1"), (2, "c2")],
        chunk_summary=lambda cn: (
            "I cannot summarize this." if cn == 2 else "Covers the ladder."
        ),
    )
    db: dict = {}
    assert gnf.process_node(node_file, "m", False, False, db) == "generated"
    (entry,) = db.values()
    assert entry["chunk_summaries"] == {"1": "Covers the ladder."}
    # And the surviving entry must itself be satisfied — otherwise the drop
    # merely moved the churn one run later.
    rel = next(iter(db))
    assert gnf.has_formats(rel, db, entry["content_hash"]) is True


# ═══════════════════════════════════════════════════════════════════════════
# Breaker: the auth cooldown split (coordinator ruling)
# ═══════════════════════════════════════════════════════════════════════════

def test_auth_demotes_for_much_less_time_than_a_rate_limit(ladder):
    """`auth` still trips on the FIRST occurrence — retrying a bad
    credential cannot succeed — but the user IS the fix and expects the next
    node to use the tier again. A rate limit they cannot fix, so its long
    cooldown stands."""
    assert ladder.REASON_AUTH in ladder.TRIPPING_REASONS
    auth = ladder.cooldown_for(ladder.REASON_AUTH)
    rate = ladder.cooldown_for(ladder.REASON_RATE_LIMIT)
    assert auth == 120.0
    assert rate == 900.0
    assert auth < rate


def test_the_auth_cooldown_knob_changes_something_observable(ladder, monkeypatch):
    """R24: a declared env var gets a reader and a test that it matters."""
    monkeypatch.setenv(ladder.ENV_BREAKER_AUTH_COOLDOWN, "45")
    assert ladder.cooldown_for(ladder.REASON_AUTH) == 45.0
    # ... and does not bleed into the neighbouring reasons.
    assert ladder.cooldown_for(ladder.REASON_RATE_LIMIT) == 900.0
    assert ladder.cooldown_for(ladder.REASON_CAPACITY) == 60.0


def test_a_junk_auth_cooldown_falls_back_to_the_default(ladder, monkeypatch):
    monkeypatch.setenv(ladder.ENV_BREAKER_AUTH_COOLDOWN, "not-a-number")
    assert ladder.cooldown_for(ladder.REASON_AUTH) == 120.0


# ═══════════════════════════════════════════════════════════════════════════
# The knobs are documented, and the documentation is real (R24, both ways)
# ═══════════════════════════════════════════════════════════════════════════

def test_every_breaker_knob_is_documented_and_every_documented_knob_is_read(ladder):
    """A knob nobody documents is unusable; a documented knob nobody reads is
    a false promise. Assert the two sets are EQUAL, so adding one without
    the other fails here."""
    source = Path(ladder.__file__).read_text(encoding="utf-8")
    declared = {
        getattr(ladder, name)
        for name in dir(ladder)
        if name.startswith("ENV_BREAKER_")
    }
    assert declared, "no breaker knobs found — did the constants get renamed?"
    for knob in declared:
        assert source.count(knob) >= 2, f"{knob} is declared but never read"

    doc = CONFIG_DOC.read_text(encoding="utf-8")
    documented = set(re.findall(r"VCO_SUMMARY_BREAKER[A-Z_]*", doc))
    assert documented == declared, (
        "docs/CONFIGURATION.md and summary_backends.py disagree about the "
        f"breaker knobs: only in docs {documented - declared}, "
        f"only in code {declared - documented}"
    )


def test_the_documented_defaults_match_the_code(ladder):
    doc = CONFIG_DOC.read_text(encoding="utf-8")
    for knob, default in (
        (ladder.ENV_BREAKER_COOLDOWN, "900"),
        (ladder.ENV_BREAKER_AUTH_COOLDOWN, "120"),
        (ladder.ENV_BREAKER_CAPACITY_COOLDOWN, "60"),
        (ladder.ENV_BREAKER_CAPACITY_STRIKES, "3"),
    ):
        row = next(ln for ln in doc.splitlines() if knob in ln)
        assert f"`{default}`" in row, f"{knob} row does not state default {default}"
    assert ladder.DEFAULT_BREAKER_COOLDOWN_S == 900.0
    assert ladder.DEFAULT_AUTH_COOLDOWN_S == 120.0
    assert ladder.DEFAULT_CAPACITY_COOLDOWN_S == 60.0
    assert ladder.DEFAULT_CAPACITY_STRIKES == 3

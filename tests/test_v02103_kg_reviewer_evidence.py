# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — the post-commit KG reviewer may not inflate evidence levels or
mint same-subject duplicate nodes.

Field report (2026-10-16, a user project):

1. the reviewer upgraded a claim to "... (CONFIRMED in <tool> via test run
   20261015-221000)" while every source it read said "inferred, untested" —
   an invented evidence level attributed to a specific run;
2. it created a new node (another folder, another title) on the same subject
   as an existing node.

The guard (``vco_lib/kg_reviewer_guard.py``) now REVERTS both, judged against
the same read corpus as the invented-flag rule (Read/Grep results + the
commit diff/stat + the commit's sha and its parent's). Every test drives the
real guard through ``validate`` (the replay entry the backend calls) or the
backend's ``run`` with a FAKED ``claude`` CLI; Weaviate is the unroutable
sentinel the suite pins. Example text is generic.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import kg_reviewer_guard as guard  # noqa: E402
from vco_lib import kg_reviewer_prompt as prompt_mod  # noqa: E402

_REAL_RUN = subprocess.run

CACHE_NODE = """---
title: Cache layer
type: concept
tags: [cache]
status: active
updated: 2026-01-01T00:00:00Z
---

# Cache layer

The cache keeps 100 entries.
Eviction on full is inferred, untested.
"""


BACKUP_BODY = "Backups run nightly via the snapshot API."
DOCKER_BODY = ("Containers start at login through the user service unit; "
               "restart policy stays unless-stopped.")


def _node(title: str, body: str = "Fact.") -> str:
    return (f"---\ntitle: {title}\ntype: concept\ntags: [x]\n"
            f"created: 2026-10-16\nupdated: 2026-10-16\nstatus: active\n---\n\n"
            f"# {title}\n\n{body}\n")


def _git(repo: Path, *args: str) -> str:
    return _REAL_RUN(["git", *args], cwd=repo, check=True,
                     capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k, v in {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)
    r = tmp_path / "proj"
    (r / "knowledge" / "concepts").mkdir(parents=True)
    (r / "knowledge" / "tools").mkdir(parents=True)
    (r / "knowledge" / "concepts" / "cache-layer.md").write_text(CACHE_NODE)
    (r / "knowledge" / "tools" / "weaviate-backups.md").write_text(
        _node("Weaviate backups", BACKUP_BODY))
    (r / "knowledge" / "concepts" / "docker-auto-start.md").write_text(
        _node("Docker auto-start", DOCKER_BODY))
    (r / "knowledge" / "concepts" / "debug-overlay-hotkey.md").write_text(
        _node("Debug overlay hotkey"))
    (r / "knowledge" / "concepts" / "embedding-model-selection.md").write_text(
        _node("Embedding model selection"))
    (r / "src.py").write_text("x = 1\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    (r / "src.py").write_text("x = 2  # verified by the parity check\n")
    _git(r, "commit", "-q", "-am", "feat: bump x")
    return r


def _key(repo: Path, rel: str) -> Path:
    return guard.path_key(repo / rel, repo.resolve())


def _reviewer_write(repo: Path, writes: dict, rel: str, content: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content)
    writes.setdefault(_key(repo, rel), []).append(("write", content))


def _validate(repo: Path, writes: dict, sources: str,
              before: Optional[str] = None):
    """Snapshot, apply ``writes`` (rel -> content), validate."""
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        for rel, content in writes.items():
            _reviewer_write(repo, w, rel, content)
        lines: List[str] = []
        res = guard.validate(snap, w, lines.append, resync=lambda _r, _h: None,
                             sources=sources)
        return res, lines
    finally:
        snap.cleanup()


# stream-json helpers (the fake CLI's transcript)


def _tool_use(i: int, name: str, **inp) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}


def _tool_result(i: int, text: str) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{i}", "content": text}]}}


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in (
        {"type": "system", "subtype": "init"}, *events,
        {"type": "result", "subtype": "success", "num_turns": 3, "result": "ok"}))


CACHE = "knowledge/concepts/cache-layer.md"
FIELD_LINE = ("Eviction on full is CONFIRMED in the app via the dev-mode test "
              "run 20261015-221000.")


# ═════════════════════════════════════════════════════════════════════════
# 1. Evidence-level inflation
# ═════════════════════════════════════════════════════════════════════════


def test_inflated_label_over_an_untested_source_is_reverted(repo):
    """The field case: the source says "inferred, untested"; the reviewer
    adds CONFIRMED + a run id it read nowhere. RED before the rule: kept."""
    src = guard.transcript_sources(_stream(
        _tool_use(1, "Read", file_path="docs/cache.md"),
        _tool_result(1, "Eviction on full: inferred, untested.")))
    res, lines = _validate(repo, {CACHE: CACHE_NODE + FIELD_LINE + "\n"}, src)
    assert res.kept == [], res.kept
    assert (repo / CACHE).read_text() == CACHE_NODE
    (name, reason), = res.reverted
    assert name == CACHE
    assert "marker 'confirmed'" in reason and "id '20261015-221000'" in reason
    assert any(ln.startswith(f"REVERTED {CACHE}: raised an evidence/confidence "
                             "label") and "CONFIRMED in the app" in ln
               for ln in lines), lines


def test_label_present_in_a_read_source_is_kept(repo):
    src = guard.transcript_sources(_stream(
        _tool_use(1, "Read", file_path="docs/cache.md"),
        _tool_result(1, "    7\tEviction on full was verified by the soak "
                        "suite (docs/cache.md).")))
    post = CACHE_NODE + "Eviction on full was Verified by the soak suite.\n"
    res, _l = _validate(repo, {CACHE: post}, src)
    assert res.kept == [CACHE] and res.reverted == []


def test_hybrid_search_text_is_not_a_source_for_a_label(repo):
    src = guard.transcript_sources(_stream(
        _tool_use(1, "mcp__weaviate-kg__hybrid_search", query="cache"),
        _tool_result(1, "an old node says eviction is verified")))
    post = CACHE_NODE + "Eviction on full is verified.\n"
    res, _l = _validate(repo, {CACHE: post}, src)
    assert res.kept == [] and "marker 'verified'" in res.reverted[0][1]


def test_invented_run_id_is_reverted_even_with_a_sourced_marker(repo):
    src = "the soak suite confirmed eviction on full (run 20261001-090000)"
    post = CACHE_NODE + "Eviction on full confirmed in run 20261015-221000.\n"
    res, _l = _validate(repo, {CACHE: post}, src)
    assert res.kept == []
    reason = res.reverted[0][1]
    assert "id '20261015-221000'" in reason and "marker" not in reason


def test_run_id_present_in_the_diff_is_kept(repo):
    diff = ("+++ b/docs/cache.md\n+Eviction on full: confirmed in test run "
            "20261015-221000.\n")
    post = CACHE_NODE + "Eviction on full is confirmed (test run 20261015-221000).\n"
    res, _l = _validate(repo, {CACHE: post}, diff)
    assert res.kept == [CACHE] and res.reverted == []


def test_invented_commit_hash_is_reverted_and_a_read_short_sha_is_kept(repo):
    full = "4f9c2a1be0d3c7e6a5b4f3e2d1c0b9a8f7e6d5c4"
    src = f"commit {full}\nverified on the staging box\n"
    ok = CACHE_NODE + "Verified at 4f9c2a1 on the staging box.\n"
    res, _l = _validate(repo, {CACHE: ok}, src)
    assert res.kept == [CACHE], res.reverted
    (repo / CACHE).write_text(CACHE_NODE)
    bad = CACHE_NODE + "Verified at 9e8d7c6b on the staging box.\n"
    res, _l = _validate(repo, {CACHE: bad}, src)
    assert res.kept == [] and "id '9e8d7c6b'" in res.reverted[0][1]


def test_pre_existing_marker_line_is_not_judged_even_when_annotated(repo):
    """LEAVE-ALONE: a line that already carried the marker (unsourced in this
    run) survives untouched or annotated in place; only added text counts."""
    pre = CACHE_NODE + "Status: CONFIRMED in run 20250101-000000.\n"
    (repo / CACHE).write_text(pre)
    _git(repo, "commit", "-q", "-am", "docs: node")
    post = (CACHE_NODE + "~~Status: CONFIRMED in run 20250101-000000.~~ "
            "(superseded, see below)\nThe cache now keeps 200 entries.\n")
    res, _l = _validate(repo, {CACHE: post}, sources="")
    assert res.kept == [CACHE] and res.reverted == []


def test_pre_existing_marker_line_left_verbatim_is_not_judged(repo):
    pre = CACHE_NODE + "Status: CONFIRMED in run 20250101-000000.\n"
    (repo / CACHE).write_text(pre)
    _git(repo, "commit", "-q", "-am", "docs: node")
    res, _l = _validate(repo, {CACHE: pre + "The cache now keeps 200 entries.\n"},
                        sources="")
    assert res.kept == [CACHE] and res.reverted == []


def test_untested_and_neutral_lines_are_not_markers(repo):
    post = CACHE_NODE + ("Eviction remains untested; it is unverified.\n"
                         "Tests live in tests/test_cache.py (run 42).\n")
    res, _l = _validate(repo, {CACHE: post}, sources="")
    assert res.kept == [CACHE] and res.reverted == []


def test_new_node_with_an_unsourced_label_is_reverted(repo):
    rel = "knowledge/concepts/queue-retry.md"
    res, _l = _validate(repo, {rel: _node("Queue retry policy",
                                          "Retry backoff is PROVEN stable.")}, "")
    assert not (repo / rel).exists()
    assert "marker 'proven'" in res.reverted[0][1]


@pytest.mark.parametrize("text, ids", [
    ("via test run 20261015-221000.", ["20261015-221000"]),
    ("run #a7 and job 42", ["a7", "42"]),
    ("build id: 1234abcd", ["1234abcd"]),
    ("at 4f9c2a1, stamp 20261015T2210Z", ["4f9c2a1", "20261015T2210Z"]),
    ("decade facade deadbeef 1234567 test run", []),
    # R30 L1: a version after "build" is a version, not a run id
    ("Validated in build 0.2.103 and build v1.4.", []),
])
def test_evidence_id_shapes(text, ids):
    assert guard.evidence_ids(text) == ids


# ── the commit's own sha and its parent count as read (backend wiring) ──


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sb = _load("_v02103ev_summary_backends", SCRIPTS_DIR / "summary_backends.py")
    sb.reset_backend_cache()
    monkeypatch.setattr(sb, "_breaker_path", lambda: tmp_path / "breaker.json")
    mod = _load("_v02103ev_kg_reviewer_backend",
                SCRIPTS_DIR / "kg_reviewer_backend.py")
    setattr(mod, "_sb", sb)
    for k in ("VCO_KG_REVIEW_MAX_TURNS", "VCO_KG_REVIEW_MODEL"):
        monkeypatch.delenv(k, raising=False)
    yield mod
    sb.reset_backend_cache()


def _run(chain, monkeypatch, repo: Path, appended: str) -> str:
    node = repo / CACHE
    text = CACHE_NODE + appended

    def run(cmd, *a, **kw):
        if cmd and cmd[0] == "claude":
            node.write_text(text)
            out = _stream(_tool_use(1, "Write", file_path=str(node), content=text))
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return _REAL_RUN(cmd, *a, **kw)

    monkeypatch.setattr(chain, "subprocess", types.SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess,
        DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE))
    monkeypatch.setattr(chain, "resolve_claude_executable", lambda: "claude")
    monkeypatch.setattr(chain, "select_reviewer_backend",
                        lambda log=None, model=None: {
                            "mode": "claude", "model": "sonnet",
                            "base_url": None, "reason": "t"})
    monkeypatch.setattr(chain, "_record_outcome", lambda *a, **k: None)
    log = repo.parent / "review.log"
    chain.main(["run", "--project-root", str(repo), "--log", str(log)])
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_collect_commit_records_the_parent(repo):
    info = prompt_mod.collect_commit(repo)
    assert info.parent == _git(repo, "rev-parse", "HEAD~1").strip()


def test_run_counts_the_parent_sha_as_read(chain, monkeypatch, repo):
    """The diff says "verified"; the node cites the PARENT commit, which is in
    no diff line — only the commit identity the backend hands the guard.
    RED when the backend leaves the parent out of the sources.

    v0.2.105: the claim names the diff's subject ("the parity check"), the
    passage a marker has to come from matches on."""
    parent = _git(repo, "rev-parse", "HEAD~1").strip()
    log = _run(chain, monkeypatch, repo,
               f"The parity check verified x against {parent} before this "
               "change.\n")
    assert "REVERTED" not in log, log
    files = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert files == [CACHE], log


def test_run_reverts_an_invented_sha(chain, monkeypatch, repo):
    log = _run(chain, monkeypatch, repo,
               "The parity check verified x against 0badc0de1 before this "
               "change.\n")
    assert f"REVERTED {CACHE}: raised an evidence" in log, log
    assert "id '0badc0de1'" in log
    assert (repo / CACHE).read_text() == CACHE_NODE


# ═════════════════════════════════════════════════════════════════════════
# 2. Same-subject duplicate nodes
# ═════════════════════════════════════════════════════════════════════════

# (existing title, new title, TRIGGERS the rule). The threshold (0.6) sits
# between the closest near-miss and the loosest duplicate in this table. A
# trigger alone does not revert since R30 M2: see the body-overlap tests.
_PAIRS = [
    ("Weaviate backups", "Weaviate backup procedure", True),        # 2/3
    ("Debug overlay hotkey", "Hotkey for the debug overlay", True),  # 3/3
    ("Docker auto-start", "Docker container auto-start", True),     # 3/4
    ("Embedding model selection", "Embedding model migration", False),  # 2/4
    ("Docker auto-start", "Docker networking", False),              # 1/4
    ("Cache layer", "Queue retry policy", False),                   # 0
]


@pytest.mark.parametrize("existing, new, caught", _PAIRS)
def test_same_subject_threshold_table(existing, new, caught):
    score, _same = guard.subject_similarity(
        new, "knowledge/x/zz-unrelated-slug.md", "knowledge/a/node.md",
        guard._norm_title(existing))
    assert (score >= 0.6) is caught, (existing, new, score)


def test_subject_tokens_drop_dates_stopwords_and_fold_plurals():
    assert guard.subject_tokens("2026-10-16 The Weaviate Backups of a DB") == {
        "weaviate", "backup", "db"}


def test_near_duplicate_new_node_is_reverted_naming_the_existing_node(repo):
    rel = "knowledge/concepts/backup-procedure.md"
    res, lines = _validate(repo, {rel: _node(
        "Weaviate backup procedure", BACKUP_BODY + " Retention is 7 days.")}, "")
    assert not (repo / rel).exists()
    reason = res.reverted[0][1]
    assert ("same subject as knowledge/tools/weaviate-backups.md" in reason
            and "extend knowledge/tools/weaviate-backups.md instead" in reason)
    assert any(ln.startswith(f"REVERTED {rel}") for ln in lines)


def test_near_duplicate_by_filename_slug_is_reverted(repo):
    rel = "knowledge/tools/docker-auto-start-on-boot.md"
    res, _l = _validate(repo, {rel: _node("Boot sequence", DOCKER_BODY)}, "")
    assert not (repo / rel).exists()
    assert "knowledge/concepts/docker-auto-start.md" in res.reverted[0][1]


@pytest.mark.parametrize("rel, title", [
    ("knowledge/concepts/docker-networking.md", "Docker networking"),
    ("knowledge/concepts/embedding-model-migration.md", "Embedding model migration"),
])
def test_distinct_subject_new_node_is_kept(repo, rel, title):
    res, _l = _validate(repo, {rel: _node(title)}, "")
    assert res.kept == [rel] and res.reverted == []


def test_prompt_states_both_rules(repo):
    text = prompt_mod.build_prompt(prompt_mod.collect_commit(repo), repo)
    assert "Never raise a confidence or evidence level" in text
    assert "never cite a test run, run\n   id, build or commit as evidence" in text
    # R30 L5: the prompt names the corpus the guard actually accepts; v0.2.105
    # adds the passage tie the guard now enforces.
    assert "in the same passage as the fact" in text
    assert "its commit stat or sha, or a\n   Read/Grep result from this run" in text
    assert "look for an\n   existing node on the same subject in ANY folder" in text


# ═════════════════════════════════════════════════════════════════════════
# 3. R30 review regressions (GLM-5.3 verify round, 2026-10-16)
# ═════════════════════════════════════════════════════════════════════════
#
# M1 — a marker word is an evidence CLAIM only with an attribution shape
# (uppercase label / followed by in|via|by|on|at|during / directly after
# is|are|was|were|be|been / on a line citing a run id or hash); fenced code
# and frontmatter are never judged.


def _read_source(text: str, i: int = 1, path: str = "docs/cache.md") -> str:
    return guard.transcript_sources(_stream(
        _tool_use(i, "Read", file_path=path), _tool_result(i, text)))


def test_r30_field_wording_still_reverted(repo):
    src = _read_source("Eviction on full: inferred, untested (game).")
    post = CACHE_NODE + ("Eviction on full is CONFIRMED in game via test run "
                         "20261015-221000.\n")
    res, _l = _validate(repo, {CACHE: post}, src)
    assert res.kept == [] and res.reverted
    assert "marker 'confirmed'" in res.reverted[0][1]
    assert "id '20261015-221000'" in res.reverted[0][1]
    assert (repo / CACHE).read_text() == CACHE_NODE


@pytest.mark.parametrize("added", [
    "The tested path keeps the cache warm between restarts.",
    "## Validated configuration keys",
    "Eviction is not tested yet.",
    "Run the validated config through the loader first.",
], ids=["prose-adjective", "heading", "negated", "prose-object"])
def test_r30_marker_word_in_prose_is_kept(repo, added):
    """RED before R30 M1: each line reverted the whole write."""
    post = CACHE_NODE + added + "\n"
    res, _l = _validate(repo, {CACHE: post}, _read_source("the cache module"))
    assert res.kept == [CACHE] and res.reverted == [], res.reverted


@pytest.mark.parametrize("added, marker", [
    ("Eviction VERIFIED.", "verified"),
    ("Eviction confirmed in staging.", "confirmed"),
    ("Eviction tested via the soak suite.", "tested"),
    ("Eviction proven by the soak suite.", "proven"),
    ("Eviction validated on the staging box.", "validated"),
    ("Eviction verified at deploy time.", "verified"),
    ("Eviction confirmed during the soak.", "confirmed"),
    ("Eviction on full is verified.", "verified"),
    ("Eviction has been tested.", "tested"),
    ("## Verified in staging", "verified"),
], ids=lambda v: v if len(v) < 14 else v[:24])
def test_r30_attributed_claim_without_source_is_reverted(repo, added, marker):
    post = CACHE_NODE + added + "\n"
    res, _l = _validate(repo, {CACHE: post}, _read_source("the cache module"))
    assert res.kept == [] and f"marker '{marker}'" in res.reverted[0][1]


def test_r30_lowercase_marker_with_an_id_is_a_claim(repo):
    post = CACHE_NODE + "Eviction tested, see run 20261015-221000.\n"
    res, _l = _validate(repo, {CACHE: post}, _read_source("the cache module"))
    assert res.kept == []
    assert "marker 'tested'" in res.reverted[0][1]
    assert "id '20261015-221000'" in res.reverted[0][1]


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_r30_fenced_code_is_not_judged(repo, fence):
    post = CACHE_NODE + (f"{fence}python\nassert tested_path(repo)\n"
                         f"# CONFIRMED in run 20261015-221000\n{fence}\n")
    res, _l = _validate(repo, {CACHE: post}, _read_source("the test module"))
    assert res.kept == [CACHE] and res.reverted == [], res.reverted


def test_r30_a_claim_after_a_closed_fence_is_judged(repo):
    post = CACHE_NODE + "```\nx = 1\n```\nEviction CONFIRMED in staging.\n"
    res, _l = _validate(repo, {CACHE: post}, _read_source("the test module"))
    assert res.kept == [] and "marker 'confirmed'" in res.reverted[0][1]


def test_r30_new_node_frontmatter_and_title_heading_are_not_claims(repo):
    rel = "knowledge/concepts/validated-config-keys.md"
    res, _l = _validate(repo, {rel: _node("Validated config keys",
                                          "Keys are read from the table.")}, "")
    assert res.kept == [rel] and res.reverted == [], res.reverted


def test_r30_new_node_frontmatter_is_not_judged(repo):
    """The frontmatter is skipped as a whole (a key is a name, not a claim) —
    proven with a claim-shaped value that only the skip lets through."""
    rel = "knowledge/concepts/queue-sizing.md"
    text = ("---\ntitle: Queue sizing\ntype: concept\ntags: [x]\n"
            "created: 2026-10-16\nupdated: 2026-10-16\nstatus: active\n"
            "summary: CONFIRMED in run 20261015-221000\n---\n\n"
            "# Queue sizing\n\nThe queue holds 64 items.\n")
    res, _l = _validate(repo, {rel: text}, "")
    assert res.kept == [rel] and res.reverted == [], res.reverted


def test_r30_corpus_word_sources_a_prose_use(repo):
    src = _read_source("# keep tested\nx = 1\n")
    post = CACHE_NODE + "The tested path keeps the cache warm.\n"
    res, _l = _validate(repo, {CACHE: post}, src)
    assert res.kept == [CACHE]


def test_r30_lowercase_corpus_marker_sources_uppercase_label(repo):
    src = _read_source("soak suite verified eviction on full")
    post = CACHE_NODE + "Eviction VERIFIED by the soak suite.\n"
    res, _l = _validate(repo, {CACHE: post}, src)
    assert res.kept == [CACHE]


def test_r30_extension_raising_the_level_is_still_reverted(repo):
    pre = CACHE_NODE + "Status: CONFIRMED in run 20250101-000000.\n"
    (repo / CACHE).write_text(pre)
    _git(repo, "commit", "-q", "-am", "docs: node")
    post = (CACHE_NODE + "Status: CONFIRMED in run 20250101-000000. "
            "(also proven in staging)\n")
    res, _l = _validate(repo, {CACHE: post}, sources=_read_source("confirmed"))
    assert res.kept == [] and "marker 'proven'" in res.reverted[0][1]


# L1 / id shapes


@pytest.mark.parametrize("added, src", [
    ("Verified on 2026-10-15 in v0.2.103.", "verified on the staging box"),
    ("Validated in build 0.2.103.", "validated in CI"),
    ("Confirmed: deadbeef on port 8081 stays deadbeef.", "confirmed by inspection"),
    ("Verified request 550e8400-e29b-41d4-a716-446655440000.",
     "request 550e8400-e29b-41d4-a716-446655440000 verified ok"),
], ids=["iso-date-version", "build-version", "deadbeef-port", "uuid"])
def test_r30_non_id_shapes_are_kept(repo, added, src):
    """``build 0.2.103`` reverted with ``id '0.2.103'`` before R30 L1."""
    res, _l = _validate(repo, {CACHE: CACHE_NODE + added + "\n"},
                        _read_source(src))
    assert res.kept == [CACHE], res.reverted


def test_r30_hex_word_is_an_id_only_when_unsourced(repo):
    res, _l = _validate(repo, {CACHE: CACHE_NODE + "Confirmed token cafe1234.\n"},
                        _read_source("confirmed; token cafe1234 in config"))
    assert res.kept == [CACHE]
    (repo / CACHE).write_text(CACHE_NODE)
    res2, _l = _validate(repo, {CACHE: CACHE_NODE + "Confirmed token cafe1234.\n"},
                         _read_source("confirmed"))
    assert res2.kept == [] and "id 'cafe1234'" in res2.reverted[0][1]


def test_r30_full_sha_needs_the_full_sha_read(repo):
    """Documented asymmetry: a short sha in the node matches a full one read;
    a full sha in the node needs the full sha in the corpus."""
    src = _read_source("commit 4f9c2a1 touched the cache (verified)")
    post = CACHE_NODE + "Verified at 4f9c2a1be0d3c7e6a5b4f3e2d1c0b9a8f7e6d5c4.\n"
    res, _l = _validate(repo, {CACHE: post}, src)
    assert res.kept == [] and res.reverted[0][1].count("id '") == 1


def test_r30_run_id_on_neutral_and_marker_lines(repo):
    res, _l = _validate(repo, {CACHE: CACHE_NODE + "Tests live in "
                               "tests/test_cache.py (run 42).\n"}, "")
    assert res.kept == [CACHE]
    (repo / CACHE).write_text(CACHE_NODE)
    res2, _l = _validate(repo, {CACHE: CACHE_NODE + "Verified in CI (run 42).\n"},
                         _read_source("verified in CI"))
    assert res2.kept == [] and "id '42'" in res2.reverted[0][1]


# corpus completeness


def test_r30_grep_and_numbered_read_results_are_sources(repo):
    src = guard.transcript_sources(_stream(
        _tool_use(1, "Grep", pattern="evict", path="src"),
        _tool_result(1, "src/cache.py:7: eviction verified by soak")))
    res, _l = _validate(repo, {CACHE: CACHE_NODE + "Eviction verified by soak.\n"},
                        src)
    assert res.kept == [CACHE]
    (repo / CACHE).write_text(CACHE_NODE)
    res2, _l = _validate(repo, {CACHE: CACHE_NODE + "Eviction verified by soak.\n"},
                         _read_source("    7\teviction verified by soak"))
    assert res2.kept == [CACHE]


def test_r30_glob_and_hybrid_results_are_not_sources(repo):
    src = guard.transcript_sources(_stream(
        _tool_use(1, "Glob", pattern="knowledge/**/*.md"),
        _tool_result(1, "knowledge/concepts/cache-layer.md"),
        _tool_use(2, "mcp__weaviate-kg__hybrid_search", query="cache"),
        _tool_result(2, "old node: eviction verified in production")))
    post = CACHE_NODE + "Eviction verified in production.\n"
    res, _l = _validate(repo, {CACHE: post}, src)
    assert res.kept == [] and "marker 'verified'" in res.reverted[0][1]


# M2 — the title trigger needs a retitle or real body overlap to revert


def test_r30_subtopic_with_its_own_content_is_kept(repo):
    """RED before R30 M2: reverted as "same subject" (title Jaccard 0.67)."""
    rel = "knowledge/concepts/cache-layer-eviction.md"
    body = ("Eviction picks the oldest entry first.\n"
            "Tuning goes through the cache.knobs table.\n")
    res, _l = _validate(repo, {rel: _node("Cache layer eviction", body)}, "")
    assert res.kept == [rel] and res.reverted == [], res.reverted


def test_r30_generic_title_next_to_a_specific_node_is_kept(repo):
    (repo / "knowledge/concepts/error-handling-launcher.md").write_text(
        _node("Error handling in the launcher",
              "The launcher shows a toast and writes the log line."))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs: eh")
    rel = "knowledge/concepts/error-handling.md"
    res, _l = _validate(repo, {rel: _node(
        "Error handling", "Errors propagate as typed results to the caller.")}, "")
    assert res.kept == [rel] and res.reverted == [], res.reverted


def test_r30_retitle_with_identical_words_is_reverted(repo):
    rel = "knowledge/reports/backups-weaviate.md"
    res, _l = _validate(repo, {rel: _node("Backups of Weaviate",
                                          "Unrelated wording entirely.")}, "")
    assert res.kept == [] and "the same title/slug words" in res.reverted[0][1]


def test_r30_body_overlap_threshold_boundary():
    """Body tokens: frontmatter dropped, stopwords / all-digit runs / tokens
    under 3 characters dropped. 4 shared tokens trigger nothing, 5 do."""
    nodes = {"knowledge/a/alpha-beta.md": "alpha beta"}
    existing = guard.body_tokens(_node("Alpha beta",
                                       "gamma delta epsilon zeta 2026 of ab"))
    assert existing == {"alpha", "beta", "gamma", "delta", "epsilon", "zeta"}
    four = guard.body_tokens("---\ntitle: x\n---\nalpha beta gamma delta\n")
    five = guard.body_tokens("---\ntitle: x\n---\nalpha beta gamma delta zeta\n")
    bodies = {"knowledge/a/alpha-beta.md": existing}
    assert guard.same_subject_node("Alpha beta notes", "knowledge/b/n.md", nodes,
                                   body=four, bodies=bodies) is None
    hit = guard.same_subject_node("Alpha beta notes", "knowledge/b/n.md", nodes,
                                  body=five, bodies=bodies)
    assert hit is not None and hit[0] == "knowledge/a/alpha-beta.md"
    assert "bodies share 5 content tokens" in hit[2]


def test_r30_new_title_against_existing_slug_is_compared(repo):
    """L4: an existing node with NO frontmatter title is reached through its
    slug, by the new node's TITLE (and the new slug by an existing title)."""
    (repo / "knowledge/concepts/release-checklist.md").write_text(
        "# notes\n\n" + "Tag on main after the full suite passes; publish assets.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs: rc")
    rel = "knowledge/tools/steps.md"
    res, _l = _validate(repo, {rel: _node(
        "Release checklist steps",
        "Tag on main after the full suite passes; publish assets.")}, "")
    assert res.kept == [] and "knowledge/concepts/release-checklist.md" in \
        res.reverted[0][1]
    score, _same = guard.subject_similarity(
        "Unrelated", "knowledge/x/weaviate-backup-procedure.md",
        "knowledge/tools/a.md", "weaviate backups")
    assert score >= 0.6


def test_r30_second_near_duplicate_new_node_in_one_run_is_reverted(repo):
    """L3: RED before the fix — both kept (the comparison set held only the
    nodes present at the snapshot)."""
    body = "Restore drills run weekly against a scratch cluster with checksums."
    n1 = "knowledge/reports/restore-drill.md"
    n2 = "knowledge/tools/restore-drill-notes.md"
    res, _l = _validate(repo, {n1: _node("Restore drill", body),
                               n2: _node("Restore drill notes", body)}, "")
    assert res.kept == [n1], (res.kept, res.reverted)
    assert res.reverted[0][0] == n2 and n1 in res.reverted[0][1]
    assert not (repo / n2).exists()


def test_r30_two_distinct_new_nodes_in_one_run_are_both_kept(repo):
    n1 = "knowledge/reports/restore-drill.md"
    n2 = "knowledge/tools/restore-drill-notes.md"
    res, _l = _validate(repo, {
        n1: _node("Restore drill", "Drills run weekly on a scratch cluster."),
        n2: _node("Restore drill notes", "Operators record timings by hand.")}, "")
    assert sorted(res.kept) == sorted([n1, n2])


def test_r30_prompt_states_the_duplicate_rule_as_enforced(repo):
    text = prompt_mod.build_prompt(prompt_mod.collect_commit(repo), repo)
    assert "repeating its content, is reverted" in text
    assert "A distinct subtopic with its own content is allowed." in text


# M3 — a reverted NEW node's KG rows are removed, not left behind


def test_r30_resync_passes_prune_missing_only_for_gone_paths(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".claude" / "scripts").mkdir(parents=True)
    (root / ".claude" / "scripts" / "sync_knowledge_graph.py").write_text("#\n")
    (root / "knowledge").mkdir()
    (root / "knowledge" / "kept.md").write_text("x\n")
    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    lines: List[str] = []
    resync = guard.resync_with(root, lines.append)
    resync(["knowledge/kept.md"])
    assert "--prune-missing" not in calls[-1]
    resync(["knowledge/kept.md", "knowledge/gone.md"])
    assert "--prune-missing" in calls[-1]
    assert str(root / "knowledge" / "gone.md") in calls[-1]
    # R33 N1: the stub printed no result line, so no removal is claimed.
    assert "removed" not in lines[-1].replace("NOT confirmed", "")
    assert "knowledge/gone.md: removal NOT confirmed" in lines[-1]


def test_r30_resync_failure_is_logged_not_raised(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".claude" / "scripts").mkdir(parents=True)
    (root / ".claude" / "scripts" / "sync_knowledge_graph.py").write_text("#\n")
    monkeypatch.setattr(guard.subprocess, "run", lambda cmd, **kw:
                        subprocess.CompletedProcess(
                            cmd, 1, stdout="", stderr="row removal failed"))
    lines: List[str] = []
    guard.resync_with(root, lines.append)(["knowledge/gone.md"])
    assert "exited 1" in lines[-1] and "row removal failed" in lines[-1]


@pytest.fixture()
def sync_mod(tmp_path, monkeypatch):
    from tests.test_v0270_kg_sync_batch_title_collision import (
        _FakeServer, _load_sync_module)
    for key in ("KG_BASE_DIR", "KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                "DUAL_EMBEDDING_ENABLED", "VCT_DISABLE_HUB_RESOLVER"):
        monkeypatch.setenv(key, "placeholder")
    mod = _load_sync_module(tmp_path)
    return mod, _FakeServer(), tmp_path


def _seed_row(mod, server, rel: str) -> None:
    server.client.collections.get(mod.COLLECTION_NAME).data.insert(
        properties={"title": "T", "file_path": rel})


def _rows(mod, server) -> list:
    store = server.client.collections.get(mod.COLLECTION_NAME)._store
    return sorted(o.properties.get("file_path") for o in store.values())


def test_r30_sync_prune_missing_deletes_the_vanished_nodes_rows(sync_mod, monkeypatch):
    mod, server, root = sync_mod
    gone = "knowledge/concepts/gone-node.md"
    _seed_row(mod, server, gone)
    _seed_row(mod, server, "knowledge/concepts/gone-node-sibling.md")
    deleted: list = []
    real = mod._delete_node_by_file_path

    def recording(srv, fp, on_error=None, **kw):
        deleted.append(fp)
        return real(srv, fp, on_error=on_error, **kw)

    monkeypatch.setattr(mod, "_delete_node_by_file_path", recording)
    monkeypatch.setattr(mod, "_PRUNE_MISSING", True)
    outcome = mod.sync_node(server, root / gone)
    assert outcome and outcome.status == mod.OUTCOME_EXCLUDED_SKIPPED
    assert deleted == [gone]
    assert _rows(mod, server) == ["knowledge/concepts/gone-node-sibling.md"]


def test_r30_sync_without_prune_missing_still_fails_and_deletes_nothing(sync_mod):
    """LEAVE-ALONE: a missing target without the flag is a failure, never a
    delete (a typo'd path must not remove rows)."""
    mod, server, root = sync_mod
    gone = "knowledge/concepts/gone-node.md"
    _seed_row(mod, server, gone)
    assert mod._PRUNE_MISSING is False
    outcome = mod.sync_node(server, root / gone)
    assert outcome.status == mod.OUTCOME_FAILED
    assert _rows(mod, server) == [gone]


def test_r30_sync_prune_missing_weaviate_down_is_a_logged_failure(
        sync_mod, monkeypatch, capsys):
    mod, server, root = sync_mod

    class _Down:
        def get(self, _name):
            raise ConnectionError("weaviate unreachable")

    server.client.collections = _Down()
    monkeypatch.setattr(mod, "_PRUNE_MISSING", True)
    outcome = mod.sync_node(server, root / "knowledge/concepts/gone-node.md")
    assert outcome.status == mod.OUTCOME_FAILED
    assert "weaviate unreachable" in outcome.reason
    assert "could not be removed: weaviate unreachable" in capsys.readouterr().out


def test_r30_sync_prune_missing_keeps_rows_in_the_shared_collection(
        sync_mod, monkeypatch):
    mod, server, root = sync_mod
    gone = "knowledge/concepts/gone-node.md"
    _seed_row(mod, server, gone)
    monkeypatch.setattr(mod, "SHARED_COLLECTION_NAME", mod.COLLECTION_NAME)
    monkeypatch.setattr(mod, "_PRUNE_MISSING", True)
    outcome = mod.sync_node(server, root / gone)
    assert outcome and "shared collection" in outcome.reason
    assert _rows(mod, server) == [gone]


def test_r30_sync_extracts_the_prune_missing_flag(tmp_path, monkeypatch):
    """The flag is consumed at import (so flag validation never sees it) and
    arms the prune; a plain file list leaves it off."""
    from tests.test_v0270_kg_sync_batch_title_collision import _load_sync_module
    for key in ("KG_BASE_DIR", "KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                "DUAL_EMBEDDING_ENABLED", "VCT_DISABLE_HUB_RESOLVER"):
        monkeypatch.setenv(key, "placeholder")
    argv = ["sync_knowledge_graph.py", "--prune-missing", "knowledge/a.md",
            "--prune-missing"]
    monkeypatch.setattr(sys, "argv", argv)
    mod = _load_sync_module(tmp_path)
    assert mod._PRUNE_MISSING is True
    assert sys.argv == ["sync_knowledge_graph.py", "knowledge/a.md"]
    mod._validate_argv_flags(sys.argv)  # no exit: nothing left to reject
    monkeypatch.setattr(sys, "argv", ["sync_knowledge_graph.py", "knowledge/a.md"])
    assert _load_sync_module(tmp_path)._PRUNE_MISSING is False


# ═════════════════════════════════════════════════════════════════════════
# 4. R33 review fixes (GLM-5.3 verify round, 2026-10-16)
# ═════════════════════════════════════════════════════════════════════════

from vco_lib import kg_prune_result  # noqa: E402
from vco_lib.knowledge_residue import (  # noqa: E402
    content_signature_excluding_updated, content_signature_of_file_bytes)

# N1 — the orchestrator root (KG collection == shared collection) deletes a
# reverted new node's rows by exact path AND content_hash; every arm reports
# what happened, and the guard logs exactly that.

_H_REVIEWER = "a" * 64
_H_SIBLING = "b" * 64
GONE = "knowledge/concepts/gone-node.md"


def _seed_hashed(mod, server, rel: str, content_hash: str) -> None:
    server.client.collections.get(mod.COLLECTION_NAME).data.insert(
        properties={"title": "T", "file_path": rel,
                    "content_hash": content_hash})


def _hashed_rows(mod, server) -> list:
    store = server.client.collections.get(mod.COLLECTION_NAME)._store
    return sorted((o.properties.get("file_path"),
                   o.properties.get("content_hash")) for o in store.values())


def _result(capsys, rel: str = GONE) -> dict:
    rec = kg_prune_result.parse_lines(capsys.readouterr().out).get(rel)
    assert rec is not None, "the sync printed no result line"
    return rec


def _as_root(mod, monkeypatch) -> None:
    monkeypatch.setattr(mod, "SHARED_COLLECTION_NAME", mod.COLLECTION_NAME)
    monkeypatch.setattr(mod, "_PRUNE_MISSING", True)


def test_r33_root_prune_deletes_only_the_hash_matching_row(
        sync_mod, monkeypatch, capsys):
    """RED before R33 N1: the root kept every row (shared-identity skip)."""
    mod, server, root = sync_mod
    _seed_hashed(mod, server, GONE, _H_REVIEWER)   # the reviewer's write
    _seed_hashed(mod, server, GONE, _H_REVIEWER)   # its second chunk
    _seed_hashed(mod, server, GONE, _H_SIBLING)    # a sibling's shared row
    _seed_hashed(mod, server, "knowledge/concepts/gone-node-sibling.md",
                 _H_REVIEWER)                      # token-superset path
    _as_root(mod, monkeypatch)
    monkeypatch.setattr(mod, "_PRUNE_HASHES", {GONE: _H_REVIEWER})
    outcome = mod.sync_node(server, root / GONE)
    assert outcome and outcome.status == mod.OUTCOME_EXCLUDED_SKIPPED
    assert _hashed_rows(mod, server) == sorted([
        (GONE, _H_SIBLING),
        ("knowledge/concepts/gone-node-sibling.md", _H_REVIEWER)])
    rec = _result(capsys)
    assert (rec["status"], rec["deleted"], rec["kept"]) == (
        kg_prune_result.STATUS_DELETED, 2, 1)


def test_r33_root_prune_with_no_matching_hash_deletes_nothing(
        sync_mod, monkeypatch, capsys):
    mod, server, root = sync_mod
    _seed_hashed(mod, server, GONE, _H_SIBLING)
    _as_root(mod, monkeypatch)
    monkeypatch.setattr(mod, "_PRUNE_HASHES", {GONE: _H_REVIEWER})
    mod.sync_node(server, root / GONE)
    assert _hashed_rows(mod, server) == [(GONE, _H_SIBLING)]
    rec = _result(capsys)
    assert (rec["status"], rec["deleted"], rec["kept"]) == (
        kg_prune_result.STATUS_NOTHING_MATCHED, 0, 1)


def test_r33_root_prune_without_a_hash_is_reported_skipped(
        sync_mod, monkeypatch, capsys):
    mod, server, root = sync_mod
    _seed_hashed(mod, server, GONE, _H_REVIEWER)
    _as_root(mod, monkeypatch)
    monkeypatch.setattr(mod, "_PRUNE_HASHES", {})
    mod.sync_node(server, root / GONE)
    assert _hashed_rows(mod, server) == [(GONE, _H_REVIEWER)]
    rec = _result(capsys)
    assert rec["status"] == kg_prune_result.STATUS_SKIPPED
    assert "shared collection" in rec["reason"]


def test_r33_non_root_prune_is_unchanged_hash_or_not(
        sync_mod, monkeypatch, capsys):
    """Non-root: every exact-path row goes; a --prune-hash does not narrow
    it (nothing else writes to a per-project collection)."""
    mod, server, root = sync_mod
    _seed_hashed(mod, server, GONE, _H_REVIEWER)
    _seed_hashed(mod, server, GONE, _H_SIBLING)
    monkeypatch.setattr(mod, "_PRUNE_MISSING", True)
    monkeypatch.setattr(mod, "_PRUNE_HASHES", {GONE: _H_REVIEWER})
    mod.sync_node(server, root / GONE)
    assert _hashed_rows(mod, server) == []
    rec = _result(capsys)
    assert (rec["status"], rec["deleted"]) == (
        kg_prune_result.STATUS_DELETED, 2)


@pytest.mark.parametrize("as_root", [False, True], ids=["non-root", "root"])
def test_r33_prune_weaviate_down_reports_failed(sync_mod, monkeypatch, capsys,
                                                as_root):
    mod, server, root = sync_mod

    class _Down:
        def get(self, _name):
            raise ConnectionError("weaviate unreachable")

    server.client.collections = _Down()
    monkeypatch.setattr(mod, "_PRUNE_MISSING", True)
    monkeypatch.setattr(mod, "_PRUNE_HASHES", {GONE: _H_REVIEWER})
    if as_root:
        _as_root(mod, monkeypatch)
    outcome = mod.sync_node(server, root / GONE)
    assert outcome.status == mod.OUTCOME_FAILED
    rec = _result(capsys)
    assert rec["status"] == kg_prune_result.STATUS_FAILED
    assert "weaviate unreachable" in rec["reason"]


def test_r33_sync_extracts_prune_hashes(tmp_path, monkeypatch):
    from tests.test_v0270_kg_sync_batch_title_collision import _load_sync_module
    for key in ("KG_BASE_DIR", "KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                "DUAL_EMBEDDING_ENABLED", "VCT_DISABLE_HUB_RESOLVER"):
        monkeypatch.setenv(key, "placeholder")
    argv = ["sync_knowledge_graph.py", "--prune-missing", "--prune-hash",
            f"{GONE}={'A' * 64}", GONE]
    monkeypatch.setattr(sys, "argv", argv)
    mod = _load_sync_module(tmp_path)
    assert mod._PRUNE_HASHES == {GONE: "a" * 64}
    assert sys.argv == ["sync_knowledge_graph.py", GONE]
    mod._validate_argv_flags(sys.argv)


@pytest.mark.parametrize("argv", [
    ["--prune-missing", "--prune-hash", "knowledge/a.md=nothex"],
    ["--prune-missing", "--prune-hash", "no-equals-sign"],
    ["--prune-missing", "--prune-hash"],
    ["--prune-hash", f"knowledge/a.md={'a' * 64}"],  # without --prune-missing
], ids=["bad-hex", "no-eq", "no-value", "no-prune-missing"])
def test_r33_sync_rejects_malformed_prune_hash(tmp_path, monkeypatch, argv):
    from tests.test_v0270_kg_sync_batch_title_collision import _load_sync_module
    for key in ("KG_BASE_DIR", "KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                "DUAL_EMBEDDING_ENABLED", "VCT_DISABLE_HUB_RESOLVER"):
        monkeypatch.setenv(key, "placeholder")
    monkeypatch.setattr(sys, "argv", ["sync_knowledge_graph.py", *argv])
    with pytest.raises(SystemExit) as exc:
        _load_sync_module(tmp_path)
    assert exc.value.code == 2


def test_r33_guard_hash_is_the_hash_the_sync_stores(sync_mod):
    """One home: the sync's signature IS the vco_lib one, and the guard's
    bytes-hash equals what the sync stores for that file after its own read
    and ``updated:`` bump (CRLF bytes included)."""
    mod, _server, root = sync_mod
    assert mod._content_signature_excluding_updated is \
        content_signature_excluding_updated
    raw = _node("Gone node", "Line one.\r\nLine two.").replace(
        "\n", "\r\n").encode("utf-8")
    path = root / GONE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    guard_hash = content_signature_of_file_bytes(raw)
    content = mod._update_frontmatter_timestamp(
        path, path.read_text(encoding="utf-8"))
    assert mod._content_signature_excluding_updated(content) == guard_hash


def test_r33_validate_hands_the_reverted_new_nodes_hash_to_resync(repo):
    rel = "knowledge/concepts/eviction-notes.md"
    text = _node("Eviction notes", "Eviction CONFIRMED in staging.")
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, rel, text)
        calls: list = []
        res = guard.validate(snap, w, [].append,
                             resync=lambda r, h: calls.append((r, h)),
                             sources=_read_source("the cache module"))
    finally:
        snap.cleanup()
    assert res.reverted and not (repo / rel).exists()
    assert calls == [([rel], {rel: content_signature_of_file_bytes(
        text.encode("utf-8"))})]


def _resync_root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".claude" / "scripts").mkdir(parents=True)
    (root / ".claude" / "scripts" / "sync_knowledge_graph.py").write_text("#\n")
    (root / "knowledge").mkdir()
    return root


@pytest.mark.parametrize("rc, stdout, expect, never", [
    (0, kg_prune_result.format_line("knowledge/gone.md", "deleted",
                                    deleted=2, kept=1),
     "knowledge/gone.md: removed 2 row(s) from the KG collection; 1 row(s) "
     "with other content at the same path kept", None),
    (0, kg_prune_result.format_line("knowledge/gone.md", "nothing_matched"),
     "knowledge/gone.md: no rows matched in the KG collection, nothing "
     "removed", "removed 0"),
    (0, kg_prune_result.format_line("knowledge/gone.md", "skipped",
                                    reason="shared collection, no hash"),
     "knowledge/gone.md: rows NOT removed: shared collection, no hash",
     "removed from"),
    (1, kg_prune_result.format_line("knowledge/gone.md", "failed",
                                    reason="weaviate unreachable"),
     "knowledge/gone.md: row removal FAILED: weaviate unreachable",
     "removed from"),
    (0, "", "knowledge/gone.md: removal NOT confirmed", "removed from"),
], ids=["deleted", "nothing-matched", "skipped", "failed", "no-result"])
def test_r33_resync_log_states_what_the_sync_reported(
        tmp_path, monkeypatch, rc, stdout, expect, never):
    """RED before R33 N1: every exit-0 arm logged "removed from the KG
    collection"."""
    root = _resync_root(tmp_path)
    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout + "\n",
                                           stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    lines: List[str] = []
    guard.resync_with(root, lines.append)(["knowledge/gone.md"],
                                          {"knowledge/gone.md": _H_REVIEWER})
    cmd = calls[-1]
    assert "--prune-missing" in cmd
    i = cmd.index("--prune-hash")
    assert cmd[i + 1] == f"knowledge/gone.md={_H_REVIEWER}"
    assert expect in lines[-1], lines[-1]
    if never:
        assert never not in lines[-1], lines[-1]
    assert ("exited 1" in lines[-1]) == (rc == 1)


def test_r33_resync_spawn_failure_claims_no_removal(tmp_path, monkeypatch):
    root = _resync_root(tmp_path)

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(guard.subprocess, "run", boom)
    lines: List[str] = []
    guard.resync_with(root, lines.append)(["knowledge/gone.md"], {})
    assert lines[-1].startswith("re-sync of reverted nodes failed:")
    assert "knowledge/gone.md: removal NOT confirmed" in lines[-1]


def test_r33_resync_without_gone_paths_passes_no_prune_flags(tmp_path,
                                                             monkeypatch):
    root = _resync_root(tmp_path)
    (root / "knowledge" / "kept.md").write_text("x\n")
    calls: list = []
    monkeypatch.setattr(guard.subprocess, "run", lambda cmd, **kw: (
        calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")))
    lines: List[str] = []
    guard.resync_with(root, lines.append)(["knowledge/kept.md"],
                                          {"knowledge/kept.md": _H_REVIEWER})
    assert "--prune-missing" not in calls[-1]
    assert "--prune-hash" not in calls[-1]
    assert lines[-1] == "re-sync of reverted nodes finished: knowledge/kept.md"


# N2 — a marker right after a label word and a colon is a claim.


@pytest.mark.parametrize("added, marker", [
    ("Status: confirmed", "confirmed"),
    ("Result: verified.", "verified"),
    ("status:verified", "verified"),
    ("OUTCOME : tested", "tested"),
    ("**Result:** proven", "proven"),
    ("**Status**: validated", "validated"),
    ("- Verdict: `confirmed`", "confirmed"),
    ("Eviction status: confirmed", "confirmed"),
    ("`Result`: verified.", "verified"),
], ids=lambda v: v if len(v) < 14 else v[:24])
def test_r33_label_colon_marker_without_source_is_reverted(repo, added,
                                                           marker):
    """RED before R33 N2: kept."""
    res, _l = _validate(repo, {CACHE: CACHE_NODE + added + "\n"},
                        _read_source("the cache module"))
    assert res.kept == [] and f"marker '{marker}'" in res.reverted[0][1]


def test_r33_label_colon_marker_in_the_corpus_is_kept(repo):
    res, _l = _validate(repo, {CACHE: CACHE_NODE + "Status: confirmed\n"},
                        _read_source("Eviction confirmed by the soak run."))
    assert res.kept == [CACHE] and res.reverted == []


@pytest.mark.parametrize("added", [
    "Status: not confirmed yet.",
    "The result: a tested path through the loader.",
    "Two cases: tested and untested inputs.",
    "Inputs: validated strings only reach the parser.",
    "Note: the tested path keeps the cache warm.",
], ids=["negated", "article", "no-label-word", "other-label", "note"])
def test_r33_label_shaped_prose_is_kept(repo, added):
    res, _l = _validate(repo, {CACHE: CACHE_NODE + added + "\n"},
                        _read_source("the cache module"))
    assert res.kept == [CACHE] and res.reverted == [], res.reverted


# N3 — an unclosed fence does not hide the rest of the node.


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_r33_claim_after_an_unclosed_fence_is_judged(repo, fence):
    """RED before R33 N3: the stray fence exempted every later line."""
    post = CACHE_NODE + (f"{fence}\nx = 1\n\n"
                         "Eviction on full: confirmed in game.\n")
    res, _l = _validate(repo, {CACHE: post}, _read_source("the cache module"))
    assert res.kept == [] and "marker 'confirmed'" in res.reverted[0][1]


def test_r33_closed_fence_inside_an_unclosed_tail_is_still_skipped():
    text = ("```\nstray opener\n~~~\n# CONFIRMED in run 20261015-221000\n~~~\n"
            "Eviction VERIFIED.\n")
    judged = guard._judged_lines(text)
    assert "stray opener" in judged and "Eviction VERIFIED." in judged
    assert "# CONFIRMED in run 20261015-221000" not in judged


def test_r33_closed_fences_behave_as_before():
    text = "a\n```\ncode CONFIRMED\n```\nb\n~~~\nmore\n~~~\nc\n"
    assert [ln for ln in guard._judged_lines(text) if ln] == ["a", "b", "c"]


# ═════════════════════════════════════════════════════════════════════════
# 5. FINAL3 L-2 (Fable ship-gate 2026-10-16): a non-root project's reverted
# `scope: shared` node was embedded into the SHARED collection by the
# edit-time sync (v0.2.89 scope routing), so the prune must delete it there
# too — hash-scoped ALWAYS (a sibling's same-path row survives unless
# byte-identical), skipped with a reason when shared writes are disabled or
# no hash names the path, and the guard's log carries the per-collection
# outcome verbatim. The root (KG == shared) and the project-collection arm
# are unchanged.
# ═════════════════════════════════════════════════════════════════════════

SHARED = "TestProject_SharedKnowledgeGraph"


def _as_non_root_shared(mod, monkeypatch):
    monkeypatch.setattr(mod, "SHARED_COLLECTION_NAME", SHARED)
    monkeypatch.setattr(mod, "_PRUNE_MISSING", True)


def _shared_seed(mod, server, rel: str, content_hash: str) -> None:
    server.client.collections.get(SHARED).data.insert(
        properties={"title": "T", "file_path": rel,
                    "content_hash": content_hash})


def _shared_rows(mod, server) -> list:
    store = server.client.collections.get(SHARED)._store
    return sorted((o.properties.get("file_path"),
                   o.properties.get("content_hash")) for o in store.values())


def test_l2_shared_rows_of_a_reverted_shared_node_are_deleted_hash_scoped(
        sync_mod, monkeypatch, capsys):
    """RED before FINAL3 L-2: the prune looked only in the project
    collection, so the shared row outlived the file."""
    mod, server, root = sync_mod
    _as_non_root_shared(mod, monkeypatch)
    _seed_hashed(mod, server, GONE, _H_SIBLING)      # a stale project row
    _seed_hashed(mod, server, "knowledge/concepts/other-node.md", _H_SIBLING)
    _shared_seed(mod, server, GONE, _H_REVIEWER)     # the reviewer's write
    _shared_seed(mod, server, GONE, _H_REVIEWER)     # its second chunk
    _shared_seed(mod, server, GONE, _H_SIBLING)      # a sibling's shared row
    _shared_seed(mod, server, "knowledge/concepts/gone-node-sibling.md",
                 _H_REVIEWER)                        # token-superset path
    monkeypatch.setattr(mod, "_PRUNE_HASHES", {GONE: _H_REVIEWER})
    outcome = mod.sync_node(server, root / GONE)
    assert outcome and outcome.status == mod.OUTCOME_EXCLUDED_SKIPPED
    # project collection: unchanged semantics — every exact-path row goes
    assert _hashed_rows(mod, server) == [
        ("knowledge/concepts/other-node.md", _H_SIBLING)]
    # shared collection: only the reviewer's byte-identical rows go
    assert _shared_rows(mod, server) == sorted([
        (GONE, _H_SIBLING),
        ("knowledge/concepts/gone-node-sibling.md", _H_REVIEWER)])
    rec = _result(capsys)
    assert rec["status"] == kg_prune_result.STATUS_DELETED
    assert rec["deleted"] == 3  # 1 project row + 2 shared rows
    assert rec["kept"] == 1     # the sibling's same-path shared row
    colls = rec["collections"]
    assert colls[mod.COLLECTION_NAME] == {
        "status": kg_prune_result.STATUS_DELETED, "deleted": 1, "kept": 0,
        "reason": ""}
    assert colls[SHARED] == {
        "status": kg_prune_result.STATUS_DELETED, "deleted": 2, "kept": 1,
        "reason": ""}
    # the guard's log phrase names both collections and both counts
    phrase = kg_prune_result.describe(rec)
    assert f"removed 1 row(s) from '{mod.COLLECTION_NAME}'" in phrase
    assert f"removed 2 row(s) from '{SHARED}'" in phrase
    assert "1 row(s) with other content" in phrase


def test_l2_shared_write_disabled_skips_the_shared_collection_with_a_reason(
        sync_mod, monkeypatch, capsys):
    """The node cannot have been embedded in the shared collection by this
    project's sync when shared writes are disabled — skipped, said so."""
    mod, server, root = sync_mod
    _as_non_root_shared(mod, monkeypatch)
    monkeypatch.setenv("SHARED_KG_WRITE_DISABLED", "true")
    _seed_hashed(mod, server, GONE, _H_SIBLING)
    _shared_seed(mod, server, GONE, _H_REVIEWER)
    monkeypatch.setattr(mod, "_PRUNE_HASHES", {GONE: _H_REVIEWER})
    mod.sync_node(server, root / GONE)
    # project arm unchanged; shared rows untouched
    assert _hashed_rows(mod, server) == []
    assert _shared_rows(mod, server) == [(GONE, _H_REVIEWER)]
    rec = _result(capsys)
    assert rec["status"] == kg_prune_result.STATUS_DELETED
    assert rec["deleted"] == 1
    sub = rec["collections"][SHARED]
    assert sub["status"] == kg_prune_result.STATUS_SKIPPED
    assert "SHARED_KG_WRITE_DISABLED" in sub["reason"]
    phrase = kg_prune_result.describe(rec)
    assert f"'{SHARED}'" in phrase and "SHARED_KG_WRITE_DISABLED" in phrase


def test_l2_no_hash_names_the_path_so_nothing_is_deleted_from_shared(
        sync_mod, monkeypatch, capsys):
    """Hash-scoped ALWAYS on the shared collection: without a hash the
    sibling-risk wins and nothing is deleted there — reported."""
    mod, server, root = sync_mod
    _as_non_root_shared(mod, monkeypatch)
    _seed_hashed(mod, server, GONE, _H_SIBLING)
    _shared_seed(mod, server, GONE, _H_REVIEWER)
    monkeypatch.setattr(mod, "_PRUNE_HASHES", {})
    mod.sync_node(server, root / GONE)
    assert _hashed_rows(mod, server) == []
    assert _shared_rows(mod, server) == [(GONE, _H_REVIEWER)]
    rec = _result(capsys)
    assert rec["status"] == kg_prune_result.STATUS_DELETED
    sub = rec["collections"][SHARED]
    assert sub["status"] == kg_prune_result.STATUS_SKIPPED
    assert "--prune-hash" in sub["reason"]
    assert kg_prune_result.describe(rec).count("removed 1 row(s)") == 1


def test_l2_shared_arm_weaviate_down_reports_the_failure_per_collection(
        sync_mod, monkeypatch, capsys):
    """A shared-collection delete failure is named, never swallowed into a
    success record."""
    mod, server, root = sync_mod
    _as_non_root_shared(mod, monkeypatch)
    real_get = server.client.collections.get

    def flaky_get(name):
        if name == SHARED:
            raise ConnectionError("shared store unreachable")
        return real_get(name)

    server.client.collections = types.SimpleNamespace(get=flaky_get)
    _seed_hashed(mod, server, GONE, _H_SIBLING)
    monkeypatch.setattr(mod, "_PRUNE_HASHES", {GONE: _H_REVIEWER})
    outcome = mod.sync_node(server, root / GONE)
    assert outcome.status == mod.OUTCOME_FAILED
    rec = _result(capsys)
    assert rec["status"] == kg_prune_result.STATUS_FAILED
    assert "shared store unreachable" in rec["reason"]
    sub = rec["collections"][SHARED]
    assert sub["status"] == kg_prune_result.STATUS_FAILED
    # the project arm's own removal is still reported, not erased
    assert rec["collections"][mod.COLLECTION_NAME]["deleted"] == 1
    assert "shared store unreachable" in kg_prune_result.describe(rec)


def test_l2_root_prune_record_stays_single_collection(
        sync_mod, monkeypatch, capsys):
    """Root (KG == shared): the record format is unchanged — one collection,
    no per-collection split."""
    mod, server, root = sync_mod
    _seed_hashed(mod, server, GONE, _H_REVIEWER)
    _as_root(mod, monkeypatch)
    monkeypatch.setattr(mod, "_PRUNE_HASHES", {GONE: _H_REVIEWER})
    mod.sync_node(server, root / GONE)
    rec = _result(capsys)
    assert (rec["status"], rec["deleted"], rec["kept"]) == (
        kg_prune_result.STATUS_DELETED, 1, 0)
    assert "collections" not in rec


def test_l2_guard_log_states_the_per_collection_outcome(
        tmp_path, monkeypatch):
    """The guard's resync log renders the per-collection record the sync
    now prints — both collection names, truthful counts."""
    root = _resync_root(tmp_path)
    stdout = kg_prune_result.format_line(
        "knowledge/gone.md", kg_prune_result.STATUS_DELETED,
        deleted=3, kept=1,
        collections={"TestProject_KnowledgeGraph": {
            "status": kg_prune_result.STATUS_DELETED, "deleted": 1,
            "kept": 0, "reason": ""},
            SHARED: {"status": kg_prune_result.STATUS_DELETED,
                     "deleted": 2, "kept": 1, "reason": ""}})

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout + "\n",
                                           stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    lines: List[str] = []
    guard.resync_with(root, lines.append)(["knowledge/gone.md"],
                                          {"knowledge/gone.md": _H_REVIEWER})
    assert "removed 1 row(s) from 'TestProject_KnowledgeGraph'" in lines[-1]
    assert f"removed 2 row(s) from '{SHARED}'" in lines[-1], lines[-1]
    assert "1 row(s) with other content" in lines[-1], lines[-1]


def test_l2_format_line_roundtrips_the_per_collection_breakdown():
    line = kg_prune_result.format_line(
        GONE, kg_prune_result.STATUS_SKIPPED,
        collections={SHARED: {
            "status": kg_prune_result.STATUS_SKIPPED, "deleted": 0,
            "kept": 0, "reason": "no hash"}})
    rec = kg_prune_result.parse_lines(line).get(GONE)
    assert rec is not None
    assert rec["collections"][SHARED]["reason"] == "no hash"
    with pytest.raises(ValueError):
        kg_prune_result.format_line(
            GONE, kg_prune_result.STATUS_DELETED,
            collections={SHARED: {"status": "nonsense"}})


def test_r37_absent_shared_class_is_skipped_not_failed(sync_mod, monkeypatch, capsys):
    """R37 N-2: a shared class that was never created holds nothing to prune —
    reported as skipped with the reason, never as a failed removal."""
    mod, server, root = sync_mod
    _as_non_root_shared(mod, monkeypatch)
    _seed_hashed(mod, server, GONE, _H_REVIEWER)
    monkeypatch.setattr(mod, "_PRUNE_HASHES", {GONE: _H_REVIEWER})
    monkeypatch.setattr(server.client.collections, "exists",
                        lambda name: name != SHARED)
    mod.sync_node(server, root / GONE)
    rec = _result(capsys)
    assert rec["collections"][SHARED]["status"] == kg_prune_result.STATUS_SKIPPED
    assert "does not exist" in rec["collections"][SHARED]["reason"]
    assert rec["collections"][mod.COLLECTION_NAME]["status"] == kg_prune_result.STATUS_DELETED


# ═════════════════════════════════════════════════════════════════════════
# v0.2.105 — the two accepted limits of the evidence check (review D2.5)
# ═════════════════════════════════════════════════════════════════════════
# (a) the marker word was not tied to the same PASSAGE as the claim: it only
#     had to occur somewhere in the corpus, so a word in an unrelated
#     paragraph sourced any claim;
# (b) "it was validated input" — a noun phrase after a copula — was flagged
#     as an evidence claim and reverted when the word was in nothing read.

_CLAIM = "Eviction on full is CONFIRMED in game."


def test_d25_a_marker_in_an_unrelated_passage_does_not_source_the_claim():
    """ACT: the corpus carries the marker, about another subject entirely."""
    sources = ("the deploy box was confirmed stable after the reboot\n"
               "the cache keeps 100 entries\n")
    assert guard.unsourced_evidence(None, _CLAIM + "\n", sources) == [
        (_CLAIM, ["marker 'confirmed'"]),
    ]


def test_d25_a_marker_in_the_claims_own_passage_sources_it():
    """LEAVE-ALONE: same word, same passage — the write is kept."""
    sources = ("the deploy box was confirmed stable after the reboot\n"
               "eviction on full confirmed in game by the soak suite\n")
    assert guard.unsourced_evidence(None, _CLAIM + "\n", sources) == []


def test_d25_the_passage_tie_is_on_the_claim_line_not_the_whole_node():
    """The claim line is what has to be sourced: a marker in a passage about
    a DIFFERENT line of the same node does not source it."""
    post = CACHE_NODE + ("The soak suite confirmed the deploy box.\n"
                         "Eviction on full is CONFIRMED in game.\n")
    sources = "the soak suite confirmed the deploy box after the reboot\n"
    missing = guard.unsourced_evidence(CACHE_NODE, post, sources)
    assert [line for line, _why in missing] == [_CLAIM]


def test_d25_a_noun_phrase_after_a_copula_is_not_a_claim():
    """ACT for (b): "validated" modifies "input"; there is no evidence
    claim here, so an unsourced word must not revert the write."""
    assert guard.evidence_claims("it was validated input", has_id=False) == []
    assert guard.evidence_claims("the tested implementation", has_id=False) == []
    assert guard.unsourced_evidence(
        None, "It was validated input read from the config table.\n", "") == []


@pytest.mark.parametrize("line, marker", [
    ("Eviction on full is validated.", "validated"),
    ("Eviction on full has been tested and shipped.", "tested"),
    ("Eviction on full is proven correct.", "proven"),
    ("The cache is verified, not inferred.", "verified"),
])
def test_d25_a_predicate_after_a_copula_is_still_a_claim(line, marker):
    """LEAVE-ALONE for (b): a real predicate — the marker ends its clause or
    is followed by a conjunction/predicative word — is still a claim."""
    assert marker in guard.evidence_claims(line, has_id=False)
    assert [why for _l, missing in
            guard.unsourced_evidence(None, line + "\n", "") for why in missing
            ] == [f"marker '{marker}'"]


def test_d25_the_passage_tie_can_be_switched_off_by_config(monkeypatch):
    """The escape hatch: 0 restores the pre-v0.2.105 whole-corpus rule."""
    g = guard._guard()
    monkeypatch.setitem(g, "evidence_passage_min_shared_tokens", 0)
    sources = "the deploy box was confirmed stable after the reboot\n"
    assert guard.unsourced_evidence(None, _CLAIM + "\n", sources) == []

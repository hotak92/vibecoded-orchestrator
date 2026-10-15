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
        _node("Weaviate backups"))
    (r / "knowledge" / "concepts" / "docker-auto-start.md").write_text(
        _node("Docker auto-start"))
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
        res = guard.validate(snap, w, lines.append, resync=lambda _r: None,
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
    RED when the backend leaves the parent out of the sources."""
    parent = _git(repo, "rev-parse", "HEAD~1").strip()
    log = _run(chain, monkeypatch, repo,
               f"x was verified against {parent} before this change.\n")
    assert "REVERTED" not in log, log
    files = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert files == [CACHE], log


def test_run_reverts_an_invented_sha(chain, monkeypatch, repo):
    log = _run(chain, monkeypatch, repo,
               "x was verified against 0badc0de1 before this change.\n")
    assert f"REVERTED {CACHE}: raised an evidence" in log, log
    assert "id '0badc0de1'" in log
    assert (repo / CACHE).read_text() == CACHE_NODE


# ═════════════════════════════════════════════════════════════════════════
# 2. Same-subject duplicate nodes
# ═════════════════════════════════════════════════════════════════════════

# (existing title, new title, must be caught). The threshold (0.6) sits
# between the closest near-miss and the loosest duplicate in this table.
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
    hit = guard.same_subject_node(new, "knowledge/x/zz-unrelated-slug.md",
                                  {"knowledge/a/node.md": guard._norm_title(existing)})
    assert (hit is not None) is caught, (existing, new, hit)


def test_subject_tokens_drop_dates_stopwords_and_fold_plurals():
    assert guard.subject_tokens("2026-10-16 The Weaviate Backups of a DB") == {
        "weaviate", "backup", "db"}


def test_near_duplicate_new_node_is_reverted_naming_the_existing_node(repo):
    rel = "knowledge/concepts/backup-procedure.md"
    res, lines = _validate(repo, {rel: _node("Weaviate backup procedure")}, "")
    assert not (repo / rel).exists()
    reason = res.reverted[0][1]
    assert ("same subject as knowledge/tools/weaviate-backups.md" in reason
            and "extend knowledge/tools/weaviate-backups.md instead" in reason)
    assert any(ln.startswith(f"REVERTED {rel}") for ln in lines)


def test_near_duplicate_by_filename_slug_is_reverted(repo):
    rel = "knowledge/tools/docker-auto-start-on-boot.md"
    res, _l = _validate(repo, {rel: _node("Boot sequence")}, "")
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
    assert "cite a\n   test run, run id, build or commit as evidence" in text
    assert "look for an\n   existing node on the same subject in ANY folder" in text

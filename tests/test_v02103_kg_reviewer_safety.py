# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — the post-commit KG reviewer's safety boundaries.

Incident (a user project): the background reviewer rewrote a "do not change
without discussion" decision node and stripped its frontmatter, edited an
ADR under ``docs/``, minted duplicate / stale status nodes from a partial
diff, and left its edits uncommitted where ``git commit -a`` swept them into
an unrelated commit. Owner rulings 2026-10-15:

* model: ONE machine-wide setting, default Sonnet (Haiku / Sonnet / Opus);
  when unavailable, only NON-flash gateway rungs, else skip;
* research-first prompt, KG only (no docs job), capped diff with a
  truncation note;
* ENFORCED boundaries: knowledge/-only tool scope + a post-run validator
  that reverts forbidden edits from a snapshot copy;
* isolation: only the reviewer's own files are committed, as
  ``kg-sync: <hash> <subject>``, never with the user's work; skip
  conditions; a recursion guard; no empty commit.

Every test drives the real code against a temp git repo; the ``claude`` CLI
is always FAKED (never a real model), and Weaviate is the unroutable
sentinel the suite pins.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Callable, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"
LIVE_SHAPE = (REPO_ROOT / "tests" / "fixtures" / "kg_reviewer"
              / "gateway-models-live-shape.json")

for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import kg_reviewer_guard as guard  # noqa: E402
from vco_lib import kg_reviewer_model as model_mod  # noqa: E402
from vco_lib import kg_reviewer_prompt as prompt_mod  # noqa: E402

_REAL_RUN = subprocess.run

LOCKED_NODE = """---
title: Storage decision
type: concept
tags: [decision, storage]
status: active
updated: 2026-01-01T00:00:00Z
---

# Storage decision

We store blobs on disk. Provenance: chosen in the first design review.
"""

PLAIN_NODE = """---
title: Cache layer
type: concept
tags: [cache]
status: active
updated: 2026-01-01T00:00:00Z
---

# Cache layer

The cache keeps 100 entries.
"""

ADR = "# ADR 1\n\nAccepted: blobs on disk.\n"


def _new_node(title: str, body: str) -> str:
    """A new node with the frontmatter the guard requires."""
    return (f"---\ntitle: {title}\ntype: concept\ntags: [x]\n"
            f"created: 2026-10-15\nupdated: 2026-10-15\nstatus: active\n---\n\n{body}\n")


# ── fixtures ─────────────────────────────────────────────────────────────


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
    (r / "docs" / "adr").mkdir(parents=True)
    (r / "knowledge" / "concepts" / "storage-decision.md").write_text(LOCKED_NODE)
    (r / "knowledge" / "concepts" / "cache-layer.md").write_text(PLAIN_NODE)
    (r / "docs" / "adr" / "0001.md").write_text(ADR)
    (r / "src.py").write_text("x = 1\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    (r / "src.py").write_text("x = 2\n")
    _git(r, "commit", "-q", "-am", "feat: bump x")
    return r


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sb = _load("_v02103_summary_backends", SCRIPTS_DIR / "summary_backends.py")
    sb.reset_backend_cache()
    monkeypatch.setattr(sb, "_breaker_path", lambda: tmp_path / "breaker.json")
    mod = _load("_v02103_kg_reviewer_backend",
                SCRIPTS_DIR / "kg_reviewer_backend.py")
    setattr(mod, "_sb", sb)
    for k in ("VCO_KG_REVIEW_MAX_TURNS", "VCO_KG_REVIEW_MODEL"):
        monkeypatch.delenv(k, raising=False)
    yield mod
    sb.reset_backend_cache()


def _live_ids() -> frozenset:
    data = json.loads(LIVE_SHAPE.read_text(encoding="utf-8"))
    return frozenset(row["id"] for row in data["data"])


class _Rig:
    def __init__(self, chain, monkeypatch, *, claude=True,
                 catalogue=frozenset(), consent=False):
        self.lines: List[str] = []
        self.chain = chain
        self.latched: dict = {}
        monkeypatch.setattr(chain.shutil, "which",
                            lambda cmd: "/usr/bin/claude" if claude else None)
        monkeypatch.setattr(chain, "gateway_catalogue",
                            lambda timeout_s=2.0: ("http://127.0.0.1:11460",
                                                   catalogue, None))
        monkeypatch.setattr(chain._sb, "breaker_state", self.latched.get)
        monkeypatch.setattr(chain._sb, "openai_consent_granted",
                            lambda: consent)
        monkeypatch.setattr(chain._sb, "openai_available", lambda: True)
        monkeypatch.setattr(chain._model, "_preferences_value", lambda: None)

    def latch(self, *tiers: str) -> None:
        for t in tiers:
            self.latched[t] = {"reason": "quota", "remaining": 100}

    def decide(self):
        return self.chain.select_reviewer_backend(log=self.lines.append)


# ── stream-json transcript helpers (the fake claude's output) ────────────


def _tool_use(i: int, name: str, **inp) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}


def _tool_result(i: int, text: str) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{i}", "content": text}]}}


_RESULT = {"type": "result", "subtype": "success", "num_turns": 5,
           "result": "done"}


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in
                     ({"type": "system", "subtype": "init"}, *events, _RESULT))


# ═════════════════════════════════════════════════════════════════════════
# 1. Prompt
# ═════════════════════════════════════════════════════════════════════════


def test_prompt_demands_research_first_and_has_no_docs_job(repo):
    commit = prompt_mod.collect_commit(repo)
    text = prompt_mod.build_prompt(commit, repo, today="2026-10-15")
    # research steps, in order
    order = ["hybrid_search", "Read the FULL current text",
             "full files, not only the diff", "prefer the NEWEST source",
             "extending an existing node", "Write only facts",
             '"blocker", "in-progress"', "When unsure about any of this, write nothing"]
    positions = [text.find(s) for s in order]
    assert all(p >= 0 for p in positions), list(zip(order, positions))
    assert positions == sorted(positions), list(zip(order, positions))
    assert "Skip trivial" in text and "test-only" in text
    # KG only: docs/ appears only in the prohibition, never as a job
    assert "never edit anything outside" in text
    assert "not docs/" in text
    lowered = text.lower()
    for job in ("and documentation", "update them", "docs (in",
                "docs reference behavior"):
        assert job not in lowered, job
    assert "## Proposed change (kg-reviewer, 2026-10-15)" in text
    assert "ONLY with the Write and Edit tools" in text
    assert "store_knowledge_node" not in text  # not a reviewer tool any more
    # R11 A3: KG search text is deliberately not a source for flags
    assert "hybrid_search results do NOT count as a source" in text
    assert "+x = 2" in text and "feat: bump x" in text


def test_prompt_diff_is_capped_above_300_lines_with_a_truncation_note(repo):
    big = repo / "big.txt"
    big.write_text("".join(f"line-{i:05d}\n" for i in range(5000)))
    _git(repo, "add", "big.txt")
    _git(repo, "commit", "-q", "-m", "big")
    commit = prompt_mod.collect_commit(repo)
    cap = model_mod.config()["prompt"]["diff_max_lines"]
    assert cap > 300
    assert commit.diff_truncated and commit.diff_lines == cap
    assert "+line-00000" in commit.diff and "line-04999" not in commit.diff
    text = prompt_mod.build_prompt(commit, repo)
    assert "TRUNCATED" in text and "Read the changed files" in text


def test_prompt_untruncated_diff_says_complete_and_root_commit_has_its_diff(
        tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    r = tmp_path / "root"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "a.txt").write_text("root-marker\n")
    _git(r, "add", "a.txt")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit",
         "-q", "-m", "root")
    commit = prompt_mod.collect_commit(r)
    assert "+root-marker" in commit.diff and not commit.diff_truncated
    assert "complete" in prompt_mod.build_prompt(commit, r)


# ═════════════════════════════════════════════════════════════════════════
# 2. Tool scope
# ═════════════════════════════════════════════════════════════════════════


def test_spawn_argv_scopes_writes_to_knowledge(chain):
    cmd = chain.build_claude_cmd("sonnet", 30)
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert "Edit(knowledge/**)" in allowed and "Write(knowledge/**)" in allowed
    assert "Edit" not in allowed and "Write" not in allowed
    assert {"Read", "Glob", "Grep"} <= set(allowed)
    for d in ("Bash", "Edit(docs/**)", "Write(docs/**)", "Edit(.git/**)",
              "MultiEdit", "mcp__weaviate-kg__store_knowledge_node"):
        assert d in denied
    # R7: every reviewer write must be replayable from the transcript
    assert not any("store_knowledge_node" in a for a in allowed)
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet"


# ═════════════════════════════════════════════════════════════════════════
# 3. Model resolution
# ═════════════════════════════════════════════════════════════════════════


def test_model_default_is_sonnet():
    assert model_mod.resolve(env={}, preferences=lambda: None) == (
        "sonnet", "default")
    assert model_mod.model_choices() == ("haiku", "sonnet", "opus")


def test_model_preference_and_env_override():
    assert model_mod.resolve(env={}, preferences=lambda: "haiku") == (
        "haiku", "preferences")
    assert model_mod.resolve(env={"VCO_KG_REVIEW_MODEL": "Opus"},
                             preferences=lambda: "haiku") == ("opus", "env")


def test_model_invalid_values_are_ignored_with_a_line():
    lines: List[str] = []
    got = model_mod.resolve(env={"VCO_KG_REVIEW_MODEL": "gpt-x"},
                            preferences=lambda: "flash", log=lines.append)
    assert got == ("sonnet", "default")
    assert any("gpt-x" in ln for ln in lines) and any("flash" in ln for ln in lines)


def test_model_reads_the_real_launcher_db_preference(tmp_path, monkeypatch):
    from tests.common.launcher_db_fixture import make_launcher_db
    db = make_launcher_db(tmp_path, app_state={"kg_reviewer.model": "opus"})
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    monkeypatch.delenv("VCO_KG_REVIEW_MODEL", raising=False)
    assert model_mod.resolve() == ("opus", "preferences")


def test_chain_runs_the_chosen_model(chain, monkeypatch):
    rig = _Rig(chain, monkeypatch, catalogue=_live_ids())
    d = rig.decide()
    assert (d["mode"], d["model"]) == ("claude", "sonnet"), rig.lines
    monkeypatch.setenv("VCO_KG_REVIEW_MODEL", "opus")
    assert rig.decide()["model"] == "opus"


def test_unavailable_model_falls_back_to_glm_53_not_a_flash(chain, monkeypatch):
    rig = _Rig(chain, monkeypatch, catalogue=_live_ids())
    rig.latch("kg_reviewer:sonnet")
    d = rig.decide()
    assert d["model"] == "claude-gw/glm-5.3[1m]", (d, rig.lines)
    rig.latch("kg_reviewer:claude-gw/glm-5.3")
    assert rig.decide()["model"] == "claude-gw/kimi/k3[1m]", rig.lines


def test_flash_only_gateway_means_skip(chain, monkeypatch):
    flash = frozenset(i for i in _live_ids() if "flash" in i)
    rig = _Rig(chain, monkeypatch, catalogue=flash)
    rig.latch("kg_reviewer:sonnet")
    d = rig.decide()
    assert d["mode"] == "skip", (d, rig.lines)
    assert any("no flash-model fallback" in ln for ln in rig.lines)
    assert all("flash" not in r for r in chain.gateway_rungs())


def test_every_comparable_rung_down_keeps_the_openai_consent_gate(
        chain, monkeypatch):
    rig = _Rig(chain, monkeypatch, catalogue=_live_ids(), consent=True)
    rig.latch("kg_reviewer:sonnet", "kg_reviewer:claude-gw/glm-5.3",
              "kg_reviewer:claude-gw/kimi/k3")
    assert rig.decide()["mode"] == "openai"
    rig2 = _Rig(chain, monkeypatch, catalogue=_live_ids(), consent=False)
    rig2.latch("kg_reviewer:sonnet", "kg_reviewer:claude-gw/glm-5.3",
               "kg_reviewer:claude-gw/kimi/k3")
    assert rig2.decide()["mode"] == "skip"


# ═════════════════════════════════════════════════════════════════════════
# 4. Validator — each rule: act + leave-alone
# ═════════════════════════════════════════════════════════════════════════
#
# The reviewer's writes are given as the transcript's Write/Edit operations
# (keyed like attributed_writes does); `_reviewer_write` also lands the bytes
# on disk, as the CLI would.


def _key(repo: Path, rel: str) -> Path:
    return guard.path_key(repo / rel, repo.resolve())


def _reviewer_write(repo: Path, writes: dict, rel: str, content: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content)
    writes.setdefault(_key(repo, rel), []).append(("write", content))


def _validate(repo: Path, writes: Optional[dict], snap, sources: str = "") -> tuple:
    lines: List[str] = []
    resynced: List[List[str]] = []
    res = guard.validate(snap, writes, lines.append, resync=resynced.append,
                         sources=sources)
    return res, lines, resynced


def _backup_of(res, rel: str) -> str:
    assert res.backup_dir is not None, "nothing was backed up"
    return (res.backup_dir / rel).read_text()


def test_rule_outside_knowledge_is_reverted_with_a_backup(repo):
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, "docs/adr/0001.md", "# ADR 1\n\nRewritten.\n")
        res, lines, _ = _validate(repo, w, snap)
        assert (repo / "docs/adr/0001.md").read_text() == ADR
        assert res.reverted[0][0] == "docs/adr/0001.md"
        assert _backup_of(res, "docs/adr/0001.md") == "# ADR 1\n\nRewritten.\n"
        assert any("REVERTED docs/adr/0001.md: outside knowledge/" in ln
                   for ln in lines)
    finally:
        snap.cleanup()


def test_rule_outside_knowledge_leaves_the_users_concurrent_edit(repo):
    snap = guard.take_snapshot(repo)
    try:
        (repo / "docs/adr/0001.md").write_text("user edit\n")  # not attributed
        res, _lines, _ = _validate(repo, {}, snap)
        assert (repo / "docs/adr/0001.md").read_text() == "user edit\n"
        assert res.reverted == [] and res.kept == []
    finally:
        snap.cleanup()


def test_rule_new_file_outside_knowledge_is_removed(repo):
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, "notes.md", "made up\n")
        res, _l, _ = _validate(repo, w, snap)
        assert not (repo / "notes.md").exists()
        assert res.reverted and _backup_of(res, "notes.md") == "made up\n"
    finally:
        snap.cleanup()


def test_rule_users_dirty_file_is_restored_to_the_users_bytes(repo):
    (repo / "src.py").write_text("x = 'user wip'\n")
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, "src.py", "x = 'reviewer'\n")
        _validate(repo, w, snap)
        assert (repo / "src.py").read_text() == "x = 'user wip'\n"
    finally:
        snap.cleanup()


def test_rule_removed_frontmatter_is_reverted(repo):
    rel = "knowledge/concepts/cache-layer.md"
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, rel, "# Cache layer\n\nThe cache keeps 200 entries.\n")
        res, _lines, resynced = _validate(repo, w, snap)
        assert (repo / rel).read_text() == PLAIN_NODE
        assert "removed the node's YAML frontmatter" in res.reverted[0][1]
        assert resynced == [[rel]]
    finally:
        snap.cleanup()


def test_rule_updated_key_and_appended_text_is_allowed(repo):
    rel = "knowledge/concepts/cache-layer.md"
    snap = guard.take_snapshot(repo)
    try:
        new = (PLAIN_NODE.replace("2026-01-01T00:00:00Z", "2026-10-15T00:00:00Z")
               + "\nSince src.py v2 the cache keeps 200 entries.\n")
        w: dict = {}
        _reviewer_write(repo, w, rel, new)
        res, _l, resynced = _validate(repo, w, snap)
        assert (repo / rel).read_text() == new
        assert res.kept == [rel] and res.reviewer_bytes[rel] == new.encode()
        assert res.reverted == [] and resynced == [] and res.backup_dir is None
    finally:
        snap.cleanup()


# ── field finding: existing text is append-and-annotate only ─────────────

DECISIONS_NODE = """---
title: Decisions
type: concept
tags: [project]
status: active
updated: 2026-01-01T00:00:00Z
---

# Decisions

- Storage: blobs live on local disk.
- Cache size is 100 entries.

Notes follow.
"""


def _judge_decisions(repo: Path, new_text: str) -> tuple:
    rel = "knowledge/concepts/decisions.md"
    (repo / rel).write_text(DECISIONS_NODE)
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, rel, new_text)
        res, lines, _ = _validate(repo, w, snap)
        return res, lines, (repo / rel).read_text()
    finally:
        snap.cleanup()


def test_append_only_a_removed_original_line_is_reverted(repo):
    new = DECISIONS_NODE.replace("- Storage: blobs live on local disk.\n", "")
    res, lines, now = _judge_decisions(repo, new + "- Storage: object store.\n")
    assert now == DECISIONS_NODE
    assert "removed or reworded 1 original line(s)" in res.reverted[0][1]
    assert any("'- Storage: blobs live on local disk.'" in ln for ln in lines)
    assert _backup_of(res, "knowledge/concepts/decisions.md").endswith("object store.\n")


def test_append_only_a_reworded_original_line_is_reverted(repo):
    """The field shape: the superseded wording dropped, the update in its place."""
    new = DECISIONS_NODE.replace("blobs live on local disk.",
                                 "blobs live in the object store (since 2026-10).")
    res, _lines, now = _judge_decisions(repo, new)
    assert now == DECISIONS_NODE
    assert "Storage: blobs live on local disk." in res.reverted[0][1]


def test_append_only_annotated_in_place_lines_are_kept(repo):
    new = (DECISIONS_NODE
           .replace("- Storage: blobs live on local disk.",
                    "- Original 2026-01-01: Storage: blobs live on local disk. "
                    "(superseded by the object store, see below)")
           .replace("- Cache size is 100 entries.", "- ~~Cache size is 100 entries.~~")
           + "- Storage: object store (src/store.py), 2026-10-15.\n")
    res, _l, now = _judge_decisions(repo, new)
    assert now == new and res.kept == ["knowledge/concepts/decisions.md"]


def test_append_only_pure_append_is_kept(repo):
    new = DECISIONS_NODE + "\n## Update 2026-10-15\n\nThe cache now evicts LRU.\n"
    res, _l, now = _judge_decisions(repo, new)
    assert now == new and res.kept and res.reverted == []


def test_append_only_whitespace_and_crlf_differences_are_kept(repo):
    body_crlf = DECISIONS_NODE.replace("\n", "\r\n").replace(
        "- Cache size is 100 entries.", "- Cache size is 100 entries.   ")
    res, _l, now = _judge_decisions(repo, body_crlf.replace("Notes follow.",
                                                            "Notes follow.\t"))
    assert res.kept == ["knowledge/concepts/decisions.md"], res.reverted


def test_append_only_a_dropped_duplicate_line_is_caught():
    pre = "- TBD\n- TBD\nend\n"
    assert guard.removed_lines(pre, "- TBD\nend\n") == ["- TBD"]
    assert guard.removed_lines(pre, pre + "more\n") == []


def test_append_only_list_markers_are_not_text_but_words_are():
    pre = "- [ ] ship it\n1. first step\n> quoted rule\n"
    ok = ("- [x] ~~ship it~~ (done 2026-10-15)\n1. Original: first step\n"
          "> quoted rule (superseded)\n")
    assert guard.removed_lines(pre, ok) == []
    assert guard.removed_lines(pre, "- [ ] ship it now\n1. second step\n> quoted rule\n") \
        == ["1. first step"]


def test_rule_edit_tool_is_replayed_and_judged(repo):
    rel = "knowledge/concepts/cache-layer.md"
    snap = guard.take_snapshot(repo)
    try:
        (repo / rel).write_text(PLAIN_NODE.replace("status: active", "status: blocked"))
        w = {_key(repo, rel): [("edit", [("status: active", "status: blocked", False)])]}
        res, _l, _ = _validate(repo, w, snap)
        assert (repo / rel).read_text() == PLAIN_NODE
        assert "keys: status" in res.reverted[0][1]
    finally:
        snap.cleanup()


def test_rule_rewritten_frontmatter_key_is_reverted(repo):
    rel = "knowledge/concepts/cache-layer.md"
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, rel, PLAIN_NODE.replace("status: active", "status: blocked"))
        res, _l, _ = _validate(repo, w, snap)
        assert (repo / rel).read_text() == PLAIN_NODE
        assert "keys: status" in res.reverted[0][1]
    finally:
        snap.cleanup()


def test_rule_frontmatter_added_to_a_bare_node_is_reverted(repo):
    rel = "knowledge/concepts/bare.md"
    (repo / rel).write_text("# Bare\n\nA plain note.\n")
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, rel, "---\ntags: [decision]\nstatus: blocked\n---\n"
                        "# Bare\n\nA plain note.\n")
        res, _l, _ = _validate(repo, w, snap)
        assert (repo / rel).read_text() == "# Bare\n\nA plain note.\n"
        assert "added YAML frontmatter" in res.reverted[0][1]
    finally:
        snap.cleanup()


def test_rule_bare_node_may_gain_only_an_updated_stamp(repo):
    rel = "knowledge/concepts/bare.md"
    (repo / rel).write_text("# Bare\n\nA plain note.\n")
    snap = guard.take_snapshot(repo)
    try:
        new = ("---\nupdated: 2026-10-15T00:00:00Z\n---\n# Bare\n\nA plain note.\n"
               "\nAn appended fact.\n")
        w: dict = {}
        _reviewer_write(repo, w, rel, new)
        res, _l, _ = _validate(repo, w, snap)
        assert (repo / rel).read_text() == new and res.kept == [rel]
    finally:
        snap.cleanup()


def test_rule_locked_node_rewrite_is_reverted(repo):
    rel = "knowledge/concepts/storage-decision.md"
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, rel, LOCKED_NODE.replace("first design review",
                                                          "a later meeting"))
        res, _l, _ = _validate(repo, w, snap)
        assert (repo / rel).read_text() == LOCKED_NODE
        assert "locked node (tagged decision)" in res.reverted[0][1]
    finally:
        snap.cleanup()


def test_rule_locked_node_appended_proposal_is_allowed(repo):
    rel = "knowledge/concepts/storage-decision.md"
    snap = guard.take_snapshot(repo)
    try:
        new = (LOCKED_NODE.replace("2026-01-01T00:00:00Z", "2026-10-15T00:00:00Z")
               + "\n## Proposed change (kg-reviewer, 2026-10-15)\n\n"
                 "Blobs moved to object storage (src/store.py).\n")
        w: dict = {}
        _reviewer_write(repo, w, rel, new)
        res, _l, _ = _validate(repo, w, snap)
        assert (repo / rel).read_text() == new and res.kept
    finally:
        snap.cleanup()


def test_rule_locked_by_body_marker_and_undated_proposal_is_reverted(repo):
    rel = "knowledge/concepts/cache-layer.md"
    locked = PLAIN_NODE + "\nDo not change without discussion.\n"
    (repo / rel).write_text(locked)
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, rel, locked + "\n## Proposed change (kg-reviewer, soon)\nx\n")
        res, _l, _ = _validate(repo, w, snap)
        assert (repo / rel).read_text() == locked
        assert "locked node" in res.reverted[0][1]
    finally:
        snap.cleanup()


def test_rule_new_node_kept_and_a_vanished_node_is_left_to_the_user(repo):
    old = "knowledge/concepts/cache-layer.md"
    new = "knowledge/concepts/new-topic.md"
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, old, PLAIN_NODE.replace("100", "150"))
        (repo / old).unlink()  # the user deleted it meanwhile
        _reviewer_write(repo, w, new, _new_node("New topic", "Fact."))
        res, lines, _ = _validate(repo, w, snap)
        assert not (repo / old).exists(), "a user's deletion is never undone"
        assert res.left_for_user == [old]
        assert res.kept == [new]
        assert any(f"LEFT AS IS {old}" in ln for ln in lines)
    finally:
        snap.cleanup()


def test_rule_non_node_file_in_knowledge_is_reverted_sidecar_left(repo):
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, "knowledge/scratch.txt", "x\n")
        _reviewer_write(repo, w, "knowledge/.node_formats.json", "{}\n")
        res, _l, _ = _validate(repo, w, snap)
        assert not (repo / "knowledge/scratch.txt").exists()
        assert (repo / "knowledge/.node_formats.json").read_text() == "{}\n"
        assert [r for r, _ in res.reverted] == ["knowledge/scratch.txt"]
    finally:
        snap.cleanup()


# ── field evidence: new-node frontmatter, duplicate titles, invented flags ─


@pytest.mark.parametrize("content, needle", [
    ("# No frontmatter\n\nA fact.\n", "without YAML frontmatter"),
    ("---\ntitle: Half\ntype: concept\n---\n\nA fact.\n",
     "missing frontmatter keys: tags, created, updated, status"),
    ("---\ntitle: Empty tags\ntype: concept\ntags: []\ncreated: 2026-10-15\n"
     "updated: 2026-10-15\nstatus: active\n---\n\nA fact.\n",
     "missing frontmatter keys: tags"),
])
def test_new_node_without_full_frontmatter_is_reverted(repo, content, needle):
    rel = "knowledge/concepts/fresh.md"
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, rel, content)
        res, _l, _ = _validate(repo, w, snap)
        assert not (repo / rel).exists()
        assert needle in res.reverted[0][1]
        assert _backup_of(res, rel) == content
    finally:
        snap.cleanup()


def test_new_node_with_full_frontmatter_and_a_new_title_is_kept(repo):
    rel = "knowledge/concepts/fresh.md"
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, rel, _new_node("A genuinely new topic", "Fact."))
        res, _l, _ = _validate(repo, w, snap)
        assert res.kept == [rel] and res.reverted == []
    finally:
        snap.cleanup()


def test_new_node_duplicating_an_existing_title_is_reverted_with_a_pointer(repo):
    rel = "knowledge/concepts/cache-again.md"
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, rel, _new_node("  cache   LAYER ", "Same topic."))
        res, lines, _ = _validate(repo, w, snap)
        assert not (repo / rel).exists()
        assert ("duplicating the title of knowledge/concepts/cache-layer.md - "
                "extend the existing node instead") in res.reverted[0][1]
        assert any("extend the existing node" in ln for ln in lines)
    finally:
        snap.cleanup()


def test_unsourced_cli_flag_in_a_new_node_is_reverted(repo):
    rel = "knowledge/concepts/tool.md"
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, rel, _new_node(
            "Tool", "Run `state --source cache` to inspect it."))
        res, _l, _ = _validate(repo, w, snap, sources="usage: state [--verbose]")
        assert not (repo / rel).exists()
        assert "CLI flags found in no file it read: --source" in res.reverted[0][1]
    finally:
        snap.cleanup()


def test_sourced_cli_flag_is_kept_and_unchanged_old_flags_are_not_rechecked(repo):
    new_rel = "knowledge/concepts/tool.md"
    old_rel = "knowledge/concepts/cache-layer.md"
    old_with_flag = PLAIN_NODE + "\nLegacy: `cache --warm`.\n"
    (repo / old_rel).write_text(old_with_flag)
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, new_rel, _new_node(
            "Tool", "Run `state --verbose` (from src/cli.py)."))
        _reviewer_write(repo, w, old_rel, old_with_flag + "\nIt holds 100 entries.\n")
        res, _l, _ = _validate(repo, w, snap,
                               sources="    12\tparser.add_argument('--verbose')")
        assert sorted(res.kept) == [old_rel, new_rel], res.reverted
    finally:
        snap.cleanup()


def test_flag_check_matches_whole_flags_only(repo):
    assert guard.unsourced_flags(None, "use `x --source`", "--sources here") == ["--source"]
    assert guard.unsourced_flags(None, "use `x --source`", "a --source b") == []
    assert guard.unsourced_flags(None, "plain --source outside backticks", "") == []


def test_transcript_sources_are_read_and_grep_results_only(repo):
    out = _stream(
        _tool_use(1, "Read", file_path="src/cli.py"),
        _tool_result(1, "add_argument('--verbose')"),
        _tool_use(2, "mcp__weaviate-kg__hybrid_search", query="q"),
        _tool_result(2, "an old node says --invented"),
        _tool_use(3, "Grep", pattern="hold"),
        _tool_result(3, "keys.py: --hold-ms"),
    )
    src = guard.transcript_sources(out)
    assert "--verbose" in src and "--hold-ms" in src
    assert "--invented" not in src  # KG text is not a primary source


# ── R7 F1/F2/F3: the user's concurrent work is never touched ─────────────


def test_f1_rule_breaking_write_plus_user_edit_same_node_is_left_backed_up(repo):
    """RED before R7: the revert wrote the snapshot over the user's bytes."""
    rel = "knowledge/concepts/cache-layer.md"
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, rel, "# stripped\n")          # rule-breaking
        with open(repo / rel, "a") as fh:
            fh.write("+ USER CONCURRENT EDIT\n")                # the user, after
        res, lines, resynced = _validate(repo, w, snap)
        assert (repo / rel).read_text() == "# stripped\n+ USER CONCURRENT EDIT\n"
        assert res.reverted == [] and res.kept == [] and res.left_for_user == [rel]
        assert _backup_of(res, rel) == "# stripped\n+ USER CONCURRENT EDIT\n"
        assert resynced == []
        assert any(f"LEFT AS IS {rel}" in ln for ln in lines)
    finally:
        snap.cleanup()


def test_f1_edit_replay_that_no_longer_matches_is_the_users(repo):
    rel = "knowledge/concepts/cache-layer.md"
    snap = guard.take_snapshot(repo)
    try:
        # the user rewrote the line the reviewer's Edit targeted
        (repo / rel).write_text(PLAIN_NODE.replace("status: active", "status: mine"))
        w = {_key(repo, rel): [("edit", [("keeps 100", "keeps 999", False)])]}
        (repo / rel).write_text((repo / rel).read_text().replace("keeps 100", "keeps 999"))
        res, _l, _ = _validate(repo, w, snap)
        assert "status: mine" in (repo / rel).read_text()
        assert res.left_for_user == [rel] and res.reverted == []
    finally:
        snap.cleanup()


def test_f3_unattributable_run_touches_nothing(repo):
    rel = "knowledge/concepts/storage-decision.md"
    snap = guard.take_snapshot(repo)
    try:
        (repo / rel).write_text("user stripped it on purpose\n")
        (repo / "knowledge/concepts/cache-layer.md").unlink()
        res, lines, resynced = _validate(repo, None, snap)
        assert (repo / rel).read_text() == "user stripped it on purpose\n"
        assert not (repo / "knowledge/concepts/cache-layer.md").exists()
        assert res.unattributed and res.reverted == [] and res.kept == []
        assert resynced == [] and res.backup_dir is None
        assert any("nothing judged, reverted or committed" in ln for ln in lines)
    finally:
        snap.cleanup()


# ── R7 F5: symlinks inside knowledge/ ────────────────────────────────────


def test_f5_write_through_a_knowledge_symlink_outside_the_project_is_reverted(
        repo, tmp_path):
    outside = tmp_path / "outside-notes.md"
    outside.write_text("outside original\n")
    link = repo / "knowledge/linked.md"
    link.symlink_to(outside)
    snap = guard.take_snapshot(repo)
    try:
        outside.write_text("reviewer wrote through the link\n")
        w = {_key(repo, "knowledge/linked.md"):
             [("write", "reviewer wrote through the link\n")]}
        res, lines, _ = _validate(repo, w, snap)
        assert outside.read_text() == "outside original\n"
        assert link.is_symlink() and link.resolve() == outside.resolve()
        assert res.kept == [] and res.reverted
        assert any("symlink" in ln for ln in lines)
    finally:
        snap.cleanup()


def test_f5_a_symlink_replaced_by_a_file_is_restored_and_not_kept(repo, tmp_path):
    outside = tmp_path / "outside-notes.md"
    outside.write_text("outside original\n")
    link = repo / "knowledge/linked.md"
    link.symlink_to(outside)
    snap = guard.take_snapshot(repo)
    try:
        link.unlink()
        link.write_text("a regular file now\n")
        w = {_key(repo, "knowledge/linked.md"): [("write", "a regular file now\n")]}
        res, _l, _ = _validate(repo, w, snap)
        assert link.is_symlink() and outside.read_text() == "outside original\n"
        assert res.kept == []
        assert _backup_of(res, "knowledge/linked.md") == "a regular file now\n"
    finally:
        snap.cleanup()


def test_f5_user_edit_to_the_symlink_target_is_left_alone(repo, tmp_path):
    outside = tmp_path / "outside-notes.md"
    outside.write_text("outside original\n")
    (repo / "knowledge/linked.md").symlink_to(outside)
    snap = guard.take_snapshot(repo)
    try:
        outside.write_text("the user's own edit\n")
        w = {_key(repo, "knowledge/linked.md"): [("write", "reviewer text\n")]}
        res, _l, _ = _validate(repo, w, snap)
        assert outside.read_text() == "the user's own edit\n"
        assert res.left_for_user == [str(outside.resolve())]
    finally:
        snap.cleanup()


# ── R11 A2: link-target cap; outside bytes stay out of the project ───────


def test_r11_oversized_link_target_is_not_copied_and_not_restorable(
        repo, tmp_path, monkeypatch):
    cap = model_mod.config()["guard"]["link_target_max_bytes"]
    big = tmp_path / "huge.md"
    big.write_bytes(b"x" * (cap + 1))
    link = repo / "knowledge/huge-link.md"
    link.symlink_to(big)
    snap = guard.take_snapshot(repo)
    try:
        assert str(big.resolve()) in snap.oversized_targets
        assert str(big.resolve()) not in snap.link_targets
        assert not (snap.copy_dir / "link-targets").exists()
        big.write_bytes(b"reviewer wrote through the link\n")
        w = {_key(repo, "knowledge/huge-link.md"):
             [("write", "reviewer wrote through the link\n")]}
        res, lines, _ = _validate(repo, w, snap)
        assert big.read_bytes() == b"reviewer wrote through the link\n"
        assert res.kept == []
        assert any("too large to snapshot" in ln for ln in lines)
        assert res.unrestorable and "too large" in res.unrestorable[0][1]
    finally:
        snap.cleanup()


def test_r11_outside_target_backup_goes_to_the_vco_state_dir(
        repo, tmp_path, monkeypatch):
    state = tmp_path / "vco-state"
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    outside = tmp_path / "outside-notes.md"
    outside.write_text("outside original\n")
    (repo / "knowledge/linked.md").symlink_to(outside)
    snap = guard.take_snapshot(repo)
    try:
        outside.write_text("reviewer wrote through the link\n")
        w = {_key(repo, "knowledge/linked.md"):
             [("write", "reviewer wrote through the link\n")]}
        res, _l, _ = _validate(repo, w, snap)
        assert outside.read_text() == "outside original\n"
        backed = [p for p in state.rglob("outside-notes.md")]
        assert backed and backed[0].read_text() == "reviewer wrote through the link\n"
        assert "kg-reviewer-outside" in str(backed[0])
        assert not list(repo.rglob("_outside_project")), "foreign bytes in the project"
        assert not list((repo / ".claude").rglob("outside-notes.md"))
    finally:
        snap.cleanup()


# ── R15: a backup never follows a symlink ────────────────────────────────


def test_r15_in_project_symlink_to_a_huge_target_backs_up_link_text_only(repo):
    """RED before R15: copyfile followed the link and copied the whole
    target into the project's backups; the revert also replaced the link
    with a regular file."""
    cap = model_mod.config()["guard"]["link_target_max_bytes"]
    big = repo / "data" / "big.bin"
    big.parent.mkdir()
    big.write_bytes(b"y" * (cap * 2))
    link = repo / "notes-link.md"
    link.symlink_to(big)
    snap = guard.take_snapshot(repo)
    try:
        # the target is still huge when the guard runs: a backup that
        # followed the link would copy all of it
        w = {_key(repo, "notes-link.md"): [("write", "reviewer\n")]}
        res, lines, _ = _validate(repo, w, snap)
        assert link.is_symlink() and link.resolve() == big.resolve()
        assert res.kept == [] and res.reverted == []
        assert any("through a symlink outside the snapshot" in ln for ln in lines)
        backed = [p for p in (repo / ".claude/backups").rglob("*") if p.is_file()]
        assert all(p.stat().st_size < 4096 for p in backed), backed
        assert any(p.name == "notes-link.md.symlink" for p in backed)
        assert (res.backup_dir / "notes-link.md.symlink").read_text() == str(big)
    finally:
        snap.cleanup()


def test_r15_a_large_regular_project_file_is_still_fully_backed_up(repo):
    cap = model_mod.config()["guard"]["link_target_max_bytes"]
    big_text = "z" * (cap * 2)
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, "docs/adr/0001.md", big_text)
        res, _l, _ = _validate(repo, w, snap)
        assert (repo / "docs/adr/0001.md").read_text() == ADR  # reverted
        assert _backup_of(res, "docs/adr/0001.md") == big_text  # full copy
    finally:
        snap.cleanup()


# ── R11 A1: backups are never stageable ──────────────────────────────────


def _staged_under_backups(repo: Path) -> List[str]:
    _git(repo, "add", "-A")
    return [p for p in _git(repo, "diff", "--cached", "--name-only").split()
            if p.startswith(".claude/backups")]


def test_r11_guard_backups_are_never_staged_by_git_add_all(repo):
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, "docs/adr/0001.md", "# ADR 1\n\nRewritten.\n")
        res, _l, _ = _validate(repo, w, snap)
        assert res.backup_dir is not None and res.backup_dir.is_dir()
    finally:
        snap.cleanup()
    assert (repo / ".claude/backups/.gitignore").read_text() == "*\n"
    assert _staged_under_backups(repo) == []


def test_r11_bundle_adoption_backups_are_never_staged_by_git_add_all(repo):
    from vco_lib.bundle_backup import backup_bytes_for_adoption
    rel = backup_bytes_for_adoption(repo, ".claude/hooks/x.sh",
                                    "20261015T000000Z", b"user bytes\n")
    assert (repo / rel).read_bytes() == b"user bytes\n"
    assert _staged_under_backups(repo) == []


def test_r11_an_existing_backups_gitignore_is_left_as_is(repo):
    from vco_lib.project_backups import ensure_backups_dir
    (repo / ".claude/backups").mkdir(parents=True)
    (repo / ".claude/backups/.gitignore").write_text("# user rule\n!keep.txt\n")
    ensure_backups_dir(repo)
    assert (repo / ".claude/backups/.gitignore").read_text() == "# user rule\n!keep.txt\n"


def test_r11_a_failed_backup_means_no_revert(repo, monkeypatch):
    monkeypatch.setattr(guard._Backups, "save", lambda self, key: (False, None))
    snap = guard.take_snapshot(repo)
    try:
        w: dict = {}
        _reviewer_write(repo, w, "docs/adr/0001.md", "# ADR 1\n\nRewritten.\n")
        res, lines, _ = _validate(repo, w, snap)
        assert (repo / "docs/adr/0001.md").read_text() == "# ADR 1\n\nRewritten.\n"
        assert res.reverted == [] and res.unrestorable
        assert any("could not be backed up" in ln for ln in lines)
    finally:
        snap.cleanup()


def test_attribution_replays_writes_and_drops_failed_calls(repo):
    out = _stream(
        _tool_use(1, "Write", file_path=str(repo / "knowledge/a.md"), content="A"),
        _tool_use(2, "Edit", file_path="knowledge/b.md", old_string="x",
                  new_string="y"),
        _tool_use(3, "mcp__weaviate-kg__store_knowledge_node", title="T",
                  content="c"),
        _tool_result(3, json.dumps({"success": True,
                                    "absolute_path": str(repo / "knowledge/c.md")})),
        _tool_use(4, "Read", file_path=str(repo / "src.py")),
        _tool_use(5, "Write", file_path=str(repo / "docs/x.md"), content="D"),
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t5", "is_error": True,
             "content": "permission denied"}]}},
    )
    writes = guard.attributed_writes(out, repo)
    assert writes is not None
    assert writes[_key(repo, "knowledge/a.md")] == [("write", "A")]
    assert writes[_key(repo, "knowledge/b.md")] == [("edit", [("x", "y", False)])]
    assert writes[_key(repo, "knowledge/c.md")][0][0] == "opaque"
    assert _key(repo, "docs/x.md") not in writes  # a denied call changed nothing
    assert _key(repo, "src.py") not in writes
    assert guard.attributed_writes(json.dumps(_RESULT), repo) is None
    assert guard.expected_bytes(b"a x b", [("edit", [("x", "y", False)])]) == b"a y b"
    assert guard.expected_bytes(b"a b", [("edit", [("x", "y", False)])]) is guard.UNKNOWN
    assert guard.expected_bytes(None, [("opaque", "t")]) is guard.UNKNOWN


# ═════════════════════════════════════════════════════════════════════════
# 5. Commit isolation
# ═════════════════════════════════════════════════════════════════════════


def _user_work(repo: Path) -> None:
    """Staged + unstaged + untracked user work the commit must not touch."""
    (repo / "src.py").write_text("x = 'staged'\n")
    _git(repo, "add", "src.py")
    (repo / "src.py").write_text("x = 'staged+unstaged'\n")
    (repo / "docs/adr/0001.md").write_text(ADR + "user unstaged\n")
    (repo / "untracked.txt").write_text("u\n")


def _commit(repo, snap, kept, attributed=True, reviewer_bytes=None) -> tuple:
    """Commit ``kept``; by default the reviewer's bytes are what is on disk
    now (the reviewer wrote them and nobody touched them since)."""
    if reviewer_bytes is None:
        reviewer_bytes = {p: (repo / p).read_bytes() for p in kept
                          if (repo / p).exists()}
    lines: List[str] = []
    sha = guard.commit_reviewer_changes(
        snap, kept, reviewer_bytes=reviewer_bytes, short="abc1234",
        subject="feat: bump x", model="sonnet", attributed=attributed,
        log=lines.append)
    return sha, lines


def test_commit_contains_only_the_reviewers_files(repo):
    _user_work(repo)
    staged_before = _git(repo, "diff", "--cached", "--name-only")
    snap = guard.take_snapshot(repo)
    try:
        (repo / "knowledge/concepts/new-topic.md").write_text("---\ntitle: N\n---\nF.\n")
        (repo / "knowledge/concepts/cache-layer.md").write_text(
            PLAIN_NODE.replace("100", "200"))
        sha, lines = _commit(repo, snap, ["knowledge/concepts/new-topic.md",
                                          "knowledge/concepts/cache-layer.md"])
    finally:
        snap.cleanup()
    assert sha, lines
    files = set(_git(repo, "show", "--name-only", "--format=", sha).split())
    assert files == {"knowledge/concepts/new-topic.md",
                     "knowledge/concepts/cache-layer.md"}
    assert _git(repo, "log", "-1", "--format=%s").strip() == \
        "kg-sync: abc1234 feat: bump x"
    # the user's work is exactly where it was
    assert _git(repo, "diff", "--cached", "--name-only") == staged_before
    assert _git(repo, "show", ":src.py") == "x = 'staged'\n"
    assert (repo / "src.py").read_text() == "x = 'staged+unstaged'\n"
    assert "user unstaged" in (repo / "docs/adr/0001.md").read_text()
    assert "?? untracked.txt" in _git(repo, "status", "--porcelain")


def test_f2_a_user_edit_after_the_reviewer_wrote_is_not_committed(repo):
    """RED before R7: the user's mid-run bytes rode into the kg-sync commit."""
    kept_node = "knowledge/concepts/cache-layer.md"
    other = "knowledge/concepts/new-topic.md"
    snap = guard.take_snapshot(repo)
    try:
        reviewer = PLAIN_NODE.replace("100", "200")
        (repo / kept_node).write_text(reviewer)
        (repo / other).write_text("---\ntitle: N\n---\nF.\n")
        rb = {kept_node: reviewer.encode(), other: (repo / other).read_bytes()}
        (repo / kept_node).write_text(reviewer + "user WIP line\n")  # the user
        sha, lines = _commit(repo, snap, [kept_node, other], reviewer_bytes=rb)
    finally:
        snap.cleanup()
    assert sha
    assert _git(repo, "show", "--name-only", "--format=", sha).split() == [other]
    assert "user WIP line" in (repo / kept_node).read_text()
    assert any(f"not committed: {kept_node}" in ln for ln in lines)


@pytest.mark.parametrize("make_block, needle", [
    (lambda r: (r / ".git" / "MERGE_HEAD").write_text("0" * 40), "MERGE_HEAD"),
    (lambda r: (r / ".git" / "CHERRY_PICK_HEAD").write_text("0" * 40),
     "CHERRY_PICK_HEAD"),
    (lambda r: (r / ".git" / "rebase-merge").mkdir(), "rebase-merge"),
    (lambda r: (r / ".git" / "BISECT_LOG").write_text(""), "BISECT_LOG"),
    (lambda r: (r / ".git" / "index.lock").write_text(""), "index.lock"),
])
def test_commit_skipped_while_git_is_busy(repo, make_block, needle):
    # A TRACKED node, so no `git add` runs first: the skip must come from
    # the reviewer's own check, not from git refusing a later step.
    node = repo / "knowledge/concepts/cache-layer.md"
    snap = guard.take_snapshot(repo)
    head = _git(repo, "rev-parse", "HEAD")
    try:
        node.write_text(PLAIN_NODE.replace("100", "300"))
        make_block(repo)
        sha, lines = _commit(repo, snap, ["knowledge/concepts/cache-layer.md"])
    finally:
        snap.cleanup()
    assert sha is None
    assert _git(repo, "rev-parse", "HEAD") == head
    assert any("commit skipped" in ln and needle in ln for ln in lines), lines
    assert "300" in node.read_text()  # the files are left in place


def test_commit_skipped_when_head_moved(repo):
    snap = guard.take_snapshot(repo)
    try:
        (repo / "src.py").write_text("x = 3\n")
        _git(repo, "commit", "-q", "-am", "user commit meanwhile")
        (repo / "knowledge/concepts/new-topic.md").write_text("x\n")
        sha, lines = _commit(repo, snap, ["knowledge/concepts/new-topic.md"])
    finally:
        snap.cleanup()
    assert sha is None and any("HEAD moved" in ln for ln in lines)


def test_commit_skipped_when_target_had_user_changes(repo):
    node = repo / "knowledge/concepts/cache-layer.md"
    node.write_text(PLAIN_NODE + "\nuser wip\n")
    snap = guard.take_snapshot(repo)
    try:
        node.write_text(PLAIN_NODE + "\nuser wip\nreviewer line\n")
        sha, lines = _commit(repo, snap, ["knowledge/concepts/cache-layer.md"])
    finally:
        snap.cleanup()
    assert sha is None
    assert any("already had uncommitted changes" in ln for ln in lines)


def test_no_empty_commit_and_unattributed_run_not_committed(repo):
    head = _git(repo, "rev-parse", "HEAD")
    snap = guard.take_snapshot(repo)
    try:
        sha, lines = _commit(repo, snap, [])
        assert sha is None and any("nothing to commit" in ln for ln in lines)
        # a kept path whose bytes are back to the start is not a change
        sha, _ = _commit(repo, snap, ["knowledge/concepts/cache-layer.md"])
        assert sha is None
        (repo / "knowledge/concepts/new-topic.md").write_text("x\n")
        sha, lines = _commit(repo, snap, ["knowledge/concepts/new-topic.md"],
                             attributed=False)
        assert sha is None and any("could not be attributed" in ln for ln in lines)
    finally:
        snap.cleanup()
    assert _git(repo, "rev-parse", "HEAD") == head


def test_ignored_knowledge_is_not_committed(repo):
    (repo / ".gitignore").write_text("/knowledge/new-*.md\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    head = _git(repo, "rev-parse", "HEAD")
    snap = guard.take_snapshot(repo)
    try:
        (repo / "knowledge/new-x.md").write_text("x\n")
        sha, lines = _commit(repo, snap, ["knowledge/new-x.md"])
    finally:
        snap.cleanup()
    assert sha is None and _git(repo, "rev-parse", "HEAD") == head
    assert any("git-ignored" in ln for ln in lines)


def test_a_rejecting_user_hook_is_respected_and_index_restored(repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'policy says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    snap = guard.take_snapshot(repo)
    try:
        (repo / "knowledge/concepts/new-topic.md").write_text("x\n")
        sha, lines = _commit(repo, snap, ["knowledge/concepts/new-topic.md"])
    finally:
        snap.cleanup()
    assert sha is None and any("policy says no" in ln for ln in lines)
    # our `git add` of the new file was undone; the file stays untracked
    assert "?? knowledge/concepts/new-topic.md" in _git(repo, "status", "--porcelain")


# ═════════════════════════════════════════════════════════════════════════
# 6. End to end through `run` (fake claude) + recursion guard
# ═════════════════════════════════════════════════════════════════════════


def _fake_subprocess(behaviour: Callable[[dict], str], calls: list):
    def run(cmd, *a, **kw):
        if cmd and cmd[0] == "claude":
            calls.append({"cmd": list(cmd), **kw})
            return subprocess.CompletedProcess(cmd, 0, stdout=behaviour(kw),
                                               stderr="")
        return _REAL_RUN(cmd, *a, **kw)
    return types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired,
                                 CompletedProcess=subprocess.CompletedProcess,
                                 DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE)


def _run_chain(chain, monkeypatch, repo, behaviour, calls) -> str:
    monkeypatch.setattr(chain, "subprocess", _fake_subprocess(behaviour, calls))
    # The spawn resolves the CLI's path (PATHEXT on Windows); the fake
    # subprocess answers for argv[0] == "claude".
    monkeypatch.setattr(chain, "resolve_claude_executable", lambda: "claude")
    monkeypatch.setattr(chain, "select_reviewer_backend",
                        lambda log=None, model=None: {
                            "mode": "claude", "model": "sonnet",
                            "base_url": None, "reason": "t"})
    monkeypatch.setattr(chain, "_record_outcome", lambda *a, **k: None)
    log = repo.parent / "review.log"
    chain.main(["run", "--project-root", str(repo), "--log", str(log)])
    return log.read_text(encoding="utf-8") if log.exists() else ""


def _decide_sonnet(log=None, model=None) -> dict:
    return {"mode": "claude", "model": "sonnet", "base_url": None, "reason": "t"}


def test_run_spawns_the_resolved_cli_path_not_a_bare_name(chain, monkeypatch, repo):
    """v0.2.103 Windows verification: an npm-installed CLI is `claude.cmd`;
    `shutil.which` finds it through PATHEXT but CreateProcess does not, so a
    bare "claude" argv[0] raised FileNotFoundError on every reviewed commit.
    RED before the fix: argv[0] was the literal "claude"."""
    resolved = str(repo.parent / "bin" / "claude.CMD")
    seen: list = []

    def run(cmd, *a, **kw):
        if cmd and cmd[0] == resolved:
            seen.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout=_stream(), stderr="")
        if cmd and cmd[0] == "claude":
            raise FileNotFoundError(2, "bare name: CreateProcess cannot see .cmd")
        return _REAL_RUN(cmd, *a, **kw)

    monkeypatch.setattr(chain, "subprocess", types.SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess,
        DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE))
    monkeypatch.setattr(chain.shutil, "which",
                        lambda name: resolved if name == "claude" else None)
    monkeypatch.setattr(chain, "select_reviewer_backend", _decide_sonnet)
    monkeypatch.setattr(chain, "_record_outcome", lambda *a, **k: None)
    log = repo.parent / "review.log"
    assert chain.main(["run", "--project-root", str(repo), "--log", str(log)]) == 0
    assert seen and seen[0][0] == resolved and seen[0][1] == "-p"


def test_run_with_the_cli_gone_at_spawn_time_spawns_nothing(chain, monkeypatch, repo):
    """LEAVE-ALONE: the CLI vanished between the availability check and the
    spawn: nothing is spawned, a line says so, no breaker strike."""
    calls: list = []
    monkeypatch.setattr(chain, "subprocess", _fake_subprocess(lambda kw: "", calls))
    monkeypatch.setattr(chain, "resolve_claude_executable", lambda: None)
    monkeypatch.setattr(chain, "select_reviewer_backend", _decide_sonnet)
    struck: list = []
    monkeypatch.setattr(chain, "_record_outcome", lambda *a, **k: struck.append(a))
    log = repo.parent / "review.log"
    assert chain.main(["run", "--project-root", str(repo), "--log", str(log)]) == 127
    assert calls == [] and struck == []
    assert "no longer on PATH" in log.read_text(encoding="utf-8")


def test_build_claude_cmd_takes_the_resolved_executable(chain):
    cmd = chain.build_claude_cmd("sonnet", 30, executable=r"C:\bin\claude.cmd")
    assert cmd[0] == r"C:\bin\claude.cmd" and cmd[1] == "-p"
    assert chain.build_claude_cmd("sonnet", 30)[0] == "claude"


def test_run_end_to_end_reverts_the_incident_and_commits_only_good_nodes(
        chain, monkeypatch, repo):
    _user_work(repo)
    reviewed = _git(repo, "rev-parse", "HEAD").strip()
    dec_text = "# Storage decision\n\nProvenance: a later meeting.\n"
    adr_text = "# ADR 1\n\nreviewer edit\n"
    good_text = _new_node("Blob store", "x is 2.")

    def behaviour(kw) -> str:
        # what the incident's reviewer did, plus one legitimate node
        dec = repo / "knowledge/concepts/storage-decision.md"
        dec.write_text(dec_text)
        (repo / "docs/adr/0001.md").write_text(adr_text)
        good = repo / "knowledge/concepts/blob-store.md"
        good.write_text(good_text)
        return _stream(
            _tool_use(1, "Write", file_path=str(dec), content=dec_text),
            _tool_use(2, "Write", file_path=str(repo / "docs/adr/0001.md"),
                      content=adr_text),
            _tool_use(3, "Write", file_path=str(good), content=good_text))

    calls: list = []
    log = _run_chain(chain, monkeypatch, repo, behaviour, calls)
    assert calls and calls[0]["cwd"] == repo.resolve()
    assert "feat: bump x" in calls[0]["input"]
    assert (repo / "knowledge/concepts/storage-decision.md").read_text() == LOCKED_NODE
    assert (repo / "docs/adr/0001.md").read_text() == ADR + "user unstaged\n"
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert head != reviewed
    files = _git(repo, "show", "--name-only", "--format=", head).split()
    assert files == ["knowledge/concepts/blob-store.md"]
    assert "REVERTED knowledge/concepts/storage-decision.md" in log
    assert "REVERTED docs/adr/0001.md" in log
    assert "committed" in log
    assert _git(repo, "show", ":src.py") == "x = 'staged'\n"
    backups = list((repo / ".claude/backups/kg-reviewer").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "docs/adr/0001.md").read_text() == adr_text


def test_run_that_writes_nothing_makes_no_commit(chain, monkeypatch, repo):
    head = _git(repo, "rev-parse", "HEAD")
    calls: list = []
    log = _run_chain(chain, monkeypatch, repo, lambda kw: _stream(), calls)
    assert calls and _git(repo, "rev-parse", "HEAD") == head
    assert "nothing to commit" in log


def test_reviewer_commits_are_never_reviewed(chain, monkeypatch, repo):
    (repo / "knowledge/concepts/x.md").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "kg-sync: abc1234 feat: bump x")
    calls: list = []
    log = _run_chain(chain, monkeypatch, repo, lambda kw: _stream(), calls)
    assert calls == [], "a kg-sync: commit must not spawn a reviewer"
    assert "not reviewed" in log


def test_run_with_unattributable_output_touches_and_commits_nothing(
        chain, monkeypatch, repo):
    head = _git(repo, "rev-parse", "HEAD")
    node = repo / "knowledge/concepts/storage-decision.md"

    def behaviour(kw) -> str:
        (repo / "knowledge/concepts/blob-store.md").write_text("---\ntitle: B\n---\nx\n")
        node.write_text("stripped\n")  # could be the user: never reverted
        return json.dumps(_RESULT)  # an older CLI: one JSON object, no events

    calls: list = []
    log = _run_chain(chain, monkeypatch, repo, behaviour, calls)
    assert _git(repo, "rev-parse", "HEAD") == head
    assert node.read_text() == "stripped\n"
    assert "not attributable" in log and "could not be attributed" in log


# ═════════════════════════════════════════════════════════════════════════
# 7. Through the real hook siblings (fake claude, real chain, real git)
# ═════════════════════════════════════════════════════════════════════════

_FAKE_REVIEWER = r"""#!/usr/bin/env bash
cat > /dev/null
root="$(pwd)"
mkdir -p "$root/knowledge"
printf -- '---\ntitle: Hook node\ntype: concept\ntags: [x]\ncreated: 2026-10-15\nupdated: 2026-10-15\nstatus: active\n---\n\nFrom the hook run.\n' > "$root/knowledge/hook-node.md"
printf 'reviewer edit\n' >> "$root/docs.md"
printf '{"type":"system","subtype":"init"}\n'
printf '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"a","name":"Write","input":{"file_path":"%s/knowledge/hook-node.md","content":"---\\ntitle: Hook node\\ntype: concept\\ntags: [x]\\ncreated: 2026-10-15\\nupdated: 2026-10-15\\nstatus: active\\n---\\n\\nFrom the hook run.\\n"}}]}}\n' "$root"
printf '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"b","name":"Edit","input":{"file_path":"%s/docs.md","old_string":"doc\\n","new_string":"doc\\nreviewer edit\\n"}}]}}\n' "$root"
printf '{"type":"result","subtype":"success","num_turns":3,"result":"done"}\n'
"""


def _git_env(repo: Path, env: dict, *args: str) -> str:
    return _REAL_RUN(["git", *args], cwd=repo, env=env, check=True,
                     capture_output=True, text=True).stdout


@pytest.mark.parametrize("sibling", ["sh", "ps1"])
def test_hook_end_to_end_commits_only_the_reviewer_node(tmp_path, sibling):
    import stat
    import time
    from tests.test_v02102_w6n_kg_reviewer_fixes import (
        _hook_cmd, _install_chain, _scratch_repo)
    hook = _hook_cmd(sibling)  # skips when pwsh is absent
    repo, env = _scratch_repo(tmp_path, commits=1)
    fake = tmp_path / "bin" / "claude"
    fake.write_text(_FAKE_REVIEWER, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    _install_chain(repo, env)
    (repo / ".gitignore").write_text(".claude/\n")
    (repo / "docs.md").write_text("doc\n")
    _git_env(repo, env, "add", ".gitignore", "docs.md")
    _git_env(repo, env, "commit", "-q", "-m", "feat: docs")
    (repo / "first_file.txt").write_text("user staged\n")
    _git_env(repo, env, "add", "first_file.txt")
    reviewed = _git_env(repo, env, "rev-parse", "HEAD").strip()
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit"},
                          "tool_response": "[main abc] committed"})
    proc = _REAL_RUN(hook, cwd=repo, env=env, input=payload, capture_output=True,
                     text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    log = repo / ".claude" / "logs" / "kg-commit-review.log"
    deadline = time.monotonic() + 90
    text = ""
    while time.monotonic() < deadline:
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        if "committed " in text or "commit skipped" in text or "commit failed" in text:
            break
        time.sleep(0.3)
    assert "committed " in text, text
    head = _git_env(repo, env, "rev-parse", "HEAD").strip()
    assert head != reviewed
    shown = _git_env(repo, env, "show", "--name-only", "--format=%s", head).split("\n")
    assert shown[0].startswith("kg-sync: ") and "feat: docs" in shown[0], shown
    assert [f for f in shown[1:] if f.strip()] == ["knowledge/hook-node.md"]
    assert (repo / "docs.md").read_text() == "doc\n"  # the docs edit was reverted
    assert _git_env(repo, env, "diff", "--cached", "--name-only").split() == ["first_file.txt"]


@pytest.mark.parametrize("sibling", ["sh", "ps1"])
def test_f6_no_claude_cli_reaches_the_backend_which_logs_its_decision(
        tmp_path, sibling):
    """RED before R7: the hooks exited silently when `claude` was not on
    PATH, so the backend's no-CLI walk (to the consent-gated OpenAI rung)
    never ran and nothing was logged."""
    import time
    from tests.test_v02102_w6n_kg_reviewer_fixes import (
        _hook_cmd, _install_chain, _scratch_repo)
    import shutil
    hook = _hook_cmd(sibling)
    repo, env = _scratch_repo(tmp_path, commits=1)
    (tmp_path / "bin" / "claude").unlink()
    # Drop EVERY PATH entry that holds a real `claude` (this test must never
    # reach a real model), and prove none is left before running anything.
    env["PATH"] = os.pathsep.join(
        d for d in env["PATH"].split(os.pathsep)
        if d and not shutil.which("claude", path=d))
    assert shutil.which("claude", path=env["PATH"]) is None
    _install_chain(repo, env)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit"},
                          "tool_response": "[main abc] committed"})
    proc = _REAL_RUN(hook, cwd=repo, env=env, input=payload, capture_output=True,
                     text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    log = repo / ".claude" / "logs" / "kg-commit-review.log"
    deadline = time.monotonic() + 60
    text = ""
    while time.monotonic() < deadline:
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        if "every rung unavailable" in text:
            break
        time.sleep(0.3)
    assert "unavailable (no `claude` CLI on PATH)" in text, text
    assert "every rung unavailable" in text

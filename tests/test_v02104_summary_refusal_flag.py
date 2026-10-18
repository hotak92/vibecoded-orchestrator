# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — a model REFUSAL is flagged, not fallen through (owner ruling
2026-10-18; D2 sweep item D2.2).

The contract being pinned:

* the shared ladder (``summary_backends.call_llm``) raises the
  ``ContentRefusal`` SUBCLASS of ``NonAnswerResponse`` for a refusal-shaped
  reply ("I cannot …", "Sorry, …", "As an AI …"), and the plain class for
  every other non-answer — so existing catch sites are untouched while the
  KG generator can tell the two apart;
* a refusal never demotes the tier and never descends to the next one
  (a refusal answered by a weaker model would be a silent substitution);
* the KG summary generator records a refused node in its per-node sidecar
  (``.node_formats.json`` — the existing per-node state; no second state
  file) as a ``refused`` flag carrying no summary, logs one plain line
  naming the node, and does NOT count it as an error (the bulk exit code
  stays 0, so no auto-retry/deferral machinery is armed);
* the flag is never a usable entry, so the content-hash gate cannot skip
  the node: the NEXT ordinary sync tries it again, and a successful retry
  replaces the flag with a real entry.

RED-PROOF
---------
``summary_backends.call_llm`` was mutated to raise the plain
``NonAnswerResponse`` for refusal shapes too (the pre-fix behaviour): the
subclass test, both bulk sidecar tests and the single-mode test failed
against it; restored from a saved copy (md5-verified) they pass.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "templates" / "scripts"
SB_PATH = SCRIPTS / "summary_backends.py"
GENERATOR = SCRIPTS / "generate-kg-summary.py"

NODE_TEXT = (
    "---\ntitle: Some Node\ntype: concept\ntags: [x]\n"
    "created: 2026-01-01T00:00:00Z\nupdated: 2026-01-01T00:00:00Z\n"
    "status: active\n---\n\nBody text of the node.\n"
)

REFUSAL = "I cannot summarize this content."
DEGENERATE = "Ready. What do you need summarized?"
GOOD = "This node documents the KG summary ladder and its refusal flag."

_SUMMARY_ENV = (
    "KG_SUMMARY_BACKEND", "CODE_SUMMARY_BACKEND", "KG_SUMMARY_TIMEOUT",
    "VCO_SUMMARY_BREAKER", "VCO_SUMMARY_BREAKER_COOLDOWN",
    "VCO_SUMMARY_BREAKER_CAPACITY_COOLDOWN",
    "VCO_SUMMARY_BREAKER_CAPACITY_STRIKES",
    "VCO_SUMMARY_BREAKER_QUOTA_COOLDOWN",
    "VCO_SUMMARY_BREAKER_OTHER_COOLDOWN",
    "VCO_SUMMARY_BREAKER_OTHER_STRIKES",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
)


def _load(name: str, path: Path):
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sb(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Fresh summary_backends, isolated state dir, scrubbed ladder env."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct-state"))
    for key in _SUMMARY_ENV:
        monkeypatch.delenv(key, raising=False)
    mod = _load("_v02104_sb", SB_PATH)
    mod.reset_backend_cache()
    mod.reset_breaker(persisted=True)
    return mod


@pytest.fixture()
def gks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Fresh generate-kg-summary module (breaker state reset behind it)."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct-state"))
    for key in _SUMMARY_ENV:
        monkeypatch.delenv(key, raising=False)
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    mod = _load(f"_v02104_gks_{uuid.uuid4().hex}", GENERATOR)
    mod._sb.reset_backend_cache()
    mod._sb.reset_breaker(persisted=True)
    return mod


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolated state dir + scrubbed ladder env (for main()-driving tests
    that load their own module instance)."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct-state"))
    for key in _SUMMARY_ENV:
        monkeypatch.delenv(key, raising=False)
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def node_file(tmp_path: Path) -> Path:
    p = tmp_path / "a.md"
    p.write_text(NODE_TEXT, encoding="utf-8")
    return p


def _pin_probes(monkeypatch: pytest.MonkeyPatch, mod, *, cli=False,
                ollama=False) -> None:
    monkeypatch.setattr(mod, "cli_available", lambda: cli)
    monkeypatch.setattr(mod, "zai_available", lambda: False)
    monkeypatch.setattr(mod, "qwen_available", lambda: False)
    monkeypatch.setattr(mod, "ollama_available", lambda: ollama)
    monkeypatch.setattr(mod, "openai_available", lambda: False)
    monkeypatch.setattr(mod, "api_available", lambda: False)


# ═══════════════════════════════════════════════════════════════════════════
# The ladder: the subclass carries the distinction, never a tier descent
# ═══════════════════════════════════════════════════════════════════════════

def test_a_refusal_raises_the_content_refusal_subclass(sb, monkeypatch):
    _pin_probes(monkeypatch, sb, cli=True)
    monkeypatch.setattr(sb, "call_cli", lambda p: REFUSAL)
    with pytest.raises(sb.ContentRefusal):
        sb.call_llm("x")
    # And it still IS a NonAnswerResponse, so every pre-existing catch site
    # keeps working unchanged.
    with pytest.raises(sb.NonAnswerResponse):
        sb.call_llm("x")


def test_a_degenerate_non_answer_raises_the_plain_class(sb, monkeypatch):
    """Other non-answers keep their current behaviour exactly."""
    _pin_probes(monkeypatch, sb, cli=True)
    monkeypatch.setattr(sb, "call_cli", lambda p: DEGENERATE)
    with pytest.raises(sb.NonAnswerResponse) as excinfo:
        sb.call_llm("x")
    assert not isinstance(excinfo.value, sb.ContentRefusal)


def test_a_refusal_never_descends_to_the_next_tier(sb, monkeypatch):
    """The flag-it-don't-fall-through ruling at the ladder level: with a
    second tier available, a refusal on the first still reaches no second
    backend and trips no breaker."""
    _pin_probes(monkeypatch, sb, cli=True, ollama=True)
    monkeypatch.setattr(sb, "call_cli", lambda p: REFUSAL)
    ollama_calls: list[str] = []
    monkeypatch.setattr(sb, "call_ollama",
                        lambda p, **kw: ollama_calls.append(p) or GOOD)
    with pytest.raises(sb.ContentRefusal):
        sb.call_llm("x")
    assert ollama_calls == [], "a refusal must not fall through to a lower tier"
    assert sb.breaker_state("cli") is None, "a content refusal demotes nothing"


def test_the_refusal_message_reports_shape_never_text(sb, monkeypatch):
    """The flagged message is LOGGED and STORED — it must carry the reply's
    shape (length), never the reply (model output here quotes node content)."""
    _pin_probes(monkeypatch, sb, cli=True)
    monkeypatch.setattr(sb, "call_cli", lambda p: "I cannot summarize SECRET-NODE-BODY.")
    with pytest.raises(sb.ContentRefusal) as excinfo:
        sb.call_llm("x")
    assert "SECRET-NODE-BODY" not in str(excinfo.value)


# ═══════════════════════════════════════════════════════════════════════════
# Bulk mode: flagged in the sidecar, retried by the next ordinary sync
# ═══════════════════════════════════════════════════════════════════════════

def test_bulk_flags_a_refusal_without_caching_a_summary(gks, monkeypatch, node_file):
    def _raising(prompt, **_kw):
        raise gks.ContentRefusal("backend 'cli' returned a non-answer (37 chars) — not cached")

    monkeypatch.setattr(gks, "call_llm", _raising)
    db: dict = {}
    status = gks._process_node_bulk(node_file, force=False, dry_run=False, formats=db)
    assert status.startswith("refused:"), status
    (entry,) = db.values()
    assert entry["refused"] is True
    assert entry["title"] == "Some Node"
    assert "summary" not in entry and "description" not in entry, \
        "the refusal text is never cached as a summary"
    assert entry["refused_at"] and entry["content_hash"]
    assert gks.stored_entry_is_usable(entry) is False, \
        "the flag must never count as a satisfied entry (else the hash gate skips)"


def test_the_next_sync_retries_a_refused_node_and_clears_the_flag(
    gks, monkeypatch, node_file
):
    def _raising(prompt, **_kw):
        raise gks.ContentRefusal("backend 'cli' returned a non-answer (37 chars) — not cached")

    monkeypatch.setattr(gks, "call_llm", _raising)
    db: dict = {}
    assert gks._process_node_bulk(
        node_file, force=False, dry_run=False, formats=db).startswith("refused:")

    # Next ordinary sync, a backend answers: the SAME content hash must not
    # skip the node — it regenerates and the flag is gone.
    monkeypatch.setattr(gks, "call_llm", lambda prompt, **_kw: GOOD)
    monkeypatch.setattr(gks, "get_chunks_from_weaviate", lambda *_a, **_k: [])
    assert gks._process_node_bulk(
        node_file, force=False, dry_run=False, formats=db) == "generated"
    (entry,) = db.values()
    assert entry["summary"] == GOOD
    assert "refused" not in entry


def test_a_non_refusal_non_answer_is_never_flagged(gks, monkeypatch, node_file):
    """The other half of the ruling: degenerate non-answers keep the old
    contract — reported, never cached, no flag written."""
    def _raising(prompt, **_kw):
        raise gks.NonAnswerResponse("backend 'cli' returned a non-answer (37 chars) — not cached")

    monkeypatch.setattr(gks, "call_llm", _raising)
    db: dict = {}
    status = gks._process_node_bulk(node_file, force=False, dry_run=False, formats=db)
    assert status.startswith("nonanswer:"), status
    assert db == {}, "a non-refusal non-answer writes no sidecar entry"


def test_bulk_run_logs_one_plain_line_and_exits_zero(gks, monkeypatch, tmp_path, capsys):
    """Through the real ``main() --all``: the REFUSED line names the node,
    the sidecar on disk carries the flag, and the run is NOT an error (exit
    0 — the post-sync refresh must not arm its auto-retry over a refusal)."""
    root = tmp_path / "proj"
    kn = root / "knowledge" / "concepts"
    kn.mkdir(parents=True)
    (kn / "node-0.md").write_text(NODE_TEXT, encoding="utf-8")

    monkeypatch.setattr(
        gks._sb, "select_backend",
        lambda **_kw: gks._sb._BACKEND_CACHE.setdefault("choice", "cli") or "cli",
    )

    def _raising(prompt, **_kw):
        raise gks.ContentRefusal("backend 'cli' returned a non-answer (37 chars) — not cached")

    monkeypatch.setattr(gks, "call_llm", _raising)
    monkeypatch.setattr(
        sys, "argv",
        ["generate-kg-summary.py", "--all", "--knowledge-dir", str(root / "knowledge")],
    )
    with pytest.raises(SystemExit) as ei:
        gks.main()
    out = capsys.readouterr().out
    assert ei.value.code == 0, out
    assert "REFUSED knowledge/concepts/node-0.md" in out
    assert "refused: 1" in out
    db = json.loads(
        (root / "knowledge" / ".node_formats.json").read_text(encoding="utf-8"))
    (entry,) = db.values()
    assert entry["refused"] is True and "summary" not in entry


# ═══════════════════════════════════════════════════════════════════════════
# Single-file mode (the per-edit hook path)
# ═══════════════════════════════════════════════════════════════════════════

def test_single_mode_flags_the_refusal_and_exits_zero(clean_env, monkeypatch, tmp_path):
    root = tmp_path / "proj"
    kn = root / "knowledge" / "concepts"
    kn.mkdir(parents=True)
    node = kn / "node-0.md"
    node.write_text(NODE_TEXT, encoding="utf-8")
    monkeypatch.setenv("KG_PROJECT_ROOT", str(root))
    # CLAUDE_PROJECT / KNOWLEDGE_DIR / FORMATS_PATH are read at import time.
    mod = _load(f"_v02104_gks_single_{uuid.uuid4().hex}", GENERATOR)
    mod._sb.reset_backend_cache()
    monkeypatch.setattr(
        mod._sb, "select_backend",
        lambda **_kw: mod._sb._BACKEND_CACHE.setdefault("choice", "cli") or "cli",
    )

    def _raising(prompt, **_kw):
        raise mod.ContentRefusal("backend 'cli' returned a non-answer (37 chars) — not cached")

    monkeypatch.setattr(mod, "call_llm", _raising)
    monkeypatch.setattr(sys, "argv", ["generate-kg-summary.py", str(node)])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 0, "a recorded refusal is not a run failure"
    db = json.loads(
        (root / "knowledge" / ".node_formats.json").read_text(encoding="utf-8"))
    (entry,) = db.values()
    assert entry["refused"] is True
    assert "summary" not in entry and "description" not in entry


def test_single_mode_a_plain_non_answer_still_fails_loudly(clean_env, monkeypatch, tmp_path):
    """Other non-answers keep their current behaviour in single mode too:
    nothing written, exit 1."""
    root = tmp_path / "proj"
    kn = root / "knowledge" / "concepts"
    kn.mkdir(parents=True)
    node = kn / "node-0.md"
    node.write_text(NODE_TEXT, encoding="utf-8")
    monkeypatch.setenv("KG_PROJECT_ROOT", str(root))
    mod = _load(f"_v02104_gks_single2_{uuid.uuid4().hex}", GENERATOR)
    mod._sb.reset_backend_cache()
    monkeypatch.setattr(
        mod._sb, "select_backend",
        lambda **_kw: mod._sb._BACKEND_CACHE.setdefault("choice", "cli") or "cli",
    )

    def _raising(prompt, **_kw):
        raise mod.NonAnswerResponse("backend 'cli' returned a non-answer (37 chars) — not cached")

    monkeypatch.setattr(mod, "call_llm", _raising)
    monkeypatch.setattr(sys, "argv", ["generate-kg-summary.py", str(node)])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 1
    assert not (root / "knowledge" / ".node_formats.json").exists()


# ═══════════════════════════════════════════════════════════════════════════
# R16 A-N1: single mode merges ITS entry into the CURRENT sidecar
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("arm", ["refusal", "generated"])
def test_single_mode_keeps_an_entry_a_concurrent_run_wrote_meanwhile(
        clean_env, monkeypatch, tmp_path, arm):
    """Run A reads the sidecar, then (during its LLM call) run B writes ITS
    entry; A's save must not drop B's. Red-proof: with the old whole-dict
    save of the pre-call read, 'node-b' vanishes."""
    root = tmp_path / "proj"
    kn = root / "knowledge" / "concepts"
    kn.mkdir(parents=True)
    node = kn / "node-a.md"
    node.write_text(NODE_TEXT, encoding="utf-8")
    monkeypatch.setenv("KG_PROJECT_ROOT", str(root))
    mod = _load(f"_v02104_gks_race_{uuid.uuid4().hex}", GENERATOR)
    mod._sb.reset_backend_cache()
    monkeypatch.setattr(
        mod._sb, "select_backend",
        lambda **_kw: mod._sb._BACKEND_CACHE.setdefault("choice", "cli") or "cli",
    )
    monkeypatch.setattr(mod, "get_chunks_from_weaviate", lambda *_a, **_k: [])
    sidecar = root / "knowledge" / ".node_formats.json"

    def _llm(prompt, **_kw):
        # Run B: reads the (empty) sidecar and saves its own entry.
        b = mod.load_formats()
        b["knowledge/concepts/node-b.md"] = {"title": "B", "summary": "s",
                                             "description": "d", "content_hash": "x"}
        mod.save_formats(b)
        if arm == "refusal":
            raise mod.ContentRefusal("backend 'cli' returned a non-answer (37 chars)")
        return "a usable answer"

    monkeypatch.setattr(mod, "call_llm", _llm)
    monkeypatch.setattr(sys, "argv", ["generate-kg-summary.py", str(node)])
    if arm == "refusal":
        with pytest.raises(SystemExit):
            mod.main()
    else:
        mod.main()
    db = json.loads(sidecar.read_text(encoding="utf-8"))
    assert "knowledge/concepts/node-b.md" in db, "run B's entry was dropped"
    assert "knowledge/concepts/node-a.md" in db, "run A's own entry is missing"

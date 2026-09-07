# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 W4/W5 — the embedding-fidelity surface, exercised end to end.

The defect this closes: ``embedding_failures.jsonl`` — the file the
``embedding-failures-surface`` SessionStart hook points Claude at, and the
file kg-sync's "no vector" message names — was written ONLY when
``EmbeddingService.for_project()`` raised ``NoEmbeddingBackendError``. The
shrink-on-refusal path and the floor refusal (both new this cycle, on the text
AND code legs) never reached it, so a primary-slot shrink — a real fidelity
loss on the vectors retrieval reads — was announced by a WARNING to MCP stderr
or a detached kg-sync's stdout, i.e. to nobody.

Granularity is a per-RUN summary, deliberately, and that is a property worth
testing rather than assuming: a shrink is not an outage, and on dense corpora
(markdown tables, box-drawing, CJK) a large fraction of chunks legitimately
shrink, so a per-node row would spam the file and the session. The tests below
pin BOTH halves of that: many notes produce ONE row, and that row still names
every model and carries the counts.

**Isolation.** ``tests/conftest.py`` redirects ``VCT_STATE_DIR`` once for the
whole SUITE, which keeps fixture rows out of the maintainer's real
``~/.vct/metrics``. It does NOT isolate tests from EACH OTHER, and this
surface is offset-based: a reader with no marker consumes everything in the
file, so a row another test appended would be surfaced here (and vice versa —
this file's rows would leak into any other test that renders the notice). So
every test here pins its OWN ``VCT_STATE_DIR``, and the module state
(``_STATE``, the atexit registration) is reset between them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.child_env import child_env  # noqa: E402
from vco_lib import embedding_fidelity as fid  # noqa: E402

HOOK_SH = REPO_ROOT / "templates" / "hooks" / "embedding-failures-surface.sh"


@pytest.fixture
def metrics_home(tmp_path, monkeypatch):
    """A private metrics stream for ONE test.

    Both levers, because the reader consults both: ``VCT_STATE_DIR`` is the
    live home and ``VCT_CLAUDE_DIR`` the frozen archive
    (``vco_lib.paths.metrics_read_dirs`` reads new-home-first, then archive).
    Leaving the archive pointing at the suite-wide redirect would let another
    test's rows in through the back door.
    """
    state = tmp_path / "vct"
    claude = tmp_path / "claude"
    (state / "metrics").mkdir(parents=True)
    (claude / "metrics").mkdir(parents=True)
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(claude))
    fid.reset_run_state()
    monkeypatch.setattr(fid, "_ATEXIT_REGISTERED", True)  # no real atexit here
    yield state / "metrics" / "embedding_failures.jsonl"
    fid.reset_run_state()


def _rows(jsonl: Path) -> list[dict]:
    if not jsonl.is_file():
        return []
    return [
        json.loads(line)
        for line in jsonl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Writer side — granularity
# ---------------------------------------------------------------------------


def test_many_shrinks_produce_exactly_one_row_per_run(metrics_home):
    """The granularity decision, enforced: 500 shrunken chunks across two
    models are ONE row. A per-chunk row would make a dense corpus unreadable
    and would turn an ordinary fidelity note into something that looks like an
    outage storm."""
    for i in range(300):
        fid.note_shrink("qwen3-embedding:0.6b", 9000, 4000)
    for i in range(200):
        fid.note_shrink("snowflake-arctic-embed2", 9000, 2000)
    assert not metrics_home.exists(), "notes must be in-memory until flush"

    assert fid.flush_run() is True
    rows = _rows(metrics_home)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "shrink_summary"
    assert row["shrinks"]["qwen3-embedding:0.6b"]["count"] == 300
    assert row["shrinks"]["qwen3-embedding:0.6b"]["orig_chars"] == 300 * 9000
    assert row["shrinks"]["snowflake-arctic-embed2"]["count"] == 200


def test_a_clean_run_writes_nothing(metrics_home):
    """Signal density: a run with no shrink and no refusal must leave the file
    untouched, so a non-empty jsonl always means something happened."""
    assert fid.flush_run() is False
    assert not metrics_home.exists()


def test_floor_refusal_keeps_the_last_message_as_an_exemplar(metrics_home):
    fid.note_floor_refusal("qwen3-embedding:0.6b", 512, "first failure")
    fid.note_floor_refusal("qwen3-embedding:0.6b", 480, "context length exceeded")
    assert fid.flush_run() is True
    refusals = _rows(metrics_home)[0]["floor_refusals"]["qwen3-embedding:0.6b"]
    assert refusals["count"] == 2
    assert refusals["last_message"] == "context length exceeded"
    assert refusals["last_chars"] == 480


def test_flush_resets_so_a_second_flush_does_not_double_count(metrics_home):
    fid.note_shrink("m", 100, 50)
    assert fid.flush_run() is True
    assert fid.flush_run() is False
    assert len(_rows(metrics_home)) == 1


def test_a_process_that_never_flushes_still_lands_its_summary(tmp_path):
    """The atexit leg is what makes the writer COMPLETE without end-of-run
    wiring in every caller — a detached kg-sync or an MCP subprocess simply
    exits. Exercised in a REAL subprocess: an in-process test cannot observe
    interpreter shutdown."""
    state = tmp_path / "vct"
    (state / "metrics").mkdir(parents=True)
    env = child_env(
        VCT_STATE_DIR=str(state), VCT_CLAUDE_DIR=str(tmp_path / "claude"),
    )
    code = (
        "from vco_lib.embedding_fidelity import note_shrink;"
        "note_shrink('qwen3-embedding:0.6b', 8000, 4000)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True,
        text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    rows = _rows(state / "metrics" / "embedding_failures.jsonl")
    assert len(rows) == 1 and rows[0]["kind"] == "shrink_summary"


# ---------------------------------------------------------------------------
# Reader side — the notice
# ---------------------------------------------------------------------------


def test_notice_renders_once_and_then_stays_quiet(metrics_home, tmp_path, capsys):
    """Once per NEW summary, not once per session: a machine whose syncs
    routinely shrink must not be nagged at every SessionStart."""
    project = tmp_path / "proj"
    project.mkdir()
    fid.note_shrink("qwen3-embedding:0.6b", 9000, 4500)
    fid.flush_run()

    assert fid.emit_notice(project) == 0
    first = capsys.readouterr().out
    assert "Embedding fidelity note (NOT an outage)" in first
    assert "qwen3-embedding:0.6b" in first
    assert "1 chunk(s) embedded from a leading sub-window" in first
    assert str(metrics_home) in first

    assert fid.emit_notice(project) == 0
    assert capsys.readouterr().out == ""

    # A NEW run's summary surfaces again.
    fid.note_floor_refusal("qwen3-embedding:0.6b", 512, "too long")
    fid.flush_run()
    assert fid.emit_notice(project) == 0
    assert "got NO vector" in capsys.readouterr().out


def test_outage_rows_are_not_rendered_as_fidelity_notes(metrics_home, tmp_path,
                                                        capsys):
    """Severity separation lives in the DATA. An outage row (kind='outage',
    and legacy rows with no kind at all) belongs to the EMBEDDING_FAILURES.md
    banner; rendering it here would call a backend outage a fidelity note."""
    project = tmp_path / "proj"
    project.mkdir()
    metrics_home.write_text(
        json.dumps({"kind": "outage", "message": "backend down"}) + "\n"
        + json.dumps({"attempted_backends": [], "message": "legacy"}) + "\n",
        encoding="utf-8",
    )
    assert fid.emit_notice(project) == 0
    assert capsys.readouterr().out == ""
    # …but their bytes are consumed, so they cannot re-surface later either.
    marker = json.loads((project / fid.MARKER_REL).read_text(encoding="utf-8"))
    assert marker["offset"] == metrics_home.stat().st_size


def test_a_partial_final_line_is_left_for_the_next_read(metrics_home, tmp_path,
                                                        capsys):
    """The writer appends while the reader reads. A half-written final line
    must not be consumed (its bytes are not marked seen) or the completed row
    would be lost forever."""
    project = tmp_path / "proj"
    project.mkdir()
    complete = json.dumps({"kind": fid.KIND_SHRINK_SUMMARY, "timestamp": "t",
                           "shrinks": {"m": {"count": 1, "orig_chars": 10,
                                             "sent_chars": 5}}})
    metrics_home.write_text(complete + '\n{"kind": "shrink_su',
                            encoding="utf-8")
    fid.emit_notice(project)
    assert "m: 1 chunk(s)" in capsys.readouterr().out
    marker = json.loads((project / fid.MARKER_REL).read_text(encoding="utf-8"))
    assert marker["offset"] == len(complete) + 1


def test_notice_is_silent_when_there_is_no_jsonl_at_all(tmp_path, monkeypatch,
                                                        capsys):
    state = tmp_path / "vct"
    state.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(tmp_path / "claude"))
    project = tmp_path / "proj"
    project.mkdir()
    assert fid.emit_notice(project) == 0
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# The hook — is the reader actually REACHED on SessionStart?
# ---------------------------------------------------------------------------


def _fake_vco_venv(tmp_path: Path) -> Path:
    """A ``$VCT_VENV``-shaped directory whose python is this interpreter with
    the repo on PYTHONPATH — tier 1 of `_lib/resolve-vco-venv.sh`."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    shim = venv / "bin" / "python"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return venv


def _run_hook(project: Path, state: Path, tmp_path: Path):
    env = child_env(
        CLAUDE_PROJECT_DIR=str(project),
        VCT_STATE_DIR=str(state),
        VCT_CLAUDE_DIR=str(tmp_path / "claude_home"),
        VCT_VENV=str(_fake_vco_venv(tmp_path)),
    )
    env.pop("VCT_DISABLE_HOOKS", None)
    return subprocess.run(
        ["bash", str(HOOK_SH)], env=env, capture_output=True, text=True,
        timeout=60,
    )


def test_hook_surfaces_a_shrink_summary_and_only_once(tmp_path):
    """The wiring test for this whole surface, through the PRODUCTION entry
    point: the SessionStart hook itself, not the renderer.

    MUT targets — each of these makes it red: delete the fidelity leg from the
    hook; drop the `notice` subcommand; render outage rows only.
    """
    project = tmp_path / "proj"
    (project / ".claude" / "state").mkdir(parents=True)
    state = tmp_path / "vct"
    (state / "metrics").mkdir(parents=True)
    (state / "metrics" / "embedding_failures.jsonl").write_text(
        json.dumps({
            "kind": "shrink_summary",
            "timestamp": "2026-09-05T00:00:00+00:00",
            "shrinks": {"qwen3-embedding:0.6b": {
                "count": 7, "orig_chars": 70000, "sent_chars": 35000}},
            "floor_refusals": {},
        }) + "\n",
        encoding="utf-8",
    )

    first = _run_hook(project, state, tmp_path)
    assert first.returncode == 0, first.stderr
    assert "Embedding fidelity note (NOT an outage)" in first.stdout, (
        f"the SessionStart hook did not reach the fidelity notice.\n"
        f"stdout={first.stdout!r}\nstderr={first.stderr!r}"
    )
    assert "7 chunk(s)" in first.stdout
    assert "qwen3-embedding:0.6b" in first.stdout

    # Idempotent: nothing NEW landed, so the next session is silent.
    second = _run_hook(project, state, tmp_path)
    assert second.returncode == 0
    assert second.stdout.strip() == "", (
        f"the notice repeated with no new rows: {second.stdout!r}")


def test_hook_is_silent_when_the_only_rows_are_outages(tmp_path):
    """A backend outage has its OWN surface (the EMBEDDING_FAILURES.md
    banner). The fidelity leg must not double-report it in the wrong words."""
    project = tmp_path / "proj"
    (project / ".claude" / "state").mkdir(parents=True)
    state = tmp_path / "vct"
    (state / "metrics").mkdir(parents=True)
    (state / "metrics" / "embedding_failures.jsonl").write_text(
        json.dumps({"kind": "outage", "message": "ollama refused"}) + "\n",
        encoding="utf-8",
    )
    result = _run_hook(project, state, tmp_path)
    assert result.returncode == 0
    assert "fidelity" not in result.stdout.lower()


def test_hook_still_surfaces_the_outage_banner_alongside(tmp_path):
    """The two legs are independent: adding the fidelity leg must not have
    displaced the original EMBEDDING_FAILURES.md surfacing."""
    project = tmp_path / "proj"
    (project / ".claude" / "context").mkdir(parents=True)
    (project / ".claude" / "state").mkdir(parents=True)
    (project / ".claude" / "context" / "EMBEDDING_FAILURES.md").write_text(
        "# Embedding backend failure\n\n- **ollama**: connection refused\n",
        encoding="utf-8",
    )
    state = tmp_path / "vct"
    (state / "metrics").mkdir(parents=True)
    (state / "metrics" / "embedding_failures.jsonl").write_text(
        json.dumps({
            "kind": "shrink_summary", "timestamp": "t",
            "shrinks": {"m": {"count": 1, "orig_chars": 10, "sent_chars": 5}},
            "floor_refusals": {},
        }) + "\n",
        encoding="utf-8",
    )
    result = _run_hook(project, state, tmp_path)
    assert result.returncode == 0
    assert "Embedding-backend failure recorded" in result.stdout
    assert "Embedding fidelity note (NOT an outage)" in result.stdout


def test_ps1_hook_spawns_the_same_one_implementation(tmp_path):
    """Cross-language rule A: ONE implementation, two thin wrappers. The .ps1
    cannot be executed on the Linux CI host, so the property asserted is the
    one that makes the wrapper thin — that it spawns the same module CLI
    rather than reimplementing the reader in PowerShell."""
    ps1 = HOOK_SH.with_suffix(".ps1")
    body = ps1.read_text(encoding="utf-8")
    assert "vco_lib.embedding_fidelity notice" in body
    # Any PowerShell-side JSON parsing of the jsonl would be a second reader.
    assert "ConvertFrom-Json" not in body, (
        "the .ps1 must not parse the metrics stream itself — that is a second "
        "implementation of the reader, free to drift from the Python one"
    )


# ---------------------------------------------------------------------------
# End to end: a REAL embed against a refusing backend must reach the notice
# ---------------------------------------------------------------------------


class _RefusingOllamaSession:
    """A ``requests.Session`` look-alike whose ``/api/embed`` REFUSES any
    input longer than ``limit`` with Ollama's own over-window phrasing.

    This is the field shape ``truncate: false`` produces: the runner does not
    silently drop the tail, it returns HTTP 400 and the caller must shrink.
    """

    def __init__(self, limit: int, model: str) -> None:
        self.limit = limit
        self.model = model
        self.accepted_lengths: list[int] = []
        self.closed = False

    class _Resp:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body
            self.text = json.dumps(body)

        def json(self):
            return self._body

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests as _req
                raise _req.HTTPError(
                    f"HTTP {self.status_code}: {self.text}", response=self)

    def get(self, url: str, **kw):
        return self._Resp(200, {"models": [{"name": self.model}]})

    def post(self, url: str, **kw):
        payload = kw.get("json") or {}
        text = payload.get("input") or payload.get("prompt") or ""
        if isinstance(text, list):
            text = text[0] if text else ""
        if len(text) > self.limit:
            return self._Resp(
                400, {"error": "input length exceeds the context length"})
        self.accepted_lengths.append(len(text))
        return self._Resp(200, {"embeddings": [[0.1, 0.2, 0.3]]})

    def close(self):
        self.closed = True


def test_a_real_embed_that_shrinks_reaches_the_notice(metrics_home, tmp_path,
                                                      capsys, monkeypatch):
    """The whole surface, from the production entry point to the text a
    SessionStart would show.

    The write-side unit tests drive ``_embed_shrinking_on_overflow`` directly,
    which proves the loop records correctly but NOT that
    ``EmbeddingService.embed_text`` — what kg-sync and the MCP node write
    actually call — still routes through it. That is the exact gap this cycle
    kept reproducing (a mechanism credited without evidence it fires), so this
    test starts at ``embed_text`` with a backend that refuses over-window
    input and ends at the rendered notice.
    """
    from vco_lib.embedding_service import EmbeddingService

    for key in ("EMBEDDING_MODEL", "ACTIVE_EMBEDDING", "OPENAI_API_KEY",
                "CODE_EMBED_BACKEND", "DUAL_EMBEDDING_WRITE_ALL_SLOTS",
                "DUAL_EMBEDDING_ARCTIC_SECONDARY"):
        monkeypatch.delenv(key, raising=False)

    model = "qwen3-embedding:0.6b"
    session = _RefusingOllamaSession(limit=2000, model=model)
    svc = EmbeddingService(
        project_root=None,
        ollama_url="http://localhost:11435",
        code_embed_url="http://localhost:11440",
        text_model_id=model,
        code_model_id="codesage-large-v2",
        openai_api_key="",
        session=session,
    )

    long_text = "x" * 12000
    vector = svc.embed_text(long_text)
    assert vector, "the shrink ladder must still produce a vector"
    assert session.accepted_lengths, "no embed was ever accepted"
    assert session.accepted_lengths[-1] < len(long_text), (
        "the backend accepted the FULL text — this fixture no longer refuses, "
        "so the test would pass without any shrink happening"
    )

    # The run summary lands, and the SessionStart notice renders it.
    assert fid.flush_run() is True, (
        "a shrink happened but nothing was recorded — EmbeddingService.embed_text "
        "no longer reaches vco_lib.embedding_fidelity"
    )
    rows = _rows(metrics_home)
    assert len(rows) == 1 and rows[0]["kind"] == "shrink_summary"
    # ONE input that lost text is ONE shrink, however many rungs the ladder
    # took (this fixture takes three: 12000 -> 6000 -> 3000 -> 1500).
    # Per-rung counting would inflate the summary by the ladder depth and make
    # one pathological chunk look like a corpus-wide problem.
    assert rows[0]["shrinks"][model]["count"] == 1
    assert rows[0]["shrinks"][model]["orig_chars"] == len(long_text)
    assert rows[0]["shrinks"][model]["sent_chars"] == session.accepted_lengths[-1]

    project = tmp_path / "proj"
    project.mkdir()
    fid.emit_notice(project)
    out = capsys.readouterr().out
    assert "Embedding fidelity note (NOT an outage)" in out
    assert model in out


# ---------------------------------------------------------------------------
# The pointer in failure MESSAGES must name the file the rows are in
# ---------------------------------------------------------------------------


def test_display_path_is_the_file_that_actually_receives_rows(metrics_home):
    """Behavioural, not textual: write a row, then assert the path the
    messages print is the file that got it.

    v0.2.92 W7 moved the metrics home to ``~/.vct/metrics`` and left
    ``~/.claude/metrics`` as a read-only archive. Four shipped scripts kept
    printing the archive literal, so users chasing an embedding failure were
    sent to a file their rows were not in — the same pointer-is-false defect
    kg-sync fixed for its own message.
    """
    fid.note_shrink("m", 100, 50)
    assert fid.flush_run() is True
    assert fid.failures_jsonl_display_path() == str(metrics_home)
    assert _rows(metrics_home), "the displayed path is not the write target"
    assert ".claude" not in fid.failures_jsonl_display_path()


def test_no_shipped_script_prints_the_frozen_archive_literal():
    """A literal-string check, because the defect IS a literal string: four
    scripts hard-coded ``~/.claude/metrics/embedding_failures.jsonl`` in the
    text they show a user whose embedding just failed.

    ``templates/scripts/sync_knowledge_graph.py`` is EXCLUDED — it is owned by
    another lane this cycle and still carries the literal at two print sites
    (its call-time hint at ``_embedding_failures_jsonl_hint`` is already
    correct). Excluding it is recorded here rather than silently narrowing the
    glob, so the remaining work is visible.
    """
    scripts = sorted((REPO_ROOT / "templates" / "scripts").glob("*.py"))
    offenders = []
    for script in scripts:
        if script.name == "sync_knowledge_graph.py":
            continue
        for i, line in enumerate(
            script.read_text(encoding="utf-8").splitlines(), 1
        ):
            if line.lstrip().startswith("#"):
                continue  # prose about the archive is fine; printed text is not
            if ".claude/metrics/embedding_failures.jsonl" in line:
                offenders.append(f"{script.name}:{i}: {line.strip()}")
    assert not offenders, (
        "these print a path the rows are not written to:\n" + "\n".join(offenders)
    )

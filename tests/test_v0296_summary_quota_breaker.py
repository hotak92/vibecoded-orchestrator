# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 WP-7 — quota/trust breaker classes, other-strikes, degradation
notice, pending scan, backfill, recheck.

Three field defects from the 2026-09-20 register (issues 13 + 14):

1. Quota exhaustion rode the 900 s rate-limit cooldown — ~20 futile
   re-probes per 5 h outage — because "usage limit reached" and a bare
   transient 429 shared one class.
2. A trust-shaped CLI refusal ("this workspace has not been trusted")
   classified as ``other``, and ``other`` never tripped: 175+ per-symbol
   ``claude -p`` spawns, each dying, each firing the StopFailure hook.
3. Nothing told the user WHICH summaries had quietly moved to a fallback
   tier, and the hash gate froze those rows forever (ordinary runs
   correctly skip them) with no exit but editing sidecars by hand.

These tests pin: the quota class (5 h cooldown + knob), trust as
terminal-per-tier, the other-strike arm (3 consecutive, long cooldown),
the once-per-event ledger entry with a sidecar-derived pending count, the
EXACT backfill set, and the recheck (clears the breaker, regenerates
exactly the pending set, resolves the entry).

All synthetic: no LLM call, no Weaviate, no network, no real user state.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import make_launcher_db

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "templates" / "scripts"
_SB_PATH = _SCRIPTS / "summary_backends.py"
_KG_PATH = _SCRIPTS / "generate-kg-summary.py"

from vco_lib import summary_health as sh  # noqa: E402

_SUMMARY_ENV = (
    "KG_SUMMARY_BACKEND", "CODE_SUMMARY_BACKEND", "KG_SUMMARY_TIMEOUT",
    "VCO_SUMMARY_BREAKER", "VCO_SUMMARY_BREAKER_COOLDOWN",
    "VCO_SUMMARY_BREAKER_CAPACITY_COOLDOWN",
    "VCO_SUMMARY_BREAKER_CAPACITY_STRIKES",
    "VCO_SUMMARY_BREAKER_QUOTA_COOLDOWN",
    "VCO_SUMMARY_BREAKER_OTHER_COOLDOWN",
    "VCO_SUMMARY_BREAKER_OTHER_STRIKES",
    "VCT_ORCHESTRATOR_ROOT", "KG_PROJECT_ROOT",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
)


def _load(name: str, path: Path) -> types.ModuleType:
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolated VCT state dir — the breaker latch must never touch ~/.vct."""
    target = tmp_path / "vct-state"
    target.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(target))
    for key in _SUMMARY_ENV:
        monkeypatch.delenv(key, raising=False)
    return target


@pytest.fixture()
def sb(state_dir: Path):
    """Fresh summary_backends with a captured logger and empty caches."""
    mod = _load("_wp7_sb", _SB_PATH)
    mod.reset_backend_cache()
    mod.reset_breaker(persisted=True)
    lines: list[str] = []
    mod.set_logger(lines.append)
    setattr(mod, "log_lines", lines)
    return mod


def _pin_probes(monkeypatch, mod, *, cli=False, ollama=False) -> None:
    monkeypatch.setattr(mod, "cli_available", lambda: cli)
    monkeypatch.setattr(mod, "ollama_available", lambda: ollama)
    monkeypatch.setattr(mod, "openai_available", lambda: False)
    monkeypatch.setattr(mod, "api_available", lambda: False)


# ══════════════════════════════════════════════════════════════════════
# Task 1 — the QUOTA class: distinct from rate-limit, 5 h cooldown
# ══════════════════════════════════════════════════════════════════════
class TestQuotaClass:
    @pytest.mark.parametrize("text", [
        "Claude AI usage limit reached. Your limit will reset at 3pm.",
        "usage_limit exceeded for this account",
        "You're out of credits — add funds to continue.",
        "credit balance is too low",
        "HTTP 429: insufficient_quota — monthly quota exhausted",
    ])
    def test_quota_wording_is_quota_not_rate_limit(self, sb, text):
        reason, signal = sb.classify_backend_failure(text=text)
        assert reason == "quota", text
        assert signal, "the matched keyword must be recorded"

    def test_a_429_whose_body_says_quota_is_quota(self, sb):
        """The brief's exact split: quota-exhaustion wording outranks the
        transient status code; a BARE 429 stays rate-limit."""
        assert sb.classify_backend_failure(
            status=429, text="too many requests") == ("rate_limit", "429")
        reason, _ = sb.classify_backend_failure(
            status=429, text="quota exceeded for this billing period")
        assert reason == "quota"

    def test_rate_limit_wording_stays_rate_limit(self, sb):
        """The split must not eat transient failures: no quota vocabulary,
        no exhaustion semantics."""
        assert sb.classify_backend_failure(
            text="rate limit exceeded, retry after 32s")[0] == "rate_limit"
        assert sb.classify_backend_failure(
            text="rate_limit_error: too many requests")[0] == "rate_limit"

    def test_quota_demotes_on_the_first_occurrence_for_5_hours(self, sb):
        assert sb.trip_backend("cli", "quota", "usage limit") is True
        record = sb.breaker_state("cli")
        assert record is not None and record["reason"] == "quota"
        assert record["remaining"] > 17_000, (
            "quota must cool down for HOURS (5 h default), not the 900 s "
            "rate-limit window — the ~20-futile-retries-per-outage defect"
        )

    def test_quota_cooldown_default_and_knob(self, sb, monkeypatch):
        assert sb.DEFAULT_QUOTA_COOLDOWN_S == 18_000.0
        assert sb.cooldown_for("quota") == 18_000.0
        monkeypatch.setenv("VCO_SUMMARY_BREAKER_QUOTA_COOLDOWN", "3600")
        assert sb.cooldown_for("quota") == 3600.0

    def test_quota_death_descends_and_never_reprobes(self, sb, monkeypatch):
        """The register-issue-13 regression: auth present, API refused for
        QUOTA — the tier opens the QUOTA class and later nodes skip it."""
        _pin_probes(monkeypatch, sb, cli=True, ollama=True)
        calls: list[str] = []

        def _cli(prompt):
            calls.append(prompt)
            raise sb.BackendUnavailable(
                "cli", "quota", "usage limit",
                "claude CLI failed: Claude AI usage limit reached")

        monkeypatch.setattr(sb, "call_cli", _cli)
        monkeypatch.setattr(sb, "call_ollama",
                            lambda p, **kw: "Served by the fallback tier.")
        for _ in range(5):
            assert sb.call_llm("x") == "Served by the fallback tier."
        assert len(calls) == 1
        assert sb.breaker_state("cli")["reason"] == "quota"


# ══════════════════════════════════════════════════════════════════════
# Task 3 — TRUST is terminal for the tier
# ══════════════════════════════════════════════════════════════════════
class TestTrustTerminal:
    @pytest.mark.parametrize("text", [
        "this workspace has not been trusted",
        "Error: the workspace has not been trusted yet",
        "workspace trust is required to run in this folder",
        "untrusted workspace: refusing to start",
    ])
    def test_trust_wording_classifies_trust(self, sb, text):
        reason, signal = sb.classify_backend_failure(text=text)
        assert reason == "trust", text
        assert signal

    def test_trust_outranks_status_codes(self, sb):
        reason, _ = sb.classify_backend_failure(
            status=403, text="forbidden: workspace has not been trusted")
        assert reason == "trust"

    def test_trust_failure_is_terminal_no_per_node_retry(self, sb,
                                                          monkeypatch):
        """The register-issue-14 storm: a real non-zero exit carrying the
        trust wording. The FIRST node classifies trust and demotes; the
        second node must not spawn the CLI again."""
        _pin_probes(monkeypatch, sb, cli=True, ollama=True)
        spawns: list[list] = []

        def _fake_run(argv, **kwargs):
            spawns.append(list(argv))
            if len(spawns) == 1:
                return types.SimpleNamespace(
                    returncode=1, stdout="",
                    stderr="Error: this workspace has not been trusted")
            return types.SimpleNamespace(
                returncode=0, stdout="ok", stderr="")

        monkeypatch.setattr(sb.shutil, "which", lambda name: "/usr/bin/claude")
        monkeypatch.setattr("subprocess.run", _fake_run)
        monkeypatch.setattr(sb, "call_ollama",
                            lambda p, **kw: "Fallback tier summary.")

        assert sb.call_llm("node one") == "Fallback tier summary."
        cli_spawn_argv = [a for a in spawns
                          if a and a[0] == "/usr/bin/claude"]
        assert cli_spawn_argv, "the first node did try the CLI"
        spawns.clear()
        assert sb.call_llm("node two") == "Fallback tier summary."
        cli_again = [a for a in spawns if a and a[0] == "/usr/bin/claude"]
        assert cli_again == [], (
            "the trust-failed CLI was retried for the second node — the "
            "175+-spawn storm defect"
        )
        assert sb.breaker_state("cli")["reason"] == "trust"

    def test_trust_shares_the_long_cooldown(self, sb):
        assert sb.trip_backend("cli", "trust", "not been trusted") is True
        record = sb.breaker_state("cli")
        assert record["reason"] == "trust"
        assert record["remaining"] > 17_000


# ══════════════════════════════════════════════════════════════════════
# Task 2 — the OTHER strike arm: an unclassified storm halts
# ══════════════════════════════════════════════════════════════════════
class TestOtherStrikes:
    def test_a_single_unknown_failure_still_never_demotes(self, sb):
        assert sb.trip_backend("cli", "other", "") is False
        assert sb.breaker_state("cli") is None

    def test_other_demotes_after_three_consecutive_strikes(self, sb):
        assert sb.trip_backend("cli", "other", "") is False
        assert sb.trip_backend("cli", "other", "") is False
        assert sb.trip_backend("cli", "other", "") is True
        record = sb.breaker_state("cli")
        assert record is not None and record["reason"] == "other"
        assert record["remaining"] > 17_000, (
            "the storm arm must cool down for HOURS — the 304-consecutive-"
            "failure storm ran a whole night because nothing ever tripped"
        )

    def test_a_success_between_strikes_resets_the_count(self, sb):
        sb.trip_backend("cli", "other", "")
        sb.clear_backend_trip("cli")               # a call succeeded
        assert sb.trip_backend("cli", "other", "") is False
        assert sb.breaker_state("cli") is None

    def test_other_strikes_survive_the_process_boundary(self, sb, state_dir):
        """One process per node (the generators' spawn shape): the strike
        count must accumulate ACROSS loads or the arm can never fire —
        the same cross-process argument as v0.2.92 BLOCKER-2."""
        assert sb.trip_backend("cli", "other", "") is False
        second = _load("_wp7_other2", _SB_PATH)
        second.reset_backend_cache()
        assert second._BREAKER_MEM == {}
        assert second.trip_backend("cli", "other", "") is False
        third = _load("_wp7_other3", _SB_PATH)
        third.reset_backend_cache()
        assert third.trip_backend("cli", "other", "") is True
        assert third.breaker_state("cli")["reason"] == "other"

    def test_strikes_and_cooldown_knobs(self, sb, monkeypatch):
        monkeypatch.setenv("VCO_SUMMARY_BREAKER_OTHER_STRIKES", "2")
        monkeypatch.setenv("VCO_SUMMARY_BREAKER_OTHER_COOLDOWN", "120")
        assert sb.other_strikes() == 2
        assert sb.cooldown_for("other") == 120.0
        assert sb.trip_backend("cli", "other", "") is False
        assert sb.trip_backend("cli", "other", "") is True

    def test_the_storm_actually_stops_the_walk(self, sb, monkeypatch):
        """End to end through call_llm: three consecutive unclassified CLI
        failures demote the tier (the third call falls through to the
        fallback in the SAME call_llm), so node four goes straight to the
        fallback instead of spawning the CLI again."""
        _pin_probes(monkeypatch, sb, cli=True, ollama=True)
        cli_calls: list[str] = []

        def _cli(prompt):
            cli_calls.append(prompt)
            raise RuntimeError(
                "claude CLI failed: unexpected json shape 0x304")

        monkeypatch.setattr(sb, "call_cli", _cli)
        monkeypatch.setattr(sb, "call_ollama",
                            lambda p, **kw: "Fallback after the storm.")
        # Strikes 1 and 2: below the threshold, so the failure propagates.
        for _ in range(2):
            with pytest.raises(RuntimeError):
                sb.call_llm("x")
        assert len(cli_calls) == 2
        # Strike 3: the arm fires, THIS call falls through to the fallback.
        assert sb.call_llm("node three") == "Fallback after the storm."
        assert len(cli_calls) == 3
        # The storm is over: the tier is open, no CLI spawn for node four.
        assert sb.call_llm("node four") == "Fallback after the storm."
        assert len(cli_calls) == 3, "the storm did not halt"


# ══════════════════════════════════════════════════════════════════════
# Task 4 — the degradation notice, once per event, with the pending count
# ══════════════════════════════════════════════════════════════════════
def _write_node(root: Path, rel: str, body: str) -> str:
    node = root / rel
    node.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\ntitle: {Path(rel).stem}\n---\n\n{body}"
    node.write_text(text, encoding="utf-8")
    return text


def _kg_entry(backend: str, full_text: str) -> dict:
    return {
        "title": "x", "description": "d", "summary": "s",
        "generated_at": "2026-09-20T00:00:00Z",
        "content_hash": sh._kg_content_hash(full_text), "backend": backend,
    }


@pytest.fixture()
def degraded_project(tmp_path: Path) -> Path:
    """A=ollama+current hash (FROZEN, pending), B=cli+current (fine),
    C=no entry (pending), D=ollama+stale hash (ordinary runs regenerate —
    NOT pending); code entries E=ollama (pending), F=cli (fine)."""
    root = tmp_path / "proj"
    (root / "knowledge" / "concepts").mkdir(parents=True)
    text_a = _write_node(root, "knowledge/concepts/a.md", "alpha body")
    text_b = _write_node(root, "knowledge/concepts/b.md", "beta body")
    _write_node(root, "knowledge/concepts/c.md", "gamma body")
    text_d = _write_node(root, "knowledge/concepts/d.md", "delta body")
    (root / "knowledge" / ".node_formats.json").write_text(
        json.dumps({
            "knowledge/concepts/a.md": _kg_entry("ollama", text_a),
            "knowledge/concepts/b.md": _kg_entry("cli", text_b),
            # D's stored hash matches OLDER content: not pending.
            "knowledge/concepts/d.md": {
                **_kg_entry("ollama", text_d),
                "content_hash": "0" * 16,
            },
        }, indent=2), encoding="utf-8")
    (root / ".claude").mkdir()
    (root / ".claude" / ".code_formats.json").write_text(
        json.dumps({
            "src/a.py::mod.func_e": {"one_liner": "e", "backend": "ollama"},
            "src/a.py::mod.func_f": {"one_liner": "f", "backend": "cli"},
        }, indent=2), encoding="utf-8")
    return root


class TestScanPending:
    def test_pending_set_is_exactly_the_degraded_rows(self, degraded_project):
        pending = sh.scan_pending(degraded_project, preferred="cli")
        assert pending.kg_stale == ["knowledge/concepts/a.md"]
        assert pending.kg_missing == ["knowledge/concepts/c.md"]
        assert pending.code_stale == ["src/a.py::mod.func_e"]
        assert pending.total == 3

    def test_hash_parity_with_the_generator(self):
        """The scan's hash must BE the sidecar's scheme (declared C-tier
        mirror) — drift here means frozen rows read as pending (or vice
        versa) forever."""
        kg = _load("_wp7_kghash", _KG_PATH)
        sample = "---\ntitle: T\n---\n\nbody with ünicode and --- separators"
        assert sh._kg_content_hash(sample) == kg.content_hash(sample)

    def test_missing_project_is_all_missing(self, tmp_path):
        root = tmp_path / "fresh"
        (root / "knowledge").mkdir(parents=True)
        _write_node(root, "knowledge/n.md", "body")
        pending = sh.scan_pending(root)
        assert pending.kg_missing == ["knowledge/n.md"]
        assert pending.code_stale == []


class TestDegradationNotice:
    def _ledger(self, root: Path) -> Path:
        return root / ".claude" / "context" / "UPDATE_DEFERRED.md"

    def test_notice_emitted_once_with_the_pending_count(self,
                                                        degraded_project):
        ok = sh.note_degradation(
            degraded_project, tier="cli", reason="quota")
        assert ok is True
        ledger = self._ledger(degraded_project).read_text(encoding="utf-8")
        assert "kg_summaries_degraded" in ledger
        assert "1 KG node(s) on a fallback backend" in ledger
        assert "1 KG node(s) with no summary" in ledger
        assert "1 code entit" in ledger
        assert "summary-recheck" in ledger
        assert ledger.count("## kg_summaries_degraded") == 1

    def test_re_notifying_replaces_it_never_stacks(self, degraded_project):
        sh.note_degradation(degraded_project, tier="cli", reason="quota")
        sh.note_degradation(degraded_project, tier="cli", reason="quota")
        ledger = self._ledger(degraded_project).read_text(encoding="utf-8")
        assert ledger.count("## kg_summaries_degraded") == 1, (
            "the entry must be once-per-EVENT — per-cid last-write-wins"
        )

    def test_trust_flavour_names_the_human_exit(self, degraded_project):
        sh.note_degradation(degraded_project, tier="cli", reason="trust")
        ledger = self._ledger(degraded_project).read_text(encoding="utf-8")
        assert "trust" in ledger
        assert "re-accept the trust dialog" in ledger

    def test_breaker_trip_spawns_the_notifier_only_for_quota_or_trust(
            self, sb, monkeypatch):
        """The trip seam's argv + its pytest guard. Under PYTEST_CURRENT_TEST
        the spawn must not happen at all; the argv is pinned directly."""

        def _explode(*a, **kw):
            raise AssertionError("Popen must not run under PYTEST_CURRENT_TEST")

        monkeypatch.setattr("subprocess.Popen", _explode)
        assert sb.trip_backend("cli", "quota", "usage limit") is True
        assert sb.trip_backend("cli", "trust", "not been trusted") is True
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        argv = sb._degradation_notify_argv("cli", "quota")
        assert argv is not None
        assert argv[1:6] == ["-m", "vco_lib.summary_health",
                             "note-degradation", "--tier", "cli"]
        assert argv[6:] == ["--reason", "quota",
                            "--project-root", argv[-1]]
        assert argv[-1], "the project root must be a real path"

    def test_notify_argv_needs_a_resolvable_root(self, sb, monkeypatch):
        monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", "/definitely/not/here")
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        # The derived fallback IS this repo (templates/scripts parent), so
        # force it to fail too by pointing the module file elsewhere.
        monkeypatch.setattr(sb, "__file__", "/nowhere/scripts/sb.py")
        assert sb._degradation_notify_argv("cli", "quota") is None


# ══════════════════════════════════════════════════════════════════════
# Tasks 5+6 — backfill regenerates EXACTLY the pending set; recheck clears
# ══════════════════════════════════════════════════════════════════════
class TestBackfill:
    def _fake_spawn(self, root: Path):
        """Simulates a successful regeneration through the preferred tier.

        The KG child's write is the REAL generator's entry shape
        (generate-kg-summary.py::main): title/description/summary/
        generated_at/content_hash/backend — a faithful fake matters here,
        because scan_pending counts a row with no summary text as STILL
        missing (the generator's own stored_entry_is_usable semantics)."""
        calls: list[tuple[list, dict]] = []

        def spawn(argv, *, cwd, env):
            calls.append((list(argv), dict(env)))
            if argv[1].endswith("generate-kg-summary.py"):
                node = Path(argv[2])
                rel = str(node.relative_to(root))
                sidecar = root / "knowledge" / ".node_formats.json"
                formats = json.loads(sidecar.read_text(encoding="utf-8"))
                formats[rel] = {**formats.get(rel, {}),
                                "title": node.stem,
                                "description": "regenerated description",
                                "summary": "regenerated summary",
                                "generated_at": "2026-09-21T00:00:00Z",
                                "backend": "cli",
                                "content_hash": sh._kg_content_hash(
                                    node.read_text(encoding="utf-8"))}
                sidecar.write_text(json.dumps(formats, indent=2),
                                   encoding="utf-8")
            elif argv[1].endswith("generate-code-summary.py"):
                sidecar = root / ".claude" / ".code_formats.json"
                formats = json.loads(sidecar.read_text(encoding="utf-8"))
                formats["src/a.py::mod.func_e"] = {
                    "one_liner": "regenerated", "backend": "cli"}
                sidecar.write_text(json.dumps(formats, indent=2),
                                   encoding="utf-8")
            return 0

        return spawn, calls

    def test_backfill_regenerates_exactly_the_pending_set(self, sb,
                                                          degraded_project):
        sh._sb.reset_breaker(persisted=True)
        sb.trip_backend("cli", "quota", "usage limit")
        pending = sh.scan_pending(degraded_project)
        spawn, calls = self._fake_spawn(degraded_project)
        result = sh.backfill(
            degraded_project, pending, spawn=spawn, project_name="Proj")

        # Exactly the pending KG nodes, forced, with the project env.
        kg_calls = [c for c in calls
                    if c[0][1].endswith("generate-kg-summary.py")]
        assert sorted(str(Path(c[0][2]).relative_to(degraded_project))
                      for c in kg_calls) == [
            "knowledge/concepts/a.md", "knowledge/concepts/c.md"]
        assert all(c[0][3] == "--force" for c in kg_calls)
        assert all(c[1]["KG_PROJECT_ROOT"] == str(degraded_project)
                   for c in calls)

        # The code leg: exactly the stale key deleted, then ONE run.
        code_calls = [c for c in calls
                      if c[0][1].endswith("generate-code-summary.py")]
        assert len(code_calls) == 1
        assert "--project" in code_calls[0][0]
        after = sh.scan_pending(degraded_project)
        assert after.total == 0
        assert result.failures == 0 and result.code_leg_skipped is False

    def test_code_leg_refuses_an_unresolved_project_name(self,
                                                         degraded_project,
                                                         monkeypatch,
                                                         tmp_path):
        """The GC hazard: with no positive launcher.db resolution the code
        generator must NOT run — its gc_dead_keys would prune the sidecar
        against an empty prefix. The stale keys stay untouched."""
        monkeypatch.setenv("VCT_LAUNCHER_DB_PATH",
                           str(tmp_path / "no-such.db"))
        pending = sh.scan_pending(degraded_project)
        spawn, calls = self._fake_spawn(degraded_project)
        result = sh.backfill(degraded_project, pending, spawn=spawn)
        assert result.code_leg_skipped is True
        code_calls = [c for c in calls
                      if c[0][1].endswith("generate-code-summary.py")]
        assert code_calls == []
        sidecar = json.loads(
            (degraded_project / ".claude" / ".code_formats.json")
            .read_text(encoding="utf-8"))
        assert "src/a.py::mod.func_e" in sidecar

    def test_code_leg_resolves_the_name_from_launcher_db(
            self, degraded_project, monkeypatch, tmp_path):
        _register_project(monkeypatch, tmp_path, degraded_project, "Proj")
        assert sh._resolve_project_name(degraded_project) == "Proj"
        pending = sh.scan_pending(degraded_project)
        spawn, calls = self._fake_spawn(degraded_project)
        sh.backfill(degraded_project, pending, spawn=spawn)
        code_calls = [c for c in calls
                      if c[0][1].endswith("generate-code-summary.py")]
        assert len(code_calls) == 1
        i = code_calls[0][0].index("--project")
        assert code_calls[0][0][i + 1] == "Proj"


def _register_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                      root: Path, name: str) -> None:
    """Point VCT_LAUNCHER_DB_PATH at a tmp launcher.db registering *root*.

    The code leg's positive project-name resolution reads this — without
    it the leg would (correctly) refuse to run. Built through the ONE
    fixture (tests/common/launcher_db_fixture) so the schema is the
    launcher's real one; the hand-rolled projects-table DDL this replaced
    was a guessed partial schema whose ``host='local'`` value the real
    CHECK constraint rejects.
    """
    db = make_launcher_db(tmp_path, projects=[{
        "project_id": "p1", "name": name, "folder_path": root,
    }])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))


class TestRecheck:
    def test_recheck_clears_the_breaker_and_backfills(self, sb,
                                                      degraded_project,
                                                      monkeypatch, tmp_path):
        _register_project(monkeypatch, tmp_path, degraded_project, "Proj")
        sb.trip_backend("cli", "quota", "usage limit")
        assert sb.breaker_state("cli") is not None
        sh.note_degradation(degraded_project, tier="cli", reason="quota")
        ledger = degraded_project / ".claude" / "context" / "UPDATE_DEFERRED.md"
        assert ledger.exists()

        spawn, calls = TestBackfill()._fake_spawn(degraded_project)
        result = sh.summary_recheck(degraded_project, spawn=spawn)

        # The breaker latch (cross-process file) is gone.
        assert not (sb._breaker_path()).exists()
        assert result.pending_before == 3 and result.pending_after == 0
        assert sh.scan_pending(degraded_project).total == 0
        # The paired resolution removed the entry.
        assert not ledger.exists()

    def test_recheck_is_idempotent(self, degraded_project):
        result = sh.summary_recheck(degraded_project, spawn=lambda *a, **k: 0)
        assert result.pending_before == 3
        second = sh.summary_recheck(degraded_project,
                                    spawn=lambda *a, **k: 0)
        assert second.pending_before == second.pending_after

    def test_recheck_keeps_the_entry_when_still_degraded(self, sb,
                                                         degraded_project):
        """Tokens still gone: the children fall back again, the post-scan
        finds the same pending set, the entry STAYS — the degradation is
        still real."""
        sh.note_degradation(degraded_project, tier="cli", reason="quota")
        ledger = degraded_project / ".claude" / "context" / "UPDATE_DEFERRED.md"

        def noop_spawn(argv, *, cwd, env):
            return 1                     # regeneration failed everywhere

        result = sh.summary_recheck(degraded_project, spawn=noop_spawn)
        assert result.pending_after == 3
        assert ledger.exists()

    def test_cli_main_wiring(self, degraded_project, capsys):
        spawn, _calls = TestBackfill()._fake_spawn(degraded_project)
        import vco_lib.summary_health as live
        original = live.backfill
        live.backfill = lambda root, pending, **kw: type(
            "R", (), {"spawned": 0, "failures": 0,
                      "code_leg_skipped": False})()
        try:
            rc = live.main(["summary-recheck",
                            "--project-root", str(degraded_project)])
        finally:
            live.backfill = original
        assert rc == 0
        out = capsys.readouterr().out
        assert "recheck" in out

    def test_cli_main_note_degradation(self, degraded_project, capsys):
        rc = sh.main(["note-degradation",
                      "--project-root", str(degraded_project),
                      "--tier", "cli", "--reason", "quota"])
        assert rc == 0
        assert (degraded_project / ".claude" / "context"
                / "UPDATE_DEFERRED.md").exists()


# ══════════════════════════════════════════════════════════════════════
# Registry — the row exists and matches what the module emits
# ══════════════════════════════════════════════════════════════════════
class TestRegistry:
    def test_condition_id_matches_the_registry_row(self):
        import tomllib
        with (_REPO_ROOT / "vco_lib" / "deferral_conditions.toml").open(
                "rb") as f:
            row = tomllib.load(f)["conditions"][sh.CONDITION_ID]
        assert row["class"] == "action_required"
        assert row["owner"] == "vco_lib.summary_health"
        assert row["clear_probe"] == "paired-resolution"
        assert "ledger" in row["emit_surfaces"]


class TestTrustOutranksQuota:
    """WP-7a review MINOR-1: the trust-before-quota ordering was unpinned —
    the review's own mutation probe (trust match moved AFTER quota) stayed
    GREEN because no test exercised a payload carrying BOTH signals. Trust
    wording CAN co-occur with quota vocabulary (a 429 body appended to a
    trust refusal), and the classification must still say trust: it is the
    terminal class (never retried), while quota would keep retrying every
    5 h into a wall that needs a human dialog instead."""

    def test_trust_wording_beats_quota_wording(self, sb):
        reason, _ = sb.classify_backend_failure(
            status=429,
            text="rate limit exceeded (usage limit reached); "
                 "this workspace has not been trusted",
        )
        assert reason == "trust", (
            "a payload carrying both trust and quota vocabulary must "
            "classify trust — terminal, not retried on the quota cadence"
        )

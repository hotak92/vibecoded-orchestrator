# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 S16-8 (owner 2026-10-07, register §16) — Bash/tool injection parity.

For each (tool call, equivalent bash command) pair the Bash path must build
the SAME retrieval keys the tool surface builds:

  * the same target path(s),
  * the same KG query text (→ the same KG titles),
  * the same code-graph symbols (→ the same exact def+callers blocks),

mapped onto the surface's OWN §2.1 profile. The profiles differ BY DESIGN
(bash_read floor 0.75 vs read_code 0.725 — the KG_GATES/CG_POLICIES table is
the one home for that difference), so parity is asserted on the KEYS, and the
designed profile pair is pinned per row.

The plans are driven DIRECTLY — the pure classifier
(``vco_lib.inject_intent.classify_bash``) plus the router's plan builders
(``hook_context_router._bash_plan`` / ``_read_plan`` / ``_edit_plan`` /
``_write_plan`` / ``_grep_plan``). No hook spawn, no producers, no network.

RED on the base tree for the gap rows (each names its fix):

* **R1** — ``perl -pi -e 's/a/b/' f`` was MECHANICAL: the intent write-verb
  regex only knew ``perl -i`` / ``ruby -i`` / ``sed -i`` as SEPARATE flag
  tokens, so the canonical grouped in-place shape (``-pi``) produced no
  EDIT and no keys at all. Fix: ``_INTENT_WRITE_VERB_RE`` accepts grouped
  in-place flag clusters (``inject_intent.py``).
* **R2** — ``Grep(pattern='def foo')`` produced NO code-graph symbol while
  the bash ``grep -n 'def foo' f`` produced ``foo``: the SF-3 multi-word
  retry (strip the leading code keyword, retry the words) lived only in the
  bash classifier. Fix: one home ``inject_intent.search_symbol_for_pattern``,
  called by ``_classify_search`` AND the router's ``_grep_plan``.
* **R3 (measured, pinned NOT aligned)** — ``cat f`` queried 2 code-graph
  symbols where ``Read(f)`` queried 5 (the §2.1 WEAK-surface taper:
  bash_read is the table's smallest surface — 0.75 floor, titles-only below
  0.85, single_chunk above). Aligning to the Read surface's 5 was IMPLEMENTED
  and measured on the replay corpus: precision 0.625 → 0.556 for NO recall
  gain (0.857 → 0.857) — the lane brief's own guard ("a recall gain must
  not cost precision") forbids the trade, so the taper stays and the
  PREFIX relation is what this file pins: the bash READ CG keys are the
  Read surface's first 2, from the same source (file_pub_symbols), on the
  same file. Full alignment is an owner decision (the measurement is in the
  lane report).
* **R4** — ``cd sub && cat only.py`` keyed on the bare ``only.py`` (the
  router resolved it against the session cwd → the WRONG file: no parent-dir
  topic word, no CG symbols) where ``Read(sub/only.py)`` keys on the
  resolved path. Fix: the classifier tracks ``cd`` segments and re-roots
  later relative targets (``inject_intent.py``).
* **R5** — ``sed -i`` keyed its KG query on the path topic only where the
  Edit tool folds the edited symbols into the same query. Fix: the router
  mirrors the Edit shape for IN-PLACE writes (the classifier's new
  ``write_kind``); overwrite shapes (tee / redirect / cp) keep the Write
  tool's topic-only key.
* **R7** — a bare-filename target queried the KG with the stem only
  (``app``) where the tool surfaces add the parent-dir word
  (``app <dir>`` — their paths are absolute). Fix:
  ``kg_query_for_targets`` resolves relative targets against cwd.

Documented divergences, pinned (NOT gaps):

* ``git show <rev>:<path>`` adds a revision pin (rev_gate) no Read call can
  express — same keys PLUS the pin.
* A heredoc write to a knowledge/docs target queries the KG with the body
  SNIPPET (``bash_write_targets.prebash_query_parts``' rule — the content of
  a knowledge write IS its topic) where the Write surface keys on the path
  topic (§C3: a new file's own text is not a useful query).
* ``python -c "open('f','w')…"`` is UNRECOVERABLE by design
  (``bash_write_targets.py`` — "What it provably CANNOT recognise": a write
  performed by an interpreter from its own source text is not recoverable
  without executing it) → MECHANICAL, pinned.
* ``cp a b`` is an EDIT-shape (overwrite) write by the shipped v0.2.101
  design (the intent write-verb vocabulary, GLM review nit-6). The lane
  brief's "(MECHANICAL)" annotation does not match the source; the actual
  behaviour is pinned here and the mismatch reported, not "fixed".
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "claude_mcp_servers" / "scripts"
for _p in (str(SCRIPTS), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# `hook_context_router` is a runtime SCRIPT (claude_mcp_servers/scripts), not
# an importable package — it resolves only via the sys.path insert above, so
# pyright cannot follow it (same contract as the WP-E replay harness).
import hook_context_router as ROUTER  # noqa: E402  # pyright: ignore[reportMissingImports]
from vco_lib.inject_intent import (  # noqa: E402
    INTENT_EDIT,
    INTENT_MECHANICAL,
    INTENT_READ,
    INTENT_SEARCH,
    classify_bash,
)

# --- fixture project -----------------------------------------------------------

_APP_BODY = "".join(f"def f{i}():\n    pass\n" for i in range(5))
_MOD_BODY = "def foo():\n    x = 1\n    return x\n"
_ONLY_BODY = "def sole():\n    pass\n"


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text(_APP_BODY, encoding="utf-8")
    (tmp_path / "mod.py").write_text(_MOD_BODY, encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "only.py").write_text(_ONLY_BODY, encoding="utf-8")
    (tmp_path / "src").mkdir()
    return tmp_path


# --- key extraction --------------------------------------------------------------


def _bash_plan(cmd: str, cwd: Path):
    return ROUTER._bash_plan({"tool_input": {"command": cmd}}, str(cwd))  # noqa: SLF001


def _read_plan(path: Path, cwd: Path, content: str):
    return ROUTER._read_plan(  # noqa: SLF001
        {"tool_input": {"file_path": str(path)},
         "tool_response": {"content": content}},
        str(cwd),
    )


def _edit_plan(path: Path, cwd: Path, old_string: str):
    return ROUTER._edit_plan(  # noqa: SLF001
        {"tool_input": {"file_path": str(path), "old_string": old_string}},
        str(cwd),
    )


def _write_plan(path: Path, cwd: Path):
    return ROUTER._write_plan({"tool_input": {"file_path": str(path)}}, str(cwd))  # noqa: SLF001


def _grep_plan(pattern: str, cwd: Path, path: str = ""):
    return ROUTER._grep_plan(  # noqa: SLF001
        {"tool_input": {"pattern": pattern, "path": path}}, str(cwd)
    )


def _symbols(plan) -> list[str]:
    return sorted({s for s, _f in plan.cg_symbols})


def _ordered_symbols(plan) -> list[str]:
    out: list[str] = []
    for s, _f in plan.cg_symbols:
        if s not in out:
            out.append(s)
    return out


def _resolved_sources(plan, cwd: Path) -> list[str]:
    out = []
    for _s, f in plan.cg_symbols:
        out.append(os.path.normpath(f if os.path.isabs(f) else os.path.join(str(cwd), f)))
    return sorted(set(out))


def _assert_same_keys(bash_plan, tool_plan, cwd: Path) -> None:
    """The parity assertion: same KG query text, same CG symbols, same
    (resolved) CG source file."""
    assert bash_plan.kg_query == tool_plan.kg_query, (
        f"KG query: bash={bash_plan.kg_query!r} tool={tool_plan.kg_query!r}")
    assert _symbols(bash_plan) == _symbols(tool_plan), (
        f"CG symbols: bash={_symbols(bash_plan)} tool={_symbols(tool_plan)}")
    assert _resolved_sources(bash_plan, cwd) == _resolved_sources(tool_plan, cwd), (
        f"CG sources: bash={_resolved_sources(bash_plan, cwd)} "
        f"tool={_resolved_sources(tool_plan, cwd)}")


# --- READ family ------------------------------------------------------------------


def _assert_same_read_keys(bash_plan, tool_plan, cwd: Path) -> None:
    """The READ parity assertion: same KG query text, same (resolved) CG
    source file, and the bash CG symbols are the tool surface's FIRST
    len(bash) — the §2.1 weak-surface taper (R3, measured and pinned)."""
    assert bash_plan.kg_query == tool_plan.kg_query, (
        f"KG query: bash={bash_plan.kg_query!r} tool={tool_plan.kg_query!r}")
    bs, ts = _ordered_symbols(bash_plan), _ordered_symbols(tool_plan)
    assert bs == ts[: len(bs)], (
        f"CG symbols must be the Read surface's tapered prefix: bash={bs} tool={ts}")
    assert _resolved_sources(bash_plan, cwd) == _resolved_sources(tool_plan, cwd), (
        f"CG sources: bash={_resolved_sources(bash_plan, cwd)} "
        f"tool={_resolved_sources(tool_plan, cwd)}")


class TestReadParity:
    """`cat`/`sed -n`/`head`/`tail`/`less`/`git diff` of a file ≡ Read(file).

    R7 (the bare-filename KG topic) is RED on the base tree; R3 is the
    measured taper pin (the 5-def app.py forces it)."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "cat app.py",
            "sed -n '10,40p' app.py",
            "head -n 50 app.py",
            "tail -n 30 app.py",
            "less app.py",
            "git diff app.py",
        ],
    )
    def test_bash_read_keys_equal_read_tool(self, cmd: str, proj: Path) -> None:
        bp = _bash_plan(cmd, proj)
        rp = _read_plan(proj / "app.py", proj, _APP_BODY)
        assert classify_bash(cmd, str(proj)).intent == INTENT_READ
        assert bp.kg_profile == "bash_read" and rp.kg_profile == "read_code"
        _assert_same_read_keys(bp, rp, proj)

    def test_bash_read_cg_is_the_weak_surface_taper(self, proj: Path) -> None:
        """R3 pinned: the bash READ CG keys are the Read surface's FIRST 2
        symbols (the §2.1 weak-surface taper), from the SAME source
        (file_pub_symbols) on the SAME file — not a different or stale set.
        Full 5-alignment was implemented and measured on the replay:
        precision 0.625 → 0.556 for no recall gain (0.857), so the taper
        stays (the brief's guard: a recall gain must not cost precision)."""
        bp = _bash_plan("cat app.py", proj)
        rp = _read_plan(proj / "app.py", proj, _APP_BODY)
        assert _symbols(rp) == ["f0", "f1", "f2", "f3", "f4"]
        assert _symbols(bp) == ["f0", "f1"]


class TestRevPinParity:
    def test_git_show_rev_path_is_read_plus_pin(self, proj: Path) -> None:
        """`git show HEAD:f` ≡ Read(f) PLUS the revision pin no Read call can
        express (the router stays silent when the code-graph stamp != rev)."""
        bi = classify_bash("git show HEAD:app.py", str(proj))
        assert bi.intent == INTENT_READ
        assert ("HEAD", "app.py") in bi.rev_paths
        bp = _bash_plan("git show HEAD:app.py", proj)
        rp = _read_plan(proj / "app.py", proj, _APP_BODY)
        assert bp.rev_gate is True and rp.rev_gate is False
        _assert_same_read_keys(bp, rp, proj)


# --- SEARCH family ----------------------------------------------------------------


class TestSearchParity:
    """Bash grep/rg ≡ the Grep tool: same exact-lookup symbol, NO KG leg on
    either surface (§2.1)."""

    def test_grep_rn_identifier_in_dir(self, proj: Path) -> None:
        bp = _bash_plan("grep -rn validate_token src/", proj)
        gp = _grep_plan("validate_token", proj, path=str(proj / "src"))
        assert classify_bash("grep -rn validate_token src/", str(proj)).intent == INTENT_SEARCH
        assert bp.kg_profile == "" and gp.kg_profile == ""  # no KG leg (§2.1)
        assert bp.kg_query == gp.kg_query == ""
        assert _symbols(bp) == _symbols(gp) == ["validate_token"]
        assert bp.cg_profile == "bash_search" and gp.cg_profile == "grep"

    def test_rg_identifier(self, proj: Path) -> None:
        bp = _bash_plan("rg validate_token", proj)
        gp = _grep_plan("validate_token", proj)
        assert _symbols(bp) == _symbols(gp) == ["validate_token"]
        assert bp.kg_query == gp.kg_query == ""

    def test_keyword_pattern_symbol(self, proj: Path) -> None:
        """R2 — RED on base: `grep -n 'def foo' f` yielded `foo` while the
        Grep tool yielded NOTHING (the SF-3 retry was bash-only)."""
        cmd = "grep -n 'def foo' mod.py"
        bp = _bash_plan(cmd, proj)
        gp = _grep_plan("def foo", proj, path=str(proj / "mod.py"))
        assert classify_bash(cmd, str(proj)).intent == INTENT_SEARCH
        assert _symbols(bp) == ["foo"]
        assert _symbols(gp) == ["foo"], (
            "Grep(pattern='def foo') must recover the same symbol the bash "
            "classifier recovers (search_symbol_for_pattern, one home)")

    def test_regex_fragment_stays_silent_on_both(self, proj: Path) -> None:
        """Leave-alone: `pub(crate)` is a regex fragment — NO exact lookup on
        either surface (the survey's wrong-query class)."""
        bp = _bash_plan("grep 'pub(crate) fn' mod.py", proj)
        gp = _grep_plan("pub(crate) fn", proj, path=str(proj / "mod.py"))
        assert classify_bash("grep 'pub(crate) fn' mod.py", str(proj)).intent == INTENT_MECHANICAL
        assert _symbols(bp) == [] and _symbols(gp) == []
        assert bp.kg_query == gp.kg_query == ""


class TestPipeParity:
    def test_read_pipe_search_carries_both_keys(self, proj: Path) -> None:
        """`cat f | grep x` = READ f + SEARCH x: the target of the read leg
        AND the symbol of the search leg both survive the merge (strongest
        intent wins the label, keys merge)."""
        cmd = "cat app.py | grep validate_token"
        bi = classify_bash(cmd, str(proj))
        assert bi.intent == INTENT_SEARCH
        assert "app.py" in bi.targets
        assert "validate_token" in bi.symbols
        bp = _bash_plan(cmd, proj)
        gp = _grep_plan("validate_token", proj, path=str(proj / "app.py"))
        assert _symbols(bp) == _symbols(gp) == ["validate_token"]
        assert _resolved_sources(bp, proj) == _resolved_sources(gp, proj)


# --- EDIT family ------------------------------------------------------------------


class TestEditParity:
    def test_sed_in_place_matches_edit_tool(self, proj: Path) -> None:
        """R5 — RED on base: the bash EDIT KG query was the path topic only;
        the Edit tool folds the edited symbols into the same query."""
        cmd = "sed -i 's/x = 1/x = 2/' mod.py"
        bp = _bash_plan(cmd, proj)
        ep = _edit_plan(proj / "mod.py", proj, "    x = 1\n")
        assert classify_bash(cmd, str(proj)).intent == INTENT_EDIT
        assert bp.kg_profile == "bash_edit" and ep.kg_profile == "edit"
        _assert_same_keys(bp, ep, proj)

    def test_perl_grouped_in_place_matches_edit_tool(self, proj: Path) -> None:
        """R1 — RED on base: `perl -pi -e` was MECHANICAL (the regex only
        knew `perl -i`), so there were no keys at all."""
        cmd = "perl -pi -e 's/x = 1/x = 2/' mod.py"
        bi = classify_bash(cmd, str(proj))
        assert bi.intent == INTENT_EDIT, (
            "perl -pi -e is THE grouped in-place edit shape — as much an "
            "EDIT as sed -i")
        assert any("mod.py" in t for t in bi.targets)
        bp = _bash_plan(cmd, proj)
        ep = _edit_plan(proj / "mod.py", proj, "    x = 1\n")
        _assert_same_keys(bp, ep, proj)

    def test_perl_split_flags_in_place(self, proj: Path) -> None:
        bi = classify_bash("perl -i -pe 's/a/b/' mod.py", str(proj))
        assert bi.intent == INTENT_EDIT
        assert any("mod.py" in t for t in bi.targets)


# --- WRITE family -----------------------------------------------------------------


class TestWriteParity:
    def test_tee_matches_write_tool(self, proj: Path) -> None:
        """`tee f` (an OVERWRITE) keeps the Write tool's topic-only KG key —
        the in-place symbol fold (R5) must NOT leak into overwrite shapes."""
        cmd = "tee mod.py"
        bp = _bash_plan(cmd, proj)
        wp = _write_plan(proj / "mod.py", proj)
        assert classify_bash(cmd, str(proj)).intent == INTENT_EDIT
        assert bp.kg_profile == "bash_edit" and wp.kg_profile == "write"
        _assert_same_keys(bp, wp, proj)

    def test_heredoc_write_to_code_matches_write_tool(self, proj: Path) -> None:
        cmd = "cat > newcode.py <<'EOF'\nprint(1)\nEOF"
        bi = classify_bash(cmd, str(proj))
        assert bi.intent == INTENT_EDIT
        assert bi.write_snippet == ""  # withheld for code targets (secrecy rule)
        bp = _bash_plan(cmd, proj)
        wp = _write_plan(proj / "newcode.py", proj)
        _assert_same_keys(bp, wp, proj)

    def test_heredoc_write_to_knowledge_is_a_documented_divergence(
        self, proj: Path
    ) -> None:
        """PINNED divergence: a heredoc write to knowledge/docs queries the
        KG with the body SNIPPET (the content IS the topic — the
        prebash_query_parts rule); the Write surface keys on the path topic
        (§C3). Both are deliberate; the difference is pinned, not fixed."""
        (proj / "knowledge").mkdir()
        cmd = "cat > knowledge/node.md <<'EOF'\nThe router budget design\nEOF"
        bi = classify_bash(cmd, str(proj))
        assert bi.intent == INTENT_EDIT
        assert "The router budget design" in bi.write_snippet
        bp = _bash_plan(cmd, proj)
        wp = _write_plan(proj / "knowledge" / "node.md", proj)
        assert bp.kg_query == "The router budget design"
        assert wp.kg_query != bp.kg_query
        assert "node" in wp.kg_query  # the path topic

    def test_python_c_write_is_unrecoverable_by_design(self, proj: Path) -> None:
        """PINNED: `python -c "open('f','w')…"` is a write performed by an
        interpreter from its own source text — NOT recoverable from the
        command string without executing it (bash_write_targets.py, "What it
        provably CANNOT recognise"). MECHANICAL: no keys, and the Write tool
        equivalence is documented as an accepted miss."""
        cmd = "python -c \"open('w.py','w').write('x')\""
        bi = classify_bash(cmd, str(proj))
        assert bi.intent == INTENT_MECHANICAL
        bp = _bash_plan(cmd, proj)
        assert bp.kg_query == "" and bp.cg_symbols == []
        # …while the equivalent Write call DOES produce keys — the accepted,
        # documented cost of not parsing interpreter source.
        wp = _write_plan(proj / "w.py", proj)
        assert wp.kg_query != ""

    def test_cp_is_an_overwrite_edit_by_shipped_design(self, proj: Path) -> None:
        """PINNED (brief/source mismatch, reported): the lane brief annotated
        `cp a b` as MECHANICAL, but the shipped v0.2.101 intent vocabulary
        (GLM review nit-6) classifies cp/mv as an EDIT-shape write keyed on
        the DESTINATION — and the §C1 if-rule group contains no cp/tee rule,
        so the branch is exercised only through the pure functions. The
        shipped behaviour is pinned; changing it is a coordinator decision
        (it would also touch the settings templates, outside this lane)."""
        bi = classify_bash("cp mod.py copy.py", str(proj))
        assert bi.intent == INTENT_EDIT
        assert any("copy.py" in t for t in bi.targets)
        bp = _bash_plan("cp mod.py copy.py", proj)
        # overwrite shape → the Write tool's topic-only key
        assert "copy" in bp.kg_query and "foo" not in bp.kg_query


# --- chained cd -------------------------------------------------------------------


class TestCdChainParity:
    def test_cd_prefix_read_matches_read_tool(self, proj: Path) -> None:
        """R4 — RED on base: `cd sub && cat only.py` keyed on the bare
        `only.py` (resolved against the session cwd → the WRONG file: no
        parent-dir topic word, no CG symbols at all)."""
        cmd = "cd sub && cat only.py"
        bi = classify_bash(cmd, str(proj))
        assert bi.intent == INTENT_READ
        assert any(t.endswith(os.path.join("sub", "only.py")) or t == "sub/only.py"
                   for t in bi.targets), bi.targets
        bp = _bash_plan(cmd, proj)
        rp = _read_plan(proj / "sub" / "only.py", proj, _ONLY_BODY)
        _assert_same_keys(bp, rp, proj)

    def test_cd_absolute_prefix_rebases_target(self, proj: Path) -> None:
        bi = classify_bash(f"cd {proj} && cat app.py", "")
        assert bi.intent == INTENT_READ
        assert any(t.endswith("app.py") for t in bi.targets)

    def test_cd_search_target_rebased(self, proj: Path) -> None:
        bi = classify_bash("cd sub && grep -rn validate_token only.py", str(proj))
        assert bi.intent == INTENT_SEARCH
        assert any("only.py" in t for t in bi.targets)
        assert any(os.path.join("sub", "only.py") in t or t == "sub/only.py"
                   for t in bi.targets), bi.targets


# --- MECHANICAL leave-alone pins ---------------------------------------------------


class TestMechanicalPins:
    @pytest.mark.parametrize("cmd", ["ls", "ls -la", "pytest tests/x.py"])
    def test_mechanical_produces_no_keys(self, cmd: str, proj: Path) -> None:
        """MECHANICAL stays MECHANICAL (leave-alone rows): no query, no
        injection, no RL retrieval event — pytest is MECHANICAL BY DESIGN
        (plan §2.1: build/test = no spawn), pinned."""
        bi = classify_bash(cmd, str(proj))
        assert bi.intent == INTENT_MECHANICAL
        bp = _bash_plan(cmd, proj)
        assert bp.kg_query == "" and bp.kg_profile == ""
        assert bp.cg_symbols == [] and bp.cg_profile == ""

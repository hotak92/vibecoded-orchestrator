# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 injection redesign (PLAN-V02101 §C1/C2/C3/C4/C6) — settings
registrations for the Wave-2 surfaces.

Asserts, on BOTH ``templates/settings.json.{linux,windows}.template``:

* the Bash PreToolUse entry is the §C1 ``if``-filtered group (one handler
  per rule, ``timeout`` 10) — a non-matching MECHANICAL command spawns
  nothing (probe-verified 2026-10-05,
  ``reviews/V02101-INJECTION-KICKOFF-PROBES-2026-10-05.md``) — and its rule
  set is EXACTLY the classifier's READ/SEARCH verb tables
  (:class:`TestIfRulesMatchClassifier`, review S1);
* Read gets a NEW PostToolUse registration (``read-context-inject``, no
  ``if`` — §C2) with ``timeout`` 10, ABOVE the router's 6 s inner budget
  (``VCO_INJECT_BUDGET_S``; the kickoff probe's root cause was a 3 s settings timeout under a 4 s
  inner bound under a 4.7-11.6 s cold CLI);
* Grep gets a NEW PreToolUse registration (``grep-context-inject``, §C6 —
  no ``if``: a pattern shape is not a path glob);
* Agent|Task gets a NEW PreToolUse registration (``agent-brief-kg-inject`,
  §C4) with ``timeout`` 10 (the measured 12 s manual run vs the old 5 s
  SubagentStart timeout; the router still cuts the search at its 6 s
  inner budget and the query cache makes retries ~ms);
* Write gets a NEW PreToolUse registration beside Edit
  (``pre-write-context-inject``, §C3), ``if: "Write(*)"`` mirroring the
  Edit entry's ``if: "Edit(*)"``;
* linux == windows modulo the shell flavour of each ``command``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"
LINUX = TEMPLATES / "settings.json.linux.template"
WINDOWS = TEMPLATES / "settings.json.windows.template"

#: §C1 verbatim (probe 1 verified every rule class matches on the live
#: harness, pipelines and ``git  show`` double-space included). The tenth
#: rule, ``Bash(git log*)``, is the wave-2 GLM review's SF-3: the classifier
#: reads ``git log`` as READ, but without the rule that branch was
#: unreachable from the bash surface and long ``git log`` commands lost
#: their pre_bash outcome events. The last eleven are the v0.2.101 Opus
#: branch review's S1: every remaining verb of the classifier's READ/SEARCH
#: tables (less/more/diff/bat/nl/awk, ag/ack/egrep/fgrep, git blame) — the
#: set equality with the classifier is pinned by TestIfRulesMatchClassifier.
C1_IF_RULES = [
    "Bash(cat *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(sed *)",
    "Bash(grep *)",
    "Bash(rg *)",
    "Bash(git show*)",
    "Bash(git grep*)",
    "Bash(git diff*)",
    "Bash(git log*)",
    "Bash(less *)",
    "Bash(more *)",
    "Bash(diff *)",
    "Bash(bat *)",
    "Bash(nl *)",
    "Bash(awk *)",
    "Bash(ag *)",
    "Bash(ack *)",
    "Bash(egrep *)",
    "Bash(fgrep *)",
    "Bash(git blame*)",
]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _groups(doc: dict, event: str) -> list[dict]:
    return list(doc.get("hooks", {}).get(event, []))


def _hook_basenames(entry: dict) -> list[str]:
    """The hook FILE basenames a group registers, in order."""
    out = []
    for h in entry.get("hooks", []):
        m = re.search(r"([A-Za-z0-9_-]+\.(?:sh|ps1))", h.get("command", ""))
        out.append(m.group(1) if m else h.get("command", ""))
    return out


def _find_group(doc: dict, event: str, hook_stem: str) -> dict | None:
    """The first group on `event` registering a hook whose file stem matches."""
    for g in _groups(doc, event):
        for name in _hook_basenames(g):
            if re.sub(r"\.(sh|ps1)$", "", name) == hook_stem:
                return g
    return None


@pytest.mark.parametrize("tpl", [LINUX, WINDOWS], ids=["linux", "windows"])
class TestSettingsRegistrations:
    def test_templates_parse_as_json(self, tpl: Path) -> None:
        _load(tpl)  # red on the base tree: raises or assertions below fail

    # --- §C1: the Bash if-group -------------------------------------------

    def test_bash_pretooluse_is_the_c1_if_group(self, tpl: Path) -> None:
        doc = _load(tpl)
        groups = [g for g in _groups(doc, "PreToolUse")
                  if g.get("matcher") == "Bash"
                  and any("pre-bash-context-inject" in h for h in _hook_basenames(g))]
        assert len(groups) == 1, "exactly one Bash group hosts pre-bash-context-inject"
        hooks = groups[0]["hooks"]
        assert [h.get("if") for h in hooks] == C1_IF_RULES, (
            "the §C1 if-rules must be one handler per rule, in plan order")
        for h in hooks:
            # SF-1 (wave-2 GLM review): 10, not 8 — at review time the
            # router's inner budget default was ALSO 8 s, so an 8 s harness
            # timeout re-created the §9 root cause (cold run = startup +
            # budget > timeout → harness kill → silently lost injection +
            # lost state/RL-event writes). The budget is now 6 s
            # (VCO_INJECT_BUDGET_S) and every injection surface sits ABOVE
            # it with startup headroom: the ROUTER decides when to stop,
            # never the harness.
            assert h.get("timeout") == 10, (
                "the §C1 group must sit ABOVE the router's 6 s inner budget "
                "plus interpreter startup")
            assert h.get("type") == "command"
            assert "pre-bash-context-inject" in h["command"]
            # The zero-spawn prefilter is the point: an `if`-less handler
            # would spawn on every MECHANICAL command.
            assert h.get("if"), "every handler in the group carries an if rule"

    def test_no_ifless_bash_inject_handler_survives(self, tpl: Path) -> None:
        """The OLD single no-`if` pre-bash entry must be GONE — keeping it
        alongside the group would double-spawn every matching command."""
        doc = _load(tpl)
        for g in _groups(doc, "PreToolUse"):
            if g.get("matcher") != "Bash":
                continue
            for h in g["hooks"]:
                if "pre-bash-context-inject" in h.get("command", ""):
                    assert h.get("if") in C1_IF_RULES

    # --- §C2: Read on PostToolUse -----------------------------------------

    def test_read_posttooluse_registration(self, tpl: Path) -> None:
        doc = _load(tpl)
        g = _find_group(doc, "PostToolUse", "read-context-inject")
        assert g is not None, "Read PostToolUse group missing (§C2)"
        assert g.get("matcher") == "Read"
        (h,) = g["hooks"]
        assert h.get("timeout") == 10, (
            "Read must sit ABOVE the router's 6 s inner budget (kickoff probe: "
            "the 3 s timeout was the zero-injection root cause)")
        assert not h.get("if"), "§C2: no if — code-vs-docs is a content decision"

    # --- §C6: Grep on PreToolUse ------------------------------------------

    def test_grep_pretooluse_registration(self, tpl: Path) -> None:
        doc = _load(tpl)
        g = _find_group(doc, "PreToolUse", "grep-context-inject")
        assert g is not None, "Grep PreToolUse group missing (§C6)"
        assert g.get("matcher") == "Grep"
        (h,) = g["hooks"]
        assert h.get("timeout") == 10, "SF-1: above the router's 6 s inner budget"
        assert not h.get("if"), "§C6: no if — a pattern shape is not a path glob"

    # --- §C4: Agent|Task on PreToolUse ------------------------------------

    def test_agent_pretooluse_registration(self, tpl: Path) -> None:
        doc = _load(tpl)
        g = _find_group(doc, "PreToolUse", "agent-brief-kg-inject")
        assert g is not None, "Agent PreToolUse group missing (§C4)"
        assert g.get("matcher") == "Agent|Task"
        (h,) = g["hooks"]
        assert h.get("timeout") == 10, (
            "§C4: timeout 10 — above the router's 6 s inner budget")

    # --- §C3: Write beside Edit -------------------------------------------

    def test_write_pretooluse_registration(self, tpl: Path) -> None:
        doc = _load(tpl)
        g = _find_group(doc, "PreToolUse", "pre-write-context-inject")
        assert g is not None, "Write PreToolUse group missing (§C3)"
        assert g.get("matcher") == "Write"
        (h,) = g["hooks"]
        assert h.get("timeout") == 10, "SF-1: above the router's 6 s inner budget"
        assert h.get("if") == "Write(*)", (
            "the Write entry mirrors the Edit entry's if: Edit(*) shape")

    # --- SF-1 (wave-2 review): the full injection-timeout ladder ------------

    def test_edit_registration_raised_above_router_budget(self, tpl: Path) -> None:
        """SF-1 named Bash/Edit/Write/Grep; the Edit entry is asserted here
        (Bash/Write/Grep are pinned in their own rows above)."""
        doc = _load(tpl)
        g = _find_group(doc, "PreToolUse", "pre-edit-context-inject")
        assert g is not None
        (h,) = g["hooks"]
        assert h.get("timeout") == 10, (
            "an 8 s timeout leaves no startup headroom over the router's 6 s "
            "inner budget — cold runs get harness-killed and silently lose "
            "injections (§9 root cause)")

    def test_every_injection_surface_sits_above_the_router_budget(self, tpl: Path) -> None:
        """The ladder invariant, stated once for ALL injection surfaces: every
        registration that spawns the router must carry a timeout strictly
        greater than the router's VCO_INJECT_BUDGET_S default (6, read from
        the router source below) — the router bounds itself; the harness must
        never be the killer."""
        router = (REPO_ROOT / "claude_mcp_servers" / "scripts" /
                  "hook_context_router.py").read_text(encoding="utf-8")
        m = re.search(
            r'_env_float\("VCO_INJECT_BUDGET_S", ([0-9.]+)\)', router)
        assert m, "router budget default not found"
        budget = float(m.group(1))
        #: The router-spawning surfaces only — `diff-context-inject`
        #: (UserPromptSubmit, timeout 3) is a legacy hook that never runs the
        #: router and is deliberately out of scope.
        router_hooks = ("pre-bash-context-inject", "pre-edit-context-inject",
                        "pre-write-context-inject", "read-context-inject",
                        "grep-context-inject", "agent-brief-kg-inject")
        doc = _load(tpl)
        seen = 0
        for event, groups in doc.get("hooks", {}).items():
            for g in groups:
                for h in g.get("hooks", []):
                    cmd = h.get("command", "")
                    if any(name in cmd for name in router_hooks):
                        seen += 1
                        assert h.get("timeout", 0) > budget, (
                            f"{event}/{g.get('matcher')}: injection hook at "
                            f"timeout {h.get('timeout')} is not above the "
                            f"router's {budget} s inner budget (SF-1)")
        assert seen >= 10, "the sweep must cover the whole §C1 group"

    # --- SF-3 (wave-2 review): the git-log rule ------------------------------

    def test_git_log_rule_present(self, tpl: Path) -> None:
        """The classifier reads `git log` as READ intent; without this rule
        that branch was unreachable and long `git log` commands produced no
        bash_task state file / pre_bash outcome event (the RL corpus silently
        lost rows vs the pre-redesign 500-char regime)."""
        doc = _load(tpl)
        groups = [g for g in _groups(doc, "PreToolUse")
                  if g.get("matcher") == "Bash"
                  and any("pre-bash-context-inject" in n
                          for n in _hook_basenames(g))]
        assert groups, "the §C1 bash group is missing"
        rules = [h.get("if") for g in groups for h in g["hooks"]]
        assert "Bash(git log*)" in rules, (
            "SF-3: the git-log READ branch needs its zero-spawn prefilter rule")

    # --- §C5: the SubagentStart snapshot hook stays registered -------------

    def test_subagent_start_snapshot_still_registered(self, tpl: Path) -> None:
        """The V52-L.1 snapshot hook keeps its SubagentStart slot (the
        SubagentStop reconciler depends on it). The filename was KEPT
        (rename would strand six out-of-lane test references; the §C5
        fallback — fix the header, keep the name — is the shipped shape)."""
        doc = _load(tpl)
        names = []
        for g in _groups(doc, "SubagentStart"):
            names.extend(_hook_basenames(g))
        assert any("subagent-start-kg-inject" in n for n in names)


def _bash_group_rules(doc: dict) -> list[str]:
    """The `if` rules of the pre-bash-context-inject Bash group(s), in order."""
    return [h.get("if") for g in _groups(doc, "PreToolUse")
            if g.get("matcher") == "Bash"
            for h in g.get("hooks", [])
            if "pre-bash-context-inject" in h.get("command", "")]


def _classifier_rule_set() -> set[str]:
    """The rule set the classifier's verb tables imply — the ONE source.

    A plain verb ``v`` → ``Bash(v *)`` (the space is the word boundary:
    ``Bash(ag *)`` must not match ``agent``); a git subcommand ``s`` →
    ``Bash(git s*)`` (the §C1 git-rule family, probe-verified incl. double
    spaces between ``git`` and the subcommand)."""
    from vco_lib import inject_intent as ii
    plain = ii._READ_VERBS | ii._SEARCH_VERBS
    git = ii._GIT_READ_SUBS | ii._GIT_SEARCH_SUBS
    return {f"Bash({v} *)" for v in plain} | {f"Bash(git {s}*)" for s in git}


@pytest.mark.parametrize("tpl", [LINUX, WINDOWS], ids=["linux", "windows"])
class TestIfRulesMatchClassifier:
    """v0.2.101 Opus branch review S1 — two tables for one concern.

    The harness spawns pre-bash-context-inject ONLY for a command matching
    one of the group's `if` rules, so the rule list IS the reachable part of
    the classifier. A classifier verb without a rule is a dead branch on a
    fresh install (``git blame`` / ``ag`` classified READ/SEARCH but never
    spawned); a rule without a verb spawns a hook that can only answer
    MECHANICAL. JSON cannot import Python, so this is the cross-language
    rule (C) lock: data in ``vco_lib/inject_intent.py`` (with a must-match
    comment), the templates pinned to it in BOTH directions."""

    def test_rule_set_equals_classifier_verb_tables(self, tpl: Path) -> None:
        rules = _bash_group_rules(_load(tpl))
        assert len(rules) == len(set(rules)), f"duplicate if rules: {rules}"
        expected = _classifier_rule_set()
        missing = sorted(expected - set(rules))
        extra = sorted(set(rules) - expected)
        assert not missing, (
            f"classifier verbs with NO if rule (dead branch on fresh "
            f"installs): {missing}")
        assert not extra, (
            f"if rules with NO classifier verb (spawn that can only answer "
            f"MECHANICAL): {extra}")

    def test_every_rule_reaches_a_live_classifier_branch(self, tpl: Path) -> None:
        """Behavioural half: a representative command for each rule
        classifies READ or SEARCH — the rule names a branch that answers."""
        from vco_lib.inject_intent import classify_bash
        for rule in _bash_group_rules(_load(tpl)):
            m = re.fullmatch(r"Bash\((git \w+|\w+)\*?(?: \*)?\)", rule)
            assert m, f"unexpected rule shape {rule!r}"
            verb = m.group(1)
            sample = (f"{verb} build_parser" if verb in
                      ("grep", "rg", "ag", "ack", "egrep", "fgrep", "git grep")
                      else f"{verb} vco_lib/packs.py")
            intent = classify_bash(sample).intent
            assert intent in ("READ", "SEARCH"), (
                f"{rule!r}: sample {sample!r} classifies {intent!r}")


class TestLinuxWindowsParity:
    def test_hook_structure_identical_modulo_shell(self) -> None:
        """(event, matcher, hook-stem, timeout, if) tuples must match across
        the two templates; only the command's shell + suffix differ."""
        def _stem(name: str) -> str:
            return re.sub(r"\.(sh|ps1)$", "", name)

        def _sig(doc: dict) -> list[tuple]:
            sig = []
            for event, groups in doc.get("hooks", {}).items():
                for g in groups:
                    for h in g.get("hooks", []):
                        sig.append((event, g.get("matcher"),
                                    tuple(_stem(n) for n in _hook_basenames(g)),
                                    h.get("timeout"), h.get("if"),
                                    h.get("async", False)))
            return sig

        assert _sig(_load(LINUX)) == _sig(_load(WINDOWS)), (
            "linux and windows settings templates diverge beyond the shell "
            "flavour of each command")

    def test_linux_commands_use_bash_windows_use_powershell(self) -> None:
        for doc, needle, other in (
            (_load(LINUX), 'bash "', "powershell"),
            (_load(WINDOWS), "powershell", 'bash "'),
        ):
            for groups in doc.get("hooks", {}).values():
                for g in groups:
                    for h in g.get("hooks", []):
                        assert needle in h.get("command", "")
                        assert other not in h.get("command", "")

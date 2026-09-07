# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 W7 — the metrics path resolvers, and the tri-OS SHAPE of the answer.

`vco_lib.paths` grew three metrics resolvers when the home moved out of
`~/.claude`. This file pins:

* what each one returns, and that they compose (`vct_metrics_dir` ==
  `vct_root_dir()/metrics`, archive == `claude_user_dir()/metrics`);
* that `claude_metrics_dir` is now an ALIAS of the new home — the deprecation
  shim three out-of-lane callers ride across the move — and that its docstring
  says so, because a name that reads like `~/.claude` and does not resolve
  there is the kind of promise this cycle is eliminating;
* the READ order (new home first, archive second) that lets a mixed-state
  machine be read correctly;
* **the tri-OS shape**: Windows, macOS and Linux. The resolution is
  `Path`-based and separator-free, so all three are exercised as a SHAPE here
  rather than being claimed for one OS and assumed for the others (R14 —
  "only verifiable on <one OS>" is not an acceptable answer).

Isolation: `$VCT_CLAUDE_DIR`/`$VCT_STATE_DIR` are pinned into `tmp_path` (the
conftest guard covers the real directories as a second layer).
"""

from __future__ import annotations

import ntpath
import posixpath
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from vco_lib import paths as vco_paths
from vco_lib.paths import (
    claude_metrics_dir,
    claude_user_dir,
    legacy_claude_metrics_dir,
    metrics_read_dirs,
    vct_metrics_dir,
    vct_root_dir,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _pinned(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(tmp_path / "claude_home"))
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct_root"))
    return tmp_path


# --------------------------------------------------------------------------- #
# composition
# --------------------------------------------------------------------------- #


def test_new_home_is_the_state_root_plus_metrics(_pinned):
    assert vct_metrics_dir() == vct_root_dir() / "metrics"
    assert vct_metrics_dir() == _pinned / "vct_root" / "metrics"


def test_archive_is_the_claude_dir_plus_metrics(_pinned):
    assert legacy_claude_metrics_dir() == claude_user_dir() / "metrics"
    assert legacy_claude_metrics_dir() == _pinned / "claude_home" / "metrics"


def test_the_new_home_is_not_under_the_claude_dir(_pinned):
    """The whole directive, as one assertion."""
    new_home = vct_metrics_dir()
    claude = claude_user_dir()
    assert claude not in new_home.parents and new_home != claude


def test_read_order_is_new_home_then_archive(_pinned):
    assert metrics_read_dirs() == (vct_metrics_dir(), legacy_claude_metrics_dir())


def test_read_dirs_name_both_even_when_neither_exists(_pinned):
    """Probing is the caller's job; the resolver never hides a directory."""
    assert not any(d.exists() for d in metrics_read_dirs())
    assert len(metrics_read_dirs()) == 2


# --------------------------------------------------------------------------- #
# the deprecation alias
# --------------------------------------------------------------------------- #


def test_claude_metrics_dir_is_an_alias_of_the_new_home(_pinned):
    """The three out-of-lane callers (install.py, cli.verify,
    embedding_service) ride the move on this alias."""
    assert claude_metrics_dir() == vct_metrics_dir()


def test_the_alias_documents_that_it_no_longer_names_the_claude_dir():
    """A name that reads `~/.claude` and resolves elsewhere must SAY so.

    R16 category 1: a docstring is the next editor's input. This one is the
    single most misleadable name in the module, so the warning is asserted
    rather than trusted.
    """
    doc = vco_paths.claude_metrics_dir.__doc__ or ""
    assert "DEPRECATED" in doc
    assert "does NOT return anything under" in doc
    assert "legacy_claude_metrics_dir" in doc, (
        "the docstring must point a reader who wants the OLD directory at the "
        "resolver that still returns it"
    )


def test_the_archive_resolver_documents_that_it_is_read_only():
    doc = vco_paths.legacy_claude_metrics_dir.__doc__ or ""
    assert "read-only archive" in doc
    assert "deletes it" in doc


# --------------------------------------------------------------------------- #
# env overrides
# --------------------------------------------------------------------------- #


def test_state_dir_override_moves_only_the_new_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(tmp_path / "claude_home"))
    assert vct_metrics_dir() == tmp_path / "elsewhere" / "metrics"
    assert legacy_claude_metrics_dir() == tmp_path / "claude_home" / "metrics"


def test_claude_dir_override_moves_only_the_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct_root"))
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(tmp_path / "old_claude"))
    assert legacy_claude_metrics_dir() == tmp_path / "old_claude" / "metrics"
    assert vct_metrics_dir() == tmp_path / "vct_root" / "metrics"


def test_an_empty_override_falls_back_to_the_default(monkeypatch, tmp_path):
    """An explicitly-empty env value is unset, not a path of "" .

    Same coercion `vct_root_dir` / `claude_user_dir` already make; asserted
    here because an empty `$VCT_STATE_DIR` exported by a shell rc is a real
    shape and `Path("")` is `Path(".")` — a metrics directory in the CWD.
    """
    monkeypatch.setenv("VCT_STATE_DIR", "   ")
    monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(tmp_path))
    assert vct_metrics_dir() == Path.home() / ".vct" / "metrics"
    assert vct_metrics_dir() != Path("metrics")


# --------------------------------------------------------------------------- #
# tri-OS SHAPE (R12 / R14) — Windows, macOS AND Linux
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "os_name,home,flavour",
    [
        ("Linux", "/home/dev", posixpath),
        ("macOS", "/Users/dev", posixpath),
        ("Windows", r"C:\Users\dev", ntpath),
    ],
)
def test_the_path_decision_has_the_same_shape_on_all_three(
    os_name, home, flavour, monkeypatch
):
    """The resolution is `<home>/<root>/metrics` on every OS.

    Driven through the per-OS path FLAVOURS rather than through the live
    platform, because a real integration on the other two cannot run in this
    process. What is asserted is the decision's shape — no separator is
    hardcoded anywhere in the chain, so the same code produces a native path
    on each. The `.sh`/`.ps1` halves get the same treatment in
    `test_v0292_wp8_metrics_shell_parity.py`.
    """
    pure = PureWindowsPath if flavour is ntpath else PurePosixPath
    monkeypatch.setenv("VCT_STATE_DIR", flavour.join(home, ".vct"))
    monkeypatch.setenv("VCT_CLAUDE_DIR", flavour.join(home, ".claude"))

    new_home = pure(str(vct_metrics_dir()))
    archive = pure(str(legacy_claude_metrics_dir()))

    assert new_home.parts[-2:] == (".vct", "metrics"), (os_name, new_home)
    assert archive.parts[-2:] == (".claude", "metrics"), (os_name, archive)
    assert new_home.parts[-1] == archive.parts[-1] == "metrics"


def test_no_separator_is_hardcoded_in_the_resolvers():
    """The v0.2.81 Windows `\\`-separator mass-delete came from exactly this.

    The resolvers must build paths with `Path.__truediv__` (a join OPERATOR,
    which renders natively per OS) and never with a separator character inside
    a string literal. Asserted over each function's AST string constants, so
    the docstrings' example paths — which legitimately show `~/.vct/metrics`
    to a human — cannot make it pass or fail.
    """
    import ast
    import inspect
    import textwrap

    for fn in (vct_metrics_dir, legacy_claude_metrics_dir, claude_metrics_dir,
               metrics_read_dirs):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        fn_node = tree.body[0]
        body = list(getattr(fn_node, "body", []))
        # Drop the docstring NODE (the first statement when it is a bare
        # string) rather than its value: it legitimately shows example paths.
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        literals = [
            node.value
            for stmt in body
            for node in ast.walk(stmt)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        for literal in literals:
            assert "/" not in literal, (
                f"{fn.__name__} hardcodes a POSIX separator in {literal!r}"
            )
            assert "\\" not in literal, (
                f"{fn.__name__} hardcodes a Windows separator in {literal!r}"
            )


# --------------------------------------------------------------------------- #
# no parallel resolver (the duplication rule)
# --------------------------------------------------------------------------- #


def test_shipped_python_does_not_reconstruct_the_metrics_path_inline():
    """One home. A second copy is a path no env pin can steer.

    Scope is the shipped Python that has `vco_lib` available — the library and
    the MCP servers. `templates/scripts/cost-summary.py` is deliberately
    EXCLUDED and pinned separately (`test_v0292_wp8_cost_summary_reader.py`):
    it ships into every project's `.claude/scripts/` and must run on a bare
    stdlib interpreter, so it cannot import `vco_lib` and carries a documented
    class-C mirror with an enforcing parity test instead.
    """
    offenders: list[str] = []
    roots = [_REPO_ROOT / "vco_lib", _REPO_ROOT / "claude_mcp_servers"]
    needles = ('".claude", "metrics"', '".claude" / "metrics"',
               '".vct", "metrics"', '".vct" / "metrics"',
               '".claude/metrics"', '".vct/metrics"')
    for root in roots:
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            if ".venv" in py.parts or "site-packages" in py.parts:
                continue
            if py.name == "paths.py" and py.parent.name == "vco_lib":
                continue  # the one home
            text = py.read_text(encoding="utf-8", errors="replace")
            for needle in needles:
                if needle in text:
                    offenders.append(f"{py.relative_to(_REPO_ROOT)}: {needle}")
    assert not offenders, (
        "inline metrics-path reconstruction found — route it through "
        "vco_lib.paths.vct_metrics_dir() / legacy_claude_metrics_dir():\n  "
        + "\n  ".join(offenders)
    )


def test_no_shipped_hook_writes_metrics_under_the_claude_dir():
    """The directive, asserted against the shipped hook bodies.

    Every metrics hook resolves through `_lib/metrics-dir.{sh,ps1}`; none may
    reconstruct `~/.claude/metrics` itself. Comments may still MENTION the old
    path (they explain the move), so only non-comment lines are scanned.
    """
    hooks = _REPO_ROOT / "templates" / "hooks"
    offenders: list[str] = []
    for path in sorted(list(hooks.glob("*.sh")) + list(hooks.glob("*.ps1"))):
        for n, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for needle in (".claude/metrics", ".claude\\metrics",
                           '".claude" / "metrics"', '".claude", "metrics"'):
                if needle in stripped:
                    offenders.append(f"{path.name}:{n}: {stripped[:100]}")
    assert not offenders, (
        "a hook still targets ~/.claude/metrics; the metrics home moved in "
        "v0.2.92 W7 and the decision belongs to _lib/metrics-dir.{sh,ps1}:\n  "
        + "\n  ".join(offenders)
    )

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W4-D LOW 2 — quote-aware glued-separator peeling in the walker.

``vco_lib.bash_command_walk._split_glued`` used to run inside
``split_chain``, i.e. AFTER ``shlex.split`` (POSIX) had already stripped
the quotes — so a QUOTED argument ending in ``;`` / ``&`` was wrongly
peeled into a chain separator (``echo "done;"`` → segments
``[['echo', 'done']]``; the real shell would not split there).

The peel now runs at TOKENIZE time against a posix=False shadow pass that
keeps the quotes, so quoted tails stay one token while glued UNQUOTED
tails (``log;`` — the wait-loop case the peel was added for) still split.
"""

from __future__ import annotations

from typing import List

from vco_lib.bash_command_walk import (
    split_chain,
    tokenize,
    walk_command,
)


def _segments(command: str) -> List[List[str]]:
    toks = tokenize(command)
    assert toks is not None, f"{command!r} must tokenize cleanly"
    return split_chain(toks)


class TestQuotedSeparatorTailsStayOneToken:
    """The bug: a quoted ``;`` / ``&`` tail is DATA, not a separator."""

    def test_double_quoted_semicolon_tail(self) -> None:
        assert tokenize('echo "done;"') == ["echo", "done;"]

    def test_single_quoted_semicolon_tail(self) -> None:
        assert tokenize("echo 'done;' x") == ["echo", "done;", "x"]

    def test_single_quoted_ampersand_tail(self) -> None:
        assert tokenize("grep 'a&' f") == ["grep", "a&", "f"]

    def test_quoted_tail_not_split_into_chain_segment(self) -> None:
        # Pre-fix this yielded [['echo', 'done']] — the quoted argument
        # was chopped at a separator the shell would never see.
        assert _segments('echo "done;"') == [["echo", "done;"]]

    def test_quoted_tail_then_real_separator(self) -> None:
        # The real (unquoted) separator still splits; the quoted tail
        # inside the FIRST segment stays whole.
        assert _segments('echo "done;" ; sleep 5') == [
            ["echo", "done;"],
            ["sleep", "5"],
        ]

    def test_walk_command_hands_quoted_arg_intact_to_collector(self) -> None:
        seen: List[List[str]] = []

        def collect(segment: List[str]) -> List[str]:
            seen.append(list(segment))
            return []

        walk_command('echo "done;"', collect)
        assert seen == [["echo", "done;"]]


class TestGluedUnquotedTailsStillSplit:
    """The reason the peel exists (W4-D): unquoted glued separators."""

    def test_glued_semicolon_peeled(self) -> None:
        assert tokenize("do tail log; sleep 5; done") == [
            "do",
            "tail",
            "log",
            ";",
            "sleep",
            "5",
            ";",
            "done",
        ]

    def test_glued_semicolon_splits_chain(self) -> None:
        assert _segments("log; sleep 5") == [["log"], ["sleep", "5"]]

    def test_glued_double_amp_peeled_as_one_separator(self) -> None:
        assert tokenize("make all&& sleep 30") == [
            "make",
            "all",
            "&&",
            "sleep",
            "30",
        ]

    def test_wait_loop_sleep_still_visible(self) -> None:
        # The wait-guard's central case: the sleep inside the loop must
        # be its own segment, not swallowed by a glued `;`.
        segs = _segments("while true; do x; sleep 5; done")
        assert ["sleep", "5"] in segs


class TestRedirectionsUntouched:
    """``&`` inside fd-duplication redirects is not a chain separator."""

    def test_fd_dup_stdout_stderr_one_token(self) -> None:
        assert tokenize("echo hi 2>&1") == ["echo", "hi", "2>&1"]

    def test_fd_dup_to_stderr_one_token(self) -> None:
        assert tokenize("echo hi >&2") == ["echo", "hi", ">&2"]

    def test_fd_dup_not_split_into_chain(self) -> None:
        assert _segments("echo hi 2>&1") == [["echo", "hi", "2>&1"]]

    def test_quoted_redirect_target_with_semicolon(self) -> None:
        # Pre-fix this was split at the quoted `;` tail, chopping the
        # redirect target out of its segment.
        assert _segments('echo x > "done;"') == [["echo", "x", ">", "done;"]]


class TestMalformedAndPathologicalInput:
    def test_unbalanced_quote_still_soft_fails(self) -> None:
        assert tokenize('echo "unclosed') is None

    def test_misaligned_shadow_pass_falls_back_to_peel(self) -> None:
        # `$(...)` makes the posix=False shadow pass disagree with the
        # posix=True token count; the walker then falls back to the
        # peel-everything semantics (pre-fix behaviour) instead of
        # guessing. Pinned so the fallback keeps working.
        toks = tokenize('echo $(echo "x;") f')
        assert toks is not None
        # No token ends in a separator here, so the fallback peel is a
        # no-op either way — the pin is "no crash, no lost tokens".
        assert split_chain(toks) == [["echo", "$(echo", "x;)", "f"]]

    def test_empty_quoted_token(self) -> None:
        assert tokenize('echo "" x') == ["echo", "", "x"]

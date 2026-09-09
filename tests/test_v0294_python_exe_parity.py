# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The Python/Rust interpreter-ladder parity pin (v0.2.94).

``vco_lib/python_exe.py`` and
``launcher/src-tauri/vct-launcher-core/src/python_resolve.rs`` walk the SAME
ladder to answer the same question: "which interpreter can run our own code?".

That is a C-tier mirror under CLAUDE.md's A>B>C rule, and it is the one shape
that genuinely resists A and B: the Rust side has to FIND a Python interpreter
before it can call one, so it cannot delegate to the Python implementation, and
a config file it parsed would still need a parser on both sides for data this
small. What the rule then demands is that only the DATA differs by transport —
and that a test locks it. This is that test.

It extracts, FROM THE RUST SOURCE:
  * the env-var names (`$VCT_VENV`, `$VCT_INSTALL_ROOT`),
  * the venv directory layouts probed under an install root,
  * the interpreter file names probed inside each layout,
and asserts each against the Python constants, ORDER INCLUDED — order is
behaviour here (a machine with both `.venv` and the legacy
`claude_mcp_servers/.venv` must get the same answer from both sides).

Why it matters: the 2026-09-09 field defect was the launcher's bundle path NOT
using this ladder at all. Having fixed that, the two sides now decide the same
thing on every install/update path, and a silent divergence would put us back
where we started — with one half of the system spawning a python the other half
would have rejected.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import python_exe as px  # noqa: E402

_RUST = (
    REPO_ROOT
    / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "python_resolve.rs"
)


def _rust_source() -> str:
    return _RUST.read_text(encoding="utf-8")


def _join_chain(expr: str) -> str:
    """``root.join("a").join("b")`` → ``"a/b"`` (POSIX-normalised)."""
    return "/".join(re.findall(r'\.join\("([^"]+)"\)', expr))


def _block_after(src: str, header: str) -> str:
    """The bracketed list literal that follows ``header``."""
    start = src.index(header) + len(header)
    depth = 0
    for i, ch in enumerate(src[start:], start=start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unterminated list after {header!r}")


class RustSourceIsShapedAsExpected(unittest.TestCase):
    """A parity test that silently extracts nothing passes forever."""

    def test_the_rust_file_exists_and_declares_the_pin(self):
        self.assertTrue(_RUST.is_file(), f"{_RUST} is missing")
        src = _rust_source()
        self.assertIn(
            "MUST MATCH `vco_lib/python_exe.py`", src,
            "the Rust mirror must declare its Python counterpart by name — a "
            "mirror nobody knows is a mirror is how mirrors drift",
        )

    def test_the_python_module_declares_the_pin(self):
        body = (REPO_ROOT / "vco_lib" / "python_exe.py").read_text(encoding="utf-8")
        self.assertIn(
            "MUST MATCH launcher/src-tauri/vct-launcher-core/src/python_resolve.rs",
            body,
            "the Python side must name the Rust mirror it is pinned to",
        )


class LadderDataParity(unittest.TestCase):
    def test_env_var_names_match(self):
        src = _rust_source()
        self.assertIn(f'std::env::var("{px.VENV_ENV_VAR}")', src,
                      "the Rust ladder must read the same override env var")
        self.assertIn(f'std::env::var("{px.INSTALL_ROOT_ENV_VARS[0]}")', src,
                      "the Rust ladder must read the same install-root env var")

    def test_venv_layouts_match_in_order(self):
        block = _block_after(_rust_source(), "for layout in ")
        rust_layouts = [
            _join_chain(line) for line in block.splitlines()
            if ".join(" in line
        ]
        self.assertEqual(
            rust_layouts, list(px.VENV_LAYOUTS),
            "the venv layouts (and their ORDER — a clone with both must resolve "
            "identically on both sides) drifted between python_exe.py and "
            "python_resolve.rs",
        )

    def test_interpreter_names_match_in_order(self):
        src = _rust_source()
        # The ACTUAL probed list on both sides, compared directly — not a union
        # (R6/9). Both sides probe all three names on every OS: the Rust mirror
        # is one cross-OS binary, and a Python side that skipped `bin/python*`
        # on Windows would decide differently on an MSYS/Git-Bash venv while a
        # union comparison reported agreement.
        expected = list(px.VENV_INTERPRETER_NAMES)
        self.assertEqual(
            expected,
            list(px.POSIX_INTERPRETER_NAMES) + list(px.WINDOWS_INTERPRETER_NAMES),
            "VENV_INTERPRETER_NAMES must stay the concatenation of the two "
            "documented groups",
        )
        # There are TWO such lists in the Rust source (the `$VCT_VENV` rung and
        # the `venv_in` helper); extract every one and require them all to
        # agree with Python — including with each other, which is the
        # within-Rust half of the same drift risk.
        found = []
        idx = 0
        while True:
            try:
                idx = src.index("for candidate in ", idx)
            except ValueError:
                break
            block = _block_after(src[idx:], "for candidate in ")
            found.append([
                _join_chain(line) for line in block.splitlines() if ".join(" in line
            ])
            idx += len("for candidate in ")
        self.assertEqual(
            len(found), 2,
            "expected exactly 2 interpreter-candidate lists in python_resolve.rs "
            f"(the $VCT_VENV rung + venv_in); found {len(found)} — the extractor "
            "is stale, and a parity test that extracts nothing passes forever",
        )
        for names in found:
            self.assertEqual(
                names, expected,
                "the interpreter file names (and their order) drifted between "
                "python_exe.py and python_resolve.rs",
            )

    def test_the_probed_list_is_os_independent_on_both_sides(self):
        """R6/9: there is no per-OS branch left to diverge.

        Python's `interpreter_names()` takes no `os_name` and returns the same
        ordered list everywhere — matching the Rust mirror, which has no
        `cfg!(windows)` in its candidate lists either. The Windows layout is
        reachable from a POSIX runner (and vice versa) simply by being in the
        list, which is also what makes the two sides comparable at all.
        """
        self.assertEqual(px.interpreter_names(), px.VENV_INTERPRETER_NAMES)
        self.assertIn("bin/python", px.VENV_INTERPRETER_NAMES)
        self.assertIn("Scripts/python.exe", px.VENV_INTERPRETER_NAMES)
        src = _rust_source()
        for header in ("for candidate in ", "for layout in "):
            block = _block_after(src, header)
            self.assertNotIn(
                "cfg!(", block,
                "the Rust candidate lists must stay OS-independent, or the "
                "per-OS comparison above stops being the whole story",
            )

    def test_the_rust_side_still_ends_in_a_path_fallback(self):
        """Documented asymmetry, asserted so it stays deliberate.

        Rust's last rung is a bare `python3` — it MUST return something, because
        it runs before anything can be imported and its callers already treat a
        broken interpreter as a soft warning. Python's last rung is
        `sys.executable` GATED ON A PREFLIGHT, then a raised
        `PythonExeUnresolved`: by the time Python is running, "no interpreter
        can import vco_lib" is a broken install and saying so is strictly more
        useful than handing back a name that cannot work.
        """
        src = _rust_source()
        self.assertIn('"python3"', src, "the Rust PATH fallback tier is part of the ladder")
        self.assertTrue(
            issubclass(px.PythonExeUnresolved, Exception),
            "the Python side must FAIL LOUDLY rather than mirror the PATH tier",
        )


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""THE interpreter-ladder parity pin — Python / Rust / shell(sh) / shell(ps1).

``vco_lib/python_exe.py`` and
``launcher/src-tauri/vct-launcher-core/src/python_resolve.rs`` walk the SAME
ladder to answer the same question: "which interpreter can run our own code?".

That is a C-tier mirror under CLAUDE.md's A>B>C rule, and it is the one shape
that genuinely resists A and B: the Rust side has to FIND a Python interpreter
before it can call one, so it cannot delegate to the Python implementation, and
a config file it parsed would still need a parser on both sides for data this
small. What the rule then demands is that only the DATA differs by transport —
and that a test locks it. This is that test.

v0.2.94 review item 2b: there are FOUR ladders, not two. The shipped shell
wrappers resolve the same interpreter through
``templates/scripts/vct_venv_ladder.sh`` and its ``.ps1`` sibling, and those two
had drifted from the other two in every dimension this file pins — a
`Scripts/python3.exe` name no other side probed, a `Scripts/python.exe`-first
order on the PowerShell side, no `bin/python3` there at all, and no
`$VCT_ORCHESTRATOR_ROOT` tier in either shell or Rust. One test, four sources,
so a divergence has nowhere to hide.

It extracts, FROM EACH SOURCE:
  * the env-var names (`$VCT_VENV`, `$VCT_INSTALL_ROOT`, `$VCT_ORCHESTRATOR_ROOT`),
  * the venv directory layouts probed under an install root,
  * the interpreter file names probed inside each layout,
and asserts each against the Python constants, ORDER INCLUDED — order is
behaviour here (a machine with both `.venv` and the legacy
`claude_mcp_servers/.venv` must get the same answer from every side).

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
_LADDER_SH = REPO_ROOT / "templates" / "scripts" / "vct_venv_ladder.sh"
_LADDER_PS1 = REPO_ROOT / "templates" / "scripts" / "vct_venv_ladder.ps1"


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
        # v0.2.94: the install-root vars are read through ONE loop over both
        # names (they were a single hardcoded `std::env::var("VCT_INSTALL_ROOT")`
        # before `VCT_ORCHESTRATOR_ROOT` joined), so the literals are asserted
        # rather than the call shape.
        for var in px.INSTALL_ROOT_ENV_VARS:
            self.assertIn(f'"{var}"', src,
                          f"the Rust ladder must read ${var}")

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

    def test_no_ladder_ends_in_a_bare_interpreter_name(self):
        """v0.2.94 review item 2c: the asymmetry is GONE, and that is the pin.

        Rust's last rung used to be a bare `python3`. It made every caller's
        `ok_or_else(...)` / `is_none()` guard unreachable while handing back a
        name that on a PEP-668 machine cannot `import weaviate` — the
        2026-09-09 field shape one layer up. All four ladders now END, and the
        caller decides: Python raises `PythonExeUnresolved`, Rust returns
        `None`, both shells refuse with a message naming every probed tier.

        Mutation check: restore `Some(PathBuf::from("python3"))` as the Rust
        tail and this fails.
        """
        src = _rust_source()
        tail = src[src.index("pub fn resolve_python_for_vco_lib()"):]
        body = tail[: tail.index("\n}\n")]
        self.assertNotIn(
            'PathBuf::from("python3")', body,
            "the Rust ladder must not invent a bare PATH interpreter",
        )
        self.assertNotIn(
            'PathBuf::from("python.exe")', body,
            "the Rust ladder must not invent a bare PATH interpreter",
        )
        self.assertTrue(
            body.rstrip().endswith("None"),
            "the Rust ladder must END with None when no tier qualifies; its "
            f"last line is {body.rstrip().splitlines()[-1]!r}",
        )
        self.assertTrue(
            issubclass(px.PythonExeUnresolved, Exception),
            "the Python side must FAIL LOUDLY rather than hand back a name",
        )
        # The shells: no BARE `python` / `python3` token anywhere. A path
        # ending in one (`.venv/bin/python3`) is a resolution; a quoted bare
        # name is a PATH fallback, which is what both wrappers used to do
        # before v0.2.94 (`exec python "$SCRIPT_DIR/..."`).
        bare = re.compile(r"""(?<![/\\])['"]python3?(\.exe)?['"]""")
        for path, label in ((_LADDER_SH, "sh"), (_LADDER_PS1, "ps1")):
            body = path.read_text(encoding="utf-8-sig")
            hits = bare.findall(body)
            self.assertFalse(
                hits,
                f"the {label} ladder names a bare interpreter ({hits}) — every "
                f"ladder must END rather than fall back to $PATH",
            )


class FourWayLadderParity(unittest.TestCase):
    """The SAME data, extracted from all four ladders (review item 2b).

    Extraction is deliberately shape-specific per source: a regex loose enough
    to match any of them would match nothing meaningful in all of them, and a
    parity test that extracts nothing passes forever. Every extractor therefore
    asserts it found something before comparing.
    """

    def _sh(self) -> str:
        return _LADDER_SH.read_text(encoding="utf-8")

    def _ps1(self) -> str:
        return _LADDER_PS1.read_text(encoding="utf-8-sig")

    def test_the_shell_ladders_exist_and_declare_the_pin(self):
        for path in (_LADDER_SH, _LADDER_PS1):
            self.assertTrue(path.is_file(), f"{path} is missing")
        for body, label in ((self._sh(), "sh"), (self._ps1(), "ps1")):
            self.assertIn(
                "vco_lib/python_exe.py", body,
                f"the {label} ladder must name the ladder it is pinned to",
            )

    def test_env_var_names_match_across_all_four(self):
        names = (px.VENV_ENV_VAR,) + tuple(px.INSTALL_ROOT_ENV_VARS)
        self.assertEqual(
            names, ("VCT_VENV", "VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT"),
            "the pinned env-var set changed — update every ladder, not just this",
        )
        rust = _rust_source()
        sh, ps1 = self._sh(), self._ps1()
        for var in names:
            self.assertIn(f'"{var}"', rust, f"Rust ladder ignores ${var}")
            self.assertIn(var, sh, f"sh ladder ignores ${var}")
            self.assertIn(var, ps1, f"ps1 ladder ignores ${var}")

    def test_interpreter_names_and_order_match_across_all_four(self):
        expected = list(px.VENV_INTERPRETER_NAMES)

        # bash: the ordered `if [ -x "$c/<name>" ]` chain in
        # `_vct_ladder_interp_for_candidate`.
        sh = self._sh()
        fn = sh[sh.index("_vct_ladder_interp_for_candidate()"):]
        fn = fn[: fn.index("\n}\n")]
        sh_names = re.findall(r'\[ -x "\$c/([^"]+)" \]', fn)
        self.assertTrue(sh_names, "extracted no interpreter names from the sh ladder")
        self.assertEqual(
            sh_names, expected,
            "the sh ladder's interpreter names (or their order) drifted from "
            "python_exe.py",
        )

        # PowerShell: `Get-VctLadderVenvPythonCandidates` is layout-major, so
        # the names repeat per layout — compare the per-layout slice.
        ps1 = self._ps1()
        block = ps1[ps1.index("function Get-VctLadderVenvPythonCandidates"):]
        block = block[: block.index("\n}\n")]
        joined = re.findall(r'Join-Path \$Root "([^"]+)"', block)
        self.assertTrue(joined, "extracted no candidates from the ps1 ladder")
        ps1_names = [
            p.replace("\\", "/").split("/", 1)[1]
            for p in joined
            if p.replace("\\", "/").startswith(".venv/")
        ]
        self.assertEqual(
            ps1_names, expected,
            "the ps1 ladder's interpreter names (or their order) drifted from "
            "python_exe.py",
        )

    def test_venv_layouts_and_order_match_across_all_four(self):
        expected = list(px.VENV_LAYOUTS)

        sh = self._sh()
        fn = sh[sh.index("vct_venv_ladder_resolve()"):]
        fn = fn[: fn.index("\n}\n")]
        sh_layouts = re.findall(r'"\$\{VCT_INSTALL_ROOT:-\}/([^"]+)"', fn)
        self.assertTrue(sh_layouts, "extracted no layouts from the sh ladder")
        self.assertEqual(
            sh_layouts, expected,
            "the sh ladder's venv layouts (or their order) drifted",
        )

        ps1 = self._ps1()
        block = ps1[ps1.index("function Get-VctLadderVenvPythonCandidates"):]
        block = block[: block.index("\n}\n")]
        joined = [
            p.replace("\\", "/")
            for p in re.findall(r'Join-Path \$Root "([^"]+)"', block)
        ]
        self.assertTrue(joined, "extracted no candidates from the ps1 ladder")
        seen: list = []
        for cand in joined:
            layout = cand.rsplit("/", 2)[0] if cand.count("/") > 1 else cand
            # `<layout>/<name-with-one-slash>` → strip the trailing 2 segments.
            layout = "/".join(cand.split("/")[:-2])
            if layout not in seen:
                seen.append(layout)
        self.assertEqual(
            seen, expected,
            "the ps1 ladder's venv layouts (or their order) drifted",
        )


if __name__ == "__main__":
    unittest.main()

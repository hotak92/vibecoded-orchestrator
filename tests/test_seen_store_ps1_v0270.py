# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 FIX-1/FIX-6 — PowerShell-side seen-store coverage.

Two concerns, neither of which depends on bash (so they live OUTSIDE the
bash-gated ``test_seen_store_v0270.py`` module):

  FIX-1 (functional, gated on pwsh/powershell): drive the REAL
  ``Invoke-VcoFilterSeenBlocks`` through PowerShell and assert the scope bug
  is fixed — injected KG/CODE blocks are emitted WHOLE (headers intact, not
  shredded), a second pass dedups (returns empty), and the inject-key file
  records the per-chunk KG key + per-entity CODE key matching what the ``.sh``
  produces. Skips cleanly when no PowerShell is on the runner.

  FIX-6 (ASCII invariant, always runs): every ``templates/hooks/_lib/*.ps1``
  whose header DECLARES the "Plain ASCII only" invariant must actually be pure
  ASCII (no byte > 0x7F, no UTF-8 BOM). The hook-os-parity CI gate EXCLUDES
  ``_lib/``, and these files have a recurring em-dash/BOM regression history,
  so this is the guard that fails CI on a future non-ASCII edit.

  Scope note: the OTHER ``_lib/*.ps1`` files (credscan, emit-context, etc.)
  deliberately use BOM-prefixed UTF-8 with em-dashes — that is the established
  convention for THOSE files (a new .ps1 with non-ASCII content needs a UTF-8
  BOM). The v0.2.70 trio (seen-store, codegraph-query, command-noise-strip)
  chose the cleaner ASCII-only invariant and declare it in their header; this
  test enforces it on exactly the files that make that promise, so it does NOT
  retroactively force the BOM-using files to ASCII.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "templates" / "hooks" / "_lib"
SEEN_PS1 = LIB_DIR / "seen-store.ps1"


# --------------------------------------------------------------------------
# FIX-6: ASCII invariant for every _lib/*.ps1 (always runs; no shell needed)
# --------------------------------------------------------------------------
def _declares_ascii_only(data: bytes) -> bool:
    """True iff the file's header declares the 'Plain ASCII only' invariant.
    Decoded latin-1 so a (BOM-carrying, non-ASCII) sibling never raises here —
    those files simply don't carry the phrase and are excluded by design."""
    head = data[:2000].decode("latin-1", errors="replace").lower()
    return "ascii only" in head


def test_ascii_declaring_lib_ps1_are_pure_ascii() -> None:
    """Every ``templates/hooks/_lib/*.ps1`` whose header DECLARES 'Plain ASCII
    only' must actually be pure ASCII (no byte > 0x7F) and carry no UTF-8 BOM.
    _lib/ is excluded from the hook-os-parity gate and the v0.2.70 ASCII-only
    trio (seen-store, codegraph-query, command-noise-strip) has a recurring
    em-dash/BOM regression risk — this is the enforcer. The BOM-using siblings
    (credscan, emit-context, ...) deliberately keep their convention and are
    excluded because they make no ASCII promise.
    """
    ps1_files = sorted(LIB_DIR.glob("*.ps1"))
    assert ps1_files, f"no _lib/*.ps1 found under {LIB_DIR}"
    checked: list[str] = []
    offenders: dict[str, list[tuple[int, int]]] = {}
    bom_offenders: list[str] = []
    for f in ps1_files:
        data = f.read_bytes()
        if not _declares_ascii_only(data):
            continue
        checked.append(f.name)
        if data[:3] == b"\xef\xbb\xbf":
            bom_offenders.append(f.name)
        bad = [(i, b) for i, b in enumerate(data) if b > 0x7F]
        if bad:
            offenders[f.name] = bad[:5]
    # The three v0.2.70 files declare the invariant — guard against a future
    # rename/removal silently shrinking coverage to nothing.
    for expected in ("seen-store.ps1", "codegraph-query.ps1",
                     "command-noise-strip.ps1"):
        assert expected in checked, (
            f"{expected} must declare 'Plain ASCII only' in its header so this "
            f"guard covers it; declaring files = {checked}"
        )
    assert not offenders, (
        f"non-ASCII bytes in an ASCII-declaring _lib/*.ps1 "
        f"(file -> [(offset, byte), ...]): {offenders}"
    )
    assert not bom_offenders, (
        f"ASCII-declaring _lib/*.ps1 must NOT carry a UTF-8 BOM: {bom_offenders}"
    )


# --------------------------------------------------------------------------
# FIX-1: functional PowerShell test (gated on pwsh/powershell availability)
# --------------------------------------------------------------------------
def _pwsh() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _sh_keys_for(input_text: str, inject: Path, tmp_path: Path) -> str | None:
    """Run the SAME input through the bash sibling's vco_filter_seen_blocks and
    return its inject-key file content, for cross-OS key-parity comparison.
    Returns None when bash isn't available (then the pwsh test asserts only the
    key SHAPE, not exact .sh equality)."""
    if shutil.which("bash") is None:
        return None
    seen_sh = LIB_DIR / "seen-store.sh"
    py = shutil.which("python3") or "python3"
    script = (
        f'export PY="{py}"\n'
        f'. "{seen_sh}"\n'
        f"IN=$(cat <<'VCO_EOF'\n{input_text}VCO_EOF\n)\n"
        f'vco_filter_seen_blocks "$IN" "{inject}" "" >/dev/null\n'
        f'cat "{inject}" 2>/dev/null || true\n'
    )
    r = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        timeout=30, cwd=str(tmp_path),
    )
    if r.returncode != 0:
        return None
    return r.stdout


@pytest.mark.skipif(_pwsh() is None, reason="pwsh/powershell required")
def test_filter_seen_blocks_preserves_then_dedups(tmp_path: Path) -> None:
    """FIX-1: scope bug fixed. Feed a 2-block stream (one KG with continuation
    lines + one CODE) through the REAL Invoke-VcoFilterSeenBlocks:
      (1) first pass OUTPUT preserves BOTH full blocks WITH headers (not
          shredded to orphaned body fragments);
      (2) second pass with the same inject store returns EMPTY (dedup fires);
      (3) the inject-key file records 'Title#<sha1[:12]>' (KG) + 'mod.func'
          (CODE) — matching the keys the .sh produces for the same input.
    """
    pwsh = _pwsh()
    assert pwsh is not None  # narrow for type-checkers (skipif guards runtime)

    inject = tmp_path / "seen_inject_s.txt"
    # KG block with TWO continuation/body lines + a CODE block. The KG body is
    # everything after the header line (joined with newlines, each line + "\n").
    kg_body_lines = ["line one of body", "line two of body"]
    code_body_lines = ["  code body line"]
    # Build the input exactly as the producers emit it (header + body lines,
    # blank line terminates each block).
    input_text = (
        "KG: My Title | concept | score=0.80 | FULL NODE:\n"
        f"{kg_body_lines[0]}\n"
        f"{kg_body_lines[1]}\n"
        "\n"
        "CODE: mod.func | CodeFunction | distance=0.1 | sig\n"
        f"{code_body_lines[0]}\n"
        "\n"
    )

    # The KG per-chunk key body = the body lines the parser accumulated. The
    # parser appends each body line as "<line>\n", INCLUDING the terminating
    # blank line that belongs to the block boundary. To stay robust against the
    # exact body-accumulation shape, assert on the stable parts: the title and
    # the CODE full_name, plus that SOME 'My Title#<hex12>' key was written.
    ps_script = f"""
$ErrorActionPreference = 'Stop'
. '{SEEN_PS1.as_posix()}'
$inj = '{inject.as_posix()}'
$in = @'
{input_text}'@
$out1 = Invoke-VcoFilterSeenBlocks -InputText $in -InjectFile $inj
$out2 = Invoke-VcoFilterSeenBlocks -InputText $in -InjectFile $inj
Write-Output '=====PASS1====='
Write-Output $out1
Write-Output '=====PASS2====='
Write-Output $out2
Write-Output '=====KEYS====='
if (Test-Path -LiteralPath $inj) {{ Get-Content -LiteralPath $inj }}
Write-Output '=====END====='
"""
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", ps_script],
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, (
        f"pwsh exited {proc.returncode}\nSTDOUT:\n{proc.stdout}\n"
        f"STDERR:\n{proc.stderr}"
    )
    out = proc.stdout
    # Split the sections.
    def _section(name: str) -> str:
        start = out.index(f"====={name}=====") + len(f"====={name}=====")
        # find the next marker after start
        rest = out[start:]
        nxt = rest.find("=====")
        return (rest[:nxt] if nxt >= 0 else rest)

    pass1 = _section("PASS1")
    pass2 = _section("PASS2")
    keys = _section("KEYS")

    # (1) First pass preserves BOTH full blocks WITH headers (not shredded).
    assert "KG: My Title | concept | score=0.80 | FULL NODE:" in pass1, (
        f"KG header missing/shredded from first-pass output:\n{pass1!r}"
    )
    assert "CODE: mod.func | CodeFunction | distance=0.1 | sig" in pass1, (
        f"CODE header missing/shredded from first-pass output:\n{pass1!r}"
    )
    assert kg_body_lines[0] in pass1 and kg_body_lines[1] in pass1, (
        f"KG body lines missing from first-pass output:\n{pass1!r}"
    )

    # (2) Second pass dedups → empty (only whitespace, if anything).
    assert pass2.strip() == "", (
        f"second pass must dedup to empty; got:\n{pass2!r}"
    )

    # (3) Inject keys: a 'My Title#<hex12>' KG key + the 'mod.func' CODE key.
    assert "mod.func" in keys, f"CODE per-entity key missing:\n{keys!r}"
    kg_key_match = re.search(r"My Title#([0-9a-f]{12})", keys)
    assert kg_key_match, f"KG per-chunk key 'My Title#<hex12>' missing:\n{keys!r}"

    # (3b) Cross-OS PARITY: when bash is also present, the SAME input through the
    # .sh sibling must produce the SAME key set (this is the "matching what the
    # .sh produces" requirement made exact, not just shape-checked).
    sh_inject = tmp_path / "seen_inject_sh.txt"
    sh_keys = _sh_keys_for(input_text, sh_inject, tmp_path)
    if sh_keys is not None:
        ps_set = {k for k in keys.splitlines() if k.strip()}
        sh_set = {k for k in sh_keys.splitlines() if k.strip()}
        assert ps_set == sh_set, (
            "cross-OS key mismatch between seen-store.ps1 and seen-store.sh\n"
            f"  ps1 keys: {sorted(ps_set)}\n  .sh keys: {sorted(sh_set)}"
        )

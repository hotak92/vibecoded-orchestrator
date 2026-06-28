# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 Stream E — unified per-session read/inject dedup (seen-store).

These are MERGE-GATING tests: E cannot ship half-done. They drive the real
`templates/hooks/_lib/seen-store.sh` functions through bash and assert:
  - empty / "default" session id -> inject blind (NO shared-bucket write)
  - per-chunk KG key (a NEW chunk of a seen node STILL injects)
  - reads-ledger suppression (a CODE/KG block whose src was Read is suppressed)
  - double-source guard (sourcing twice is a no-op)
  - the `.ps1` sibling exists (the hook-os-parity CI gate EXCLUDES _lib/, so this
    must be asserted explicitly)
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "templates" / "hooks" / "_lib"
SEEN_SH = LIB_DIR / "seen-store.sh"
SEEN_PS1 = LIB_DIR / "seen-store.ps1"
CG_SH = LIB_DIR / "codegraph-query.sh"
CG_PS1 = LIB_DIR / "codegraph-query.ps1"


def _has_bash() -> bool:
    return shutil.which("bash") is not None


pytestmark = pytest.mark.skipif(not _has_bash(), reason="bash required")


def _run(snippet: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run a bash snippet with seen-store.sh sourced and $PY set."""
    py = shutil.which("python3") or "python3"
    script = (
        f'export PY="{py}"\n'
        f'. "{SEEN_SH}"\n'
        f'{snippet}\n'
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30, cwd=str(tmp_path)
    )


# --------------------------------------------------------------------------
# .ps1 sibling existence (the gate CI misses for _lib/)
# --------------------------------------------------------------------------
def test_seen_store_ps1_sibling_exists() -> None:
    assert SEEN_SH.exists(), "seen-store.sh missing"
    assert SEEN_PS1.exists(), (
        "seen-store.ps1 sibling MISSING — check_hook_parity.py EXCLUDES _lib/, "
        "so this must be hand-verified here."
    )


def test_codegraph_query_ps1_sibling_exists() -> None:
    assert CG_SH.exists(), "codegraph-query.sh missing"
    assert CG_PS1.exists(), (
        "codegraph-query.ps1 sibling MISSING — _lib/ is excluded from the "
        "parity gate; this assertion is the guard."
    )


def test_lib_helpers_have_must_match_comments() -> None:
    """The 5 cross-language must-match pairs are documented in the .sh files."""
    seen = SEEN_SH.read_text(encoding="utf-8")
    cg = CG_SH.read_text(encoding="utf-8")
    assert "MUST MATCH" in seen and "seen-store.ps1" in seen
    assert "MUST MATCH" in cg and "codegraph-query.ps1" in cg


# --------------------------------------------------------------------------
# inject-blind on empty / "default" session id (cross-session-bleed guard)
# --------------------------------------------------------------------------
def test_empty_session_id_resolves_empty_store_path(tmp_path: Path) -> None:
    r = _run('vco_seen_store_path inject "" /proj', tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip() == "", (
        f"empty session id must resolve to an EMPTY store path; got {r.stdout!r}"
    )


def test_default_session_id_resolves_empty_store_path(tmp_path: Path) -> None:
    r = _run('vco_seen_store_path inject "default" /proj', tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip() == "", (
        f'"default" session id must resolve to an EMPTY store path (no shared '
        f"bucket); got {r.stdout!r}"
    )


def test_good_session_id_resolves_real_paths(tmp_path: Path) -> None:
    r = _run(
        'vco_seen_store_path inject "abc123" /proj\n'
        'echo "---"\n'
        'vco_seen_store_path reads "abc123" /proj',
        tmp_path,
    )
    out = r.stdout.split("---")
    assert "/proj/.claude/state/seen_inject_abc123.txt" in out[0]
    # SF-1: the INJECTOR reads store is seen_reads_<sid>.txt — DISTINCT from
    # pre-tool-use's Build-Anchor reads_<sid>.txt (which holds the as-Read shape).
    assert "/proj/.claude/state/seen_reads_abc123.txt" in out[1]


def test_inject_blind_never_writes_default_bucket(tmp_path: Path) -> None:
    """The load-bearing negative: with an EMPTY inject file, dedup is disabled
    and NOTHING is written to any 'default' store. Re-injecting the same block
    twice still emits it both times (no suppression)."""
    snippet = (
        'IN=$\'KG: NodeX | concept | score=0.9 | FULL NODE:\\nbody\\n\\n\'\n'
        # Empty inject file => inject blind.
        'OUT1="$(vco_filter_seen_blocks "$IN" "" "")"\n'
        'OUT2="$(vco_filter_seen_blocks "$IN" "" "")"\n'
        '[ -n "$OUT1" ] && echo "first-nonempty"\n'
        '[ -n "$OUT2" ] && echo "second-nonempty"\n'
        # Assert NO seen_inject_default.txt was created anywhere under cwd.
        'if ls .claude/state/seen_inject_default.txt >/dev/null 2>&1; then echo "BLED"; else echo "no-bleed"; fi\n'
    )
    r = _run(snippet, tmp_path)
    assert "first-nonempty" in r.stdout
    assert "second-nonempty" in r.stdout, "inject-blind must NOT dedup"
    assert "no-bleed" in r.stdout
    assert "BLED" not in r.stdout


# --------------------------------------------------------------------------
# per-chunk KG key: a NEW chunk of a seen node still injects
# --------------------------------------------------------------------------
def test_per_chunk_kg_key(tmp_path: Path) -> None:
    inj = tmp_path / "seen_inject_s.txt"
    snippet = (
        f'INJ="{inj}"\n'
        'IN1=$\'KG: NodeA | concept | score=0.8 | FULL NODE:\\nchunk-one body\\n\\n\'\n'
        'IN2=$\'KG: NodeA | concept | score=0.8 | FULL NODE:\\nchunk-TWO different\\n\\n\'\n'
        'O1="$(vco_filter_seen_blocks "$IN1" "$INJ" "")"\n'
        '[ -n "$O1" ] && echo "chunk1-injected"\n'
        # same chunk again -> suppressed
        'O1b="$(vco_filter_seen_blocks "$IN1" "$INJ" "")"\n'
        '[ -z "$O1b" ] && echo "chunk1-resuppressed"\n'
        # different chunk of SAME node -> still injects
        'O2="$(vco_filter_seen_blocks "$IN2" "$INJ" "")"\n'
        '[ -n "$O2" ] && echo "chunk2-injected"\n'
    )
    r = _run(snippet, tmp_path)
    assert "chunk1-injected" in r.stdout
    assert "chunk1-resuppressed" in r.stdout, "same chunk must dedup"
    assert "chunk2-injected" in r.stdout, (
        "a NEW chunk of a seen node MUST still inject (per-chunk key)"
    )


# --------------------------------------------------------------------------
# reads-ledger suppression — SF-1: uses the REAL repo-relative producer shape
# --------------------------------------------------------------------------
def test_reads_ledger_suppresses_already_read_source(tmp_path: Path) -> None:
    """SF-1 regression: the producers emit a REPO-RELATIVE `| src=<path>`
    trailer (KG entry.file_path = 'knowledge/...'; CODE file_path = repo-relative
    POSIX) and the reads ledger now stores repo-relative paths too. This test
    uses those REAL shapes (relative on BOTH sides) — the earlier version used
    matched ABSOLUTE paths, which masked the abs-vs-relative mismatch that made
    the feature dead on arrival."""
    inj = tmp_path / "seen_inject_s.txt"
    rds = tmp_path / "reads_s.txt"
    # Reads ledger holds REPO-RELATIVE entries (what pre-tool-use now writes).
    rds.write_text("knowledge/concepts/foo.md\nsrc/bar.py\n", encoding="utf-8")
    snippet = (
        f'INJ="{inj}"\n'
        f'RDS="{rds}"\n'
        # KG block whose RELATIVE src is in the ledger -> suppressed.
        'INK=$\'KG: Foo Node | concept | score=0.8 | FULL NODE: | src=knowledge/concepts/foo.md\\nbody\\n\\n\'\n'
        'OUTK="$(vco_filter_seen_blocks "$INK" "$INJ" "$RDS")"\n'
        '[ -z "$OUTK" ] && echo "kg-read-src-suppressed"\n'
        # CODE block whose RELATIVE src is in the ledger -> suppressed.
        'INC=$\'CODE: bar.func | CodeFunction | distance=0.3 | src=src/bar.py\\n  body\\n\\n\'\n'
        'OUTC="$(vco_filter_seen_blocks "$INC" "$INJ" "$RDS")"\n'
        '[ -z "$OUTC" ] && echo "code-read-src-suppressed"\n'
        # CODE block whose RELATIVE src is NOT in the ledger -> injects.
        'INC2=$\'CODE: other.sym | CodeFunction | distance=0.3 | src=src/unread.py\\n  body\\n\\n\'\n'
        'OUTC2="$(vco_filter_seen_blocks "$INC2" "$INJ" "$RDS")"\n'
        '[ -n "$OUTC2" ] && echo "unread-src-injected"\n'
    )
    r = _run(snippet, tmp_path)
    assert "kg-read-src-suppressed" in r.stdout, (
        "a KG block whose repo-relative source was already Read must be suppressed"
    )
    assert "code-read-src-suppressed" in r.stdout, (
        "a CODE block whose repo-relative source was already Read must be suppressed"
    )
    assert "unread-src-injected" in r.stdout


def test_pretooluse_writes_repo_relative_to_unified_reads(tmp_path: Path) -> None:
    """SF-1 end-to-end: the pre-tool-use.sh Read branch must write a
    REPO-RELATIVE path into the unified reads store (seen-store kind 'reads'),
    matching the producers' repo-relative `| src=` shape. Drives the real hook
    with a Read payload whose file_path is ABSOLUTE under the project root, and
    asserts the recorded reads entry is the repo-relative form."""
    # Sandbox: a project root with the _lib helpers the hook sources.
    proot = tmp_path / "proj"
    (proot / "templates" / "hooks" / "_lib").mkdir(parents=True)
    (proot / ".claude" / "state").mkdir(parents=True)
    (proot / ".claude" / "scripts").mkdir(parents=True)
    for lib in ("session-id.sh", "seen-store.sh", "codegraph-query.sh"):
        (proot / "templates" / "hooks" / "_lib" / lib).write_bytes((LIB_DIR / lib).read_bytes())
    # Minimal _lib shims the hook sources unconditionally.
    (proot / "templates" / "hooks" / "_lib" / "stderr-cap.sh").write_text("# noop\n", encoding="utf-8")
    (proot / "templates" / "hooks" / "_lib" / "find-python.sh").write_text(
        'PY="$(command -v python3)"\n', encoding="utf-8"
    )
    (proot / "templates" / "hooks" / "_lib" / "emit-context.sh").write_text(
        "emit_additional_context() { :; }\n", encoding="utf-8"
    )
    hook = proot / "templates" / "hooks" / "pre-tool-use.sh"
    hook.write_bytes((REPO_ROOT / "templates" / "hooks" / "pre-tool-use.sh").read_bytes())

    # A code file under the project root; the Read payload uses its ABSOLUTE path.
    code_rel = "src/widget.py"
    code_abs = proot / code_rel
    code_abs.parent.mkdir(parents=True, exist_ok=True)
    code_abs.write_text("def f(): pass\n", encoding="utf-8")

    import json
    sid = "sf1sess"
    payload = {"tool_name": "Read", "session_id": sid, "tool_input": {"file_path": str(code_abs)}}
    import os
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(proot)}
    subprocess.run(
        ["bash", str(hook)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=30, env=env, cwd=str(proot),
    )
    # SF-1: the INJECTOR reads store is seen_reads_<sid>.txt (distinct from the
    # Build-Anchor reads_<sid>.txt). It must hold the REPO-RELATIVE path.
    unified = proot / ".claude" / "state" / f"seen_reads_{sid}.txt"
    assert unified.exists(), "injector reads store (seen_reads_<sid>.txt) was not written"
    content = unified.read_text(encoding="utf-8")
    assert code_rel in content.splitlines(), (
        f"injector reads store must hold the REPO-RELATIVE path '{code_rel}' "
        f"(matching producer src shape); got {content!r}"
    )
    assert str(code_abs) not in content, (
        f"injector reads store must NOT hold the absolute path; got {content!r}"
    )
    # The Build-Anchor ledger (reads_<sid>.txt) keeps the as-Read absolute shape.
    anchor = proot / ".claude" / "state" / f"reads_{sid}.txt"
    assert anchor.exists() and str(code_abs) in anchor.read_text(encoding="utf-8"), (
        "Build-Anchor reads_<sid>.txt must keep the as-Read (absolute) shape"
    )


# --------------------------------------------------------------------------
# double-source guard
# --------------------------------------------------------------------------
def test_double_source_is_noop(tmp_path: Path) -> None:
    py = shutil.which("python3") or "python3"
    script = (
        f'export PY="{py}"\n'
        f'. "{SEEN_SH}"\n'
        f'. "{SEEN_SH}"\n'   # source twice
        'echo "guard=$_VCO_SEEN_STORE_SOURCED"\n'
        # functions still defined + working after double-source
        'vco_seen_store_path inject abc /p\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "guard=1" in r.stdout
    assert "/p/.claude/state/seen_inject_abc.txt" in r.stdout


# --------------------------------------------------------------------------
# reset-on-compact wipes BOTH the inject store AND the injector reads store
# --------------------------------------------------------------------------
def test_post_compact_wipes_seen_inject_and_reads() -> None:
    """post-compact must wipe the inject store (E-5) AND the injector
    seen_reads store (SF-1) — on both OSes."""
    src = (REPO_ROOT / "templates" / "hooks" / "post-compact.sh").read_text(encoding="utf-8")
    assert "seen_inject_${SESSION_ID}.txt" in src, (
        "post-compact.sh must wipe the seen_inject_<id> store"
    )
    assert "seen_reads_${SESSION_ID}.txt" in src, (
        "post-compact.sh must wipe the seen_reads_<id> injector store (SF-1)"
    )
    ps1 = (REPO_ROOT / "templates" / "hooks" / "post-compact.ps1").read_text(encoding="utf-8")
    assert "seen_inject_$SessionId.txt" in ps1, (
        "post-compact.ps1 must wipe the seen_inject_<id> store"
    )
    assert "seen_reads_$SessionId.txt" in ps1, (
        "post-compact.ps1 must wipe the seen_reads_<id> injector store (SF-1)"
    )


# --------------------------------------------------------------------------
# shared abs->relative normaliser (one home — SF-1 / coordinator directive #1)
# --------------------------------------------------------------------------
def test_vco_to_repo_relative_strips_project_root(tmp_path: Path) -> None:
    """vco_to_repo_relative is the ONE shell-side home for the abs->relative
    src normalisation. Strips the project-root prefix; leaves already-relative
    + outside-root paths unchanged."""
    snippet = (
        'echo "[$(vco_to_repo_relative /proj/src/foo.py /proj)]"\n'      # under root -> relative
        'echo "[$(vco_to_repo_relative knowledge/bar.md /proj)]"\n'      # already relative -> unchanged
        'echo "[$(vco_to_repo_relative /other/baz.py /proj)]"\n'         # outside root -> unchanged
    )
    r = _run(snippet, tmp_path)
    assert "[src/foo.py]" in r.stdout, r.stdout
    assert "[knowledge/bar.md]" in r.stdout, r.stdout
    assert "[/other/baz.py]" in r.stdout, r.stdout


def test_pretooluse_uses_shared_normaliser_not_inline() -> None:
    """SF-1 one-home: pre-tool-use must CALL vco_to_repo_relative (no inline
    `${...#PROJECT_ROOT}` copy of the conversion)."""
    body = (REPO_ROOT / "templates" / "hooks" / "pre-tool-use.sh").read_text(encoding="utf-8")
    assert "vco_to_repo_relative" in body, (
        "pre-tool-use.sh must call the shared vco_to_repo_relative helper"
    )
    ps1 = (REPO_ROOT / "templates" / "hooks" / "pre-tool-use.ps1").read_text(encoding="utf-8")
    assert "ConvertTo-VcoRepoRelative" in ps1, (
        "pre-tool-use.ps1 must call the shared ConvertTo-VcoRepoRelative helper"
    )

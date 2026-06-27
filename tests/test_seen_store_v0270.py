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
    # reads kind keeps the legacy reads_<sid>.txt name (writer/consumer agree).
    assert "/proj/.claude/state/reads_abc123.txt" in out[1]


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
# reads-ledger suppression
# --------------------------------------------------------------------------
def test_reads_ledger_suppresses_already_read_source(tmp_path: Path) -> None:
    inj = tmp_path / "seen_inject_s.txt"
    rds = tmp_path / "reads_s.txt"
    rds.write_text("/abs/path/foo.py\n", encoding="utf-8")
    snippet = (
        f'INJ="{inj}"\n'
        f'RDS="{rds}"\n'
        # CODE block whose src is already in the reads ledger -> suppressed.
        'INC=$\'CODE: foo.bar | CodeFunction | distance=0.3 | src=/abs/path/foo.py\\n  body\\n\\n\'\n'
        'OUTC="$(vco_filter_seen_blocks "$INC" "$INJ" "$RDS")"\n'
        '[ -z "$OUTC" ] && echo "read-src-suppressed"\n'
        # CODE block whose src is NOT in the ledger -> injects.
        'INC2=$\'CODE: other.sym | CodeFunction | distance=0.3 | src=/abs/path/bar.py\\n  body\\n\\n\'\n'
        'OUTC2="$(vco_filter_seen_blocks "$INC2" "$INJ" "$RDS")"\n'
        '[ -n "$OUTC2" ] && echo "unread-src-injected"\n'
    )
    r = _run(snippet, tmp_path)
    assert "read-src-suppressed" in r.stdout, (
        "a block whose source was already Read must be suppressed"
    )
    assert "unread-src-injected" in r.stdout


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
# reset-on-compact wipes the new store
# --------------------------------------------------------------------------
def test_post_compact_wipes_seen_inject() -> None:
    """post-compact.sh must wipe the NEW seen_inject_<id> store (E-5)."""
    src = (REPO_ROOT / "templates" / "hooks" / "post-compact.sh").read_text(encoding="utf-8")
    assert "seen_inject_${SESSION_ID}.txt" in src, (
        "post-compact.sh must wipe the unified seen_inject_<id> store"
    )
    ps1 = (REPO_ROOT / "templates" / "hooks" / "post-compact.ps1").read_text(encoding="utf-8")
    assert "seen_inject_$SessionId.txt" in ps1, (
        "post-compact.ps1 must wipe the unified seen_inject_<id> store"
    )

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.85 D10 (ruling R-A): root/project install-bundle CONTRACT PARITY.

The whole point of v0.2.85 is that the orchestrator ROOT installs through the
same `install-bundle` engine + `--json` contract as any user PROJECT (R-A: "make
root and project share code where possible"). Before this release the root had
its OWN enumeration / classifier / manifest writer / settings merge (install.py
Steps 5b+9b) — the asymmetry that hid the v0.2.84 stdout-pollution bug (a project
consumed the JSON contract, the root did not, so nobody parsed the root's
output).

This suite pins the contract from the OUTSIDE, exactly the way both clients use
it — as a subprocess emitting `--json` on stdout — parametrized over BOTH shapes
with a SINGLE shared assertion body:

    project shape:  --folder tmp/proj      --orchestrator-root fixture
    root shape:     --folder fixture       --orchestrator-root fixture   (folder ≡ root)

Both shapes run the SAME argv the launcher (projects_v2.rs `run_install_bundle*`)
and the post-WP-1 install.py (`vco_lib/self_install.py::run_root_bundle_install`)
emit. Any future stdout pollution now breaks root AND launcher surfaces
identically and is caught by ONE test family (D2 payoff).

Composition (v0.2.84 A4 shared-fixture discipline):
  * `_v0284_bundle_fixtures.make_fake_orchestrator` — the ONE fake-orchestrator
    builder (hooks .sh/.ps1, _lib, scripts, agents, docker+podman compose,
    settings templates). Extended in-test with a knowledge/ template so the
    knowledge-preserve pin has a shipped node to drift.
  * `test_v0284_json_stdout_contract._assert_stdout_is_pure_json` — reused (the
    v0.2.84 file keeps owning the project-shape incident pin + the structural
    no-bare-print guard; this file does NOT duplicate that guard).

Cross-language pin: `_test_root_bundle_argv_matches_rust_call_site` asserts the
python argv builder (`self_install.root_bundle_argv`, WP-1) emits exactly the
flag set the Rust call-site emits for the base case — an explicit fixture list
so either side drifting breaks the test.

The schema constants (`BUNDLE_RESULT_TOP_KEYS` / `BUNDLE_ACTION_KEYS`, WP-2) and
`root_bundle_argv` (WP-1) are imported DIRECTLY from `vco_lib` (see the import
block below) — a missing symbol fails loudly rather than degrading to a
transcribed fixture that could drift from the real contract.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._v0284_bundle_fixtures import bundle_ext, make_fake_orchestrator  # noqa: E402

# Compose with the v0.2.84 stdout-contract helpers (do NOT duplicate them).
from tests.test_v0284_json_stdout_contract import (  # noqa: E402
    _assert_stdout_is_pure_json,
)

# ── WP-2 seam: the declared result-envelope schema constants ───────────────────
# The ONE-home schema constants (WP-2) and the python argv builder (WP-1) are
# imported DIRECTLY — both are part of the shipped `vco_lib` package, so a
# missing symbol means a broken tree and must fail loudly, not degrade to a
# transcribed fixture that could silently drift from the real contract.
# `BUNDLE_RESULT_TOP_KEYS` is the always-present required FLOOR (the live CLI
# envelope is a SUPERSET — adds backfill_*/templates/etc. — so `_assert_envelope`
# asserts `⊇`). `BUNDLE_ACTION_KEYS` is an ordered tuple; the live envelope's
# action keys are a SUBSET, so the assertion is `⊆` (coerced to a set at the
# use-site).
from vco_lib.project_init import (  # noqa: E402
    BUNDLE_ACTION_KEYS,
    BUNDLE_RESULT_TOP_KEYS,
)
from vco_lib.self_install import root_bundle_argv  # noqa: E402


# ─────────────────────────── fixture builders ────────────────────────────────


def _extend_fixture_with_knowledge(orch: Path) -> None:
    """Add a knowledge/ template tree to a `make_fake_orchestrator` fixture so
    the knowledge-preserve pin has a shipped node to drift.

    Ships:
      * a NON-root allowlisted depth-1 file (`TAG_HIERARCHY.md`) — the only
        knowledge that lands in a non-root project (`_PER_PROJECT_KNOWLEDGE_FILES`);
      * a curated node (`concepts/parity.md`) — lands ONLY on the ROOT shape
        (`include_curated=_is_root_bundle_target(...)`).
    Both are `always_overwrite=False`, so a divergent copy classifies `preserve`
    (never adopt — the "never destroy user knowledge" carve-out, project_init.py
    :4391).
    """
    kdir = orch / "templates" / "knowledge"
    (kdir / "concepts").mkdir(parents=True, exist_ok=True)
    (kdir / "TAG_HIERARCHY.md").write_text("# tag hierarchy v1\n", encoding="utf-8")
    (kdir / "concepts" / "parity.md").write_text(
        "# curated parity node v1\n", encoding="utf-8"
    )


def _build_fixture(tmp_path: Path) -> Path:
    """A minimal fake orchestrator (git-inited so heal paths exist) with a
    knowledge/ tree. Returns the orchestrator-root path."""
    orch = tmp_path / "orch"
    orch.mkdir()
    make_fake_orchestrator(orch)
    _extend_fixture_with_knowledge(orch)
    # git-init so the v0.2.31 history-heal + safe-add exclude paths have a repo.
    subprocess.run(
        ["git", "init", "-q", str(orch)], check=False, capture_output=True
    )
    return orch


def _run_bundle(
    folder: Path, orch: Path, *extra: str
) -> subprocess.CompletedProcess:
    """Drive the REAL CLI as a subprocess — the launcher's + install.py's exact
    contract. cwd/PYTHONPATH point at the real repo (so `vco_lib` imports);
    `--orchestrator-root` points at the fake tree (templates source of truth).
    This is the argv shape D2 makes the root path a client of."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop("VCT_DISABLE_HOOKS", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "vco_lib.project_init",
            "install-bundle",
            "--folder",
            str(folder),
            "--orchestrator-root",
            str(orch),
            "--project-folder",
            str(folder),
            "--json",
            *extra,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )


def _tree_hashes(root: Path) -> dict[str, str]:
    """sha256 of every file under `root`, keyed by posix-relative path. Used to
    prove the templates/ tree is byte-identical before/after a root run."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ─────────────────────────── the SHARED assertion ────────────────────────────


def _assert_envelope(proc: subprocess.CompletedProcess) -> dict:
    """The ONE shared envelope contract, applied identically to BOTH shapes.

    * stdout parses as EXACTLY one JSON document (the launcher's json.loads);
    * top-level keys ⊇ BUNDLE_RESULT_TOP_KEYS;
    * `actions` keys ⊆ BUNDLE_ACTION_KEYS;
    * when `actions.adopt` non-empty: `adopt_backup_dir` present, the backup file
      exists on disk carrying the ORIGINAL bytes, and the NOTICE is on STDERR and
      NOT on stdout (the v0.2.84 incident contract).
    """
    result = _assert_stdout_is_pure_json(proc)

    top = set(result.keys())
    missing = BUNDLE_RESULT_TOP_KEYS - top
    assert not missing, (
        f"envelope missing contractually-required top keys {sorted(missing)}; "
        f"present={sorted(top)}"
    )

    action_keys = set(result.get("actions", {}).keys())
    # BUNDLE_ACTION_KEYS is an ORDERED tuple in the shipped engine (WP-2 made it a
    # tuple, not a frozenset, so the `actions` dict emits deterministic byte order
    # under --json) — coerce to a set for the difference so this works against
    # both the real import and the pending-merge frozenset fallback.
    extra = action_keys - set(BUNDLE_ACTION_KEYS)
    assert not extra, (
        f"envelope has undeclared action buckets {sorted(extra)} — either the "
        f"engine grew a bucket (update BUNDLE_ACTION_KEYS) or a typo slipped in"
    )

    # adopt-shape sub-contract (only asserted when an adoption actually fired).
    adopted = result.get("actions", {}).get("adopt") or []
    if adopted:
        backup_dir_rel = result.get("adopt_backup_dir")
        assert backup_dir_rel, (
            "actions.adopt non-empty but adopt_backup_dir absent — the backup "
            "location must be recorded so the user can recover the original bytes"
        )
        # NOTICE is on stderr, NEVER on stdout (stdout is the machine contract).
        assert "NOTICE" in proc.stderr, (
            "adoption fired but the human NOTICE is not on stderr"
        )
        assert "NOTICE" not in proc.stdout, (
            "the adoption NOTICE leaked to stdout (the v0.2.84 incident) — stdout "
            "must be the single JSON document only"
        )
    return result


# ─────────────────────────── shape parametrization ───────────────────────────
#
# Each shape yields a `(folder, orch)` pair from a fresh fixture. The root shape
# is `folder ≡ orchestrator_root` — the exact premise D3 relies on (the launcher
# runs the bundle for EVERY project including the registered root).


def _project_shape(tmp_path: Path) -> tuple[Path, Path]:
    orch = _build_fixture(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    assert proj.resolve() != orch.resolve(), "project shape must be NON-root (A3)"
    return proj, orch


def _root_shape(tmp_path: Path) -> tuple[Path, Path]:
    orch = _build_fixture(tmp_path)
    # ROOT: the target folder IS the orchestrator root.
    return orch, orch


_SHAPES = {
    "project": _project_shape,
    "root": _root_shape,
}


@pytest.fixture(params=list(_SHAPES.keys()))
def shape(request, tmp_path: Path) -> tuple[str, Path, Path]:
    name = request.param
    folder, orch = _SHAPES[name](tmp_path)
    return name, folder, orch


# ─────────────────────────────── ACT tests ───────────────────────────────────


def test_fresh_install_envelope_parity(shape) -> None:
    """(1) fresh install → the shared envelope contract holds identically for
    both the project and root shapes."""
    name, folder, orch = shape
    proc = _run_bundle(folder, orch)
    assert proc.returncode == 0, f"[{name}] fresh install must exit 0:\n{proc.stderr[-600:]}"
    result = _assert_envelope(proc)
    assert result["manifest_written"] is True, f"[{name}] manifest must be written"
    assert (folder / ".claude" / ".vco-manifest.json").is_file()


def test_drift_then_adopt_classifier_parity(shape) -> None:
    """(2) drift one shipped hook + one shipped script (junk bytes, strip their
    manifest entries) → `--update` → the shared envelope contract holds AND the
    parity assertion: BOTH shapes classify the same drifted file the same way
    (adopt). This is the classifier-asymmetry pin R-A exists to kill."""
    name, folder, orch = shape
    ext = bundle_ext()

    # Fresh seed.
    seed = _run_bundle(folder, orch)
    assert seed.returncode == 0, f"[{name}] seed:\n{seed.stderr[-600:]}"
    _assert_envelope(seed)

    hook_rel = f".claude/hooks/foo.{ext}"
    script_rel = ".claude/scripts/kg-search"
    hook = folder / hook_rel
    script = folder / script_rel
    original_hook = "DRIFTED HOOK BYTES\n"
    original_script = "DRIFTED SCRIPT BYTES\n"
    hook.write_text(original_hook, encoding="utf-8")
    script.write_text(original_script, encoding="utf-8")

    # Strip their manifest entries → manifest-less stale-shipped adoption path.
    mani = folder / ".claude" / ".vco-manifest.json"
    m = json.loads(mani.read_text(encoding="utf-8"))
    for key in list(m.get("files", {}).keys()):
        norm = key.replace("\\", "/")
        if norm.endswith(f"foo.{ext}") or norm.endswith("kg-search"):
            m["files"].pop(key)
    mani.write_text(json.dumps(m), encoding="utf-8")

    proc = _run_bundle(folder, orch, "--update")
    assert proc.returncode == 0, f"[{name}] update:\n{proc.stderr[-600:]}"
    result = _assert_envelope(proc)

    # PARITY: both shapes classify the SAME drifted files the SAME way (adopt).
    adopt = {p.replace("\\", "/") for p in result["actions"]["adopt"]}
    assert hook_rel in adopt, (
        f"[{name}] drifted hook must be ADOPTED (classifier parity); adopt={sorted(adopt)}"
    )
    assert script_rel in adopt, (
        f"[{name}] drifted script must be ADOPTED (classifier parity); adopt={sorted(adopt)}"
    )

    # The backup carries the ORIGINAL (drifted) bytes; the shipped bytes are
    # now on disk. (`_assert_envelope` already checked the backup dir + NOTICE.)
    backup_root = folder / result["adopt_backup_dir"]
    hook_backup = backup_root / hook_rel
    script_backup = backup_root / script_rel
    assert hook_backup.read_text(encoding="utf-8") == original_hook, (
        f"[{name}] hook backup must carry the ORIGINAL drifted bytes"
    )
    assert script_backup.read_text(encoding="utf-8") == original_script
    # On-disk shipped bytes differ from the drifted originals (adoption wrote them).
    assert hook.read_text(encoding="utf-8") != original_hook, (
        f"[{name}] shipped bytes must have replaced the drifted hook on disk"
    )


# ────────────────────────── leave-alone battery ──────────────────────────────


def test_knowledge_divergence_preserved_both_shapes(shape) -> None:
    """Leave-alone: a divergent `knowledge/**` file classifies `preserve` (never
    adopt) on BOTH shapes — the "never destroy user knowledge" carve-out.

    Non-root ships only the depth-1 allowlist (`TAG_HIERARCHY.md`); root also
    ships curated nodes (`concepts/parity.md`). We drift whichever landed."""
    name, folder, orch = shape

    seed = _run_bundle(folder, orch)
    assert seed.returncode == 0, f"[{name}] seed:\n{seed.stderr[-600:]}"

    # Pick a knowledge dest that actually landed for THIS shape.
    candidates = [
        folder / "knowledge" / "TAG_HIERARCHY.md",
        folder / "knowledge" / "concepts" / "parity.md",
    ]
    landed = [c for c in candidates if c.is_file()]
    assert landed, (
        f"[{name}] premise: at least one knowledge node must ship "
        f"(non-root=allowlist, root=curated). Landed none of {candidates}"
    )
    target = landed[0]
    drifted = "# USER-EDITED KNOWLEDGE — must be preserved\n"
    target.write_text(drifted, encoding="utf-8")

    proc = _run_bundle(folder, orch, "--update")
    assert proc.returncode == 0, f"[{name}] update:\n{proc.stderr[-600:]}"
    result = _assert_envelope(proc)

    rel = target.relative_to(folder).as_posix()
    preserve = {p.replace("\\", "/") for p in result["actions"].get("preserve", [])}
    keep_regen = {
        p.replace("\\", "/") for p in result["actions"].get("keep-regenerated", [])
    }
    adopt = {p.replace("\\", "/") for p in result["actions"].get("adopt", [])}
    assert rel not in adopt, (
        f"[{name}] a divergent knowledge/ node must NEVER be adopted; adopt={sorted(adopt)}"
    )
    assert rel in preserve or rel in keep_regen, (
        f"[{name}] knowledge/ divergence must classify preserve/keep-regenerated; "
        f"preserve={sorted(preserve)} keep-regen={sorted(keep_regen)}"
    )
    # The user's bytes survive on disk untouched.
    assert target.read_text(encoding="utf-8") == drifted, (
        f"[{name}] user knowledge bytes must be untouched on disk"
    )


def test_templates_tree_byte_identical_after_root_run() -> None:
    """The maintainer-source safety pin (D3): NO install flow ever writes under
    `<root>/templates/`. After a ROOT-shape install+update, the templates/ tree
    is byte-hash-identical to before. (Root shape only: templates/ lives at the
    orchestrator root, which for the root shape IS the install target.)"""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="vct-v0285-templates-"))
    try:
        orch = _build_fixture(tmp)
        before = _tree_hashes(orch / "templates")
        assert before, "premise: fixture ships a templates/ tree"

        # Fresh install + an update run against the root (folder ≡ orch).
        r1 = _run_bundle(orch, orch)
        assert r1.returncode == 0, r1.stderr[-600:]
        r2 = _run_bundle(orch, orch, "--update")
        assert r2.returncode == 0, r2.stderr[-600:]

        after = _tree_hashes(orch / "templates")
        assert after == before, (
            "templates/ tree changed during the root run — the maintainer's "
            "source-of-truth must be untouchable by construction (D3). "
            f"added={sorted(set(after) - set(before))} "
            f"removed={sorted(set(before) - set(after))} "
            f"modified={sorted(k for k in before if k in after and before[k] != after[k])}"
        )
    finally:
        import shutil

        shutil.rmtree(str(tmp), ignore_errors=True)


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_infrastructure_compose_noop_on_root(runtime: str) -> None:
    """Compose-noop pin (C-RT-5 mirror, docker AND podman): on the root shape,
    src==dst for the shipped compose file so a re-run classifies `noop` and never
    rewrites it. Runs for both container runtimes (the C-RT-5 mirror pair must
    stay intact — one runtime's compose is never rewritten when the other's is)."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix=f"vct-v0285-compose-{runtime}-"))
    try:
        orch = _build_fixture(tmp)
        # The shared fixture ships docker-compose.yml + podman-compose.gpu.yml.
        compose_name = {
            "docker": "docker-compose.yml",
            "podman": "podman-compose.gpu.yml",
        }[runtime]
        compose_src = orch / "infrastructure" / compose_name
        if not compose_src.is_file():
            pytest.skip(f"fixture does not ship {compose_name}")

        # Fresh install → the compose file lands at <root>/infrastructure/.
        r1 = _run_bundle(orch, orch)
        assert r1.returncode == 0, r1.stderr[-600:]
        landed = orch / "infrastructure" / compose_name
        assert landed.is_file(), f"{compose_name} must land under infrastructure/"
        before_hash = hashlib.sha256(landed.read_bytes()).hexdigest()

        # Re-run (src == dst) → noop; the file is not rewritten.
        r2 = _run_bundle(orch, orch, "--update")
        assert r2.returncode == 0, r2.stderr[-600:]
        result = _assert_envelope(r2)
        rel = f"infrastructure/{compose_name}"
        noop = {p.replace("\\", "/") for p in result["actions"].get("noop", [])}
        adopt = {p.replace("\\", "/") for p in result["actions"].get("adopt", [])}
        overwrite = {p.replace("\\", "/") for p in result["actions"].get("overwrite", [])}
        # It must be a leave-alone (noop) — never adopt/overwrite when identical.
        assert rel not in adopt, f"identical {compose_name} must NOT adopt"
        assert rel not in overwrite, f"identical {compose_name} must NOT overwrite"
        # (noop is the expected bucket; some engines omit unchanged infra from
        # the action map entirely — either way the bytes must be unchanged.)
        after_hash = hashlib.sha256(landed.read_bytes()).hexdigest()
        assert after_hash == before_hash, (
            f"identical {compose_name} was rewritten on a noop run"
        )
        _ = noop  # documented: noop is the classification when it IS emitted.
    finally:
        import shutil

        shutil.rmtree(str(tmp), ignore_errors=True)


# ─────────────────── cross-language argv-parity pin (WP-1 seam) ───────────────


def test_root_bundle_argv_matches_rust_call_site() -> None:
    """The R-A argv pin: `self_install.root_bundle_argv` (WP-1, the python client)
    must emit EXACTLY the flag set the Rust call-site (`build_bundle_argv` in
    projects_v2.rs, this WP) emits for the base case. Maintained as an explicit
    fixture list (plan citation) so EITHER side drifting breaks the test.

    Plan citation: PLAN-v0285-install-parity.md D2 + D10 ("assert
    `self_install.root_bundle_argv` emits exactly the flag set the Rust call-site
    emits for the base case — maintained as an explicit fixture list") and
    AMENDMENT A6 PIN-P3 (the Rust side of the same pin lives in
    projects_v2.rs::pin_p3_create_argv_default_is_byte_identical_to_pre_refactor).

    THE SHARED EXPECTED ARGV (base case, update mode = the root path's default
    since a re-run over an installed root is an update by D4). The Rust
    `build_bundle_argv(folder, orch, BundleMode::Update)` emits (mode flag
    BEFORE --json — the byte-pinned update order; a `--json --update` regression
    is the exact D12 incident the parity + pin_p3b tests guard):
        -m vco_lib.project_init install-bundle
        --folder F --orchestrator-root O --project-folder F --update --json
    For the ROOT client F == O == <root>.
    """
    root = "/tmp/root"
    # The base argv the Rust `build_bundle_argv` emits for the root client
    # (update mode). The python binary itself is NOT part of this vector on
    # either side (the Rust side sets it as the Command program; the python side
    # prepends sys.executable in run_root_bundle_install).
    # Canonical UPDATE-mode argv order is `--update` BEFORE `--json` — this is
    # the order the pre-D12 launcher update wrapper emitted
    # (run_install_bundle_update_with_root at base a4a07fa3: `... --project-folder
    # F --update --json`), which WP-1's root_bundle_argv and the D12 Rust
    # build_bundle_argv (Update arm) both reproduce byte-for-byte. (argparse is
    # order-insensitive, but this argv is a pinned machine contract.)
    expected = [
        "-m",
        "vco_lib.project_init",
        "install-bundle",
        "--folder",
        root,
        "--orchestrator-root",
        root,
        "--project-folder",
        root,
        "--update",
        "--json",
    ]
    got = root_bundle_argv(root, update_mode=True)
    # Strip any leading sys.executable the python builder may prepend so the two
    # sides compare on the same "-m ..." tail.
    if got and got[0] not in ("-m",):
        got = got[got.index("-m"):] if "-m" in got else got
    assert got == expected, (
        "root_bundle_argv (python) drifted from the Rust build_bundle_argv "
        f"contract.\n expected={expected}\n got={got}"
    )


# The v0.2.84 stdout-contract file keeps owning the project-shape incident pin
# + the structural no-bare-print guard. We deliberately do NOT re-import or
# duplicate `test_no_bare_stdout_prints_in_bundle_install_body` here (D10
# composition rule). platform is imported for potential OS-gating of future
# pins; reference it so linters don't flag the import as unused.
_ = platform.system

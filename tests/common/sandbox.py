# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-run sandbox isolation for v0.2.21 integration tests.

The Step 22 multi-project access-matrix regression test (and any
follow-up integration tests that touch real launcher state) MUST
NOT leak fixture state into a real user's ``~/.vct/`` directory,
real Weaviate collection namespace, or real OS keychain.

This module owns the four defenses:

1. **Per-run state-dir override** — ``VCT_STATE_DIR`` is set to a
   scratch directory keyed on a unique RUN_ID (``$GITHUB_RUN_ID`` if
   set, else random hex). All hub artifacts (``launcher.db``,
   ``hub.pid``, ``hub.port``, ``hub.token``, ``cache/``) land there.
2. **Per-run Weaviate collection prefix** — collections the fixture
   creates are namespaced with ``STEP22_<run_id>_…`` so teardown can
   drop them as a group via prefix-match without risking a real
   ``ClaudeKnowledgeGraph`` / ``VibeCodedOrchestrator_KnowledgeGraph``.
3. **Per-run keychain prefix** — any secrets the fixture writes use
   module-id prefix ``step22-<run_id>-`` so teardown can delete the
   set as a group.
4. **CI-only refuse-to-run** — :func:`assert_sandbox_safe` exits 1
   if ``VCO_CI_FIXTURE`` is unset or ``VCT_STATE_DIR`` points at
   ``$HOME/.vct`` (or is unset). Defense-in-depth so a developer
   can't accidentally run the fixture on their workstation and
   corrupt real state.

Designed to be importable from both pytest and standalone scripts —
no pytest dependency at module level.
"""
from __future__ import annotations

import os
import secrets
import shutil
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional


# Magic suffix shared by every artifact a fixture creates. Teardown
# matches on this prefix to scope its destructiveness.
STEP22_COLLECTION_PREFIX = "STEP22_"
STEP22_KEYCHAIN_MODULE_PREFIX = "step22-"


def compute_run_id() -> str:
    """Return a stable, unique ID for this CI run.

    Priority: ``$GITHUB_RUN_ID`` (real CI), else ``$VCO_TEST_RUN_ID``
    (caller-supplied override for local debugging), else a fresh
    8-hex-char random token. The returned string is safe for use in
    file paths and Weaviate collection names (alphanumeric only)."""
    for key in ("GITHUB_RUN_ID", "VCO_TEST_RUN_ID"):
        v = os.environ.get(key, "").strip()
        if v and v.isalnum():
            return v
    return secrets.token_hex(4)


def assert_sandbox_safe(state_dir: Path) -> None:
    """Refuse to run the fixture if defenses (1)/(4) are absent.

    Raises ``SystemExit(1)`` with a precise error message rather than
    a python exception so the script exits with a CI-visible message
    even when invoked outside pytest."""
    if os.environ.get("VCO_CI_FIXTURE", "") != "1":
        print(
            "[step22-fixture] REFUSING TO RUN: VCO_CI_FIXTURE=1 is not set. "
            "This fixture must only run in CI or under explicit local debug. "
            "Set VCO_CI_FIXTURE=1 to acknowledge.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    home = Path.home().resolve()
    default_vct = home / ".vct"
    sd = state_dir.resolve()
    if sd == default_vct or sd == home:
        print(
            f"[step22-fixture] REFUSING TO RUN: VCT_STATE_DIR={sd} points at "
            f"the real default state dir ({default_vct}). Pick a scratch path.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    # Also reject anything inside ~/.vct/ proper — that would polluate
    # the same hub.pid / launcher.db files even if the user's relative
    # path is "deeper".
    try:
        sd.relative_to(default_vct)
        print(
            f"[step22-fixture] REFUSING TO RUN: VCT_STATE_DIR={sd} is a "
            f"subpath of the real {default_vct}. Pick a scratch path "
            f"outside ~/.vct/.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except ValueError:
        # Good — state_dir is NOT under ~/.vct/.
        pass


@dataclass
class SandboxLayout:
    """Computed paths + namespaces for a single test run.

    Created by :func:`make_sandbox`. Owns no resources directly —
    just precomputes every name a fixture / teardown needs.
    """

    run_id: str
    state_dir: Path
    # Collection name prefix scoped to this run. Use as
    # ``layout.collection_name("ProjectA_KnowledgeGraph")`` to get
    # ``STEP22_<run_id>_ProjectA_KnowledgeGraph``.
    collection_prefix: str
    keychain_module_prefix: str
    # Optional: scratch root for the 3 mock project directories. Not
    # populated until ``make_sandbox(..., with_project_root=True)``.
    project_root: Optional[Path] = None
    # Track every collection name we touched so teardown can drop them
    # without re-discovering via HTTP list endpoints.
    created_collections: list[str] = field(default_factory=list)

    def collection_name(self, suffix: str) -> str:
        """Compose a sandbox-scoped Weaviate collection name."""
        return f"{self.collection_prefix}{suffix}"

    def keychain_module(self, suffix: str) -> str:
        """Compose a sandbox-scoped keychain module-id."""
        return f"{self.keychain_module_prefix}{suffix}"

    def launcher_db_path(self) -> Path:
        return self.state_dir / "launcher.db"

    def hub_port_file(self) -> Path:
        return self.state_dir / "hub.port"

    def hub_token_file(self) -> Path:
        return self.state_dir / "hub.token"

    def hub_pid_file(self) -> Path:
        return self.state_dir / "hub.pid"

    def hub_stdout_log(self) -> Path:
        return self.state_dir / "hub.stdout.log"

    def hub_stderr_log(self) -> Path:
        """Where a spawned hub's diagnostics are REDIRECTED to.

        The hub logs every diagnostic to stderr for the lifetime of the
        process (see vct-hub/src/main.rs's "Diagnostics" section); real
        callers send that to a file or /dev/null. A test harness that
        hands it ``subprocess.PIPE`` and never reads the pipe gives it a
        fixed-size kernel buffer instead — 4 KiB on Windows, 64 KiB on
        Linux, 16 KiB on macOS — and when that buffer fills the hub
        BLOCKS inside the write, holding Rust's process-wide stderr lock,
        so every request handler that logs blocks with it. The symptom is
        a request that is accepted and then never answered: the client
        times out in ``getresponse()``, not at connect. Windows sees it
        first because its buffer is the smallest.

        A regular file never blocks, so the redirect removes the hazard
        outright rather than racing a reader thread against it — and it
        leaves the hub's own account of a failure on disk, which a pipe
        that was drained into a discard would not.
        """
        return self.state_dir / "hub.stderr.log"

    def cache_dir(self) -> Path:
        return self.state_dir / "cache"


def make_sandbox(
    *,
    runner_temp: Optional[Path] = None,
    with_project_root: bool = False,
    run_id: Optional[str] = None,
) -> SandboxLayout:
    """Materialise a sandbox layout on disk and return it.

    ``runner_temp`` defaults to ``$RUNNER_TEMP`` (set by GitHub Actions)
    or the platform tempdir. ``with_project_root=True`` also creates a
    ``projects/`` subdirectory inside the sandbox where the caller can
    mkdir the per-project folders that get registered in launcher.db.

    Runs :func:`assert_sandbox_safe` on the chosen ``state_dir`` before
    creating anything — raises ``SystemExit(1)`` if a defense fails.
    """
    if runner_temp is None:
        rt = os.environ.get("RUNNER_TEMP", "").strip()
        if rt:
            runner_temp = Path(rt)
        else:
            import tempfile

            runner_temp = Path(tempfile.gettempdir())

    rid = run_id or compute_run_id()
    state_dir = runner_temp / f".vct-step22-{rid}"
    # Important: assert BEFORE mkdir so we never create a forbidden dir.
    assert_sandbox_safe(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "cache").mkdir(exist_ok=True)

    layout = SandboxLayout(
        run_id=rid,
        state_dir=state_dir,
        collection_prefix=f"{STEP22_COLLECTION_PREFIX}{rid}_",
        keychain_module_prefix=f"{STEP22_KEYCHAIN_MODULE_PREFIX}{rid}-",
    )
    if with_project_root:
        layout.project_root = state_dir / "projects"
        layout.project_root.mkdir(exist_ok=True)
    return layout


def teardown_sandbox(
    layout: SandboxLayout,
    *,
    drop_weaviate_collections: bool = True,
    weaviate_url: Optional[str] = None,
    delete_keychain: bool = False,
) -> list[str]:
    """Drop every artifact tied to this sandbox.

    Returns a list of human-readable status lines describing what was
    cleaned (or skipped, with reason). Designed to be IDEMPOTENT —
    running it twice is a no-op the second time.

    Always runs the on-disk cleanup. Weaviate / keychain cleanups are
    opt-in because not every fixture creates those side effects.
    """
    notes: list[str] = []

    # 1. Weaviate collections (HTTP — works without the python client lib).
    if drop_weaviate_collections and layout.created_collections:
        wu = weaviate_url or os.environ.get(
            "WEAVIATE_URL", "http://localhost:8081"
        ).rstrip("/")
        notes.extend(_drop_weaviate_prefix(wu, layout))

    # 2. Keychain entries. Opt-in — not all fixtures touch secrets.
    if delete_keychain:
        notes.append(
            f"[keychain] skipped — keychain teardown handled out-of-band "
            f"(no fixture in step22 wrote secrets under {layout.keychain_module_prefix})"
        )

    # 3. On-disk state. Always runs.
    if layout.state_dir.exists():
        try:
            shutil.rmtree(layout.state_dir, ignore_errors=False)
            notes.append(f"[state-dir] removed {layout.state_dir}")
        except OSError as e:
            notes.append(f"[state-dir] FAILED to remove {layout.state_dir}: {e}")
    else:
        notes.append(f"[state-dir] already gone: {layout.state_dir}")

    return notes


def _drop_weaviate_prefix(weaviate_url: str, layout: SandboxLayout) -> list[str]:
    """DELETE every Weaviate class whose name starts with layout.collection_prefix.

    We iterate the recorded ``created_collections`` list rather than
    GET /v1/schema and prefix-match, because (a) the recorded list is
    deterministic and (b) avoids accidentally dropping a real-prod
    collection if STEP22_ collision somehow occurs.
    """
    import urllib.request
    import urllib.error

    notes: list[str] = []
    for coll in sorted(set(layout.created_collections)):
        url = f"{weaviate_url}/v1/schema/{coll}"
        req = urllib.request.Request(url, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if 200 <= resp.status < 300:
                    notes.append(f"[weaviate] dropped {coll}")
                else:
                    notes.append(
                        f"[weaviate] DELETE {coll} returned HTTP {resp.status}"
                    )
        except urllib.error.HTTPError as e:
            if e.code == 404:
                notes.append(f"[weaviate] {coll} already absent")
            else:
                notes.append(f"[weaviate] DELETE {coll} HTTPError {e.code}")
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            notes.append(
                f"[weaviate] DELETE {coll} connection failed: {e}; "
                f"non-fatal (test may have run without Weaviate)"
            )
    return notes


@contextmanager
def sandbox_environ(layout: SandboxLayout) -> Iterator[dict[str, str]]:
    """Context manager that sets the env vars callers need to invoke
    the hub binary / resolver client against the sandbox.

    Yields the env dict that callers can pass to subprocess. Restores
    prior values on exit.
    """
    overrides = {
        "VCT_STATE_DIR": str(layout.state_dir),
        "VCO_CI_FIXTURE": "1",
        # Keep these test-mode env vars in the contract for clarity.
        "VCO_TEST_RUN_ID": layout.run_id,
    }
    saved: dict[str, Optional[str]] = {
        k: os.environ.get(k) for k in overrides.keys()
    }
    try:
        for k, v in overrides.items():
            os.environ[k] = v
        yield {**os.environ, **overrides}
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev

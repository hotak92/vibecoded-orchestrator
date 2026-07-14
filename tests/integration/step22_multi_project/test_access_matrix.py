# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Step 22 — multi-project access-matrix regression (pytest entrypoint).

This is the consumer side of the fixture in :mod:`.fixture`. It
boots the ``vct-hub`` binary against a sandboxed launcher.db with 3
projects + asymmetric access-matrix rows, then proves that:

1. Every project reaches the hub and gets a complete
   ``ProjectConfigResponse`` envelope.
2. The ``kg_access_list`` for each project matches EXACTLY the
   collections that project's ``kg_collection_access`` rows + its
   own primary KG binding would imply — and NOTHING else.
3. The ``codegraph_access_list`` for each project matches EXACTLY
   the grantor NAMES that ``codegraph_access`` rows + its own name
   would imply — and NOTHING else (GAP-CG-1 fix 2026-07-14: was
   grantor slugs; the consumer needs the analyzer-stamped NAME).
4. The single-field key= shortcut produces identical results to the
   full envelope (no client-side reassembly drift).
5. The resolver client script ``vct_project_config.sh`` returns the
   same JSON as a direct HTTP call (no shell-side filtering drift).
6. The 'none' access-level rows are filtered out of `kg_access_list`
   (explicit-deny pathway).
7. Cross-project codegraph reads stay scoped per the grant matrix —
   ``codegraph_access_list`` contains the GRANTOR's NAME (because B
   was granted read on A's codegraph), not the GRANTEE's.

These 7 assertions together cover acceptance criterion properties
(5), (6), (7), (8 — via env propagation), and (9 — KG_COLLECTION
matches resolver's answer) of v0.2.21's release-gate list.
Properties (1–4) are seeded into the fixture by hand and re-asserted
on read. Property (10) is GUI-driven and not in scope here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest

# Allow tests.common import when pytest is launched from repo root.
_TESTS_DIR = Path(__file__).resolve().parents[2]
if str(_TESTS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR.parent))

from tests.common.sandbox import compute_run_id, teardown_sandbox  # noqa: E402
from tests.integration.step22_multi_project.fixture import (  # noqa: E402
    CANONICAL_PROJECTS,
    FixtureResult,
    HubProc,
    ProjectSpec,
    build_fixture,
    start_hub,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
HUB_BINARY = (
    REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "target"
    / "release"
    / "vct-hub"
)
RESOLVER_CLIENT = REPO_ROOT / "templates" / "scripts" / "vct_project_config.sh"


# ─── Pytest fixtures ─────────────────────────────────────────────


def _hub_binary_path() -> Path:
    """Honour ``VCT_HUB_BINARY`` env (workflow injects the absolute
    path) so the workflow can build to a non-default target dir
    without the test having to guess. Falls back to the standard
    release-profile location used locally."""
    custom = os.environ.get("VCT_HUB_BINARY", "").strip()
    if custom:
        return Path(custom)
    return HUB_BINARY


@pytest.fixture(scope="module")
def fixture_result() -> Iterator[FixtureResult]:
    """Module-scoped fixture: build the sandbox + launcher.db once,
    reuse across every test in this file. Teardown drops every
    sandbox artifact on exit."""
    # Defense (4) — refuse without VCO_CI_FIXTURE=1. The fixture's
    # own assert_sandbox_safe does this, but we hoist a clearer
    # skip message when running locally without intent.
    if os.environ.get("VCO_CI_FIXTURE", "") != "1":
        pytest.skip(
            "Step 22 integration tests require VCO_CI_FIXTURE=1 "
            "(this is the sandbox-isolation refusal gate). Run with "
            "`VCO_CI_FIXTURE=1 pytest tests/integration/step22_multi_project/`"
        )
    fx = build_fixture()
    try:
        yield fx
    finally:
        # Drop on-disk state + (best-effort) Weaviate classes. We
        # don't fail the test on teardown errors — they're logged.
        notes = teardown_sandbox(
            fx.layout,
            drop_weaviate_collections=True,
        )
        for n in notes:
            print(n, file=sys.stderr)


@pytest.fixture(scope="module")
def hub(fixture_result: FixtureResult) -> Iterator[HubProc]:
    """Spawn vct-hub once and reuse it across the test module."""
    bin_path = _hub_binary_path()
    if not bin_path.exists():
        pytest.skip(
            f"vct-hub binary not found at {bin_path}. Build with "
            f"`cargo build --release -p vct-hub` (from launcher/src-tauri/) "
            f"or set $VCT_HUB_BINARY to the absolute path of the binary."
        )
    proc = start_hub(fixture_result.layout, bin_path)
    try:
        yield proc
    finally:
        proc.stop()


# ─── HTTP helpers ────────────────────────────────────────────────


def _scoped_token(hub: HubProc, project_id: str) -> str:
    """Read the per-project scoped token the hub minted at startup.

    v0.2.77 Part 8 (flip): the per-project ``/env`` + ``/config`` routes
    require the SCOPED ``hub.token.<project_id>`` (the global ``hub.token``
    is refused once the compat window closes). The hub writes one scoped
    file per project into ``VCT_STATE_DIR`` at startup mint, BEFORE it
    writes ``hub.port`` (so by the time ``start_hub`` returns, every
    project's file exists). This helper reads it so Step22 exercises the
    POST-flip reality — the same credential a real resolver presents.
    """
    path = hub.layout.state_dir / f"hub.token.{project_id}"
    return path.read_text(encoding="utf-8").strip()


def _hub_get(
    hub: HubProc, path: str, *, project_id: str | None = None
) -> tuple[int, dict]:
    """GET <hub>/<path>. Returns (status, json).

    When ``project_id`` is given, present that project's SCOPED token
    (the credential the per-project routes require post-flip). Otherwise
    present the global ``hub.token`` (for non-per-project routes, or to
    deliberately exercise the compat/refusal path).
    """
    token = _scoped_token(hub, project_id) if project_id is not None else hub.token
    url = f"{hub.base_url()}/{path.lstrip('/')}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw_body": body}


def _expected_kg_access_list(
    project: ProjectSpec, fx: FixtureResult
) -> list[str]:
    """Compute the resolver's expected kg_access_list for `project`.

    Reproduces config_api.rs's logic (filter 'none', add own primary,
    sort + dedup) but from the canonical access-matrix shape encoded
    in fixture.py rather than re-reading the DB. The whole point of
    the test is to PIN that the implementation agrees with this
    canonical computation."""
    layout = fx.layout
    own_primary = layout.collection_name(project.kg_collection_name())
    extras: set[str] = set()
    a = fx.project_by_slug("proj-a-alpha")
    b = fx.project_by_slug("proj-b-beta")
    if project.slug == a.slug:
        # A explicitly reads B's primary.
        extras.add(layout.collection_name(b.kg_collection_name()))
    # B / C have no extra rows (B has nothing; C has only a 'none'
    # row which the resolver MUST filter out).
    return sorted({own_primary, *extras})


def _expected_codegraph_access_list(project: ProjectSpec) -> list[str]:
    """Compute the resolver's expected codegraph_access_list.

    GAP-CG-1 fix (2026-07-14): the list carries grantor display NAMES,
    not slugs — the consumer rebuilds each peer's Weaviate class prefix +
    ``project`` filter from the NAME the analyzer wrote (a slug diverges
    on both). The step22 fixture's projects already have two-token
    display names that differ from their slugs (``"Project A (Alpha)"``
    vs ``proj-a-alpha``), so this assertion now genuinely exercises the
    slug≠name divergence the fix closes.

    From fixture.py: A grants B read on A's codegraph (and that's
    the only grant). So (by NAME):
        A receives nothing → [A]              (own name only)
        B receives [A] (from A) → [A, B]      (with own name added)
        C receives nothing → [C]              (own name only)
    """
    a = _spec_by_slug("proj-a-alpha")
    b = _spec_by_slug("proj-b-beta")
    if project.slug == "proj-b-beta":
        return sorted([a.display_name, b.display_name])
    return [project.display_name]


def _spec_by_slug(slug: str) -> ProjectSpec:
    """Look up a canonical fixture ProjectSpec by slug (module-level so the
    expectation helper can resolve grantor NAMES from their slugs)."""
    for spec in CANONICAL_PROJECTS:
        if spec.slug == slug:
            return spec
    raise KeyError(slug)


# ─── Tests ───────────────────────────────────────────────────────


def test_hub_health_responds(hub: HubProc) -> None:
    """Smoke check: /api/v1/health responds without auth.

    The auth middleware exempts ``/api/v1/health`` so liveness
    probes can run without owning the token (see vct-hub::auth's
    ``HEALTH_PATHS`` allowlist). This test pins that exemption."""
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{hub.port}/api/v1/health")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200


def test_resolver_returns_full_envelope_for_every_project(
    fixture_result: FixtureResult, hub: HubProc
) -> None:
    """Property (7) — every registered project's /config returns 200
    with every documented field populated and non-empty."""
    required_fields = {
        "project_id",
        "project_path",
        "project_slug",
        "project_display_name",
        "code_graph_project",
        "code_graph_collection_prefix",
        "kg_collection",
        "shared_kg_collection",
        "development_collection",
        "active_embedding",
        "embedding_models",
        "kg_access_list",
        "codegraph_access_list",
        "weaviate_url",
        "ollama_url",
        "grpc_port",
        "shared_kg_write_disabled",
    }
    for p in fixture_result.projects:
        status, body = _hub_get(
            hub, f"projects/{p.project_id}/config", project_id=p.project_id
        )
        assert status == 200, f"project {p.slug}: status {status} body {body}"
        missing = required_fields - body.keys()
        assert not missing, (
            f"project {p.slug}: missing fields {missing} from /config"
        )
        # Critical correctness asserts:
        assert body["project_id"] == p.project_id
        assert body["project_slug"] == p.slug
        # `code_graph_project` is a documented alias for `project_slug`
        # (config_api.rs line 137 comment) — verify it stays in lockstep.
        assert body["code_graph_project"] == body["project_slug"]


def test_kg_access_list_matches_matrix_per_project(
    fixture_result: FixtureResult, hub: HubProc
) -> None:
    """Property (5) — kg_access_list reflects EXACTLY the rows in
    kg_collection_access with level IN ('read','write'), plus own
    primary, sorted + deduped. NO leakage across projects.

    The strongest assertion in this test file. We pin the EXACT
    list against the canonical access matrix from fixture.py so a
    silent regression (e.g. the filter dropping 'read' instead of
    'none') would surface as a list mismatch."""
    for p in fixture_result.projects:
        status, body = _hub_get(
            hub, f"projects/{p.project_id}/config", project_id=p.project_id
        )
        assert status == 200
        actual = sorted(body["kg_access_list"])
        expected = _expected_kg_access_list(p, fixture_result)
        assert actual == expected, (
            f"project {p.slug}: kg_access_list mismatch\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}"
        )


def test_kg_access_list_filters_explicit_none_rows(
    fixture_result: FixtureResult, hub: HubProc
) -> None:
    """The fixture seeds an explicit ``access_level='none'`` row for
    C → B's primary KG. The resolver MUST filter it out. (Resolver
    bug class: filtering by row-existence instead of by level would
    leak B's KG into C's access list.)"""
    c = fixture_result.project_by_slug("proj-c-gamma")
    b = fixture_result.project_by_slug("proj-b-beta")
    status, body = _hub_get(
        hub, f"projects/{c.project_id}/config", project_id=c.project_id
    )
    assert status == 200
    b_kg = fixture_result.layout.collection_name(b.kg_collection_name())
    assert b_kg not in body["kg_access_list"], (
        f"C's kg_access_list LEAKED B's collection {b_kg!r}: "
        f"{body['kg_access_list']!r} — the 'none' row was not filtered"
    )


def test_codegraph_access_list_matches_grant_matrix(
    fixture_result: FixtureResult, hub: HubProc
) -> None:
    """Property (6) — codegraph_access_list contains GRANTOR NAMES
    (not grantee), plus own name, sorted + deduped. GAP-CG-1 fix
    (2026-07-14): was grantor slugs; the consumer rebuilds each peer's
    prefix + project filter from the analyzer-stamped NAME, so the list
    must carry NAMES. The fixture's two-token display names
    (``"Project A (Alpha)"`` vs slug ``proj-a-alpha``) make this
    assertion exercise the slug≠name divergence the fix closes.

    The grant in fixture.py is A→B (A grants B read). So (by NAME):
        * B's list MUST contain 'Project A (Alpha)' (the grantor it reads).
        * A's list MUST NOT contain 'Project B (Beta)' (A is the grantor,
          and B has not granted A back).
        * C's list MUST NOT contain either A or B (no grants involve C).

    Catches the common direction-flip bug where grantor + grantee
    are swapped in the JOIN, AND the GAP-CG-1 identity-form bug."""
    for p in fixture_result.projects:
        status, body = _hub_get(
            hub, f"projects/{p.project_id}/config", project_id=p.project_id
        )
        assert status == 200
        actual = sorted(body["codegraph_access_list"])
        expected = _expected_codegraph_access_list(p)
        assert actual == expected, (
            f"project {p.slug}: codegraph_access_list mismatch\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}"
        )


def test_no_kg_access_leakage_between_unrelated_projects(
    fixture_result: FixtureResult, hub: HubProc
) -> None:
    """Stronger leakage check: for every pair (Pi, Pj) where Pi has
    NOT been explicitly granted read on Pj's primary KG, Pj's
    primary collection must NOT appear in Pi's kg_access_list.

    This catches the regression class where access checks become
    permissive (e.g. 'all read access by default')."""
    projects = fixture_result.projects
    layout = fixture_result.layout
    explicit_grants: set[tuple[str, str]] = {
        # (reader_slug, target_owner_slug)
        ("proj-a-alpha", "proj-b-beta"),  # only explicit cross-grant
    }
    for reader in projects:
        status, body = _hub_get(
            hub, f"projects/{reader.project_id}/config", project_id=reader.project_id
        )
        assert status == 200
        for owner in projects:
            if reader.slug == owner.slug:
                continue
            owner_primary = layout.collection_name(owner.kg_collection_name())
            allowed = (reader.slug, owner.slug) in explicit_grants
            present = owner_primary in body["kg_access_list"]
            assert present == allowed, (
                f"leakage check failed: reader={reader.slug} "
                f"owner={owner.slug} collection={owner_primary!r} "
                f"allowed={allowed} present={present}"
            )


def test_single_field_response_matches_full_envelope(
    fixture_result: FixtureResult, hub: HubProc
) -> None:
    """Property (7 — alias surface) — `?key=<field>` shortcut MUST
    return the same value as picking that field out of the full
    envelope. Pins that the single-field path doesn't run a
    different code branch with drift potential."""
    for p in fixture_result.projects:
        _, full = _hub_get(
            hub, f"projects/{p.project_id}/config", project_id=p.project_id
        )
        for key in ("kg_collection", "code_graph_project", "kg_access_list"):
            status, partial = _hub_get(
                hub,
                f"projects/{p.project_id}/config?key={key}",
                project_id=p.project_id,
            )
            assert status == 200, f"key={key} partial returned {status}"
            assert key in partial, f"key={key} missing from partial response"
            assert partial[key] == full[key], (
                f"project {p.slug} key={key}: full vs partial drift\n"
                f"  full:    {full[key]!r}\n"
                f"  partial: {partial[key]!r}"
            )


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="vct_project_config.sh is bash-only; .ps1 sibling has its own gate",
)
def test_resolver_client_script_returns_same_json(
    fixture_result: FixtureResult, hub: HubProc, tmp_path: Path
) -> None:
    """Property (8 surface) — the resolver client script
    ``vct_project_config.sh`` (which is what the hooks invoke) MUST
    produce the same JSON envelope as a direct HTTP GET. This is the
    layer that injects the env vars the pre-edit hook + downstream
    KG search consume.

    Catches:
        * URL or auth-header construction drift
        * JSON field-name divergence between server + client
        * The shell forgetting to forward VCT_HUB_PORT / VCT_STATE_DIR
    """
    if not RESOLVER_CLIENT.exists():
        pytest.skip(f"resolver client not found at {RESOLVER_CLIENT}")

    # v0.2.77 Part 8 (flip): the shell script discovers the hub via
    # $VCT_HUB_PORT + $VCT_STATE_DIR/hub.port, and — crucially — reads the
    # SCOPED token file $VCT_STATE_DIR/hub.token.<project_id> for the
    # per-project /config route (the resolver's own token-preference logic).
    # We DELIBERATELY do NOT set $VCT_HUB_TOKEN: that env pins the GLOBAL
    # token and overrides the scoped-file preference, which post-flip would
    # 403 on /config. Leaving it unset exercises exactly the resolver
    # behaviour a real hook relies on: pick up the scoped token from
    # VCT_STATE_DIR. (The by-path lookup, a non-gated route, still works on
    # the global token the resolver reads from VCT_STATE_DIR/hub.token.)
    env = {
        **os.environ,
        "VCT_HUB_PORT": str(hub.port),
        "VCT_STATE_DIR": str(fixture_result.layout.state_dir),
        "VCO_CI_FIXTURE": "1",
    }
    # Ensure no ambient VCT_HUB_TOKEN leaks in from the parent env.
    env.pop("VCT_HUB_TOKEN", None)
    for p in fixture_result.projects:
        # 1. Resolve project_id by path via the shell script.
        result = subprocess.run(
            ["bash", str(RESOLVER_CLIENT), "resolve-project", str(p.folder_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"resolve-project for {p.slug} failed:\n"
            f"  stdout: {result.stdout!r}\n  stderr: {result.stderr!r}"
        )
        resolved_id = result.stdout.strip()
        assert resolved_id == p.project_id, (
            f"resolve-project: got {resolved_id!r} expected {p.project_id!r}"
        )

        # 2. Fetch full config via the shell script.
        result = subprocess.run(
            ["bash", str(RESOLVER_CLIENT), p.project_id],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"config fetch for {p.slug} failed:\n"
            f"  stdout: {result.stdout!r}\n  stderr: {result.stderr!r}"
        )
        shell_envelope = json.loads(result.stdout)

        # 3. Direct HTTP fetch (scoped token, matching the resolver).
        status, http_envelope = _hub_get(
            hub, f"projects/{p.project_id}/config", project_id=p.project_id
        )
        assert status == 200

        # The two envelopes must agree on every load-bearing field.
        for k in (
            "project_id",
            "project_slug",
            "kg_collection",
            "kg_access_list",
            "codegraph_access_list",
            "code_graph_collection_prefix",
        ):
            assert shell_envelope[k] == http_envelope[k], (
                f"resolver-client drift on {k}: shell={shell_envelope[k]!r} "
                f"http={http_envelope[k]!r}"
            )


# ─── v0.2.77 Part 8 (flip): compat-window case ───────────────────


@pytest.fixture(scope="module")
def compat_hub() -> Iterator[tuple[FixtureResult, HubProc]]:
    """A SEPARATE fixture + hub spawned with
    ``VCT_HUB_LEGACY_GLOBAL_ENV=1`` so the one-release global-token compat
    window is REOPENED on this hub. It uses its own sandbox state-dir (the
    hub's per-state-dir lockfile forbids two live hubs sharing one), so it
    coexists with the module ``hub`` fixture.

    This is the ONE case that proves the deprecation flag stays FUNCTIONAL
    after the flip: the global ``hub.token`` must still authorize the
    per-project routes when an operator opts back in. Every OTHER test in
    this file uses the SCOPED token (the post-flip default reality).
    """
    if os.environ.get("VCO_CI_FIXTURE", "") != "1":
        pytest.skip("Step 22 integration tests require VCO_CI_FIXTURE=1")
    bin_path = _hub_binary_path()
    if not bin_path.exists():
        pytest.skip(f"vct-hub binary not found at {bin_path}")
    # Fixture isolation: this module has TWO module-scoped fixtures that
    # each call build_fixture() — `fixture_result` and this one. With no
    # explicit run_id, build_fixture() derives it from compute_run_id(),
    # which returns $GITHUB_RUN_ID in CI: a CONSTANT for the whole run. So
    # both fixtures would resolve to the same VCT_STATE_DIR / launcher.db,
    # and the second _seed_project_row() would hit
    # `UNIQUE constraint failed: projects.slug`. (Locally GITHUB_RUN_ID is
    # unset, so compute_run_id() returns a fresh random token per call and
    # the collision never surfaces — hence CI-only.) Give this fixture its
    # OWN fully-isolated sandbox by suffixing the base run_id. The suffix
    # stays alphanumeric so it remains a valid sandbox/collection namespace.
    compat_run_id = f"{compute_run_id()}compat"
    fx = build_fixture(run_id=compat_run_id)
    proc = None
    try:
        proc = start_hub(
            fx.layout,
            bin_path,
            extra_env={"VCT_HUB_LEGACY_GLOBAL_ENV": "1"},
        )
        yield fx, proc
    finally:
        if proc is not None:
            proc.stop()
        notes = teardown_sandbox(fx.layout, drop_weaviate_collections=True)
        for n in notes:
            print(n, file=sys.stderr)


def test_global_token_still_allowed_under_compat_flag(
    compat_hub: tuple[FixtureResult, HubProc],
) -> None:
    """Deprecation-allow: with ``VCT_HUB_LEGACY_GLOBAL_ENV=1`` set on the
    hub, the GLOBAL ``hub.token`` still authorizes ``/config`` (the
    one-release compat window). This must remain green after the flip —
    the flag is the operator's migration escape hatch.
    """
    fx, chub = compat_hub
    p = fx.projects[0]
    # Present the GLOBAL token (chub.token) — NOT the scoped one — on the
    # per-project route. Under the compat flag this is accepted (200).
    url = f"{chub.base_url()}/projects/{p.project_id}/config"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {chub.token}"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200, "global token must be accepted under the compat flag"
        body = json.loads(resp.read())
    assert body["project_id"] == p.project_id

    # Sanity: the SCOPED token also works on the compat hub (the flag only
    # RE-ADMITS the global token; it never disables the scoped path).
    status, scoped_body = _hub_get(
        chub, f"projects/{p.project_id}/config", project_id=p.project_id
    )
    assert status == 200
    assert scoped_body["project_id"] == p.project_id

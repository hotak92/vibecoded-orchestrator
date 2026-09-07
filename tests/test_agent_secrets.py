# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for vco_lib.agent_secrets (v0.2.54 S-7).

Two tiers:

* **Offline tests** — file-store fallback semantics with the hub
  unreachable (``VCT_STATE_DIR`` pointed at an empty tmp dir so no
  ``hub.token`` exists). No mocks of our own code; the real resolution
  chain runs.
* **Live hub tests** — auto-skip when no vct-hub is running. They
  exercise a real HTTP round-trip (discovery → bearer auth → 404
  envelope parsing) using an UNREGISTERED tmp path, so they are
  machine-agnostic and leak nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.common.child_env import child_env  # noqa: E402
from vco_lib import agent_secrets  # noqa: E402
from vco_lib import project_config  # noqa: E402
from vco_lib.agent_secrets import (  # noqa: E402
    AccessDenied,
    Forbidden,
    HubUnreachable,
    ProjectNotFound,
    SecretNotFound,
    exec_with_secrets,
    get,
)


# ─── Helpers ────────────────────────────────────────────────────────────


@pytest.fixture()
def offline_hub(monkeypatch, tmp_path):
    """Point hub discovery at an empty state dir → HubUnreachable."""
    monkeypatch.delenv("VCT_HUB_TOKEN", raising=False)
    monkeypatch.delenv("VCT_HUB_PORT", raising=False)
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "empty-state"))
    project_config._test_clear_cache()
    yield
    project_config._test_clear_cache()


@pytest.fixture()
def file_store(monkeypatch, tmp_path):
    """Seed an isolated file store; never touches ~/.vct-secrets."""
    root = tmp_path / "store"
    (root / "shared").mkdir(parents=True)
    (root / "projects" / "demo").mkdir(parents=True)
    (root / "shared" / "github_pat").write_text("shared-token-value\n")
    (root / "projects" / "demo" / "github_pat").write_text("demo-token-value")
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))
    return root


def _hub_reachable() -> bool:
    try:
        port, _token = project_config._discover_hub()
    except Exception:
        return False
    try:
        import requests

        # Live-verified 2026-06-11: the no-auth liveness route is
        # /api/v1/health (bare /health returns 401 from the auth layer).
        r = requests.get(f"http://127.0.0.1:{port}/api/v1/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


live_hub = pytest.mark.skipif(
    not _hub_reachable(), reason="vct-hub not running — live tier skipped"
)


# ─── Offline: file-store fallback ───────────────────────────────────────


def test_offline_falls_back_to_shared(offline_hub, file_store):
    assert get("github_pat") == "shared-token-value"  # newline stripped


def test_offline_project_name_overrides_shared(offline_hub, file_store):
    assert get("github_pat", project="demo") == "demo-token-value"


def test_offline_vct_project_marker_detected(offline_hub, file_store, tmp_path, monkeypatch):
    proj_dir = tmp_path / "checkout" / "sub"
    proj_dir.mkdir(parents=True)
    (tmp_path / "checkout" / ".vct-project").write_text("demo\n")
    monkeypatch.chdir(proj_dir)
    assert get("github_pat") == "demo-token-value"


def test_offline_missing_key_raises_secret_not_found(offline_hub, file_store):
    with pytest.raises(SecretNotFound) as exc_info:
        get("definitely_absent_key")
    # Error must point at remediation, not just fail.
    assert "vct set" in str(exc_info.value)


def test_offline_fallback_disabled_raises_hub_error(offline_hub, file_store):
    with pytest.raises((HubUnreachable, ProjectNotFound)):
        get("github_pat", allow_file_fallback=False)


def test_offline_value_never_in_exception(offline_hub, file_store):
    try:
        get("definitely_absent_key")
    except SecretNotFound as e:
        assert "shared-token-value" not in str(e)
        assert "demo-token-value" not in str(e)


# ─── Offline: exec_with_secrets ─────────────────────────────────────────


def test_exec_injects_env_not_argv(offline_hub, file_store):
    cp = exec_with_secrets(
        [sys.executable, "-c", "import os; print(os.environ['GH_TOKEN'])"],
        secrets={"github_pat": "GH_TOKEN"},
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0
    assert cp.stdout.strip() == "shared-token-value"
    # Parent env untouched.
    assert "GH_TOKEN" not in os.environ


def test_exec_fails_fast_before_running_child(offline_hub, file_store, tmp_path):
    sentinel = tmp_path / "child-ran"
    with pytest.raises(SecretNotFound):
        exec_with_secrets(
            [sys.executable, "-c", f"open({str(sentinel)!r}, 'w').close()"],
            secrets={"absent_key": "X"},
        )
    assert not sentinel.exists(), "child executed despite missing secret"


def test_exec_refuses_shell_true(offline_hub, file_store):
    with pytest.raises(ValueError, match="shell"):
        exec_with_secrets(
            ["true"], secrets={"github_pat": "T"}, shell=True
        )


def test_exec_rejects_bad_env_var_name(offline_hub, file_store):
    with pytest.raises(ValueError, match="env var"):
        exec_with_secrets(["true"], secrets={"github_pat": "BAD NAME"})


# ─── Live hub tier (auto-skip when hub down) ────────────────────────────


@live_hub
def test_live_unregistered_path_raises_project_not_found(tmp_path, monkeypatch):
    """Real HTTP round-trip: by-path lookup of an unregistered tmp dir
    must come back as ProjectNotFound (parsed from the hub's 404 JSON
    envelope), not as a generic error."""
    monkeypatch.delenv("VCT_SECRETS_DIR", raising=False)
    project_config._test_clear_cache()
    with pytest.raises(ProjectNotFound):
        get("github_pat", project=str(tmp_path), allow_file_fallback=False)


@live_hub
def test_live_hub_miss_still_falls_back_to_file_store(tmp_path, monkeypatch):
    """Hub reachable but project unregistered → file store answers."""
    root = tmp_path / "store"
    (root / "shared").mkdir(parents=True)
    (root / "shared" / "some_ci_key").write_text("from-file-store")
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))
    project_config._test_clear_cache()
    assert (
        get("some_ci_key", project=str(tmp_path)) == "from-file-store"
    )


def test_key_not_active_falls_back_to_file_store(tmp_path, monkeypatch):
    """Hub `key_not_active` conflates "paused" with "never declared"
    (live-verified 2026-06-11), so the file store must still answer —
    otherwise every user-managed key is stranded while the launcher
    runs."""
    root = tmp_path / "store"
    (root / "shared").mkdir(parents=True)
    (root / "shared" / "gated_key").write_text("file-copy")
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))

    def fake_hub_get(key, project):
        raise AccessDenied(f"key {key!r} not active for project x")

    monkeypatch.setattr(agent_secrets, "_hub_get", fake_hub_get)
    assert get("gated_key", project="anything") == "file-copy"


def test_access_denied_surfaces_when_file_store_misses(tmp_path, monkeypatch):
    """When the hub gates the key AND no file copy exists, the caller
    gets the informative AccessDenied (not a generic SecretNotFound)."""
    root = tmp_path / "store"
    (root / "shared").mkdir(parents=True)
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))

    def fake_hub_get(key, project):
        raise AccessDenied(f"key {key!r} not active for project x")

    monkeypatch.setattr(agent_secrets, "_hub_get", fake_hub_get)
    with pytest.raises(AccessDenied):
        get("gated_key", project="anything")


# ─── v0.2.77 L3-F3: 403 forbidden classification ────────────────────────


def test_forbidden_falls_back_to_file_store(tmp_path, monkeypatch):
    """A hub 403 (Forbidden) must NOT hard-fail: the file store is a
    legitimate independent secrets tier, so a keychain-route refusal should
    still resolve a file-store copy."""
    root = tmp_path / "store"
    (root / "shared").mkdir(parents=True)
    (root / "shared" / "gh_pat").write_text("file-copy")
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))

    def fake_hub_get(key, project):
        raise Forbidden(f"hub returned 403 forbidden for {key!r}")

    monkeypatch.setattr(agent_secrets, "_hub_get", fake_hub_get)
    assert get("gh_pat", project="anything") == "file-copy"


def test_forbidden_propagates_when_fallback_disabled(tmp_path, monkeypatch):
    """With fallback disabled, a 403 surfaces as the distinct Forbidden type
    (NOT HubUnreachable) so the diagnostic is honest."""
    monkeypatch.setenv("VCT_SECRETS_DIR", str(tmp_path / "empty"))

    def fake_hub_get(key, project):
        raise Forbidden("hub returned 403 forbidden")

    monkeypatch.setattr(agent_secrets, "_hub_get", fake_hub_get)
    with pytest.raises(Forbidden):
        get("gh_pat", project="anything", allow_file_fallback=False)


def test_forbidden_is_not_hub_unreachable():
    """Forbidden must not be a HubUnreachable subclass — callers that catch
    HubUnreachable for env-fallback must NOT swallow a 403."""
    assert not issubclass(Forbidden, HubUnreachable)


def test_forbidden_message_names_scoped_token(tmp_path, monkeypatch):
    """The Forbidden surfaced on an all-miss carries the scoped-token
    remediation, not a 'hub unreachable' mislabel."""
    monkeypatch.setenv("VCT_SECRETS_DIR", str(tmp_path / "empty"))

    def fake_hub_get(key, project):
        raise Forbidden(
            "hub returned 403 forbidden; present the scoped hub.token.<id>"
        )

    monkeypatch.setattr(agent_secrets, "_hub_get", fake_hub_get)
    with pytest.raises(SecretNotFound) as ei:
        get("gh_pat", project="anything")
    msg = str(ei.value).lower()
    assert "403" in msg or "forbidden" in msg
    assert "unreachable" not in msg.split("tier 1 hub:")[0]


# ─── Module probe never prints values ───────────────────────────────────


def test_cli_probe_never_prints_value(offline_hub, file_store):
    env = dict(os.environ)
    cp = subprocess.run(
        [sys.executable, "-m", "vco_lib.agent_secrets", "github_pat"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=child_env(env),
    )
    combined = cp.stdout + cp.stderr
    assert "shared-token-value" not in combined
    assert "github_pat" in combined


# ─── v0.2.73 tier 3: the project's own .env (read-only, lowest) ─────────
#
# Synthetic fixture shared with the sh + ps1 siblings
# (tests/test_vct_secrets_resolve.sh, tests/test_vct_secrets_resolve_ps1.py)
# — same key names, same values, same parse cases. Keep the three in
# lockstep (the resolvers carry "must match" headers).

_DOTENV_FIXTURE = (
    "# comment line — skipped\n"
    "export EXPORTED_KEY=plain-exported\n"
    'QUOTED_KEY="double quoted value"\n'
    "SINGLE_KEY='single quoted value'\n"
    "FIRST_MATCH=first-wins\n"
    "FIRST_MATCH=second-loses\n"
    "NO_EXPANSION=$HOME/literal\n"
    "MISMATCHED='half\"\n"
)


@pytest.fixture()
def empty_file_store(monkeypatch, tmp_path):
    """Isolated EMPTY file store so tier 2 always misses."""
    root = tmp_path / "empty-store"
    (root / "shared").mkdir(parents=True)
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))
    return root


@pytest.mark.parametrize(
    ("key", "want"),
    [
        ("EXPORTED_KEY", "plain-exported"),
        ("QUOTED_KEY", "double quoted value"),
        ("SINGLE_KEY", "single quoted value"),
        ("FIRST_MATCH", "first-wins"),
        ("NO_EXPANSION", "$HOME/literal"),  # NO variable expansion
        ("MISMATCHED", "'half\""),  # mismatched quotes NOT stripped
    ],
)
def test_dotenv_parse_cases(key, want):
    """Tier-3 parsing rule, unit level (identical ×3 across sh/ps1/py)."""
    assert agent_secrets._parse_dotenv_value(_DOTENV_FIXTURE, key) == want


def test_dotenv_parse_absent_key_returns_none():
    assert agent_secrets._parse_dotenv_value(_DOTENV_FIXTURE, "ABSENT") is None


def test_dotenv_resolves_when_hub_and_store_miss(
    offline_hub, empty_file_store, tmp_path
):
    proj = tmp_path / "proj-with-dotenv"
    proj.mkdir()
    (proj / ".env").write_text(_DOTENV_FIXTURE, encoding="utf-8")
    assert get("EXPORTED_KEY", project=str(proj)) == "plain-exported"


def test_dotenv_cwd_used_when_project_is_none(
    offline_hub, empty_file_store, tmp_path, monkeypatch
):
    proj = tmp_path / "cwd-proj"
    proj.mkdir()
    (proj / ".env").write_text(_DOTENV_FIXTURE, encoding="utf-8")
    monkeypatch.chdir(proj)
    assert get("QUOTED_KEY") == "double quoted value"


def test_dotenv_file_store_beats_dotenv(offline_hub, file_store, tmp_path):
    """Tier order: the managed file store (tier 2) wins over the ambient
    .env (tier 3) — a user migrating a key into the managed store gets
    the managed copy without deleting their .env line."""
    proj = tmp_path / "precedence-proj"
    proj.mkdir()
    (proj / ".env").write_text("github_pat=dotenv-should-lose\n", encoding="utf-8")
    # `file_store` seeds shared/github_pat = "shared-token-value".
    assert get("github_pat", project=str(proj)) == "shared-token-value"


def test_dotenv_skipped_when_fallback_disabled(
    offline_hub, empty_file_store, tmp_path
):
    """allow_file_fallback=False gates BOTH tier 2 and tier 3."""
    proj = tmp_path / "gated-proj"
    proj.mkdir()
    (proj / ".env").write_text(_DOTENV_FIXTURE, encoding="utf-8")
    with pytest.raises((HubUnreachable, ProjectNotFound)):
        get("EXPORTED_KEY", project=str(proj), allow_file_fallback=False)


def test_dotenv_key_not_active_falls_to_dotenv(tmp_path, monkeypatch):
    """key_not_active falls through tier 2 (empty) to tier 3."""
    root = tmp_path / "store"
    (root / "shared").mkdir(parents=True)
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))
    proj = tmp_path / "kna-proj"
    proj.mkdir()
    (proj / ".env").write_text("GATED_KEY=dotenv-answers\n", encoding="utf-8")

    def fake_hub_get(key, project):
        raise AccessDenied(f"key {key!r} not active for project x")

    monkeypatch.setattr(agent_secrets, "_hub_get", fake_hub_get)
    assert get("GATED_KEY", project=str(proj)) == "dotenv-answers"


def test_dotenv_values_never_in_exception(
    offline_hub, empty_file_store, tmp_path
):
    """Errors name keys + tiers, never values — including tier-3 ones."""
    proj = tmp_path / "leak-proj"
    proj.mkdir()
    (proj / ".env").write_text(_DOTENV_FIXTURE, encoding="utf-8")
    with pytest.raises(SecretNotFound) as exc_info:
        get("TOTALLY_MISSING_KEY", project=str(proj))
    msg = str(exc_info.value)
    for leaked in (
        "plain-exported",
        "double quoted value",
        "single quoted value",
        "first-wins",
    ):
        assert leaked not in msg
    # The message names the tiers consulted.
    assert "tier 3" in msg and ".env" in msg


# ─── Tier 2: the `.no-shared-fallback` per-project opt-out ──────────────
#
# `docs/VCT_SECRETS_PRIMITIVE.md` §"Design choices" promises a project can
# opt out of the SHARED file-store tier by placing
# `projects/<NAME>/.no-shared-fallback`, and the launcher's
# "Disable shared secrets for this project" toggle WRITES that marker
# (`secrets_cmd.rs::set_shared_secrets_read_disabled`). Until v0.3.0 no
# resolver READ it, so the toggle gated tier 1 (keychain) while tier 2 kept
# serving the very values the user had opted out of — a privacy control
# that was documented, written, and inert.
#
# These exercise the production entry point `get()`, not the private
# predicate: a test that called `_shared_fallback_disabled` directly would
# still pass if `_file_store_get` stopped consulting it.


@pytest.fixture()
def opted_out_store(monkeypatch, tmp_path):
    """File store where project `demo` has opted out of `shared/`.

    `shared/` holds a key `demo` does NOT have its own copy of, so a
    resolution that reaches shared is unambiguous evidence the gate did
    not fire.
    """
    root = tmp_path / "store"
    (root / "shared").mkdir(parents=True)
    (root / "projects" / "demo").mkdir(parents=True)
    (root / "projects" / "other").mkdir(parents=True)
    (root / "shared" / "shared_only_key").write_text("shared-only-value\n")
    (root / "projects" / "demo" / "own_key").write_text("demo-own-value")
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))
    return root


def _opt_out(root: Path, name: str) -> None:
    (root / "projects" / name / agent_secrets.NO_SHARED_FALLBACK_MARKER).write_text("")


def test_marker_absent_shared_still_resolves(offline_hub, opted_out_store):
    """LEAVE-ALONE half — without the marker nothing changes."""
    assert get("shared_only_key", project="demo") == "shared-only-value"


def test_marker_blocks_the_shared_tier(offline_hub, opted_out_store):
    """ACT half — with the marker the shared copy must NOT resolve."""
    _opt_out(opted_out_store, "demo")
    with pytest.raises(SecretNotFound):
        get("shared_only_key", project="demo")


def test_marker_leaves_the_projects_own_keys_alone(offline_hub, opted_out_store):
    """The opt-out is about `shared/`, not about the project's own tier."""
    _opt_out(opted_out_store, "demo")
    assert get("own_key", project="demo") == "demo-own-value"


def test_marker_is_scoped_to_the_project_that_placed_it(
    offline_hub, opted_out_store
):
    """`other` must keep resolving shared even though `demo` opted out."""
    _opt_out(opted_out_store, "demo")
    assert get("shared_only_key", project="other") == "shared-only-value"


def test_marker_applies_via_the_vct_project_marker_path(
    offline_hub, opted_out_store, tmp_path, monkeypatch
):
    """Name detection by `.vct-project` walk-up honours the opt-out too.

    The launcher writes the marker under the project NAME; a caller
    passing a PATH must reach the same decision, or the gate would hold
    for `get(project="demo")` and leak for a hook running inside the
    checkout.
    """
    _opt_out(opted_out_store, "demo")
    checkout = tmp_path / "checkout"
    (checkout / "sub").mkdir(parents=True)
    (checkout / ".vct-project").write_text("demo\n")
    monkeypatch.chdir(checkout / "sub")
    with pytest.raises(SecretNotFound):
        get("shared_only_key")


def test_no_project_identity_cannot_opt_out(
    offline_hub, opted_out_store, tmp_path, monkeypatch
):
    """No name → no marker to consult → shared resolves, as documented."""
    _opt_out(opted_out_store, "demo")
    nowhere = tmp_path / "unmarked"
    nowhere.mkdir()
    monkeypatch.chdir(nowhere)
    assert get("shared_only_key") == "shared-only-value"


def test_shared_pseudo_name_never_opts_itself_out(
    offline_hub, opted_out_store
):
    """A stray `projects/shared/` orphan must not kill every shared read.

    `vct get` defaults to `project="shared"` when `--project` is omitted,
    so a marker there would disable the shared tier machine-wide.
    """
    (opted_out_store / "projects" / "shared").mkdir(parents=True, exist_ok=True)
    _opt_out(opted_out_store, "shared")
    assert get("shared_only_key", project="shared") == "shared-only-value"


def test_opt_out_also_gates_exec_injection(
    offline_hub, opted_out_store, tmp_path
):
    """The gate must hold on the injection path, not just plain `get`.

    `exec_with_secrets` is the PREFERRED consumer API (CLAUDE.md §Secrets),
    so a gate that only covered `get` would leave the recommended path open.
    """
    _opt_out(opted_out_store, "demo")
    with pytest.raises(SecretNotFound):
        exec_with_secrets(
            [sys.executable, "-c", "pass"],
            secrets={"shared_only_key": "SHARED_ONLY"},
            project="demo",
        )


def test_opt_out_error_never_names_the_value(offline_hub, opted_out_store):
    """The refusal must not leak what it refused to serve."""
    _opt_out(opted_out_store, "demo")
    try:
        get("shared_only_key", project="demo")
    except SecretNotFound as e:
        assert "shared-only-value" not in str(e)


def test_pseudo_name_exclusion_is_case_sensitive(offline_hub, opted_out_store):
    """A project literally NAMED "Shared" is a real project, not the scope.

    Sibling of `test_ps1_pseudo_name_exclusion_is_case_sensitive` — the
    PowerShell resolver needs `-ceq` to reach this same verdict.
    """
    (opted_out_store / "projects" / "Shared").mkdir(parents=True, exist_ok=True)
    _opt_out(opted_out_store, "Shared")
    with pytest.raises(SecretNotFound):
        get("shared_only_key", project="Shared")

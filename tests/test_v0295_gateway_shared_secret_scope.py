# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The gateway's vendor key is SHARED by default; a project may override it.

Owner ruling 2026-09-17: *"gateway secrets should be shared in the VCO system,
but user can override per-project."*

The ground truth this suite encodes, verified in source rather than assumed:

* **Both stores already resolve project-first-then-shared.** The hub's
  ``/api/v1/projects/{id}/env`` walks per-project → shared → global, first
  wins, each bucket gated on the ASKING project as requester
  (``modules_api.rs::project_env``, loop 4); the file store tries
  ``projects/<NAME>/<key>`` then ``shared/<key>``
  (``agent_secrets._file_store_get``). So the ruling needs NO new mechanism.
* **What it does need is a registered asker.** Shared keychain secrets live in
  their own bucket (``_user_shared_``), owned by no project, and there is no
  hub route that serves them without a project id. A daemon with no scope was
  falling back to ``Path.cwd()`` — the state root for a boot unit — which is
  not registered, so tier 1 was skipped entirely and every shared key the user
  saved in the launcher was invisible (the 2026-09-10 503 ``no key found for
  vendor 'zai'``).

Hence: the DEFAULT scope is this install's orchestrator root, resolved at
RUNTIME (never baked into a unit file, which is the defect the hand-written
systemd drop-in embodies), and ``VCT_MODEL_GATEWAY_SECRET_PROJECT`` is the one
override — the same env var as before, doing the same thing.

The end-to-end class drives the REAL ``vco_lib.agent_secrets`` chain against a
tmp file store with the hub unreachable: no mock of our own resolution order,
so these tests would fail if the tier order ever changed underneath the
gateway.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))

from model_router.secrets import SCOPE_ENV, VendorKeyResolver  # noqa: E402
from model_router.vendors import Vendor  # noqa: E402
from vco_lib import project_config  # noqa: E402

VENDOR = Vendor(
    vendor_id="testvendor",
    display_suffix=" (Test)",
    namespace="claude-gw/",
    upstream="https://example.invalid",
    secret_keys=("test_api_key",),
    bare_id_prefixes=("test-",),
)

#: Distinct per store slot, so an assertion names WHICH slot answered rather
#: than merely that something did.
SHARED_VALUE = "value-from-shared"
PROJECT_VALUE = "value-from-project"


class _Spy:
    """A getter that records the ``project=`` it was handed. Never a value."""

    def __init__(self, value: str = "k") -> None:
        self.scopes: list[str | None] = []
        self._value = value

    def __call__(self, key, project=None):
        self.scopes.append(project)
        return self._value


# ─── Which scope the daemon asks in ─────────────────────────────────────


def test_the_default_scope_is_the_install_root_not_the_cwd():
    """The fix itself. Pre-v0.2.95 this was ``Path.cwd()``, which for a boot
    unit is the state root — unregistered, so tier 1 never ran."""
    resolver = VendorKeyResolver(install_root=lambda: "/opt/vco-clone")
    assert resolver.effective_project == "/opt/vco-clone"
    assert resolver.scope_origin() == "install_root"


def test_the_default_scope_reaches_the_secrets_chain():
    """Behavioural, not a property read: the chain must be ASKED in that scope.

    ``_resolve_uncached`` used to pass the raw pin (``None`` by default), which
    made the property above true and the resolution wrong.
    """
    spy = _Spy()
    VendorKeyResolver(install_root=lambda: "/opt/vco-clone", getter=spy).resolve(VENDOR)
    assert spy.scopes == ["/opt/vco-clone"]


def test_a_pin_overrides_the_install_root_default():
    spy = _Spy()
    resolver = VendorKeyResolver(
        project="/home/u/work/acme", install_root=lambda: "/opt/vco-clone", getter=spy,
    )
    resolver.resolve(VENDOR)
    assert resolver.effective_project == "/home/u/work/acme"
    assert resolver.scope_origin() == "pin"
    assert spy.scopes == ["/home/u/work/acme"]


def test_the_cwd_is_the_last_resort_when_no_clone_resolves():
    """A machine with no orchestrator clone keeps the pre-v0.2.95 behaviour —
    and SAYS so, rather than pretending to a scope it does not have."""
    resolver = VendorKeyResolver(install_root=lambda: None)
    assert resolver.effective_project == str(Path.cwd())
    assert resolver.scope_origin() == "cwd"
    assert SCOPE_ENV in resolver.describe_scope()


def test_a_broken_install_root_resolver_is_an_answer_not_a_crash():
    """The default must never be able to take the daemon down: a vendor key is
    not worth a process."""

    def boom():
        raise RuntimeError("no clone here")

    resolver = VendorKeyResolver(install_root=boom)
    with pytest.raises(RuntimeError):
        resolver.effective_project  # the injected resolver is the caller's own
    # ...but the SHIPPED default swallows it — that is where the guarantee is.
    from model_router import secrets as mod

    assert mod._default_install_root() is None or isinstance(
        mod._default_install_root(), str
    )


def test_the_install_root_is_resolved_once_per_process():
    """``/health`` calls ``scope_status`` on every probe; the default must not
    re-stat the filesystem each time."""
    calls = []

    def once():
        calls.append(1)
        return "/opt/vco-clone"

    resolver = VendorKeyResolver(install_root=once, getter=_Spy())
    for _ in range(5):
        resolver.effective_project
        resolver.scope_status()
    assert len(calls) == 1


# ─── End to end, through the real resolution chain ──────────────────────


@pytest.fixture()
def offline_hub(monkeypatch, tmp_path):
    """Hub discovery pointed at an empty state dir → tier 1 unreachable.

    This is also the "hub down" arm: everything below resolves with NO hub.
    """
    monkeypatch.delenv("VCT_HUB_TOKEN", raising=False)
    monkeypatch.delenv("VCT_HUB_PORT", raising=False)
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "empty-state"))
    project_config._test_clear_cache()
    yield
    project_config._test_clear_cache()


@pytest.fixture()
def store(monkeypatch, tmp_path):
    """An isolated file store plus two candidate scopes on disk.

    ``clone/`` carries no ``.vct-project`` marker — like an orchestrator root
    that owns no per-project file-store bucket — so it resolves SHARED.
    ``acme/`` carries one, so it resolves ``projects/acme/`` first.
    """
    root = tmp_path / "secrets"
    (root / "shared").mkdir(parents=True)
    (root / "projects" / "acme").mkdir(parents=True)
    monkeypatch.setenv("VCT_SECRETS_DIR", str(root))

    clone = tmp_path / "clone"
    clone.mkdir()
    acme = tmp_path / "acme"
    acme.mkdir()
    (acme / ".vct-project").write_text("acme\n")
    return root, clone, acme


def _resolver(clone, project=None):
    """A resolver wired exactly as the daemon wires one, minus the hub probe."""
    return VendorKeyResolver(project=project, install_root=lambda: str(clone))


def test_shared_key_resolves_through_the_default_scope(offline_hub, store):
    root, clone, _ = store
    (root / "shared" / "test_api_key").write_text(SHARED_VALUE + "\n")

    result = _resolver(clone).resolve(VENDOR)

    assert result.key == SHARED_VALUE
    assert result.resolved_from == "test_api_key"


def test_a_projects_own_key_wins_over_the_shared_one(offline_hub, store):
    root, clone, acme = store
    (root / "shared" / "test_api_key").write_text(SHARED_VALUE)
    (root / "projects" / "acme" / "test_api_key").write_text(PROJECT_VALUE)

    assert _resolver(clone).resolve(VENDOR).key == SHARED_VALUE
    assert _resolver(clone, project=str(acme)).resolve(VENDOR).key == PROJECT_VALUE


def test_no_shared_fallback_marker_keeps_the_shared_key_out(offline_hub, store):
    """The launcher's per-project "disable shared secrets" toggle is honoured
    by the gateway because the gateway asks through the SAME chain — there is
    no second opt-out to keep in step."""
    root, clone, acme = store
    (root / "shared" / "test_api_key").write_text(SHARED_VALUE)
    (root / "projects" / "acme" / ".no-shared-fallback").write_text("")

    result = _resolver(clone, project=str(acme)).resolve(VENDOR)

    assert result.key is None
    assert result.state == "missing"
    # And the shared value is not leaked into the diagnosis.
    assert SHARED_VALUE not in (result.problem or "")


def test_a_full_miss_names_both_shared_write_paths_and_the_override(
    offline_hub, store,
):
    """"No key found" is not an answer a user can act on; WHERE to put one is.

    Both named destinations write the SHARED scope — the launcher's Secrets
    panel (OS keychain) and ``vct set --shared`` (file store). The per-project
    override is named too, because a user who wants one key per project has to
    be told which knob does that.
    """
    _root, clone, _acme = store

    problem = _resolver(clone).resolve(VENDOR).problem or ""
    # Assert on the GATEWAY-authored half only. Everything after "Resolver
    # detail:" is relayed verbatim from `agent_secrets`, whose own miss
    # message already names `vct set --shared` — so a whole-string assertion
    # passes even with the gateway's copy deleted. (Found by red-proof: the
    # first version of this test did exactly that.)
    authored, _, relayed = problem.partition("Resolver detail:")
    assert relayed, "the resolver detail must still be relayed"

    assert "test_api_key" in authored
    assert "Secrets panel" in authored
    assert "vct set --shared --key test_api_key" in authored
    assert "~/.vct-secrets/shared/" in authored
    assert SCOPE_ENV in authored
    assert str(clone) in authored  # the scope actually consulted, named


def test_the_miss_never_carries_a_value(offline_hub, store):
    root, clone, _ = store
    (root / "projects" / "acme" / "test_api_key").write_text(PROJECT_VALUE)

    problem = _resolver(clone).resolve(VENDOR).problem or ""

    assert PROJECT_VALUE not in problem


# ─── Loud, in the place a user looks ────────────────────────────────────


def test_an_unresolvable_scope_is_logged_as_a_warning_with_the_remedy(caplog):
    """Before this, the startup probe stored its verdict and wrote NOTHING:
    the first evidence of a keyless daemon was a 503 inside Claude Code, with
    no trace in the gateway's own log."""

    def prober(_arg):
        raise LookupError("no project registered at path")

    resolver = VendorKeyResolver(
        install_root=lambda: "/opt/vco-clone", scope_prober=prober, getter=_Spy(),
    )
    with caplog.at_level(logging.WARNING, logger="model_router.secrets"):
        resolver.probe_scope()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "/opt/vco-clone" in text
    assert SCOPE_ENV in text
    assert "keychain" in text


def test_a_stale_pin_is_told_which_value_would_work():
    """A pin baked by an earlier registration outlives a moved install root —
    the same shape as the drop-in this release retires. The daemon may not
    overrule an explicit pin, but it can name the path it is running from."""

    def prober(_arg):
        raise LookupError("no project registered at path")

    status = VendorKeyResolver(
        project="/opt/old-clone",
        install_root=lambda: "/opt/new-clone",
        scope_prober=prober,
        getter=_Spy(),
    ).probe_scope()

    assert "/opt/new-clone" in (status.reason or "")


def test_a_pin_that_matches_the_install_root_gets_no_stale_hint():
    """The leave-alone half: a pin naming this very clone is not stale, and
    telling the user to 'clear the stale pin' would be advice to break it."""

    def prober(_arg):
        raise LookupError("hub down")

    status = VendorKeyResolver(
        project="/opt/clone",
        install_root=lambda: "/opt/clone",
        scope_prober=prober,
        getter=_Spy(),
    ).probe_scope()

    assert "stale" not in (status.reason or "")


def test_a_resolvable_scope_is_stated_once_not_on_every_probe(caplog):
    clock = [0.0]
    resolver = VendorKeyResolver(
        install_root=lambda: "/opt/vco-clone",
        scope_prober=lambda arg: "project-uuid-1",
        getter=_Spy(),
        clock=lambda: clock[0],
        ttl_s=10,
    )
    with caplog.at_level(logging.INFO, logger="model_router.secrets"):
        resolver.probe_scope()
        clock[0] = 1000.0  # past the TTL — the probe runs again
        resolver.probe_scope()

    stated = [r for r in caplog.records if "/opt/vco-clone" in r.getMessage()]
    assert len(stated) == 1, "a stable verdict must be stated once, not per probe"


def test_a_verdict_that_changes_is_stated_again(caplog):
    """A hub that comes up later flips the verdict without a restart, and that
    flip is exactly the line a user waiting for the gateway wants to see."""
    clock = [0.0]
    answers = [LookupError("hub unreachable"), "project-uuid-1"]

    def prober(_arg):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    resolver = VendorKeyResolver(
        install_root=lambda: "/opt/vco-clone",
        scope_prober=prober,
        getter=_Spy(),
        clock=lambda: clock[0],
        ttl_s=10,
    )
    with caplog.at_level(logging.INFO, logger="model_router.secrets"):
        resolver.probe_scope()
        clock[0] = 1000.0
        resolver.probe_scope()

    levels = [
        r.levelno for r in caplog.records if "/opt/vco-clone" in r.getMessage()
    ]
    assert levels == [logging.WARNING, logging.INFO]

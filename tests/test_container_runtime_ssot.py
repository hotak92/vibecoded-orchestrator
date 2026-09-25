# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 duplication-merge (PLAN-EXTENSION §3.5 / R13) — ONE Python home
for container-runtime + compose resolution, pinned to the shared parity
fixture the Rust parity test still reads (through the decide client — the Rust
mirror itself was retired in v0.2.97 R12).

Before the merge FIVE detectors answered "which runtime, which compose?"
(install.py ×4 helpers, ``vco_lib.containers._resolve_runtime``, three hook
pairs) and their compose preference orders had drifted four ways. Now:

* :func:`vco_lib.containers.resolve` is the decision; install.py's helpers
  are thin calls into it; the hooks run ``python -m vco_lib.containers
  resolve --json``.
* ``tests/fixtures/container_runtime_parity.json`` describes hosts and the
  expected decision; this file drives ``resolve`` through injected probes
  (no podman/docker needed) and ``runtime.rs``'s parity test drives the
  same scenarios through the Python client (``runtime_verdict::decide`` —
  v0.2.97 retired the Rust ``candidate_order``/``select_runtime`` mirror)
  via the SAME file.

Red-proofed against the pre-merge tree (``/tmp/merge-lane/pre/3.5/``):
scenario ``both_compose_forms_prefer_subcommand`` returned ``podman-compose``
from the old ``install._get_compose_command`` (standalone-first) and
``podman compose`` from the merged one — the divergence the fixture locks.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.common.child_env import child_env  # noqa: E402
from vco_lib import containers  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "container_runtime_parity.json"
SCENARIOS = json.loads(FIXTURE.read_text(encoding="utf-8"))["scenarios"]


class _Result:
    def __init__(self, rc: int, stdout: str = "") -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = ""


def _probes(sc: dict):
    """Build ``which`` / ``run`` doubles for one fixture scenario.

    Besides the version / info / compose probes, serves the READ-ONLY record
    reconcile's container / volume listings when the scenario models them
    (``vco_data_under`` / ``vco_data_unlistable``, v0.2.97 R8 follow-up): a
    runtime listed as holding VCO's data lists ``vco_*`` names, one that does
    not lists a foreign container, and an unlistable runtime's listing exits
    non-zero. ``vco_data_kind`` (R11 L2) says WHAT it holds: a ``running``
    VCO container (``ps`` lists it — the default for ``vco_data_under``), a
    ``stopped`` one (``ps -a`` only) or only ``volumes``."""
    on_path = set(sc["on_path"]) | set(sc["standalone_on_path"])
    data = sc.get("vco_data_under") or {}
    kinds = sc.get("vco_data_kind") or {}
    unlistable = set(sc.get("vco_data_unlistable") or [])

    def kind(rt: str) -> str:
        return kinds.get(rt) or ("running" if data.get(rt) else "none")

    def which(name: str) -> Optional[str]:
        return f"/usr/bin/{name}" if name in on_path else None

    def run(argv, **_kw):
        rt, sub = argv[0], argv[1:]
        if rt in sc["probe_unknown"]:
            raise subprocess.TimeoutExpired(argv, 1)
        if sub == ["version"]:
            return _Result(0 if rt in sc["version_ok"] else 1)
        if sub == ["info"]:
            return _Result(0 if rt in sc["daemon_ok"] else 1)
        if sub == ["compose", "version"]:
            return _Result(0 if rt in sc["compose_subcommand_ok"] else 1)
        if sub[:2] == ["ps", "-a"]:
            if rt in unlistable:
                return _Result(1)
            return _Result(0, "vco_weaviate\n" if kind(rt) in ("running", "stopped")
                           else "someone_elses_ollama\n")
        if sub[:2] == ["ps", "--format"]:
            if rt in unlistable:
                return _Result(1)
            return _Result(0, "vco_weaviate\n" if kind(rt) == "running" else "")
        if sub[:2] == ["volume", "ls"]:
            if rt in unlistable:
                return _Result(1)
            return _Result(0, "vco_weaviate_data\n" if kind(rt) != "none" else "someone_elses_data\n")
        raise AssertionError(f"unexpected probe {argv!r}")

    # The fake's _Result is not a CompletedProcess subclass (see the
    # module's other doubles); cast keeps this helper assignable to the
    # resolver's RunFn without growing pyright's pre-existing count.
    return which, cast(containers.RunFn, run)


def _install_root(tmp_path: Path, runtime_txt: Optional[str],
                  confirmed: Optional[str] = None, bind_data: object = False) -> Path:
    """An install root holding the scenario's ``state/install/runtime.txt``
    (or none) and, when the scenario declares ``runtime_confirmed``, its
    ``state/install/runtime.confirmed`` — never the machine's own clone, whose
    record would leak in. ``bind_data`` (R10 J2): its ``infrastructure/.env``
    relocates Weaviate's data to a non-empty folder (``_bind_dir``); ``"all"``
    (R11 L3) relocates Ollama's and the code_embed cache's too."""
    root = tmp_path / "install-root"
    root.mkdir()
    if bind_data:
        folder = _bind_dir(root)
        folder.mkdir(parents=True)
        (folder / "classifications.db").write_text("x", encoding="utf-8")
        (root / "infrastructure").mkdir()
        lines = f"VCT_WEAVIATE_DATA_SOURCE={folder}\nVCT_WEAVIATE_VOLUME_NAME=\n"
        if bind_data == "all":
            for key, name in (("VCT_OLLAMA_DATA_SOURCE", "ollama-folder"),
                              ("VCT_CODE_EMBED_CACHE_SOURCE", "code-embed-folder")):
                (root / name).mkdir()
                (root / name / "blob").write_text("x", encoding="utf-8")
                lines += f"{key}={root / name}\n"
        (root / "infrastructure" / ".env").write_text(lines, encoding="utf-8")
    if runtime_txt is not None:
        containers.runtime_txt_path(root).parent.mkdir(parents=True)
        containers.runtime_txt_path(root).write_text(runtime_txt + "\n", encoding="utf-8")
    if confirmed is not None:
        (root / "state" / "install").mkdir(parents=True, exist_ok=True)
        (root / "state" / "install" / "runtime.confirmed").write_text(confirmed + "\n", encoding="utf-8")
    return root


def _bind_dir(root: Path) -> Path:
    return root / "weaviate-folder"


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_resolve_matches_the_parity_fixture(sc: dict, tmp_path: Path):
    which, run = _probes(sc)
    env = {} if sc["env"] is None else {"VCT_CONTAINER_RUNTIME": sc["env"]}
    warnings: list[str] = []
    root = _install_root(tmp_path, sc.get("runtime_txt"), sc.get("runtime_confirmed"),
                         sc.get("bind_data") or False)
    res = containers.resolve(
        env=env, which=which, run=run, warn=warnings.append, home=tmp_path,
        install_root=root,
    )
    exp = sc["expect"]
    got = {
        "state": res.state.value, "runtime": res.runtime,
        "compose_form": res.compose_form, "installed": res.installed,
        "requested": res.requested, "requested_via": res.requested_via,
        "substituted": res.substituted,
    }
    # `record_reconciled` rides along in `expect` only on the reconcile rows;
    # it is asserted on its own line right below.
    assert got == {k: v for k, v in exp.items() if k != "record_reconciled"}, (
        f"{sc['name']}: {got} != {exp} ({res.reason})"
    )
    assert res.record_reconciled is exp.get("record_reconciled", False), sc["name"]
    if sc["env"] == "bogus":
        assert warnings and "unrecognized" in warnings[0]
    # Ruling (v0.2.92 BLOCKER-4, overturning ASK #1): a pin is HONOURED or
    # REFUSED — never swapped. `substituted` is still computed from
    # `pref is not None and candidate != pref`, so this is a live invariant
    # over every host in the fixture, not an assertion about a constant.
    # The ONE exception (v0.2.97 R8 follow-up, owner rule "users never need
    # a manual step"): case (a) of the READ-ONLY record reconcile — the
    # RECORD pin names a runtime that is not installed and the other one
    # answers, holding VCO's data (or no data exists anywhere) — is answered
    # with the other runtime, writing nothing. Only that arm may substitute,
    # only via the record channel, and only with a reason that says so.
    if exp["substituted"]:
        assert exp["requested_via"] == containers.PIN_VIA_RUNTIME_TXT, sc["name"]
        assert res.record_reconciled is True, sc["name"]
        assert "stale runtime record" in res.reason, f"{sc['name']}: {res.reason}"
    else:
        assert res.substituted is False, (
            f"{sc['name']}: resolver substituted {res.runtime!r} for the pinned "
            f"{res.requested!r} — that forks the data plane (separate named volumes)"
        )
    assert res.to_dict()["substituted"] is exp["substituted"]  # what --json carries
    # R10 J6: WHY the stale record was not switched, in the shared wording of
    # runtime_reconcile_messages.toml (rendered only by Python; every other
    # surface shows this same text through the decide verdict).
    decline = sc.get("expect_not_switched")
    if decline is not None:
        from vco_lib.runtime_reconcile import unusable_detail

        detail = unusable_detail(exp["requested"], str(containers.runtime_txt_path(root)),
                                 "missing", decline,
                                 bind=str(_bind_dir(root)) if sc.get("bind_data") else "")
        assert f"(not switched: {detail})" in res.reason, f"{sc['name']}: {res.reason}"
    else:
        assert "(not switched:" not in res.reason, f"{sc['name']}: {res.reason}"
    if exp["requested"] and exp["state"] != "resolved":
        # A refused pin is USELESS unless it says what to do next.
        assert res.runtime is None and res.compose is None
        assert res.alternative_usable in (None, "podman", "docker")
        assert res.alternative_usable != exp["requested"]
        if exp["requested_via"] == containers.PIN_VIA_RUNTIME_TXT:
            # R7b F5: the record pins, and the refusal names the FILE (its
            # absolute path) and the override that supersedes it.
            assert str(containers.runtime_txt_path(root)) in res.reason
            assert f"recorded {exp['requested']}" in res.reason
            assert (
                f"set VCT_CONTAINER_RUNTIME={res.alternative_usable}" in res.reason
                or "install it" in res.reason
            )
        else:
            assert f"VCT_CONTAINER_RUNTIME={exp['requested']}" in res.reason
            assert "unset VCT_CONTAINER_RUNTIME" in res.reason or "install it" in res.reason
        assert any(exp["requested"] in w for w in warnings), (
            f"refusal was silent: warnings={warnings!r}"
        )
    else:
        assert res.alternative_usable is None


def test_fixture_declares_every_rust_expectation():
    """The Rust side reads ``expect_rust``; a scenario without it would be
    silently skipped there. Same for the module-plane leg
    (``expect_module_plane``, read by container_runtime.rs's fixture test
    since the v0.2.92 delivery audit M1): a scenario lacking it would be
    silently skipped by the third surface — the exact blind spot the
    audit was written to close."""
    for sc in SCENARIOS:
        assert "expect_rust" in sc, sc["name"]
        assert "expect_module_plane" in sc, sc["name"]


def _resolve_host(
    *, pinned: str, on_path: tuple[str, ...], version_ok: tuple[str, ...],
    daemon_ok: tuple[str, ...], compose_ok: tuple[str, ...], tmp_path: Path,
):
    """Resolve one synthetic host with a pin, returning (result, warnings)."""
    def which(name: str) -> Optional[str]:
        return f"/usr/bin/{name}" if name in on_path else None

    def run(argv, **_kw):
        rt, sub = argv[0], argv[1:]
        if sub == ["version"]:
            return _Result(0 if rt in version_ok else 1)
        if sub == ["info"]:
            return _Result(0 if rt in daemon_ok else 1)
        if sub == ["compose", "version"]:
            return _Result(0 if rt in compose_ok else 1)
        raise AssertionError(f"unexpected probe {argv!r}")

    warnings: list[str] = []
    res = containers.resolve(
        env={"VCT_CONTAINER_RUNTIME": pinned}, which=which, run=run,
        warn=warnings.append, home=tmp_path,
    )
    return res, warnings


def test_a_pinned_runtime_that_is_down_is_refused_and_names_the_alternative(tmp_path: Path):
    """v0.2.92 BLOCKER-4, the field case: `VCT_CONTAINER_RUNTIME=podman`,
    podman machine stopped after a reboot, Docker Desktop running. Before the
    fix this RESOLVED docker and the session-start hook ran `docker compose
    up -d`, standing up an EMPTY Weaviate on :8081 (the two runtimes have
    separate named volumes) while the launcher said "no runtime installed".
    Now: refused, with the three facts a user needs to act."""
    res, warnings = _resolve_host(
        pinned="podman", on_path=("podman", "docker"), version_ok=("podman", "docker"),
        daemon_ok=("docker",), compose_ok=("podman", "docker"), tmp_path=tmp_path,
    )
    assert res.state is containers.RuntimeState.ABSENT
    assert res.runtime is None and res.compose is None      # nothing is driven
    assert res.requested == "podman"
    assert res.requested_installed is True                  # "start it", not "install it"
    assert res.alternative_usable == "docker"               # the repin target
    assert res.substituted is False
    for fragment in (
        "VCT_CONTAINER_RUNTIME=podman is set but",
        "podman info` failed",
        "docker is usable",
        "SEPARATE named volumes",
        "start podman",
        "unset VCT_CONTAINER_RUNTIME / set it to docker",
    ):
        assert fragment in res.reason, f"hint lacks {fragment!r}: {res.reason}"
    assert warnings and res.reason in warnings


def test_a_pinned_runtime_that_is_not_installed_says_so(tmp_path: Path):
    """`requested_installed` splits the two remedies: a pin naming a runtime
    that is not on PATH at all is a misconfiguration (repin / install), not a
    stopped daemon (start it)."""
    res, _ = _resolve_host(
        pinned="podman", on_path=("docker",), version_ok=("docker",),
        daemon_ok=("docker",), compose_ok=("docker",), tmp_path=tmp_path,
    )
    assert res.state is containers.RuntimeState.ABSENT
    assert res.requested == "podman" and res.requested_installed is False
    assert res.alternative_usable == "docker"
    assert "podman is not on PATH" in res.reason


def test_a_pinned_refusal_with_no_usable_alternative_does_not_invent_one(tmp_path: Path):
    """Both runtimes down: the hint must not name docker as a way out."""
    res, _ = _resolve_host(
        pinned="podman", on_path=("podman", "docker"), version_ok=("podman", "docker"),
        daemon_ok=(), compose_ok=("podman", "docker"), tmp_path=tmp_path,
    )
    assert res.state is containers.RuntimeState.ABSENT
    assert res.alternative_usable is None
    assert "docker is not usable either" in res.reason
    assert "set it to docker" not in res.reason


def test_a_pinned_runtime_that_works_is_still_resolved(tmp_path: Path):
    """The refusal must not swallow the happy path a pin exists for."""
    res, warnings = _resolve_host(
        pinned="docker", on_path=("podman", "docker"), version_ok=("podman", "docker"),
        daemon_ok=("podman", "docker"), compose_ok=("podman", "docker"), tmp_path=tmp_path,
    )
    assert res.state is containers.RuntimeState.RESOLVED
    assert res.runtime == "docker" and res.compose == ["docker", "compose"]
    assert res.alternative_usable is None and warnings == []


def test_a_pinned_probe_timeout_stays_unknown_not_a_refusal(tmp_path: Path):
    """Tri-state (§4): a probe that could not RUN is UNKNOWN even under a pin
    — collapsing it into the ABSENT refusal would assert a fact nobody
    established."""
    def which(name: str) -> Optional[str]:
        return f"/usr/bin/{name}"

    def run(argv, **_kw):
        if argv[0] == "podman":
            raise subprocess.TimeoutExpired(argv, 1)
        return _Result(0)

    res = containers.resolve(
        env={"VCT_CONTAINER_RUNTIME": "podman"}, which=which, run=run,
        warn=lambda _m: None, home=tmp_path,
    )
    assert res.state is containers.RuntimeState.UNKNOWN
    assert res.runtime is None and res.requested == "podman"
    assert res.requested_installed is True


def test_a_pin_is_the_whole_candidate_order():
    """A pin is the ONLY candidate. The Rust side is held to the same rule by
    EXECUTION, not by a source pattern: ``runtime.rs``'s
    ``parity_fixture_infra_plane_matches_every_scenario`` asks the Python
    verdict client for every pinned row of the shared fixture
    (``env_pref_unusable_*``, ``runtime_txt_pin_unusable_*``), so a client
    or verdict that fell through to the other runtime turns those rows red
    there."""
    assert containers.runtime_candidate_order("podman") == ["podman"]
    assert containers.runtime_candidate_order("docker") == ["docker"]
    assert containers.runtime_candidate_order(None) == ["podman", "docker"]


def test_runtime_pin_precedence_env_then_record_then_none(tmp_path: Path):
    """R7b F5: ONE precedence — ``VCT_CONTAINER_RUNTIME`` → runtime.txt →
    no pin; ``auto`` and garbage are no preference; ``None`` means no install
    root at all (so no record)."""
    root = _install_root(tmp_path, "docker")
    rec = containers.runtime_txt_path(root)
    assert containers.runtime_pin({}, install_root=root) == ("docker", containers.PIN_VIA_RUNTIME_TXT, rec)
    assert containers.runtime_pin({"VCT_CONTAINER_RUNTIME": "podman"}, install_root=root) == (
        "podman", containers.PIN_VIA_ENV, None)
    auto = containers.runtime_pin({"VCT_CONTAINER_RUNTIME": "auto"}, install_root=root)
    assert auto is not None and auto[0] == "docker"
    assert containers.runtime_pin({}, install_root=None) is None
    rec.write_text("nerdctl\n", encoding="utf-8")
    assert containers.runtime_pin({}, install_root=root) is None, "an unknown token is no record"


def test_tri_state_is_never_collapsed():
    """ABSENT and UNKNOWN are different answers (§4)."""
    states = {sc["expect"]["state"] for sc in SCENARIOS}
    assert states == {"resolved", "absent", "unknown"}


def test_install_py_helpers_are_thin_calls_into_the_home():
    """The straggler proof for install.py: no second copy of the decision.
    Each helper's body is a call into ``_containers`` and nothing else that
    probes (no ``shutil.which`` / ``subprocess.run`` of its own)."""
    import ast

    src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {
        "_runtime_preference_from_env", "_detect_container_runtime",
        "_container_runtime_reachable", "_detect_installed_runtime",
        "_get_compose_command", "_detect_selinux_enforcing",
    }
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            seen.add(node.name)
            body_src = ast.get_source_segment(src, node) or ""
            assert "_containers." in body_src, node.name
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                    owner = getattr(call.func.value, "id", None)
                    assert (owner, call.func.attr) not in {
                        ("shutil", "which"), ("subprocess", "run"),
                    }, f"{node.name} still probes on its own via {owner}.{call.func.attr}"
    assert seen == names, f"missing helpers: {names - seen}"


def test_compose_order_is_the_launchers(tmp_path: Path):
    """Subcommand first, then standalone on PATH, then ~/.local/bin.
    (install.py used to prefer standalone `podman-compose` — the four-way
    split R13 closes.)"""
    calls: list[list[str]] = []

    def run(argv, **_kw):
        calls.append(list(argv))
        return _Result(1)

    home = tmp_path
    (home / ".local" / "bin").mkdir(parents=True)
    local = home / ".local" / "bin" / "podman-compose"
    local.write_text("#!/bin/sh\n")
    got = containers.compose_command("podman", which=lambda _n: None, run=run, home=home)
    assert got == ([str(local)], "standalone")
    assert calls == [["podman", "compose", "version"]]

    got = containers.compose_command(
        "podman", which=lambda n: "/usr/bin/podman-compose" if n == "podman-compose" else None,
        run=run, home=home,
    )
    assert got == (["podman-compose"], "standalone")

    got = containers.compose_command("podman", which=lambda _n: None, run=lambda *a, **k: _Result(0), home=home)
    assert got == (["podman", "compose"], "subcommand")


def test_cli_exit_codes_follow_the_state(tmp_path: Path):
    """``python -m vco_lib.containers resolve`` — the hooks' entry point —
    exits 0/1/2 for resolved/absent/unknown and prints JSON on ``--json``."""
    empty_path = tmp_path / "empty-bin"
    empty_path.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.containers", "resolve", "--json"],
        env=child_env(PATH=str(empty_path), VCT_CONTAINER_RUNTIME=""),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == containers.RESOLVE_EXIT_CODES[containers.RuntimeState.ABSENT], proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["state"] == "absent"
    assert payload["runtime"] is None
    assert payload["reason"]


def test_selinux_enforcing_chain(tmp_path: Path):
    sysfs = tmp_path / "enforce"
    # getenforce present and Enforcing → True
    ok = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "Enforcing\n"})()  # noqa: E731
    assert containers.selinux_enforcing(which=lambda n: "/usr/sbin/getenforce", run=ok, sysfs=sysfs, system="Linux")
    # no getenforce, sysfs says 1 → True; says 0 → False; absent → False
    sysfs.write_text("1\n")
    assert containers.selinux_enforcing(which=lambda n: None, sysfs=sysfs, system="Linux")
    sysfs.write_text("0\n")
    assert not containers.selinux_enforcing(which=lambda n: None, sysfs=sysfs, system="Linux")
    sysfs.unlink()
    assert not containers.selinux_enforcing(which=lambda n: None, sysfs=sysfs, system="Linux")
    # non-Linux never probes
    assert not containers.selinux_enforcing(which=lambda n: "/x", run=ok, sysfs=sysfs, system="Darwin")


HOOK_PAIRS = (
    "ensure-containers", "ensure-code-embed-service", "verify-container-ports",
)


@pytest.mark.parametrize("hook", HOOK_PAIRS)
@pytest.mark.parametrize("suffix", [".sh", ".ps1"])
def test_hook_pairs_call_the_resolver_instead_of_mirroring_it(hook: str, suffix: str):
    """The §3.5 straggler proof as a test, both OS siblings: each hook runs
    ``python -m vco_lib.containers resolve`` and carries NO inline
    podman/docker or compose-form detection of its own."""
    body = (REPO_ROOT / "templates" / "hooks" / f"{hook}{suffix}").read_text(encoding="utf-8")
    assert "vco_lib.containers resolve" in body, f"{hook}{suffix} does not call the resolver"
    # v0.2.92 BLOCKER-4 + MAJOR-6. Two properties, both load-bearing:
    #   1. No substitution branch survives — a pin is refused, not swapped.
    #   2. The refusal REACHES THE USER. A SessionStart hook's stderr is not
    #      surfaced on exit 0; only stdout is injected as session context. So
    #      the resolver's reason must be printed on STDOUT.
    for gone in ("VCO_RUNTIME_SUBSTITUTED", "substitution_reason",
                 "requested but unusable", ".substituted"):
        assert gone not in body, f"{hook}{suffix} still carries substitution logic: {gone!r}"
    if suffix == ".sh":
        reporting = [
            ln for ln in body.splitlines()
            if "$VCO_RUNTIME_REASON" in ln and ln.strip().startswith("echo")
        ]
        assert reporting, f"{hook}.sh never prints the resolver's reason"
        for ln in reporting:
            assert ">&2" not in ln, (
                f"{hook}.sh reports the refusal on stderr, which a SessionStart "
                f"hook never surfaces on exit 0: {ln.strip()}"
            )
    else:
        reporting = [
            ln for ln in body.splitlines()
            if "$($VcoRt.reason)" in ln and "Write-Output" in ln
        ]
        assert reporting, f"{hook}.ps1 never prints the resolver's reason on stdout"
        assert "[Console]::Error.WriteLine(\"" not in "".join(reporting)
        # The resolver's own stderr must survive a crash (Windows used to get
        # "resolve failed (rc=N)" with no reason because of `2>$null`).
        assert "resolve --json 2>$null" not in body, (
            f"{hook}.ps1 still discards the resolver's stderr"
        )
        assert "$VcoRtErr" in body, f"{hook}.ps1 does not capture the resolver's stderr"
    for mirror in ("command -v podman", "command -v docker", "Get-Command podman",
                   "Get-Command docker", "podman compose version", "docker compose version",
                   "Get-Command podman-compose", "Get-Command docker-compose"):
        assert mirror not in body, f"{hook}{suffix} still mirrors detection: {mirror!r}"


# ---------------------------------------------------------------------------
# The uninstaller (v0.2.92 MAJOR-3) — it used to be a FOURTH copy of the
# decision: `shutil.which("podman") or shutil.which("docker")` + a hardcoded
# `<runtime> compose down`, ignoring VCT_CONTAINER_RUNTIME and assuming the
# compose SUBCOMMAND exists. Both scenarios below are `--dry-run`, which
# returns after printing the plan, so nothing on the machine is touched.
# ---------------------------------------------------------------------------


def _uninstall_plan(tmp_path: Path, bin_dir: Path, **env: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "install.py"), "--uninstall", "--dry-run"],
        capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT),
        env=child_env(PATH=str(bin_dir), **env),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout


def _fake_runtime_bin(tmp_path: Path, *, compose_subcommand: bool) -> Path:
    """A podman whose `version` / `info` succeed and whose `compose`
    subcommand is absent, next to a standalone `podman-compose`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    podman = bin_dir / "podman"
    podman.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  version|info) exit 0 ;;\n"
        f"  compose) exit {0 if compose_subcommand else 1} ;;\n"
        "esac\n"
        "exit 1\n",
        encoding="utf-8",
    )
    podman.chmod(0o755)
    standalone = bin_dir / "podman-compose"
    standalone.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    standalone.chmod(0o755)
    return bin_dir


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shim scripts")
def test_uninstall_drives_the_compose_command_the_resolver_found(tmp_path: Path):
    """A standalone-`podman-compose` host: the plan must name THAT driver.
    The old fourth copy printed `podman compose down`, which such a host
    cannot run — so uninstall step 1 failed and silently did nothing."""
    bin_dir = _fake_runtime_bin(tmp_path, compose_subcommand=False)
    out = _uninstall_plan(tmp_path, bin_dir, VCT_CONTAINER_RUNTIME="podman")
    assert "[1] Stop containers via `podman-compose down`" in out, out[:3000]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shim scripts")
def test_uninstall_honours_the_pin_and_reports_a_refusal(tmp_path: Path):
    """v0.2.92 BLOCKER-4 reaching the uninstaller: with `docker` pinned on a
    host where only podman works, the old code ran `podman compose down`
    (leaving the user's docker stack up) and printed volume-removal commands
    naming podman. Now it refuses and says why."""
    bin_dir = _fake_runtime_bin(tmp_path, compose_subcommand=True)
    out = _uninstall_plan(tmp_path, bin_dir, VCT_CONTAINER_RUNTIME="docker")
    assert "[1] [skip] Containers not stopped" in out, out[:3000]
    assert "VCT_CONTAINER_RUNTIME=docker is set but docker is not on PATH" in out
    assert "podman-compose down" not in out and "podman compose down" not in out


def test_the_uninstaller_holds_no_runtime_detection_of_its_own():
    """The straggler proof for MAJOR-3: the uninstall path resolves through
    the ONE home, so it can never drift from the other three consumers."""
    import ast

    src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_run_uninstall"
    )
    body = ast.get_source_segment(src, fn) or ""
    assert "_containers.resolve()" in body, "uninstaller does not use the resolver"
    # AST, not grep: the comment above the call explains the old copy and would
    # match a string search for it.
    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        target = ast.unparse(call.func)
        if target not in {"shutil.which", "which"}:
            continue
        named = [
            a.value for a in call.args
            if isinstance(a, ast.Constant) and isinstance(a.value, str)
        ]
        assert not ({"podman", "docker"} & set(named)), (
            f"uninstaller still probes for a runtime itself: {ast.unparse(call)}"
        )


def test_rust_mirror_reads_the_same_fixture():
    """The Rust parity test must read THIS fixture, not a copy — and must go
    through the Python verdict client (the mirror fns are retired, v0.2.97)."""
    rs = (REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "services" / "runtime.rs").read_text(encoding="utf-8")
    assert "container_runtime_parity.json" in rs
    assert "runtime_verdict::decide" in rs
    assert "fn select_runtime" not in rs and "fn candidate_order" not in rs, (
        "the retired Rust mirror must not come back"
    )


def test_no_vco_lib_marker_points_at_the_retired_rust_mirrors():
    """R12bis F1: v0.2.97 R12 retired the Rust runtime mirrors
    (``runtime_evidence.rs`` outright; the reconcile fns of
    ``container_runtime.rs`` / ``runtime.rs``). Every Rust surface now asks
    ``runtime_verdict::decide``. A ``MUST MATCH <deleted fn>`` marker in
    vco_lib directs a reader at code that no longer exists — nothing
    satisfies the parity it promises, so the markers were retired with the
    mirrors; this keeps them retired."""
    retired = (
        "runtime_evidence.rs",
        "::candidate_order", "::select_runtime", "::detect_compose_form",
        "::module_runtime_pin_refusal", "::runtime_preference_from_env",
        "::read_runtime_txt", "::record_reconcile_note",
        "::vco_compose_project", "::bind_data_source", "::vco_data_kind",
        "::data_evidence", "::bind_verdict", "::all_services_bind",
        "::bind_probe", "::bind_folder_holds_data", "::vco_volume_names",
        "::volume_names_from", "::VCO_VOLUME_NAME_KEYS",
        "::VCO_DATA_SOURCE_KEYS", "::VCO_OUR_CONTAINER_NAMES",
        "::DataKind", "::data_kind",
    )
    for rel in ("vco_lib/runtime_reconcile.py", "vco_lib/containers.py"):
        body = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for needle in retired:
            assert needle not in body, (
                f"{rel} still points at retired Rust code: {needle!r}"
            )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shim scripts")
def test_the_hooks_entry_point_honours_the_install_record(tmp_path: Path):
    """R7b F5, end to end through the CLI the session-start hooks run
    (``python -m vco_lib.containers resolve --json``): both runtimes answer,
    nothing pins the env, and the install recorded docker — the hooks must
    drive docker, the runtime storage/volumes and the module plane already
    followed. Before v0.2.97 they auto-detected podman (podman-first)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for rt in ("podman", "docker"):
        shim = bin_dir / rt
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
    root = tmp_path / "clone"
    (root / "vco_lib").mkdir(parents=True)      # what resolve_install_root
    (root / ".claude").mkdir()                  # accepts as a clone
    containers.runtime_txt_path(root).parent.mkdir(parents=True)
    containers.runtime_txt_path(root).write_text("docker\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.containers", "resolve", "--json"],
        env=child_env(PATH=str(bin_dir), VCT_CONTAINER_RUNTIME="", VCT_INSTALL_ROOT=str(root)),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert (payload["runtime"], payload["requested"], payload["requested_via"]) == (
        "docker", "docker", "state/install/runtime.txt")


# ---------------------------------------------------------------------------
# v0.2.97 R8 follow-up — the READ-ONLY record reconcile inside the resolver.
# The record pin (state/install/runtime.txt) naming a runtime that is NOT
# installed, with the other runtime answering and holding VCO's data (or no
# VCO data anywhere), is ANSWERED with the other runtime: nothing written,
# the env pin never reconciled, installed-but-down still refused. The fixture
# rows above pin the same rule; these drive it directly with an install root.
# ---------------------------------------------------------------------------


def _record_doubles(on_path: tuple[str, ...], daemon_ok: tuple[str, ...],
                    data: dict[str, bool]):
    """which/run doubles for a host with an install record: version/info
    exit 0 for runtimes in ``daemon_ok`` (and 1 for an installed one whose
    daemon is down), and the reconcile's ps/volume listings answer per
    ``data`` (True → vco_* names, False → a foreign name)."""
    def which(name: str) -> Optional[str]:
        return f"/usr/bin/{name}" if name in on_path else None

    def run(argv, **_kw):
        rt, sub = argv[0], argv[1:]
        if sub == ["version"]:
            return _Result(0 if rt in on_path else 1)
        if sub == ["info"]:
            return _Result(0 if rt in daemon_ok else 1)
        if sub == ["compose", "version"]:
            return _Result(0)
        if sub[:2] == ["ps", "-a"]:
            return _Result(0, "vco_weaviate\n" if data.get(rt) else "someone_elses_ollama\n")
        if sub[:2] == ["volume", "ls"]:
            return _Result(0, "vco_weaviate_data\n" if data.get(rt) else "someone_elses_data\n")
        raise AssertionError(f"unexpected probe {argv!r}")

    # The fake's _Result is not a CompletedProcess subclass (see the
    # module's other doubles); cast keeps this helper assignable to the
    # resolver's RunFn without growing pyright's pre-existing count.
    return which, cast(containers.RunFn, run)


def test_a_stale_record_with_the_other_runtime_holding_the_data_resolves_to_it(
    tmp_path: Path,
):
    """Case (a), the owner's field shape: the install recorded docker, the
    user removed Docker Desktop and moved to podman (the boot wrapper already
    brought the stack up under podman) — the resolver must ANSWER podman
    instead of refusing every session until the next update re-records, and
    it must WRITE NOTHING (the update owns the record)."""
    root = _install_root(tmp_path, "docker")
    which, run = _record_doubles(("podman",), ("podman",), {"podman": True})
    res = containers.resolve(env={}, which=which, run=run, warn=lambda _m: None,
                             home=tmp_path, install_root=root)
    assert res.state is containers.RuntimeState.RESOLVED
    assert res.runtime == "podman" and res.compose == ["podman", "compose"]
    assert res.requested == "docker"
    assert res.requested_via == containers.PIN_VIA_RUNTIME_TXT
    assert res.substituted is True and res.record_reconciled is True
    assert "stale runtime record" in res.reason
    assert "holds VCO's containers/volumes" in res.reason
    # Nothing is written: the record is the update's to re-make.
    assert containers.read_runtime_txt(root) == "docker"


def test_a_recorded_runtime_installed_but_down_is_still_refused(tmp_path: Path):
    """The reconcile never lifts a pin whose runtime is INSTALLED but its
    daemon does not answer — that machine may well hold the data; starting
    the runtime is the remedy, not switching."""
    root = _install_root(tmp_path, "docker")
    which, run = _record_doubles(("podman", "docker"), ("podman",), {"podman": True})
    warnings: list[str] = []
    res = containers.resolve(env={}, which=which, run=run, warn=warnings.append,
                             home=tmp_path, install_root=root)
    assert res.state is containers.RuntimeState.ABSENT
    assert res.runtime is None
    assert res.substituted is False and res.record_reconciled is False
    assert res.requested == "docker" and res.requested_installed is True
    assert containers.read_runtime_txt(root) == "docker"


def test_the_env_pin_is_never_reconciled(tmp_path: Path):
    """The USER's pin is strict everywhere: docker pinned through
    VCT_CONTAINER_RUNTIME on a host where docker is gone and podman holds
    everything stays a refusal — even though the record would reconcile."""
    root = _install_root(tmp_path, "podman")
    which, run = _record_doubles(("podman",), ("podman",), {"podman": True})
    warnings: list[str] = []
    res = containers.resolve(env={"VCT_CONTAINER_RUNTIME": "docker"}, which=which,
                             run=run, warn=warnings.append, home=tmp_path,
                             install_root=root)
    assert res.state is containers.RuntimeState.ABSENT
    assert res.substituted is False and res.record_reconciled is False
    assert res.requested == "docker"
    assert res.requested_via == containers.PIN_VIA_ENV
    assert res.alternative_usable == "podman"
    assert any("VCT_CONTAINER_RUNTIME=docker" in w for w in warnings)


def test_fixture_comments_name_retired_rust_readers_only_as_retired():
    """R12bis verify N2: the fixtures' ``_comment`` blocks told a reader
    which Rust functions consume them. Those readers were retired in v0.2.97
    R12 (every Rust surface asks ``runtime_verdict::decide``); a comment
    line may still NAME a retired function for history, but only on a line
    that says it was retired."""
    retired = ("candidate_order", "select_runtime", "decide_module_runtime",
               "runtime_evidence.rs")
    for rel in ("tests/fixtures/container_runtime_parity.json",
                "tests/fixtures/runtime_data_evidence_cases.json"):
        comment = json.loads((REPO_ROOT / rel).read_text(encoding="utf-8"))["_comment"]
        for line in comment:
            if any(name in line for name in retired):
                assert "retired" in line, (
                    f"{rel} _comment names a retired Rust reader as live: {line!r}"
                )

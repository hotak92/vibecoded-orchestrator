# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-04 — typed compose failures and their non-destructive heals.

Every runtime interaction is an injected fake: no podman, no docker, no
systemctl is ever run. Destructive branches (``network rm``, ``rmdir``,
``podman rm --storage``, the ``--build``-less retry) each have an ACT test
and at least one LEAVE-ALONE test.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
from pathlib import Path

import pytest

from vco_lib import code_embed_image
from vco_lib import compose_provider as cp
from vco_lib import compose_recovery as cr
from vco_lib import containers
from vco_lib import install_services_guard as guard

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS = json.loads((REPO_ROOT / "tests" / "fixtures" / "compose_failure_corpus.json")
                    .read_text(encoding="utf-8"))["cases"]


def _cp(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


# ---------------------------------------------------------------------------
# classify — the recorded corpus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CORPUS, ids=[c["id"] for c in CORPUS])
def test_classifier_on_recorded_stderr(case):
    f = cr.classify(case["stderr"], runtime=case["runtime"])
    assert f.cause == case["cause"], f.evidence
    if "subject" in case:
        assert f.subject == case["subject"]
    # the evidence is a real line of the stderr, never a banner or a paraphrase
    assert f.evidence and f.evidence in case["stderr"]
    assert ">>>>" not in f.evidence


def test_corpus_covers_every_cause_but_provider_mismatch():
    """PROVIDER_MISMATCH is established from labels (``refine``), not text."""
    seen = {c["cause"] for c in CORPUS}
    expected = {cr.SOCKET_MISSING, cr.DAEMON_DOWN, cr.PORT_TAKEN,
                cr.NAME_CONFLICT_STORAGE_LEFTOVER, cr.NAME_CONFLICT_OTHER_PROVIDER,
                cr.NETWORK_LABEL_MISMATCH, cr.BUILD_FAILED, cr.BUILD_FLAG_UNSUPPORTED,
                cr.UNKNOWN}
    assert expected <= seen


@pytest.mark.parametrize("stderr", [
    "Error response from daemon: no such image",
    "the daemon said something",
    "bind: something odd",
    "Error: bind mount failed for /x",
])
def test_bare_daemon_or_bind_words_are_never_a_cause(stderr):
    f = cr.classify(stderr, runtime="podman")
    assert f.cause == cr.UNKNOWN


def test_name_conflict_is_never_reported_as_daemon_not_running():
    stderr = next(c["stderr"] for c in CORPUS if c["id"] == "field_2026_09_29_storage_leftover")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        guard.print_compose_failure_hints(cr.classify(stderr, runtime="podman"), "podman")
    out = buf.getvalue()
    assert "daemon not running" not in out.lower()
    assert "not running" not in out.lower()
    assert "storage-only leftover" in out
    assert "vco_code_embed" in out


def test_socket_hint_names_the_socket_path():
    stderr = next(c["stderr"] for c in CORPUS if c["id"] == "field_2026_09_29_socket_file_missing")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        guard.print_compose_failure_hints(stderr, "podman")  # raw stderr is classified first
    out = buf.getvalue()
    assert "/run/user/1000/podman/podman.sock" in out
    assert "restart podman.socket" in out


# ---------------------------------------------------------------------------
# refine — provider mismatch from labels
# ---------------------------------------------------------------------------


def _provider(family, runtime="podman"):
    engine = {"docker": cp.ENGINE_DOCKER_COMPOSE, "podman": cp.ENGINE_PODMAN_COMPOSE}[family]
    return cp.ComposeProvider("subcommand", engine, ("podman", "compose"), family, runtime)


def _name_conflict():
    stderr = next(c["stderr"] for c in CORPUS if c["id"] == "podman_name_conflict_known_container")
    return cr.classify(stderr, runtime="podman")


def test_refine_upgrades_to_provider_mismatch_when_labels_name_the_other_tool(monkeypatch):
    monkeypatch.setattr(containers, "compose_identity_of",
                        lambda name, rt, run=None: containers.ComposeIdentity(
                            "vibecoded", "/x", "compose.yaml", provider="podman"))
    f = cr.refine(_name_conflict(), provider=_provider("docker"), runtime="podman")
    assert f.cause == cr.PROVIDER_MISMATCH and not f.healable
    rows = cr.deferral_entries(cr.UpResult(1, failure=f), manual_cmd="x")
    assert [r.condition_id for r in rows] == [cr.CID_PROVIDER_MISMATCH]


@pytest.mark.parametrize("ident", [
    None,
    containers.ComposeIdentity("infrastructure", provider="docker"),
    containers.ComposeIdentity("infrastructure", provider=""),
])
def test_refine_leaves_the_conflict_alone_without_positive_evidence(monkeypatch, ident):
    monkeypatch.setattr(containers, "compose_identity_of", lambda name, rt, run=None: ident)
    f = cr.refine(_name_conflict(), provider=_provider("docker"), runtime="podman")
    assert f.cause == cr.NAME_CONFLICT_OTHER_PROVIDER


# ---------------------------------------------------------------------------
# heal — network label
# ---------------------------------------------------------------------------


#: the field case: podman-compose made `infrastructure_default` for project
#: `infrastructure`, then docker-compose v2 refused it for the missing label
PODMAN_MADE = {"io.podman.compose.project": "infrastructure"}
INSPECT = ["podman", "network", "inspect", "infrastructure_default", "--format",
           "{{json .Labels}}"]


class NetRun:
    def __init__(self, attached="", ps_rc=0, labels=None, inspect_rc=0, raw=None):
        self.attached, self.ps_rc, self.calls = attached, ps_rc, []
        self.labels = PODMAN_MADE if labels is None else labels
        self.inspect_rc, self.raw = inspect_rc, raw

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[1:3] == ["network", "inspect"]:
            out = self.raw if self.raw is not None else json.dumps(self.labels)
            return _cp(argv, self.inspect_rc, out)
        if argv[1] == "ps":
            return _cp(argv, self.ps_rc, self.attached)
        if argv[1:3] == ["network", "rm"]:
            return _cp(argv)
        raise AssertionError(argv)

    @property
    def removed(self):
        return any(c[1:3] == ["network", "rm"] for c in self.calls)


def _net_failure():
    stderr = next(c["stderr"] for c in CORPUS if c["id"] == "field_2026_09_29_network_label")
    return cr.classify(stderr, runtime="podman")


def _net_heal(run, project="infrastructure"):
    return cr.heal(_net_failure(), runtime="podman", run=run, log=lambda m: None,
                   project=project)


@pytest.mark.parametrize("labels", [
    PODMAN_MADE,                                            # podman-compose family
    {"com.docker.compose.project": "infrastructure"},       # docker-compose family
])
def test_compose_made_network_with_nothing_attached_is_removed_act(labels):
    run = NetRun(attached="", labels=labels)
    h = _net_heal(run)
    assert h.healed
    assert run.calls == [
        INSPECT,
        ["podman", "ps", "-a", "--filter", "network=infrastructure_default", "-q"],
        ["podman", "network", "rm", "infrastructure_default"],
    ]


@pytest.mark.parametrize("run_kw,why", [
    ({"labels": {}}, "no compose project label"),                          # hand-made
    ({"labels": {"io.podman.compose.project": "otherstack"}}, "otherstack"),  # other project
    ({"labels": {"com.docker.compose.project": "Infrastructure"}}, "Infrastructure"),  # exact
    ({"inspect_rc": 125}, "could not inspect"),                           # unreadable
    ({"raw": "{not json"}, "could not parse"),                            # unparseable
])
def test_network_without_positive_provenance_is_never_removed_leave_alone(run_kw, why):
    """W1R-01: "nothing attached" alone is not evidence the network is VCO's."""
    run = NetRun(attached="", **run_kw)
    h = _net_heal(run)
    assert not h.healed and h.deferral_cid == cr.CID_NETWORK_LABEL_ATTACHED
    assert why in h.reason
    assert not run.removed
    assert not any(c[1] == "ps" for c in run.calls), "provenance is checked FIRST"
    row = cr.heal_deferral_entry(h, manual_cmd="cd x && up")
    assert row is not None and "infrastructure_default" in row.command_to_apply
    assert "network rm infrastructure_default" in row.command_to_apply  # commented recipe


def test_network_heal_without_a_known_project_refuses():
    run = NetRun(attached="")
    h = _net_heal(run, project=None)
    assert not h.healed and h.deferral_cid == cr.CID_NETWORK_LABEL_ATTACHED
    assert run.calls == [] and not run.removed


def test_network_with_attached_containers_is_never_removed_and_ledgered():
    run = NetRun(attached="abc123def456\n")
    h = _net_heal(run)
    assert not h.healed and h.deferral_cid == cr.CID_NETWORK_LABEL_ATTACHED
    assert not run.removed
    rows = cr.deferral_entries(cr.UpResult(1, heals=[h]), manual_cmd="cd x && up")
    assert [r.condition_id for r in rows] == [cr.CID_NETWORK_LABEL_ATTACHED]


def test_network_whose_attachments_cannot_be_listed_is_left_alone():
    run = NetRun(ps_rc=125)
    h = _net_heal(run)
    assert not h.healed and h.deferral_cid is None
    assert not run.removed


@pytest.mark.parametrize("argv,env,want", [
    (["podman", "compose", "-f", "/x/infra/dc.yml", "-p", "vco", "up"], None, "vco"),
    (["docker", "compose", "--project-name", "p1", "up"], None, "p1"),
    (["docker", "compose", "--project-name=p2", "up"], None, "p2"),
    (["docker-compose", "up"], {"COMPOSE_PROJECT_NAME": "envp"}, "envp"),
    (["docker-compose", "up"], None, ""),
])
def test_compose_project_from_argv(argv, env, want):
    assert cr.compose_project_from_argv(argv, env) == want


def test_recovery_hands_the_argv_project_to_the_heal():
    seen = []
    script = ComposeScript((1, _net_failure().evidence), (0, ""))
    _up(script, heal_fn=lambda f, **k: (seen.append(k.get("project")),
                                        cp.HealResult(True, [], "healed"))[1])
    assert seen == ["infrastructure"]  # ARGV's `-p infrastructure`


# ---------------------------------------------------------------------------
# heal — storage-only leftover
# ---------------------------------------------------------------------------

CID = "5d1c0e8b7a2f9c3e4b6a1d0f8e7c6b5a4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b9a"
LAYER = "7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f"


class StorageWorld:
    def __init__(self, tmp: Path, *, name="vco_code_embed", state="storage",
                 userns_mountinfo="", unshare_rc=0, rm_rc=0):
        self.root = tmp / "storage"
        (self.root / "overlay-containers").mkdir(parents=True)
        (self.root / "overlay-containers" / "containers.json").write_text(
            json.dumps([{"id": CID, "names": [name], "layer": LAYER}]), encoding="utf-8")
        self.merged = self.root / "overlay" / LAYER / "merged"
        (self.merged / "app").mkdir(parents=True)
        self.name, self.state = name, state
        self.userns_mountinfo, self.unshare_rc, self.rm_rc = userns_mountinfo, unshare_rc, rm_rc
        self.calls = []

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[1:4] == ["ps", "-a", "--external"]:
            return _cp(argv, 0, json.dumps([{"Id": CID, "Names": [self.name],
                                             "State": self.state, "Status": self.state.title()}]))
        if argv[1:3] == ["info", "--format"]:
            return _cp(argv, 0, str(self.root) + "\n")
        if argv[1] == "unshare":
            return _cp(argv, self.unshare_rc, self.userns_mountinfo)
        if argv[1:4] == ["rm", "--storage", "--force"]:
            return _cp(argv, self.rm_rc)
        raise AssertionError(argv)

    def failure(self):
        return cr.classify(
            f'Error: creating container storage: the container name "{self.name}" is already '
            f"in use by {CID}. You have to remove that container to be able to reuse that name: "
            "that name is already in use by an external entity, or use --replace",
            runtime="podman")

    def heal(self, host_mountinfo="", runtime="podman"):
        return cr.heal(self.failure(), runtime=runtime, run=self.run, log=lambda m: None,
                       read_host_mountinfo=lambda: host_mountinfo)

    @property
    def removed_storage(self):
        return any(c[1:3] == ["rm", "--storage"] for c in self.calls)


def test_empty_storage_leftover_is_cleaned_non_recursively(tmp_path):
    w = StorageWorld(tmp_path)
    h = w.heal(host_mountinfo="36 25 0:32 / / rw - overlay overlay rw\n")
    assert h.healed, h.reason
    assert not (w.merged / "app").exists()
    assert w.merged.exists(), "merged/ itself is podman's to remove, never ours"
    # W1R-02: the verb verified by hand on 2026-09-29, after the rmdir.
    assert ["podman", "rm", "--storage", "--force", CID] in w.calls
    assert h.actions == [f"rmdir {w.merged / 'app'}", f"podman rm --storage --force {CID}"]


def test_leftover_still_mounted_on_the_host_is_refused(tmp_path):
    w = StorageWorld(tmp_path)
    h = w.heal(host_mountinfo=f"99 25 0:52 / /x/overlay/{LAYER}/merged rw - overlay overlay rw\n")
    assert not h.healed and h.deferral_cid == cr.CID_STORAGE_LEFTOVER_UNSAFE
    assert (w.merged / "app").exists() and not w.removed_storage


def test_leftover_still_mounted_in_the_user_namespace_is_refused(tmp_path):
    w = StorageWorld(tmp_path, userns_mountinfo=f"88 1 0:52 / /s/overlay/{LAYER}/merged/app rw\n")
    h = w.heal()
    assert not h.healed and h.deferral_cid == cr.CID_STORAGE_LEFTOVER_UNSAFE
    assert (w.merged / "app").exists() and not w.removed_storage


def test_unreadable_user_namespace_view_is_refused(tmp_path):
    w = StorageWorld(tmp_path, unshare_rc=1)
    h = w.heal()
    assert not h.healed and not w.removed_storage and (w.merged / "app").exists()


def test_non_empty_merged_is_refused_and_nothing_is_deleted(tmp_path):
    w = StorageWorld(tmp_path)
    data = w.merged / "app" / "model.bin"
    data.write_bytes(b"host data")
    h = w.heal()
    assert not h.healed and h.deferral_cid == cr.CID_STORAGE_LEFTOVER_UNSAFE
    assert data.read_bytes() == b"host data"
    assert (w.merged / "app").exists() and not w.removed_storage


def test_symlink_under_merged_is_refused(tmp_path):
    w = StorageWorld(tmp_path)
    target = tmp_path / "host_dir"
    target.mkdir()
    (w.merged / "app" / "link").symlink_to(target)
    h = w.heal()
    assert not h.healed and target.exists() and not w.removed_storage


def test_non_canonical_name_is_never_touched(tmp_path):
    w = StorageWorld(tmp_path, name="someone_elses_db")
    h = w.heal()
    assert not h.healed and w.calls == [] and (w.merged / "app").exists()


def test_a_container_podman_still_knows_is_not_a_leftover(tmp_path):
    w = StorageWorld(tmp_path, state="exited")
    h = w.heal()
    assert not h.healed and not w.removed_storage and (w.merged / "app").exists()


def test_storage_heal_does_not_apply_to_docker(tmp_path):
    w = StorageWorld(tmp_path)
    f = w.failure()
    h = cr.heal(f, runtime="docker", run=w.run, log=lambda m: None,
                read_host_mountinfo=lambda: "")
    assert not h.healed and w.calls == []


def test_failed_storage_rm_is_reported(tmp_path):
    """W1R-02 fail-closed: an rm this podman rejects is a refusal with the
    EXACT manual recipe (real id, layer, merged path), never a guess."""
    w = StorageWorld(tmp_path, rm_rc=125)
    h = w.heal()
    assert not h.healed and h.deferral_cid == cr.CID_STORAGE_LEFTOVER_UNSAFE
    row = cr.heal_deferral_entry(h, manual_cmd="cd x && up")
    assert row is not None
    recipe = row.command_to_apply
    assert f"podman rm --storage --force {CID}" in recipe
    assert f"grep {LAYER} /proc/self/mountinfo" in recipe
    assert f"podman unshare grep {LAYER} /proc/self/mountinfo" in recipe
    assert str(w.merged) in recipe and "rm -r" not in recipe.replace("never rm -r", "")
    assert "<id>" not in recipe


def test_refused_storage_heal_before_the_id_is_known_still_names_the_recipe(tmp_path):
    w = StorageWorld(tmp_path, name="not_a_vco_name")
    h = w.heal()
    row = cr.heal_deferral_entry(h, manual_cmd="m")
    assert row is not None and "podman rm --storage --force <id>" in row.command_to_apply


# ---------------------------------------------------------------------------
# socket heal routing (the heal itself is tested in test_v02100_compose_provider)
# ---------------------------------------------------------------------------


def test_socket_heal_probes_the_socket_compose_named(monkeypatch):
    """W1R-04: the path in compose's error is the one checked, and the
    provider is passed through (W1R-07)."""
    stderr = next(c["stderr"] for c in CORPUS if c["id"] == "field_2026_09_29_socket_file_missing")
    seen = {}
    monkeypatch.setattr(cp, "heal_socket", lambda rt, **kw: (seen.update(kw),
                                                           cp.HealResult(False))[1])
    prov = cp.ComposeProvider("subcommand", cp.ENGINE_DOCKER_COMPOSE, (), "docker", "podman")
    cr.heal(cr.classify(stderr, runtime="podman"), runtime="podman", run=lambda *a, **k: None,
            log=lambda m: None, provider=prov)
    assert seen["path"] == "/run/user/1000/podman/podman.sock" and seen["provider"] is prov


def test_ok_socket_is_one_compose_attempt_and_no_heal_or_row():
    """W1R-04: "nothing to heal" must end the loop — no three identical
    retries, no "healed" record, no ledger row."""
    stderr = next(c["stderr"] for c in CORPUS if c["id"] == "field_2026_09_29_socket_file_missing")
    script = ComposeScript((1, stderr))
    res = _up(script, heal_fn=lambda f, **k: cr.heal(
        f, socket_heal=lambda: cp.heal_socket("podman", status=cp.SocketStatus(cp.SOCKET_OK)),
        **k))
    assert len(script.calls) == 1
    assert [h.healed for h in res.heals] == [False]
    assert cr.deferral_entries(res, manual_cmd="m") == []


@pytest.mark.parametrize("system,actions,want,unwanted", [
    ("Linux", ["systemctl --user restart podman.socket"], "systemctl --user restart", "machine"),
    ("Linux", ["systemctl restart podman.socket"], "systemctl restart podman.socket", "--user"),
    ("Darwin", ["podman machine start"], "podman machine start", "systemctl"),
    ("Windows", ["podman machine start"], "podman machine start", "systemctl"),
])
def test_socket_row_recipe_matches_the_os(system, actions, want, unwanted):
    """W1R-11: the recipe is the one for the OS the heal ran on."""
    h = cp.HealResult(False, actions, "still missing", cr.CID_SOCKET_HEAL_FAILED)
    row = cr.heal_deferral_entry(h, manual_cmd="m", system=system)
    assert row is not None and want in row.command_to_apply
    assert unwanted not in row.command_to_apply


def test_socket_missing_heal_routes_to_the_socket_healer():
    stderr = next(c["stderr"] for c in CORPUS if c["id"] == "field_2026_09_29_socket_file_missing")
    called = []
    h = cr.heal(cr.classify(stderr, runtime="podman"), runtime="podman",
                socket_heal=lambda: (called.append(1), cp.HealResult(True, ["x"], "ok"))[1],
                log=lambda m: None)
    assert h.healed and called == [1]


def test_unhealable_causes_are_not_healed():
    stderr = next(c["stderr"] for c in CORPUS if c["id"] == "docker_port_allocated")
    h = cr.heal(cr.classify(stderr, runtime="docker"), runtime="docker",
                run=lambda *a, **k: pytest.fail("no runtime call"), log=lambda m: None)
    assert not h.healed


# ---------------------------------------------------------------------------
# compose_up_with_recovery
# ---------------------------------------------------------------------------


class ComposeScript:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        rc, err = self.results.pop(0)
        if rc == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        return _cp(argv, rc, "", err)


ARGV = ["podman", "compose", "-f", "docker-compose.yml", "-p", "infrastructure",
        "up", "-d", "--build", "--no-deps", "code_embed"]
NET = next(c["stderr"] for c in CORPUS if c["id"] == "field_2026_09_29_network_label")
BUILD_FAILED = next(c["stderr"] for c in CORPUS if c["id"] == "buildkit_failed_to_solve")
BUILD_FLAG = next(c["stderr"] for c in CORPUS if c["id"] == "podman_compose_rejects_build")


def _up(script, heal_fn=None, **kw):
    return cr.compose_up_with_recovery(
        ARGV, runtime="podman", compose_run=script, run=lambda *a, **k: _cp(a[0], 1),
        log=lambda m: None, heal_fn=heal_fn or (lambda f, **k: cp.HealResult(True, [], "healed")),
        **kw)


def test_first_stderr_is_kept_across_a_heal_and_retry():
    script = ComposeScript((1, NET), (1, "Error: something else entirely\n"))
    res = _up(script)
    assert not res.ok
    assert res.first_stderr == NET
    assert res.failure.cause == cr.NETWORK_LABEL_MISMATCH
    assert res.final_failure.cause == cr.UNKNOWN
    assert len(script.calls) == 2


def test_healed_failure_retries_and_succeeds():
    script = ComposeScript((1, NET), (0, ""))
    res = _up(script)
    assert res.ok and len(res.heals) == 1 and res.first_stderr == NET


def test_build_flag_is_dropped_only_on_positive_evidence():
    script = ComposeScript((1, BUILD_FLAG), (0, ""))
    res = _up(script)
    assert res.ok and res.build_dropped
    assert "--build" in script.calls[0] and "--build" not in script.calls[1]
    assert res.failure.cause == cr.BUILD_FLAG_UNSUPPORTED


def test_real_build_failure_is_not_retried_without_build():
    script = ComposeScript((1, BUILD_FAILED))
    res = _up(script)
    assert not res.ok and not res.build_dropped
    assert len(script.calls) == 1
    assert res.failure.cause == cr.BUILD_FAILED
    assert "failed to solve" in res.failure.evidence


def test_unknown_failure_is_not_retried():
    script = ComposeScript((1, "boom\n"))
    res = _up(script)
    assert len(script.calls) == 1 and res.heals == [] and res.failure.cause == cr.UNKNOWN


def test_refused_heal_stops_the_loop():
    script = ComposeScript((1, NET))
    res = _up(script, heal_fn=lambda f, **k: cp.HealResult(False, [], "attached", "x"))
    assert len(script.calls) == 1 and len(res.heals) == 1


def test_heals_are_capped():
    script = ComposeScript(*[(1, NET)] * 10)
    res = _up(script, max_heals=2)
    assert len(res.heals) == 2 and len(script.calls) == 3


def test_timeout_is_reported():
    res = _up(ComposeScript(("timeout", "")))
    assert res.timed_out and res.returncode is None


# ---------------------------------------------------------------------------
# build_rejected_lines — only on positive BUILD_FLAG_UNSUPPORTED
# ---------------------------------------------------------------------------


def test_build_rejected_lines_only_for_unsupported_flag():
    manual = "podman compose -f /i/docker-compose.yml -p infrastructure up -d --build code_embed"
    assert code_embed_image.build_rejected_lines(cr.classify(BUILD_FAILED), manual, "/i") == ()
    assert code_embed_image.build_rejected_lines(None, manual, "/i") == ()
    lines = code_embed_image.build_rejected_lines(cr.classify(BUILD_FLAG), manual, "/i")
    assert lines and any(manual in ln for ln in lines)


# ---------------------------------------------------------------------------
# containers.py — provider labels + the --external listing
# ---------------------------------------------------------------------------


def test_compose_identity_reads_the_provider_labels(monkeypatch):
    monkeypatch.setattr(containers, "_resolve_runtime", lambda rt, *a, **k: "podman")
    run = lambda argv, **kw: _cp(argv, 0, "vibecoded\t/x\tcompose.yaml\tvibecoded\t\n")  # noqa: E731
    ident = containers.compose_identity_of("vco_weaviate", "podman", run=run)
    assert ident.provider == "podman"
    run = lambda argv, **kw: _cp(argv, 0, "infrastructure\t/i\td.yml\t\tabc123\n")  # noqa: E731
    assert containers.compose_identity_of("vco_weaviate", "podman", run=run).provider == "docker"


def test_external_listing_parses_storage_rows_and_refuses_to_guess():
    out = json.dumps([{"Id": "a1", "Names": ["vco_ollama"], "State": "storage"},
                      {"Id": "b2", "Names": ["vco_weaviate"], "State": "running"}])
    rows = containers.list_external_containers("podman", run=lambda a, **k: _cp(a, 0, out))
    assert [(r.names, r.storage_only) for r in rows] == [(("vco_ollama",), True),
                                                         (("vco_weaviate",), False)]
    assert containers.list_external_containers("podman", run=lambda a, **k: _cp(a, 125)) is None
    assert containers.list_external_containers("docker", run=lambda a, **k: pytest.fail()) is None

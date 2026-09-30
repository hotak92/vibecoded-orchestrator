# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-06 root-cause task (AD-5 / L2-F08): reproduce the 2026-09-29
container removal against a REAL podman — two candidate mechanisms:

  (i)  podman-compose `up` naming a service whose container_name is already
       held by a STOPPED container it may not own — (a) labelled by another
       compose project, (b) labelled by the SAME project with a stale
       config hash (the "leftover carrying the installer's label" candidate);
  (ii) podman stopping / cleaning up a container whose rootfs (`merged/`)
       unmount fails because a process holds it open.

This test RECORDS what happens (exit codes, stderr, `podman events`,
whether the original container id survived, `ps -a --external`) into a JSON
report; its assertions are only the safety invariants (nothing outside its
own uniquely-named objects changed). The CHANGELOG states guards only; a
mechanism is written down only once this report proves it.

SAFETY — it runs ONLY when explicitly asked:
  * skipped unless ``VCO_PODMAN_REPRO=1`` (podman being installed is NOT
    enough: a maintainer's full `pytest` on a machine with a live VCO stack
    must never run it);
  * skipped when podman or podman-compose is absent, or no suitable image is
    already present locally (it never pulls);
  * every object it creates is named ``wp06repro-<random>-…`` in a temp
    compose project of the same prefix; it never names, lists-for-action or
    removes anything else (no ``vco_*`` container, no existing volume, no
    ``prune``); it creates NO volume;
  * cleanup removes only the ids/names it created itself.

Run (by the orchestrator, when the owner agrees the live stack may be
touched by a throwaway container on the same podman):

    VCO_PODMAN_REPRO=1 VCO_PODMAN_REPRO_REPORT=/tmp/wp06-podman-repro.json \\
      PYTHONPATH=<public repo> <VCO_dev>/.venv/bin/python -m pytest \\
      tests/test_v02100_podman_removal_repro.py -q -p no:cacheprovider -s

then read the three reports `/tmp/wp06-podman-repro-{i-foreign_project,
i-same_project_stale_hash,ii-held-rootfs}.json` (and the `-s` output).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

PODMAN = shutil.which("podman")
PODMAN_COMPOSE = shutil.which("podman-compose")
OPT_IN = os.environ.get("VCO_PODMAN_REPRO") == "1"
#: Images tried, in order — only ones ALREADY present locally are used.
CANDIDATE_IMAGES = ("docker.io/library/alpine:latest", "docker.io/library/busybox:latest",
                    "docker.io/library/alpine:3.20", "quay.io/libpod/alpine:latest")
PREFIX = "wp06repro"

pytestmark = pytest.mark.skipif(
    not (OPT_IN and PODMAN and PODMAN_COMPOSE),
    reason="real-podman repro: set VCO_PODMAN_REPRO=1 on a host with podman + podman-compose "
           "(never runs by default — see the module docstring)",
)


def _run(argv, timeout=120, **kw) -> dict:
    """Run and RECORD (never raises on a non-zero exit)."""
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kw)
        return {"argv": argv, "rc": p.returncode, "stdout": p.stdout[-4000:],
                "stderr": p.stderr[-4000:], "secs": round(time.time() - t0, 2)}
    except subprocess.TimeoutExpired as e:
        return {"argv": argv, "rc": None, "timeout": timeout, "stdout": str(e.stdout)[-2000:],
                "stderr": str(e.stderr)[-2000:]}


def _mine(name: str) -> str:
    assert name.startswith(PREFIX + "-"), f"refusing to act on a name that is not the test's own: {name}"
    return name


def _all_names() -> set[str]:
    out = _run([PODMAN, "ps", "-a", "--external", "--format", "{{.Names}}"])
    return {n.strip() for n in out["stdout"].splitlines() if n.strip()}


def _foreign_snapshot() -> set[str]:
    """Every container name that is NOT this test's — must be identical
    before and after (the safety invariant)."""
    return {n for n in _all_names() if not n.startswith(PREFIX + "-")}


def _image() -> str:
    for img in CANDIDATE_IMAGES:
        if _run([PODMAN, "image", "exists", img])["rc"] == 0:
            return img
    pytest.skip("no small image present locally (the repro never pulls): "
                + ", ".join(CANDIDATE_IMAGES))


def _container_id(name: str) -> str:
    return _run([PODMAN, "inspect", "--type", "container", "--format", "{{.Id}}", _mine(name)])["stdout"].strip()


def _events(since: float, name: str) -> list:
    out = _run([PODMAN, "events", "--since", str(int(since)), "--until", str(int(time.time()) + 1),
                "--format", "json", "--filter", f"container={_mine(name)}"], timeout=30)
    rows = []
    for line in out["stdout"].splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            rows.append(line)
    return rows


class _Cleanup:
    def __init__(self):
        self.names: list[str] = []
        self.ids: list[str] = []
        self.networks: list[str] = []
        self.procs: list[subprocess.Popen] = []

    def run(self, report: dict):
        steps = []
        for p in self.procs:
            try:
                p.kill()
                p.wait(timeout=10)
            except Exception as exc:  # noqa: BLE001 — recorded
                steps.append(f"holder kill: {exc}")
        for n in self.names:
            steps.append(_run([PODMAN, "rm", "-f", "-t", "0", _mine(n)]))
        for cid in self.ids:
            # A storage-only leftover of OUR OWN container (recorded id).
            if cid:
                steps.append(_run([PODMAN, "rm", "--storage", cid]))
        for net in self.networks:
            steps.append(_run([PODMAN, "network", "rm", _mine(net)]))
        report["cleanup"] = steps


def _write_report(report: dict, name: str):
    base = Path(os.environ.get("VCO_PODMAN_REPRO_REPORT")
                or Path(tempfile.gettempdir()) / f"{PREFIX}-report.json")
    path = base.with_name(f"{base.stem}-{name}{base.suffix or '.json'}")
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n[wp06 repro] report: {path}")


def _compose_file(dirpath: Path, container: str, image: str) -> Path:
    f = dirpath / "docker-compose.yml"
    f.write_text(
        "services:\n"
        "  svc:\n"
        f"    image: {image}\n"
        f"    container_name: {_mine(container)}\n"
        "    command: [\"sleep\", \"3600\"]\n",
        encoding="utf-8",
    )
    return f


@pytest.mark.parametrize("variant", ["foreign_project", "same_project_stale_hash"])
def test_i_podman_compose_up_against_a_stopped_container_it_may_not_own(variant):
    report: dict = {"scenario": f"(i) {variant}", "podman": _run([PODMAN, "--version"])["stdout"].strip(),
                    "podman_compose": _run([PODMAN_COMPOSE, "--version"])["stdout"].strip()[-300:]}
    tag = uuid.uuid4().hex[:8]
    project = f"{PREFIX}-{tag}-proj"
    container = f"{PREFIX}-{tag}-c"
    label_project = f"{PREFIX}-{tag}-other" if variant == "foreign_project" else project
    image = _image()
    before = _foreign_snapshot()
    clean = _Cleanup()
    clean.names.append(container)
    clean.networks.append(f"{project}_default")
    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix=f"{PREFIX}-") as d:
            compose = _compose_file(Path(d), container, image)
            labels = ["--label", f"com.docker.compose.project={label_project}",
                      "--label", f"io.podman.compose.project={label_project}",
                      "--label", "com.docker.compose.service=svc"]
            if variant == "same_project_stale_hash":
                labels += ["--label", "io.podman.compose.config-hash=stale-" + tag]
            report["create"] = _run([PODMAN, "run", "-d", "--name", container, *labels, image, "sleep", "3600"])
            original = _container_id(container)
            clean.ids.append(original)
            report["original_id"] = original
            report["stop"] = _run([PODMAN, "stop", "-t", "0", container])
            report["compose_up"] = _run([PODMAN_COMPOSE, "-p", project, "-f", str(compose),
                                         "up", "-d", "--no-deps", "svc"], timeout=300, cwd=d)
            after_id = _container_id(container)
            report["id_after"] = after_id
            report["original_survived"] = _run([PODMAN, "container", "exists", original])["rc"] == 0
            report["external_after"] = _run([PODMAN, "ps", "-a", "--external", "--format", "json",
                                            "--filter", f"name={container}"])["stdout"]
            if after_id and after_id != original:
                clean.ids.append(after_id)
    finally:
        report["events"] = _events(t0 - 1, container)
        clean.run(report)
        report["foreign_unchanged"] = _foreign_snapshot() == before
        _write_report(report, f"i-{variant}")
    assert report["foreign_unchanged"], "the repro touched a container that is not its own"


def test_ii_stop_of_a_container_whose_rootfs_is_held_open():
    report: dict = {"scenario": "(ii) stop with merged/ held open",
                    "podman": _run([PODMAN, "--version"])["stdout"].strip()}
    tag = uuid.uuid4().hex[:8]
    container = f"{PREFIX}-{tag}-held"
    image = _image()
    before = _foreign_snapshot()
    clean = _Cleanup()
    clean.names.append(container)
    t0 = time.time()
    try:
        report["create"] = _run([PODMAN, "run", "-d", "--name", container, image, "sleep", "3600"])
        cid = _container_id(container)
        clean.ids.append(cid)
        report["id"] = cid
        # Hold the rootfs open from inside podman's user namespace: a shell
        # whose cwd is the container's merged/ directory.
        holder = subprocess.Popen(
            [PODMAN, "unshare", "sh", "-c", f'cd "$({PODMAN} mount {cid})" && exec sleep 120'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        clean.procs.append(holder)
        time.sleep(3)
        report["holder_alive"] = holder.poll() is None
        report["stop"] = _run([PODMAN, "stop", "-t", "2", container], timeout=90)
        report["inspect_after_stop"] = _run([PODMAN, "inspect", "--type", "container", "--format",
                                             "{{.State.Status}}", container])
        report["external_after_stop"] = _run([PODMAN, "ps", "-a", "--external", "--format", "json",
                                              "--filter", f"name={container}"])["stdout"]
        report["start_again"] = _run([PODMAN, "start", container], timeout=90)
    finally:
        report["events"] = _events(t0 - 1, container)
        clean.run(report)
        report["foreign_unchanged"] = _foreign_snapshot() == before
        _write_report(report, "ii-held-rootfs")
    assert report["foreign_unchanged"], "the repro touched a container that is not its own"

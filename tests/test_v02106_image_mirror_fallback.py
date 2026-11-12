# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 (owner ruling 2026-11-12): a pull refused by a registry rate limit
is retried from Google's Docker Hub mirror (``mirror.gcr.io``) in the ONE pull
home ``compose_image_prepull.ensure_image``.

Field case: a fresh macOS install's anonymous pull of
``cr.weaviate.io/semitechnologies/weaviate:1.37.18`` (served by Docker Hub)
failed with "toomanyrequests: You have reached your unauthenticated pull rate
limit".
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vco_lib import compose_image_prepull as pp

WEAVIATE = "cr.weaviate.io/semitechnologies/weaviate:1.37.18"
WEAVIATE_MIRROR = "mirror.gcr.io/semitechnologies/weaviate:1.37.18"
RATE = ("Error: initializing source docker://cr.weaviate.io/semitechnologies/weaviate:1.37.18: "
        "reading manifest 1.37.18: toomanyrequests: You have reached your unauthenticated "
        "pull rate limit. https://www.docker.com/increase-rate-limit")
D_AMD64 = "sha256:" + "a" * 64
D_ARM64 = "sha256:" + "b" * 64
D_OTHER = "sha256:" + "c" * 64


def _registry(ref: str) -> str:
    """The registry host an image reference names (its first path segment)."""
    return ref.split("/", 1)[0]


def _names_mirror(argv: list[str]) -> bool:
    """``argv`` names an image ON the mirror (exact registry host, not a
    substring anywhere in the command line)."""
    return any(_registry(a) == pp.MIRROR_HOST for a in argv)


class Registry:
    """A fake engine: which pulls work, what ``manifest inspect`` reads."""

    def __init__(self, *, refuse=RATE, mirror_ok=True, origin_manifest=None,
                 mirror_manifest=None, mirror_repo_digests=()):
        self.refuse, self.mirror_ok = refuse, mirror_ok
        self.origin_manifest, self.mirror_manifest = origin_manifest, mirror_manifest
        self.mirror_repo_digests = list(mirror_repo_digests)
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        verb = argv[1:3]
        if verb == ["image", "inspect"]:
            if "--format" in argv:
                out = "[" + ",".join(f'"{WEAVIATE_MIRROR.split(":")[0]}@{d}"'
                                     for d in self.mirror_repo_digests) + "]"
                return subprocess.CompletedProcess(argv, 0, out, "")
            return subprocess.CompletedProcess(argv, 125, "", "image not known")
        if argv[1] == "pull":
            if _registry(argv[2]) != pp.MIRROR_HOST:
                return subprocess.CompletedProcess(argv, 125, "", self.refuse)
            return subprocess.CompletedProcess(argv, 0 if self.mirror_ok else 125, "",
                                               "" if self.mirror_ok else "mirror down")
        if verb == ["manifest", "inspect"]:
            body = self.origin_manifest if argv[3] == WEAVIATE else self.mirror_manifest
            if body is None:
                return subprocess.CompletedProcess(argv, 125, "", RATE)
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[1] in ("tag", "rmi"):
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


def _index(*digests):
    return '{"manifests": [' + ",".join(f'{{"digest": "{d}"}}' for d in digests) + "]}"


@pytest.mark.parametrize("image,mirror", [
    (WEAVIATE, WEAVIATE_MIRROR),
    ("docker.io/ollama/ollama:0.40.5", "mirror.gcr.io/ollama/ollama:0.40.5"),
    ("registry-1.docker.io/ollama/ollama:0.40.5", "mirror.gcr.io/ollama/ollama:0.40.5"),
    ("docker.io/redis:7", "mirror.gcr.io/library/redis:7"),
    ("redis:7", "mirror.gcr.io/library/redis:7"),
    ("ns/img:1", "mirror.gcr.io/ns/img:1"),
    ("docker.io/library/alpine@" + D_AMD64, "mirror.gcr.io/library/alpine@" + D_AMD64),
    ("ghcr.io/org/img:1", None),
    ("quay.io/org/img:1", None),
    ("localhost/vco-code-embed:latest", None),
    ("localhost:5000/img:1", None),
])
def test_mirror_ref_keeps_the_repository_path_and_tag(image, mirror):
    assert pp.mirror_ref(image) == mirror


def test_rate_limit_pulls_the_mirror_and_tags_it_unverified_when_origin_unreadable():
    reg, notes = Registry(), []
    assert pp.ensure_image("podman", WEAVIATE, run=reg, note=notes.append) == (True, "")
    assert ["podman", "pull", WEAVIATE_MIRROR] in reg.calls
    assert reg.calls[-1] == ["podman", "tag", WEAVIATE_MIRROR, WEAVIATE]
    (line,) = notes
    assert WEAVIATE_MIRROR in line and "unverified" in line


def test_rate_limit_with_a_matching_digest_is_accepted_and_says_verified():
    reg, notes = Registry(origin_manifest=_index(D_AMD64, D_ARM64),
                          mirror_manifest=_index(D_AMD64, D_ARM64)), []
    assert pp.ensure_image("docker", WEAVIATE, run=reg, note=notes.append) == (True, "")
    assert ["docker", "tag", WEAVIATE_MIRROR, WEAVIATE] in reg.calls
    assert "digest matches" in notes[0]


def test_local_repo_digest_counts_when_the_mirror_manifest_is_unreadable():
    reg = Registry(origin_manifest=_index(D_AMD64, D_ARM64), mirror_manifest=None,
                   mirror_repo_digests=[D_ARM64])
    assert pp.ensure_image("podman", WEAVIATE, run=reg)[0] is True


def test_a_digest_mismatch_is_refused_and_the_mirror_image_removed():
    reg = Registry(origin_manifest=_index(D_AMD64, D_ARM64), mirror_manifest=_index(D_OTHER),
                   mirror_repo_digests=[D_OTHER])
    ok, err = pp.ensure_image("podman", WEAVIATE, run=reg)
    assert ok is False and "does not match" in err and "toomanyrequests" in err
    assert ["podman", "rmi", WEAVIATE_MIRROR] in reg.calls
    assert not [c for c in reg.calls if c[1] == "tag"]


def test_a_non_rate_limit_failure_never_tries_the_mirror():
    reg = Registry(refuse="Error: unauthorized: authentication required")
    ok, err = pp.ensure_image("podman", WEAVIATE, run=reg)
    assert (ok, err) == (False, "Error: unauthorized: authentication required")
    assert not [c for c in reg.calls if _names_mirror(c)]


def test_a_failed_mirror_pull_keeps_the_original_refusal():
    ok, err = pp.ensure_image("podman", WEAVIATE, run=Registry(mirror_ok=False))
    assert ok is False and err.startswith(RATE) and "mirror down" in err


def test_a_non_docker_hub_image_has_no_mirror():
    reg = Registry()
    ok, err = pp.ensure_image("podman", "ghcr.io/org/img:1", run=reg)
    assert ok is False and not [c for c in reg.calls if _names_mirror(c)]


def test_recreate_path_reports_and_logs_the_mirror(capsys):
    """drop_unpullable_recreates: the rate-limited service is NOT dropped —
    the mirror supplied its image — and the line + install-log event name it."""
    events, services = [], ["weaviate"]
    pp.drop_unpullable_recreates(
        services, runtime="podman", root=Path("/r"), deferral_report=None, rerun="x",
        run=Registry(), log_event=lambda *a, **k: events.append((a, k)))
    assert services == ["weaviate"]
    assert WEAVIATE_MIRROR in capsys.readouterr().out
    assert events and events[0][1]["data"]["mirror"] == "mirror.gcr.io"


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    monkeypatch.setattr("vco_lib.service_adoption.pinned_image",
                        lambda svc, root: WEAVIATE if svc == "weaviate" else None)


def test_start_path_prepulls_through_the_mirror():
    """A fresh install STARTS weaviate (no recreate): prepull_starts gets the
    image in place before compose up would hit the rate limit."""
    reg = Registry()
    pp.prepull_starts(["weaviate", "code_embed"], runtime="podman", root=Path("/r"), run=reg)
    assert ["podman", "tag", WEAVIATE_MIRROR, WEAVIATE] in reg.calls


def test_start_path_failure_changes_nothing():
    reg = Registry(refuse="Error: no route to host")
    pp.prepull_starts(["weaviate"], runtime="podman", root=Path("/r"), run=reg)
    assert [c[1] for c in reg.calls] == ["image", "pull"]

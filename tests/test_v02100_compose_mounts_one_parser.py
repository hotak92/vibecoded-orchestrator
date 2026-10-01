# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 F-W2-13 / F-W3-10: ONE parser of compose mount entries.

``data_identity.render_mount`` (strict, the recreate guard) and
``service_adoption.config_mounts`` (the adoption's per-destination gate) both
read entries through :mod:`vco_lib.compose_mounts`. These tests pin that the
two readers AGREE on every successful shape of the recorded render corpus
(the drift the review found: an unnamed volume resolved two different ways),
that the third copy of the bind-source rule delegates, and that the
migration no longer re-reads the mount from a Python merge after the guard
proved it from the provider's own render.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from vco_lib import compose_mounts as cm
from vco_lib import data_identity as di
from vco_lib import runtime_reconcile as rr
from vco_lib import service_adoption as sa
from vco_lib import service_lifecycle as sl

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS = json.loads((REPO_ROOT / "tests" / "fixtures" / "compose_config_render_corpus.json")
                    .read_text(encoding="utf-8"))
#: Cases the strict reader resolves without a project or a compose directory —
#: the inputs on which the lenient per-destination reader must agree exactly.
AGREEING = [
    c for c in CORPUS["cases"]
    if c.get("expect_mount") and "{infra}" not in c["expect_mount"]["source"]
    and not c["expect_mount"]["source"].startswith(f"{c['project']}_")
]


@pytest.mark.parametrize("case", AGREEING, ids=lambda c: c["name"])
def test_both_readers_agree_on_every_resolvable_corpus_shape(case) -> None:
    doc = yaml.safe_load(case["render"])
    strict, error = di.render_mount(doc, case["service"], project=case["project"])
    assert error == "", error
    lenient = sa.config_mounts(doc["services"][case["service"]], doc.get("volumes") or {})
    dest = di.destination(case["service"])
    got = lenient[dest]
    assert (got.kind, got.source, got.destination) == (
        strict["kind"], strict["source"], strict["destination"])


def test_the_agreeing_set_covers_both_engines_and_windows() -> None:
    names = " | ".join(c["name"] for c in AGREEING)
    for shape in ("docker compose v2", "podman-compose", "Windows", "external"):
        assert shape in names, shape


def test_the_legacy_external_name_form_resolves_in_the_adoption_reader_too() -> None:
    # Before the shared parser, config_mounts read only `name:` and returned
    # the KEY for `external: {name: X}` — a mount the live container never has.
    svc = {"volumes": ["models:/root/.ollama"]}
    top = {"models": {"external": {"name": "shared_ollama_models"}}}
    assert sa.config_mounts(svc, top)["/root/.ollama"].source == "shared_ollama_models"


def test_an_unreadable_entry_is_skipped_by_the_lenient_reader_and_refused_by_the_strict() -> None:
    svc = {"volumes": [42, "/srv/models:/root/.ollama"]}
    assert set(sa.config_mounts(svc, {})) == {"/root/.ollama"}
    _mount, error = di.render_mount({"services": {"ollama": svc}}, "ollama")
    assert "neither" not in error and "not a mount string or mapping" in error


@pytest.mark.parametrize("source,expected", [
    ("/srv/x", True), ("./rel", True), ("~/x", True), ("\\\\server\\share", True),
    ("C:\\Users\\x", True), ("c:/x", True), ("vco_ollama_data", False), ("v", False),
])
def test_one_bind_source_rule_everywhere(source: str, expected: bool) -> None:
    assert cm.is_bind_source(source) is expected
    assert rr._is_bind_source(source) is expected
    assert sa._is_bind_source(source) is expected


def test_the_migration_reads_the_effective_mount_only_through_the_guard() -> None:
    # The second, Python-merge read of the same question after the guard's
    # `compose config` proof is gone (one home: data_identity.guard_recreate).
    assert not hasattr(sl, "_effective_mount")

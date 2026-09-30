# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-06 (AD-5 / AD-12): who owns a container, Python leg.

`tests/fixtures/container_ownership_parity.json` is executed here against
`vco_lib/containers.py` (`compose_label_family`, `ComposeIdentity` — its
`provider` field — `foreign_compose_identity`, `compose_project_name`) and
`vco_lib/service_lifecycle.py::zombie_action`, and in Rust by
`launcher/src-tauri/vct-launcher-core/src/services/container_ownership.rs`'s
tests (the label parse, `ownership`, `compose_project_name`,
`service_endpoints::zombie_action_given`). A change to either side that the
table does not follow turns one of the two legs red.

Two columns are Rust-only by construction and say so here rather than being
silently skipped: the Rust `ownership` returns `unknown` for an unreadable
container (Python's `compose_identity_of` folds "unreadable" into `None`,
which `foreign_compose_identity` then reads as foreign — the conservative
answer on the Python side, where the caller never acts on it), and the
zombie verb given ownership (`expect_on_zombie`) exists only in Rust — the
Python recreate path is gated by the data-identity guard instead
(`vco_lib.data_identity.guard_recreate`). For that column the Python leg pins
the BASE verb the Rust rule starts from.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from vco_lib import containers
from vco_lib import service_endpoints as se
from vco_lib import service_lifecycle

REPO_ROOT = Path(__file__).resolve().parent.parent
TABLE = json.loads((REPO_ROOT / "tests" / "fixtures" / "container_ownership_parity.json").read_text(encoding="utf-8"))


def _identity(labels) -> "containers.ComposeIdentity | None":
    """The ComposeIdentity `compose_identity_of` would build from these
    labels (its own parse: project from `com.docker.compose.project`,
    provider from `compose_label_family`)."""
    labels = labels or {}
    project = (labels.get(containers.COMPOSE_PROJECT_LABEL) or "").strip()
    if not project:
        return None
    return containers.ComposeIdentity(
        project=project,
        provider=containers.compose_label_family(
            labels.get(containers.PODMAN_COMPOSE_PROJECT_LABEL, ""),
            labels.get(containers.DOCKER_COMPOSE_CONFIG_HASH_LABEL, ""),
        ),
    )


def _row(service: str, spec) -> "se.EndpointRow | None":
    if spec is None:
        return None
    defaults = {"weaviate": 8081, "ollama": 11435, "code_embed": 11440}
    return se.EndpointRow(
        service=service,
        mode=spec["mode"],
        port=defaults[service],
        grpc_port=50052 if service == "weaviate" else None,
        container_name=spec.get("container_name"),
        compose_project=spec.get("compose_project"),
        enabled=spec.get("enabled", True),
        autostart=spec.get("autostart", True),
        source="install_probe",
    )


class ContainerOwnershipParity(unittest.TestCase):
    def test_label_keys_match(self):
        labels = TABLE["labels"]
        self.assertEqual(labels["compose_project"], containers.COMPOSE_PROJECT_LABEL)
        self.assertEqual(labels["podman_compose_project"], containers.PODMAN_COMPOSE_PROJECT_LABEL)
        self.assertEqual(labels["docker_config_hash"], containers.DOCKER_COMPOSE_CONFIG_HASH_LABEL)

    def test_identity_cases(self):
        for case in TABLE["identity_cases"]:
            with self.subTest(case["name"]):
                labels = case["labels"] or {}
                self.assertEqual(
                    containers.compose_label_family(
                        labels.get(containers.PODMAN_COMPOSE_PROJECT_LABEL, ""),
                        labels.get(containers.DOCKER_COMPOSE_CONFIG_HASH_LABEL, ""),
                    ),
                    case["expect_provider"],
                )
                ident = _identity(case["labels"])
                self.assertEqual(ident.project if ident else None, case["expect_project"])

    def test_identity_reader_carries_the_provider(self):
        """`compose_identity_of` itself (with a scripted runtime) fills
        `ComposeIdentity.provider` from the same labels the table uses."""
        import subprocess

        case = TABLE["identity_cases"][0]
        labels = case["labels"]

        def run(argv, **_kw):
            fields = [labels.get(containers.COMPOSE_PROJECT_LABEL, ""), "", "",
                      labels.get(containers.PODMAN_COMPOSE_PROJECT_LABEL, ""),
                      labels.get(containers.DOCKER_COMPOSE_CONFIG_HASH_LABEL, "")]
            return subprocess.CompletedProcess(argv, 0, "\t".join(fields) + "\n", "")

        from unittest import mock

        with mock.patch.object(containers, "_resolve_runtime", return_value="podman"):
            ident = containers.compose_identity_of("vco_code_embed", "podman", run=run)
        self.assertEqual((ident.project, ident.provider), (case["expect_project"], case["expect_provider"]))

    def test_project_name_cases(self):
        for case in TABLE["project_name_cases"]:
            with self.subTest(case["name"]):
                self.assertEqual(
                    containers.compose_project_name(Path(case["dir"]), case["compose_text"]),
                    case["expect"],
                )

    def test_ownership_cases(self):
        for case in TABLE["ownership_cases"]:
            with self.subTest(case["name"]):
                row = _row(case["service"], case["row"])
                own_project = (row.compose_project if row and row.compose_project else None) \
                    or case["installer_project"]
                verdict = containers.foreign_compose_identity(_identity(case["labels"]), own_project)
                self.assertEqual("owned" if verdict is None else "foreign", case["expect_ownership"])

    def test_zombie_base_verb_matches_the_rust_rules_input(self):
        """Rust's `zombie_action_given` = the base verb, downgraded to
        `start` when not owned. The base is Python's `zombie_action`."""
        for case in TABLE["ownership_cases"]:
            with self.subTest(case["name"]):
                row = _row(case["service"], case["row"])
                rows = {case["service"]: row} if row is not None else {}
                base = service_lifecycle.zombie_action(rows, case["service"])
                want = case["expect_on_zombie"]
                if case["expect_ownership"] == "owned":
                    self.assertEqual(base, want)
                else:
                    self.assertEqual(want, "start" if base == "recreate" else base)


if __name__ == "__main__":
    unittest.main()

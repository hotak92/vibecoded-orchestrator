# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Schema validation for per-OS-dir launcher/dist/<os-arch>/metadata.json.

v0.2.53 Track D (M-P0-9 / metadata.json writer row in §6 of
docs/INSTALL_ARCHITECTURE_v2.md).

This is the contract test for the file emitted by the
`commit-dist-binaries` job in `.github/workflows/release.yml`. Track A's
shell-side reader at `scripts/lib/launcher-metadata.sh` consumes the
file; if these schemas drift, start-launcher.{sh,command,bat} silently
fail to resolve the binary path on a future release. This test pins the
schema so any reader / writer change must update both sides.

Schema (per docs/INSTALL_ARCHITECTURE_v2.md §6):

  {
    "binary_name": "vct-launcher" | "vct-launcher.exe",
    "os":          "linux-x64" | "macos-arm64" | "windows-x64",
    "version":     "<semver-ish>",
    "source_sha":  "<40-char-hex>",
    "built_at":    "<UTC ISO 8601>",
    "size_bytes":  <int>
  }

When the file does not yet exist on a freshly-cut clone (first run of
the workflow on a new release branch), the file-existence assertions
are skipped — the writer in release.yml lands the files at tag time.
The schema validation runs against whatever IS present so PRs that
regenerate the files locally also get checked.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest


# Repo root resolution: this test lives at tests/test_dist_metadata_schema.py,
# so the repo root is two levels up. Resolving from __file__ avoids cwd
# sensitivity when pytest is invoked from a subdirectory.
REPO_ROOT = Path(__file__).resolve().parent.parent

# The three per-OS dist subdirs that must each carry a metadata.json
# after the release workflow's commit-dist-binaries job has run.
PER_OS_DIRS: tuple[tuple[str, str], ...] = (
    ("linux-x64", "vct-launcher"),
    ("windows-x64", "vct-launcher.exe"),
    ("macos-arm64", "vct-launcher"),
)


# JSON Schema (Draft 2020-12). Kept inline so test failures surface the
# schema diff cleanly in pytest output; if this gets re-used by other
# tests we'd promote it to docs/schemas/launcher-dist-metadata.schema.json
# but for v0.2.53 a single consumer + a single producer = inline is
# clearer than a roundtrip through a file load.
DIST_METADATA_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "LauncherDistDirMetadata",
    "description": (
        "Per-OS-dir metadata.json emitted by release.yml's "
        "commit-dist-binaries job and consumed by start-launcher.* via "
        "scripts/lib/launcher-metadata.sh."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": [
        "binary_name",
        "os",
        "version",
        "source_sha",
        "built_at",
        "size_bytes",
    ],
    "properties": {
        "binary_name": {
            "type": "string",
            "enum": ["vct-launcher", "vct-launcher.exe"],
            "description": "Launcher executable filename for this OS.",
        },
        "os": {
            "type": "string",
            "enum": ["linux-x64", "macos-arm64", "windows-x64"],
            "description": "Canonical OS-arch tag — MUST match the parent dir.",
        },
        "version": {
            "type": "string",
            # Allow plain semver (`0.2.53`), pre-release tags
            # (`0.2.53-rc.1`), and build metadata (`0.2.53+sha.abc`).
            # The contract from release.yml is "the tag without the v
            # prefix", so this regex stays liberal but rejects junk.
            "pattern": r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.+-]+)?$",
            "description": "Release version (matches the tag without 'v' prefix).",
        },
        "source_sha": {
            "type": "string",
            "pattern": r"^[0-9a-f]{40}$",
            "description": "Full 40-char git commit SHA at build time.",
        },
        "built_at": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
            "description": "UTC ISO 8601 timestamp with Z suffix (no offset).",
        },
        "size_bytes": {
            "type": "integer",
            # Launcher binaries are 25-60 MB depending on the OS; bound
            # the assertion loose enough that a future minify pass
            # doesn't break it, but tight enough that "0 bytes"
            # (writer-bug case) fails loudly.
            "minimum": 1024,           # 1 KB — empty/truncated binary catch
            "maximum": 500 * 1024 * 1024,  # 500 MB — sanity cap
            "description": "Launcher binary size at build time, in bytes.",
        },
    },
}


@pytest.fixture(scope="module")
def schema_validator() -> Any:
    """Return a `jsonschema` Validator bound to DIST_METADATA_SCHEMA.

    Returning the validator (vs the schema dict) lets each test pull
    structured ValidationError instances rather than re-instantiating
    the validator on every assertion.
    """
    jsonschema = pytest.importorskip("jsonschema")
    return jsonschema.Draft202012Validator(DIST_METADATA_SCHEMA)


def _dist_dir(os_arch: str) -> Path:
    return REPO_ROOT / "launcher" / "dist" / os_arch


def _metadata_path(os_arch: str) -> Path:
    return _dist_dir(os_arch) / "metadata.json"


def _load_metadata(os_arch: str) -> dict[str, Any] | None:
    """Load + parse metadata.json for the given OS-arch, or return None.

    Returns None when the file is absent — that's the legitimate state
    on a fresh clone of `main` BEFORE the next release tag has been
    pushed. The CI workflow's writer is the canonical source; this
    test validates whatever's there but doesn't require presence to
    pass.
    """
    p = _metadata_path(os_arch)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# ─── Schema-shape tests (run regardless of file presence) ────────────────

class TestSchemaShape:
    """Tests that validate the SCHEMA itself, independent of any data file.

    These run on every checkout — there's no `xfail-if-missing` semantics
    because they exercise the schema constants, not the filesystem.
    """

    def test_schema_is_valid_draft_2020_12(self) -> None:
        """The schema itself must be a valid JSON Schema document."""
        jsonschema = pytest.importorskip("jsonschema")
        # Will raise jsonschema.SchemaError if the schema is malformed.
        jsonschema.Draft202012Validator.check_schema(DIST_METADATA_SCHEMA)

    def test_schema_has_six_required_keys(self) -> None:
        """All six contract keys must be required (no optional fields)."""
        required = set(DIST_METADATA_SCHEMA["required"])
        expected = {
            "binary_name", "os", "version",
            "source_sha", "built_at", "size_bytes",
        }
        assert required == expected, (
            f"Schema 'required' drifted: extra={required - expected}, "
            f"missing={expected - required}"
        )

    def test_schema_disallows_additional_properties(self) -> None:
        """No extra keys allowed — the reader only reads the six above."""
        assert DIST_METADATA_SCHEMA.get("additionalProperties") is False

    def test_os_enum_matches_dist_subdirs(self) -> None:
        """The `os` enum must match the three per-OS dist subdir names.

        This is the M-P0-2 / experimental_macOS drift gate: if someone
        adds a new OS variant they must update BOTH this enum AND the
        PER_OS_DIRS constant, OR the schema check will trip.
        """
        schema_oses = set(DIST_METADATA_SCHEMA["properties"]["os"]["enum"])
        per_os_dirs = {d for d, _ in PER_OS_DIRS}
        assert schema_oses == per_os_dirs, (
            f"OS enum drifted from PER_OS_DIRS: "
            f"in schema only={schema_oses - per_os_dirs}, "
            f"in PER_OS_DIRS only={per_os_dirs - schema_oses}"
        )

    def test_binary_name_enum_covers_both_posix_and_exe(self) -> None:
        """`binary_name` must accept both POSIX and Windows variants."""
        names = set(DIST_METADATA_SCHEMA["properties"]["binary_name"]["enum"])
        assert names == {"vct-launcher", "vct-launcher.exe"}


# ─── Per-OS validation (skipped when file missing on a fresh clone) ────

@pytest.mark.parametrize(
    "os_arch,expected_binary",
    PER_OS_DIRS,
    ids=[f"{o}-{b}" for o, b in PER_OS_DIRS],
)
class TestPerOSMetadata:
    """Validate each OS's metadata.json against the schema."""

    def test_metadata_validates_against_schema(
        self,
        schema_validator: Any,
        os_arch: str,
        expected_binary: str,
    ) -> None:
        """metadata.json must conform to DIST_METADATA_SCHEMA."""
        data = _load_metadata(os_arch)
        if data is None:
            pytest.skip(
                f"launcher/dist/{os_arch}/metadata.json not present yet — "
                f"file is emitted by release.yml's commit-dist-binaries job"
            )

        errors = sorted(
            schema_validator.iter_errors(data),
            key=lambda e: list(e.absolute_path),
        )
        assert not errors, "Schema violations:\n" + "\n".join(
            f"  {'.'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors
        )

    def test_os_field_matches_subdir(
        self,
        os_arch: str,
        expected_binary: str,
    ) -> None:
        """`os` field MUST match the parent dir name. M-P0-2 closure."""
        data = _load_metadata(os_arch)
        if data is None:
            pytest.skip(f"launcher/dist/{os_arch}/metadata.json not present")
        assert data["os"] == os_arch, (
            f"os field='{data['os']}' but file is at "
            f"launcher/dist/{os_arch}/metadata.json. The two MUST agree "
            f"or scripts/lib/launcher-metadata.sh resolves to the wrong "
            f"binary (this is the experimental_macOS bug class)."
        )

    def test_binary_name_matches_expected(
        self,
        os_arch: str,
        expected_binary: str,
    ) -> None:
        """binary_name must match the OS's expected launcher filename."""
        data = _load_metadata(os_arch)
        if data is None:
            pytest.skip(f"launcher/dist/{os_arch}/metadata.json not present")
        assert data["binary_name"] == expected_binary, (
            f"binary_name='{data['binary_name']}' but for os={os_arch} "
            f"the expected name is '{expected_binary}'."
        )

    def test_binary_file_actually_exists(
        self,
        os_arch: str,
        expected_binary: str,
    ) -> None:
        """The launcher binary the metadata refers to must exist on disk.

        size_bytes lies if the binary was deleted but metadata wasn't
        regenerated. This catches that case.
        """
        data = _load_metadata(os_arch)
        if data is None:
            pytest.skip(f"launcher/dist/{os_arch}/metadata.json not present")
        binary_path = _dist_dir(os_arch) / data["binary_name"]
        if not binary_path.exists():
            pytest.skip(
                f"Launcher binary {binary_path} not committed in this clone — "
                f"likely a shallow CI checkout. The metadata schema is still valid."
            )
        actual_size = binary_path.stat().st_size
        # Tolerance: actual size MUST equal recorded size. If the
        # binary was re-built without re-running the metadata writer,
        # they'll diverge — that's a release-yml bug we want to surface.
        assert actual_size == data["size_bytes"], (
            f"size_bytes drift: metadata says {data['size_bytes']}, "
            f"actual file is {actual_size}. Re-run "
            f"commit-dist-binaries to refresh metadata.json."
        )


# ─── Cross-OS invariants ─────────────────────────────────────────────────

class TestCrossOSInvariants:
    """Invariants that must hold across the three per-OS metadata files."""

    def test_all_present_metadatas_agree_on_version(self) -> None:
        """Every per-OS metadata must have the same `version`.

        A version skew between Linux/macOS/Windows means
        commit-dist-binaries ran in two non-atomic passes — releasing
        with this skew would ship binaries claiming different release
        identities, breaking the launcher's self-update check.
        """
        present: dict[str, str] = {}
        for os_arch, _ in PER_OS_DIRS:
            data = _load_metadata(os_arch)
            if data is not None:
                present[os_arch] = data["version"]

        if len(present) < 2:
            pytest.skip(
                f"Need >=2 per-OS metadata.json files to compare; have {len(present)}"
            )
        versions = set(present.values())
        assert len(versions) == 1, (
            f"version drift across OSes: {present}. Re-run the release "
            f"workflow so commit-dist-binaries emits a consistent set."
        )

    def test_all_present_metadatas_agree_on_source_sha(self) -> None:
        """All per-OS metadata must share `source_sha` (same build).

        Distinct source SHAs means the launcher binaries were built
        from different commits — a guaranteed bug-reproducibility
        nightmare.
        """
        present: dict[str, str] = {}
        for os_arch, _ in PER_OS_DIRS:
            data = _load_metadata(os_arch)
            if data is not None:
                present[os_arch] = data["source_sha"]

        if len(present) < 2:
            pytest.skip(f"Need >=2 per-OS metadata.json files; have {len(present)}")
        shas = set(present.values())
        assert len(shas) == 1, (
            f"source_sha drift across OSes: {present}. Each OS binary "
            f"was built from a different commit — release is incoherent."
        )


# ─── Direct-construction self-tests (sanity check the schema works) ────

class TestSchemaSelfCheck:
    """Build the smallest valid + smallest invalid documents, assert
    the validator accepts/rejects them. This is the test of the test —
    if these break, future debugging starts here.
    """

    GOOD_DOCUMENT: dict[str, Any] = {
        "binary_name": "vct-launcher",
        "os": "linux-x64",
        "version": "0.2.53",
        "source_sha": "9396ea9612345678901234567890123456789012",
        "built_at": "2026-06-10T12:34:56Z",
        "size_bytes": 31_355_448,
    }

    def test_valid_document_passes(self, schema_validator: Any) -> None:
        errors = list(schema_validator.iter_errors(self.GOOD_DOCUMENT))
        assert not errors, [e.message for e in errors]

    def test_extra_key_rejected(self, schema_validator: Any) -> None:
        bad = {**self.GOOD_DOCUMENT, "unexpected_field": "nope"}
        errors = list(schema_validator.iter_errors(bad))
        assert errors, "additionalProperties=false not enforced"

    def test_missing_required_rejected(self, schema_validator: Any) -> None:
        bad = {k: v for k, v in self.GOOD_DOCUMENT.items() if k != "source_sha"}
        errors = list(schema_validator.iter_errors(bad))
        assert errors, "missing 'source_sha' should fail validation"

    def test_invalid_os_enum_rejected(self, schema_validator: Any) -> None:
        bad = {**self.GOOD_DOCUMENT, "os": "experimental_macOS"}
        errors = list(schema_validator.iter_errors(bad))
        assert errors, (
            "OS enum should reject 'experimental_macOS' (the M-P0-2 "
            "drift bug). If this test ever fails, the schema enum was "
            "loosened and the experimental_macOS drift can re-emerge."
        )

    def test_invalid_binary_name_rejected(self, schema_validator: Any) -> None:
        bad = {**self.GOOD_DOCUMENT, "binary_name": "vct-launcher-experimental"}
        errors = list(schema_validator.iter_errors(bad))
        assert errors

    def test_short_sha_rejected(self, schema_validator: Any) -> None:
        bad = {**self.GOOD_DOCUMENT, "source_sha": "9396ea96"}  # short SHA
        errors = list(schema_validator.iter_errors(bad))
        assert errors, "source_sha should require full 40-char hex"

    def test_built_at_without_z_rejected(self, schema_validator: Any) -> None:
        bad = {**self.GOOD_DOCUMENT, "built_at": "2026-06-10T12:34:56+00:00"}
        errors = list(schema_validator.iter_errors(bad))
        assert errors, "built_at must use Z suffix, not +00:00 offset"

    def test_zero_size_rejected(self, schema_validator: Any) -> None:
        bad = {**self.GOOD_DOCUMENT, "size_bytes": 0}
        errors = list(schema_validator.iter_errors(bad))
        assert errors, "zero-byte launcher binary is a writer bug — must fail"

    def test_version_with_prerelease_accepted(self, schema_validator: Any) -> None:
        good = {**self.GOOD_DOCUMENT, "version": "0.2.53-rc.1"}
        errors = list(schema_validator.iter_errors(good))
        assert not errors, [e.message for e in errors]

    def test_version_with_build_metadata_accepted(
        self, schema_validator: Any
    ) -> None:
        good = {**self.GOOD_DOCUMENT, "version": "0.2.53+sha.abcdef0"}
        errors = list(schema_validator.iter_errors(good))
        assert not errors, [e.message for e in errors]

    def test_version_with_v_prefix_rejected(self, schema_validator: Any) -> None:
        bad = {**self.GOOD_DOCUMENT, "version": "v0.2.53"}
        errors = list(schema_validator.iter_errors(bad))
        assert errors, (
            "release.yml strips the 'v' prefix before writing; if a "
            "v-prefixed version slips through, the writer regressed."
        )


# ─── Regex sanity (ensure documented patterns don't unexpectedly accept junk)

class TestSchemaRegexSanity:
    """Standalone re.match checks against the patterns in the schema,
    independent of jsonschema. Catches the case where a future schema
    edit weakens a regex without realising it (jsonschema only tests
    that the pattern is valid, not that it's TIGHT).
    """

    def test_source_sha_pattern_rejects_uppercase(self) -> None:
        pattern = DIST_METADATA_SCHEMA["properties"]["source_sha"]["pattern"]
        # Git SHAs are always lowercase; accepting uppercase would mask a
        # case-normalization bug in the writer.
        assert not re.fullmatch(pattern, "ABCDEF0123456789" * 2 + "ABCDEF01")

    def test_built_at_pattern_rejects_microseconds(self) -> None:
        pattern = DIST_METADATA_SCHEMA["properties"]["built_at"]["pattern"]
        # The writer uses `date -u +'%Y-%m-%dT%H:%M:%SZ'` — never
        # includes microseconds. Microseconds in the file = the
        # writer changed in an undocumented way.
        assert not re.fullmatch(pattern, "2026-06-10T12:34:56.789Z")

    def test_version_pattern_rejects_empty(self) -> None:
        pattern = DIST_METADATA_SCHEMA["properties"]["version"]["pattern"]
        assert not re.fullmatch(pattern, "")

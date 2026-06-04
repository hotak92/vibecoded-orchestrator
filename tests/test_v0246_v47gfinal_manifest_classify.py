"""v0.2.46 post-adversarial L2 — `.vco-manifest.json` defensive validation.

Pins ``_v47g_classify_manifest`` (the three-way classifier added in
install.py) and the detection-path behavior under each manifest state.

Adversarial review S2 surfaced: an empty / unparseable
``.claude/.vco-manifest.json`` would silently short-circuit
``_detect_third_party_project`` to None (= "this is a VCO project, no
adopt prompt"), even though the manifest is broken. The user would
never learn their VCO state needs repair.

L2 fix:
  - ``_v47g_classify_manifest(path)`` returns ``"absent"`` | ``"valid"``
    | ``"broken"``.
  - Detection short-circuits ONLY on ``"valid"``. ``"absent"`` proceeds
    normally; ``"broken"`` proceeds AND adds a dedicated 6th signal so
    the user sees the bad state in the modal.
  - The Rust mirror (installer.rs) gets the same three-way classifier
    so the launcher GUI matches.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_INSTALL_PY = Path(__file__).resolve().parent.parent / "install.py"
_spec = importlib.util.spec_from_file_location("install_py_v47g_l2", _INSTALL_PY)
install_py = importlib.util.module_from_spec(_spec)
sys.modules["install_py_v47g_l2"] = install_py
_spec.loader.exec_module(install_py)


# ---------------------------------------------------------------------------
# Section 1 — _v47g_classify_manifest pure-function classifier
# ---------------------------------------------------------------------------


class TestV47gClassifyManifest:
    def test_absent_when_file_does_not_exist(self, tmp_path: Path) -> None:
        manifest = tmp_path / ".claude" / ".vco-manifest.json"
        # parent dir doesn't even exist
        assert install_py._v47g_classify_manifest(manifest) == "absent"

    def test_broken_when_file_is_empty(self, tmp_path: Path) -> None:
        manifest = tmp_path / ".vco-manifest.json"
        manifest.write_text("")
        assert install_py._v47g_classify_manifest(manifest) == "broken"

    def test_broken_when_file_is_whitespace_only(self, tmp_path: Path) -> None:
        manifest = tmp_path / ".vco-manifest.json"
        manifest.write_text("   \n\n  \t  ")
        assert install_py._v47g_classify_manifest(manifest) == "broken"

    def test_broken_when_file_is_not_json(self, tmp_path: Path) -> None:
        manifest = tmp_path / ".vco-manifest.json"
        manifest.write_text("this is not json {")
        assert install_py._v47g_classify_manifest(manifest) == "broken"

    def test_broken_when_root_is_array(self, tmp_path: Path) -> None:
        manifest = tmp_path / ".vco-manifest.json"
        manifest.write_text('["not", "a", "manifest"]')
        assert install_py._v47g_classify_manifest(manifest) == "broken"

    def test_broken_when_object_missing_expected_keys(self, tmp_path: Path) -> None:
        manifest = tmp_path / ".vco-manifest.json"
        manifest.write_text('{"hello": "world", "unrelated": 42}')
        assert install_py._v47g_classify_manifest(manifest) == "broken"

    def test_valid_with_vco_version_key(self, tmp_path: Path) -> None:
        manifest = tmp_path / ".vco-manifest.json"
        manifest.write_text('{"vco_version": "0.2.46"}')
        assert install_py._v47g_classify_manifest(manifest) == "valid"

    def test_valid_with_schema_version_key(self, tmp_path: Path) -> None:
        manifest = tmp_path / ".vco-manifest.json"
        manifest.write_text('{"schema_version": 2}')
        assert install_py._v47g_classify_manifest(manifest) == "valid"

    def test_valid_with_files_key(self, tmp_path: Path) -> None:
        manifest = tmp_path / ".vco-manifest.json"
        manifest.write_text('{"files": {}}')
        assert install_py._v47g_classify_manifest(manifest) == "valid"

    def test_valid_with_legacy_bundled_files_key(self, tmp_path: Path) -> None:
        # Back-compat: older manifests used "bundled_files" instead of "files".
        manifest = tmp_path / ".vco-manifest.json"
        manifest.write_text('{"bundled_files": {}}')
        assert install_py._v47g_classify_manifest(manifest) == "valid"

    def test_valid_full_manifest_shape(self, tmp_path: Path) -> None:
        manifest = tmp_path / ".vco-manifest.json"
        manifest.write_text(
            '{"schema_version": 2, "vco_version": "0.2.46", '
            '"installed_at": "2026-06-04T00:00:00Z", '
            '"updated_at": "2026-06-04T00:00:00Z", '
            '"files": {}, "preserved_files": {}}'
        )
        assert install_py._v47g_classify_manifest(manifest) == "valid"


# ---------------------------------------------------------------------------
# Section 2 — _detect_third_party_project integration with the classifier
# ---------------------------------------------------------------------------


class TestDetectionWithManifestStates:
    """The detection function's behavior must match the classifier."""

    def _build_fixture(self, root: Path, *, with_claude_dir: bool = True,
                       with_claude_md: bool = False, manifest_state: str = "absent",
                       manifest_content: str = "") -> None:
        """Build a fixture under `root` based on the desired states."""
        if with_claude_dir:
            (root / ".claude").mkdir(parents=True, exist_ok=True)
            # Add a single non-manifest artifact so .claude/ produces a signal.
            (root / ".claude" / "PROJECT_REGISTRY.md").write_text("fixture")
        if with_claude_md:
            (root / "CLAUDE.md").write_text("fixture project instructions")
        if manifest_state in ("valid", "broken"):
            (root / ".claude").mkdir(parents=True, exist_ok=True)
            (root / ".claude" / ".vco-manifest.json").write_text(manifest_content)

    def test_valid_manifest_short_circuits_to_none(self, tmp_path: Path) -> None:
        """Well-formed manifest = existing VCO project = no adopt prompt."""
        self._build_fixture(
            tmp_path,
            with_claude_dir=True,
            with_claude_md=True,
            manifest_state="valid",
            manifest_content='{"vco_version": "0.2.46"}',
        )
        assert install_py._detect_third_party_project(tmp_path) is None

    def test_absent_manifest_returns_signals(self, tmp_path: Path) -> None:
        """No manifest + existing artifacts = third-party project signals."""
        self._build_fixture(
            tmp_path,
            with_claude_dir=True,
            with_claude_md=True,
            manifest_state="absent",
        )
        result = install_py._detect_third_party_project(tmp_path)
        assert result is not None
        assert result["manifest_status"] == "absent"
        # No broken-manifest signal present.
        assert not any("unparseable" in s for s in result["signals"])

    def test_broken_manifest_emits_dedicated_signal(self, tmp_path: Path) -> None:
        """Broken manifest = surfaced as its own signal, NOT silently treated as valid."""
        self._build_fixture(
            tmp_path,
            with_claude_dir=True,
            manifest_state="broken",
            manifest_content="",  # empty file
        )
        result = install_py._detect_third_party_project(tmp_path)
        assert result is not None, (
            "Broken manifest should NOT short-circuit detection; user must be told."
        )
        assert result["manifest_status"] == "broken"
        # Verify the dedicated signal is in the list.
        assert any("unparseable" in s for s in result["signals"]), (
            f"Expected the L2 broken-manifest signal in result; got: {result['signals']}"
        )
        # Verify details contain the manifest_broken explainer.
        assert "manifest_broken" in result["details"]

    def test_broken_manifest_with_garbage_json(self, tmp_path: Path) -> None:
        self._build_fixture(
            tmp_path,
            with_claude_dir=True,
            manifest_state="broken",
            manifest_content="not valid json {{{",
        )
        result = install_py._detect_third_party_project(tmp_path)
        assert result is not None
        assert result["manifest_status"] == "broken"

    def test_broken_manifest_with_unrelated_json_object(self, tmp_path: Path) -> None:
        """A valid JSON object that's not a manifest shape = broken."""
        self._build_fixture(
            tmp_path,
            with_claude_dir=True,
            manifest_state="broken",
            manifest_content='{"hello": "world"}',
        )
        result = install_py._detect_third_party_project(tmp_path)
        assert result is not None
        assert result["manifest_status"] == "broken"

    def test_legacy_consumers_see_manifest_present_false(self, tmp_path: Path) -> None:
        """Back-compat: the boolean `manifest_present` stays False for broken + absent.

        Wave-2 consumers (V47-G-final's prompt, the Rust GUI mirror) may not
        yet read the new `manifest_status` field. They MUST see
        `manifest_present=False` so they show the adopt prompt.
        """
        self._build_fixture(
            tmp_path,
            with_claude_dir=True,
            manifest_state="broken",
            manifest_content="",
        )
        result = install_py._detect_third_party_project(tmp_path)
        assert result is not None
        assert result["manifest_present"] is False

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Tests for design D1' — sidecar dynamic-only type embeddings (v0.2.2+).

Covers the on-disk layout split (network .pt holds only builtin rows 0..8;
dynamic-type rows live in <state_dir>/type_embs/<sanitized>__<hash>.pt)
and the runtime contracts that follow from it:

  1. Round-trip:   save + reload preserves dynamic-type embeddings + scores.
  2. Rotation:     swap the network .pt → dynamic-type rows survive (the
                   v0.2.0/v0.2.1 bug this design fixes).
  3. Migration:    a legacy v0.2.1 single-file checkpoint is split on first
                   load; idempotent on subsequent loads.
  4. Crash safety: partial mid-save state recovers cleanly.
  5. User isolation: two model instances with different state_paths but
                   overlapping type names don't cross-contaminate (the
                   row-24 collision bug D1' eliminates).
  6. Filename collision: distinct names that sanitize identically are
                   stored under distinct filenames AND round-trip the
                   unsanitized names.
  7. Builtin rotation: rows 0..8 follow the rotated network; dynamic rows
                   come from sidecars and stay put.

All tests force device="cpu" so they run on machines without CUDA.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

# Repo-root-as-package import (matches conftest.py setup).

torch = pytest.importorskip("torch")

from rl_reranker.rl_model import (  # noqa: E402
    NODE_TYPES,
    N_ENTITY_TYPES,
    SIDECAR_DIR_NAME,
    SIDECAR_FORMAT_VERSION,
    SIDECAR_MARKER_NAME,
    TYPE_EMB_DIM,
    RLModel,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Small emb_dim keeps the model lightweight (~few k params) so 7 tests
# complete in a few seconds on CPU.
_EMB_DIM = 32


def _make_model(state_path: Path) -> RLModel:
    """Construct a CPU-only RLModel at the given state_path."""
    return RLModel.load_or_create(
        state_path=state_path, emb_dim=_EMB_DIM, device="cpu",
    )


def _register_type_with_known_emb(
    model: RLModel, name: str, fill_value: float,
) -> int:
    """Register `name` and overwrite its embedding row with a constant tensor.

    Returns the row index. We pin the embedding to a known constant so
    later assertions can verify the exact tensor round-tripped (the
    builtin-mean init that _register_new_type does makes equality
    comparisons brittle otherwise).
    """
    idx = model.get_type_idx(name)
    with torch.no_grad():
        model._net.type_embs.weight[idx].fill_(fill_value)
    return idx


def _score(model: RLModel, q: np.ndarray, n: np.ndarray, n_type_idx: int) -> float:
    """Run a single forward pass through model.score with no linked nodes."""
    return model.score(q, n, linked_raws=[], n_type_idx=n_type_idx)


def _rand_emb(seed: int, dim: int = _EMB_DIM) -> np.ndarray:
    """Deterministic random embedding for reproducible scoring."""
    rng = np.random.default_rng(seed)
    return rng.standard_normal(dim).astype(np.float32)


# ---------------------------------------------------------------------------
# 1. Round-trip — save + reload preserves dynamic types.
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_dynamic_types_persist_across_save_load(self, tmp_path):
        state_path = tmp_path / "rl_model.pt"

        m1 = _make_model(state_path)
        idx_a = _register_type_with_known_emb(m1, "tomatoes", 0.123)
        idx_b = _register_type_with_known_emb(m1, "recipe", 0.456)
        idx_c = _register_type_with_known_emb(m1, "ingredient", 0.789)

        # All should land at rows N_ENTITY_TYPES, +1, +2 (the first three
        # dynamic slots — insertion order).
        assert idx_a == N_ENTITY_TYPES
        assert idx_b == N_ENTITY_TYPES + 1
        assert idx_c == N_ENTITY_TYPES + 2

        m1.save()

        # Verify on-disk layout: .pt + sidecars dir + marker.
        sidecar_dir = state_path.parent / SIDECAR_DIR_NAME
        marker = state_path.parent / SIDECAR_MARKER_NAME
        assert state_path.exists()
        assert sidecar_dir.is_dir()
        assert marker.exists()
        sidecar_files = sorted(sidecar_dir.glob("*.pt"))
        assert len(sidecar_files) == 3

        # Verify .pt no longer carries dynamic-type rows.
        ckpt = torch.load(str(state_path), map_location="cpu", weights_only=True)
        type_embs_weight = ckpt["state_dict"]["type_embs.weight"]
        assert type_embs_weight.shape == (N_ENTITY_TYPES, TYPE_EMB_DIM)
        assert ckpt["next_type_slot"] == N_ENTITY_TYPES
        for builtin in NODE_TYPES:
            assert builtin in ckpt["type_registry"]
        assert "tomatoes" not in ckpt["type_registry"]

        # Reload and check the registry + tensors match.
        m2 = _make_model(state_path)
        assert "tomatoes" in m2._type_registry
        assert "recipe" in m2._type_registry
        assert "ingredient" in m2._type_registry
        for name, fill in (
            ("tomatoes", 0.123),
            ("recipe", 0.456),
            ("ingredient", 0.789),
        ):
            row = m2._type_registry[name]
            actual = m2._net.type_embs.weight[row].detach().cpu().numpy()
            assert np.allclose(actual, np.full(TYPE_EMB_DIM, fill), atol=1e-5)

        # Scores reproduce (same network weights + same type embeddings).
        q = _rand_emb(1)
        n = _rand_emb(2)
        s1 = _score(m1, q, n, idx_a)
        s2 = _score(m2, q, n, m2._type_registry["tomatoes"])
        assert abs(s1 - s2) < 1e-5

    def test_no_dynamic_types_means_no_sidecar_dir_clutter(self, tmp_path):
        """A model with only builtin types should still produce a valid
        layout: .pt + marker, sidecar dir may or may not exist (we create
        it lazily). Reload must succeed unchanged."""
        state_path = tmp_path / "rl_model.pt"
        m1 = _make_model(state_path)
        m1.save()

        assert state_path.exists()
        assert (state_path.parent / SIDECAR_MARKER_NAME).exists()

        m2 = _make_model(state_path)
        # Builtin types only.
        for builtin in NODE_TYPES:
            assert m2._type_registry[builtin] < N_ENTITY_TYPES
        assert m2._next_type_slot == N_ENTITY_TYPES


# ---------------------------------------------------------------------------
# 2. Rotation simulation — replace network .pt; sidecars survive.
# ---------------------------------------------------------------------------

class TestRotation:
    def test_rotate_network_preserves_dynamic_types(self, tmp_path):
        state_path = tmp_path / "rl_model.pt"

        # Build user state with 3 dynamic types.
        m1 = _make_model(state_path)
        for name, fill in (
            ("tomatoes", 0.11),
            ("recipe", 0.22),
            ("ingredient", 0.33),
        ):
            _register_type_with_known_emb(m1, name, fill)
        m1.save()

        # Build a SECOND model at a SEPARATE dir, save it, then copy its
        # .pt over the first — simulates the launcher's /rotate_weights
        # flow where a new global checkpoint lands at the project's
        # state path. Ship dir is isolated because m_ship.save()'s orphan
        # GC would otherwise prune the user's sidecars (they share tmp_path).
        ship_dir = tmp_path / "ship_staging"
        ship_dir.mkdir()
        ship_path = ship_dir / "shipped_model.pt"
        m_ship = _make_model(ship_path)
        # Pin shipped builtin embeddings to a known constant so we can
        # verify rotation actually moves the network weights.
        with torch.no_grad():
            m_ship._net.type_embs.weight[:N_ENTITY_TYPES].fill_(0.999)
        m_ship.save()

        # Atomic file swap (mimics what the launcher does: write new
        # weights into state/rl_model_<tag>.pt). The sidecar dir is
        # NOT touched.
        import shutil

        shutil.copyfile(ship_path, state_path)

        # Reload from the rotated state_path.
        m2 = _make_model(state_path)

        # Builtin rows reflect the rotated network.
        for i in range(N_ENTITY_TYPES):
            assert torch.allclose(
                m2._net.type_embs.weight[i],
                torch.full((TYPE_EMB_DIM,), 0.999),
                atol=1e-5,
            )

        # Dynamic types survived the rotation.
        for name, fill in (
            ("tomatoes", 0.11),
            ("recipe", 0.22),
            ("ingredient", 0.33),
        ):
            assert name in m2._type_registry
            row = m2._type_registry[name]
            assert row >= N_ENTITY_TYPES
            actual = m2._net.type_embs.weight[row].detach().cpu().numpy()
            assert np.allclose(actual, np.full(TYPE_EMB_DIM, fill), atol=1e-5)


# ---------------------------------------------------------------------------
# 3. Migration from v0.2.1 single-file format.
# ---------------------------------------------------------------------------

def _synthesize_legacy_checkpoint(
    state_path: Path,
    emb_dim: int = _EMB_DIM,
    dynamic_names_and_fills: list[tuple[str, float]] | None = None,
) -> None:
    """Write a legacy-format (v0.2.0/v0.2.1) checkpoint to state_path.

    Legacy = single .pt with `type_registry` carrying entries at idx >= 9
    AND `type_embs.weight` having those rows populated; no marker file.
    """
    if dynamic_names_and_fills is None:
        dynamic_names_and_fills = []

    # Build a fresh model in memory (gives us a valid state_dict + opt
    # state + builtin registry). Then we mutate the in-memory state to
    # mimic legacy format, save it raw via torch.save (bypassing
    # RLModel.save which would split into sidecars), and ensure the
    # marker is absent.
    scratch_path = state_path.parent / "_scratch.pt"
    m = RLModel.load_or_create(
        state_path=scratch_path, emb_dim=emb_dim, device="cpu",
    )
    for i, (name, fill) in enumerate(dynamic_names_and_fills):
        row = N_ENTITY_TYPES + i
        with torch.no_grad():
            m._net.type_embs.weight[row].fill_(fill)
        m._type_registry[name] = row
        m._next_type_slot = row + 1

    # Raw save — single-file format, no sidecars, no marker.
    cpu_state = {k: v.detach().cpu() for k, v in m._net.state_dict().items()}
    torch.save(
        {
            "state_dict": cpu_state,
            "opt_state": m._opt.state_dict(),
            "emb_dim": emb_dim,
            "arch_version": "v2",
            "type_registry": dict(m._type_registry),
            "next_type_slot": m._next_type_slot,
            # NB: no "types_layout" tag — legacy.
        },
        str(state_path),
    )
    # Remove the scratch artefacts to keep the test dir tidy.
    if scratch_path.exists():
        scratch_path.unlink()
    # Ensure no marker / sidecar dir lingers from scratch construction.
    marker = state_path.parent / SIDECAR_MARKER_NAME
    if marker.exists():
        marker.unlink()
    sidecar_dir = state_path.parent / SIDECAR_DIR_NAME
    if sidecar_dir.exists():
        import shutil

        shutil.rmtree(sidecar_dir)


class TestMigration:
    def test_legacy_pt_migrates_to_sidecars_on_first_load(self, tmp_path):
        state_path = tmp_path / "rl_model.pt"
        _synthesize_legacy_checkpoint(
            state_path,
            dynamic_names_and_fills=[
                ("tomatoes", 0.11),
                ("recipe", 0.22),
                ("ingredient", 0.33),
                ("herb", 0.44),
                ("spice", 0.55),
            ],
        )

        marker = state_path.parent / SIDECAR_MARKER_NAME
        sidecar_dir = state_path.parent / SIDECAR_DIR_NAME
        # Sanity: pre-migration layout.
        assert not marker.exists()
        assert not sidecar_dir.exists()

        # Load — should trigger migration.
        m = _make_model(state_path)

        # Post-migration layout: sidecars present, marker dropped, .pt
        # rewritten with builtin-only registry.
        assert marker.exists()
        assert sidecar_dir.is_dir()
        sidecars = sorted(sidecar_dir.glob("*.pt"))
        assert len(sidecars) == 5

        ckpt = torch.load(str(state_path), map_location="cpu", weights_only=True)
        assert ckpt["state_dict"]["type_embs.weight"].shape == (N_ENTITY_TYPES, TYPE_EMB_DIM)
        assert ckpt["next_type_slot"] == N_ENTITY_TYPES
        for name in ("tomatoes", "recipe", "ingredient", "herb", "spice"):
            assert name not in ckpt["type_registry"]

        # In-memory registry still has the 5 dynamic types AND their
        # embeddings round-tripped.
        for name, fill in (
            ("tomatoes", 0.11),
            ("recipe", 0.22),
            ("ingredient", 0.33),
            ("herb", 0.44),
            ("spice", 0.55),
        ):
            assert name in m._type_registry
            row = m._type_registry[name]
            actual = m._net.type_embs.weight[row].detach().cpu().numpy()
            assert np.allclose(actual, np.full(TYPE_EMB_DIM, fill), atol=1e-5)

    def test_migration_is_idempotent(self, tmp_path):
        """Running load twice on the same dir must not re-migrate."""
        state_path = tmp_path / "rl_model.pt"
        _synthesize_legacy_checkpoint(
            state_path,
            dynamic_names_and_fills=[("alpha", 0.1), ("beta", 0.2)],
        )

        _make_model(state_path)
        marker = state_path.parent / SIDECAR_MARKER_NAME
        sidecar_dir = state_path.parent / SIDECAR_DIR_NAME
        assert marker.exists()
        files_after_first_load = sorted(p.name for p in sidecar_dir.glob("*.pt"))
        # Record marker mtime — must NOT change on second load if we
        # correctly skip migration.
        marker_mtime_first = marker.stat().st_mtime
        pt_mtime_first = state_path.stat().st_mtime

        # Touch nothing — just construct a second model from the same path.
        m2 = _make_model(state_path)

        files_after_second_load = sorted(p.name for p in sidecar_dir.glob("*.pt"))
        assert files_after_second_load == files_after_first_load

        # Re-migration would re-write the .pt + marker, bumping their
        # mtimes. The second load should leave both untouched.
        assert marker.stat().st_mtime == marker_mtime_first
        assert state_path.stat().st_mtime == pt_mtime_first

        # State matches between the two loads.
        for name, fill in (("alpha", 0.1), ("beta", 0.2)):
            row = m2._type_registry[name]
            actual = m2._net.type_embs.weight[row].detach().cpu().numpy()
            assert np.allclose(actual, np.full(TYPE_EMB_DIM, fill), atol=1e-5)


# ---------------------------------------------------------------------------
# 4. Crash-safety — partial mid-save state recovers cleanly.
# ---------------------------------------------------------------------------

class TestCrashSafety:
    def test_orphan_tempfile_does_not_pollute(self, tmp_path):
        """Simulate a sidecar write that was killed AFTER tempfile creation
        but BEFORE os.replace. Next save() must succeed and the orphan
        tempfile must not survive into a subsequent load."""
        state_path = tmp_path / "rl_model.pt"
        m1 = _make_model(state_path)
        _register_type_with_known_emb(m1, "alpha", 0.42)
        m1.save()

        sidecar_dir = state_path.parent / SIDECAR_DIR_NAME

        # Inject an orphan tempfile (simulates a killed-mid-write save).
        orphan = sidecar_dir / "beta__deadbeef.pt.tmp"
        orphan.write_bytes(b"\x00\x01partial garbage\x02")
        assert orphan.exists()

        # Subsequent save() must complete cleanly (tempfiles aren't
        # cleaned up by _write_sidecar — that's fine; load() ignores
        # them because they don't have a .pt suffix).
        _register_type_with_known_emb(m1, "gamma", 0.99)
        m1.save()

        # Load and verify state is consistent — orphan was ignored.
        m2 = _make_model(state_path)
        assert "alpha" in m2._type_registry
        assert "gamma" in m2._type_registry
        # The orphan does NOT appear as a registered type.
        for name in m2._type_registry:
            assert name != "beta"

    def test_marker_missing_after_clean_split_pt_is_repaired(self, tmp_path):
        """Case (C) from _try_load: marker absent but .pt is already in
        post-migration form (builtins only) AND sidecars exist on disk.
        Next load should drop the marker without re-doing anything."""
        state_path = tmp_path / "rl_model.pt"
        m1 = _make_model(state_path)
        _register_type_with_known_emb(m1, "alpha", 0.42)
        m1.save()

        marker = state_path.parent / SIDECAR_MARKER_NAME
        assert marker.exists()
        # Remove the marker to simulate a crash between .pt rewrite and
        # marker write.
        marker.unlink()
        assert not marker.exists()

        # Load — should drop the marker without re-saving.
        pt_mtime_before = state_path.stat().st_mtime
        m2 = _make_model(state_path)
        assert marker.exists()
        # The .pt should not have been re-written.
        pt_mtime_after = state_path.stat().st_mtime
        assert pt_mtime_before == pt_mtime_after

        # Dynamic type intact.
        assert "alpha" in m2._type_registry
        row = m2._type_registry["alpha"]
        assert np.allclose(
            m2._net.type_embs.weight[row].detach().cpu().numpy(),
            np.full(TYPE_EMB_DIM, 0.42),
            atol=1e-5,
        )


# ---------------------------------------------------------------------------
# 5. User1/user2 isolation — overlapping names, different state paths.
# ---------------------------------------------------------------------------

class TestUserIsolation:
    def test_two_users_overlapping_names_no_cross_contamination(self, tmp_path):
        """Headline D1' bug fix: under v0.2.0/v0.2.1, user1 "tomatoes" at
        row 24 and user2 "recipe" at row 24 produced mutually
        incompatible .pt files. Under D1', the same registry but at
        DIFFERENT state_dirs must stay completely isolated."""
        user1_path = tmp_path / "user1" / "rl_model.pt"
        user2_path = tmp_path / "user2" / "rl_model.pt"
        user1_path.parent.mkdir()
        user2_path.parent.mkdir()

        m_u1 = _make_model(user1_path)
        m_u2 = _make_model(user2_path)

        # Both users register OVERLAPPING names with DIFFERENT fills.
        _register_type_with_known_emb(m_u1, "tomatoes", 0.111)
        _register_type_with_known_emb(m_u1, "shared", 0.222)

        _register_type_with_known_emb(m_u2, "recipe", 0.333)
        _register_type_with_known_emb(m_u2, "shared", 0.444)  # same name, different fill

        m_u1.save()
        m_u2.save()

        # Reload both and verify cross-isolation.
        m_u1_loaded = _make_model(user1_path)
        m_u2_loaded = _make_model(user2_path)

        # User 1 has "tomatoes" and "shared"@0.222, NOT "recipe".
        assert "tomatoes" in m_u1_loaded._type_registry
        assert "shared" in m_u1_loaded._type_registry
        assert "recipe" not in m_u1_loaded._type_registry
        row_shared_u1 = m_u1_loaded._type_registry["shared"]
        assert np.allclose(
            m_u1_loaded._net.type_embs.weight[row_shared_u1].detach().cpu().numpy(),
            np.full(TYPE_EMB_DIM, 0.222),
            atol=1e-5,
        )

        # User 2 has "recipe" and "shared"@0.444, NOT "tomatoes".
        assert "recipe" in m_u2_loaded._type_registry
        assert "shared" in m_u2_loaded._type_registry
        assert "tomatoes" not in m_u2_loaded._type_registry
        row_shared_u2 = m_u2_loaded._type_registry["shared"]
        assert np.allclose(
            m_u2_loaded._net.type_embs.weight[row_shared_u2].detach().cpu().numpy(),
            np.full(TYPE_EMB_DIM, 0.444),
            atol=1e-5,
        )


# ---------------------------------------------------------------------------
# 6. Sanitized-filename collision handling.
# ---------------------------------------------------------------------------

class TestFilenameCollision:
    def test_names_with_same_sanitization_round_trip_distinctly(self, tmp_path):
        """`"a/b"` and `"a-b"` both sanitize to `"a_b"`. The hash suffix
        must keep them as distinct files, and the unsanitized name must
        round-trip via the sidecar's inner `name` field."""
        state_path = tmp_path / "rl_model.pt"
        m1 = _make_model(state_path)

        _register_type_with_known_emb(m1, "a/b", 0.111)
        _register_type_with_known_emb(m1, "a-b", 0.222)
        # Also a third name with non-ASCII to exercise the sanitization.
        _register_type_with_known_emb(m1, "café/noir", 0.333)

        m1.save()

        sidecar_dir = state_path.parent / SIDECAR_DIR_NAME
        files = sorted(p.name for p in sidecar_dir.glob("*.pt"))
        # Exactly 3 distinct files (no collision).
        assert len(files) == 3
        # Every filename has the `<sanitized>__<8-hex>.pt` shape.
        import re

        pat = re.compile(r"^[A-Za-z0-9_\-]+__[0-9a-f]{8}\.pt$")
        for fname in files:
            assert pat.match(fname), f"unexpected sidecar filename {fname!r}"

        # Reload and verify ALL THREE original names are restored.
        m2 = _make_model(state_path)
        for name, fill in (("a/b", 0.111), ("a-b", 0.222), ("café/noir", 0.333)):
            assert name in m2._type_registry, f"missing {name!r}"
            row = m2._type_registry[name]
            assert np.allclose(
                m2._net.type_embs.weight[row].detach().cpu().numpy(),
                np.full(TYPE_EMB_DIM, fill),
                atol=1e-5,
            )

    def test_filename_helper_is_deterministic(self, tmp_path):
        """Same input → same filename, run after run."""
        f1 = RLModel._sidecar_filename("tomatoes")
        f2 = RLModel._sidecar_filename("tomatoes")
        assert f1 == f2
        # Different inputs → different filenames (hash-domain check).
        assert RLModel._sidecar_filename("a/b") != RLModel._sidecar_filename("a-b")
        assert RLModel._sidecar_filename("tomatoes") != RLModel._sidecar_filename("Tomatoes")


# ---------------------------------------------------------------------------
# 7. Builtin rotation — rows 0..8 follow the shipped weights.
# ---------------------------------------------------------------------------

class TestBuiltinRotation:
    def test_rotation_replaces_builtins_keeps_dynamics(self, tmp_path):
        """The flip side of TestRotation: when we rotate, builtin rows
        0..8 should reflect the new shipped weights AND dynamic rows
        should remain user-specific."""
        state_path = tmp_path / "rl_model.pt"

        # Pre-rotation: user model with one dynamic type + custom
        # builtin row.
        m1 = _make_model(state_path)
        # Mark the project's pre-rotation builtin "concept" row with a
        # distinctive value.
        with torch.no_grad():
            m1._net.type_embs.weight[0].fill_(0.001)  # NODE_TYPES[0] = "concept"
        _register_type_with_known_emb(m1, "userType", 0.777)
        m1.save()

        # Shipped checkpoint with a DIFFERENT builtin "concept" row.
        # Isolated ship dir — see TestRotation comment.
        ship_dir = tmp_path / "ship_staging"
        ship_dir.mkdir()
        ship_path = ship_dir / "shipped.pt"
        m_ship = _make_model(ship_path)
        with torch.no_grad():
            m_ship._net.type_embs.weight[0].fill_(0.999)  # the new shipped "concept"
        # Shipped checkpoint has NO dynamic types (typical for a global
        # release).
        m_ship.save()

        # Simulate rotation: copy the shipped .pt over the user's .pt.
        # Sidecar dir stays where it is.
        import shutil

        shutil.copyfile(ship_path, state_path)

        # Reload from the rotated state_path.
        m2 = _make_model(state_path)

        # Builtin row 0 ("concept") reflects the shipped weights.
        assert torch.allclose(
            m2._net.type_embs.weight[0],
            torch.full((TYPE_EMB_DIM,), 0.999),
            atol=1e-5,
        ), "builtin row 0 should follow the shipped weights"

        # Dynamic type "userType" survived with its pre-rotation value.
        assert "userType" in m2._type_registry
        row = m2._type_registry["userType"]
        assert row >= N_ENTITY_TYPES
        assert np.allclose(
            m2._net.type_embs.weight[row].detach().cpu().numpy(),
            np.full(TYPE_EMB_DIM, 0.777),
            atol=1e-5,
        )


# ---------------------------------------------------------------------------
# Sidecar-format invariants (whitebox spot-check).
# ---------------------------------------------------------------------------

class TestSidecarFormat:
    def test_sidecar_payload_shape(self, tmp_path):
        """One sidecar opened directly must carry {name, emb, version}."""
        state_path = tmp_path / "rl_model.pt"
        m = _make_model(state_path)
        _register_type_with_known_emb(m, "probe", 0.5)
        m.save()

        sidecar_dir = state_path.parent / SIDECAR_DIR_NAME
        files = list(sidecar_dir.glob("*.pt"))
        assert len(files) == 1
        payload = torch.load(str(files[0]), map_location="cpu", weights_only=True)
        assert payload["name"] == "probe"
        assert payload["version"] == SIDECAR_FORMAT_VERSION
        assert payload["emb"].shape == (TYPE_EMB_DIM,)
        assert payload["emb"].dtype == torch.float32

    def test_marker_payload_is_valid_json(self, tmp_path):
        """The marker file's contents are a JSON sentinel — useful for
        forensics + a cheap way to verify it's not a stray empty file."""
        state_path = tmp_path / "rl_model.pt"
        m = _make_model(state_path)
        m.save()

        marker = state_path.parent / SIDECAR_MARKER_NAME
        body = marker.read_text(encoding="utf-8")
        parsed = json.loads(body)
        assert parsed["layout"] == "v2"
        assert parsed["version"] == SIDECAR_FORMAT_VERSION


# ---------------------------------------------------------------------------
# 8. Rotation onto legacy-format .pt (C1 regression).
#
# Scenario: user is on v0.2.2 (marker present, sidecars populated). The
# launcher rotates in a legacy-format global checkpoint (rows 9..255 baked
# into type_embs.weight, dynamic-type entries inside the .pt registry).
# Without the defense-in-depth in _try_load, this would produce row
# collisions — _load_sidecars allocates starting at row N_ENTITY_TYPES,
# which load_state_dict already filled from the legacy .pt.
# ---------------------------------------------------------------------------


class TestRotationOntoLegacy:
    def test_user_sidecars_win_when_legacy_pt_rotated_in(self, tmp_path):
        state_path = tmp_path / "rl_model.pt"

        # Step 1: build v0.2.2 user state with a dynamic type.
        m_user = _make_model(state_path)
        _register_type_with_known_emb(m_user, "tomatoes", 0.42)
        m_user.save()
        marker = state_path.parent / SIDECAR_MARKER_NAME
        sidecar_dir = state_path.parent / SIDECAR_DIR_NAME
        assert marker.exists()
        assert sidecar_dir.is_dir()
        assert len(list(sidecar_dir.glob("*.pt"))) == 1

        # Step 2: synthesize a legacy-format shipped checkpoint with a
        # dynamic-type entry at row N_ENTITY_TYPES — this is the exact
        # collision target.
        # ship_path MUST live in a separate dir — _synthesize_legacy_checkpoint
        # wipes any marker / sidecar dir in its parent as part of producing
        # legacy output, which would also wipe the user's state if we shared
        # tmp_path.
        ship_dir = tmp_path / "ship_staging"
        ship_dir.mkdir()
        ship_path = ship_dir / "shipped_legacy.pt"
        _synthesize_legacy_checkpoint(
            ship_path,
            dynamic_names_and_fills=[("shipped_concept", 0.999)],
        )

        # Step 3: rotate it in — file-level overwrite, marker + sidecars
        # untouched (what the launcher's /rotate_weights flow does).
        import shutil

        shutil.copyfile(ship_path, state_path)
        # Marker MUST still be present after the rotation (defense-in-depth
        # only fires when both marker and dynamic-in-pt are true).
        assert marker.exists()
        assert sidecar_dir.is_dir()

        # Step 4: reload. Defense-in-depth should drop the stale shipped
        # entry from the registry and merge the user sidecar at row 9.
        m_reloaded = _make_model(state_path)

        # Tomatoes wins at row N_ENTITY_TYPES with the user's embedding.
        assert "tomatoes" in m_reloaded._type_registry
        tomatoes_row = m_reloaded._type_registry["tomatoes"]
        assert tomatoes_row == N_ENTITY_TYPES
        actual = m_reloaded._net.type_embs.weight[tomatoes_row].detach().cpu().numpy()
        assert np.allclose(actual, np.full(TYPE_EMB_DIM, 0.42), atol=1e-5), (
            "User sidecar embedding must win — not the legacy .pt's shipped value"
        )

        # Shipped concept is NOT in the registry (dropped by defense logic).
        assert "shipped_concept" not in m_reloaded._type_registry

        # next_type_slot reflects the merged sidecar count, not the legacy
        # .pt's overshoot.
        assert m_reloaded._next_type_slot == N_ENTITY_TYPES + 1

    def test_corrupted_state_with_legacy_pt_drop_in_does_not_collide(self, tmp_path):
        """Even with 5 shipped dynamic entries AND 3 user sidecars, no row collisions."""
        state_path = tmp_path / "rl_model.pt"

        # User has 3 sidecars at rows 9, 10, 11.
        m_user = _make_model(state_path)
        _register_type_with_known_emb(m_user, "user_a", 0.10)
        _register_type_with_known_emb(m_user, "user_b", 0.20)
        _register_type_with_known_emb(m_user, "user_c", 0.30)
        m_user.save()

        # Drop a legacy .pt with 5 shipped dynamic types over the .pt only.
        # ship_path MUST live in a separate dir — _synthesize_legacy_checkpoint
        # wipes any marker / sidecar dir in its parent as part of producing
        # legacy output, which would also wipe the user's state if we shared
        # tmp_path.
        ship_dir = tmp_path / "ship_staging"
        ship_dir.mkdir()
        ship_path = ship_dir / "shipped_legacy.pt"
        _synthesize_legacy_checkpoint(
            ship_path,
            dynamic_names_and_fills=[
                ("ship_1", 0.91), ("ship_2", 0.92), ("ship_3", 0.93),
                ("ship_4", 0.94), ("ship_5", 0.95),
            ],
        )
        import shutil

        shutil.copyfile(ship_path, state_path)

        m_reloaded = _make_model(state_path)

        # All 3 user types present at distinct rows; no shipped names leaked.
        rows = set()
        for name, fill in (("user_a", 0.10), ("user_b", 0.20), ("user_c", 0.30)):
            assert name in m_reloaded._type_registry
            row = m_reloaded._type_registry[name]
            assert row >= N_ENTITY_TYPES
            assert row not in rows, f"Row collision at {row}!"
            rows.add(row)
            actual = m_reloaded._net.type_embs.weight[row].detach().cpu().numpy()
            assert np.allclose(actual, np.full(TYPE_EMB_DIM, fill), atol=1e-5)

        for shipped_name in ("ship_1", "ship_2", "ship_3", "ship_4", "ship_5"):
            assert shipped_name not in m_reloaded._type_registry


# ---------------------------------------------------------------------------
# 9. Sidecar dir is a regular file (M1 regression).
#
# A corrupted state or a misconfigured bind mount could leave a regular
# file at state/type_embs/ instead of a directory. _load_sidecars must
# treat this the same as "no sidecars" — not crash.
# ---------------------------------------------------------------------------


class TestSidecarDirNotADirectory:
    def test_regular_file_at_sidecar_path_does_not_crash(self, tmp_path):
        state_path = tmp_path / "rl_model.pt"

        # Build a model and save first so the .pt exists.
        m1 = _make_model(state_path)
        m1.save()

        # Replace the sidecar dir with a regular file.
        sidecar_dir = state_path.parent / SIDECAR_DIR_NAME
        if sidecar_dir.is_dir():
            import shutil

            shutil.rmtree(sidecar_dir)
        sidecar_dir.write_text("not a directory", encoding="utf-8")
        assert sidecar_dir.is_file()

        # Loading must succeed (no dynamic types — sidecar dir is a file
        # so load_sidecars treats it as absent).
        m2 = _make_model(state_path)
        assert m2._next_type_slot == N_ENTITY_TYPES
        for builtin in NODE_TYPES:
            assert builtin in m2._type_registry


# ---------------------------------------------------------------------------
# 10. Save-on-register — newly-registered types are durable from creation.
# ---------------------------------------------------------------------------


class TestSaveOnRegister:
    def test_register_persists_sidecar_immediately(self, tmp_path):
        """A registered type must survive a process crash before next save()."""
        state_path = tmp_path / "rl_model.pt"

        m1 = _make_model(state_path)
        # Just register — do NOT call m1.save() afterwards. Pre-D1' this
        # path would lose the type on the next load.
        idx = m1.get_type_idx("just_registered")
        assert idx == N_ENTITY_TYPES

        # Sidecar must already be on disk.
        sidecar_dir = state_path.parent / SIDECAR_DIR_NAME
        sidecars = list(sidecar_dir.glob("*.pt"))
        assert len(sidecars) == 1, (
            "save-on-register failed — sidecar should exist immediately after get_type_idx"
        )

        # Note: marker is NOT yet on disk because no full save() ran.
        # Next time _try_load fires (without full save) it'll hit case-C
        # repair and write the marker. We don't assert marker absence
        # here because the test contract is about the SIDECAR being
        # durable, not about marker write semantics.


# ---------------------------------------------------------------------------
# 11. Orphan sidecar GC — save() reconciles sidecar dir to registry.
# ---------------------------------------------------------------------------


class TestOrphanSidecarGC:
    def test_save_prunes_sidecars_not_in_registry(self, tmp_path):
        state_path = tmp_path / "rl_model.pt"
        sidecar_dir = state_path.parent / SIDECAR_DIR_NAME

        # Build model with 2 registered types.
        m1 = _make_model(state_path)
        _register_type_with_known_emb(m1, "alpha", 0.11)
        _register_type_with_known_emb(m1, "beta", 0.22)
        m1.save()
        assert len(list(sidecar_dir.glob("*.pt"))) == 2

        # Manually drop a stale sidecar — the kind of detritus that could
        # land on disk if a user copied files between projects or if a
        # future "forget unused types" feature ever runs.
        stale_payload = {
            "name": "ghost",
            "emb": torch.zeros(TYPE_EMB_DIM, dtype=torch.float32),
            "version": 1,
        }
        torch.save(stale_payload, str(sidecar_dir / "ghost__deadbeef.pt"))
        assert len(list(sidecar_dir.glob("*.pt"))) == 3

        # Next save() should prune the orphan.
        m1.save()
        files_after = sorted(p.name for p in sidecar_dir.glob("*.pt"))
        assert len(files_after) == 2
        for fname in files_after:
            assert not fname.startswith("ghost__")


# ---------------------------------------------------------------------------
# 12. Non-finite values in a sidecar are rejected.
# ---------------------------------------------------------------------------


class TestSidecarFiniteCheck:
    def test_sidecar_with_nan_is_skipped(self, tmp_path):
        state_path = tmp_path / "rl_model.pt"
        m1 = _make_model(state_path)
        _register_type_with_known_emb(m1, "good", 0.5)
        m1.save()

        sidecar_dir = state_path.parent / SIDECAR_DIR_NAME
        # Drop a poisoned sidecar with NaN values.
        nan_payload = {
            "name": "poisoned",
            "emb": torch.full((TYPE_EMB_DIM,), float("nan"), dtype=torch.float32),
            "version": 1,
        }
        torch.save(nan_payload, str(sidecar_dir / "poisoned__cafef00d.pt"))

        # Reload — poisoned sidecar must be skipped, good sidecar loads.
        m2 = _make_model(state_path)
        assert "good" in m2._type_registry
        assert "poisoned" not in m2._type_registry
        # And good's embedding is still finite.
        good_row = m2._type_registry["good"]
        good_emb = m2._net.type_embs.weight[good_row]
        assert bool(torch.isfinite(good_emb).all())

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Dual-write chunk budget — WP-O rework (2026-07-22, no-functionality-loss rule).

Under ``DUAL_EMBEDDING_WRITE_ALL_SLOTS`` the same KG chunk is embedded into every
configured text slot. STANDING RULE: the ACTIVE slot's chunk fidelity must NEVER
drop below the single-write baseline — so active-slot chunk boundaries follow the
ACTIVE model's own preset UNCLAMPED. The earlier G5/fix-r2 min-across-slots clamp
(v0.2.87) shrank the active budget to the tightest slot (arctic 4 096) and is
REVERTED. The degradable side is the SECONDARY: when a chunk exceeds a secondary's
num_ctx, ``EmbeddingService.embed_text_all_configured`` embeds it from a bounded,
EXPLICITLY-TAGGED sub-window (never a silent Ollama/OpenAI truncation, never a
clamp on the active chunk).

Tests:
  1. ``chunking_preset_for_models`` (retained min-across-slots UTILITY, no longer
     wired into the active write path) still resolves the tightest tier.
  2. TestActiveSlotFidelityInvariant — the ACTIVE chunker is active-model sized
     (unclamped); boundaries byte-identical dual-on vs off (RED-PROOF, leaf-tier).
  3. TestCallSiteActiveFidelity (R3-1) — drives ``store_knowledge_node``'s REAL
     multi-chunk write path (the server.py ~:6238 chunker construction) with
     dual-write ON vs OFF and asserts the STORED chunk boundaries are identical and
     equal the unclamped active-model sizing. This is the call-site pin: it bites
     when the min-across-slots clamp is reintroduced at the write site (the
     ``parity-tests-pin-the-call-site-not-the-pure-function`` lesson — pin the
     caller, not only the leaf function).
  4. ``configured_text_models`` reflects the write-all-slots + arctic-secondary env
     decision (the WRITE fan-out set — not the active chunk budget).
  5. The chunker-revision sentinel advanced to v0.2.88 (active boundaries changed
     for the dual-write installs that ran v0.2.87 → its own documented contract).
The tagged-degradation path itself (bounded sub-window + last_secondary_truncated)
is red-proofed in tests/test_embedding_service.py.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import uuid as _uuid
from pathlib import Path

from claude_mcp_servers.weaviate_mcp.chunking import (
    CHUNKING_PRESETS,
    MODEL_TOKEN_LIMITS,
    Chunker,
    TokenCounter,
    chunking_preset_for_model,
    chunking_preset_for_models,
    _CHUNKER_REVISION,
)

_QWEN = "qwen3-embedding:0.6b"
_ARCTIC = "snowflake-arctic-embed2:latest"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MCP_DIR = _PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(_PROJECT_ROOT), str(_MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class TestMinAcrossSlots:
    """``chunking_preset_for_models`` is RETAINED as a min-across-slots UTILITY
    (WP-O rework un-wired it from the active-slot write path — see
    TestActiveSlotFidelityInvariant — but the function's own contract still holds).
    """

    def test_qwen_plus_arctic_uses_medium_tier(self) -> None:
        # min(num_ctx) = min(10 240, 4 096) = 4 096 -> medium_context.
        assert chunking_preset_for_models([_QWEN, _ARCTIC]) == \
            CHUNKING_PRESETS["medium_context"]

    def test_singleton_matches_single_model_resolver(self) -> None:
        # Non-dual path is byte-unchanged: min of one model is that model.
        assert chunking_preset_for_models([_QWEN]) == \
            chunking_preset_for_model(_QWEN)

    def test_empty_list_falls_back_to_large_context(self) -> None:
        assert chunking_preset_for_models([]) == CHUNKING_PRESETS["large_context"]

    def test_unknown_model_never_widens_budget(self) -> None:
        # arctic (4 096) + an unknown model (defaulted to 8 192) still resolves to
        # arctic's tighter medium tier.
        assert chunking_preset_for_models([_ARCTIC, "totally-unknown-model:9b"]) == \
            CHUNKING_PRESETS["medium_context"]

    def test_order_independent(self) -> None:
        assert chunking_preset_for_models([_QWEN, _ARCTIC]) == \
            chunking_preset_for_models([_ARCTIC, _QWEN])


class TestActiveSlotFidelityInvariant:
    """WP-O rework (no-functionality-loss rule): the ACTIVE slot's chunk
    boundaries must be UNCLAMPED — identical whether dual-write is ON or OFF. The
    secondary slots absorb any degradation (bounded sub-window), never the active
    chunk. RED-PROOF: reverting the active chunker to ``Chunker.for_models(min)``
    would shrink the active boundaries under dual-on → this test would fail.

    NOTE: this class pins the LEAF (``Chunker.for_model``) — both sides are built
    by the test. The call-site pin that bites when the write path itself is
    re-clamped lives in TestCallSiteActiveFidelity below (R3-1).
    """

    def _big_text(self, approx_tokens: int) -> str:
        sentence = "The retrieval model scores each candidate node against the query. "
        n = (approx_tokens // 10) + 50
        return " ".join(f"{sentence}Item {i} details here." for i in range(n))

    def test_active_chunker_is_active_model_not_min(self) -> None:
        # The active-slot chunker follows the ACTIVE model's own preset. On a
        # qwen3-active install that is qwen3's XLARGE tier — NOT arctic's medium
        # tier (which min-across-slots would give). This is the invariant the
        # store_knowledge_node write path now honours (Chunker.for_model(active)).
        active_chunker = Chunker.for_model(_QWEN)
        assert (active_chunker.min_tokens, active_chunker.max_tokens,
                active_chunker.target_tokens) == CHUNKING_PRESETS["xlarge_context"]
        # And it must NOT be arctic's medium tier (the pre-rework min-clamp value).
        assert (active_chunker.min_tokens, active_chunker.max_tokens,
                active_chunker.target_tokens) != CHUNKING_PRESETS["medium_context"]

    def test_active_boundaries_identical_dual_on_vs_off(self) -> None:
        """RED-PROOF (active fidelity): the ACTIVE-slot chunk boundaries must be
        byte-identical whether or not a secondary is configured. The write path
        sizes to ``Chunker.for_model(active)`` REGARDLESS of dual state, so a
        qwen3-active install's chunks are the same with arctic-secondary on or off.
        """
        arctic_ctx = MODEL_TOKEN_LIMITS[_ARCTIC]  # 4 096
        text = self._big_text(arctic_ctx * 3)  # > arctic window, within qwen3's
        # Active-slot chunker: qwen3 (active), the SAME object the write path builds
        # whether dual is on or off (it never consults the secondary set).
        active_chunks = Chunker.for_model(_QWEN).chunk_text(text, source_id="a")
        assert active_chunks, "expected chunks for a large text"
        # At least one active chunk EXCEEDS arctic's window — proof the active slot
        # is NOT clamped to arctic (full qwen3 fidelity retained under dual-on).
        overflow = [c for c in active_chunks if c.token_count > arctic_ctx]
        assert overflow, (
            "RED-PROOF failed: the active qwen3 chunker must produce at least one "
            "chunk exceeding arctic's num_ctx on a >4k-token text — proving the "
            "active slot keeps its full budget (not clamped to the arctic min). If "
            "this fails, the min-across-slots clamp has been reintroduced and the "
            "active slot's fidelity dropped below single-write."
        )
        # And the active boundaries equal single-write sizing exactly.
        single_write_chunks = Chunker.for_model(_QWEN).chunk_text(text, source_id="s")
        assert [c.token_count for c in active_chunks] == \
            [c.token_count for c in single_write_chunks], (
            "active-slot chunk boundaries must be byte-identical to single-write"
        )


class TestConfiguredTextModels:
    def test_off_returns_active_model_only(self, monkeypatch) -> None:
        from vco_lib.embedding_service import configured_text_models
        monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "false")
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        # Hermetic: EMBEDDING_MODEL now wins for the active slot (R2-3), so a
        # host env with it set must not leak into the profile-derived case.
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        models = configured_text_models()
        assert models == [_QWEN], (
            "write-all OFF must return the active model alone (single-model "
            "preset preserved)"
        )

    def test_on_qwen_active_includes_no_arctic_without_arctic_flag(
        self, monkeypatch
    ) -> None:
        # qwen3 active + write-all ON but the WP-O arctic gate OFF: the arctic
        # secondary is NOT configured (the gate genuinely gates), so the set is
        # just [qwen3] (openai only if a key exists). This is the pre-WP-O
        # behaviour, still exact when the dedicated arctic flag is off.
        from vco_lib.embedding_service import configured_text_models
        monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        monkeypatch.delenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_EMBEDDING_API_KEY", raising=False)
        models = configured_text_models()
        assert _ARCTIC not in models, (
            "arctic gate OFF must NOT add the arctic secondary"
        )
        assert models == [_QWEN]

    def test_arctic_active_includes_qwen_secondary(
        self, monkeypatch
    ) -> None:
        # arctic ACTIVE + write-all ON: qwen3 is the secondary (already the
        # default model), so both slots are configured and the dual ARCTIC budget
        # (arctic's tighter num_ctx) is exercised on an arctic-active install.
        from vco_lib.embedding_service import configured_text_models
        monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")
        monkeypatch.setenv("ACTIVE_EMBEDDING", "arctic")
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_EMBEDDING_API_KEY", raising=False)
        models = configured_text_models()
        # arctic active + qwen3 secondary → both slots configured.
        assert _ARCTIC in models
        assert _QWEN in models
        # min-across-slots then uses arctic's tighter budget.
        assert chunking_preset_for_models(models) == CHUNKING_PRESETS["medium_context"]

    def test_wpo_qwen_active_arctic_secondary_min_across_slots(
        self, monkeypatch
    ) -> None:
        """WP-O RED-PROOF (iii): qwen3 ACTIVE + write-all + arctic-secondary flag
        → ``configured_text_models = [qwen3, arctic]`` (the write fan-out embeds
        BOTH slots). This is the CONFIG-SET assertion — NOT the active-slot chunk
        budget: after the WP-O rework the active-slot chunker is sized to qwen3
        ALONE (unclamped, see TestActiveSlotFidelityInvariant), and the arctic
        secondary is embedded from a bounded sub-window instead of clamping the
        chunk. The min-across-slots utility value is still asserted here (it is a
        retained helper), but ONLY as the "what fits all slots" answer, not the
        boundary the write path uses.

        On pre-WP-O code ``configured_text_models`` returns [qwen3] alone (no arctic
        secondary branch) → the membership assertion FAILS.
        """
        from vco_lib.embedding_service import configured_text_models
        monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")
        monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "true")
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_EMBEDDING_API_KEY", raising=False)
        models = configured_text_models()
        assert models[0] == _QWEN, "active model first"
        assert _ARCTIC in models, (
            "WP-O: the arctic secondary must be configured on a qwen3-active "
            "install when DUAL_EMBEDDING_ARCTIC_SECONDARY is on"
        )
        # The min-across-slots UTILITY (retained, NOT the active write budget)
        # answers "smallest window that fits all slots" = arctic's medium tier.
        assert chunking_preset_for_models(models) == CHUNKING_PRESETS["medium_context"]
        # The ACTIVE-slot chunk budget, by contrast, is qwen3's own xlarge tier
        # (unclamped) — proving the write path does NOT use the min for the active.
        assert chunking_preset_for_model(_QWEN) == CHUNKING_PRESETS["xlarge_context"]
        assert chunking_preset_for_model(_QWEN) != chunking_preset_for_models(models)

    def test_embedding_model_env_override_sizes_active_slot(self, monkeypatch) -> None:
        """R2-3: a custom ``EMBEDDING_MODEL`` install (profile qwen3, dual OFF)
        must size chunks to the CUSTOM model's ctx, not qwen3's. Before the fix
        ``configured_text_models`` derived the active slot from the PROFILE only,
        so ``embeddinggemma:300m-bf16`` (num_ctx 2 048) got qwen3's xlarge budget
        → silent truncation of the active slot.

        Red-proof: the single-model preset returned here must equal
        ``chunking_preset_for_model(env value)`` — reverting
        ``resolve_active_text_model_id`` to derive from the profile makes this
        return the qwen3 (large/xlarge) preset instead → FAIL."""
        from vco_lib.embedding_service import configured_text_models
        custom = "embeddinggemma:300m-bf16"  # num_ctx 2 048, in MODEL_TOKEN_LIMITS
        monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "false")
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        monkeypatch.setenv("EMBEDDING_MODEL", custom)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_EMBEDDING_API_KEY", raising=False)
        models = configured_text_models()
        assert models == [custom], (
            "the active slot must be the EMBEDDING_MODEL env value, not the "
            "profile-derived qwen3 model"
        )
        # The single-model preset must be the custom model's (2 048-ctx) tier,
        # NOT qwen3's larger budget — this is the truncation-hazard guard.
        assert chunking_preset_for_models(models) == chunking_preset_for_model(custom)
        assert chunking_preset_for_models(models) != chunking_preset_for_model(_QWEN), (
            "embeddinggemma (2 048 ctx) and qwen3 must resolve to DIFFERENT presets; "
            "if equal, the truncation hazard is not exercised"
        )

    def test_embedding_model_env_override_with_dual_secondary(self, monkeypatch) -> None:
        """R2-3 + dual: custom active EMBEDDING_MODEL + write-all ON adds qwen3 as
        the secondary; the min-across-slots budget is the TIGHTER of the two."""
        from vco_lib.embedding_service import configured_text_models
        custom = "embeddinggemma:300m-bf16"  # 2 048 ctx (tighter than qwen3)
        monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        monkeypatch.setenv("EMBEDDING_MODEL", custom)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_EMBEDDING_API_KEY", raising=False)
        models = configured_text_models()
        # active = custom (env), secondary = qwen3 (DEFAULT_TEXT_MODEL, != active).
        assert models[0] == custom
        assert _QWEN in models
        # min-across-slots = embeddinggemma's tighter 2 048-ctx tier.
        assert chunking_preset_for_models(models) == chunking_preset_for_model(custom)


class TestChunkerRevisionBumped:
    def test_revision_reflects_dual_budget_change(self) -> None:
        # The sentinel's own contract: bump when boundaries change for anyone. The
        # WP-O rework (v0.2.88) reverts the v0.2.87 min-across-slots clamp back to
        # active-model sizing — active boundaries change for the dual-write installs
        # that ran v0.2.87, so the revision advanced. (Single-model installs were
        # byte-unchanged by both v0.2.87 and this rework.)
        assert _CHUNKER_REVISION != "v0.2.47.5"
        assert _CHUNKER_REVISION != "v0.2.87", (
            "v0.2.87's min-clamp was reverted by WP-O; the revision must advance so "
            "dual-write installs re-sync back to active-model boundaries"
        )
        assert _CHUNKER_REVISION == "v0.2.88"


# ===========================================================================
# R3-1 — CALL-SITE PIN: drive the REAL store_knowledge_node multi-chunk write
# path (server.py ~:6238 chunker construction) rather than the leaf function.
#
# The lesson [[parity-tests-pin-the-call-site-not-the-pure-function-2026-07-21]]:
# a parity invariant that only exercises the leaf (both Chunker sides built by
# the test) stays GREEN when the CALLER is re-wired. Re-introducing the
# min-across-slots clamp at the write site — ``Chunker.for_models(
# configured_text_models())`` in place of ``Chunker.for_model(
# _active_chunk_model_id())`` — passes every leaf-tier test, because those
# never touch server.py's wiring. The tests below run the actual write body and
# read back the STORED chunks, so re-clamping the site changes the persisted
# chunk boundaries and turns them RED.
# ===========================================================================


def _server():
    return importlib.import_module("weaviate_mcp.server")


def _unwrap(tool):
    """Unwrap an @mcp.tool()-decorated function to its plain callable."""
    return getattr(tool, "fn", None) or getattr(tool, "__wrapped__", None) or tool


class _FakeObj:
    def __init__(self, properties: dict):
        self.uuid = str(_uuid.uuid4())
        self.properties = properties


class _FakePredicate:
    def __init__(self, fn):
        self._fn = fn

    def matches(self, props: dict) -> bool:
        return bool(self._fn(props))

    def __and__(self, other: "_FakePredicate") -> "_FakePredicate":
        return _FakePredicate(lambda p: self.matches(p) and other.matches(p))


class _FakeByProperty:
    def __init__(self, name: str):
        self._name = name

    def equal(self, value) -> _FakePredicate:
        return _FakePredicate(lambda p, n=self._name, v=value: p.get(n) == v)


class _FakeFilter:
    @staticmethod
    def by_property(name: str) -> _FakeByProperty:
        return _FakeByProperty(name)

    @staticmethod
    def any_of(predicates) -> _FakePredicate:
        preds = list(predicates)
        return _FakePredicate(lambda p: any(pr.matches(p) for pr in preds))


class _FakeQuery:
    def __init__(self, coll: "_FakeCollection"):
        self._coll = coll

    def fetch_objects(self, filters=None, limit=100, offset=0):
        objs = [
            o for o in self._coll.objects
            if filters is None or filters.matches(o.properties)
        ]

        class _Resp:
            pass

        resp = _Resp()
        start = offset or 0
        resp.objects = objs[start:start + limit]
        return resp


class _FakeData:
    def __init__(self, coll: "_FakeCollection"):
        self._coll = coll

    def insert(self, properties=None, vector=None):
        self._coll.objects.append(_FakeObj(dict(properties or {})))

    def delete_by_id(self, uid):
        self._coll.objects = [o for o in self._coll.objects if o.uuid != uid]


class _FakeCollection:
    def __init__(self):
        self.objects: list[_FakeObj] = []
        self.query = _FakeQuery(self)
        self.data = _FakeData(self)


class _FakeCollections:
    def __init__(self, coll: _FakeCollection):
        self._coll = coll

    def get(self, name: str) -> _FakeCollection:
        return self._coll


class _FakeClient:
    def __init__(self, coll: _FakeCollection):
        self.collections = _FakeCollections(coll)


# A large, deterministic body whose UNCLAMPED (qwen3 xlarge, max 13 500) and
# CLAMPED (min-across-slots → arctic medium, max 3 200) chunk sets DIVERGE in
# count and boundaries — the divergence input the parity lesson requires.
def _divergent_body() -> str:
    sentence = "The retrieval model scores each candidate node against the query. "
    return " ".join(
        f"{sentence}Item {i} details here and more words to pad this out nicely."
        for i in range(700)
    )


def _chunk_header_prefix(n_from_one: int, total: int) -> str:
    return f"[chunk {n_from_one}/{total}]\n\n"


def _stored_chunk_bodies(coll: _FakeCollection) -> "list[str]":
    """Read back the chunk contents the write path persisted, with the
    ``[chunk N/total]`` ordering header stripped, in insertion order."""
    bodies = []
    for obj in coll.objects:
        content = obj.properties.get("content", "")
        # Strip the "[chunk N/M]\n\n" header the write path prepends.
        if content.startswith("[chunk "):
            nl = content.find("\n\n")
            if nl != -1:
                content = content[nl + 2:]
        bodies.append(content)
    return bodies


def _patch_server_for_write(monkeypatch, tmp_path, coll: _FakeCollection):
    """Patch the server so store_knowledge_node runs against the fake collection
    on the MULTI-CHUNK path (token_count forced above the single-chunk floor),
    embedding each chunk deterministically."""
    srv = _server()
    monkeypatch.setattr(srv, "get_weaviate_client", lambda: _FakeClient(coll))
    monkeypatch.setattr(srv, "Filter", _FakeFilter)
    monkeypatch.setattr(srv, "KG_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "EMBEDDING_SOURCE", "ollama")
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", False)
    monkeypatch.setattr(srv, "_emit_gate_skipped_metric", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_emit_gate_skipped_deferral", lambda *a, **k: None)
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)

    async def _count_tokens(content):
        # Force the multi-chunk branch: real token count of the divergent body.
        return TokenCounter.count_tokens(content)

    monkeypatch.setattr(srv, "count_tokens_async", _count_tokens)

    async def _embed(_text):
        return [0.1] * 8

    monkeypatch.setattr(srv, "get_embedding", _embed)
    return srv


def _store(srv, **kwargs) -> dict:
    fn = _unwrap(srv.store_knowledge_node)
    defaults = dict(
        title="Call Site Pin",
        content=_divergent_body(),
        node_type="concept",
        tags=["sample"],
        links=[],
        file_path="knowledge/concepts/call_site_pin.md",
        scope="project",
    )
    defaults.update(kwargs)
    raw = asyncio.run(fn(**defaults))
    return json.loads(raw)


class TestCallSiteActiveFidelity:
    """R3-1: the active-fidelity invariant pinned at the WRITE call site.

    ``store_knowledge_node`` builds its multi-chunk chunker from
    ``Chunker.for_model(_active_chunk_model_id())`` (server.py ~:6238). These
    tests drive that real body and read back the stored chunks, so re-introducing
    the min-across-slots clamp AT THE SITE (``Chunker.for_models(
    configured_text_models())``) changes the persisted boundaries and fails —
    which the leaf-tier tests above cannot detect.
    """

    def _dual_on_env(self, monkeypatch) -> None:
        # qwen3 ACTIVE + write-all + arctic secondary. If the site used the min
        # clamp, configured_text_models() = [qwen3, arctic] → arctic-medium
        # boundaries (9 chunks). The correct site uses the ACTIVE model alone.
        monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")
        monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "true")
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        monkeypatch.setenv("EMBEDDING_MODEL", _QWEN)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_EMBEDDING_API_KEY", raising=False)

    def _dual_off_env(self, monkeypatch) -> None:
        monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "false")
        monkeypatch.delenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", raising=False)
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")
        monkeypatch.setenv("EMBEDDING_MODEL", _QWEN)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_EMBEDDING_API_KEY", raising=False)

    def test_divergent_body_actually_diverges(self) -> None:
        """Sanity: the fixture body must produce a DIFFERENT chunk count under the
        unclamped active budget vs the min-across-slots clamp — otherwise the
        call-site pin below could not bite on a re-clamp."""
        body = _divergent_body()
        unclamped = Chunker.for_model(_QWEN).chunk_text(body, source_id="x")
        clamped = Chunker.for_models([_QWEN, _ARCTIC]).chunk_text(body, source_id="x")
        assert len(unclamped) != len(clamped), (
            "the divergence fixture no longer diverges — re-size _divergent_body "
            "so unclamped (qwen3 xlarge) and clamped (arctic medium) differ"
        )

    def test_write_site_uses_unclamped_active_boundaries(
        self, monkeypatch, tmp_path
    ) -> None:
        """Under dual-write ON, the STORED chunks must equal the UNCLAMPED
        active-model (qwen3) sizing — NOT the min-across-slots clamp. If the site
        is re-clamped to ``Chunker.for_models(configured_text_models())``, the
        persisted chunk count/boundaries become arctic-medium and this fails."""
        self._dual_on_env(monkeypatch)
        coll = _FakeCollection()
        srv = _patch_server_for_write(monkeypatch, tmp_path, coll)
        result = _store(srv)
        assert result.get("success") is True

        stored = _stored_chunk_bodies(coll)
        expected = [
            c.content
            for c in Chunker.for_model(_QWEN).chunk_text(
                _divergent_body(), source_id="s"
            )
        ]
        assert stored == expected, (
            "the write site stored non-active-model chunk boundaries — the "
            "min-across-slots clamp appears to have been reintroduced at "
            "server.py's Chunker construction (~:6238). Stored count "
            f"{len(stored)}, expected unclamped active count {len(expected)}."
        )
        # And explicitly NOT the clamped set (belt-and-braces on the re-clamp).
        clamped = [
            c.content
            for c in Chunker.for_models([_QWEN, _ARCTIC]).chunk_text(
                _divergent_body(), source_id="c"
            )
        ]
        assert stored != clamped, (
            "stored chunks match the CLAMPED (min-across-slots) boundaries — the "
            "active slot's fidelity dropped below single-write at the write site"
        )

    def test_write_site_boundaries_identical_dual_on_vs_off(
        self, monkeypatch, tmp_path
    ) -> None:
        """The persisted active-slot chunks must be byte-identical whether
        dual-write is ON or OFF — driven through the real write path both times.
        A re-clamp diverges the two (arctic-medium under ON, qwen3-xlarge under
        OFF), so this is the direct dual-on-vs-off call-site pin."""
        self._dual_on_env(monkeypatch)
        coll_on = _FakeCollection()
        srv = _patch_server_for_write(monkeypatch, tmp_path, coll_on)
        assert _store(srv).get("success") is True
        stored_on = _stored_chunk_bodies(coll_on)

        self._dual_off_env(monkeypatch)
        coll_off = _FakeCollection()
        srv = _patch_server_for_write(monkeypatch, tmp_path / "off", coll_off)
        assert _store(srv).get("success") is True
        stored_off = _stored_chunk_bodies(coll_off)

        assert stored_on == stored_off, (
            "active-slot chunk boundaries differ between dual-write ON and OFF at "
            "the write path — the invariant WP-O exists to hold is broken. This "
            "fires when the site re-clamps to min-across-slots under dual-ON."
        )
        # Both equal the unclamped active-model sizing.
        expected = [
            c.content
            for c in Chunker.for_model(_QWEN).chunk_text(
                _divergent_body(), source_id="s"
            )
        ]
        assert stored_on == expected

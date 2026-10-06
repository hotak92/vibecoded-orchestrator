# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 — per-model QUERY-side instruction prefixes.

Retrieval quality on some embedding models depends on a short instruction
prepended to SEARCH QUERIES only (documents stay unprefixed, so stored vectors
are byte-identical and nothing is re-embedded). Before this release every
search embedded its query exactly like a document.

The prefix table lives ONCE, beside ``MODEL_TOKEN_LIMITS`` in
``claude_mcp_servers/weaviate_mcp/chunking.py`` (``MODEL_QUERY_PREFIXES``). The
merge-side query methods (``EmbeddingService.embed_text_query`` /
``embed_code_query``) apply it; the code-embed SERVICE resolves its own loaded
model's prefix from the SAME table.

RED-PROOF design: every wiring assertion mutates the production call and
asserts the behaviour flips (a no-op mutation makes the test fail), never a
source scan.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.weaviate_mcp.chunking import (  # noqa: E402
    MODEL_QUERY_PREFIXES,
    MODEL_TOKEN_LIMITS,
    QUERY_TASKS,
    query_prefix_for_model,
)
from vco_lib.embedding_providers.codeembed import CodeEmbedAdapter  # noqa: E402
from vco_lib.embedding_providers.ollama import OllamaAdapter  # noqa: E402
from vco_lib.embedding_providers.openai import OpenAIAdapter  # noqa: E402
from vco_lib.embedding_service import EmbeddingService  # noqa: E402

QWEN3 = "qwen3-embedding:0.6b"
ARCTIC = "snowflake-arctic-embed2:latest"


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------


def test_qwen3_prefix_is_the_card_template_verbatim():
    """qwen3-embedding's ST ``prompts["query"]`` is templated on the task and
    joins the text with NO space after 'Query:' (model card, re-checked
    2026-10-04)."""
    assert MODEL_QUERY_PREFIXES["qwen3-embedding"] == "Instruct: {task}\nQuery:"
    prefix = query_prefix_for_model(QWEN3, "kg_search")
    assert prefix.startswith("Instruct: ")
    assert prefix.endswith("\nQuery:")
    assert not prefix.endswith("Query: ")  # no space after the colon


def test_arctic_prefix_is_literal_and_keeps_the_trailing_space():
    """snowflake-arctic-embed2's ST ``prompts["query"]`` is exactly 'query: '
    (trailing space matters, and it is NOT task-templated)."""
    assert MODEL_QUERY_PREFIXES["snowflake-arctic-embed2"] == "query: "
    for task in (None, "kg_search", "hook_injection", "code_nl"):
        assert query_prefix_for_model(ARCTIC, task) == "query: "


def test_instruction_free_models_resolve_to_empty():
    """jina-code, codesage and OpenAI text-embedding-3-small need NO prefix
    (model cards re-checked 2026-10-04: no ST prompt config / symmetric API)."""
    for model in (
        "jina-embeddings-v2-base-code",
        "unclemusclez/jina-embeddings-v2-base-code:latest",
        "codesage/codesage-large-v2",
        "codesage-large-v2",
        "text-embedding-3-small",
    ):
        assert query_prefix_for_model(model) == "", model


def test_unknown_model_gets_no_prefix():
    """A model the table does not know is never handed an instruction (only
    models whose card asks for one get one)."""
    assert query_prefix_for_model("totally-unknown-model:42b") == ""


def test_task_wording_is_distinct_per_use():
    """KG search, hook injection, NL→code and code→code carry DIFFERENT task
    sentences in the templated qwen3 instruction."""
    kg = query_prefix_for_model(QWEN3, "kg_search")
    hook = query_prefix_for_model(QWEN3, "hook_injection")
    code_nl = query_prefix_for_model(QWEN3, "code_nl")
    code_sim = query_prefix_for_model(QWEN3, "code_similarity")
    assert len({kg, hook, code_nl, code_sim}) == 4
    assert QUERY_TASKS["hook_injection"] in hook
    # Unknown task key falls back to the default (KG) wording.
    assert query_prefix_for_model(QWEN3, "no-such-task") == kg


def test_removed_non_vco_models_absent_from_prefix_table():
    """bge-m3 / embeddinggemma / granite-embedding are not VCO models — they
    must appear in NO table (token limits OR prefixes)."""
    for removed in (
        "bge-m3", "bge-m3:latest",
        "embeddinggemma", "embeddinggemma:300m-bf16",
        "granite-embedding", "granite-embedding:278m-fp16",
    ):
        assert removed not in MODEL_TOKEN_LIMITS
        assert all(removed not in key for key in MODEL_QUERY_PREFIXES)


def test_tag_variant_resolves_to_base_entry():
    """The lookup uses the same partial-match rule as the num_ctx resolver."""
    assert query_prefix_for_model("qwen3-embedding:0.6b") == query_prefix_for_model(
        "qwen3-embedding"
    )
    assert query_prefix_for_model("snowflake-arctic-embed2:568m") == "query: "


# ---------------------------------------------------------------------------
# EmbeddingService: prefix on QUERIES, none on DOCUMENTS
# ---------------------------------------------------------------------------


def _build_service(*, text_model: str, code_model: str, ollama_embed, code_embed):
    """An EmbeddingService with fully mocked adapters (no network)."""
    ollama = MagicMock(spec=OllamaAdapter)
    ollama.is_reachable.return_value = True
    ollama.embed.side_effect = ollama_embed

    codee = MagicMock(spec=CodeEmbedAdapter)
    codee.is_reachable.return_value = True
    codee.embed.side_effect = code_embed

    oa = MagicMock(spec=OpenAIAdapter)
    oa.api_key = ""
    oa.is_reachable.return_value = False

    return EmbeddingService(
        project_root=None,
        ollama_url="http://localhost:11435",
        code_embed_url="http://localhost:11440",
        text_model_id=text_model,
        code_model_id=code_model,
        openai_api_key="",
        ollama_adapter=ollama,
        code_adapter=codee,
        openai_adapter=oa,
    )


def test_qwen3_text_query_is_prefixed_and_document_is_not():
    sent: list[str] = []

    def _ollama_embed(model, text, num_ctx=None):
        sent.append(text)
        return [0.1, 0.2, 0.3]

    svc = _build_service(
        text_model=QWEN3,
        code_model="codesage/codesage-large-v2",
        ollama_embed=_ollama_embed,
        code_embed=lambda text, is_query=False, task=None: [1.0],
    )
    svc.embed_text("hello")                      # DOCUMENT
    assert sent[-1] == "hello"
    svc.embed_text_query("hello")                # QUERY
    assert sent[-1] == query_prefix_for_model(QWEN3, "kg_search") + "hello"


def test_arctic_text_query_prefix_is_applied():
    sent: list[str] = []

    def _ollama_embed(model, text, num_ctx=None):
        sent.append(text)
        return [0.1]

    svc = _build_service(
        text_model=ARCTIC,
        code_model="codesage/codesage-large-v2",
        ollama_embed=_ollama_embed,
        code_embed=lambda text, is_query=False, task=None: [1.0],
    )
    svc.embed_text_query("hello")
    assert sent[-1] == "query: hello"


def test_instruction_free_text_query_equals_document():
    sent: list[str] = []

    def _ollama_embed(model, text, num_ctx=None):
        sent.append(text)
        return [0.1]

    svc = _build_service(
        text_model="jina-embeddings-v2-base-code",
        code_model="jina-embeddings-v2-base-code",
        ollama_embed=_ollama_embed,
        code_embed=lambda text, is_query=False, task=None: [1.0],
    )
    # jina needs no query prefix → query and document embeds are identical.
    assert svc.embed_text_query("hello") == svc.embed_text("hello")
    assert sent == ["hello"]


def test_code_query_sends_is_query_true_to_the_service():
    seen: list[tuple] = []

    def _code_embed(text, is_query=False, task=None):
        seen.append((text, is_query))
        return [1.0, 2.0]

    svc = _build_service(
        text_model=QWEN3,
        code_model="codesage/codesage-large-v2",
        ollama_embed=lambda model, text, num_ctx=None: [0.0],
        code_embed=_code_embed,
    )
    svc.embed_code("def f(): pass")           # DOCUMENT / entity
    assert seen[-1] == ("def f(): pass", False)
    svc.embed_code_query("def f(): pass")     # QUERY
    assert seen[-1] == ("def f(): pass", True)
    # The client does NOT prepend for the service leg — the service owns the
    # prefix (no double prefix). The RAW text — never a client-side prefixed
    # copy — is what crosses the wire (v0.2.101 SF-2 nit: pin the text, not
    # just the is_query flag, or a client-side prepend would stay green).
    assert [is_q for _, is_q in seen].count(True) == 1


def test_code_query_service_leg_forwards_the_task():
    """SF-2: a caller-supplied task must NOT be dropped on the service leg —
    the service resolves its model's prefix for THAT task, and the RAW query
    text is what reaches it (no client-side prepend → no double prefix)."""
    seen: list[tuple] = []

    def _code_embed(text, is_query=False, task=None):
        seen.append((text, is_query, task))
        return [1.0, 2.0]

    svc = _build_service(
        text_model=QWEN3,
        code_model="codesage/codesage-large-v2",
        ollama_embed=lambda model, text, num_ctx=None: [0.0],
        code_embed=_code_embed,
    )
    svc.embed_code_query("def f(): pass", task="code_similarity")
    assert seen[-1] == ("def f(): pass", True, "code_similarity")
    svc.embed_code_query("def f(): pass")  # default → code wording
    assert seen[-1] == ("def f(): pass", True, "code_nl")


def test_code_query_ollama_leg_applies_prefix_client_side():
    sent: list[str] = []

    def _ollama_embed(model, text, num_ctx=None):
        sent.append(text)
        return [0.1]

    # code slot is the qwen3 fallback (no codesage service in play).
    svc = _build_service(
        text_model=QWEN3,
        code_model=QWEN3,
        ollama_embed=_ollama_embed,
        code_embed=lambda text, is_query=False, task=None: [0.0],
    )
    svc.codeembed.is_reachable.return_value = False
    svc.embed_code_query("def f(): pass", task="code_nl")
    assert sent[-1] == query_prefix_for_model(QWEN3, "code_nl") + "def f(): pass"


def test_code_query_defaults_to_the_code_task_not_the_kg_task():
    """A code query with NO explicit task must get the CODE wording — never
    the knowledge-graph instruction (the two are different sentences)."""
    sent: list[str] = []

    def _ollama_embed(model, text, num_ctx=None):
        sent.append(text)
        return [0.1]

    svc = _build_service(
        text_model=QWEN3,
        code_model=QWEN3,
        ollama_embed=_ollama_embed,
        code_embed=lambda text, is_query=False, task=None: [0.0],
    )
    svc.codeembed.is_reachable.return_value = False
    svc.embed_code_query("def f(): pass")  # no task
    assert QUERY_TASKS["code_nl"] in sent[-1]
    assert QUERY_TASKS["kg_search"] not in sent[-1]


# ---------------------------------------------------------------------------
# Search paths + the hook-injection path use the query methods
# ---------------------------------------------------------------------------


class _RecordingSvc:
    text_vector_slot = "qwen3_embed"
    code_vector_slot = "codesage_embed"

    def __init__(self) -> None:
        self.calls: dict[str, tuple] = {}

    def embed_text_query(self, text, task=None):
        self.calls["text_query"] = (text, task)
        return [0.11]

    def embed_code_query(self, text, task=None):
        self.calls["code_query"] = (text, task)
        return [0.22]

    def embed_text(self, text):  # pragma: no cover — must never be hit
        raise AssertionError("document-side embed_text used on a search path")

    def embed_code(self, text):  # pragma: no cover — must never be hit
        raise AssertionError("document-side embed_code used on a search path")


def test_get_search_vector_uses_query_method(monkeypatch):
    import weaviate_mcp.server as server

    fake = _RecordingSvc()
    monkeypatch.setattr(server, "_get_embedding_service", lambda: fake)

    vec, target = asyncio.run(server._get_search_vector("q", "kg"))
    assert vec == [0.11] and target == "qwen3_embed"
    assert fake.calls["text_query"] == ("q", None)

    vec, target = asyncio.run(server._get_search_vector("q", "code"))
    assert vec == [0.22] and target == "codesage_embed"
    assert fake.calls["code_query"] == ("q", None)


def test_get_code_query_embedding_uses_query_method(monkeypatch):
    import weaviate_mcp.server as server

    fake = _RecordingSvc()
    monkeypatch.setattr(server, "_get_embedding_service", lambda: fake)

    vec = asyncio.run(server.get_code_query_embedding("q", task="code_nl"))
    assert vec == [0.22]
    assert fake.calls["code_query"] == ("q", "code_nl")


def test_hook_path_uses_query_method_with_hook_task(monkeypatch):
    import weaviate_mcp.server as server
    import claude_mcp_servers.scripts.rl_kg_search as hook

    seen: dict = {}

    async def _fake_search_vector(text, scheme="kg", task=None):
        seen["text"] = text
        seen["task"] = task
        return [1.0], "qwen3_embed"

    monkeypatch.setattr(server, "_get_search_vector", _fake_search_vector)
    vec, target = asyncio.run(hook.embed_hook_query("edit context"))
    assert vec == [1.0] and target == "qwen3_embed"
    assert seen == {"text": "edit context", "task": "hook_injection"}


# ---------------------------------------------------------------------------
# Code-embed SERVICE resolves its loaded model's prefix from the same table
# ---------------------------------------------------------------------------


def test_service_resolves_prefix_from_shared_table():
    import claude_mcp_servers.code_embedding_service.server as ces

    # A model that wants an instruction resolves it from the SHARED table.
    assert ces._resolve_query_instruction(QWEN3) == query_prefix_for_model(
        QWEN3, "code_nl"
    )
    # The shipped container models need none.
    assert ces._resolve_query_instruction("codesage/codesage-large-v2") == ""
    assert ces._resolve_query_instruction("jina-embeddings-v2-base-code") == ""


def test_service_code_embed_instruction_env_is_the_override(monkeypatch):
    import claude_mcp_servers.code_embedding_service.server as ces

    monkeypatch.setattr(ces, "INSTRUCTION", "OVERRIDE: ")
    assert ces._resolve_query_instruction(QWEN3) == "OVERRIDE: "


def test_service_applies_instruction_only_on_query(monkeypatch):
    import requests

    import claude_mcp_servers.code_embedding_service.server as ces

    captured: list[str] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"embedding": [0.1, 0.2]}

    def _fake_post(url, json=None, timeout=None):
        captured.append(json["prompt"])
        return _Resp()

    monkeypatch.setattr(ces, "MODEL_NAME", "some-model")
    monkeypatch.setattr(ces, "INSTRUCTION", "PFX:")
    monkeypatch.setattr(requests, "post", _fake_post)

    ces._embed_ollama(["hello"], is_query=False)
    assert captured[-1] == "hello"
    ces._embed_ollama(["hello"], is_query=True)
    assert captured[-1] == "PFX:hello"


def test_service_task_selects_the_wording():
    """SF-2: the service resolves the instruction for the REQUESTED task — a
    similarity query must not get the code_nl wording (when the model has a
    templated prefix)."""
    import claude_mcp_servers.code_embedding_service.server as ces

    nl = ces._resolve_query_instruction(QWEN3, "code_nl")
    sim = ces._resolve_query_instruction(QWEN3, "code_similarity")
    assert QUERY_TASKS["code_nl"] in nl
    assert QUERY_TASKS["code_similarity"] in sim
    assert nl != sim


# ---------------------------------------------------------------------------
# CLI search paths (load the script by path, then drive its embed helper)
# ---------------------------------------------------------------------------


def _load_script(name: str, relpath: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_query_code_graph_cli_uses_code_query_method(monkeypatch):
    """templates/scripts/query_code_graph.py::generate_code_embedding embeds a
    QUERY — it must route through ``embed_code_query`` (the MCP calls
    ``get_code_query_embedding``, which does the same: the CLI≡MCP invariant)."""
    mod = _load_script("_qcg_v02101", "templates/scripts/query_code_graph.py")
    seen: dict = {}

    class _Svc:
        def embed_code_query(self, text, task=None):
            seen["query"] = (text, task)
            return [0.5]

        def embed_code(self, text):  # pragma: no cover — must not be used
            raise AssertionError("CLI query path used the document-side embed_code")

    monkeypatch.setattr(mod, "_get_or_create_embedding_service", lambda: _Svc())
    assert mod.generate_code_embedding("find auth") == [0.5]
    assert seen["query"] == ("find auth", None)


def test_search_knowledge_cli_uses_text_query_method(monkeypatch):
    """templates/scripts/search_knowledge.py::get_embedding embeds the CLI's
    retrieval QUERY — it must route through ``embed_text_query``."""
    mod = _load_script("_sk_v02101", "templates/scripts/search_knowledge.py")
    seen: dict = {}

    class _Svc:
        def embed_text_query(self, text, task=None):
            seen["query"] = (text, task)
            return [0.7]

        def embed_text(self, text):  # pragma: no cover — must not be used
            raise AssertionError("CLI query path used the document-side embed_text")

    monkeypatch.setattr(mod, "_get_or_create_embedding_service", lambda: _Svc())
    assert mod.get_embedding("how does x work") == [0.7]
    assert seen["query"] == ("how does x work", None)


# ---------------------------------------------------------------------------
# Code → code similarity: the code-graph "similar" path (SF-2)
# ---------------------------------------------------------------------------


def _fake_similarity_client(rec: dict, ref, other):
    from types import SimpleNamespace

    class _QB:
        def __init__(self, res):
            self._res = res

        def where(self, *a, **k):
            return self

        def do(self):
            return self._res

    class _Q:
        def fetch_objects(self, **kw):
            return SimpleNamespace(objects=[ref])

        def near_vector(self, **kw):
            rec["near_vector"] = kw
            return _QB(SimpleNamespace(objects=[ref, other]))

        def near_object(self, **kw):
            rec["near_object"] = kw
            return _QB(SimpleNamespace(objects=[ref, other]))

    class _Coll:
        def __init__(self):
            self.query = _Q()

    class _Client:
        class _Colls:
            @staticmethod
            def get(_name):
                return _Coll()

        def __init__(self):
            self.collections = self._Colls()

    return _Client()


def _similarity_objs():
    from types import SimpleNamespace

    class _Obj:
        def __init__(self, uuid, props, distance=0.1):
            self.uuid = uuid
            self.properties = props
            self.metadata = SimpleNamespace(distance=distance)

    ref = _Obj(
        "ref-uuid",
        {"full_name": "pkg.f", "function_body": "def f(): ...", "signature": "f()"},
    )
    other = _Obj("other-uuid", {"full_name": "pkg.g", "signature": "g()"}, 0.4)
    return ref, other


def test_code_graph_similar_path_uses_code_similarity_task(monkeypatch):
    """SF-2: the code-graph ``similar`` subcommand is the code→code path — it
    must embed the reference entity as a QUERY with the ``code_similarity``
    wording (not ``code_nl``), then search by vector."""
    mod = _load_script("_qcg_similar_v02101", "templates/scripts/query_code_graph.py")
    ref, other = _similarity_objs()
    rec: dict = {}
    captured: dict = {}

    class _Svc:
        def embed_code_query(self, text, task=None):
            captured["text"] = text
            captured["task"] = task
            return [0.1, 0.2, 0.3]

    monkeypatch.setattr(mod, "_get_or_create_embedding_service", lambda: _Svc())
    monkeypatch.setattr(mod, "_active_code_vector_slot", lambda: "codesage_embed")

    q = mod.CodeGraphQuery(project=None)
    q.client = _fake_similarity_client(rec, ref, other)
    q.find_similar("pkg.f", "CodeFunction", 5)

    assert captured == {"text": "def f(): ...", "task": "code_similarity"}
    assert "near_vector" in rec, "the similarity path must embed + search by vector"
    assert "near_object" not in rec
    assert rec["near_vector"]["target_vector"] == "codesage_embed"


def test_code_graph_similar_falls_back_to_near_object_without_a_backend(monkeypatch):
    """No embedding backend → the historical ``near_object`` path (the command
    keeps working on a half-migrated install)."""
    mod = _load_script("_qcg_similar_fb_v02101", "templates/scripts/query_code_graph.py")
    ref, other = _similarity_objs()
    rec: dict = {}

    monkeypatch.setattr(mod, "_get_or_create_embedding_service", lambda: None)

    q = mod.CodeGraphQuery(project=None)
    q.client = _fake_similarity_client(rec, ref, other)
    q.find_similar("pkg.f", "CodeFunction", 5)

    assert "near_object" in rec
    assert "near_vector" not in rec


def test_code_graph_similar_falls_back_when_near_vector_execution_fails(monkeypatch):
    """SF-2 nit: ``near_vector`` can fail at EXECUTION time — a dimension/slot
    mismatch against the collection's named vector — not only while the query
    is built. The command must catch that and fall back to the historical
    ``near_object`` path, which always worked on a half-migrated install,
    rather than error out a call that used to succeed."""
    from types import SimpleNamespace

    mod = _load_script("_qcg_similar_exec_v02101", "templates/scripts/query_code_graph.py")
    ref, other = _similarity_objs()
    rec: dict = {}

    class _QB:
        def __init__(self, res=None, explode=False):
            self._res = res
            self._explode = explode

        def where(self, *a, **k):
            return self

        def do(self):
            if self._explode:
                raise RuntimeError("vector dimension mismatch")
            return self._res

    class _Q:
        def fetch_objects(self, **kw):
            return SimpleNamespace(objects=[ref])

        def near_vector(self, **kw):
            rec["near_vector"] = kw
            return _QB(explode=True)          # builds fine, FAILS on execute

        def near_object(self, **kw):
            rec["near_object"] = kw
            return _QB(SimpleNamespace(objects=[ref, other]))

    class _Coll:
        def __init__(self):
            self.query = _Q()

    class _Client:
        class _Colls:
            @staticmethod
            def get(_name):
                return _Coll()

        def __init__(self):
            self.collections = self._Colls()

    class _Svc:
        def embed_code_query(self, text, task=None):
            return [0.1, 0.2, 0.3]

    monkeypatch.setattr(mod, "_get_or_create_embedding_service", lambda: _Svc())
    monkeypatch.setattr(mod, "_active_code_vector_slot", lambda: "codesage_embed")

    q = mod.CodeGraphQuery(project=None)
    q.client = _Client()
    q.find_similar("pkg.f", "CodeFunction", 5)   # must NOT raise

    assert "near_vector" in rec, "the vector path must be attempted first"
    assert "near_object" in rec, (
        "a near_vector EXECUTION failure must fall back to near_object, "
        "not error out the command"
    )


# ---------------------------------------------------------------------------
# RL dual-log twin query (caller audit Gap 1)
# ---------------------------------------------------------------------------


def _fanout_service(monkeypatch, sent, *, active_model=ARCTIC):
    """An EmbeddingService with dual-write + arctic-secondary ON, and an
    Ollama mock recording every ``(model, text)`` it is handed."""
    monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")
    monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "true")

    def _ollama_embed(model, text, num_ctx=None):
        sent.append((model, text))
        return [0.1] * 4

    return _build_service(
        text_model=active_model,
        code_model="codesage/codesage-large-v2",
        ollama_embed=_ollama_embed,
        code_embed=lambda text, is_query=False, task=None: [0.0],
    )


def test_query_fanout_prefixes_each_slot_documents_do_not(monkeypatch):
    """The query-side fan-out is the DOCUMENT fan-out's twin: each slot gets
    ITS model's query prefix, while documents stay byte-identical."""
    sent: list[tuple] = []
    svc = _fanout_service(monkeypatch, sent)

    # DOCUMENT fan-out → unprefixed for every slot.
    svc.embed_text_all_configured("hello")
    doc = dict(sent)
    assert doc[ARCTIC] == "hello"
    assert doc["qwen3-embedding:0.6b"] == "hello"

    # QUERY fan-out → each slot carries its own model's prefix.
    sent.clear()
    svc.embed_text_query_all_configured("hello")
    qry = dict(sent)
    assert qry[ARCTIC] == query_prefix_for_model(ARCTIC, "kg_search") + "hello"
    assert qry["qwen3-embedding:0.6b"] == (
        query_prefix_for_model("qwen3-embedding:0.6b", "kg_search") + "hello"
    )
    assert qry[ARCTIC] != doc[ARCTIC]


def test_dual_log_twin_query_is_the_slot_query_embedding_with_the_search_task(monkeypatch):
    """Gap 1: the logged twin vector must equal what a retrieval in THAT slot
    would produce — that model's prefix + the SAME task wording as the search
    (``hook_injection`` here), never the unprefixed document vector."""
    import weaviate_mcp.server as server
    import weaviate_mcp.rl_enrichment as rle

    sent: list[tuple] = []
    # Active slot = arctic2_embed, so the fan-out adds the qwen3 secondary:
    # the twin lands on qwen3_embed (a model whose prefix is templated on task).
    svc = _fanout_service(monkeypatch, sent)
    monkeypatch.setattr(server, "_resolve_dual_rl_log_enabled", lambda: True)
    monkeypatch.setattr(server, "_get_embedding_service", lambda: svc)
    monkeypatch.setattr(
        "vco_lib.embedding_service.configured_text_models",
        lambda: [ARCTIC, "qwen3-embedding:0.6b"],
    )

    dual = asyncio.run(
        rle._resolve_dual_rl_log_inputs(
            "how does x work", "arctic2_embed", query_task="hook_injection"
        )
    )
    assert dual is not None and dual["other_slot"] == "qwen3_embed"
    twin_text = dict(sent)["qwen3-embedding:0.6b"]
    assert twin_text == (
        query_prefix_for_model("qwen3-embedding:0.6b", "hook_injection")
        + "how does x work"
    )
    # It must NOT be the document vector (the pre-Gap-1 shape).
    sent.clear()
    svc.embed_text_all_configured("how does x work")
    assert dict(sent)["qwen3-embedding:0.6b"] != twin_text


def test_hook_twin_query_task_matches_the_hook_retrieval(monkeypatch, tmp_path):
    """Wiring (not a source scan): the hook's twin query must be embedded with
    the SAME task wording its retrieval used (``hook_injection``) — run the real
    hook flow against the service-free harness and read the recorded task."""
    import importlib

    from tests.common import rl_kg_search_harness as H
    from tests import test_v02100_dual_rl_hook_logging as _base

    proj = tmp_path / "proj_a"
    (proj / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    monkeypatch.setenv("VCT_SESSION_ID", "sess-gap1")
    for k, v in _base.DUAL_ENV.items():
        monkeypatch.setenv(k, v)

    from vco_lib import rl_telemetry_loss

    rl_telemetry_loss._reset_warned_for_test()

    mod = H.import_rl_kg_search()
    state: dict = {}
    H.install_fakes(
        importlib.import_module("weaviate_mcp.server"),
        importlib.import_module("claude_mcp_servers.rl_client.search_pipeline"),
        cfg={"collections": ["ProjA_KnowledgeGraph"]},
        state=state,
        patch=monkeypatch.setattr,
    )
    H.run_main(mod)
    assert state["svc"].query_tasks == ["hook_injection"], (
        "the hook's dual-log twin must use the hook_injection task, matching "
        "the vector its retrieval logged"
    )
#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Code Graph Analyzer - Extract code entities and relationships into Weaviate

Creates and populates Weaviate collections for code analysis:
- CodeModule: Files/modules with imports and complexity metrics
- CodeClass: Classes with inheritance and methods
- CodeFunction: Functions with call graphs
- CodeAPI: API endpoints with handlers (for web frameworks)

Supports incremental analysis (only re-parse changed files).

Supported languages: Python (AST), Lua (regex), C++/C (regex), JavaScript/TypeScript/JSX (regex),
                    Go (regex), Rust (regex), Java (regex), Ruby (regex), Shell (regex).

Usage:
    python analyze_code_graph.py /path/to/repo
    python analyze_code_graph.py /path/to/repo --project "MyProject"
    python analyze_code_graph.py /path/to/repo --incremental
    python analyze_code_graph.py /path/to/repo --create-collections
    python analyze_code_graph.py /path/to/repo --language python
    python analyze_code_graph.py /path/to/repo --language lua
    python analyze_code_graph.py /path/to/repo --language cpp
    python analyze_code_graph.py /path/to/repo --language javascript
    python analyze_code_graph.py /path/to/repo --language typescript
"""

import argparse
import ast
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import uuid
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple
import subprocess


def _deterministic_uuid(project: str, full_name: str) -> str:
    """Generate a deterministic UUID from project + full_name.

    This ensures that re-indexing the same entity produces the same UUID,
    turning inserts into upserts and preventing duplicates in Weaviate.
    """
    key = f"{project}::{full_name}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, key))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Collection name helpers for per-project code graph collections
# ---------------------------------------------------------------------------

# Base collection names (used as suffixes with project prefix)
CODE_GRAPH_BASES = ["CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction"]


def _sanitize_collection_prefix(name: str) -> str:
    """Sanitize project name for use as Weaviate collection prefix.

    Weaviate collection names must be alphanumeric + underscore, starting with uppercase.
    """
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    if sanitized and not sanitized[0].isupper():
        sanitized = sanitized[0].upper() + sanitized[1:]
    return sanitized


def _collection_name(base: str, project_name: str) -> str:
    """Return per-project collection name, e.g. 'MyProject_CodeModule'.

    Falls back to bare base name if project_name is empty.
    """
    if not project_name:
        return base
    prefix = _sanitize_collection_prefix(project_name)
    return f"{prefix}_{base}"


try:
    import weaviate
    from weaviate.classes.config import Configure, Property, DataType, ReferenceProperty
    from weaviate.classes.query import Filter
except ImportError:
    print("Error: weaviate-client not installed. Install with: pip install weaviate-client", file=sys.stderr)
    sys.exit(1)

# Code embedding configuration — supports two backends:
#   "service": FastAPI code embedding service (CodeSage-Large-v2 on GPU, default)
#   "ollama":  Ollama API (any model — legacy jina, qwen3, etc.)
CODE_EMBED_BACKEND = os.getenv("CODE_EMBED_BACKEND", "service")
CODE_EMBED_SERVICE_URL = os.getenv("CODE_EMBED_SERVICE_URL", "http://localhost:11440")
OLLAMA_CONFIG = {
    "url": os.getenv("OLLAMA_URL", "http://localhost:11435"),
    "model": os.getenv("CODE_EMBED_MODEL", "unclemusclez/jina-embeddings-v2-base-code:latest"),
}

# Named vector key matches the active backend — used for all insert_params["vector"] dicts.
# Must match the collection's named vector configuration in Weaviate.
_ACTIVE_CODE_VECTOR = "codesage_embed" if CODE_EMBED_BACKEND == "service" else "ollama_code_embed"

# Named vector support: when enabled, vectors are stored as {_ACTIVE_CODE_VECTOR: vec}
# instead of flat vectors. Must match the collection's named vector configuration.
DUAL_EMBEDDING_ENABLED = os.getenv("DUAL_EMBEDDING_ENABLED", "true").lower() == "true"

# Note: We use manual vectorization (vectorizer=None) and generate embeddings via the configured backend.
# This avoids requiring Weaviate text2vec-ollama module configuration

# Add scripts directory to path for shared config
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# Smart code truncation — import from weaviate_mcp package.
# PR-2 portability (2026-05-06): orchestrator clone resolved via
# $VCT_ORCHESTRATOR_ROOT (.claude/env) with in-tree fallback. Falls back
# to naive char-based truncation if the module is unavailable (CPU-only
# user projects without the venv). Pure utility import (no service
# runtime); see PR-2 design notes.
_env_root = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
if _env_root and (Path(_env_root) / "claude_mcp_servers").is_dir():
    _mcp_dir = Path(_env_root) / "claude_mcp_servers"
else:
    _mcp_dir = Path(__file__).resolve().parent.parent.parent / "claude_mcp_servers"
if str(_mcp_dir) not in sys.path:
    sys.path.insert(0, str(_mcp_dir))
# VCO-REWIRE-END: orchestrator-root-resolution
try:
    from weaviate_mcp.code_truncation import (
        truncate_function_for_embedding,
        truncate_class_for_embedding,
        truncate_module_for_embedding,
    )
except ImportError:
    # Inline fallbacks — naive char-based truncation (no model-awareness)
    def truncate_function_for_embedding(signature, body, language="python", model=None):
        return f"{signature}\n{body[:600]}"

    def truncate_class_for_embedding(signature, class_body, methods=None, language="python", model=None):
        methods_str = ", ".join(methods[:10]) if methods else ""
        return f"{signature}\nMethods: {methods_str}\n{class_body[:500]}"

    def truncate_module_for_embedding(module_summary, model=None):
        return module_summary[:2000]

# Resolve which code embedding model is active (for token budget in truncation)
_CODE_MODEL = (
    os.getenv("CODE_EMBED_MODEL", "codesage/codesage-large-v2")
    if CODE_EMBED_BACKEND == "service"
    else OLLAMA_CONFIG["model"]
)


def generate_embedding(text: str) -> Optional[List[float]]:
    """Generate code embedding using the configured backend (service or Ollama)."""
    try:
        if CODE_EMBED_BACKEND == "service":
            response = requests.post(
                f"{CODE_EMBED_SERVICE_URL}/api/embeddings",
                json={"model": "", "prompt": text},
                timeout=60,
            )
        else:
            response = requests.post(
                f"{OLLAMA_CONFIG['url']}/api/embeddings",
                json={"model": OLLAMA_CONFIG["model"], "prompt": text},
                timeout=30,
            )

        if response.status_code == 200:
            data = response.json()
            return data.get("embedding")
        else:
            print(f"⚠️  Embedding generation failed: HTTP {response.status_code}")
            return None
    except Exception as e:
        print(f"⚠️  Embedding generation error: {e}")
        return None


def embed_function(signature: str, body: str, language: str = "python") -> Optional[List[float]]:
    """Truncate function smartly, then generate embedding."""
    text = truncate_function_for_embedding(signature, body, language=language, model=_CODE_MODEL)
    return generate_embedding(text)


def embed_class(signature: str, class_body: str, methods: Optional[List[str]] = None,
                language: str = "python") -> Optional[List[float]]:
    """Truncate class smartly, then generate embedding."""
    text = truncate_class_for_embedding(signature, class_body, methods=methods,
                                        language=language, model=_CODE_MODEL)
    return generate_embedding(text)


def embed_module(module_summary: str) -> Optional[List[float]]:
    """Truncate module summary, then generate embedding."""
    text = truncate_module_for_embedding(module_summary, model=_CODE_MODEL)
    return generate_embedding(text)


# ---------------------------------------------------------------------------
# Cross-language call extraction
# ---------------------------------------------------------------------------

# HTTP client library → canonical name (used as import gate)
_HTTP_LIBS: Dict[str, str] = {
    # Python
    "requests": "requests", "httpx": "httpx", "aiohttp": "aiohttp",
    "urllib.request": "urllib", "urllib3": "urllib3",
    # JS/TS
    "axios": "axios", "node-fetch": "node-fetch", "got": "got",
    "cross-fetch": "cross-fetch",
    # Ruby
    "net/http": "net/http", "faraday": "faraday", "httparty": "httparty",
    "rest-client": "rest-client",
}
_GRPC_LIBS = {"grpc", "grpc-js", "@grpc/grpc-js", "grpc.io", "google.golang.org/grpc"}
_MQ_LIBS: Dict[str, str] = {
    "kafka-python": "kafka", "confluent-kafka": "kafka", "kafka": "kafka",
    "kafkajs": "kafka", "pika": "rabbitmq", "amqplib": "rabbitmq",
    "aio-pika": "rabbitmq", "redis": "redis",
}
_WS_LIBS = {"websocket", "websocket-client", "websockets", "socket.io-client", "ws"}


def _strip_triple_quoted(content: str) -> str:
    """Remove Python/JS triple-quoted strings to avoid extracting URLs from docstrings."""
    content = re.sub(r'""".*?"""', '""', content, flags=re.DOTALL)
    content = re.sub(r"'''.*?'''", "''", content, flags=re.DOTALL)
    return content


def _extract_external_calls(
    content_clean: str,
    imports: List[str],
    language: str,
    source_file: str = "",
) -> List[Dict[str, str]]:
    """
    Extract cross-language / cross-service communication calls from source code.

    False-positive prevention strategy:
    1. Import gate: only trigger when the relevant client library is imported.
    2. Literal gate: only extract calls where a literal string (not a plain variable)
       is used as the target. Partial templates (f"{VAR}/literal") yield medium confidence.
    3. Scope gate: strip triple-quoted strings so URLs in docstrings are ignored.

    Returns list of dicts with keys:
        interaction_type, direction, protocol, endpoint, raw_target, confidence
    """
    results: List[Dict[str, str]] = []

    # Normalise imports to a flat set of lowercase strings
    import_set = {i.lower().strip() for i in imports}

    def _has_any(lib_keys) -> bool:
        return any(k in import_set for k in lib_keys)

    # Work on comment-stripped, triple-quote-stripped content
    c = _strip_triple_quoted(content_clean)

    # -----------------------------------------------------------------------
    # HTTP calls
    # -----------------------------------------------------------------------
    http_lib = None
    for k, v in _HTTP_LIBS.items():
        if k in import_set:
            http_lib = v
            break

    # Shell: gate on literal `curl` or `wget` command
    if language == "shell":
        http_lib = "curl/wget"  # always check shell files for curl/wget

    if http_lib or language in ("csharp",):
        # Literal URL patterns — only http(s):// or ws(s):// URLs
        # Match: method("URL"  or  method('URL'  or  method(`URL`  (no ${} inside)
        literal_url = re.compile(
            r'(?:'
            # requests/httpx/aiohttp style: lib.method(["']url["']
            r'(?:requests|httpx|aiohttp|http|client|session|RestTemplate|HttpClient|'
            r'fetch|axios|got|Faraday|HTTParty|Net::HTTP|curl)\s*[.(]\s*'
            r'(?:["\']([A-Za-z][^"\'<>\s]{4,})["\']'        # literal string arg
            r'|`((?!.*\$\{)[A-Za-z][^`<>\s]{4,})`)'         # template literal, no ${
            r'|'
            # Shell: curl/wget "url" or curl url (without quotes, not $VAR)
            r'(?:curl|wget)(?:\s+-[^\s]+)*\s+'
            r'(?:["\']?(https?://[^\s"\'$<>]{5,})["\']?)'
            r')',
            re.MULTILINE,
        )
        for m in literal_url.finditer(c):
            raw = (m.group(1) or m.group(2) or m.group(3) or "").strip()
            if not raw or raw.startswith("$"):
                continue
            # Infer HTTP method from context
            ctx = c[max(0, m.start() - 60):m.start() + len(raw) + 10].lower()
            method = "GET"
            for verb in ("post", "put", "patch", "delete"):
                if verb in ctx:
                    method = verb.upper()
                    break
            # Extract just the path if it's a full URL
            try:
                from urllib.parse import urlparse as _up
                parsed = _up(raw)
                endpoint = parsed.path or raw
                if parsed.scheme in ("ws", "wss"):
                    results.append({
                        "interaction_type": "websocket", "direction": "outbound",
                        "protocol": parsed.scheme.upper(), "endpoint": endpoint,
                        "raw_target": raw, "confidence": "high",
                    })
                    continue
            except Exception:
                endpoint = raw
            results.append({
                "interaction_type": "http", "direction": "outbound",
                "protocol": method, "endpoint": endpoint,
                "raw_target": raw, "confidence": "high",
            })

        # Partial template: f"{VAR}/literal/path" or `${VAR}/literal/path`
        partial_template = re.compile(
            r'(?:f["\']|`)'                     # f-string or template literal
            r'(?:\{[^}]+\}|\$\{[^}]+\})'        # variable substitution at start
            r'(/[A-Za-z0-9/_-]{3,})'            # literal path segment follows
        )
        for m in partial_template.finditer(c):
            path = m.group(1)
            if http_lib and len(path) >= 4:
                # Only emit if there's a call context nearby
                ctx = c[max(0, m.start() - 100):m.start() + 10].lower()
                if any(k in ctx for k in ("get(", "post(", "put(", "delete(", "patch(", "fetch(", "request(")):
                    results.append({
                        "interaction_type": "http", "direction": "outbound",
                        "protocol": "HTTP", "endpoint": path,
                        "raw_target": m.group(0), "confidence": "medium",
                    })

    # -----------------------------------------------------------------------
    # gRPC calls
    # -----------------------------------------------------------------------
    if _has_any(_GRPC_LIBS):
        # Python/JS: SomeStub(channel).MethodName(request) or stub.MethodName(request)
        # Go: conn, _ := grpc.Dial("host:port", ...)
        grpc_dial = re.compile(r'grpc\.(?:Dial|dial|insecure_channel|secure_channel)\s*\(\s*["\']([^"\']+)["\']')
        for m in grpc_dial.finditer(c):
            raw = m.group(1)
            results.append({
                "interaction_type": "grpc", "direction": "outbound",
                "protocol": "gRPC", "endpoint": f"grpc:{raw}",
                "raw_target": raw, "confidence": "high",
            })

        # Stub method call: SomeServiceStub.MethodName( or stub.MethodName(
        stub_call = re.compile(r'\b(\w*(?:Stub|Client|ServiceClient))\s*\.\s*(\w+)\s*\(')
        for m in stub_call.finditer(c):
            stub, method = m.group(1), m.group(2)
            if method.lower() in ("__init__", "new", "create", "connect", "close", "init"):
                continue
            results.append({
                "interaction_type": "grpc", "direction": "outbound",
                "protocol": "gRPC", "endpoint": f"grpc:{stub}.{method}",
                "raw_target": f"{stub}.{method}()", "confidence": "medium",
            })

    # -----------------------------------------------------------------------
    # Message queue calls
    # -----------------------------------------------------------------------
    mq_lib = None
    for k, v in _MQ_LIBS.items():
        if k in import_set:
            mq_lib = v
            break

    if mq_lib == "kafka":
        # Python kafka: producer.send("topic-name", ...)
        # JS kafkajs: producer.send({ topic: "literal", ... })
        kafka_send = re.compile(
            r'(?:'
            r'(?:producer|kafka)\s*\.\s*send\s*\(\s*["\']([^"\']+)["\']'  # Python style
            r'|topic:\s*["\']([^"\']+)["\']'                               # JS object style
            r')'
        )
        for m in kafka_send.finditer(c):
            topic = (m.group(1) or m.group(2) or "").strip()
            if topic:
                results.append({
                    "interaction_type": "mq", "direction": "pubsub",
                    "protocol": "kafka", "endpoint": f"topic:{topic}",
                    "raw_target": topic, "confidence": "high",
                })

    if mq_lib == "rabbitmq":
        # Python pika: channel.basic_publish(exchange='x', routing_key='queue')
        rmq_pub = re.compile(
            r'basic_publish\s*\([^)]*routing_key\s*=\s*["\']([^"\']+)["\']'
        )
        for m in rmq_pub.finditer(c):
            key = m.group(1)
            results.append({
                "interaction_type": "mq", "direction": "pubsub",
                "protocol": "rabbitmq", "endpoint": f"queue:{key}",
                "raw_target": key, "confidence": "high",
            })
        # exchange
        rmq_exch = re.compile(
            r'basic_publish\s*\([^)]*exchange\s*=\s*["\']([^"\']+)["\']'
        )
        for m in rmq_exch.finditer(c):
            exch = m.group(1)
            if exch:  # skip empty exchange (default direct exchange)
                results.append({
                    "interaction_type": "mq", "direction": "pubsub",
                    "protocol": "rabbitmq", "endpoint": f"exchange:{exch}",
                    "raw_target": exch, "confidence": "high",
                })

    if mq_lib == "redis":
        # Redis pub/sub: r.publish("channel", message)
        redis_pub = re.compile(r'\.publish\s*\(\s*["\']([^"\']+)["\']')
        for m in redis_pub.finditer(c):
            ch = m.group(1)
            results.append({
                "interaction_type": "mq", "direction": "pubsub",
                "protocol": "redis", "endpoint": f"channel:{ch}",
                "raw_target": ch, "confidence": "high",
            })

    # -----------------------------------------------------------------------
    # WebSocket calls (when WS library imported but not caught by HTTP block)
    # -----------------------------------------------------------------------
    if _has_any(_WS_LIBS):
        ws_connect = re.compile(
            r'(?:WebSocketApp|create_connection|WebSocket|io)\s*\(\s*["\']'
            r'(wss?://[^"\'<>\s]{5,})["\']'
        )
        for m in ws_connect.finditer(c):
            raw = m.group(1)
            try:
                from urllib.parse import urlparse as _up
                parsed = _up(raw)
                endpoint = parsed.netloc + parsed.path
            except Exception:
                endpoint = raw
            results.append({
                "interaction_type": "websocket", "direction": "outbound",
                "protocol": "WS", "endpoint": endpoint,
                "raw_target": raw, "confidence": "high",
            })

    # Deduplicate by (interaction_type, protocol, endpoint)
    seen: set = set()
    deduped: List[Dict[str, str]] = []
    for r in results:
        key = (r["interaction_type"], r["protocol"], r["endpoint"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return deduped


class CodeGraphAnalyzer:
    """Analyzes codebase and extracts entities into Weaviate code graph."""

    def __init__(self, project_name: str, weaviate_url: str = "http://localhost:8081",
                 grpc_port: int = 50052, named_vectors: bool = DUAL_EMBEDDING_ENABLED):
        self.project_name = project_name
        self.weaviate_url = weaviate_url
        self.grpc_port = grpc_port
        self.named_vectors = named_vectors
        self.client = None

        # Per-project collection names
        self.coll_module = _collection_name("CodeModule", project_name)
        self.coll_class = _collection_name("CodeClass", project_name)
        self.coll_function = _collection_name("CodeFunction", project_name)
        self.coll_api = _collection_name("CodeAPI", project_name)
        self.coll_interaction = _collection_name("CodeInteraction", project_name)

        # Collections
        self.modules_collection = None
        self.classes_collection = None
        self.functions_collection = None
        self.apis_collection = None

        # Cache for entity lookups
        self.module_cache: Dict[str, str] = {}  # path -> UUID
        self.class_cache: Dict[str, str] = {}  # full_name -> UUID
        self.function_cache: Dict[str, str] = {}  # full_name -> UUID
        self.module_imports: Dict[str, List[str]] = {}  # path -> [import names]

    def connect(self):
        """Connect to Weaviate."""
        try:
            self.client = weaviate.connect_to_custom(
                http_host='localhost',
                http_port=8081,
                http_secure=False,
                grpc_host='localhost',
                grpc_port=50052,
                grpc_secure=False
            )
            print(f"✅ Connected to Weaviate at {self.weaviate_url}")
            return True
        except Exception as e:
            print(f"❌ Failed to connect to Weaviate: {e}", file=sys.stderr)
            return False

    def _vectorizer_config(self):
        """Return appropriate vectorizer config based on named_vectors flag.

        Three slots, matching `VECTOR_SCHEMES["code"]` in
        weaviate_mcp/server.py. Pre-2026-04-30 only declared two
        (active + legacy), which broke `_get_all_code_embeddings` writes
        and `backfill_embeddings(provider="openai")` on freshly-analyzed
        collections (audit finding Code-M1, 2026-04-30). De-dup against
        `_ACTIVE_CODE_VECTOR` for the case where the active slot is
        `ollama_code_embed` (legacy backend).
        """
        if not self.named_vectors:
            return Configure.Vectorizer.none()
        slot_names = {_ACTIVE_CODE_VECTOR, "ollama_code_embed", "openai_embed"}
        return [Configure.NamedVectors.none(name=n) for n in sorted(slot_names)]

    def _inverted_index_config(self):
        """Always return an inverted index config with `index_null_state=True`.

        Required for `is_none(True)` filters (e.g. future stale-filter on
        valid_until). Cannot be retro-added on existing collections —
        Weaviate ≤1.30 ignores `Reconfigure.inverted_index(...)` for
        index_null_state, so this MUST be set at create time. Audit
        finding Code-M2 (2026-04-30).
        """
        return Configure.inverted_index(index_null_state=True)

    def create_collections(self, force: bool = False):
        """Create Weaviate collections for code graph (per-project names)."""

        if not self.client:
            raise RuntimeError("Not connected to Weaviate")

        collections_created = []

        # CodeModule collection
        try:
            if force and self.client.collections.exists(self.coll_module):
                self.client.collections.delete(self.coll_module)
                print(f"🗑️  Deleted existing {self.coll_module} collection")

            if not self.client.collections.exists(self.coll_module):
                self.client.collections.create(
                    name=self.coll_module,
                    description="Code modules/files with imports and metrics",
                    vectorizer_config=self._vectorizer_config(),
                    inverted_index_config=self._inverted_index_config(),
                    properties=[
                        Property(name="path", data_type=DataType.TEXT, description="File path relative to repo root", skip_vectorization=True),
                        Property(name="language", data_type=DataType.TEXT, description="Programming language", skip_vectorization=True),
                        Property(name="module_summary", data_type=DataType.TEXT, description="Summary of module purpose and contents (for embedding)"),
                        Property(name="loc", data_type=DataType.INT, description="Lines of code", skip_vectorization=True),
                        Property(name="complexity", data_type=DataType.NUMBER, description="Cyclomatic complexity", skip_vectorization=True),
                        Property(name="project", data_type=DataType.TEXT, description="Project name", skip_vectorization=True),
                        Property(name="last_modified", data_type=DataType.DATE, description="Last modification time", skip_vectorization=True),
                        Property(name="file_hash", data_type=DataType.TEXT, description="SHA256 hash of file content", skip_vectorization=True),
                        Property(name="import_names", data_type=DataType.TEXT_ARRAY, description="List of imported module/package names", skip_vectorization=True),
                    ],
                    references=[
                        ReferenceProperty(name="imports", target_collection=self.coll_module, description="Imported modules"),
                    ]
                )
                collections_created.append(self.coll_module)
                print(f"✅ Created {self.coll_module} collection")
        except Exception as e:
            print(f"⚠️  CodeModule: {e}")

        # CodeClass collection
        try:
            if force and self.client.collections.exists(self.coll_class):
                self.client.collections.delete(self.coll_class)
                print(f"🗑️  Deleted existing {self.coll_class} collection")

            if not self.client.collections.exists(self.coll_class):
                self.client.collections.create(
                    name=self.coll_class,
                    description="Classes with inheritance and methods",
                    vectorizer_config=self._vectorizer_config(),
                    inverted_index_config=self._inverted_index_config(),
                    properties=[
                        Property(name="name", data_type=DataType.TEXT, description="Class name", skip_vectorization=True),
                        Property(name="full_name", data_type=DataType.TEXT, description="Fully qualified name", skip_vectorization=True),
                        Property(name="class_body", data_type=DataType.TEXT, description="Full class source code (for embedding)"),
                        Property(name="methods", data_type=DataType.TEXT_ARRAY, description="Method names", skip_vectorization=True),
                        Property(name="signature", data_type=DataType.TEXT, description="Class signature only", skip_vectorization=True),
                        Property(name="doc", data_type=DataType.TEXT, description="Docstring"),
                        Property(name="start_line", data_type=DataType.INT, description="Start line number", skip_vectorization=True),
                        Property(name="end_line", data_type=DataType.INT, description="End line number", skip_vectorization=True),
                        Property(name="project", data_type=DataType.TEXT, description="Project name", skip_vectorization=True),
                        Property(name="field_types", data_type=DataType.TEXT_ARRAY, description="field_name:TypeName pairs from annotated fields", skip_vectorization=True),
                        Property(name="composes", data_type=DataType.TEXT_ARRAY, description="Class names used as field types (composition)", skip_vectorization=True),
                        Property(name="primary_layer", data_type=DataType.TEXT, description="Primary architectural layer (API, Service, Data, UI, Utility, etc.)", skip_vectorization=True),
                        Property(name="secondary_layers", data_type=DataType.TEXT_ARRAY, description="Secondary architectural layers if class spans multiple", skip_vectorization=True),
                    ],
                    references=[
                        ReferenceProperty(name="module", target_collection=self.coll_module, description="Parent module"),
                        ReferenceProperty(name="extends", target_collection=self.coll_class, description="Base classes"),
                    ]
                )
                collections_created.append(self.coll_class)
                print(f"✅ Created {self.coll_class} collection")
        except Exception as e:
            print(f"⚠️  CodeClass: {e}")

        # CodeFunction collection
        try:
            if force and self.client.collections.exists(self.coll_function):
                self.client.collections.delete(self.coll_function)
                print(f"🗑️  Deleted existing {self.coll_function} collection")

            if not self.client.collections.exists(self.coll_function):
                self.client.collections.create(
                    name=self.coll_function,
                    description="Functions with call graphs",
                    vectorizer_config=self._vectorizer_config(),
                    inverted_index_config=self._inverted_index_config(),
                    properties=[
                        Property(name="name", data_type=DataType.TEXT, description="Function name", skip_vectorization=True),
                        Property(name="full_name", data_type=DataType.TEXT, description="Fully qualified name", skip_vectorization=True),
                        Property(name="function_body", data_type=DataType.TEXT, description="Full function source code (for embedding)"),
                        Property(name="signature", data_type=DataType.TEXT, description="Function signature only", skip_vectorization=True),
                        Property(name="doc", data_type=DataType.TEXT, description="Docstring"),
                        Property(name="start_line", data_type=DataType.INT, description="Start line number", skip_vectorization=True),
                        Property(name="end_line", data_type=DataType.INT, description="End line number", skip_vectorization=True),
                        Property(name="is_async", data_type=DataType.BOOL, description="Is async function", skip_vectorization=True),
                        Property(name="project", data_type=DataType.TEXT, description="Project name", skip_vectorization=True),
                        Property(name="type_uses", data_type=DataType.TEXT_ARRAY, description="Type names referenced in function annotations", skip_vectorization=True),
                        Property(name="cfg_summary", data_type=DataType.TEXT, description="CFG summary: branches/loops/max_depth counts (from Joern)", skip_vectorization=True),
                        Property(name="data_flow_vars", data_type=DataType.TEXT_ARRAY, description="Variable names that flow through the function (from Joern PDG)", skip_vectorization=True),
                        Property(name="layer", data_type=DataType.TEXT, description="Architectural layer (API, Service, Data, UI, Utility, etc.)", skip_vectorization=True),
                        Property(name="call_names", data_type=DataType.TEXT_ARRAY, description="Names of called functions (for callers queries)", skip_vectorization=True),
                    ],
                    references=[
                        ReferenceProperty(name="module", target_collection=self.coll_module, description="Parent module"),
                        ReferenceProperty(name="calls", target_collection=self.coll_function, description="Called functions"),
                    ]
                )
                collections_created.append(self.coll_function)
                print(f"✅ Created {self.coll_function} collection")
        except Exception as e:
            print(f"⚠️  CodeFunction: {e}")

        # CodeAPI collection
        try:
            if force and self.client.collections.exists(self.coll_api):
                self.client.collections.delete(self.coll_api)
                print(f"🗑️  Deleted existing {self.coll_api} collection")

            if not self.client.collections.exists(self.coll_api):
                self.client.collections.create(
                    name=self.coll_api,
                    description="API endpoints with handlers",
                    vectorizer_config=self._vectorizer_config(),
                    inverted_index_config=self._inverted_index_config(),
                    properties=[
                        Property(name="endpoint", data_type=DataType.TEXT, description="API endpoint path", skip_vectorization=True),
                        Property(name="method", data_type=DataType.TEXT, description="HTTP method", skip_vectorization=True),
                        Property(name="api_description", data_type=DataType.TEXT, description="Description of API endpoint and its purpose (for embedding)"),
                        Property(name="parameters", data_type=DataType.TEXT_ARRAY, description="Parameter names", skip_vectorization=True),
                        Property(name="returns", data_type=DataType.TEXT, description="Return type/description"),
                        Property(name="project", data_type=DataType.TEXT, description="Project name", skip_vectorization=True),
                        Property(name="proxy_target", data_type=DataType.TEXT, description="Target endpoint for proxy/forwarding routes (cross-language linking)", skip_vectorization=True),
                    ],
                    references=[
                        ReferenceProperty(name="handler", target_collection=self.coll_function, description="Handler function"),
                    ]
                )
                collections_created.append(self.coll_api)
                print(f"✅ Created {self.coll_api} collection")
        except Exception as e:
            print(f"⚠️  CodeAPI: {e}")

        # CodeInteraction collection — cross-language / cross-service calls
        try:
            if force and self.client.collections.exists(self.coll_interaction):
                self.client.collections.delete(self.coll_interaction)
                print(f"🗑️  Deleted existing {self.coll_interaction} collection")

            if not self.client.collections.exists(self.coll_interaction):
                self.client.collections.create(
                    name=self.coll_interaction,
                    description="Cross-language and cross-service communication calls",
                    vectorizer_config=self._vectorizer_config(),
                    inverted_index_config=self._inverted_index_config(),
                    properties=[
                        Property(name="source_project", data_type=DataType.TEXT, description="Project that initiates the call", skip_vectorization=True),
                        Property(name="interaction_type", data_type=DataType.TEXT, description="http | grpc | mq | websocket", skip_vectorization=True),
                        Property(name="direction", data_type=DataType.TEXT, description="outbound | inbound | pubsub", skip_vectorization=True),
                        Property(name="protocol", data_type=DataType.TEXT, description="GET/POST/... | kafka | rabbitmq | grpc | ws", skip_vectorization=True),
                        Property(name="endpoint", data_type=DataType.TEXT, description="Extracted target: /path, grpc:Service.Method, topic:name", skip_vectorization=True),
                        Property(name="raw_target", data_type=DataType.TEXT, description="Full literal string as seen in source code", skip_vectorization=True),
                        Property(name="confidence", data_type=DataType.TEXT, description="high | medium", skip_vectorization=True),
                        Property(name="description", data_type=DataType.TEXT, description="Human-readable summary for embedding (Python→HTTP POST /api/users via requests)"),
                    ],
                    references=[
                        ReferenceProperty(name="source_function", target_collection=self.coll_function, description="Function that makes the call"),
                        ReferenceProperty(name="source_module", target_collection=self.coll_module, description="Module containing the call"),
                    ]
                )
                collections_created.append(self.coll_interaction)
                print(f"✅ Created {self.coll_interaction} collection")
        except Exception as e:
            print(f"⚠️  CodeInteraction: {e}")

        if collections_created:
            print(f"\n✅ Created {len(collections_created)} collections: {', '.join(collections_created)}")
        else:
            print("\n✅ All collections already exist")

        # Get collection references (per-project names)
        self.modules_collection = self.client.collections.get(self.coll_module)
        self.classes_collection = self.client.collections.get(self.coll_class)
        self.functions_collection = self.client.collections.get(self.coll_function)
        self.apis_collection = self.client.collections.get(self.coll_api)
        self.interactions_collection = self.client.collections.get(self.coll_interaction)

        # Schema migration: ensure import_names property exists on CodeModule
        self._ensure_import_names_property()

    def _dedup_insert(self, collection, insert_params: dict, identity_key: str) -> str:
        """Insert with deterministic UUID to prevent duplicates.

        Args:
            collection: Weaviate collection reference
            insert_params: dict with 'properties', 'vector', 'references' keys
            identity_key: unique key for this entity (e.g. full_name, path)

        Returns:
            UUID string of the inserted/updated object
        """
        det_uuid = _deterministic_uuid(self.project_name, identity_key)
        return collection.data.insert(uuid=det_uuid, **insert_params)

    def _ensure_import_names_property(self):
        """Add import_names property to CodeModule if missing (schema migration)."""
        try:
            config = self.modules_collection.config.get()
            existing_props = {p.name for p in config.properties}
            if "import_names" not in existing_props:
                self.modules_collection.config.add_property(
                    Property(name="import_names", data_type=DataType.TEXT_ARRAY,
                             description="List of imported module/package names",
                             skip_vectorization=True)
                )
                print("   Added import_names property to CodeModule schema")
        except Exception as e:
            logger.debug(f"Schema migration check failed: {e}")

    def analyze_repository(self, repo_path: Path, language: Optional[str] = None,
                          incremental: bool = False,
                          extract_cfg: bool = False,
                          extract_pdg: bool = False) -> Dict[str, Any]:
        """Analyze repository and extract code entities.

        Args:
            repo_path: Path to repository
            language: Specific language to analyze (None = all)
            incremental: Only analyze changed files (requires git)
            extract_cfg: Run Joern CFG extraction (requires joern in PATH)
            extract_pdg: Run Joern PDG extraction (requires joern in PATH)

        Returns:
            Dictionary with analysis statistics
        """

        if not repo_path.exists():
            raise ValueError(f"Repository path does not exist: {repo_path}")

        # Run Joern CFG/PDG pre-pass if requested; store on instance for use by _extract_function
        if extract_cfg or extract_pdg:
            lang_hint = language or "python"
            print("🔬 Running Joern CFG/PDG extraction (this may take a while)...")
            self._cfg_pdg_data: Dict[str, Any] = self._extract_cfg_pdg(
                repo_path, lang_hint, extract_cfg=extract_cfg, extract_pdg=extract_pdg
            )
            if self._cfg_pdg_data:
                print(f"   Extracted data for {len(self._cfg_pdg_data)} functions")
            else:
                print("   No CFG/PDG data extracted (joern unavailable or no output)")
        else:
            self._cfg_pdg_data = {}

        stats = {
            'modules': 0,
            'classes': 0,
            'functions': 0,
            'apis': 0,
            'files_analyzed': 0,
            'files_skipped': 0,
        }

        # Language dispatch: auto-detect from extensions, or filter by --language
        lang = language.lower() if language else None

        lang_dispatch = [
            ('python',     self._find_python_files, self._analyze_python_file),
            ('lua',        self._find_lua_files,    self._analyze_lua_file),
            ('cpp',        self._find_cpp_files,    self._analyze_cpp_file),
            ('javascript', self._find_js_files,     self._analyze_js_file),
            ('typescript', self._find_ts_files,     self._analyze_js_file),
            ('go',         self._find_go_files,     self._analyze_go_file),
            ('rust',       self._find_rust_files,   self._analyze_rust_file),
            ('java',       self._find_java_files,   self._analyze_java_file),
            ('ruby',       self._find_ruby_files,   self._analyze_ruby_file),
            ('shell',      self._find_shell_files,  self._analyze_shell_file),
            ('csharp',     self._find_csharp_files, self._analyze_csharp_file),
            ('proto',      self._find_proto_files,  self._analyze_proto_file),
        ]

        for lang_name, find_fn, analyze_fn in lang_dispatch:
            if lang and lang != lang_name:
                continue

            files = find_fn(repo_path)
            if not files:
                continue

            if incremental:
                files = self._filter_changed_files(repo_path, files)
                if not files:
                    print(f"ℹ️  No changed {lang_name} files to analyze")
                    continue

            print(f"📂 Found {len(files)} {lang_name} files to analyze")

            for f in files:
                try:
                    result = analyze_fn(f, repo_path)
                    stats['modules']  += result.get('modules', 0)
                    stats['classes']  += result.get('classes', 0)
                    stats['functions'] += result.get('functions', 0)
                    stats['apis']     += result.get('apis', 0)
                    stats['files_analyzed'] += 1
                except Exception as e:
                    print(f"⚠️  Error analyzing {f.relative_to(repo_path)}: {e}")
                    stats['files_skipped'] += 1

        return stats

    def _find_python_files(self, repo_path: Path) -> List[Path]:
        """Find all Python files in repository."""
        python_files = []

        # Directories to ignore
        ignore_dirs = {
            'venv', '.venv', 'env', '.env',
            'node_modules', '__pycache__',
            '.git', '.svn', '.hg',
            'build', 'dist', '.tox', '.pytest_cache',
            'site-packages', 'virtualenv',
        }

        for py_file in repo_path.rglob('*.py'):
            # Skip if in ignored directory
            if any(ignored in py_file.parts for ignored in ignore_dirs):
                continue
            python_files.append(py_file)

        return sorted(python_files)

    def _find_lua_files(self, repo_path: Path) -> List[Path]:
        """Find all Lua files in repository."""
        ignore_dirs = {'.git', '.svn', 'build', 'dist', '__pycache__', 'node_modules'}
        return sorted([
            f for f in repo_path.rglob('*.lua')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_cpp_files(self, repo_path: Path) -> List[Path]:
        """Find all C++/header files in repository."""
        ignore_dirs = {'.git', '.svn', 'build', 'dist', '__pycache__', 'node_modules', '.venv', 'venv'}
        files = []
        for ext in ('*.cpp', '*.cc', '*.cxx', '*.c', '*.h', '*.hpp'):
            files.extend([
                f for f in repo_path.rglob(ext)
                if not any(d in f.parts for d in ignore_dirs)
            ])
        return sorted(files)

    def _find_js_files(self, repo_path: Path) -> List[Path]:
        """Find all JavaScript files in repository."""
        ignore_dirs = {
            '.git', '.svn', 'node_modules', 'dist', 'build',
            '__pycache__', '.venv', 'venv', 'coverage',
        }
        skip_suffixes = {'.min.js', '.config.js', '.config.mjs'}
        files = []
        for ext in ('*.js', '*.mjs', '*.jsx'):
            for f in repo_path.rglob(ext):
                if any(d in f.parts for d in ignore_dirs):
                    continue
                if any(f.name.endswith(s) for s in skip_suffixes):
                    continue
                if f.name.startswith('vite.config'):
                    continue
                files.append(f)
        return sorted(files)

    def _find_ts_files(self, repo_path: Path) -> List[Path]:
        """Find all TypeScript files in repository."""
        ignore_dirs = {
            '.git', '.svn', 'node_modules', 'dist', 'build',
            '__pycache__', '.venv', 'venv', 'coverage',
        }
        skip_suffixes = {'.config.ts', '.config.mts'}
        files = []
        for ext in ('*.ts', '*.tsx'):
            for f in repo_path.rglob(ext):
                if any(d in f.parts for d in ignore_dirs):
                    continue
                if any(f.name.endswith(s) for s in skip_suffixes):
                    continue
                if f.name.startswith('vite.config'):
                    continue
                # Skip .d.ts declaration files (type stubs, not source)
                if f.name.endswith('.d.ts'):
                    continue
                files.append(f)
        return sorted(files)

    def _find_go_files(self, repo_path: Path) -> List[Path]:
        """Find all Go files in repository."""
        ignore_dirs = {'.git', '.svn', 'vendor', 'build', 'dist', '__pycache__', 'node_modules'}
        return sorted([
            f for f in repo_path.rglob('*.go')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_rust_files(self, repo_path: Path) -> List[Path]:
        """Find all Rust files in repository."""
        ignore_dirs = {'.git', '.svn', 'target', 'build', 'dist', '__pycache__', 'node_modules'}
        return sorted([
            f for f in repo_path.rglob('*.rs')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_java_files(self, repo_path: Path) -> List[Path]:
        """Find all Java files in repository."""
        ignore_dirs = {'.git', '.svn', 'build', 'dist', 'target', '__pycache__', 'node_modules', '.gradle'}
        return sorted([
            f for f in repo_path.rglob('*.java')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_ruby_files(self, repo_path: Path) -> List[Path]:
        """Find all Ruby files in repository."""
        ignore_dirs = {'.git', '.svn', 'vendor', 'build', 'dist', '__pycache__', 'node_modules', '.bundle'}
        return sorted([
            f for f in repo_path.rglob('*.rb')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_shell_files(self, repo_path: Path) -> List[Path]:
        """Find all Shell script files in repository."""
        ignore_dirs = {'.git', '.svn', 'build', 'dist', '__pycache__', 'node_modules', '.venv', 'venv'}
        files = []
        for ext in ('*.sh', '*.bash'):
            files.extend([
                f for f in repo_path.rglob(ext)
                if not any(d in f.parts for d in ignore_dirs)
            ])
        return sorted(files)

    def _find_csharp_files(self, repo_path: Path) -> List[Path]:
        """Find all C# files in repository."""
        ignore_dirs = {'.git', '.svn', 'obj', 'bin', '.vs', 'build', 'dist', '__pycache__', 'node_modules'}
        return sorted([
            f for f in repo_path.rglob('*.cs')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _find_proto_files(self, repo_path: Path) -> List[Path]:
        """Find all Protocol Buffer definition files."""
        ignore_dirs = {'.git', '.svn', 'build', 'dist', '__pycache__', 'node_modules'}
        return sorted([
            f for f in repo_path.rglob('*.proto')
            if not any(d in f.parts for d in ignore_dirs)
        ])

    def _analyze_csharp_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a C# file using regex-based parsing.

        Extracts using directives, classes, interfaces, methods, and ASP.NET route attributes.
        Also populates CodeAPI for [HttpGet/Post/Put/Delete/Patch] annotated methods.
        """
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines
                   if l.strip() and not l.strip().startswith('//')
                   and not l.strip().startswith('*')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = str(file_path.relative_to(repo_root))

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # using directives
        imports = re.findall(r'^\s*using\s+([\w.]+)\s*;', content, re.MULTILINE)

        # namespace
        ns_match = re.search(r'namespace\s+([\w.]+)', content)
        ns = ns_match.group(1) if ns_match else file_path.stem

        # Classes / interfaces / records
        class_pattern = re.compile(
            r'(?:public|private|protected|internal|abstract|sealed|partial|\s)+'
            r'(?:class|interface|record|struct)\s+([\w<>, ]+?)(?:\s*:\s*[\w<>, ]+?)?\s*\{',
            re.MULTILINE
        )
        class_info: Dict[str, int] = {}
        for m in class_pattern.finditer(content_clean):
            raw = m.group(1).strip().split('<')[0].strip()  # strip generics
            if not raw or raw[0].islower():
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            class_info[raw] = start_line

        # Methods: access modifier + return type + name(...)
        method_pattern = re.compile(
            r'(?:public|private|protected|internal|static|virtual|override|async|abstract|\s)+'
            r'(?:[\w<>\[\]?]+\s+)+([\w]+)\s*\([^)]*\)\s*(?:\{|=>|;)',
            re.MULTILINE
        )

        # ASP.NET HTTP attributes for CodeAPI
        http_attr_pattern = re.compile(
            r'\[Http(Get|Post|Put|Delete|Patch|Options|Head)\s*(?:\([^)]*\))?\]'
            r'(?:\s*\[[^\]]*\])*'           # other attributes in between
            r'[^{]*?'                        # skip to method
            r'(?:public|private|protected)\s+'
            r'(?:async\s+)?(?:Task[<>\w]*\s+|IActionResult\s+|ActionResult[<>\w]*\s+)?'
            r'([\w]+)\s*\(',
            re.MULTILINE | re.DOTALL
        )
        # Route attribute (base route on controller or per-method)
        route_attr_pattern = re.compile(r'\[Route\s*\(\s*["\']([^"\']+)["\']')

        # Module summary
        file_comment = ''
        for line in source_lines[:20]:
            s = line.strip()
            if s.startswith('///') or s.startswith('//'):
                file_comment = s.lstrip('/').strip()
                break
        summary_parts = [f"C# module: {relative_path} (namespace {ns})"]
        if file_comment:
            summary_parts.append(file_comment)
        if class_info:
            summary_parts.append(f"Classes: {', '.join(list(class_info.keys())[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if (', 'while (', 'for (', 'foreach (', 'switch (', 'catch (']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="C#", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        # Classes
        for cname, start_line in class_info.items():
            class_lines = source_lines[max(0, start_line - 1):min(start_line + 60, len(source_lines))]
            class_body = '\n'.join(class_lines)
            methods = [m.group(1) for m in method_pattern.finditer(content_clean)]
            signature = f"class {cname}"
            embedding = embed_class(signature, class_body, methods=methods[:10], language="csharp")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": cname, "full_name": f"{ns}.{cname}",
                    "class_body": class_body, "methods": methods[:20],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['classes'] += 1

        # Methods
        for m in method_pattern.finditer(content_clean):
            mname = m.group(1)
            if mname in ('if', 'while', 'for', 'foreach', 'switch', 'catch', 'try', 'return', 'new', 'throw'):
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = min(start_line + 50, len(source_lines))
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            enclosing = next(
                (c for c, cl in sorted(class_info.items(), key=lambda x: x[1], reverse=True)
                 if cl <= start_line), file_path.stem
            )
            is_async = bool(re.search(r'\basync\b', body[:200]))
            full_name = f"{ns}.{enclosing}.{mname}"
            signature = f"{mname}(...)"
            embedding = embed_function(signature, body, language="csharp")
            insert_params = {
                "properties": {
                    "name": mname, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": is_async, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            func_uuid = self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['functions'] += 1

            # ASP.NET route entries for HTTP-attributed methods
            # Check if this method has an [Http*] attribute in the lines just above it
            pre_lines = source_lines[max(0, start_line - 5):start_line]
            pre_ctx = '\n'.join(pre_lines)
            http_m = re.search(r'\[Http(Get|Post|Put|Delete|Patch|Options|Head)', pre_ctx, re.IGNORECASE)
            if http_m:
                http_method = http_m.group(1).upper()
                # Extract route from attribute or from class [Route] base
                route_m = re.search(r'\[Http\w+\s*\(\s*["\']([^"\']+)["\']', pre_ctx)
                route = route_m.group(1) if route_m else f"/{mname.lower()}"
                # Base controller route
                ctrl_route = ''
                base_route_m = route_attr_pattern.search(content_clean[:m.start()])
                if base_route_m:
                    ctrl_route = '/' + base_route_m.group(1).strip('/')
                full_route = ctrl_route + ('/' if ctrl_route else '') + route.lstrip('/')
                api_desc = f"C# ASP.NET {http_method} {full_route} → {ns}.{enclosing}.{mname}"
                api_embedding = generate_embedding(api_desc)
                api_params: Dict[str, Any] = {
                    "properties": {
                        "endpoint": full_route, "method": http_method,
                        "api_description": api_desc,
                        "parameters": [], "returns": "",
                        "project": self.project_name, "proxy_target": "",
                    },
                    "references": {"handler": func_uuid},
                }
                if api_embedding:
                    api_params["vector"] = {_ACTIVE_CODE_VECTOR: api_embedding} if DUAL_EMBEDDING_ENABLED else api_embedding
                self._dedup_insert(self.apis_collection, api_params, api_params["properties"].get("endpoint", "") + ":" + api_params["properties"].get("method", ""))
                stats.setdefault('apis', 0)
                stats['apis'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content_clean, imports, "csharp", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "C#", module_uuid)

        return stats

    def _analyze_proto_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Protocol Buffer (.proto) file.

        Proto files define cross-language service contracts. Each RPC method is stored
        as a CodeAPI entry (inbound contract), and each message type as a CodeClass.
        """
        stats = {'modules': 0, 'classes': 0, 'apis': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('//')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = str(file_path.relative_to(repo_root))

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Strip comments
        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # package name
        pkg_match = re.search(r'^package\s+([\w.]+)\s*;', content, re.MULTILINE)
        pkg = pkg_match.group(1) if pkg_match else file_path.stem

        # imports (other .proto files)
        imports = re.findall(r'import\s+["\']([^"\']+)["\']', content)

        # Message types → CodeClass
        msg_pattern = re.compile(r'^message\s+([\w]+)\s*\{', re.MULTILINE)
        message_names: List[str] = []
        for m in msg_pattern.finditer(content_clean):
            mname = m.group(1)
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = min(start_line + 30, len(source_lines))
            class_body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            signature = f"message {mname}"
            embedding = generate_embedding(f"Proto message: {mname}\n{class_body[:400]}")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": mname, "full_name": f"{pkg}.{mname}",
                    "class_body": class_body, "methods": [],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": end_line,
                    "project": self.project_name,
                },
                "references": {"module": ""},   # no module UUID yet; filled after
            }
            message_names.append(mname)
            # Store after module creation below

        # Service RPC methods → CodeAPI
        svc_pattern = re.compile(r'^service\s+([\w]+)\s*\{', re.MULTILINE)
        rpc_pattern = re.compile(
            r'rpc\s+([\w]+)\s*\(\s*([\w.]+)\s*\)\s*returns\s*\(\s*([\w.]+)\s*\)',
            re.MULTILINE
        )
        rpc_entries: list = []
        for svc_m in svc_pattern.finditer(content_clean):
            svc_name = svc_m.group(1)
            svc_start = svc_m.start()
            # Find matching closing brace
            depth, svc_end = 0, len(content_clean)
            for i, ch in enumerate(content_clean[svc_start:]):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        svc_end = svc_start + i
                        break
            svc_body = content_clean[svc_start:svc_end]
            for rpc_m in rpc_pattern.finditer(svc_body):
                rpc_entries.append({
                    'service': svc_name,
                    'method': rpc_m.group(1),
                    'input': rpc_m.group(2),
                    'output': rpc_m.group(3),
                })

        # Module summary
        summary_parts = [f"Proto: {relative_path} (package {pkg})"]
        if message_names:
            summary_parts.append(f"Messages: {', '.join(message_names[:8])}")
        if rpc_entries:
            svc_names = list({e['service'] for e in rpc_entries})
            summary_parts.append(f"Services: {', '.join(svc_names)}")
        module_summary = '\n'.join(summary_parts)

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Proto", loc=loc, complexity=1.0,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        # Now insert message classes with proper module UUID
        for m in msg_pattern.finditer(content_clean):
            mname = m.group(1)
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = min(start_line + 30, len(source_lines))
            class_body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            signature = f"message {mname}"
            embedding = generate_embedding(f"Proto message: {mname}\n{class_body[:400]}")
            insert_params = {
                "properties": {
                    "name": mname, "full_name": f"{pkg}.{mname}",
                    "class_body": class_body, "methods": [],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": end_line,
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['classes'] += 1

        # Insert RPC methods as CodeAPI entries (inbound service contract)
        for entry in rpc_entries:
            endpoint = f"grpc:{pkg}.{entry['service']}/{entry['method']}"
            api_desc = (
                f"gRPC {entry['service']}.{entry['method']} "
                f"({entry['input']}) → ({entry['output']}) [{pkg}]"
            )
            embedding = generate_embedding(api_desc)
            api_params: Dict[str, Any] = {
                "properties": {
                    "endpoint": endpoint, "method": "gRPC",
                    "api_description": api_desc,
                    "parameters": [entry['input']], "returns": entry['output'],
                    "project": self.project_name, "proxy_target": "",
                },
            }
            if embedding:
                api_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.apis_collection, api_params, api_params["properties"].get("endpoint", "") + ":" + api_params["properties"].get("method", ""))
            stats['apis'] += 1

        return stats

    def _analyze_js_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a JavaScript/TypeScript file using regex-based parsing.

        Extracts imports, functions, classes, Fastify route definitions, and
        external HTTP calls (fetch). Handles both .js/.mjs and .ts/.tsx files.
        """
        stats = {'modules': 0, 'classes': 0, 'functions': 0, 'apis': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines
                   if l.strip() and not l.strip().startswith('//') and not l.strip().startswith('*')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = str(file_path.relative_to(repo_root))

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Determine language label from extension
        suffix = file_path.suffix.lower()
        if suffix in ('.ts', '.tsx'):
            language = "TypeScript"
        else:
            language = "JavaScript"

        # Strip single-line and multi-line comments for cleaner pattern matching
        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # --- Imports ---
        imports: List[str] = []
        # ES module imports: import { x } from './y' / import x from './y' / import './y'
        for m in re.finditer(r"""import\s+(?:(?:\{[^}]*\}|[\w*]+(?:\s+as\s+\w+)?)\s+from\s+)?['"]([^'"]+)['"]""", content):
            imports.append(m.group(1))
        # CommonJS require: require('./y') / require('y')
        for m in re.finditer(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""", content):
            imports.append(m.group(1))

        # --- Classes ---
        class_names: List[str] = []
        class_pattern = re.compile(
            r'(?:export\s+(?:default\s+)?)?class\s+([\w]+)\s*(?:extends\s+([\w.]+)\s*)?{',
            re.MULTILINE
        )
        class_info: Dict[str, Tuple[int, Optional[str]]] = {}  # name -> (start_line, base_class)
        for m in class_pattern.finditer(content_clean):
            cname = m.group(1)
            base = m.group(2)
            start_line = content_clean[:m.start()].count('\n') + 1
            class_info[cname] = (start_line, base)
            class_names.append(cname)

        # --- Functions ---
        # Covers: export async function name(, export function name(,
        #         async function name(, function name(
        func_pattern = re.compile(
            r'(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([\w]+)\s*\(',
            re.MULTILINE
        )
        all_func_names: List[str] = []
        func_matches: List[Tuple[str, int, bool]] = []  # (name, start_line, is_async)
        for m in func_pattern.finditer(content_clean):
            fname = m.group(1)
            start_line = content_clean[:m.start()].count('\n') + 1
            # Check if 'async' appears before 'function' in the matched text
            match_text = content_clean[m.start():m.end()]
            is_async = 'async' in match_text
            func_matches.append((fname, start_line, is_async))
            all_func_names.append(fname)

        # Also catch arrow function exports: export const name = async (...) =>
        arrow_pattern = re.compile(
            r'(?:export\s+)?(?:const|let|var)\s+([\w]+)\s*=\s*(async\s+)?(?:\([^)]*\)|[\w]+)\s*=>',
            re.MULTILINE
        )
        for m in arrow_pattern.finditer(content_clean):
            fname = m.group(1)
            start_line = content_clean[:m.start()].count('\n') + 1
            is_async = m.group(2) is not None
            func_matches.append((fname, start_line, is_async))
            all_func_names.append(fname)

        # --- Fastify route definitions ---
        # Pattern: { secure: true/false, method: 'POST', url: '/tx/build', handler, schema }
        # May span multiple lines
        route_pattern = re.compile(
            r'\{[^}]*method:\s*[\'"](\w+)[\'"][^}]*url:\s*[\'"]([^\'"]+)[\'"][^}]*\}',
            re.DOTALL
        )
        routes: List[Dict[str, Any]] = []
        for m in route_pattern.finditer(content):
            route_block = m.group(0)
            method = m.group(1).upper()
            url = m.group(2)
            # Extract secure flag
            secure_match = re.search(r'secure:\s*(true|false)', route_block)
            secure = secure_match.group(1) == 'true' if secure_match else False
            # Extract handler reference
            handler_match = re.search(r'handler:\s*([\w.]+)', route_block)
            handler_ref = handler_match.group(1) if handler_match else None
            routes.append({
                'method': method,
                'url': url,
                'secure': secure,
                'handler': handler_ref,
            })

        # --- External HTTP calls (fetch) ---
        # Match: fetch(`${EXAMPLE_API_URL}/api/tx/build`, ...) or fetch('http://example:8000/api/somepath', ...)
        external_calls: List[str] = []
        # Template literal with env var prefix: fetch(`${VAR}/path/here`...)
        for m in re.finditer(r'fetch\s*\(\s*`\$\{[\w]+\}(/[^`]*)`', content):
            external_calls.append(m.group(1))
        # Literal URL with protocol: fetch('http://host:port/path'...)
        for m in re.finditer(r"""fetch\s*\(\s*['"]https?://[^/'"]*(/[^'"]*?)['"]""", content):
            external_calls.append(m.group(1))
        # Template literal without env var but with http prefix: fetch(`http://host:port/path`...)
        for m in re.finditer(r'fetch\s*\(\s*`https?://[^/`]*(\/[^`]*?)`', content):
            path = m.group(1)
            if path not in external_calls:
                external_calls.append(path)

        # --- Module summary ---
        first_comments: List[str] = []
        for line in source_lines[:15]:
            s = line.strip()
            if s.startswith('//'):
                first_comments.append(s.lstrip('/').strip())
            elif s.startswith('*') and not s.startswith('*/'):
                first_comments.append(s.lstrip('*').strip())
            elif s and not s.startswith('/*'):
                break

        summary_parts = [f"{language} module: {relative_path}"]
        if first_comments:
            summary_parts.append(' '.join(first_comments[:3]))
        if class_names:
            summary_parts.append(f"Classes: {', '.join(class_names[:8])}")
        if all_func_names:
            summary_parts.append(f"Functions: {', '.join(all_func_names[:8])}")
        if routes:
            route_strs = [f"{r['method']} {r['url']}" for r in routes[:5]]
            summary_parts.append(f"Routes: {', '.join(route_strs)}")
        if external_calls:
            summary_parts.append(f"External calls: {', '.join(external_calls[:5])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if (', 'if(', 'else if', '? ', 'while (', 'while(',
                                              'for (', 'for(', 'switch (', 'switch(', 'catch (', 'catch(']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language=language, loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        # --- Store classes ---
        for cname, (start_line, base_class) in class_info.items():
            # Extract methods defined inside the class body (rough heuristic: indented methods)
            methods: List[str] = []
            method_inside = re.compile(
                rf'(?:async\s+)?([\w]+)\s*\([^)]*\)\s*\{{',
                re.MULTILINE
            )
            # Grab lines from class start (rough: next 80 lines)
            class_lines = source_lines[max(0, start_line - 1):min(start_line + 80, len(source_lines))]
            class_body = '\n'.join(class_lines)
            for mm in method_inside.finditer(class_body):
                mname = mm.group(1)
                # Skip keywords that match function-like syntax
                if mname not in ('if', 'else', 'while', 'for', 'switch', 'return',
                                 'class', 'new', 'catch', 'constructor'):
                    methods.append(mname)

            signature = f"class {cname}"
            if base_class:
                signature += f" extends {base_class}"

            embedding = embed_class(signature, class_body, methods=methods[:10], language="javascript")

            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": cname,
                    "full_name": f"{file_path.stem}.{cname}",
                    "class_body": class_body,
                    "methods": methods[:20],
                    "signature": signature,
                    "doc": "",
                    "start_line": start_line,
                    "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['classes'] += 1

        # --- Store functions ---
        for fname, start_line, is_async in func_matches:
            end_line = min(start_line + 40, len(source_lines))
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            full_name = f"{file_path.stem}.{fname}"
            signature = f"{'async ' if is_async else ''}function {fname}()"
            embedding = embed_function(signature, body, language="javascript")

            insert_params = {
                "properties": {
                    "name": fname,
                    "full_name": full_name,
                    "function_body": body,
                    "signature": signature,
                    "doc": "",
                    "start_line": start_line,
                    "end_line": end_line,
                    "is_async": is_async,
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['functions'] += 1

        # --- Store Fastify routes as CodeAPI ---
        for route in routes:
            # Check if route handler calls fetch to a known path (proxy detection)
            proxy_target = None
            if route['handler'] and external_calls:
                # Simple heuristic: if there are external calls in the same file and
                # the route URL has a matching path segment, link them
                for ext_call in external_calls:
                    # If external call path matches or contains the route URL
                    if route['url'] in ext_call or ext_call.endswith(route['url']):
                        proxy_target = ext_call
                        break

            description = (
                f"{route['method']} {route['url']} "
                f"({'authenticated' if route['secure'] else 'public'})"
            )
            if route['handler']:
                description += f" -> {route['handler']}"
            if proxy_target:
                description += f" [proxies to {proxy_target}]"

            embedding = generate_embedding(description)

            insert_params = {
                "properties": {
                    "endpoint": route['url'],
                    "method": route['method'],
                    "api_description": description,
                    "parameters": [],
                    "returns": "",
                    "project": self.project_name,
                    "proxy_target": proxy_target or "",
                },
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.apis_collection, insert_params, insert_params["properties"].get("endpoint", "") + ":" + insert_params["properties"].get("method", ""))
            stats['apis'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content_clean, imports, language, relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, language, module_uuid)

        return stats

    def _filter_changed_files(self, repo_path: Path, files: List[Path]) -> List[Path]:
        """Filter files to only those changed according to git."""
        try:
            # Get changed files from git
            result = subprocess.run(
                ['git', 'diff', '--name-only', 'HEAD~1', 'HEAD'],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True
            )

            changed_paths = {repo_path / line.strip() for line in result.stdout.split('\n') if line.strip()}

            # Filter to only changed files
            return [f for f in files if f in changed_paths]

        except subprocess.CalledProcessError:
            print("⚠️  Git not available or not a git repo, analyzing all files")
            return files

    def _analyze_lua_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Lua file using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('--')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = str(file_path.relative_to(repo_root))

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Imports: require('mod') or require "mod"
        imports = re.findall(r"require\s*[(\s]*[\"']([^\"']+)[\"']", content)

        # Detect table-based classes: uppercase Name = {} or Name.__index = Name
        class_names: Set[str] = set()
        for m in re.finditer(r'^([A-Z][\w]*)\s*=\s*\{\}', content, re.MULTILINE):
            class_names.add(m.group(1))
        for m in re.finditer(r'^([\w]+)\.__index\s*=\s*\1', content, re.MULTILINE):
            class_names.add(m.group(1))

        # Function pattern: covers function f(), local function f(), Obj.f = function()
        func_pattern = re.compile(
            r'^(?:local\s+)?function\s+([\w.:]+)\s*\(([^)]*)\)|'
            r'^([\w.]+)\.([\w]+)\s*=\s*function\s*\(([^)]*)\)',
            re.MULTILINE
        )
        all_func_names = []
        for m in func_pattern.finditer(content):
            name = m.group(1) if m.group(1) else f'{m.group(3)}.{m.group(4)}'
            all_func_names.append(name)

        # Module summary from leading comments
        first_comments: List[str] = []
        for line in source_lines[:15]:
            s = line.strip()
            if s.startswith('--'):
                first_comments.append(s.lstrip('-').strip())
            elif s:
                break

        summary_parts = [f"Lua module: {relative_path}"]
        if first_comments:
            summary_parts.append(' '.join(first_comments[:3]))
        if class_names:
            summary_parts.append(f"Classes: {', '.join(sorted(class_names))}")
        if all_func_names:
            summary_parts.append(f"Functions: {', '.join(all_func_names[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content.count(kw) for kw in ['if ', 'elseif ', 'while ', 'for ', 'repeat ']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Lua", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        # Extract classes (table-OOP pattern)
        for class_name in class_names:
            methods: List[str] = []
            for m in re.finditer(
                rf'(?:function\s+{re.escape(class_name)}[.:]([\w]+)\s*\(|'
                rf'{re.escape(class_name)}\.([\w]+)\s*=\s*function\s*\()',
                content
            ):
                methods.append(m.group(1) or m.group(2))

            class_match = re.search(rf'^{re.escape(class_name)}\s*=\s*\{{', content, re.MULTILINE)
            start_line = content[:class_match.start()].count('\n') + 1 if class_match else 1

            body = f"{class_name} = {{}}\n" + '\n'.join(
                f"function {class_name}.{mth}(...) end" for mth in methods
            )
            signature = f"{class_name} = {{}} -- Lua table class"
            embedding = embed_class(signature, "", methods=methods, language="javascript")

            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": class_name, "full_name": f"{file_path.stem}.{class_name}",
                    "class_body": body, "methods": methods, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": start_line,
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['classes'] += 1

        # Extract standalone functions (skip class methods already indexed)
        class_prefixes = tuple(cn + '.' for cn in class_names) + tuple(cn + ':' for cn in class_names)

        for m in func_pattern.finditer(content):
            if m.group(1):
                func_name, args_str = m.group(1), m.group(2)
            else:
                func_name, args_str = f'{m.group(3)}.{m.group(4)}', m.group(5) or ''

            if any(func_name.startswith(p) for p in class_prefixes):
                continue

            start_line = content[:m.start()].count('\n') + 1
            end_line = min(start_line + 40, len(source_lines))
            body = '\n'.join(source_lines[start_line - 1:end_line])

            func_full_name = f"{file_path.stem}.{func_name}"
            embedding = embed_function(f"function {func_name}({args_str})", body, language="javascript")

            insert_params = {
                "properties": {
                    "name": func_name.split('.')[-1].split(':')[-1],
                    "full_name": func_full_name,
                    "function_body": body, "signature": f"{func_name}({args_str})",
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": False, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['functions'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content, imports, "Lua", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Lua", module_uuid)

        return stats

    def _analyze_cpp_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a C++/header file using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines
                   if l.strip() and not l.strip().startswith('//') and not l.strip().startswith('*')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = str(file_path.relative_to(repo_root))

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Strip comments for pattern matching
        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # Includes
        includes = re.findall(r'#include\s*[<"]([^>"]+)[>"]', content)

        # Classes / structs
        class_pattern = re.compile(
            r'(?:^|\n)\s*(?:class|struct)\s+([\w]+)\s*'
            r'(?::[^{]*)?\{',
            re.MULTILINE
        )
        class_info: Dict[str, int] = {}
        for m in class_pattern.finditer(content_clean):
            cname = m.group(1)
            if cname in ('if', 'else', 'while', 'for', 'switch', 'namespace', 'return'):
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            class_info[cname] = start_line

        # Method implementations: ClassName::methodName(...)
        method_pattern = re.compile(
            r'\b([\w]+)\s*::\s*([\w~]+)\s*\(([^)]*)\)\s*(?:const\s*)?(?:override\s*)?(?:noexcept\s*)?\{',
            re.MULTILINE
        )

        # Module summary from file-level comment
        file_comment = ''
        for line in source_lines[:20]:
            s = line.strip()
            if s.startswith('//') or (s.startswith('*') and not s.startswith('*/')):
                cleaned = s.lstrip('/*').strip()
                if cleaned:
                    file_comment = cleaned
                    break

        summary_parts = [f"C++ module: {relative_path}"]
        if file_comment:
            summary_parts.append(file_comment)
        if class_info:
            summary_parts.append(f"Classes: {', '.join(list(class_info.keys())[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if (', 'while (', 'for (', 'switch (', 'else if']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="C++", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=includes, module_summary=module_summary,
        )
        stats['modules'] = 1

        # Extract classes
        for cname, start_line in class_info.items():
            methods = [m.group(2) for m in method_pattern.finditer(content_clean)
                       if m.group(1) == cname]
            class_lines = source_lines[max(0, start_line - 1):min(start_line + 60, len(source_lines))]
            class_body = '\n'.join(class_lines)
            signature = f"class {cname}"
            embedding = generate_embedding(
                f"{signature}\nMethods: {', '.join(methods[:10])}\n{class_body[:500]}"
            )
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": cname, "full_name": f"{file_path.stem}.{cname}",
                    "class_body": class_body, "methods": methods[:20],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['classes'] += 1

        # Extract method implementations
        for m in method_pattern.finditer(content_clean):
            class_name, method_name, args_str = m.group(1), m.group(2), m.group(3)
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = min(start_line + 50, len(source_lines))
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            full_name = f"{file_path.stem}.{class_name}.{method_name}"
            signature = f"{class_name}::{method_name}({args_str})"
            embedding = embed_function(signature, body, language="cpp")
            insert_params = {
                "properties": {
                    "name": method_name, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": False, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['functions'] += 1

        # Cross-language interactions (C++ uses #include as import gate)
        ix = _extract_external_calls(content_clean, includes, "C++", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "C++", module_uuid)

        return stats

    def _analyze_go_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Go file using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('//')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = str(file_path.relative_to(repo_root))

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Strip comments
        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # Imports
        single_imports = re.findall(r'import\s+"([^"]+)"', content)
        block_imports = re.findall(r'"([^"]+)"', re.sub(r'import\s*\(([^)]*)\)', r'\1', content, flags=re.DOTALL))
        imports = list(dict.fromkeys(single_imports + block_imports))

        # Structs / interfaces as "classes"
        type_pattern = re.compile(r'type\s+([\w]+)\s+(?:struct|interface)\s*\{', re.MULTILINE)
        struct_info: Dict[str, int] = {}
        for m in type_pattern.finditer(content_clean):
            name = m.group(1)
            start_line = content_clean[:m.start()].count('\n') + 1
            struct_info[name] = start_line

        # Functions: func Name(...) and methods: func (recv Type) Name(...)
        func_pattern = re.compile(
            r'func\s+(?:\([^)]+\)\s+)?([\w]+)\s*\(([^)]*)\)',
            re.MULTILINE
        )

        # Module summary
        pkg_match = re.search(r'^package\s+(\w+)', content, re.MULTILINE)
        pkg_name = pkg_match.group(1) if pkg_match else file_path.stem
        file_comment = ''
        for line in source_lines[:15]:
            s = line.strip()
            if s.startswith('//'):
                file_comment = s.lstrip('/').strip()
                break
        summary_parts = [f"Go module: {relative_path} (package {pkg_name})"]
        if file_comment:
            summary_parts.append(file_comment)
        if struct_info:
            summary_parts.append(f"Types: {', '.join(list(struct_info.keys())[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if ', 'for ', 'switch ', 'select {']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Go", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        # Struct/interface entries
        for sname, start_line in struct_info.items():
            class_lines = source_lines[max(0, start_line - 1):min(start_line + 40, len(source_lines))]
            class_body = '\n'.join(class_lines)
            signature = f"type {sname} struct/interface"
            methods = [m.group(1) for m in func_pattern.finditer(content_clean)]
            embedding = embed_class(signature, class_body, language="go")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": sname, "full_name": f"{pkg_name}.{sname}",
                    "class_body": class_body, "methods": methods[:20],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['classes'] += 1

        # Function entries
        for m in func_pattern.finditer(content_clean):
            fname, args_str = m.group(1), m.group(2)
            if fname[0].islower() and fname in ('if', 'for', 'switch', 'select'):
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = min(start_line + 40, len(source_lines))
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            full_name = f"{pkg_name}.{fname}"
            signature = f"func {fname}({args_str})"
            embedding = embed_function(signature, body, language="go")
            insert_params = {
                "properties": {
                    "name": fname, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": False, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['functions'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content_clean, imports, "Go", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Go", module_uuid)

        return stats

    def _analyze_rust_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Rust file using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('//')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = str(file_path.relative_to(repo_root))

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # use statements
        imports = re.findall(r'use\s+([\w::{}, ]+);', content)

        # struct, enum, trait types
        type_pattern = re.compile(
            r'(?:pub\s+)?(?:struct|enum|trait)\s+([\w]+)', re.MULTILINE
        )
        struct_info: Dict[str, int] = {}
        for m in type_pattern.finditer(content_clean):
            name = m.group(1)
            start_line = content_clean[:m.start()].count('\n') + 1
            struct_info[name] = start_line

        # Functions: fn name(...)
        func_pattern = re.compile(
            r'(?:pub\s+)?(?:async\s+)?fn\s+([\w]+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)',
            re.MULTILINE
        )

        # Module summary
        crate_comment = ''
        for line in source_lines[:15]:
            s = line.strip()
            if s.startswith('//!') or s.startswith('///'):
                crate_comment = s.lstrip('/!').strip()
                break
        summary_parts = [f"Rust module: {relative_path}"]
        if crate_comment:
            summary_parts.append(crate_comment)
        if struct_info:
            summary_parts.append(f"Types: {', '.join(list(struct_info.keys())[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if ', 'while ', 'for ', 'match ', 'loop {']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Rust", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        for sname, start_line in struct_info.items():
            class_lines = source_lines[max(0, start_line - 1):min(start_line + 40, len(source_lines))]
            class_body = '\n'.join(class_lines)
            signature = f"struct/enum/trait {sname}"
            methods = [m.group(1) for m in func_pattern.finditer(content_clean)]
            embedding = embed_class(signature, class_body, language="rust")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": sname, "full_name": f"{file_path.stem}.{sname}",
                    "class_body": class_body, "methods": methods[:20],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['classes'] += 1

        for m in func_pattern.finditer(content_clean):
            fname, args_str = m.group(1), m.group(2)
            is_async = bool(re.search(rf'async\s+fn\s+{re.escape(fname)}', content_clean))
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = min(start_line + 40, len(source_lines))
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            full_name = f"{file_path.stem}.{fname}"
            signature = f"fn {fname}({args_str})"
            embedding = embed_function(signature, body, language="rust")
            insert_params = {
                "properties": {
                    "name": fname, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": is_async, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['functions'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content_clean, imports, "Rust", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Rust", module_uuid)

        return stats

    def _analyze_java_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Java file using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines
                   if l.strip() and not l.strip().startswith('//')
                   and not l.strip().startswith('*')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = str(file_path.relative_to(repo_root))

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

        # import statements
        imports = re.findall(r'import\s+([\w.]+);', content)

        # class / interface / enum
        class_pattern = re.compile(
            r'(?:public|private|protected|abstract|final|\s)*'
            r'(?:class|interface|enum)\s+([\w]+)'
            r'(?:\s+extends\s+[\w<>, ]+)?(?:\s+implements\s+[\w<>, ]+)?\s*\{',
            re.MULTILINE
        )
        class_info: Dict[str, int] = {}
        for m in class_pattern.finditer(content_clean):
            name = m.group(1)
            if not name or name[0].islower():
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            class_info[name] = start_line

        # Methods
        method_pattern = re.compile(
            r'(?:public|private|protected|static|final|synchronized|native|abstract|\s)+'
            r'(?:[\w<>\[\]]+\s+)+([\w]+)\s*\(([^)]*)\)\s*(?:throws\s+[\w, ]+)?\s*\{',
            re.MULTILINE
        )

        # Package + summary
        pkg_match = re.search(r'^package\s+([\w.]+);', content, re.MULTILINE)
        pkg_name = pkg_match.group(1) if pkg_match else ''
        summary_parts = [f"Java module: {relative_path}"]
        if pkg_name:
            summary_parts.append(f"Package: {pkg_name}")
        if class_info:
            summary_parts.append(f"Classes: {', '.join(list(class_info.keys())[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if (', 'while (', 'for (', 'switch (', 'catch (']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Java", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        for cname, start_line in class_info.items():
            class_lines = source_lines[max(0, start_line - 1):min(start_line + 60, len(source_lines))]
            class_body = '\n'.join(class_lines)
            methods = [m.group(1) for m in method_pattern.finditer(content_clean)]
            signature = f"class {cname}"
            embedding = embed_class(signature, class_body, methods=methods[:10], language="java")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": cname,
                    "full_name": f"{pkg_name}.{cname}" if pkg_name else cname,
                    "class_body": class_body, "methods": methods[:20],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['classes'] += 1

        for m in method_pattern.finditer(content_clean):
            mname, args_str = m.group(1), m.group(2)
            if mname in ('if', 'while', 'for', 'switch', 'catch', 'try', 'else', 'return'):
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = min(start_line + 50, len(source_lines))
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            # Find enclosing class
            enclosing = next(
                (c for c, cl in sorted(class_info.items(), key=lambda x: x[1], reverse=True)
                 if cl <= start_line), file_path.stem
            )
            full_name = f"{enclosing}.{mname}"
            signature = f"{mname}({args_str})"
            embedding = embed_function(signature, body, language="java")
            insert_params = {
                "properties": {
                    "name": mname, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": False, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['functions'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content_clean, imports, "Java", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Java", module_uuid)

        return stats

    def _analyze_ruby_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Ruby file using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('#')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = str(file_path.relative_to(repo_root))

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Strip inline comments
        content_clean = re.sub(r'#.*$', '', content, flags=re.MULTILINE)
        # Strip =begin/=end blocks
        content_clean = re.sub(r'^=begin.*?^=end', ' ', content_clean, flags=re.MULTILINE | re.DOTALL)

        # require / require_relative
        imports = re.findall(r'require(?:_relative)?\s+[\'"]([^\'"]+)[\'"]', content)

        # class / module definitions
        class_pattern = re.compile(
            r'^(?:class|module)\s+([\w:]+)(?:\s*<\s*[\w:]+)?\s*$',
            re.MULTILINE
        )
        class_info: Dict[str, int] = {}
        for m in class_pattern.finditer(content_clean):
            name = m.group(1).split('::')[-1]  # unqualified name
            start_line = content_clean[:m.start()].count('\n') + 1
            class_info[name] = start_line

        # methods: def name or def self.name
        func_pattern = re.compile(
            r'^[ \t]*def\s+(?:self\.)?([\w?!]+)\s*(?:\(([^)]*)\))?',
            re.MULTILINE
        )

        # Module summary
        file_comment = ''
        for line in source_lines[:15]:
            s = line.strip()
            if s.startswith('#') and not s.startswith('#!'):
                file_comment = s.lstrip('#').strip()
                break
        summary_parts = [f"Ruby module: {relative_path}"]
        if file_comment:
            summary_parts.append(file_comment)
        if class_info:
            summary_parts.append(f"Classes: {', '.join(list(class_info.keys())[:8])}")
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if ', 'unless ', 'while ', 'until ', 'case ', 'rescue ']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Ruby", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        for cname, start_line in class_info.items():
            class_lines = source_lines[max(0, start_line - 1):min(start_line + 50, len(source_lines))]
            class_body = '\n'.join(class_lines)
            methods = [m.group(1) for m in func_pattern.finditer(content_clean)]
            signature = f"class {cname}"
            embedding = embed_class(signature, class_body, methods=methods[:10], language="ruby")
            insert_params: Dict[str, Any] = {
                "properties": {
                    "name": cname, "full_name": f"{file_path.stem}.{cname}",
                    "class_body": class_body, "methods": methods[:20],
                    "signature": signature, "doc": "",
                    "start_line": start_line, "end_line": start_line + len(class_lines),
                    "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['classes'] += 1

        for m in func_pattern.finditer(content_clean):
            fname = m.group(1)
            args_str = m.group(2) or ''
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = min(start_line + 30, len(source_lines))
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            enclosing = next(
                (c for c, cl in sorted(class_info.items(), key=lambda x: x[1], reverse=True)
                 if cl <= start_line), file_path.stem
            )
            full_name = f"{enclosing}.{fname}"
            signature = f"def {fname}({args_str})"
            embedding = embed_function(signature, body, language="ruby")
            insert_params = {
                "properties": {
                    "name": fname, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": False, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['functions'] += 1

        # Cross-language interactions
        ix = _extract_external_calls(content_clean, imports, "Ruby", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Ruby", module_uuid)

        return stats

    def _analyze_shell_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a Shell script using regex-based parsing."""
        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')
        loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('#')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()
        relative_path = str(file_path.relative_to(repo_root))

        if self._get_existing_module(relative_path, file_hash):
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        content_clean = re.sub(r'#.*$', '', content, flags=re.MULTILINE)

        # source / . file
        imports = re.findall(r'(?:source|\.)\s+([\w./${}/_-]+)', content)

        # Functions: name() { or function name {
        func_pattern = re.compile(
            r'^[ \t]*(?:function\s+)?([\w:.-]+)\s*\(\s*\)\s*\{|^[ \t]*function\s+([\w:.-]+)\s*\{',
            re.MULTILINE
        )

        # Module summary from top comment
        file_comment = ''
        for line in source_lines[:20]:
            s = line.strip()
            if s.startswith('#') and not s.startswith('#!') and s.lstrip('#').strip():
                file_comment = s.lstrip('#').strip()
                break
        summary_parts = [f"Shell script: {relative_path}"]
        if file_comment:
            summary_parts.append(file_comment)
        module_summary = '\n'.join(summary_parts)

        complexity = float(1 + sum(content_clean.count(kw)
                                   for kw in ['if [', 'if [[', 'while ', 'for ', 'case ']))

        module_uuid = self._create_or_update_module(
            path=relative_path, language="Shell", loc=loc, complexity=complexity,
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash, imports=imports, module_summary=module_summary,
        )
        stats['modules'] = 1

        for m in func_pattern.finditer(content_clean):
            fname = m.group(1) or m.group(2)
            if not fname:
                continue
            start_line = content_clean[:m.start()].count('\n') + 1
            end_line = min(start_line + 30, len(source_lines))
            body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
            full_name = f"{file_path.stem}.{fname}"
            signature = f"{fname}()"
            embedding = embed_function(signature, body, language="python")
            insert_params = {
                "properties": {
                    "name": fname, "full_name": full_name,
                    "function_body": body, "signature": signature,
                    "doc": "", "start_line": start_line, "end_line": end_line,
                    "is_async": False, "project": self.project_name,
                },
                "references": {"module": module_uuid},
            }
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))
            stats['functions'] += 1

        # Cross-language interactions (shell: gate on curl/wget presence in content)
        ix = _extract_external_calls(content_clean, imports + ["curl", "wget"], "shell", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Shell", module_uuid)

        return stats

    def _analyze_python_file(self, file_path: Path, repo_root: Path) -> Dict[str, int]:
        """Analyze a single Python file and extract entities."""

        stats = {'modules': 0, 'classes': 0, 'functions': 0}

        # Read file
        content = file_path.read_text(encoding='utf-8', errors='ignore')
        source_lines = content.split('\n')

        # Parse AST
        try:
            tree = ast.parse(content, filename=str(file_path))
        except SyntaxError as e:
            print(f"⚠️  Syntax error in {file_path.relative_to(repo_root)}: {e}")
            return stats

        # Calculate file metrics
        loc = len([line for line in source_lines if line.strip() and not line.strip().startswith('#')])
        file_hash = hashlib.sha256(content.encode()).hexdigest()

        # Check if file already analyzed (by hash)
        relative_path = str(file_path.relative_to(repo_root))
        existing_module = self._get_existing_module(relative_path, file_hash)

        if existing_module:
            print(f"⏭️  Skipping {relative_path} (unchanged)")
            return stats

        # Extract imports
        imports = self._extract_imports(tree)

        # Generate module summary (first docstring or file description)
        module_summary = self._generate_module_summary(tree, source_lines, relative_path)

        # Create/update module
        module_uuid = self._create_or_update_module(
            path=relative_path,
            language="Python",
            loc=loc,
            complexity=self._calculate_complexity(tree),
            last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
            file_hash=file_hash,
            imports=imports,
            module_summary=module_summary
        )

        # Cache imports for cross-reference linking
        self.module_imports[relative_path] = imports

        stats['modules'] = 1

        # Track methods to avoid double-counting
        methods_seen = set()

        # Extract classes first and track their methods
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                self._extract_class(node, module_uuid, file_path, repo_root, source_lines)
                stats['classes'] += 1
                # Track all methods in this class
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods_seen.add(id(item))

        # Extract only top-level functions (not methods)
        for node in tree.body:  # Only check top-level items
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if id(node) not in methods_seen:
                    self._extract_function(node, module_uuid, file_path, repo_root, source_lines)
                    stats['functions'] += 1

        # Cross-language interactions (Python: use raw content; _strip_triple_quoted handles docstrings)
        ix = _extract_external_calls(content, imports, "Python", relative_path)
        if ix:
            stats['interactions'] = self._store_interactions(ix, "Python", module_uuid)

        return stats

    # ------------------------------------------------------------------
    # Cross-language interaction storage
    # ------------------------------------------------------------------

    def _store_interactions(
        self,
        interactions: List[Dict[str, str]],
        language: str,
        module_uuid: str,
        func_uuid: Optional[str] = None,
    ) -> int:
        """Store a list of extracted interactions into CodeInteraction collection.

        Args:
            interactions: Output of _extract_external_calls().
            language:     Source language label (Python, Go, etc.)
            module_uuid:  UUID of the CodeModule containing the calls.
            func_uuid:    UUID of the specific CodeFunction (optional).

        Returns:
            Number of interactions stored.
        """
        count = 0
        for ix in interactions:
            description = (
                f"{language}→{ix['interaction_type'].upper()} "
                f"{ix['protocol']} {ix['endpoint']} "
                f"[{ix['confidence']}]"
            )
            embedding = generate_embedding(description)
            insert_params: Dict[str, Any] = {
                "properties": {
                    "source_project": self.project_name,
                    "interaction_type": ix["interaction_type"],
                    "direction": ix.get("direction", "outbound"),
                    "protocol": ix["protocol"],
                    "endpoint": ix["endpoint"],
                    "raw_target": ix.get("raw_target", ""),
                    "confidence": ix.get("confidence", "high"),
                    "description": description,
                },
                "references": {"source_module": module_uuid},
            }
            if func_uuid:
                insert_params["references"]["source_function"] = func_uuid
            if embedding:
                insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding
            try:
                self._dedup_insert(self.interactions_collection, insert_params, f"ix::{ix.get('source','')}::{ix.get('endpoint','')}")
                count += 1
            except Exception as exc:
                # Non-fatal — log and continue
                pass
        return count

    def _get_existing_module(self, path: str, file_hash: str) -> Optional[str]:
        """Check if module already exists with same hash.

        Returns the UUID if a module with matching path and hash exists,
        so it can be skipped during incremental analysis.
        """
        try:
            result = self.modules_collection.query.fetch_objects(
                filters=Filter.by_property("path").equal(path) &
                        Filter.by_property("file_hash").equal(file_hash),
                limit=1
            )
            if result.objects:
                return result.objects[0].uuid
            return None
        except Exception:
            return None

    def _extract_imports(self, tree: ast.AST) -> List[str]:
        """Extract import statements from AST."""
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        return imports

    def _generate_module_summary(self, tree: ast.AST, source_lines: List[str], path: str) -> str:
        """Generate a summary of the module for embedding."""
        # Try to get module docstring
        docstring = ast.get_docstring(tree)
        if docstring:
            return f"Module: {path}\n{docstring}"

        # Otherwise create summary from file structure
        classes = []
        functions = []

        for node in ast.walk(tree):
            try:
                if isinstance(node, ast.ClassDef):
                    classes.append(node.name)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.append(node.name)
            except (AttributeError, TypeError):
                # Skip nodes that don't have expected attributes
                continue

        summary_parts = [f"Module: {path}"]
        if classes:
            summary_parts.append(f"Classes: {', '.join(classes[:5])}")  # Limit to first 5
        if functions:
            summary_parts.append(f"Functions: {', '.join(functions[:5])}")  # Limit to first 5

        return "\n".join(summary_parts)

    def _calculate_complexity(self, tree: ast.AST) -> float:
        """Calculate cyclomatic complexity (simplified)."""
        complexity = 1  # Base complexity

        for node in ast.walk(tree):
            # Decision points
            if isinstance(node, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(node, ast.BoolOp):
                complexity += len(node.values) - 1

        return float(complexity)

    def _create_or_update_module(self, path: str, language: str, loc: int,
                                 complexity: float, last_modified: datetime,
                                 file_hash: str, imports: List[str], module_summary: str) -> str:
        """Create or update module in Weaviate."""

        # Check if exists
        if path in self.module_cache:
            # Update existing
            self.modules_collection.data.update(
                uuid=self.module_cache[path],
                properties={
                    "module_summary": module_summary,
                    "loc": loc,
                    "complexity": complexity,
                    "last_modified": last_modified.isoformat(),
                    "file_hash": file_hash,
                    "import_names": imports,
                }
            )
            return self.module_cache[path]

        # Create new - generate embedding from module_summary
        embedding = embed_module(module_summary)

        insert_params = {
            "properties": {
                "path": path,
                "language": language,
                "module_summary": module_summary,
                "loc": loc,
                "complexity": complexity,
                "project": self.project_name,
                "last_modified": last_modified.isoformat(),
                "file_hash": file_hash,
                "import_names": imports,
            }
        }

        # Add vector if embedding generation succeeded
        if embedding:
            insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding

        uuid = self._dedup_insert(self.modules_collection, insert_params, f"module::{path}")

        self.module_cache[path] = uuid
        return uuid

    def _extract_function_calls(self, node: ast.AST) -> List[str]:
        """Extract names of functions/methods called within an AST node.

        Returns list of call target names (e.g. 'func_name', 'ClassName.method').
        Only extracts simple Name and Attribute calls, skipping builtins.
        """
        _BUILTINS = {
            'print', 'len', 'range', 'enumerate', 'zip', 'map', 'filter',
            'sorted', 'reversed', 'list', 'dict', 'set', 'tuple', 'str',
            'int', 'float', 'bool', 'bytes', 'type', 'isinstance',
            'issubclass', 'hasattr', 'getattr', 'setattr', 'delattr',
            'super', 'property', 'staticmethod', 'classmethod',
            'open', 'iter', 'next', 'id', 'hash', 'repr', 'abs',
            'min', 'max', 'sum', 'any', 'all', 'ord', 'chr', 'hex',
            'vars', 'dir', 'format', 'input', 'round',
        }
        calls: List[str] = []
        seen: Set[str] = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                # e.g. self.method() -> 'method', obj.func() -> 'func'
                name = func.attr
            if name and name not in _BUILTINS and name not in seen:
                seen.add(name)
                calls.append(name)
        return calls

    def _populate_caches_from_weaviate(self):
        """Load all existing objects into caches from Weaviate.

        This allows cross-reference creation even when no files were re-analyzed
        (e.g., all skipped as unchanged in incremental mode).
        """
        if not self.client:
            return

        print("   Loading existing entities from Weaviate (merging with analysis cache)...")

        # Load modules (including import_names for cross-ref linking)
        try:
            for obj in self.modules_collection.iterator():
                path = obj.properties.get("path", "")
                if path:
                    self.module_cache[path] = str(obj.uuid)
                    # Populate module_imports from stored import_names
                    import_names = obj.properties.get("import_names")
                    if import_names:
                        self.module_imports[path] = import_names
        except Exception as e:
            print(f"   ⚠️  Failed to load modules: {e}")

        # Load classes
        try:
            for obj in self.classes_collection.iterator():
                full_name = obj.properties.get("full_name", "")
                if full_name:
                    self.class_cache[full_name] = str(obj.uuid)
        except Exception as e:
            print(f"   ⚠️  Failed to load classes: {e}")

        # Load functions
        try:
            for obj in self.functions_collection.iterator():
                full_name = obj.properties.get("full_name", "")
                if full_name:
                    self.function_cache[full_name] = str(obj.uuid)
        except Exception as e:
            print(f"   ⚠️  Failed to load functions: {e}")

        print(f"   Loaded {len(self.module_cache)} modules, {len(self.class_cache)} classes, {len(self.function_cache)} functions")

    def create_cross_references(self) -> Dict[str, int]:
        """Post-processing pass: create cross-references between Weaviate objects.

        Uses the caches (module_cache, class_cache, function_cache) populated
        during analysis to resolve string names to UUIDs and create references:
        - CodeFunction.calls -> CodeFunction (by matching call names to full_names)
        - CodeClass.extends -> CodeClass (by matching base class names)
        - CodeModule.imports -> CodeModule (by matching import names to paths)

        Returns dict with counts of references created per type.
        """
        stats = {'calls': 0, 'extends': 0, 'imports': 0}

        if not self.client:
            print("⚠️  Not connected to Weaviate, skipping cross-references")
            return stats

        print("\n🔗 Creating cross-references...")

        # If caches are empty (all files skipped), populate from Weaviate
        # Always populate caches from Weaviate to ensure all entities are available
        # for cross-reference resolution (not just the ones analyzed this run)
        self._populate_caches_from_weaviate()

        if not self.function_cache and not self.class_cache and not self.module_cache:
            print("   ⚠️  No entities found, skipping cross-references")
            return stats

        # Build reverse lookups for matching
        # function name (short) -> list of full_names that end with that name
        func_name_to_full: Dict[str, List[str]] = {}
        for full_name in self.function_cache:
            short_name = full_name.rsplit(".", 1)[-1]
            func_name_to_full.setdefault(short_name, []).append(full_name)

        # class name (short) -> list of full_names
        class_name_to_full: Dict[str, List[str]] = {}
        for full_name in self.class_cache:
            short_name = full_name.rsplit(".", 1)[-1]
            class_name_to_full.setdefault(short_name, []).append(full_name)

        # import module name -> module path (match last component of path)
        # e.g. import "foo.bar" matches path "src/foo/bar.py" or module "bar"
        module_name_to_path: Dict[str, List[str]] = {}
        for path in self.module_cache:
            # "src/foo/bar.py" -> stem "bar"
            stem = Path(path).stem
            module_name_to_path.setdefault(stem, []).append(path)
            # Also index dotted path: "src/foo/bar.py" -> "foo.bar"
            parts = Path(path).with_suffix("").parts
            if len(parts) > 1:
                dotted = ".".join(parts[-2:])
                module_name_to_path.setdefault(dotted, []).append(path)

        # --- 1. Function calls ---
        print("   Linking function calls...")
        for full_name, func_uuid in self.function_cache.items():
            # Fetch the function to get its body and extract calls
            try:
                resp = self.functions_collection.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(full_name),
                    limit=1,
                )
                if not resp.objects:
                    continue
                body = resp.objects[0].properties.get("function_body", "")
                if not body:
                    continue

                # Parse body to extract calls
                try:
                    tree = ast.parse(body)
                except SyntaxError:
                    continue

                call_names = self._extract_function_calls(tree)
                refs_to_add = []
                for call_name in call_names:
                    # Try exact full_name match first
                    if call_name in self.function_cache:
                        refs_to_add.append(self.function_cache[call_name])
                        continue
                    # Try short name match (prefer same module)
                    candidates = func_name_to_full.get(call_name, [])
                    if len(candidates) == 1:
                        refs_to_add.append(self.function_cache[candidates[0]])
                    elif len(candidates) > 1:
                        # Prefer same module prefix
                        module_prefix = full_name.rsplit(".", 1)[0] if "." in full_name else ""
                        same_module = [c for c in candidates if c.startswith(module_prefix + ".")]
                        if same_module:
                            refs_to_add.append(self.function_cache[same_module[0]])
                        else:
                            refs_to_add.append(self.function_cache[candidates[0]])

                # Store call_names as text array for callers queries
                if call_names:
                    try:
                        self.functions_collection.data.update(
                            uuid=func_uuid,
                            properties={"call_names": list(call_names)},
                        )
                    except Exception:
                        pass

                if refs_to_add:
                    try:
                        for ref_uuid in refs_to_add:
                            self.functions_collection.data.reference_add(
                                from_uuid=func_uuid,
                                from_property="calls",
                                to=ref_uuid,
                            )
                            stats['calls'] += 1
                    except Exception as e:
                        logger.debug(f"Failed to add call refs for {full_name}: {e}")

            except Exception as e:
                logger.debug(f"Error processing calls for {full_name}: {e}")

        # --- 2. Class extends ---
        print("   Linking class inheritance...")
        for full_name, class_uuid in self.class_cache.items():
            try:
                resp = self.classes_collection.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(full_name),
                    limit=1,
                )
                if not resp.objects:
                    continue
                props = resp.objects[0].properties
                signature = props.get("signature", "")
                # Extract base class names from signature: "class Foo(Bar, Baz)"
                base_match = re.search(r'\(([^)]+)\)', signature)
                if not base_match:
                    continue
                base_names = [b.strip() for b in base_match.group(1).split(",")]

                for base_name in base_names:
                    # Skip common non-project bases (builtins, stdlib, popular libs)
                    if base_name in ('object', 'Exception', 'BaseException',
                                     'ABC', 'Protocol', 'TypedDict', 'Enum',
                                     'IntEnum', 'StrEnum', 'BaseModel',
                                     'unittest.TestCase', 'TestCase',
                                     'str', 'int', 'float', 'bytes', 'dict',
                                     'list', 'tuple', 'set', 'frozenset',
                                     'type', 'Generic', 'NamedTuple',
                                     'Thread', 'Process', 'Handler',
                                     'logging.Handler'):
                        continue
                    # Try exact match
                    if base_name in self.class_cache:
                        ref_uuid = self.class_cache[base_name]
                    else:
                        # Try short name
                        candidates = class_name_to_full.get(base_name, [])
                        if not candidates:
                            continue
                        ref_uuid = self.class_cache[candidates[0]]

                    try:
                        self.classes_collection.data.reference_add(
                            from_uuid=class_uuid,
                            from_property="extends",
                            to=ref_uuid,
                        )
                        stats['extends'] += 1
                    except Exception as e:
                        logger.debug(f"Failed to add extends ref {full_name}->{base_name}: {e}")

            except Exception as e:
                logger.debug(f"Error processing extends for {full_name}: {e}")

        # --- 3. Module imports ---
        print("   Linking module imports...")
        for mod_path, import_names in self.module_imports.items():
            mod_uuid = self.module_cache.get(mod_path)
            if not mod_uuid or not import_names:
                continue
            for imp_name in import_names:
                # Try matching import name to a module in cache
                # "os.path" -> try "path", "os.path", "os"
                target_path = None
                # Direct stem match: import "bar" -> "bar.py"
                candidates = module_name_to_path.get(imp_name, [])
                if not candidates:
                    # Try last component: "foo.bar" -> "bar"
                    last = imp_name.rsplit(".", 1)[-1]
                    candidates = module_name_to_path.get(last, [])
                if candidates:
                    target_path = candidates[0]

                if target_path and target_path != mod_path:
                    target_uuid = self.module_cache.get(target_path)
                    if target_uuid:
                        try:
                            self.modules_collection.data.reference_add(
                                from_uuid=mod_uuid,
                                from_property="imports",
                                to=target_uuid,
                            )
                            stats['imports'] += 1
                        except Exception as e:
                            logger.debug(f"Failed to add import ref {mod_path}->{target_path}: {e}")

        print(f"   Done: {stats['calls']} call refs, {stats['extends']} extends refs, {stats['imports']} import refs")
        return stats

    def _extract_annotation_type_names(self, annotation: Optional[ast.expr]) -> List[str]:
        """Recursively extract simple type names from an AST annotation node.

        Handles ast.Name ('MyClass'), ast.Attribute ('module.Type'),
        ast.Subscript ('List[X]', 'Optional[X]'), and ast.BinOp ('X | Y').
        Returns a deduplicated list of non-builtin type name strings.
        """
        if annotation is None:
            return []

        _BUILTINS = {
            'str', 'int', 'float', 'bool', 'bytes', 'None', 'NoneType',
            'list', 'dict', 'set', 'tuple', 'frozenset',
            'List', 'Dict', 'Set', 'Tuple', 'FrozenSet',
            'Optional', 'Union', 'Any', 'Callable', 'Type',
            'Sequence', 'Iterable', 'Iterator', 'Generator',
            'Awaitable', 'Coroutine', 'AsyncIterator', 'AsyncGenerator',
            'ClassVar', 'Final', 'Literal', 'Annotated', 'TypeVar',
        }

        names: List[str] = []

        def _walk(node: ast.expr) -> None:
            if isinstance(node, ast.Name):
                if node.id not in _BUILTINS:
                    names.append(node.id)
            elif isinstance(node, ast.Attribute):
                # e.g. 'module.Type' — take the leaf attribute name
                if node.attr not in _BUILTINS:
                    names.append(node.attr)
            elif isinstance(node, ast.Subscript):
                # e.g. List[MyClass] — recurse into the slice only (skip container name)
                _walk(node.slice)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                # PEP 604 union: X | Y
                _walk(node.left)
                _walk(node.right)
            elif isinstance(node, ast.Tuple):
                for elt in node.elts:
                    _walk(elt)

        _walk(annotation)

        # Deduplicate while preserving order
        seen: Set[str] = set()
        result: List[str] = []
        for n in names:
            if n not in seen:
                seen.add(n)
                result.append(n)
        return result

    def _extract_field_types(self, node: ast.ClassDef) -> List[str]:
        """Extract annotated field declarations from a class as 'field_name:TypeName' strings.

        Covers:
        - Class-level annotated attributes: ``x: MyType``
        - Annotated assignments in __init__: ``self.x: MyType = ...``
        """
        field_pairs: List[str] = []
        seen_fields: Set[str] = set()

        def _record(field_name: str, annotation: ast.expr) -> None:
            type_names = self._extract_annotation_type_names(annotation)
            for type_name in type_names:
                pair = f"{field_name}:{type_name}"
                if pair not in seen_fields:
                    seen_fields.add(pair)
                    field_pairs.append(pair)

        # Class-level annotated attributes
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                _record(item.target.id, item.annotation)

        # Annotated assignments inside __init__
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == '__init__':
                for stmt in ast.walk(item):
                    if isinstance(stmt, ast.AnnAssign):
                        target = stmt.target
                        # self.x: Type
                        if (isinstance(target, ast.Attribute)
                                and isinstance(target.value, ast.Name)
                                and target.value.id == 'self'):
                            _record(target.attr, stmt.annotation)
                break

        return field_pairs

    def _extract_class(self, node: ast.ClassDef, module_uuid: str,
                      file_path: Path, repo_root: Path, source_lines: List[str]):
        """Extract class definition."""

        # Get methods
        methods = [m.name for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]

        # Get docstring
        doc = ast.get_docstring(node) or ""

        # Get base classes
        base_names = [self._get_name(base) for base in node.bases]

        # Extract full class body for embedding
        class_body = self._extract_source_code(node, source_lines)

        # Get signature (class definition line only)
        signature = f"class {node.name}"
        if node.bases:
            signature += f"({', '.join(base_names)})"

        # Create class - smart truncation for embedding
        # class_body is full source (includes class line + docstring + body)
        embedding = embed_class(signature, class_body, methods=methods, language="python")

        # Extract SCG-style composition edges
        field_types = self._extract_field_types(node)
        # composes = unique class names that appear as field types
        composes: List[str] = []
        seen_composes: Set[str] = set()
        for pair in field_types:
            type_name = pair.split(':', 1)[1] if ':' in pair else ''
            if type_name and type_name not in seen_composes:
                seen_composes.add(type_name)
                composes.append(type_name)

        insert_params = {
            "properties": {
                "name": node.name,
                "full_name": f"{file_path.stem}.{node.name}",
                "class_body": class_body,
                "methods": methods,
                "signature": signature,
                "doc": doc,
                "start_line": node.lineno,
                "end_line": node.end_lineno or node.lineno,
                "project": self.project_name,
                "field_types": field_types,
                "composes": composes,
            },
            "references": {
                "module": module_uuid,
            }
        }

        # Add vector if embedding generation succeeded
        if embedding:
            insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding

        self._dedup_insert(self.classes_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))

        self.class_cache[f"{file_path.stem}.{node.name}"] = class_uuid

        # Extract methods
        for method in node.body:
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._extract_function(method, module_uuid, file_path, repo_root, source_lines,
                                     parent_class=node.name)

    def _extract_function(self, node: ast.FunctionDef, module_uuid: str,
                         file_path: Path, repo_root: Path, source_lines: List[str],
                         parent_class: Optional[str] = None):
        """Extract function definition."""

        # Get signature
        args = [arg.arg for arg in node.args.args]
        signature = f"{node.name}({', '.join(args)})"

        # Get docstring
        doc = ast.get_docstring(node) or ""

        # Extract full function body for embedding
        function_body = self._extract_source_code(node, source_lines)

        # Determine full name
        if parent_class:
            full_name = f"{file_path.stem}.{parent_class}.{node.name}"
        else:
            full_name = f"{file_path.stem}.{node.name}"

        # Extract SCG-style type_uses from argument annotations and return annotation
        type_uses: List[str] = []
        seen_type_uses: Set[str] = set()

        def _add_type_names(annotation: Optional[ast.expr]) -> None:
            for t in self._extract_annotation_type_names(annotation):
                if t not in seen_type_uses:
                    seen_type_uses.add(t)
                    type_uses.append(t)

        for arg in node.args.args:
            _add_type_names(arg.annotation)
        for arg in node.args.posonlyargs:
            _add_type_names(arg.annotation)
        for arg in node.args.kwonlyargs:
            _add_type_names(arg.annotation)
        if node.args.vararg:
            _add_type_names(node.args.vararg.annotation)
        if node.args.kwarg:
            _add_type_names(node.args.kwarg.annotation)
        _add_type_names(node.returns)

        # CFG/PDG data (optional, populated by analyze_repository's pre-pass)
        cfg_pdg_store = getattr(self, '_cfg_pdg_data', {})
        cfg_pdg = cfg_pdg_store.get(full_name, {})
        cfg_summary = cfg_pdg.get("cfg_summary", "")
        data_flow_vars = cfg_pdg.get("data_flow_vars", [])

        # Create function - smart truncation for embedding
        # function_body is full source (includes def line + docstring + body)
        embedding = embed_function(signature, function_body, language="python")

        insert_params = {
            "properties": {
                "name": node.name,
                "full_name": full_name,
                "function_body": function_body,
                "signature": signature,
                "doc": doc,
                "start_line": node.lineno,
                "end_line": node.end_lineno or node.lineno,
                "is_async": isinstance(node, ast.AsyncFunctionDef),
                "project": self.project_name,
                "type_uses": type_uses,
                "cfg_summary": cfg_summary,
                "data_flow_vars": data_flow_vars,
            },
            "references": {
                "module": module_uuid,
            }
        }

        # Add vector if embedding generation succeeded
        if embedding:
            insert_params["vector"] = {_ACTIVE_CODE_VECTOR: embedding} if DUAL_EMBEDDING_ENABLED else embedding

        func_uuid = self._dedup_insert(self.functions_collection, insert_params, insert_params["properties"].get("full_name", insert_params["properties"]["name"]))

        self.function_cache[full_name] = func_uuid

    def _get_name(self, node: ast.AST) -> str:
        """Get name from AST node."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return f"{self._get_name(node.value)}.{node.attr}"
        else:
            return ""

    def _extract_source_code(self, node: ast.AST, source_lines: List[str]) -> str:
        """Extract source code for an AST node using line numbers."""
        if not hasattr(node, 'lineno') or not hasattr(node, 'end_lineno'):
            return ""

        start = node.lineno - 1  # Convert to 0-based index
        end = node.end_lineno  # end_lineno is inclusive, so we don't subtract 1

        if start < 0 or end > len(source_lines):
            return ""

        return '\n'.join(source_lines[start:end])

    def _extract_cfg_pdg(
        self,
        repo_path: Path,
        language: str,
        extract_cfg: bool = False,
        extract_pdg: bool = False,
    ) -> Dict[str, Any]:
        """Run Joern to extract CFG/PDG data for functions in a repository.

        Both flags are opt-in and require joern in PATH.
        Returns empty dict on any error (non-blocking).

        Args:
            repo_path: Path to repository root.
            language: Language hint for Joern ('python', 'cpp', etc.).
            extract_cfg: Whether to extract CFG summaries.
            extract_pdg: Whether to extract PDG data-flow variable lists.

        Returns:
            Dict mapping full_name -> {"cfg_summary": str, "data_flow_vars": list[str]}.
            Empty dict if joern unavailable or any error occurs.
        """
        if not extract_cfg and not extract_pdg:
            return {}

        if not shutil.which("joern"):
            logger.warning("joern not found in PATH; skipping CFG/PDG extraction")
            return {}

        # Joern script: export CPG, collect CFG edge counts and PDG variable names per method
        joern_script = """
importCode(inputPath="{repo_path}", projectName="tmp_cgraph")
val methods = cpg.method.l
val result = methods.map {{ m =>
  val cfg_branches = m.cfgNode.isControlStructure.l.size
  val cfg_loops = m.cfgNode.isControlStructure.filter(_.controlStructureType.matches("FOR|WHILE|DO")).l.size
  val cfg_max_depth = m.depth
  val pdg_vars = m.local.name.l.distinct
  val entry = ujson.Obj(
    "full_name" -> m.fullName,
    "cfg_summary" -> s"branches:${{cfg_branches}} loops:${{cfg_loops}} max_depth:${{cfg_max_depth}}",
    "data_flow_vars" -> ujson.Arr(pdg_vars.map(ujson.Str(_)): _*)
  )
  entry
}}
println(upickle.default.write(result))
exit
""".strip().format(repo_path=str(repo_path))

        result: Dict[str, Any] = {}
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.sc', delete=False, prefix='joern_cgraph_'
            ) as tmp:
                tmp.write(joern_script)
                tmp_path = tmp.name

            proc = subprocess.run(
                ["joern", "--script", tmp_path],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if proc.returncode != 0:
                logger.warning(f"joern exited with code {proc.returncode}: {proc.stderr[:500]}")
                return {}

            # Parse JSON array from stdout — find the first '[' to skip Joern banner lines
            stdout = proc.stdout
            bracket_idx = stdout.find('[')
            if bracket_idx == -1:
                logger.warning("joern output did not contain JSON array")
                return {}

            entries = json.loads(stdout[bracket_idx:])
            for entry in entries:
                full_name = entry.get("full_name", "")
                if not full_name:
                    continue
                result[full_name] = {
                    "cfg_summary": entry.get("cfg_summary", "") if extract_cfg else "",
                    "data_flow_vars": entry.get("data_flow_vars", []) if extract_pdg else [],
                }

        except subprocess.TimeoutExpired:
            logger.warning("joern timed out after 120s; skipping CFG/PDG extraction")
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse joern JSON output: {e}")
        except Exception as e:
            logger.warning(f"CFG/PDG extraction error: {e}")
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

        return result

    def close(self):
        """Close Weaviate connection."""
        if self.client:
            self.client.close()


def _migrate_from_shared(project_name: str, named_vectors: bool = False) -> int:
    """Migrate objects from shared collections to per-project collections."""
    try:
        client = weaviate.connect_to_custom(
            http_host='localhost', http_port=8081, http_secure=False,
            grpc_host='localhost', grpc_port=50052, grpc_secure=False
        )
    except Exception as e:
        print(f"❌ Failed to connect to Weaviate: {e}", file=sys.stderr)
        return 1

    try:
        # Create per-project collections first
        analyzer = CodeGraphAnalyzer(project_name, named_vectors=named_vectors)
        analyzer.client = client
        analyzer.create_collections(force=False)

        for base in CODE_GRAPH_BASES:
            shared_name = base
            target_name = _collection_name(base, project_name)

            if not client.collections.exists(shared_name):
                print(f"⚠️  Shared collection {shared_name} does not exist, skipping")
                continue

            shared_coll = client.collections.get(shared_name)
            target_coll = client.collections.get(target_name)

            # Fetch all objects filtered by project
            count = 0
            for obj in shared_coll.iterator(
                return_properties=True,
            ):
                props = obj.properties
                if props.get("project") != project_name and props.get("source_project") != project_name:
                    continue

                # Insert into per-project collection (without references for now)
                try:
                    target_coll.data.insert(
                        properties=props,
                        vector=obj.vector.get("default") if obj.vector else None,
                    )
                    count += 1
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        logger.warning(f"Failed to migrate object: {e}")

            print(f"✅ Migrated {count} objects from {shared_name} → {target_name}")

        print(f"\n✅ Migration complete for project '{project_name}'")
        print("   Note: Cross-collection references (imports, calls, extends, etc.) are NOT migrated.")
        print("   Re-run full analysis to rebuild references in the new collections.")
        return 0
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Analyze codebase and extract entities into Weaviate code graph",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument('repo_path', type=Path, help='Path to repository to analyze')
    parser.add_argument('--project', '-p', type=str, help='Project name (default: repo directory name)')
    parser.add_argument('--language', '-l', type=str,
                       choices=['python', 'lua', 'cpp', 'javascript', 'typescript',
                                'go', 'rust', 'java', 'ruby', 'shell', 'csharp', 'proto'],
                       default=None,
                       help='Language to analyze (default: all supported; language inferred from file extensions)')
    parser.add_argument('--incremental', '-i', action='store_true',
                       help='Only analyze changed files (requires git)')
    parser.add_argument('--create-collections', action='store_true',
                       help='Create Weaviate collections before analysis')
    parser.add_argument('--force-recreate', action='store_true',
                       help='Delete and recreate collections (WARNING: deletes all data)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    # Joern CFG/PDG: auto-on when joern is installed (or VCT_JOERN_AVAILABLE=1).
    # Opt out per-run with --no-cfg/--no-pdg, or set VCT_JOERN_AVAILABLE=0.
    _joern_default = (
        os.environ.get("VCT_JOERN_AVAILABLE", "").strip() == "1"
        or shutil.which("joern") is not None
    )
    parser.add_argument('--cfg', dest='cfg', action='store_true', default=_joern_default,
                       help='Extract CFG summaries via Joern (default: on if joern is in PATH)')
    parser.add_argument('--no-cfg', dest='cfg', action='store_false',
                       help='Skip CFG extraction (faster; useful in CI)')
    parser.add_argument('--pdg', dest='pdg', action='store_true', default=_joern_default,
                       help='Extract PDG data-flow variables via Joern (default: on if joern is in PATH)')
    parser.add_argument('--no-pdg', dest='pdg', action='store_false',
                       help='Skip PDG extraction')
    parser.add_argument('--named-vectors', action='store_true',
                       default=True,
                       help='Create collections with named vector support (default: True)')
    parser.add_argument('--migrate-from-shared', action='store_true',
                       help='Migrate objects from shared collections to per-project collections')

    args = parser.parse_args()

    # Validate repo path
    repo_path = args.repo_path.resolve()
    if not repo_path.exists():
        print(f"❌ Repository path does not exist: {repo_path}", file=sys.stderr)
        return 1

    # Determine project name
    project_name = args.project or repo_path.name

    if args.verbose:
        print(f"📂 Repository: {repo_path}")
        print(f"📦 Project: {project_name}")
        print(f"📁 Collections: {_collection_name('Code*', project_name)}")
        print(f"🔄 Incremental: {args.incremental}")
        if args.named_vectors:
            print(f"📐 Named vectors: enabled")
        print()

    # Handle migration from shared collections
    if args.migrate_from_shared:
        return _migrate_from_shared(project_name, args.named_vectors)

    # Create analyzer
    analyzer = CodeGraphAnalyzer(project_name, named_vectors=args.named_vectors)

    # Connect to Weaviate
    if not analyzer.connect():
        return 1

    try:
        # Always ensure collections exist with correct schema (named vectors)
        # before getting references — Weaviate v4 client auto-creates flat-vector
        # collections on .get() if they don't exist, which breaks named vector inserts.
        analyzer.create_collections(force=args.force_recreate)

        # Analyze repository
        print("🔍 Analyzing codebase...")
        stats = analyzer.analyze_repository(
            repo_path,
            language=args.language,
            incremental=args.incremental,
            extract_cfg=args.cfg,
            extract_pdg=args.pdg,
        )

        # Post-processing: create cross-references
        ref_stats = analyzer.create_cross_references()

        # Report results
        print("\n" + "="*60)
        print("✅ Code Graph Analysis Complete")
        print("="*60)
        print(f"📊 Statistics:")
        print(f"   Modules: {stats['modules']}")
        print(f"   Classes: {stats['classes']}")
        print(f"   Functions: {stats['functions']}")
        print(f"   APIs: {stats['apis']}")
        print(f"   Files analyzed: {stats['files_analyzed']}")
        print(f"   Files skipped: {stats['files_skipped']}")
        print(f"   Cross-references: {ref_stats['calls']} calls, {ref_stats['extends']} extends, {ref_stats['imports']} imports")
        print()

        return 0

    finally:
        analyzer.close()


if __name__ == '__main__':
    sys.exit(main())

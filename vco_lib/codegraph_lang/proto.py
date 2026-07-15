# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Protobuf / gRPC extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``:
``CodeGraphAnalyzer._analyze_proto_file`` — only body edits are the mechanical ``self.`` -> ``ctx.`` rename
(``ctx`` IS the analyzer instance) and the analyzer-resident embedding
seams reached via ``ctx.``. Behavior is pinned byte-identically by
``tests/test_codegraph_golden.py``.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from vco_lib.codegraph_entities import (
    CodeEntity,
    FileExtraction,
    KIND_API,
    KIND_CLASS,
    ModuleDescriptor,
)
from vco_lib.codegraph_lang._shared import (
    _extract_balanced_block,
    run_pure_extractor,
)


def extract_proto_file(
    source_text: str, file_path: Path, repo_root: Path, helpers: Any,
) -> FileExtraction:
    """Pure producer: parse a .proto file, RETURN a :class:`FileExtraction`.

    Proto files define cross-language service contracts. Each RPC method is stored
    as a CodeAPI entry (inbound contract), and each message type as a CodeClass.
    """
    content = source_text
    source_lines = content.split('\n')
    loc = len([l for l in source_lines if l.strip() and not l.strip().startswith('//')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    # Strip comments
    content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
    content_clean = re.sub(r'/\*.*?\*/', ' ', content_clean, flags=re.DOTALL)

    # package name
    pkg_match = re.search(r'^package\s+([\w.]+)\s*;', content, re.MULTILINE)
    pkg = pkg_match.group(1) if pkg_match else file_path.stem

    # imports (other .proto files)
    imports = re.findall(r'import\s+["\']([^"\']+)["\']', content)

    # Message types → CodeClass. This pre-pass only COLLECTS the message
    # names (used to build the module summary below); the actual CodeClass
    # store happens in a second `msg_pattern` loop after the module UUID
    # exists. (Pre-P2f this loop also derived line spans + built a throwaway
    # ``insert_params`` dict that was discarded — removed; the second loop
    # re-derives everything it needs.)
    msg_pattern = re.compile(r'^message\s+([\w]+)\s*\{', re.MULTILINE)
    message_names: List[str] = [
        m.group(1) for m in msg_pattern.finditer(content_clean)
    ]

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

    module = ModuleDescriptor(
        path=relative_path, language="Proto", loc=loc, complexity=1.0,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash, imports=imports, module_summary=module_summary,
    )
    entities: List[CodeEntity] = []
    stats: Dict[str, int] = {'modules': 1, 'classes': 0, 'apis': 0}

    # Now insert message classes with proper module UUID
    for m in msg_pattern.finditer(content_clean):
        mname = m.group(1)
        start_line = content_clean[:m.start()].count('\n') + 1
        end_line = _extract_balanced_block(source_lines, start_line)  # V52-O.11.E (was: start_line + 30)
        class_body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
        signature = f"message {mname}"
        # v0.2.82 (G1 task 2): defer the class embed (this proto message CLASS
        # site embedded EAGERLY pre-G1, defeating the guard). Embed text is
        # BYTE-IDENTICAL to the eager form; default-arg capture pins mname/body.
        entities.append(CodeEntity(
            kind=KIND_CLASS, file_path_rel=relative_path,
            name=mname, full_name=f"{pkg}.{mname}",
            body=class_body, signature=signature, doc="",
            start_line=start_line, end_line=end_line,
            project=helpers.project_name,
            extras={"methods": []},
            deferred_embed=(
                lambda mn=mname, cb=class_body:
                helpers.generate_embedding(f"Proto message: {mn}\n{cb[:400]}")
            ),
        ))
        stats['classes'] += 1

    # Insert RPC methods as CodeAPI entries (inbound service contract)
    for entry in rpc_entries:
        endpoint = f"grpc:{pkg}.{entry['service']}/{entry['method']}"
        api_desc = (
            f"gRPC {entry['service']}.{entry['method']} "
            f"({entry['input']}) → ({entry['output']}) [{pkg}]"
        )
        # v0.2.82 (G1 task 2): defer the API embed (SKIP/STAMP on a
        # metadata-only revision bump). Default-arg capture pins api_desc.
        entities.append(CodeEntity(
            kind=KIND_API, file_path_rel=relative_path,
            extras={
                "endpoint": endpoint, "method": "gRPC",
                "api_description": api_desc,
                "parameters": [entry['input']], "returns": entry['output'],
                "project": helpers.project_name, "proxy_target": "",
            },
            deferred_embed=(
                lambda d=api_desc: helpers.generate_embedding(d)
            ),
        ))
        stats['apis'] += 1

    return FileExtraction(
        module=module, entities=entities, interactions=[],
        imports=[], stats=stats,
    )


def analyze_proto_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Thin shim: skip gates analyzer-side, then extract -> write."""
    return run_pure_extractor(
        ctx, file_path, repo_root, extract_proto_file,
        {'modules': 0, 'classes': 0, 'apis': 0},
    )

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Protobuf / gRPC extractor for the code-graph analyzer (P2f stage 2, v0.2.76).

Moved VERBATIM from ``templates/scripts/analyze_code_graph.py``:
``CodeGraphAnalyzer._analyze_proto_file`` — only body edits are the mechanical ``self.`` -> ``ctx.`` rename
(``ctx`` IS the analyzer instance) and the analyzer-resident embedding
seams reached via ``ctx.``. The MOVE was verbatim; behaviour has since
been corrected here (v0.2.92 block-comment scrub), so it is no longer
byte-identical to the analyzer's original.
``tests/test_codegraph_golden.py`` pins what it does TODAY.

v0.2.102 W6-L: definitions come from the tree-sitter layer
(:func:`vco_lib.codegraph_defs.extract_definitions` — the OPTIONAL ``proto``
grammar VCO offers when a project contains ``.proto`` files) when the parser
is installed and the file parses cleanly: messages and enums at any depth
(nested ones as ``<pkg>.<Outer>.<Inner>``), services as classes whose
``methods`` are their rpcs, and every rpc — streaming ones included — as a
``CodeAPI`` row. The regex discovery (:func:`_regex_definitions`: column-0
messages, non-streaming rpcs) is the fallback, unchanged.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from vco_lib.codegraph_defs import extract_definitions, note_path
from vco_lib.codegraph_entities import (
    CodeEntity,
    FileExtraction,
    KIND_CLASS,
    ModuleDescriptor,
)
from vco_lib.codegraph_lang._shared import (
    _extract_balanced_block,
    blank_block_comments_preserving_lines,
    build_api_entity,
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
    loc = len([line for line in source_lines
               if line.strip() and not line.strip().startswith('//')])
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    relative_path = file_path.relative_to(repo_root).as_posix()

    # Strip comments
    content_clean = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
    content_clean = blank_block_comments_preserving_lines(content_clean)

    # package name
    pkg_match = re.search(r'^package\s+([\w.]+)\s*;', content, re.MULTILINE)
    pkg = pkg_match.group(1) if pkg_match else file_path.stem

    # imports (other .proto files)
    imports = re.findall(r'import\s+["\']([^"\']+)["\']', content)

    # Definitions: tree-sitter first (v0.2.102 W6-L — the OPTIONAL ``proto``
    # grammar from tree-sitter-language-pack, offered on demand), the regex
    # discovery below as the fallback (parser absent, or a parse error).
    defs = extract_definitions("proto", content, origin=relative_path)
    note_path("proto", "tree-sitter" if defs is not None else "regex")
    #   class_records: (keyword, name, full_name, start_line, end_line, methods)
    class_records: List[Tuple[str, str, str, int, int, List[str]]] = []
    if defs is not None:
        for c in defs.classes:
            qualified = f"{c.container}.{c.name}" if c.container else c.name
            class_records.append((
                c.keyword, c.name, f"{pkg}.{qualified}", c.start_line, c.end_line,
                defs.methods_of(c.name) if c.keyword == "service" else [],
            ))
        rpc_entries: list = []
        for f in defs.functions:
            if f.keyword != "rpc" or not f.container:
                continue
            types = f.params.split(" -> ") if f.params else []
            if len(types) != 2:
                continue
            rpc_entries.append({'service': f.container, 'method': f.name,
                                'input': types[0], 'output': types[1]})
    else:
        class_records, rpc_entries = _regex_definitions(content_clean, source_lines, pkg)
    message_names: List[str] = [r[1] for r in class_records if r[0] == "message"]

    # Module summary
    summary_parts = [f"Proto: {relative_path} (package {pkg})"]
    if message_names:
        summary_parts.append(f"Messages: {', '.join(message_names[:8])}")
    if rpc_entries:
        # Source order (was a set — its order varied run to run).
        svc_names = list(dict.fromkeys(e['service'] for e in rpc_entries))
        summary_parts.append(f"Services: {', '.join(svc_names)}")
    module_summary = '\n'.join(summary_parts)

    module = ModuleDescriptor(
        path=relative_path, language="Proto", loc=loc, complexity=1.0,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=file_hash, imports=imports, module_summary=module_summary,
    )
    entities: List[CodeEntity] = []
    stats: Dict[str, int] = {'modules': 1, 'classes': 0, 'apis': 0}

    for keyword, mname, full_name, start_line, end_line, methods in class_records:
        class_body = '\n'.join(source_lines[max(0, start_line - 1):end_line])
        signature = f"{keyword} {mname}"
        # v0.2.82 (G1 task 2): defer the class embed (this proto message CLASS
        # site embedded EAGERLY pre-G1, defeating the guard). Embed text is
        # BYTE-IDENTICAL to the eager form; default-arg capture pins mname/body.
        entities.append(CodeEntity(
            kind=KIND_CLASS, file_path_rel=relative_path,
            name=mname, full_name=full_name,
            body=class_body, signature=signature, doc="",
            start_line=start_line, end_line=end_line,
            project=helpers.project_name,
            extras={"methods": methods},
            deferred_embed=(
                lambda kw=keyword, mn=mname, cb=class_body:
                helpers.generate_embedding(f"Proto {kw}: {mn}\n{cb[:400]}")
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
        # v0.2.82 (G1 task 2): the API embed is DEFERRED (SKIP/STAMP on a
        # metadata-only revision bump); the shared builder owns the default-arg
        # capture that pins api_desc.
        # v0.2.92: constructed by the ONE shared builder (see _shared).
        entities.append(build_api_entity(
            file_path_rel=relative_path,
            endpoint=endpoint,
            method="gRPC",
            description=api_desc,
            project=helpers.project_name,
            parameters=[entry['input']],
            returns=entry['output'],
            embed=helpers.generate_embedding,
        ))
        stats['apis'] += 1

    return FileExtraction(
        module=module, entities=entities, interactions=[],
        imports=[], stats=stats,
    )


def _regex_definitions(
    content_clean: str, source_lines: List[str], pkg: str,
) -> Tuple[List[Tuple[str, str, str, int, int, List[str]]], list]:
    """The pre-v0.2.102 regex discovery (the fallback): top-level messages
    (column 0) and the ``rpc`` lines of top-level services."""
    msg_pattern = re.compile(r'^message\s+([\w]+)\s*\{', re.MULTILINE)
    class_records: List[Tuple[str, str, str, int, int, List[str]]] = []
    for m in msg_pattern.finditer(content_clean):
        mname = m.group(1)
        start_line = content_clean[:m.start()].count('\n') + 1
        end_line = _extract_balanced_block(source_lines, start_line, language="proto")  # V52-O.11.E (was: start_line + 30)
        class_records.append(("message", mname, f"{pkg}.{mname}", start_line, end_line, []))

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
    return class_records, rpc_entries


def analyze_proto_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Thin shim: skip gates analyzer-side, then extract -> write."""
    return run_pure_extractor(
        ctx, file_path, repo_root, extract_proto_file,
        {'modules': 0, 'classes': 0, 'apis': 0},
    )

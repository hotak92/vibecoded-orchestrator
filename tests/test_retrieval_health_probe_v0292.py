# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""KG-3 correctness (v0.2.92) — `session-start-retrieval-health.{sh,ps1}`.

THE DEFECT. The hook probed a hardcoded global ``CodeFunction`` class:

    CODE_CLASS = "CodeFunction"

That class belongs to a retired naming generation — code-graph classes have
been per-project (``<prefix>_CodeFunction``) since the multi-project rollout.
Weaviate answers an aggregate over an unknown class with a GraphQL *validation*
error, the pre-fix ``_aggregate_count`` mapped that to ``None``, and ``None``
printed "codegraph not built". So the banner read

    Retrieval: KG 10 nodes, codegraph not built.

on EVERY project on EVERY install using per-project naming — including a field
project with 399 modules / 1477 functions that was demonstrably serving live
distance-scored results at the same moment. A probe that can only ever return
one answer carries no signal, and it masks the real degradation it exists to
catch: a genuinely stale graph printed the identical line.

WHAT THESE TESTS PIN.

1. The probe reports on the class the READERS query, derived from the
   authoritative ``CODE_GRAPH_PROJECT`` projection — and it is read VERBATIM,
   never re-sanitized (KG collection names drop underscores, code-graph class
   names preserve them; deriving one from the other is the bug family that
   mislabelled live collections as orphans).
2. Three distinguishable states. A project with NO graph must still say "not
   built"; a probe that could not RUN must say "unknown", not "not built". The
   pre-fix code collapsed both onto "not built" — an always-healthy probe would
   have been exactly as useless as the always-broken one it replaced.
3. "weaviate down" means BOTH probes failed at the transport level. Pre-fix the
   codegraph probe could never succeed, so a live Weaviate that merely lacked
   the KG class was reported as down.
4. The .ps1 twin carries a BYTE-IDENTICAL payload. The defect was a shared one;
   pinning the bytes is what stops one side being fixed and the other left
   behind.

The fake Weaviate below speaks just enough GraphQL to reproduce the three
outcomes, so no test here needs a live server.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
HEALTH_SH = HOOKS / "session-start-retrieval-health.sh"
HEALTH_PS1 = HOOKS / "session-start-retrieval-health.ps1"

_CLASS_FIELD_RE = re.compile(r"\{\s*Aggregate\s*\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\{")


class _FakeWeaviate(BaseHTTPRequestHandler):
    """Answers ``/v1/graphql`` aggregates from ``server.counts``.

    A class present in ``counts`` returns its object count; anything else gets
    the same GraphQL validation error a real Weaviate emits for an unknown
    field (verified against Weaviate 1.x on the reporting machine).
    """

    def log_message(self, *_args):  # silence the default stderr access log
        return

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            query = json.loads(raw)["query"]
        except Exception:
            query = ""
        match = _CLASS_FIELD_RE.search(query)
        klass = match.group(1) if match else ""
        counts = self.server.counts  # type: ignore[attr-defined]
        if klass in counts:
            body = {
                "data": {"Aggregate": {klass: [{"meta": {"count": counts[klass]}}]}}
            }
        elif klass in getattr(self.server, "empty_classes", ()):  # in schema, 0 objects
            body = {"data": {"Aggregate": {klass: []}}}
        else:
            body = {
                "errors": [
                    {
                        "message": (
                            f'Cannot query field "{klass}" on type '
                            f'"AggregateObjectsObj".'
                        ),
                        "path": None,
                    }
                ]
            }
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Server:
    def __init__(self, counts: dict, empty_classes=()):
        self.httpd = HTTPServer(("127.0.0.1", 0), _FakeWeaviate)
        self.httpd.counts = dict(counts)  # type: ignore[attr-defined]
        self.httpd.empty_classes = set(empty_classes)  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _run_hook(script: Path = None, **env_extra) -> str:
    # Resolved at CALL time (not bound as a default) so the module constant
    # stays the single knob — that is what lets a red-proof harness swap in a
    # pre-fix copy of the hook.
    script = script or HEALTH_SH
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": tempfile.gettempdir(),
    }
    env.update({k: v for k, v in env_extra.items() if v is not None})
    result = subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@unittest.skipUnless(sys.platform != "win32", "bash hook")
class CodeGraphProbeTargetsThePerProjectClass(unittest.TestCase):
    """The probe must ask about MY project's class, not a retired global one."""

    def test_healthy_graph_reports_built(self):
        with _Server({"Proj_KnowledgeGraph": 10, "Proj_CodeFunction": 1477}) as url:
            out = _run_hook(
                WEAVIATE_URL=url,
                KG_COLLECTION="Proj_KnowledgeGraph",
                CODE_GRAPH_PROJECT="Proj",
            )
        self.assertIn("codegraph 1477 functions", out)
        self.assertIn("KG 10 nodes", out)
        self.assertNotIn("not built", out)

    def test_prefix_is_used_verbatim_not_re_sanitized(self):
        """An underscore-carrying prefix must survive untouched.

        ``CODE_GRAPH_PROJECT`` holds the binding-row ``collection_prefix`` —
        the value the analyzer WROTE the classes with. Re-deriving it with the
        KG sanitizer would turn ``VCT_transcrypt`` into ``VCTTranscrypt`` and
        probe a class nobody created.
        """
        with _Server({"VCT_transcrypt_CodeFunction": 1493, "K": 1}) as url:
            out = _run_hook(
                WEAVIATE_URL=url,
                KG_COLLECTION="K",
                CODE_GRAPH_PROJECT="VCT_transcrypt",
            )
        self.assertIn("codegraph 1493 functions", out)

    def test_bare_global_codefunction_is_not_a_fallback(self):
        """A bare ``CodeFunction`` left over from the old generation must NOT
        rescue the report: no reader queries it for a project that has a
        prefix, so counting it would be a false POSITIVE."""
        with _Server({"K": 1, "CodeFunction": 99999}) as url:
            out = _run_hook(
                WEAVIATE_URL=url,
                KG_COLLECTION="K",
                CODE_GRAPH_PROJECT="Proj",
            )
        self.assertIn("codegraph not built", out)
        self.assertIn("Proj_CodeFunction", out)
        self.assertNotIn("99999", out)


@unittest.skipUnless(sys.platform != "win32", "bash hook")
class ThreeDistinguishableStates(unittest.TestCase):
    """built / not built / could-not-check must not collapse into each other."""

    def test_no_graph_still_reports_not_built(self):
        """THE leave-alone case. Fixing the false negative must not turn the
        probe into one that always says healthy."""
        with _Server({"Proj_KnowledgeGraph": 10}) as url:
            out = _run_hook(
                WEAVIATE_URL=url,
                KG_COLLECTION="Proj_KnowledgeGraph",
                CODE_GRAPH_PROJECT="Proj",
            )
        self.assertIn("codegraph not built", out)
        # Self-describing: name the class we looked for.
        self.assertIn("Proj_CodeFunction", out)

    def test_class_in_schema_but_empty_is_empty_not_not_built(self):
        with _Server({"Proj_KnowledgeGraph": 3}, empty_classes={"Proj_CodeFunction"}) as url:
            out = _run_hook(
                WEAVIATE_URL=url,
                KG_COLLECTION="Proj_KnowledgeGraph",
                CODE_GRAPH_PROJECT="Proj",
            )
        self.assertIn("codegraph empty", out)
        self.assertNotIn("not built", out)

    def test_weaviate_unreachable_is_unknown_not_not_built(self):
        out = _run_hook(
            WEAVIATE_URL="http://127.0.0.1:1",
            KG_COLLECTION="Proj_KnowledgeGraph",
            CODE_GRAPH_PROJECT="Proj",
        )
        self.assertIn("unavailable", out)
        self.assertNotIn("not built", out)

    def test_kg_class_missing_does_not_claim_weaviate_is_down(self):
        """Pre-fix both probes returned None here (KG absent + the codegraph
        probe that could never succeed), so a LIVE Weaviate was reported down."""
        with _Server({"SomeOther_CodeFunction": 5}) as url:
            out = _run_hook(
                WEAVIATE_URL=url,
                KG_COLLECTION="Absent_KnowledgeGraph",
                CODE_GRAPH_PROJECT="Proj",
            )
        self.assertNotIn("weaviate down", out)
        self.assertIn("missing", out)
        self.assertIn("not built", out)

    def test_invalid_prefix_is_unknown_not_not_built(self):
        """A prefix that cannot form a class name means we could not check —
        claiming "not built" would be a fabricated fact about the graph."""
        with _Server({"K": 1}) as url:
            out = _run_hook(
                WEAVIATE_URL=url,
                KG_COLLECTION="K",
                CODE_GRAPH_PROJECT="My Cool App",
            )
        self.assertIn("codegraph unknown", out)
        self.assertNotIn("not built", out)

    def test_unset_prefix_says_so(self):
        with _Server({"K": 1}) as url:
            out = _run_hook(WEAVIATE_URL=url, KG_COLLECTION="K")
        self.assertIn("CODE_GRAPH_PROJECT unset", out)

    def test_unset_kg_collection_is_unknown(self):
        with _Server({"Proj_CodeFunction": 7}) as url:
            out = _run_hook(WEAVIATE_URL=url, CODE_GRAPH_PROJECT="Proj")
        self.assertIn("KG unknown", out)
        self.assertIn("codegraph 7 functions", out)

    def test_always_exits_zero_and_never_blocks(self):
        out = _run_hook(WEAVIATE_URL="not-a-url", KG_COLLECTION="K", CODE_GRAPH_PROJECT="P")
        self.assertTrue(out.startswith("Retrieval:"), out)

    def test_disabled_is_silent(self):
        out = _run_hook(VCT_DISABLE_HOOKS="1", WEAVIATE_URL="http://127.0.0.1:1")
        self.assertEqual(out, "")


class SourceContract(unittest.TestCase):
    """Structural pins that survive a future refactor of the payload."""

    def test_no_hardcoded_global_codefunction_class(self):
        body = HEALTH_SH.read_text(encoding="utf-8")
        self.assertNotRegex(
            body,
            r'CODE_CLASS\s*=\s*"CodeFunction"',
            "the retired global class must not be hardcoded as the probe "
            "target again — that is the v0.2.92 defect verbatim.",
        )
        self.assertIn("CODE_GRAPH_PROJECT", body)

    def test_probe_does_not_re_derive_the_prefix(self):
        """No sanitizer call inside the hook: the prefix is READ, not computed.

        A second derivation here would eventually diverge from the analyzer's
        (they already use different rules for KG vs code-graph names).
        """
        body = HEALTH_SH.read_text(encoding="utf-8")
        for forbidden in (
            "sanitize_for_weaviate_class",
            "canonical_class_prefix",
            "sanitize_collection_prefix",
        ):
            self.assertNotIn(
                forbidden,
                body,
                f"{forbidden} must not be applied to CODE_GRAPH_PROJECT — it "
                "already IS the binding-row collection_prefix.",
            )


class ShPs1PayloadParity(unittest.TestCase):
    """MUST MATCH, enforced on the bytes rather than on fingerprints."""

    @staticmethod
    def _sh_payload() -> str:
        text = HEALTH_SH.read_text(encoding="utf-8")
        match = re.search(r"<<'PYEOF'[^\n]*\n(.*?)\nPYEOF\n", text, re.S)
        assert match, "heredoc payload not found in the .sh hook"
        return match.group(1)

    @staticmethod
    def _ps1_payload() -> str:
        text = HEALTH_PS1.read_text(encoding="utf-8-sig")
        match = re.search(r"\$pyCode = @'\n(.*?)\n'@\n", text, re.S)
        assert match, "here-string payload not found in the .ps1 hook"
        return match.group(1)

    def test_payloads_are_byte_identical(self):
        self.assertEqual(
            self._sh_payload(),
            self._ps1_payload(),
            "the .sh and .ps1 Python payloads have drifted. Re-copy the .sh "
            "heredoc body into the .ps1 here-string verbatim — the defect this "
            "hook shipped with lived in BOTH copies.",
        )

    def test_ps1_keeps_its_bom(self):
        self.assertTrue(
            HEALTH_PS1.read_bytes().startswith(b"\xef\xbb\xbf"),
            "PowerShell 5.1 needs the UTF-8 BOM to read non-ASCII correctly.",
        )

    def test_ps1_still_honours_the_disable_flag(self):
        self.assertIn(
            "VCT_DISABLE_HOOKS", HEALTH_PS1.read_text(encoding="utf-8-sig")
        )


if __name__ == "__main__":
    unittest.main()

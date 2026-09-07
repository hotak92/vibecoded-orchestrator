"""Every Ollama embedding request must set `num_ctx` explicitly (v0.2.92 R40).

An UNSET `num_ctx` means Ollama's 2048 default. A longer input is then
truncated to that window and the request still returns **HTTP 200** with a
real-looking vector — the content past the cutoff simply never influenced it,
and nothing downstream can tell. There is no assertion to write against the
symptom, because a truncated embedding is indistinguishable from a healthy one
once it exists. So the guard has to live at the REQUEST SITE.

This is a ratchet: it scans shipped source for `/api/embeddings` POSTs and
fails when one does not set `num_ctx` in its body. Four such sites existed
before v0.2.92 and lost data silently on the paths whose budgets exceeded 2048.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SEARCH_ROOTS = ("vco_lib", "claude_mcp_servers", "templates")


def _embedding_post_sites() -> list[tuple[Path, int, str]]:
    """Every OUTBOUND POST to an Ollama embeddings endpoint, found via AST.

    A text scan is not good enough here and the first draft of this file proved
    it: matching the URL PATH alone also hits (a) VCO's own code-embed service,
    which exposes an Ollama-COMPATIBLE endpoint but owns its backend's window,
    and (b) the ``@app.post`` route DECLARATION of that endpoint — a receiver,
    not a caller. Widening the window to catch the URL variable then matched
    prose in nearby comments. A check that reports sites it has not actually
    identified is noise, and noise gets suppressed.

    So: walk the AST, keep only ``*.post(...)`` / ``bounded_post(...)`` calls
    whose URL argument names an embeddings path, and hand back the unparsed
    call for inspection.

    Two corrections from round 6, both of which had made this scanner miss the
    single most important site in the repo — ``OllamaAdapter.embed`` /
    ``embed_batch``, which nearly every embed in the product flows through:

    1. **The URL is not always argument 0.** The canonical adapter calls
       ``bounded_post(self.session, f"{self.base_url}/api/embed", ...)`` — the
       session is first. Every positional argument is now considered.
    2. **The filter fails CLOSED, not open.** It used to require the literal
       string ``OLLAMA`` in the URL expression, so a URL built from an
       attribute (``self.base_url``) was silently out of scope. A ratchet whose
       matcher is an ALLOW-list of naming conventions stops guarding the moment
       someone names a variable differently — and nothing tells you. The rule
       is inverted: an ``/api/embed*`` POST is in scope unless it names VCO's
       OWN code-embedding service, which speaks an Ollama-compatible dialect
       but owns its backend's window and is therefore not an Ollama call.

    Route DECLARATIONS (``@app.post("/api/embeddings")``) are receivers, not
    callers, and are excluded by skipping decorator expressions.
    """
    sites: list[tuple[Path, int, str]] = []
    for root in SEARCH_ROOTS:
        for py in sorted((REPO_ROOT / root).rglob("*.py")):
            if "/.venv/" in str(py) or "site-packages" in str(py):
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            decorators = set()
            for n in ast.walk(tree):
                for dec in getattr(n, "decorator_list", []):
                    for sub in ast.walk(dec):
                        decorators.add(id(sub))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or id(node) in decorators:
                    continue
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (
                    fn.id if isinstance(fn, ast.Name) else "")
                if not name.endswith("post"):
                    continue
                url = next(
                    (u for u in (ast.unparse(a) for a in node.args)
                     if "/api/embed" in u),
                    None,
                )
                if url is None:
                    continue
                if "CODE_EMBED" in url.upper():
                    # VCO's own code-embedding service: Ollama-compatible
                    # dialect, its own backend, its own window.
                    continue
                sites.append((py, node.lineno, ast.unparse(node)))
    return sites


class NumCtxIsAlwaysExplicitTests(unittest.TestCase):
    def test_every_ollama_embedding_post_sets_num_ctx(self) -> None:
        offenders = []
        for path, line, window in _embedding_post_sites():
            if "num_ctx" not in window:
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{line}")
        self.assertEqual(
            offenders, [],
            "Ollama embedding request(s) with no `num_ctx` — these inherit the "
            "2048 default and SILENTLY truncate longer input while returning "
            "HTTP 200 (v0.2.92 R40). Set it explicitly at each site:\n  "
            + "\n  ".join(offenders),
        )

    def test_every_ollama_embedding_post_sets_truncate_false(self) -> None:
        """R45's other half, on the same scan (round-5 m-R5-5).

        `num_ctx` alone does not close the hole it was added for. With
        `truncate` unset, an input past the window is still accepted at HTTP
        200 with its tail silently dropped — measured 2026-09-04: qwen3 and
        jina truncate that way, arctic refuses. `truncate: false` makes all
        three refuse uniformly, which is the only outcome a caller can react
        to; the shrink-and-retry path then turns the refusal into a
        leading-window vector that is KNOWN to be one.

        Both halves are asserted at the request site for the same reason: a
        truncated embedding is indistinguishable from a healthy one once it
        exists, so there is no downstream symptom to assert against.
        """
        offenders = []
        for path, line, window in _embedding_post_sites():
            if "truncate" not in window:
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{line}")
        self.assertEqual(
            offenders, [],
            "Ollama embedding request(s) with no `truncate` — an over-window "
            "input is then accepted at HTTP 200 with its tail dropped and "
            "nothing downstream can tell (v0.2.92 R45). Send "
            "`\"truncate\": False` at each site:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_scanner_actually_finds_sites(self) -> None:
        """Anti-vacuity: a scan that finds nothing would pass the test above."""
        self.assertGreaterEqual(
            len(_embedding_post_sites()), 3,
            "the scanner found almost no /api/embeddings sites — it is probably "
            "broken, and a broken scanner makes the guard above vacuously green",
        )

    def test_the_scanner_covers_the_canonical_adapter(self) -> None:
        """Anti-vacuity, sharpened (round-6 MAJOR-R6-2).

        Counting sites is not enough: for two releases this scanner found
        seven sites and green-lit them all while never once visiting
        ``OllamaAdapter.embed`` / ``embed_batch`` — the path nearly every
        embed in the product actually takes. It was invisible for two
        independent reasons (URL in argument 1, no literal ``OLLAMA`` in the
        expression), and a count-based anti-vacuity check cannot see that.

        So name the site that matters and require it by NAME.
        """
        adapter_sites = [
            (path, line) for path, line, _ in _embedding_post_sites()
            if path.name == "ollama.py" and "embedding_providers" in str(path)
        ]
        self.assertGreaterEqual(
            len(adapter_sites), 2,
            "the canonical OllamaAdapter's embed + embed_batch POSTs must be "
            "in scope; if they are not, this whole gate is decorative for the "
            "path that carries the traffic",
        )


if __name__ == "__main__":
    unittest.main()

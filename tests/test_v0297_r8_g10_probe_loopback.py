# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 review R8 G10 — the install-time hub probe and the loopback rule.

* ``hub_ensure.probe_hub_health`` opens through the one probe opener
  (``service_probe_http.open_probe``): a stray process on the port
  ``hub.port`` names answering ``302`` is not a healthy hub, and the redirect
  is never followed.
* A loopback probe never goes through a proxy exported in the environment.
* "Loopback" is ONE literal rule in Python, the Rust rule's twin:
  ``localhost`` in any case (RFC 6761), 127/8, ``::1``; a name that merely
  resolves to loopback is not.

Every server here is a test-owned responder on an ephemeral 127.0.0.1 port;
the "proxy" is the discard port 9, which nothing answers.
"""
from __future__ import annotations

import http.server
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib import hub_ensure
from vco_lib.service_probe_http import is_loopback_host, is_loopback_url

REPO_ROOT = Path(__file__).resolve().parent.parent


def _serve(handler_body) -> tuple[int, http.server.HTTPServer]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            handler_body(self)

        def log_message(self, format, *args):  # noqa: A002 - http.server API
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1], server


@pytest.fixture
def healthy_elsewhere():
    hits: list[str] = []

    def ok(h):
        hits.append(h.path)
        h.send_response(200)
        h.send_header("Content-Length", "2")
        h.end_headers()
        h.wfile.write(b"ok")

    port, server = _serve(ok)
    yield port, hits
    server.shutdown()


def test_a_redirecting_port_is_not_a_healthy_hub(tmp_path, healthy_elsewhere):
    target_port, hits = healthy_elsewhere

    def redirect(h):
        h.send_response(302)
        h.send_header("Location", f"http://127.0.0.1:{target_port}/api/v1/health")
        h.send_header("Content-Length", "0")
        h.end_headers()

    port, server = _serve(redirect)
    try:
        (tmp_path / "hub.port").write_text(f"{port}\n", encoding="utf-8")
        assert hub_ensure.probe_hub_health(timeout=2.0, vct_root=tmp_path) is False
        assert hits == [], "the probe followed the redirect"
    finally:
        server.shutdown()


def test_a_hub_that_answers_is_healthy(tmp_path, healthy_elsewhere):
    port, hits = healthy_elsewhere
    (tmp_path / "hub.port").write_text(f"{port}\n", encoding="utf-8")
    assert hub_ensure.probe_hub_health(timeout=2.0, vct_root=tmp_path) is True
    assert hits == ["/api/v1/health"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "LocalHost"])
def test_a_loopback_probe_ignores_an_exported_proxy(healthy_elsewhere, host):
    """The proxy environment is read when the opener is built (at import), so
    the probe runs in a child that starts with a dead proxy exported."""
    port, hits = healthy_elsewhere
    env = child_env()
    for key in ("NO_PROXY", "no_proxy"):
        env.pop(key, None)
    for key in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        env[key] = "http://127.0.0.1:9"
    code = ("import sys; from vco_lib.service_probe_http import open_probe; "
            "print(open_probe(sys.argv[1], 5).status)")
    proc = subprocess.run([sys.executable, "-c", code, f"http://{host}:{port}/probe"],
                          env=env, capture_output=True, text=True, timeout=30, cwd=str(REPO_ROOT))
    assert proc.returncode == 0 and proc.stdout.strip() == "200", proc.stderr
    assert hits == ["/probe"]


#: `service_endpoints::is_loopback_host`'s own Rust test table
#: (`is_loopback_host_is_the_whole_loopback_range`) — the two rules must agree.
RUST_YES = ["localhost", "LocalHost", "127.0.0.1", "127.0.0.2", "::1", "[::1]", "", " 127.1.2.3 "]
RUST_NO = ["192.168.1.5", "10.0.0.1", "localhost.example.com", "gpu-box", "::2", "0.0.0.0"]


def test_the_loopback_host_rule_matches_the_rust_rule():
    for host in RUST_YES:
        assert is_loopback_host(host), repr(host)
    for host in RUST_NO + ["localhost.localdomain", "::ffff:127.0.0.1", "::1%lo"]:
        assert not is_loopback_host(host), repr(host)


def test_the_loopback_url_rule_matches_the_rust_rule():
    # `loopback_http::tests::loopback_urls`, plus the any-case `localhost`.
    for url in ["http://127.0.0.1:7700/api/v1", "http://localhost:8081", "http://LOCALHOST:8081",
                "http://[::1]:11434/api/tags", "http://127.0.0.2:1/"]:
        assert is_loopback_url(url), url
    for url in ["http://gpu-box:11434", "http://192.168.1.5:8081", "not a url",
                "http://localhost.example.com/", "http://localhost.localdomain:8081", "http:///x"]:
        assert not is_loopback_url(url), url

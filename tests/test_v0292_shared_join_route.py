# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92: the mount-prefix + route-path join has ONE home.

``python._py_join_route`` and ``csharp._csharp_join_route`` were byte-equivalent
apart from a whitespace strip the C# copy applied to the method template. Both
were replaced by :func:`vco_lib.codegraph_lang._shared.join_route`, with the C#
producer normalising its own regex capture at the call site.

The extraction claims to be a PURE refactor, so this module pins that claim the
only way that is worth anything: it keeps verbatim copies of the two ORIGINAL
implementations and asserts the new arrangement reproduces them across an input
matrix — including the whitespace cases that were the two copies' only
divergence.

Red-proof: reverting :func:`join_route` to the C#-flavoured body (strip the
``path`` argument inside the join) fails ``test_python_arrangement_matches_...``
on the padded-path rows; re-adding a private copy to either producer fails
``test_producers_have_no_private_join_copy``.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.codegraph_lang import csharp as csharp_mod  # noqa: E402
from vco_lib.codegraph_lang import python as python_mod  # noqa: E402
from vco_lib.codegraph_lang._shared import join_route  # noqa: E402


# ─── verbatim pre-extraction implementations (do NOT "clean up") ─────────────

def _legacy_py_join_route(prefix: str, path: str) -> str:
    """``python._py_join_route`` as it stood before the extraction."""
    p = (prefix or "").strip().rstrip("/")
    if p and not p.startswith("/"):
        p = "/" + p
    if not path:
        return p or "/"
    if not path.startswith("/"):
        path = "/" + path
    return p + path


def _legacy_csharp_join_route(controller_route: str, method_route: str) -> str:
    """``csharp._csharp_join_route`` as it stood before the extraction."""
    base = (controller_route or "").strip().rstrip("/")
    if base and not base.startswith("/"):
        base = "/" + base
    path = (method_route or "").strip()
    if not path:
        return base or "/"
    if not path.startswith("/"):
        path = "/" + path
    return base + path


_PREFIXES = [
    "", "/", "/v1", "v1", "/v1/", "api/v2", "  /v1  ", "///", "/a/b/",
]
_PATHS = [
    "", "/", "users", "/users", "/users/", "  /users  ", "   ",
    "/users/{id}", "all",
]


def test_shared_join_matches_legacy_python_for_every_input():
    """The shared join IS the python implementation, argument for argument."""
    for prefix in _PREFIXES:
        for path in _PATHS:
            assert join_route(prefix, path) == _legacy_py_join_route(prefix, path), (
                f"python parity broke at prefix={prefix!r} path={path!r}"
            )


def test_csharp_arrangement_matches_legacy_csharp_for_every_input():
    """shared join + the C# call-site strip IS the old C# implementation.

    ``(route or "").strip()`` is the exact normalisation csharp.py now applies
    before calling the shared helper, so this pins the whole arrangement rather
    than only the extracted half.
    """
    for ctrl in _PREFIXES:
        for route in _PATHS:
            arranged = join_route(ctrl, (route or "").strip())
            assert arranged == _legacy_csharp_join_route(ctrl, route), (
                f"csharp parity broke at ctrl={ctrl!r} route={route!r}"
            )


def test_the_two_legacy_copies_diverge_only_on_padded_paths():
    """Pin (not a regression guard): the copies really were equivalent except
    for the template strip — which is WHY sharing them is safe, and which rows
    of the matrix above are load-bearing."""
    divergent = [
        (prefix, path)
        for prefix in _PREFIXES
        for path in _PATHS
        if _legacy_py_join_route(prefix, path)
        != _legacy_csharp_join_route(prefix, path)
    ]
    assert divergent, "matrix no longer covers the strip divergence"
    assert all(p != p.strip() for _, p in divergent), (
        f"copies diverge on a NON-whitespace input: {divergent}"
    )


def test_join_route_behaviour_contract():
    """The documented seam semantics, pinned directly."""
    # leading slash is always produced
    assert join_route("v1", "users") == "/v1/users"
    # trailing slash on the path is meaningful (Flask) and preserved
    assert join_route("/v1", "/users/") == "/v1/users/"
    # a trailing slash on the PREFIX is a seam artefact and is dropped
    assert join_route("/v1/", "/users") == "/v1/users"
    # empty path yields the bare prefix (FastAPI's prefix + "" == prefix)
    assert join_route("/v1", "") == "/v1"
    # empty everything is the root, never ""
    assert join_route("", "") == "/"
    assert join_route(None, None) == "/"  # type: ignore[arg-type]
    # no prefix still yields a rooted endpoint (the v0.2.92 C# fix)
    assert join_route("", "all") == "/all"


def test_producers_have_no_private_join_copy():
    """Neither producer may re-grow a private join (extract-before-duplicate)."""
    for mod in (python_mod, csharp_mod):
        names = [n for n in dir(mod) if "join_route" in n]
        assert names == ["join_route"], (
            f"{mod.__name__} exposes {names}; the only join_route it may carry "
            f"is the one imported from _shared"
        )
        assert mod.join_route is join_route, (
            f"{mod.__name__}.join_route is not the shared helper"
        )


def test_csharp_call_site_normalises_its_own_capture():
    """The strip that used to live inside the C# join must still happen — at
    the call site now. Without it the C# producer would stop matching its
    legacy behaviour on a padded ``[HttpGet(" all ")]`` template.

    A SOURCE pin (the behavioural sibling below is the one that matters); kept
    because it names the exact expression a future editor must not drop.
    """
    src = inspect.getsource(csharp_mod)
    assert 'join_route(ctrl_route, (route or "").strip())' in src, (
        "csharp.py no longer normalises the route capture before joining"
    )


class _CsHelpers:
    """The whole surface the C# producer touches on the analyzer."""

    project_name = "JoinRouteProj"

    def embed_class(self, *_a, **_k):
        return None

    def embed_function(self, *_a, **_k):
        return None

    def generate_embedding(self, *_a, **_k):
        return None


_PADDED_TEMPLATE_CONTROLLER = '''namespace Shop
{
    [Route("api/orders")]
    public class OrdersController
    {
        [HttpGet("  all  ")]
        public List<Order> GetAll()
        {
            return new List<Order>();
        }
    }
}
'''


def test_csharp_producer_still_normalises_a_padded_template(tmp_path):
    """End-to-end: a whitespace-padded ASP.NET template still yields the
    stripped endpoint the pre-extraction C# join produced.

    This is the behavioural half of the call-site pin — drop the ``.strip()``
    in csharp.py and the producer emits ``/api/orders/  all  ``.
    """
    target = tmp_path / "OrdersController.cs"
    target.write_text(_PADDED_TEMPLATE_CONTROLLER, encoding="utf-8")
    fx = csharp_mod.extract_csharp_file(
        _PADDED_TEMPLATE_CONTROLLER, target, tmp_path, _CsHelpers(),
    )
    endpoints = [
        e.extras["endpoint"] for e in fx.entities if e.extras.get("endpoint")
    ]
    assert endpoints == ["/api/orders/all"], endpoints

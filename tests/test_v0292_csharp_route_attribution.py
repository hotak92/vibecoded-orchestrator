# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — the C# producer's ASP.NET route extraction: two silent defects.

WHY THIS EXISTS
---------------
``tests/test_codegraph_golden.py`` already covers a C# controller with a
``[HttpGet("all")]`` and a ``[HttpPost("add")]``, and it passed throughout.
Both defects below survived it because the golden fixture is the ONE shape
that happens to be immune: its two actions are separated by a blank line AND
both carry an explicit template AND the controller carries a ``[Route]``.

DEFECT 1 — the route attribute was located by a 5-line LOOKBACK WINDOW, which
is a proximity heuristic standing in for structural attribution. It failed in
both directions at once for adjacent actions: the window ENDED before the
method's own attribute (``start_line`` is derived from a regex whose leading
group starts matching at the whitespace after the PREVIOUS token, so for a
method that follows a sibling it lands on the sibling's closing brace) while
still REACHING BACK far enough to see the sibling's attribute, which
``re.search`` returned as the first match. Live effect, reproduced below: a
``[HttpPost]`` action directly under a ``[HttpGet("all")]`` one was extracted
as ``GET all`` — the same ``"<endpoint>:<method>"`` dedup identity as its
neighbour, so the two rows COLLAPSED to one and the POST endpoint was lost
outright.

DEFECT 2 — with no controller-level ``[Route]``, the endpoint was stored as
``"all"``: no leading slash, unlike every other producer (python's
``_shared.join_route`` guarantees one, javascript stores Fastify ``url`` values
which have one, and C# itself produced one whenever a ``[Route]`` existed).

WHAT IS PINNED
--------------
* the ACT: adjacent annotated actions yield one row EACH, with their OWN verb
  and template, and distinct dedup identities;
* the LEAVE-ALONE: an undecorated method — and a method that merely FOLLOWS a
  decorated one — yields ZERO rows. A fabricated endpoint is worse than a
  missing one, which is the rule the python producer already states;
* the attribute shapes handled, and that the shapes deliberately NOT guessed
  at emit nothing rather than something wrong;
* the leading-slash contract, with and without a controller ``[Route]``.

LATER IN THE SAME RELEASE (WP-5) — three expectations in this file moved, and
the note is here so a reader does not read them as drift. The producer used to
FABRICATE a path segment from the method name when an action carried no
template (``[HttpPost]`` under ``[Route("api/items")]`` was stored as
``/api/items/add``, a route the service does not serve). That default is gone:
the endpoint is now the controller route, or the application root when there
is none. It could not be removed when this file was written, because the
fabricated segment was the only thing keeping two no-template actions that
share a verb from colliding on one dedup identity — the occurrence
disambiguation now covers that, verified end to end in
``tests/test_v0292_wp5_csharp_route_default.py``. Everything this file is
actually about — which attribute binds to which declaration — is unchanged.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Tuple

from vco_lib.codegraph_entities import KIND_API, KIND_FUNCTION
from vco_lib.codegraph_lang.csharp import extract_csharp_file

_PROJECT = "CsProj"


class _Helpers:
    """The whole surface the C# producer touches on the analyzer."""

    project_name = _PROJECT

    def embed_class(self, *_a: Any, **_k: Any) -> None:
        return None

    def embed_function(self, *_a: Any, **_k: Any) -> None:
        return None

    def generate_embedding(self, *_a: Any, **_k: Any) -> None:
        return None


def _extract(tmp_path: Path, source: str, name: str = "Controller.cs"):
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    return extract_csharp_file(source, target, tmp_path, _Helpers())


def _routes(fx) -> List[Tuple[str, str]]:
    """``(method, endpoint)`` per emitted CodeAPI row, in emission order."""
    return [
        (e.extras["method"], e.extras["endpoint"])
        for e in fx.entities
        if e.kind == KIND_API
    ]


def _identities(fx) -> List[str]:
    return [e.identity_key() for e in fx.entities if e.kind == KIND_API]


def _functions(fx) -> List[str]:
    return [e.name for e in fx.entities if e.kind == KIND_FUNCTION]


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT 1 — the act: an attribute binds to the method it decorates
# ═══════════════════════════════════════════════════════════════════════════
_ADJACENT = '''namespace Shop
{
    [Route("api/orders")]
    public class OrdersController
    {
        [HttpGet("all")]
        public List<Order> GetAll()
        {
            return new List<Order>();
        }
        [HttpPost("add")]
        public Order Create(Order o)
        {
            return o;
        }
    }
}
'''


def test_adjacent_actions_keep_their_own_verb_and_template(tmp_path) -> None:
    """A PIN, not a red-proof — this shape was already correct.

    Worth stating why, because it is what made the defect so quiet: when EVERY
    verb attribute carries a parenthesised template, the ``(`` stops the method
    regex's return-type group from swallowing the attribute, the match then
    begins on the attribute's own line, and the 5-line window happens to end on
    it. The golden fixture is exactly this shape. Change any of that and the
    window silently reads the neighbour's attribute — see the next test.
    """
    fx = _extract(tmp_path, _ADJACENT)
    assert _routes(fx) == [
        ("GET", "/api/orders/all"),
        ("POST", "/api/orders/add"),
    ]


_ADJACENT_NO_TEMPLATE = '''namespace Shop
{
    public class OrdersController
    {
        [HttpGet("all")]
        public List<Order> GetAll()
        {
            return new List<Order>();
        }
        [HttpPost]
        public Order Create(Order o)
        {
            return o;
        }
    }
}
'''


def test_bare_verb_attribute_below_a_templated_one(tmp_path) -> None:
    """THE ACT, and the exact reported repro: a bare ``[HttpPost]`` two lines
    below a ``[HttpGet("all")]``.

    A verb attribute with no parentheses (``[HttpPost]``) IS swallowed by the
    method regex's return-type group — ``[``, ``]`` and word characters are all
    in its character class — so the match begins back on the previous member's
    closing brace and the window looks one member too far up. Pre-fix this
    yielded ``[('GET', 'all'), ('GET', 'all')]``.

    WP-5 amendment: the POST's endpoint is now ``/`` rather than ``/create``.
    This controller declares no ``[Route]``, and the bare ``[HttpPost]``
    carries no template, so there is no path to build from — ASP.NET combines
    two empty templates to the application root. ``/create`` was a segment
    invented from the method name, and inventing it is the defect WP-5 closed
    (see ``tests/test_v0292_wp5_csharp_route_default.py``). What THIS test is
    about is unaffected: the two actions still keep their own VERB and their
    own template, and still hold two distinct identities.
    """
    fx = _extract(tmp_path, _ADJACENT_NO_TEMPLATE)
    assert _routes(fx) == [("GET", "/all"), ("POST", "/")]


def test_adjacent_actions_do_not_collapse_to_one_row(tmp_path) -> None:
    """The data-loss half, stated as the thing that actually lost data: pre-fix
    both rows carried the identity ``all:GET``, and two rows with one identity
    dedup to ONE stored row — the POST endpoint never reached Weaviate."""
    fx = _extract(tmp_path, _ADJACENT_NO_TEMPLATE)
    ids = _identities(fx)
    assert len(ids) == 2
    assert len(set(ids)) == 2, f"identities collapsed: {ids}"


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT 1 — the leave-alone: no attribute means no row
# ═══════════════════════════════════════════════════════════════════════════
def test_undecorated_method_yields_no_api_row(tmp_path) -> None:
    """THE LEAVE-ALONE."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class Helper
    {
        public int Add(int a, int b)
        {
            return a + b;
        }
    }
}
''')
    assert _routes(fx) == []
    assert "Add" in _functions(fx), "the function row itself is unaffected"


def test_method_following_a_decorated_one_is_not_decorated_by_it(tmp_path) -> None:
    """The sibling-leak direction of defect 1, isolated: the SECOND method has
    no attribute of its own and must produce nothing. Pre-fix it inherited the
    first method's ``[HttpGet("all")]``."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class OrdersController
    {
        [HttpGet("all")]
        public List<Order> GetAll()
        {
            return new List<Order>();
        }
        public Order Normalise(Order o)
        {
            return o;
        }
    }
}
''')
    assert _routes(fx) == [("GET", "/all")]


def test_controller_route_attribute_is_not_a_verb(tmp_path) -> None:
    """A class-level ``[Route]`` decorates the CLASS. It must never make the
    first method in the body look annotated."""
    fx = _extract(tmp_path, '''namespace Shop
{
    [Route("api/orders")]
    public class OrdersController
    {
        public Order Normalise(Order o)
        {
            return o;
        }
    }
}
''')
    assert _routes(fx) == []


def test_attribute_on_a_preceding_member_does_not_leak(tmp_path) -> None:
    """An attribute that decorates a PROPERTY stops at that property; the
    method below it carries only its own."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class OrdersController
    {
        [Required]
        public string Name { get; set; }

        [HttpGet("all")]
        public List<Order> GetAll()
        {
            return new List<Order>();
        }
    }
}
''')
    assert _routes(fx) == [("GET", "/all")]


# ═══════════════════════════════════════════════════════════════════════════
# Attribute SHAPES that are handled
# ═══════════════════════════════════════════════════════════════════════════
def test_stacked_attributes_on_separate_lines(tmp_path) -> None:
    fx = _extract(tmp_path, '''namespace Shop
{
    public class OrdersController
    {
        [Authorize]
        [Produces("application/json")]
        [HttpPost("add")]
        public Order Create(Order o)
        {
            return o;
        }
    }
}
''')
    assert _routes(fx) == [("POST", "/add")]


def test_attribute_on_the_same_line_as_the_declaration(tmp_path) -> None:
    fx = _extract(tmp_path, '''namespace Shop
{
    public class OrdersController
    {
        [HttpGet("all")] public List<Order> GetAll() { return null; }
    }
}
''')
    assert _routes(fx) == [("GET", "/all")]


def test_shared_bracket_verb_and_route(tmp_path) -> None:
    """``[HttpGet, Route("all")]`` — two attributes in one bracket. The verb
    carries no template, so the sibling ``Route`` in the same block supplies
    it, which is how ASP.NET resolves it too."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class OrdersController
    {
        [HttpGet, Route("all")]
        public List<Order> GetAll()
        {
            return new List<Order>();
        }
    }
}
''')
    assert _routes(fx) == [("GET", "/all")]


def test_separate_method_level_route_attribute(tmp_path) -> None:
    """``[HttpGet]`` + ``[Route("all")]`` as two attributes, with a controller
    prefix. The method's own ``[Route]`` must be its TEMPLATE and must not
    also be read as the controller prefix and joined to itself."""
    fx = _extract(tmp_path, '''namespace Shop
{
    [Route("api/orders")]
    public class OrdersController
    {
        [HttpGet]
        [Route("all")]
        public List<Order> GetAll()
        {
            return new List<Order>();
        }
    }
}
''')
    assert _routes(fx) == [("GET", "/api/orders/all")]


def test_clr_attribute_suffix_is_the_same_attribute(tmp_path) -> None:
    """``[HttpGetAttribute]`` is what ``[HttpGet]`` abbreviates."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class OrdersController
    {
        [HttpGetAttribute("all")]
        public List<Order> GetAll()
        {
            return new List<Order>();
        }
    }
}
''')
    assert _routes(fx) == [("GET", "/all")]


def test_two_verb_attributes_on_one_action_yield_two_rows(tmp_path) -> None:
    """An action may declare more than one verb. Emitting a single row for it
    loses an endpoint the same way the lookback bug did."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class OrdersController
    {
        [HttpGet("item")]
        [HttpHead("item")]
        public Order Fetch()
        {
            return null;
        }
    }
}
''')
    assert _routes(fx) == [("GET", "/item"), ("HEAD", "/item")]
    assert len(set(_identities(fx))) == 2


def test_route_token_replacement_brackets_are_balanced_not_terminal(tmp_path) -> None:
    """``[Route("api/[controller]")]`` nests brackets INSIDE a string. The
    attribute block must be matched by depth, not by the first ``[``."""
    fx = _extract(tmp_path, '''namespace Shop
{
    [Route("api/[controller]")]
    public class OrdersController
    {
        [HttpGet("all")]
        public List<Order> GetAll()
        {
            return new List<Order>();
        }
    }
}
''')
    assert _routes(fx) == [("GET", "/api/[controller]/all")]


# ═══════════════════════════════════════════════════════════════════════════
# Shapes deliberately NOT guessed at
# ═══════════════════════════════════════════════════════════════════════════
def test_non_literal_template_is_not_invented(tmp_path) -> None:
    """A template that is not a string literal (``RouteNames.All``) yields NO
    template — and, since WP-5, no invented segment either. The point of the
    test is the negative: the action must never pick up the literal from the
    NEIGHBOURING ``[HttpPost("add")]`` attribute. It does not; it falls to the
    empty-template default, which with no controller ``[Route]`` is ``/``.

    Pre-WP-5 the expectation here was ``/getall`` — a path assembled from the
    method name, i.e. exactly the fabrication this file's own module docstring
    calls "worse than a missing one"."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class OrdersController
    {
        [HttpGet(RouteNames.All)]
        public List<Order> GetAll()
        {
            return new List<Order>();
        }
        [HttpPost("add")]
        public Order Create(Order o)
        {
            return o;
        }
    }
}
''')
    assert _routes(fx) == [("GET", "/"), ("POST", "/add")]


def test_verb_named_inside_a_route_template_is_not_a_verb(tmp_path) -> None:
    """``Http…`` appearing inside a template STRING is not an attribute."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class OrdersController
    {
        [Route("api/HttpGetThings")]
        public List<Order> Listing()
        {
            return new List<Order>();
        }
    }
}
''')
    assert _routes(fx) == []


def test_unbalanced_attribute_bracket_emits_nothing(tmp_path) -> None:
    """Cannot attribute it -> record nothing, rather than guess."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class OrdersController
    {
        [HttpGet("all"
        public List<Order> GetAll()
        {
            return new List<Order>();
        }
    }
}
''')
    assert _routes(fx) == []


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT 2 — the leading slash
# ═══════════════════════════════════════════════════════════════════════════
def test_endpoint_without_controller_route_has_a_leading_slash(tmp_path) -> None:
    """Pre-fix this stored ``"all"``. Every other producer stores ``/all``."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class PingController
    {
        [HttpGet("all")]
        public string All()
        {
            return "ok";
        }
    }
}
''')
    assert _routes(fx) == [("GET", "/all")]


def test_endpoint_with_controller_route_is_unchanged(tmp_path) -> None:
    """The shape the golden fixture pins: joining a controller prefix already
    produced a leading slash and must keep producing the identical string."""
    fx = _extract(tmp_path, '''namespace Shop
{
    [Route("api/items")]
    public class ItemsController
    {
        [HttpGet("all")]
        public string All()
        {
            return "ok";
        }
    }
}
''')
    assert _routes(fx) == [("GET", "/api/items/all")]


def test_no_template_and_no_controller_route_yields_the_root(tmp_path) -> None:
    """RENAMED AND RE-POINTED by WP-5, because the old name
    (``test_default_method_name_route_has_a_leading_slash``) promised a
    behaviour that is now a defect rather than a contract.

    This shape used to store ``/ping`` — a segment built from the method name,
    for a route the service does not serve. The endpoint is now what ASP.NET's
    own template combination gives an empty controller template plus an empty
    action template: the application root. The leading-slash contract that the
    original test existed for still holds, and is asserted below and in
    ``test_every_emitted_endpoint_starts_with_a_slash``.

    Full coverage of the change, including the identity collision its removal
    exposes, lives in ``tests/test_v0292_wp5_csharp_route_default.py``.
    """
    fx = _extract(tmp_path, '''namespace Shop
{
    public class PingController
    {
        [HttpPost]
        public string Ping()
        {
            return "ok";
        }
    }
}
''')
    assert _routes(fx) == [("POST", "/")]
    assert _routes(fx)[0][1].startswith("/")


def test_every_emitted_endpoint_starts_with_a_slash(tmp_path) -> None:
    """The contract itself, over every shape in one file."""
    fx = _extract(tmp_path, '''namespace Shop
{
    public class MixedController
    {
        [HttpGet("all")]
        public string All() { return "ok"; }
        [HttpPost]
        public string Add() { return "ok"; }
        [HttpDelete("/rooted")]
        public string Drop() { return "ok"; }
    }
}
''')
    endpoints = [ep for _m, ep in _routes(fx)]
    assert endpoints, "expected some routes"
    assert all(ep.startswith("/") for ep in endpoints), endpoints
    assert "//rooted" not in endpoints, "an already-rooted template must not double up"

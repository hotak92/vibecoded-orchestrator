# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P1e (v0.2.75): the codegraph symbol-extractor rejects garbage queries.

The extractor used to:
  * match env-assignments (``LEAN_CTX_OFF=1`` via the snake_case rule),
  * match non-code paths (``/tmp/x.log`` via the dotted rule),
  * match grep regex/glob fragments,
  * and — worst — fall back to the WHOLE COMMAND TEXT when nothing matched,
    issuing garbage codegraph queries for e.g. ``git diff <sha>..HEAD``.

The fix rejects those word shapes and returns EMPTY when no discrete symbol
is isolable; callers then skip injection entirely.

v0.2.101 Wave 2 RETARGET: this corpus originally drove the shell
``_lib/codegraph-query.sh::codegraph_extract_symbol`` (and its ``.ps1``
sibling ``Get-VcoCodegraphSymbol``). Those shell copies were RETIRED with
their last legacy callers (the pre-bash/pre-edit router rewires + the
pre-tool-use branch removal); the ONE home is now
``vco_lib/inject_intent.py::extract_symbol``, which this file drives
directly. The corpus is unchanged — every P1e row still pins the same
rejection/acceptance behaviour against the surviving implementation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib.inject_intent import extract_symbol  # noqa: E402

# (command_text, expected_symbol). Empty string = no injection.
CASES = [
    # env-prefixed command → the real file symbol, NOT the env assignment.
    ("LEAN_CTX_OFF=1 python3 install.py --update", "install.py"),
    # grep with a regex pattern → the file, not the pattern.
    ("grep -nE 'def .*foo' server.py", "server.py"),
    # log tail → no code symbol → no injection.
    ("tail -f /var/log/app.log", ""),
    ("cat /tmp/output.log", ""),
    # git diff sha..HEAD → no symbol (the sha-range is not code).
    ("git diff abc123..HEAD", ""),
    # env-assignment only → no symbol.
    ("LEAN_CTX_OFF=1", ""),
    # a real call-shape survives (grep for a function call; the quoted
    # `symbol(` word keeps `(` — deliberately NOT a rejected metachar).
    ("grep 'migrate_collections(' server.py", "migrate_collections("),
    # snake_case identifier survives.
    ("grep resolve_test_penalty code_ranking.py", "resolve_test_penalty"),
    # CamelCase identifier survives.
    ("rg OrderManager", "OrderManager"),
    # source path with dir survives (real source file).
    ("cat vco_lib/embedding_service.py", "vco_lib/embedding_service.py"),
    # non-code path with dir → skip.
    ("cat config/settings.yaml", ""),
    # a URL → skip.
    ("curl https://example.com/foo", ""),
    # redirect token → skip; but a real symbol later wins.
    ("run_thing 2> errors_out", "run_thing"),
]


@pytest.mark.parametrize("text,expected", CASES)
def test_extract_symbol_one_home(text, expected):
    assert extract_symbol(text) == expected, (
        f"cmd={text!r} → expected {expected!r}"
    )


def test_cap_200_survives_the_retarget() -> None:
    long = "a_" + "b" * 300
    assert len(extract_symbol(long)) <= 200


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

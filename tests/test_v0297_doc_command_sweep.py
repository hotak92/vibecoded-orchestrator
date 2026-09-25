# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""F5 (v0.2.97 review round 7): every `python -m vco_lib.service_endpoints …`
command printed in command position under `docs/` parses against the REAL
parser.

The deferral argparse sweep (`tests/test_deferral_command_argparse_sweep.py`)
covers ledger entries only — docs are unswept, which is how
`docs/post-install/CONTAINER-RECOVERY.md` shipped a recovery step
(`service_endpoints plan`, no `--shell`/`--json`) the CLI rejects with
``error: one of the arguments --shell --json is required``.

Scope, deliberately narrow:

* COMMAND POSITION only — a line whose stripped text STARTS with
  ``python -m vco_lib.service_endpoints`` (the copy-pasteable shape used in
  fenced blocks). Inline prose mentions with alternatives
  (``adopt|use-vco-copy|move``) or placeholders (``--port <free-port>``) are
  templates, not runnable commands, and are out of scope.
* An inline trailing ``# comment`` is stripped before tokenising (docs
  annotate commands that way, e.g. GETTING_STARTED.md).
* The whole line is then tokenised and fed to
  ``vco_lib.service_endpoints._build_arg_parser`` — the same contract the
  argv-contract sweep uses, never a hand-kept flag list. Required groups and
  subcommand choices are enforced because ``parse_args`` (not
  ``parse_known_args``) runs.

Nothing is executed: parser construction only.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vco_lib import service_endpoints as se  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS = _REPO_ROOT / "docs"

#: The module whose CLI docs print in command position. One module for now —
#: the F5 family; extend only with an importable parser builder.
_MODULE_PREFIX = "vco_lib.service_endpoints"


def _doc_command_lines() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in sorted(_DOCS.rglob("*.md")):
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith(f"python -m {_MODULE_PREFIX}"):
                out.append((f"{path.relative_to(_REPO_ROOT)}:{line}", line))
    return out


def test_the_sweep_finds_commands_to_check() -> None:
    assert _doc_command_lines(), "the sweep went blind: no docs command lines matched"


@pytest.mark.parametrize(
    ("where", "line"),
    _doc_command_lines(),
    ids=[where for where, _line in _doc_command_lines()],
)
def test_documented_service_endpoints_commands_parse(where: str, line: str) -> None:
    # Strip a trailing inline comment (docs annotate commands with ` # …`).
    body = line.split(" #", 1)[0]
    argv = shlex.split(body)[3:]  # drop `python -m vco_lib.service_endpoints`
    try:
        se._build_arg_parser().parse_args(argv)
    except SystemExit as exc:  # argparse errors exit(2), never raise ValueError
        pytest.fail(f"{where} does not parse against the real CLI: {line!r} ({exc})")


def test_the_sweep_rejects_the_f5_shape() -> None:
    """The exact F5 bug must stay detectable: `plan` with no format flag."""
    with pytest.raises(SystemExit):
        se._build_arg_parser().parse_args(["plan"])

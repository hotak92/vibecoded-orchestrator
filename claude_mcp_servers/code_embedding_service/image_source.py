# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE definition of "what source is this container image built from".

Why this module exists
----------------------
``code_embed`` is the only VCO service that ships as an image **built from
this checkout** (``infrastructure/docker-compose.yml::code_embed.build``),
and ``compose up`` builds an image only when it is MISSING — a changed build
context is not a rebuild trigger.  So a source fix (v0.2.92's over-window
REFUSAL instead of a silent truncation) could be correct in ``server.py``,
green in CI, and still absent from every existing install's running service.
It was: on the maintainer's machine the image dated 2026-05-16 while the
container had been ``--force-recreate``d as recently as 2026-07-12.

The remedy has two halves and both need one shared, falsifiable answer to
"is the running service built from the current source?":

* the **service** reports :func:`source_sha` of its OWN ``/app`` directory on
  ``/health`` (it hashes the files it is actually running, so it cannot
  overstate its freshness);
* the **host** (``vco_lib.code_embed_image``) computes :func:`source_sha` of
  the checkout's build context and compares.

Both sides call THIS function, and this file is itself COPYed into the image,
so there is no mirror to drift: one rule, one home (CLAUDE.md A>B>C, rule A).
That is also why the module has no imports beyond ``hashlib``/``pathlib`` —
it must be importable inside a minimal image that contains neither
``vco_lib`` nor ``claude_mcp_servers``.

Covered surface (deliberately exact, so the guarantee is not overstated):
the files the Dockerfiles ``COPY`` into the image.  A change to a Dockerfile
itself (e.g. a base-image bump) is NOT covered — the Dockerfile is not
present in the image, so the two sides could not hash the same bytes, and an
asymmetric hash is worse than an honest gap.  Both Dockerfiles must therefore
keep their ``COPY`` set in step with :data:`IMAGE_SOURCE_FILES`; the parity
test ``tests/test_v0292_code_embed_image_rebuild.py`` fails if they drift.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

#: Bumped when the HASHING RULE changes (not when the hashed files change).
#: Part of the digest, so an image built under an older rule can never
#: accidentally compare equal to a host running a newer one.
IMAGE_SOURCE_SCHEME = "vco-code-embed-source-v1"

#: Every file the Dockerfiles COPY into the image, in a fixed order.
#: MUST match the ``COPY`` lines in BOTH ``Dockerfile`` and
#: ``Dockerfile.cuda`` (pinned by a test).
IMAGE_SOURCE_FILES = ("requirements.txt", "image_source.py", "server.py")


def source_sha(directory) -> Optional[str]:
    """sha256 over :data:`IMAGE_SOURCE_FILES` in ``directory``.

    Returns ``None`` when ANY of the files is missing or unreadable — a
    digest that silently skipped a file would compare unequal forever (an
    endless rebuild) or, worse, equal across a real change.  ``None`` means
    "could not look", which every caller renders as *unknown*, never as
    *current* (positive evidence only).

    The name and byte-length of each file are folded in before its bytes so
    that moving content between two of them cannot produce the same digest.
    """
    base = Path(directory)
    digest = hashlib.sha256()
    digest.update(IMAGE_SOURCE_SCHEME.encode("utf-8"))
    digest.update(b"\0")
    for name in IMAGE_SOURCE_FILES:
        path = base / name
        try:
            payload = path.read_bytes()
        except OSError:
            return None
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
    return digest.hexdigest()

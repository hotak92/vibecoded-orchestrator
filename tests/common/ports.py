# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Free loopback ports for tests — the ONE home (v0.2.94).

Five files had grown their own copy of "bind to :0, read the port, close it"
(``_free_port`` in ``test_migrate_shared_kg_schema``,
``test_migrate_development_temporal_props``, ``test_v0292_code_embed_hook_build``;
``_pick_free_port`` in ``test_vct_access_check_clients``; ``_free_block`` /
``_free_port`` in ``test_v0294_gateway_port_chain``), and a sixth was about to
be added. Every copy answers the same question with the same three lines and
the same caveat, which is exactly the shape CLAUDE.md's extract-before-
duplicate rule exists to stop.

**The caveat, stated once here instead of five times**: a port proved free is
free at the moment of the probe, not at the moment of the bind. Nothing
short of holding the socket can close that window, and holding it would
defeat the purpose — the caller wants to bind it. In practice a test suite is
the only thing racing for the high ephemeral range on a developer machine, so
the probe is enough; a test that cannot tolerate even that should bind ``:0``
itself and read the port back from the live socket.
"""
from __future__ import annotations

import socket
import unittest

__all__ = ["free_port", "free_block"]

#: Where :func:`free_block` looks for a contiguous run. Above the ephemeral
#: ranges a developer machine hands out by default and clear of every port
#: this repo documents, so a discovered block is very unlikely to collide
#: with a real service.
_BLOCK_SEARCH = range(12000, 13000, 16)


def free_port() -> int:
    """One free loopback port: bind ``:0``, read what the kernel gave, close."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def free_block(size: int) -> range:
    """A CONTIGUOUS run of ``size`` free loopback ports, each proved free.

    Contiguity is what a fallback-range test needs: the code under test walks
    a ``range``, so the injected one must be a range of ports that are all
    bindable. ``:0`` cannot answer that — it hands out whatever is free, not
    a run — so the block is searched for and each candidate is really bound.

    Raises ``unittest.SkipTest`` when the machine has no such run, which is a
    fact about the machine rather than a failure of the code under test.
    """
    for base in _BLOCK_SEARCH:
        held = []
        try:
            for offset in range(size):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(("127.0.0.1", base + offset))
                held.append(sock)
        except OSError:
            continue
        finally:
            for sock in held:
                sock.close()
        if len(held) == size:
            return range(base, base + size)
    raise unittest.SkipTest("no contiguous free port block on this machine")

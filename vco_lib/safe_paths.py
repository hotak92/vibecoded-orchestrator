# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Paths that come from a request, contained before any file operation.

The one home for "a path named by someone else may only reach files under
these roots". Stdlib-only: the RL reranker image COPIES this file (see
``claude_mcp_servers/rl_reranker/Dockerfile``).

Two shapes, both checked at the point they return:

* :func:`contained_path` — the path RESOLVED (``os.path.realpath``: ``..`` and
  symlinks gone) and then required to start with an allowed root's own
  resolved prefix. The value returned is the resolved one that passed the
  check, so the string that reaches ``open``/``mkdir``/``copy`` is exactly
  the string that was checked. For a path that may not exist yet (a
  directory to create, a log file to append to).
* :func:`locate_existing` — for a file that must already exist and is about
  to be READ (a checkpoint ``torch.load`` deserializes): after the
  containment check, the path is rebuilt by walking the allowed root's
  directory listing one component at a time. The result is assembled from
  directory entries, never from the request's bytes, so only a file that
  really exists under the root can be named — and a static analyser can see
  that no request data reaches the read.
"""
from __future__ import annotations

import os
from typing import Iterable, Optional, Union

__all__ = ["contained_path", "existing_entry", "locate_existing"]

PathArg = Union[str, "os.PathLike[str]"]


def _root_prefix(root: PathArg) -> str:
    """``root`` resolved, with exactly one trailing separator — so that
    ``/data/state-x`` never passes as inside ``/data/state``."""
    real = os.path.realpath(os.fspath(root))
    return real if real.endswith(os.sep) else real + os.sep


def contained_path(candidate: PathArg, roots: Iterable[PathArg]) -> Optional[str]:
    """``realpath(candidate)`` when it lies strictly inside one of ``roots``;
    ``None`` otherwise (a root itself is not "inside" it). Never raises."""
    try:
        resolved = os.path.realpath(os.fspath(candidate))
    except (TypeError, ValueError, OSError):
        return None
    for root in roots:
        if resolved.startswith(_root_prefix(root)):
            return resolved
    return None


def existing_entry(parent: PathArg, name: str) -> Optional[str]:
    """The path of ``parent``'s directory entry whose name is exactly
    ``name``, built from the listing (``os.DirEntry.path``); ``None`` when
    there is no such entry or ``parent`` cannot be listed. ``name`` is only
    ever COMPARED, so a separator or ``..`` in it simply matches nothing."""
    try:
        with os.scandir(os.fspath(parent)) as entries:
            for entry in entries:
                if entry.name == name:
                    return entry.path
    except OSError:
        return None
    return None


def locate_existing(candidate: PathArg, roots: Iterable[PathArg]) -> Optional[str]:
    """The existing filesystem entry ``candidate`` names, when it resolves
    strictly inside one of ``roots``: returned as a path assembled from the
    root's directory listing (see the module docstring). ``None`` when it is
    outside every root or does not exist."""
    root_list = list(roots)
    resolved = contained_path(candidate, root_list)
    if resolved is None:
        return None
    for root in root_list:
        prefix = _root_prefix(root)
        if not resolved.startswith(prefix):
            continue
        current = prefix[:-1] or os.sep
        for part in resolved[len(prefix):].split(os.sep):
            found = existing_entry(current, part)
            if found is None:
                return None
            current = found
        return current
    return None

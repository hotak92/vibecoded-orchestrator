# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE home for loading the code graph's tree-sitter grammars (v0.2.102 W6-E).

Two consumers parse source with tree-sitter:

* :mod:`vco_lib.codegraph_calls` — CALL-name extraction for the ``calls`` edge
  (since v0.2.77);
* :mod:`vco_lib.codegraph_defs` — the DEFINITIONS layer (classes, functions,
  methods, line ranges) the per-language extractors in
  :mod:`vco_lib.codegraph_lang` use first (since v0.2.102).

Both load grammars through :func:`load_language` here, so the grammar table,
the per-process cache and the once-per-language "unavailable" log line exist
exactly once.

The code graph is AST-based for all fourteen languages it indexes: Python
through ``ast``, the other thirteen through the tree-sitter grammars below
(Svelte through its ``<script>`` blocks), each with the regex extractors as
the fallback when its grammar cannot load or a file does not parse cleanly.

Default and optional grammars
-----------------------------
* **Default** — the ``codegraph-ts`` extra (``pyproject.toml``): Python, Rust,
  JavaScript, TypeScript/TSX, Go, Java, C#, C/C++, Ruby, Lua, Bash.
  ``install.py`` installs it on every machine (``_install_codegraph_treesitter``;
  opt out with ``VCT_SKIP_CODEGRAPH_TS=1``).
* **Optional** (:data:`OPTIONAL_GRAMMARS`, v0.2.102 W6-L) — PowerShell
  (``tree-sitter-powershell``) and Protocol Buffers (``proto`` from
  ``tree-sitter-language-pack``). NOT installed by default: VCO detects which
  languages each registered project contains and the launcher OFFERS the
  missing parsers in a modal (:mod:`vco_lib.codegraph_parsers`). Missing
  default grammars (an opted-out or failed install) are offered the same way.

The language pack (``proto``)
-----------------------------
``tree-sitter-language-pack`` 1.x ships no parser binaries: it downloads ONE
whole-platform bundle (~22 MB, sha256 from a per-release manifest) from
GitHub Releases the first time a language is requested. VCO never lets that
happen during a code-graph walk: the parser INSTALL pre-fetches the grammar
(:func:`download_pack_grammars`, run by the installer helper right after pip),
and :func:`load_language` only loads a pack grammar that is ALREADY on disk —
an absent one is "unavailable" like any missing wheel, never a download. The
cache lives inside VCO's venv (:func:`pack_cache_base`), so it is per machine,
identical for every process that runs VCO's interpreter whatever its
environment, and removed with the venv.

Svelte's own template grammar is not used: Svelte ``<script>`` blocks are
parsed with the TypeScript grammar by :mod:`vco_lib.codegraph_defs`.

Soft-fail contract
------------------
A grammar that cannot be imported, instantiated or found on disk makes
:func:`load_language` return ``None`` (logged once per language per process)
and each consumer keeps its pre-tree-sitter behaviour — no call edges for that
language, regex definitions. This is the one sanctioned exception to
"loud-fail over silent degrade", because the grammars are optional by design;
the LOUD half is the offer: a missing grammar a project needs is surfaced to
the user by the launcher, and a failed install is reported there.
"""
from __future__ import annotations

import importlib
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

#: canonical grammar id -> (grammar module, factory attribute). The ids are
#: the analyzer's canonical language ids, plus ``tsx`` (TypeScript + JSX —
#: ``.tsx`` files need the separate factory; ``.jsx`` parses with the
#: JavaScript grammar, which includes JSX).
#:
#: Every module here is pinned in a ``pyproject.toml`` ``codegraph-ts*`` extra
#: (the default ``codegraph-ts`` or an optional ``codegraph-ts-<parser>``) —
#: ``tests/test_codegraph_defs.py`` pins the parity.
GRAMMARS: Dict[str, Tuple[str, str]] = {
    "python": ("tree_sitter_python", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "csharp": ("tree_sitter_c_sharp", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "ruby": ("tree_sitter_ruby", "language"),
    "lua": ("tree_sitter_lua", "language"),
    "shell": ("tree_sitter_bash", "language"),
    "powershell": ("tree_sitter_powershell", "language"),
}

#: grammar id -> language name inside ``tree-sitter-language-pack`` (the
#: pack's grammars are downloaded at INSTALL time, see the module docstring).
PACK_GRAMMARS: Dict[str, str] = {
    "proto": "proto",
}

#: The pack's import name.
PACK_MODULE = "tree_sitter_language_pack"

#: Grammars that are NOT in the default ``codegraph-ts`` extra.
OPTIONAL_GRAMMARS = frozenset({"powershell", "proto"})

# lang -> loaded ``tree_sitter.Language`` or ``None`` ("tried; unavailable").
# Membership distinguishes "not attempted" from "attempted and failed", so a
# missing grammar is probed and logged exactly once per process.
_LANGUAGES: Dict[str, Optional[Any]] = {}
_LOCK = threading.Lock()


def all_grammar_ids() -> "frozenset[str]":
    """Every grammar id :func:`load_language` knows (default + optional)."""
    return frozenset(GRAMMARS) | frozenset(PACK_GRAMMARS)


def pack_cache_base() -> Path:
    """Base directory of the language pack's parser cache: inside the venv of
    the interpreter running VCO (``<venv>/share/vco-codegraph-parsers``), so
    the installer that downloads and every walk that loads agree on it
    regardless of ``XDG_CACHE_HOME`` / ``HOME`` differences between processes.
    The pack appends ``tree-sitter-language-pack/v<version>/libs`` and refuses
    a group- or world-writable directory (it creates it ``0700``)."""
    return Path(sys.prefix) / "share" / "vco-codegraph-parsers"


def _configured_pack() -> Any:
    """Import the pack and point it at :func:`pack_cache_base`. Raises when
    the pack is not installed (callers decide how loud that is)."""
    pack = importlib.import_module(PACK_MODULE)
    pack.configure(pack.PackConfig(cache_dir=str(pack_cache_base())))
    return pack


def pack_grammar_downloaded(lang: str) -> bool:
    """True when the pack is installed AND ``lang``'s grammar is already in
    :func:`pack_cache_base` (no network). False for a non-pack grammar."""
    name = PACK_GRAMMARS.get(lang)
    if name is None:
        return False
    try:
        return name in set(_configured_pack().downloaded_languages())
    except Exception:  # noqa: BLE001 — absent pack / unreadable cache = not downloaded
        return False


def download_pack_grammars(langs: Iterable[str]) -> None:
    """Fetch the named pack grammars into :func:`pack_cache_base`. INSTALL
    time only (the parser installer calls it right after pip). Raises on any
    failure — ImportError when the pack is missing, the pack's
    ``DownloadError`` / ``ChecksumMismatchError`` on a failed fetch (measured:
    no network fails at once; an unreachable host fails after the pack's own
    ~60 s timeout) — so the installer reports it, never swallows it."""
    names = [PACK_GRAMMARS[g] for g in langs if g in PACK_GRAMMARS]
    if not names:
        return
    _configured_pack().download(names)


def _try_load(lang: str) -> Optional[Any]:
    spec = GRAMMARS.get(lang)
    pack_name = PACK_GRAMMARS.get(lang)
    if spec is None and pack_name is None:
        return None
    try:
        from tree_sitter import Language  # type: ignore

        if pack_name is not None:
            pack = _configured_pack()
            # NEVER download during a walk: an absent grammar is unavailable.
            if pack_name not in set(pack.downloaded_languages()):
                raise LookupError(
                    f"'{pack_name}' is not downloaded into {pack_cache_base()}")
            language = pack.get_language(pack_name)
            return language if isinstance(language, Language) else Language(language)
        assert spec is not None
        mod_name, factory_attr = spec
        grammar_mod = importlib.import_module(mod_name)
        return Language(getattr(grammar_mod, factory_attr)())
    except Exception as exc:  # noqa: BLE001 — soft-fail: ImportError and any
        # grammar-load / ABI error leave the language on its fallback path.
        hint = ("VCO offers this optional parser in the launcher when a project "
                "contains the language" if lang in OPTIONAL_GRAMMARS else
                "Install the optional extra to enable it: pip install '.[codegraph-ts]'")
        logger.info(
            "codegraph_grammars: tree-sitter grammar unavailable for '%s' "
            "(%s: %s); the code graph uses its non-tree-sitter fallback for "
            "this language. %s",
            lang, type(exc).__name__, exc, hint,
        )
        return None


def load_language(lang: str) -> Optional[Any]:
    """Return the cached ``tree_sitter.Language`` for ``lang``, or ``None``
    when the core or the grammar cannot be loaded (logged once)."""
    if lang in _LANGUAGES:
        return _LANGUAGES[lang]
    with _LOCK:
        if lang not in _LANGUAGES:
            _LANGUAGES[lang] = _try_load(lang)
        return _LANGUAGES[lang]


def parse(lang: str, source: bytes) -> Optional[Any]:
    """Parse ``source`` (UTF-8 bytes) with ``lang``'s grammar.

    Returns the ``tree_sitter.Tree``, or ``None`` when the grammar is
    unavailable or the parser raised (logged; the caller falls back).
    """
    language = load_language(lang)
    if language is None:
        return None
    try:
        from tree_sitter import Parser  # type: ignore

        return Parser(language).parse(source)
    except Exception as exc:  # noqa: BLE001 — a parser failure is per-file
        logger.info(
            "codegraph_grammars: tree-sitter parse failed for '%s' (%s: %s)",
            lang, type(exc).__name__, exc,
        )
        return None

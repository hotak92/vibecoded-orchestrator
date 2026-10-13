# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
r"""Credential-SHAPE vocabulary SSOT (one home, explicit precision contexts).

This module is the single source of truth for "does this text have the shape of
a live credential?" — the vocabulary that had forked into five hand-maintained
copies, each drifting independently until the content scanners went blind to
whole vendor families (see :data:`SK_VENDOR_KEY` and the module notes below).

It is DISTINCT from two neighbouring SSOTs; do not merge them:

* ``vco_lib/secret_value_shape.py`` — is a value a single well-formed secret vs
  a multi-secret blob? (LINE STRUCTURE, not vendor shape.)
* ``vco_lib/mcp_scan_rules.toml`` ``[env].secret_shaped_needles`` — is an ENV
  KEY NAME credential-shaped? (``TOKEN`` / ``SECRET`` / ``PAT`` … segments.)

Source of truth (A > B > C)
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Python is the SSOT. Option **A** (shared code, ``python -m``) is NOT reachable
for the consumers: ``templates/hooks/_lib/credscan.sh`` is pure ``grep`` with no
Python at all, and ``tools/vct-secrets/vct`` runs standalone from
``~/.vct-secrets/vct`` in git-credential-helper context with no venv /
PYTHONPATH guarantee. Option **B** (a parsed config table) is equally
unreachable — parsing TOML from those scripts *is* the Python dependency they
cannot take. So this vocabulary uses option **C**: hand-written mirrors, locked
to this module by a behaviour fixture, exactly as
``vco_lib/secret_value_shape.py`` does.

Mirrors (all pinned to ``tests/fixtures/credential_shape_parity.json``):

* ``templates/hooks/_lib/credshapes.sh``   — bash, hooks deployment root.
* ``templates/hooks/_lib/credshapes.ps1``  — PowerShell, same root (Windows
  parity is a hard repo rule).
* ``tools/vct-secrets/lib/credential_shapes.sh`` — bash, the ``vct`` deployment
  root. A SECOND bash mirror is forced by deployment topology, not by
  sloppiness: ``.claude/hooks/_lib/`` and ``~/.vct-secrets/lib/`` are disjoint
  trees, and a relative symlink between them would dangle under the documented
  ``cp -a`` deployment. It carries ONLY the ``argv_name`` projection (the one
  context ``vct`` uses), so it is a projection, not a copy of the hooks mirror.

When you change a pattern here, update the fixture AND every mirror in the same
change.

Precision contexts — the reason the copies diverged, kept deliberately
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The five copies did not merely drift; some of their differences were EARNED,
and flattening them into one regex would reintroduce real false positives. The
error costs genuinely invert between haystacks, so the vocabulary is one table
with a per-context pattern rather than one pattern:

``repo_scan``
    Haystack: a whole repository / build-output tree — INCLUDING vendored
    third-party bundles and compiled binaries. **Precision-biased.** A false
    positive here fires on every commit and trains people to ignore the
    scanner, which is how a real alert gets missed. Two false-positive corpora
    were PROVEN present in this repo's former vendored Excalidraw bundle
    (deleted in v0.2.102; the shape rules it justified are KEPT — a generic
    secret shape stays a shape regardless of which bundle proved it):

      * ``assets/index-*.js`` carries the Slovak locale chunk name
        ``sk-SK-<hash>-<hash>``, which a loose ``sk-`` tail matches.
      * ``assets/subset-shared.chunk-*.js`` embeds a base64 WASM/font payload
        in which ``AKIA`` + 16 upper-alnum occurs BY CHANCE — base64's alphabet
        makes such runs inevitable in a ~1.8 MB blob.

    Corroboration: ``.github/secret_scanning.yml`` already ``paths-ignore``s
    that same directory for GitHub's own secret scanner. Two independent
    systems having to exclude it is why ``aws_access_key_id``, ``jwt`` and
    ``atlassian_token`` are ABSENT from this context.

``content_scan``
    Haystack: ONE text file a user just wrote or edited (mime-filtered, <= 5 MB).
    **Recall-leaning.** The alert is non-blocking and reaches the author of that
    exact file, so a false positive costs one glance; a false negative means a
    credential sits on disk unremarked. Vendored base64 blobs are not the
    typical input here, so the shapes ``repo_scan`` must skip are safe.

``command_scan``
    Haystack: ONE shell command line. **Maximum recall — the loosest context.**
    Error costs are the most asymmetric of the four: a false positive only
    forfeits output compression for one command, while a false negative routes a
    credential-bearing command through a wrapper that is not credential-aware —
    which has already produced real auth failures with VALID tokens in the
    field. Hence ``{8,}`` tails where other contexts demand exact lengths: a
    truncated-looking token still means "credential handling in progress".

``argv_name``
    Haystack: ONE complete, hand-typed identifier (a ``--key`` argument).
    **Whole-string anchored** (``^…$``) — a name is one token, not a line to be
    searched — which is what lets it be recall-biased on the ``sk-`` family
    without inheriting ``repo_scan``'s locale-asset problem: the vendored
    haystack cannot occur in an argv position. Prefix families keep EXACT
    lengths so a plausible human name like ``github_pat_personal`` is not
    refused.

Choosing a context is DELIBERATE: :func:`patterns_for_context` takes it as a
required argument and raises on an unknown value. There is no default, because
every default would silently hand some caller the wrong bias.

Regex portability
~~~~~~~~~~~~~~~~~

Every pattern must be valid in ALL of: POSIX ERE (``grep -E``), Python ``re``,
and .NET (``-match`` in PowerShell). That is the intersection, so:

* Use ``(…)`` groups — ``(?:…)`` is not ERE.
* Do NOT use POSIX classes like ``[[:space:]]`` — Python and .NET read those as
  a literal character set, not a class.
* ``\s`` IS used (in the generic-assignment shapes only) because it works in
  GNU ``grep -E``, Python and .NET alike, and because those patterns already
  shipped that way — a consolidation must not silently change matching
  semantics. Note it is a GNU extension, so BSD/macOS ``grep -E`` may differ;
  that is a PRE-EXISTING condition inherited verbatim, not introduced here.

Security
~~~~~~~~

Nothing in this module prints, logs or returns a candidate value. Predicates
return labels and booleans only.
"""

from __future__ import annotations

import re
from typing import Dict, Mapping, Optional, Tuple

__all__ = [
    "CONTEXTS",
    "CredentialShape",
    "SHAPES",
    "pattern_for",
    "patterns_for_context",
    "combined_pattern",
    "matching_labels",
    "is_credential_shaped_name",
    "TEST_PLACEHOLDER_MARKER",
    "FILE_KINDS",
    "ENV_CONFIG_EXACT_NAMES",
    "ENV_CONFIG_NAME_PREFIXES",
    "ENV_CONFIG_NAME_INFIXES",
    "ENV_CONFIG_NAME_SUFFIXES",
    "YAML_NAME_SUFFIXES",
    "BACKTICK_CODE_NAME_SUFFIXES",
    "file_kind",
    "patterns_for_file",
]

#: The ONE explicit opt-out a content scanner honours (owner ruling
#: 2026-10-12): a line carrying this text -- normally inside a comment, e.g.
#: ``"ghp_..." // vco-secret-scan: test-placeholder`` -- is a declared test
#: placeholder and is not reported. There is deliberately NO heuristic on how
#: a value looks ("a real password could be 123456789"): only this marker, on
#: the SAME line as the value, silences it. Mirrored as
#: ``CSHAPES_TEST_PLACEHOLDER_MARKER`` in ``_lib/credshapes.sh`` and
#: ``Get-CredShapesTestPlaceholderMarker`` in ``_lib/credshapes.ps1``; the
#: parity test pins both to this value.
TEST_PLACEHOLDER_MARKER = "vco-secret-scan: test-placeholder"

#: File kinds for the content scanners (owner ruling 2026-10-13, W6-V review
#: F2, refined in re-review 2): the kind is decided by the file's NAME, never
#: by how a value looks.
#:
#: * ``env_config`` -- an unquoted value is a literal: .env / .env.* / *.env.*,
#:   .ini / .cnf, .properties, .conf / .cfg, shell scripts and rc files,
#:   Dockerfile / Containerfile (``ENV``), .pypirc and .npmrc (ini syntax).
#: * ``yaml`` -- the same, with YAML's own value syntax (``null``, ``~``, the
#:   boolean words, ``*alias``, flow / template braces) silent: its own
#:   unquoted shape.
#: * ``backtick_code`` -- a language with backtick STRING literals (JS / TS and
#:   their component formats, Go raw strings); the only kind where a backtick
#:   literal is a value. In shell / env files a backtick is command
#:   substitution and in prose (markdown, rst) inline-code syntax.
#: * ``code`` -- everything else: an unquoted value is an expression.
FILE_KINDS: Tuple[str, ...] = ("env_config", "yaml", "backtick_code", "code")

#: The ONE file-kind table. Matched against the LOWER-CASED basename, in this
#: order: env_config (exact name, prefix, infix, suffix), yaml, backtick_code,
#: else code. Mirrored as data in ``_lib/credshapes.sh`` / ``.ps1`` (they
#: classify a path with a basename ``case``, no Python needed) and pinned by
#: ``tests/test_credential_shapes_parity.py`` -- both the literal lists and a
#: behaviour table in the parity fixture.
#:
#: ``.npmrc`` is env_config: npm config is ini syntax and its credential keys
#: (``_password``, ``//host/:_password``) hold literal values; a
#: ``${NPM_TOKEN}`` reference stays silent like any ``$`` reference.
ENV_CONFIG_EXACT_NAMES: Tuple[str, ...] = (
    ".env", "env", ".envrc", "dockerfile", "containerfile",
    ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".zshenv", ".profile",
    ".pypirc", ".npmrc",
)
ENV_CONFIG_NAME_PREFIXES: Tuple[str, ...] = ("dockerfile.", "containerfile.")
ENV_CONFIG_NAME_INFIXES: Tuple[str, ...] = (".env.",)
ENV_CONFIG_NAME_SUFFIXES: Tuple[str, ...] = (
    ".env", ".ini", ".cnf", ".properties", ".conf", ".cfg",
    ".sh", ".bash", ".zsh", ".dockerfile",
)
YAML_NAME_SUFFIXES: Tuple[str, ...] = (".yaml", ".yml")
BACKTICK_CODE_NAME_SUFFIXES: Tuple[str, ...] = (
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".vue", ".svelte", ".go",
)


def file_kind(path: str) -> str:
    """The :data:`FILE_KINDS` entry for ``path``, from its basename only."""
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if (name in ENV_CONFIG_EXACT_NAMES
            or name.startswith(ENV_CONFIG_NAME_PREFIXES)
            or any(i in name for i in ENV_CONFIG_NAME_INFIXES)
            or name.endswith(ENV_CONFIG_NAME_SUFFIXES)):
        return "env_config"
    if name.endswith(YAML_NAME_SUFFIXES):
        return "yaml"
    if name.endswith(BACKTICK_CODE_NAME_SUFFIXES):
        return "backtick_code"
    return "code"


#: The four haystacks, each with its own documented bias. A caller MUST name one.
CONTEXTS: Tuple[str, ...] = ("repo_scan", "content_scan", "command_scan", "argv_name")


class CredentialShape:
    """One credential family, with the regex to use in each context it serves.

    ``patterns`` maps a context name to an ERE-compatible pattern. A context
    ABSENT from the mapping means this shape deliberately does not participate
    in that context (see the module docstring for why ``aws_access_key_id`` is
    absent from ``repo_scan``). Absence is a design statement, not an omission.

    ``file_kinds`` (``None`` = every kind) restricts the shape to files of
    those :data:`FILE_KINDS` when a scanner knows the path
    (:func:`patterns_for_file`).
    """

    __slots__ = ("id", "label", "patterns", "note", "file_kinds")

    def __init__(
        self, id: str, label: str, patterns: Mapping[str, str], note: str = "",
        file_kinds: Optional[Tuple[str, ...]] = None,
    ) -> None:
        self.id = id
        self.label = label
        self.patterns: Dict[str, str] = dict(patterns)
        self.note = note
        self.file_kinds = file_kinds
        for ctx in self.patterns:
            if ctx not in CONTEXTS:
                raise ValueError(f"{id}: unknown context {ctx!r}")
        for kind in file_kinds or ():
            if kind not in FILE_KINDS:
                raise ValueError(f"{id}: unknown file kind {kind!r}")
        # The mirrors encode a restriction as ONE kind per shape id.
        if file_kinds is not None and len(file_kinds) != 1:
            raise ValueError(f"{id}: a restricted shape names exactly one kind")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"CredentialShape({self.id!r}, contexts={sorted(self.patterns)})"


#: Key names of the two generic-secret shapes (``content_scan`` only). Each
#: letter is a ``[Xx]`` pair because the three regex engines disagree on case:
#: ``grep -E`` and Python ``re`` are case-sensitive, .NET ``-match`` is not,
#: and there is no inline-flag syntax common to all three. Matched unanchored,
#: so compounds such as ``DB_PASSWORD`` / ``client_secret`` / ``MYSQL_PWD``
#: are covered. PASSWORD / PASSWD / PWD / PASSPHRASE joined the list with the
#: W6-V owner ruling (2026-10-12). The mirrors carry the expanded literal.
_GENERIC_KEY = (
    "("
    "[Ss][Ee][Cc][Rr][Ee][Tt]"
    "|[Aa][Pp][Ii]_[Kk][Ee][Yy]"
    "|[Aa][Cc][Cc][Ee][Ss][Ss]_[Tt][Oo][Kk][Ee][Nn]"
    "|[Pp][Rr][Ii][Vv][Aa][Tt][Ee]_[Kk][Ee][Yy]"
    "|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]"
    "|[Pp][Aa][Ss][Ss][Ww][Dd]"
    "|[Pp][Ww][Dd]"
    "|[Pp][Aa][Ss][Ss][Pp][Hh][Rr][Aa][Ss][Ee]"
    ")"
)

#: In front of the key (re-review 2 #2): the compound key (``DB_PASSWORD``,
#: ``db.password``, ``--password``) must start at line start, after a ``{`` not
#: preceded by ``$``, or after any char that is not ``$``, an identifier char
#: or ``{``. A key that is itself a ``$`` / ``${`` reference (``"$PWD:/app"``,
#: ``PYTHONPATH=$PWD:/x``, ``"${SECRET}"``) is syntax, not a key.
_GENERIC_KEY_BOUNDARY = "(^|[^$A-Za-z0-9_{]|(^|[^$])\\{)[A-Za-z0-9_.-]*"

#: Between the key and a quoted value (W6-V review F1, re-review 2 #4): an
#: optional closing quote (a JSON / dict key), an optional ``?`` (a TS optional
#: member), an optional type annotation, then ``=``, ``:`` or ``:=``. The
#: annotation is either ``: TYPE`` -- with an optional ``&`` and Rust lifetime
#: (``&str``, ``&'static str``), dotted / ``::`` paths and one ``<...>`` or
#: ``[...]`` suffix (``Option<&str>``, ``Optional[str]``) -- or Go's bare
#: ``NAME TYPE =`` form. The key must END right there, so prefix compounds
#: match and suffix compounds (``password_hint``) do not: key matching, not a
#: judgement on the value. ``==`` / ``!=`` never match.
_GENERIC_SEP = (
    "[\"']?\\??\\s*"
    "(:\\s*&?('[A-Za-z_]+\\s+)?[A-Za-z_][A-Za-z0-9_.:]*"
    "(<[A-Za-z0-9_., &'<>]*>|\\[[A-Za-z0-9_., ]*\\])?\\s*"
    "|\\s+[A-Za-z_][A-Za-z0-9_.]*\\s*)?"
    ":?[:=]\\s*"
)

#: A quoted literal of ANY non-empty length (owner ruling 2026-10-12): single,
#: double or triple quotes with an optional string prefix (``r`` ``b`` ``u``
#: ``f``, up to two letters). The only skips are SYNTAX, never the look of a
#: value: a DOUBLE-quoted value opening with ``$`` or ``{`` (shell / template
#: expansion, a format template, an f-string ``f"{x}"``), and an ``f``-prefixed
#: single-quoted value opening with ``{`` (Python ``f'{x}'``). A plain
#: single-quoted ``'$x'`` / ``'{x}'`` is a literal (review F3).
_GENERIC_QUOTED_VALUE = (
    "([rRbBuUfF]{0,2}\"\"\"[^\"${][^\"]*\"\"\""
    "|[rRbBuU]{0,2}'''[^'][^']*'''"
    "|[rRfF]{1,2}'''[^'{][^']*'''"
    "|[rRbBuUfF]{0,2}\"[^\"${][^\"]*\""
    "|[rRbBuU]{0,2}'[^'][^']*'"
    "|[rRfF]{1,2}'[^'{][^']*')"
)

#: A backtick literal without ``${`` interpolation: its own shape, restricted
#: to ``backtick_code`` files (coordinator decision 2026-10-13 + re-review 2
#: #1): elsewhere a backtick is shell command substitution or markdown inline
#: code -- syntax, so silent.
_GENERIC_BACKTICK_VALUE = "`([^`$]|\\$[^`{])+\\$?`"

#: YAML unquoted value: as the env/config arm, plus YAML's own value syntax
#: silent (coordinator decision 2026-10-13 under the owner's rules): ``null``
#: (any case), ``~``, an ``*alias``, flow ``{...}`` / ``[...]`` and
#: ``{{ template }}``. Block-scalar indicators ``|`` / ``>`` (with ``-`` /
#: ``+``) ALERT -- a literal follows on the next line -- and so does an anchored
#: value ``&a hunter2``.
#:
#: YAML booleans are syntax too (W6-V review R2 V-2): ``yes`` / ``no`` /
#: ``true`` / ``false`` / ``on`` / ``off`` are silent, in any letter case like
#: ``null``, when the word ends the value or is followed by a space (so
#: ``false # disabled`` is silent, ``offset`` / ``no-reply`` / ``yesterday``
#: alert). Choice: the YAML 1.1 boolean words that real loaders resolve
#: (PyYAML's 1.1 resolver set) but NOT the single letters ``y`` / ``n`` /
#: ``Y`` / ``N`` -- the 1.1 spec lists them, PyYAML does not resolve them and
#: YAML 1.2 makes them plain strings, so ``password: y`` stays a literal and
#: alerts. Only the ``yaml`` kind: in an env/config file ``PASSWORD=no`` is a
#: literal and alerts. Like ``null``, the words are silent after either
#: separator (``KEY: no`` / ``KEY=no`` inside a YAML file).
#:
#: grep -E has no lookahead, so the words are excluded as a trie of
#: "differs at this letter" arms; the first-character class excludes their
#: initials (n t f y o) and each arm re-admits a value that leaves the trie.
_GENERIC_YAML_VALUE = (
    "([^\"'$`~*{\\[nNtTfFyYoO #]"
    "|[nN]($|[^uUoO])|[nN][oO][^ ]"
    "|[nN][uU]($|[^lL])|[nN][uU][lL]($|[^lL])|[nN][uU][lL][lL][^ ]"
    "|[tT]($|[^rR])|[tT][rR]($|[^uU])|[tT][rR][uU]($|[^eE])|[tT][rR][uU][eE][^ ]"
    "|[fF]($|[^aA])|[fF][aA]($|[^lL])|[fF][aA][lL]($|[^sS])"
    "|[fF][aA][lL][sS]($|[^eE])|[fF][aA][lL][sS][eE][^ ]"
    "|[yY]($|[^eE])|[yY][eE]($|[^sS])|[yY][eE][sS][^ ]"
    "|[oO]($|[^nNfF])|[oO][nN][^ ]|[oO][fF]($|[^fF])|[oO][fF][fF][^ ])"
)

# ---------------------------------------------------------------------------
# The vocabulary. Mirrors reproduce these EXACTLY (id, label and per-context
# pattern), and the parity fixture proves it behaviourally.
# ---------------------------------------------------------------------------

SHAPES: Tuple[CredentialShape, ...] = (
    CredentialShape(
        "github_token",
        "GitHub token",
        {
            # gh[pousr]_ covers classic PAT, OAuth, user-to-server, server-to-
            # server and refresh tokens — all exactly 36 base62 after the
            # prefix. repo_scan previously carried only `ghp_`; widening to the
            # whole family is a strict superset and was verified to add zero
            # matches against this repo's dist text + dist binaries.
            "repo_scan": "gh[pousr]_[A-Za-z0-9]{36}",
            "content_scan": "gh[pousr]_[A-Za-z0-9]{36}",
            "command_scan": "gh[pousr]_[A-Za-z0-9]{8,}",
            "argv_name": "^gh[pousr]_[A-Za-z0-9]{36}$",
        },
    ),
    CredentialShape(
        "github_fine_grained_pat",
        "GitHub fine-grained PAT",
        {
            # The exact format (22 + `_` + 59) rather than a loose
            # `github_pat_[A-Za-z0-9_]{60,}`: the loose form false-positives on
            # Rust release-binary rodata, where rustc fuses static strings into
            # unseparated word runs beginning `github_pat_` (e.g. the command
            # name `get_github_pat_preview` plus its neighbours). It also keeps
            # argv_name from refusing a plausible human name like
            # `github_pat_personal`.
            "repo_scan": "github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}",
            "content_scan": "github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}",
            "command_scan": "github_pat_[A-Za-z0-9_]{8,}",
            "argv_name": "^github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}$",
        },
    ),
    CredentialShape(
        "aws_access_key_id",
        # Label names the ID specifically: this shape matches the access key
        # ID, which is an identifier, NOT the 40-char secret access key. Saying
        # "AWS access key" would overclaim.
        "AWS access key ID (AKIA)",
        {
            "content_scan": "AKIA[0-9A-Z]{16}",
            "command_scan": "AKIA[0-9A-Z]{16}",
            "argv_name": "^AKIA[0-9A-Z]{16}$",
        },
        note=(
            "ABSENT from repo_scan: PROVEN base64 collision (found in the "
            "former vendored Excalidraw bundle, deleted v0.2.102): its "
            "base64 WASM/font payload contains an AKIA+16-upper-alnum run "
            "by chance. Adding it to repo_scan would make "
            "check-no-secrets.sh fail on every commit."
        ),
    ),
    CredentialShape(
        "gitlab_pat",
        "GitLab PAT (glpat-)",
        {
            "repo_scan": "glpat-[A-Za-z0-9_-]{20,}",
            "content_scan": "glpat-[A-Za-z0-9_-]{20,}",
            "command_scan": "glpat-[A-Za-z0-9_-]{8,}",
            "argv_name": "^glpat-[A-Za-z0-9_-]{20,}$",
        },
    ),
    CredentialShape(
        "slack_token",
        "Slack token (xox*)",
        {
            "repo_scan": "xox[bpoas]-[A-Za-z0-9-]{20,}",
            "content_scan": "xox[bpoas]-[A-Za-z0-9-]{20,}",
            "command_scan": "xox[bpoas]-[A-Za-z0-9-]{8,}",
            "argv_name": "^xox[bpoas]-[A-Za-z0-9_-]{20,}$",
        },
    ),
    CredentialShape(
        "atlassian_token",
        "Atlassian API token",
        {
            "content_scan": "ATATT[A-Za-z0-9_=-]{20,}",
            "command_scan": "ATATT[A-Za-z0-9_=-]{8,}",
            "argv_name": "^ATATT[A-Za-z0-9_-]{20,}$",
        },
        note=(
            "ABSENT from repo_scan for the same reason as aws_access_key_id: a "
            "5-char all-uppercase prefix over a base64-ish tail is collision-"
            "prone in vendored binary/base64 haystacks. It scored zero hits "
            "today, but repo_scan is the context where a false positive is "
            "most expensive, so the conservative default holds."
        ),
    ),
    CredentialShape(
        "jwt",
        "JWT / bearer-shaped token",
        {
            "content_scan": "eyJ[A-Za-z0-9_-]{20,}",
            "command_scan": "eyJ[A-Za-z0-9_-]{20,}",
            "argv_name": "^eyJ[A-Za-z0-9_-]{20,}$",
        },
        note=(
            "ABSENT from repo_scan: `eyJ` is simply base64 for '{\"', so it "
            "appears in ANY bundled base64-encoded JSON, not only in JWTs."
        ),
    ),
    CredentialShape(
        "sk_vendor_key",
        # The old label on this shape was "Anthropic/OpenAI API key" while the
        # pattern matched neither modern OpenAI nor OpenRouter — see note.
        "Vendor API key (sk-*: OpenAI / Anthropic / OpenRouter / compatible)",
        {
            # TWO arms, and the FIRST is the one that closes the vendor-blindness:
            #
            #  arm 1 (entropy): `sk-` + up to three short dashed vendor segments
            #    + an UNBROKEN alphanumeric run of >= 32. This stops enumerating
            #    vendors altogether — the discriminator is the long entropy tail
            #    every real key has, which is why it catches sk-or-v1-*,
            #    sk-ant-api03-*, sk-proj-*, sk-svcacct-*, sk-admin-* and bare
            #    legacy sk-* alike, and any future vendor infix for free.
            #  arm 2 (named vendors): preserved verbatim from the previous
            #    repo_scan list so this consolidation is a strict superset —
            #    an OpenAI project key whose tail happens to be dash-broken
            #    below the 32 floor is still caught.
            #
            # Neither arm matches the Slovak locale asset `sk-SK-<8>-<8>`: its
            # longest unbroken run is 8, and `SK` is not a named vendor.
            "repo_scan": (
                "sk-([A-Za-z0-9]+-){0,3}[A-Za-z0-9]{32,}"
                "|sk-(proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"
            ),
            "content_scan": (
                "sk-([A-Za-z0-9]+-){0,3}[A-Za-z0-9]{32,}"
                "|sk-(proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"
            ),
            # Loosest floors, per this context's max-recall bias — still well
            # above the 8-char runs in the locale-asset shape.
            "command_scan": (
                "sk-([A-Za-z0-9]+-){0,3}[A-Za-z0-9]{16,}"
                "|sk-(proj|svcacct|admin)-[A-Za-z0-9_-]{8,}"
            ),
            # Whole-string anchored, so the vendored-asset haystack cannot
            # arise; free to be maximally inclusive across the whole family.
            "argv_name": "^sk-[A-Za-z0-9_-]{20,}$",
        },
        note=(
            "THE LIVE GAP THIS CONSOLIDATION CLOSES. The content scanners used "
            "`sk-(ant-api03|[a-zA-Z0-9]{30,})-[a-zA-Z0-9]` under the label "
            "'Anthropic/OpenAI API key'. The alternation's second arm needs 30+ "
            "alphanumerics BEFORE the next dash, so every short vendor infix "
            "fell through: `or` and `v1` (OpenRouter), and also `proj`, "
            "`svcacct` and `admin` — i.e. EVERY key OpenAI issues today. The "
            "label claimed coverage the pattern did not have, which is worse "
            "than a missing check because it suppresses the question."
        ),
    ),
    CredentialShape(
        "pem_private_key",
        "PEM private key",
        {
            # repo_scan keeps the full `-----` framing (higher precision against
            # binaries). content_scan matches the bare marker, as the hooks
            # always have.
            #
            # `[A-Z0-9 ]*` covers RSA / EC / DSA / OPENSSH / ENCRYPTED / bare.
            # The content scanners previously hard-coded `(RSA |EC |OPENSSH |)`,
            # which silently missed `BEGIN DSA PRIVATE KEY` and `BEGIN
            # ENCRYPTED PRIVATE KEY` while the label said "PEM private key" —
            # a second, smaller instance of the overclaiming-label defect.
            "repo_scan": "-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
            "content_scan": "BEGIN [A-Z0-9 ]*PRIVATE KEY",
        },
        note=(
            "No body-length floor: every BEGIN ... PRIVATE KEY marker is a "
            "finding unless its own line carries TEST_PLACEHOLDER_MARKER "
            "(owner ruling 2026-10-12 removed the v0.2.82 120-char floor; "
            "a secret may be short)."
        ),
    ),
    CredentialShape(
        "generic_secret_quoted",
        "Generic secret",
        {"content_scan": (_GENERIC_KEY_BOUNDARY + _GENERIC_KEY + _GENERIC_SEP
                          + _GENERIC_QUOTED_VALUE)},
        note=(
            "Owner ruling 2026-10-12 (W6-V): a secret may be a password, a "
            "port or an IP, far shorter than an API key, so ANY non-empty "
            "QUOTED literal under a secret-shaped key alerts -- no length "
            "floor (it was 32+ chars of a base64-ish alphabet). The forms are "
            "documented on _GENERIC_SEP (JSON keys, type annotations, `:=`) "
            "and _GENERIC_QUOTED_VALUE (prefixes, triple quotes). "
            "A literal must be closed on the same line. Not literals, so not "
            "matched: an unquoted RHS (`getenv(...)`, `os.environ[...]`, "
            "`$PASS`), a DOUBLE-quoted value or an f-prefixed single-quoted "
            "value that opens with `$` / `{` -- syntax, not a value -- a key "
            "that is itself a `$` reference, and the empty string. Only the "
            "OPENING character is read: `\"pre$X\"` alerts while "
            "`\"${X}suffix\"` does not; telling them apart would need a "
            "judgement on the rest of the value, which the owner rules out. "
            "Nothing about the value itself "
            "is judged (a `<YOUR_...>` placeholder, a single-quoted `'$x'` "
            "alert too): only TEST_PLACEHOLDER_MARKER on the line silences it."
        ),
    ),
    CredentialShape(
        "generic_secret_backtick",
        "Generic secret (backtick literal)",
        {"content_scan": (_GENERIC_KEY_BOUNDARY + _GENERIC_KEY + _GENERIC_SEP
                          + _GENERIC_BACKTICK_VALUE)},
        note=(
            "A JS / TS / Go backtick literal assigned to a secret-shaped key, "
            "any non-empty length, unless it contains `${` (interpolation is "
            "syntax). backtick_code files only: in shell / env files a "
            "backtick is command substitution and in prose inline code."
        ),
        file_kinds=("backtick_code",),
    ),
    CredentialShape(
        "generic_secret_unquoted",
        "Generic secret (unquoted)",
        {"content_scan": (
            _GENERIC_KEY_BOUNDARY + _GENERIC_KEY
            + "[\"']?\\s*[:=]([^\"'$` ]|\\s+[^\"'$`# ])"
            "|ENV\\s+[A-Za-z0-9_]*" + _GENERIC_KEY + "\\s+[\"']?[^\"'$`# ]"
        )},
        note=(
            "Owner ruling 2026-10-13 (W6-V review F2): an unquoted value is a "
            "literal in an env/config-style file and an expression in code, so "
            "this shape serves ONLY file_kind() == 'env_config' (dropped by "
            "patterns_for_file for every other path), and there it matches ANY "
            "non-empty unquoted value -- the 32-char floor is gone. Skipped as "
            "syntax, never by how a value looks: a `$` reference (`$X`, "
            "`${X}`, `$(...)`), a backtick command substitution, a quoted "
            "value (generic_secret_quoted's job), and `#` AFTER whitespace (a "
            "comment; directly after `=` it is part of the value), and a key "
            "that is itself a `$` reference (`$PWD:/app`). A quoted key "
            "(`\"password\": x`) counts. The second arm is Dockerfile's legacy "
            "`ENV KEY value` form. YAML files use generic_secret_unquoted_yaml."
        ),
        file_kinds=("env_config",),
    ),
    CredentialShape(
        "generic_secret_unquoted_yaml",
        "Generic secret (unquoted, YAML)",
        {"content_scan": (_GENERIC_KEY_BOUNDARY + _GENERIC_KEY
                          + "[\"']?\\s*[:=]\\s*" + _GENERIC_YAML_VALUE)},
        note=(
            "generic_secret_unquoted for YAML files: any non-empty unquoted "
            "scalar under a secret-shaped key, with YAML's value syntax silent "
            "(see _GENERIC_YAML_VALUE). A `#` always opens a comment here."
        ),
        file_kinds=("yaml",),
    ),
    CredentialShape(
        "leak_probe",
        "Hook leak-test marker",
        {"content_scan": "VCT_HOOK_LEAK_PROBE_a3f7c2"},
        note=(
            "Smoke-test sentinel, not a real credential family. VCT-prefixed "
            "and hex-tailed so it cannot appear in prose by accident."
        ),
    ),
)


def _require_context(context: str) -> None:
    if context not in CONTEXTS:
        raise ValueError(
            f"unknown credential-shape context {context!r}; "
            f"choose one of {', '.join(CONTEXTS)}"
        )


def pattern_for(shape_id: str, context: str) -> str:
    """Return the pattern ``shape_id`` uses in ``context``.

    Raises ``KeyError`` when the shape does not participate in that context —
    absence is meaningful (see :class:`CredentialShape`), so it is an error to
    ask for it rather than something to paper over with ``None``.
    """
    _require_context(context)
    for shape in SHAPES:
        if shape.id == shape_id:
            return shape.patterns[context]
    raise KeyError(f"unknown credential shape {shape_id!r}")


def patterns_for_context(context: str) -> Tuple[Tuple[str, str, str], ...]:
    """Return ``(id, label, pattern)`` for every shape serving ``context``.

    ``context`` is required and validated — there is deliberately no default,
    because any default would silently give some caller the wrong precision
    bias. Order is the declaration order in :data:`SHAPES`, which the mirrors
    reproduce so that a scanner's reported label order is stable.
    """
    _require_context(context)
    return tuple(
        (s.id, s.label, s.patterns[context]) for s in SHAPES if context in s.patterns
    )


def patterns_for_file(context: str, path: str) -> Tuple[Tuple[str, str, str], ...]:
    """:func:`patterns_for_context`, restricted to the shapes that serve the
    :func:`file_kind` of ``path``. Every content scanner knows the path it
    scans and must use this (or its mirror), not the unrestricted list."""
    kind = file_kind(path)
    _require_context(context)
    return tuple(
        (s.id, s.label, s.patterns[context]) for s in SHAPES
        if context in s.patterns and (s.file_kinds is None or kind in s.file_kinds)
    )


def combined_pattern(context: str) -> str:
    """Return one ``|``-joined alternation of every pattern in ``context``.

    For consumers that want a single predicate rather than per-label reporting
    (``vct``'s ``argv_name`` guard). Each participating pattern is inserted
    verbatim; every ``argv_name`` pattern is already ``^…$``-anchored, so the
    alternation stays whole-string.
    """
    return "|".join(p for _id, _label, p in patterns_for_context(context))


def matching_labels(
    text: str, context: str, path: Optional[str] = None
) -> Tuple[str, ...]:
    """Return the labels whose ``context`` pattern matches ``text``.

    With ``path``, only the shapes serving that file's kind are tried
    (:func:`patterns_for_file`). Never returns, logs or echoes any part of
    ``text`` itself — only labels.
    """
    _require_context(context)
    shapes = (patterns_for_context(context) if path is None
              else patterns_for_file(context, path))
    out = []
    for _id, label, pattern in shapes:
        if re.search(pattern, text):
            out.append(label)
    return tuple(out)


def is_credential_shaped_name(name: str) -> bool:
    """True when ``name`` (one complete identifier) has a live-credential shape.

    The Python side of ``vct``'s ``argv_name`` guard. Never prints ``name``.
    """
    return bool(re.search(combined_pattern("argv_name"), name or ""))

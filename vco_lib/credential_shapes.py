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
    are PROVEN present in this repo (both in the vendored Excalidraw bundle,
    both inside ``check-no-secrets.sh``'s ``*/dist/*`` scan set):

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
from typing import Dict, Mapping, Tuple

__all__ = [
    "CONTEXTS",
    "CredentialShape",
    "SHAPES",
    "pattern_for",
    "patterns_for_context",
    "combined_pattern",
    "matching_labels",
    "is_credential_shaped_name",
]

#: The four haystacks, each with its own documented bias. A caller MUST name one.
CONTEXTS: Tuple[str, ...] = ("repo_scan", "content_scan", "command_scan", "argv_name")


class CredentialShape:
    """One credential family, with the regex to use in each context it serves.

    ``patterns`` maps a context name to an ERE-compatible pattern. A context
    ABSENT from the mapping means this shape deliberately does not participate
    in that context (see the module docstring for why ``aws_access_key_id`` is
    absent from ``repo_scan``). Absence is a design statement, not an omission.
    """

    __slots__ = ("id", "label", "patterns", "note")

    def __init__(
        self, id: str, label: str, patterns: Mapping[str, str], note: str = ""
    ) -> None:
        self.id = id
        self.label = label
        self.patterns: Dict[str, str] = dict(patterns)
        self.note = note
        for ctx in self.patterns:
            if ctx not in CONTEXTS:
                raise ValueError(f"{id}: unknown context {ctx!r}")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"CredentialShape({self.id!r}, contexts={sorted(self.patterns)})"


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
            "ABSENT from repo_scan: PROVEN base64 collision. The vendored "
            "Excalidraw bundle's base64 WASM/font payload contains an "
            "AKIA+16-upper-alnum run by chance. Adding it to repo_scan would "
            "make check-no-secrets.sh fail on every commit."
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
            "The >=120-char base64 BODY FLOOR that suppresses stub/pattern "
            "files is CONTROL FLOW over a multi-line window, not vocabulary, "
            "and grep -E cannot express it. It stays language-local in "
            "post-tool-security.{sh,ps1}, exactly as the segment-split "
            "predicate stays language-local in the env-key-needle SSOT."
        ),
    ),
    CredentialShape(
        "generic_secret_quoted",
        "Generic secret",
        {"content_scan": "(SECRET|API_KEY|ACCESS_TOKEN|PRIVATE_KEY)\\s*[:=]\\s*[\"'][a-zA-Z0-9+/=_\\-]{32,}"},
    ),
    CredentialShape(
        "generic_secret_unquoted",
        "Generic secret (unquoted)",
        {"content_scan": "(SECRET|API_KEY|ACCESS_TOKEN|PRIVATE_KEY)\\s*[:=]\\s*[a-zA-Z0-9+/=_\\-]{32,}"},
        note=(
            "The dotenv shape `API_KEY=abc…` with no quotes. The quoted variant "
            "requires an opening quote after `=` and misses it entirely."
        ),
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


def combined_pattern(context: str) -> str:
    """Return one ``|``-joined alternation of every pattern in ``context``.

    For consumers that want a single predicate rather than per-label reporting
    (``vct``'s ``argv_name`` guard). Each participating pattern is inserted
    verbatim; every ``argv_name`` pattern is already ``^…$``-anchored, so the
    alternation stays whole-string.
    """
    return "|".join(p for _id, _label, p in patterns_for_context(context))


def matching_labels(text: str, context: str) -> Tuple[str, ...]:
    """Return the labels whose ``context`` pattern matches ``text``.

    Never returns, logs or echoes any part of ``text`` itself — only labels.
    """
    _require_context(context)
    out = []
    for _id, label, pattern in patterns_for_context(context):
        if re.search(pattern, text):
            out.append(label)
    return tuple(out)


def is_credential_shaped_name(name: str) -> bool:
    """True when ``name`` (one complete identifier) has a live-credential shape.

    The Python side of ``vct``'s ``argv_name`` guard. Never prints ``name``.
    """
    return bool(re.search(combined_pattern("argv_name"), name or ""))

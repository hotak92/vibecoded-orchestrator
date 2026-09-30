# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The WebFetch SSRF guard's ONE URL decision (v0.2.100, review R18F-04).

Called by ``templates/hooks/pre-tool-use.{sh,ps1}`` through their thin
``_lib/ssrf-allowlist.{sh,ps1}`` callers::

    printf '%s' "$URL" | python -m vco_lib.ssrf_url verdict
    python -m vco_lib.ssrf_url verdict --url-hex <UTF-8 bytes of the URL, hex>

Until v0.2.100 this decision was a ~450-line URL parser written twice, once
in bash and once in PowerShell, kept in step by a shared case table. The two
drifted in ways the table could not see (R18F-02: Windows PowerShell 5.1 drops
the double quotes inside a native command's argument, so the IDNA helper it
spawned never ran and every non-ASCII host was blocked on Windows only). A
decision that must be identical on every OS is shared CODE (A>B>C rule A):
this module; the shell files only locate the interpreter, pass the URL and map
the verdict.

Output (stdout): line 1 is ONE verdict word —

* ``allow`` — the URL is one of this machine's own VCO services;
* ``block`` — it targets a private / internal address, or cannot be read;
* ``pass``  — a public address.

After ``block`` a second line lists the allowed ``host:port`` pairs (the
loopback spellings ``127.0.0.1`` / ``[::1]`` folded into ``localhost``) for
the hook's message. Anything else — no output, an import failure, a word the
caller does not know — is treated as ``block`` by the callers: a guard that
cannot give an answer does not vouch for the URL.

Allowed pairs
-------------
Derived at run time from the projected env (``config_projection`` writes these
from the ``service_endpoints`` rows), never from a hand-edited list:

=========== ==========================================================
Weaviate    :func:`vco_lib.weaviate_helpers.weaviate_url_default`
            (``WEAVIATE_URL`` → ``http://localhost:$WEAVIATE_PORT`` → 8081)
Ollama      ``OLLAMA_URL``, else the compiled default port
code-embed  ``CODE_EMBED_SERVICE_URL``, else the compiled default port
vct-hub     :func:`vco_lib.hub_ensure.resolve_hub_port`
            (``VCT_HUB_PORT`` → ``hub.port`` → 7700)
always      ``:8082`` and Gradio ``:7860`` (no env knob; historical)
=========== ==========================================================

A loopback pair is allowed as ``localhost``, ``127.0.0.1`` and ``[::1]``
alike; a non-loopback pair (an adopted service on ``gpu.lan``) as exactly that
``host:port``. ALLOW also needs a CLEAN authority: one carrying ``@``, ``\\``,
``%``, whitespace, a control character or a non-ASCII character is never
allowed, whatever it parses to — that closes the whole userinfo /
delimiter-confusion class rather than one spelling of it.

How a URL is read (WHATWG, for the special schemes a fetcher follows)
--------------------------------------------------------------------
* tab / LF / CR are removed anywhere; C0 controls and space trimmed at the ends;
* the scheme is case-insensitive; any run of ``/`` or ``\\`` after it is skipped
  (``http:\\\\h``, ``http:h``, ``HTTP:///h`` all reach host ``h``); a non-special
  scheme without ``//`` is read as a scheme-less authority (``localhost:8081/x``);
* ``\\`` is a path delimiter exactly like ``/`` — in
  ``http://10.0.0.1:22\\@localhost:8081/`` the host is ``10.0.0.1``, port 22;
* userinfo ends at the LAST ``@`` of the authority;
* the host is percent-decoded; a non-ASCII host is NFKC-normalised and
  IDNA-encoded (``ｌｏｃａｌｈｏｓｔ`` → ``localhost``, ``bücher.de`` →
  ``xn--bcher-kva.de``); it is ASCII-lower-cased and trailing dots dropped;
* a host whose last label is numeric is IPv4 in any WHATWG spelling — decimal
  ``2130706433``, hex ``0x7f.1``, octal ``0177.0.0.1``, short ``127.1``;
* a bracketed host is IPv6, expanded to eight hextets (``::``, long form,
  IPv4-mapped ``::ffff:7f00:1``, an embedded dotted tail).

BLOCK when any of:

* the host is loopback / private / link-local / unspecified / CGNAT /
  reserved IPv4 (also when embedded in IPv4-mapped / -compatible / NAT64 /
  6to4 IPv6), IPv6 ULA / link-local / site-local / multicast, or the name
  ``localhost`` / ``*.localhost``;
* the URL does not parse (a bad port, a bad IPv6 literal, a forbidden host
  character, an out-of-range numeric IPv4);
* the host is not ASCII and cannot be converted to its IDNA form;
* the legacy raw-substring pattern matches anywhere in the URL
  (case-insensitive): kept so a DNS name embedding a private address
  (``127.0.0.1.nip.io``) stays blocked as it always was.

The check runs in ``allow`` → ``block`` → ``pass`` order: an allowed service
is never blocked by the legacy pattern that also matches ``localhost``.

Stdlib-only apart from two ``vco_lib`` leaves, imported when the allowed pairs
are first needed; the whole run is one interpreter start per WebFetch.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import Optional, Sequence

#: Schemes whose URLs a fetcher parses as hierarchical (WHATWG "special").
_SPECIAL_SCHEMES = frozenset({"http", "https", "ws", "wss", "ftp"})
_DEFAULT_PORTS = {"https": 443, "wss": 443, "ftp": 21}

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
#: C0 controls and space — trimmed from both ends (WHATWG).
_EDGE_TRIM = "".join(chr(c) for c in range(0x21))
#: An authority carrying any of these is never CLEAN (so never allowed).
_UNCLEAN_RE = re.compile(r"[\x00-\x20\x7f]")
#: Characters WHATWG forbids in a (non-IPv6) host.
_FORBIDDEN_HOST_RE = re.compile(r"[\[\] #/:<>?@\\^|%]")
_PRINTABLE_ASCII_RE = re.compile(r"^[\x20-\x7e]*$")
_DOTTED_QUAD_RE = re.compile(
    r"^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$"
)
_HEXTET_RE = re.compile(r"^[0-9a-f]{1,4}$")
_IPV6_CHARS_RE = re.compile(r"^[0-9a-f:.]+$")

#: The legacy substring pattern (see the module docstring). ``re.ASCII`` keeps
#: the case folding to A-Z, as ``grep -i`` did: a Unicode fold would let the
#: long s (U+017F) match the ``s`` of ``localhost``.
LEGACY_PATTERN = re.compile(
    r"(localhost|127\.|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\."
    r"|192\.168\.[0-9]+\.|169\.254\.[0-9]+\.|0\.0\.0\.0|::1)",
    re.IGNORECASE | re.ASCII,
)

#: Canonical spellings of the loopback host; a loopback pair is allowed as all three.
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[0:0:0:0:0:0:0:1]")
#: Always-allowed local ports with no env knob (the historical literals).
_FIXED_LOCAL_PORTS = (8082, 7860)

VERDICTS = ("allow", "block", "pass")


@dataclass(frozen=True)
class ParsedUrl:
    """A URL as the guard reads it.

    ``kind`` is ``name`` | ``ipv4`` | ``ipv6`` | ``nonascii`` (a non-ASCII host
    that could not be converted to ASCII). ``host`` is canonical: a lower-case
    name, a dotted quad, or ``[h:h:h:h:h:h:h:h]``. ``clean`` is False when the
    authority carries ``@ \\ %``, whitespace, a control or a non-ASCII
    character. ``value`` holds the IPv4 value, ``hextets`` the IPv6 groups.
    """

    kind: str
    host: str
    port: int
    clean: bool
    value: int = 0
    hextets: tuple[int, ...] = ()

    @property
    def pair(self) -> str:
        return f"{self.host}:{self.port}"


# ─── pieces of the parse ─────────────────────────────────────────────────


def _ipv4_number(part: str) -> Optional[int]:
    """WHATWG IPv4-number: ``0x`` hex, ``0``-prefixed octal, else decimal."""
    if not part:
        return None
    s, radix = part, 10
    if s[:2] in ("0x", "0X"):
        s, radix = s[2:], 16
    elif len(s) >= 2 and s.startswith("0"):
        s, radix = s[1:], 8
    if not s:
        return 0
    digits = {16: "0123456789abcdefABCDEF", 8: "01234567", 10: "0123456789"}[radix]
    if any(ch not in digits for ch in s):
        return None
    s = s.lstrip("0") or "0"
    # Past 11 significant digits every radix is beyond 2^32.
    if len(s) > 11:
        return None
    value = int(s, radix)
    return value if value <= 0xFFFFFFFF else None


def _ends_in_number(host: str) -> bool:
    last = host.rsplit(".", 1)[-1]
    return bool(re.fullmatch(r"[0-9]+", last) or re.fullmatch(r"0[xX][0-9a-fA-F]*", last))


def _parse_ipv4(host: str) -> Optional[int]:
    """The 32-bit value of a WHATWG IPv4 host (trailing dots already dropped)."""
    if not host or host.startswith(".") or host.endswith(".") or ".." in host:
        return None
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    value = 0
    for i, part in enumerate(parts[:-1]):
        n = _ipv4_number(part)
        if n is None or n > 255:
            return None
        value += n << (8 * (3 - i))
    last = _ipv4_number(parts[-1])
    if last is None or last >= 1 << (8 * (5 - len(parts))):
        return None
    return value + last


def _dotted(value: int) -> str:
    return ".".join(str((value >> shift) & 255) for shift in (24, 16, 8, 0))


def _parse_ipv6(inner: str) -> Optional[tuple[int, ...]]:
    """Eight hextets of a bracket-less, lower-case IPv6 literal."""
    s = inner
    if not s or not _IPV6_CHARS_RE.match(s):
        return None
    if "." in s:
        dq = s.rsplit(":", 1)[-1]
        m = _DOTTED_QUAD_RE.match(dq)
        if not m or any(int(g) > 255 for g in m.groups()):
            return None
        a, b, c, d = (int(g) for g in m.groups())
        v4 = (a << 24) + (b << 16) + (c << 8) + d
        s = s[: len(s) - len(dq)] + f"{v4 >> 16:x}:{v4 & 0xFFFF:x}"
    compressed = "::" in s
    head, tail = (s.split("::", 1) if compressed else (s, ""))
    if "::" in tail:
        return None
    for group in (head, tail):
        if group.startswith(":") or group.endswith(":"):
            return None
    hg = head.split(":") if head else []
    tg = tail.split(":") if tail else []
    if compressed:
        if len(hg) + len(tg) > 7:
            return None
        groups = hg + ["0"] * (8 - len(hg) - len(tg)) + tg
    else:
        if len(hg) != 8:
            return None
        groups = hg
    if any(not _HEXTET_RE.match(g) for g in groups):
        return None
    return tuple(int(g, 16) for g in groups)


def _percent_decode(text: str) -> Optional[bytes]:
    """The host's raw bytes (literal text as UTF-8); None on an encoded
    control / DEL byte. A malformed ``%`` stays literal (and is then refused
    as a forbidden host character, as WHATWG does)."""
    raw = text.encode("utf-8", "surrogateescape")
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x25 and re.fullmatch(rb"[0-9A-Fa-f]{2}", raw[i + 1:i + 3]):
            val = int(raw[i + 1:i + 3], 16)
            if val < 32 or val == 127:
                return None
            out.append(val)
            i += 3
            continue
        out.append(raw[i])
        i += 1
    return bytes(out)


def _idna(raw: bytes) -> Optional[str]:
    """NFKC-normalised IDNA (ASCII) form of a non-ASCII host, or None."""
    try:
        text = unicodedata.normalize("NFKC", raw.decode("utf-8"))
        out = text.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    return out if out and _PRINTABLE_ASCII_RE.match(out) else None


# ─── the parse ───────────────────────────────────────────────────────────


def parse_url(url: str) -> Optional[ParsedUrl]:
    """Read ``url`` as described in the module docstring; None = unreadable."""
    u = url.replace("\t", "").replace("\n", "").replace("\r", "").strip(_EDGE_TRIM)
    clean = True
    m = _SCHEME_RE.match(u)
    if m:
        scheme = m.group(1).lower()
        rest = u[m.end():]
        if scheme not in _SPECIAL_SCHEMES:
            if rest.startswith("//"):
                rest = rest[2:]
            else:
                # `localhost:8081/x` — not a scheme, a scheme-less authority.
                scheme, rest = "http", u
    else:
        scheme, rest = "http", u
    rest = rest.lstrip("/\\")
    if "\\" in re.split(r"[/?#]", rest, maxsplit=1)[0]:
        clean = False
    rest = rest.replace("\\", "/")
    auth = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if "@" in auth:
        clean = False
        auth = auth.rsplit("@", 1)[1]
    if "%" in auth or _UNCLEAN_RE.search(auth):
        clean = False
    if auth.startswith("["):
        close = auth.find("]")
        if close < 0:
            return None
        host_part, port_text = auth[: close + 1], auth[close + 1:]
        if port_text:
            if not port_text.startswith(":"):
                return None
            port_text = port_text[1:]
    else:
        host_part, _, port_text = auth.partition(":")
    port_text = port_text or str(_DEFAULT_PORTS.get(scheme, 80))
    if not re.fullmatch(r"[0-9]+", port_text, re.ASCII):
        return None
    port_text = port_text.lstrip("0") or "0"
    if len(port_text) > 5 or int(port_text) > 65535:
        return None
    port = int(port_text)
    if not host_part:
        return None
    if host_part.startswith("["):
        hextets = _parse_ipv6(host_part[1:-1].lower())
        if hextets is None:
            return None
        host = "[" + ":".join(f"{h:x}" for h in hextets) + "]"
        return ParsedUrl("ipv6", host, port, clean, hextets=hextets)
    raw = _percent_decode(host_part)
    if raw is None:
        return None
    if all(0x20 <= b <= 0x7E for b in raw):
        host = raw.decode("ascii")
    else:
        clean = False
        converted = _idna(raw)
        if converted is None:
            return ParsedUrl("nonascii", raw.decode("utf-8", "replace"), port, clean)
        host = converted
    if _FORBIDDEN_HOST_RE.search(host):
        return None
    host = host.lower().rstrip(".")
    if not host:
        return None
    if _ends_in_number(host):
        value = _parse_ipv4(host)
        if value is None:
            return None
        return ParsedUrl("ipv4", _dotted(value), port, clean, value=value)
    return ParsedUrl("name", host, port, clean)


# ─── the decision ────────────────────────────────────────────────────────


def ipv4_is_internal(v: int) -> bool:
    """True when the 32-bit IPv4 value is not a public unicast address."""
    a = v >> 24
    return (
        a in (0, 10, 127)
        or a >= 224                              # multicast + 240/4 + broadcast
        or v & 0xFFC00000 == 0x64400000          # 100.64/10 CGNAT
        or v >> 16 == 0xA9FE                     # 169.254/16
        or v & 0xFFF00000 == 0xAC100000          # 172.16/12
        or v >> 8 == 0xC00000                    # 192.0.0/24
        or v >> 16 == 0xC0A8                     # 192.168/16
        or v & 0xFFFE0000 == 0xC6120000          # 198.18/15
    )


def ipv6_is_internal(h: Sequence[int]) -> bool:
    """True when eight hextets are not a public unicast address (an embedded
    IPv4 is judged as IPv4)."""
    h0 = h[0]
    if (h0 & 0xFE00 == 0xFC00               # fc00::/7 ULA
            or h0 & 0xFFC0 == 0xFE80        # fe80::/10 link-local
            or h0 & 0xFFC0 == 0xFEC0        # fec0::/10 site-local
            or h0 & 0xFF00 == 0xFF00):      # ff00::/8 multicast
        return True
    if h0 == 0 and h[1] == h[2] == h[3] == h[4] == 0 and h[5] in (0, 0xFFFF):
        # ::/96 (::, ::1, IPv4-compatible) and ::ffff:0:0/96 (IPv4-mapped).
        return ipv4_is_internal((h[6] << 16) + h[7])
    if h0 == 0x64 and h[1] == 0xFF9B and h[2] == h[3] == h[4] == h[5] == 0:
        return ipv4_is_internal((h[6] << 16) + h[7])   # 64:ff9b::/96 NAT64
    if h0 == 0x2002:
        return ipv4_is_internal((h[1] << 16) + h[2])   # 2002::/16 6to4
    return False


def is_blocked(url: str) -> bool:
    """True when ``url`` targets a private / internal address, cannot be read,
    has an unconvertible non-ASCII host, or matches the legacy pattern."""
    if LEGACY_PATTERN.search(url):
        return True
    p = parse_url(url)
    if p is None or p.kind == "nonascii":
        return True
    if p.kind == "ipv4":
        return ipv4_is_internal(p.value)
    if p.kind == "ipv6":
        return ipv6_is_internal(p.hextets)
    return p.host == "localhost" or p.host.endswith(".localhost")


def _env(name: str) -> str:
    """The variable's value, stripped; empty or whitespace-only = unset."""
    return (os.environ.get(name) or "").strip()


def service_urls() -> list[str]:
    """The URL of every service WebFetch may reach on this machine."""
    from vco_lib.hub_ensure import resolve_hub_port
    from vco_lib.service_endpoints import DEFAULT_PORTS
    from vco_lib.weaviate_helpers import weaviate_url_default

    urls = [
        weaviate_url_default(),
        _env("OLLAMA_URL") or f"http://localhost:{DEFAULT_PORTS['ollama']}",
        _env("CODE_EMBED_SERVICE_URL") or f"http://localhost:{DEFAULT_PORTS['code_embed']}",
        f"http://localhost:{resolve_hub_port()}",
    ]
    urls.extend(f"http://localhost:{port}" for port in _FIXED_LOCAL_PORTS)
    return urls


def allowed_pairs() -> list[str]:
    """Every allowed canonical ``host:port`` (loopback pairs in all three
    spellings). A configured URL is read by the same parser as a request."""
    pairs: list[str] = []
    for url in service_urls():
        p = parse_url(url)
        if p is None:
            continue
        hosts = _LOOPBACK_HOSTS if p.host in _LOOPBACK_HOSTS else (p.host,)
        for host in hosts:
            pair = f"{host}:{p.port}"
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def display_pairs(pairs: Sequence[str]) -> str:
    """The pairs for a human: the loopback spellings folded into ``localhost``."""
    folded = ("127.0.0.1:", "[0:0:0:0:0:0:0:1]:")
    return " ".join(p for p in pairs if not p.startswith(folded))


def is_allowed(url: str, pairs: Optional[Sequence[str]] = None) -> bool:
    """True when ``url``'s authority is clean and its pair is allowed."""
    p = parse_url(url)
    if p is None or not p.clean:
        return False
    return p.pair in (allowed_pairs() if pairs is None else pairs)


def verdict(url: str) -> str:
    """``allow`` | ``block`` | ``pass`` — see the module docstring."""
    if is_allowed(url):
        return "allow"
    return "block" if is_blocked(url) else "pass"


# ─── CLI ─────────────────────────────────────────────────────────────────


def _read_url(url_hex: Optional[str]) -> str:
    """The URL from ``--url-hex`` (UTF-8 bytes as hex — what the PowerShell
    caller passes, because no quoting or console encoding can alter a hex
    string) or else from stdin's raw bytes. Undecodable bytes survive as
    surrogates, so the host's bytes reach the parser unchanged."""
    raw = bytes.fromhex(url_hex) if url_hex is not None else sys.stdin.buffer.read()
    return raw.decode("utf-8", "surrogateescape")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="vco_lib.ssrf_url", description="The WebFetch SSRF guard's URL decision.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verdict", help="print allow | block | pass for one URL")
    v.add_argument("--url-hex", help="the URL as the hex of its UTF-8 bytes (default: read stdin)")
    sub.add_parser("pairs", help="print the allowed host:port pairs, one per line")
    args = parser.parse_args(argv)
    if args.cmd == "pairs":
        for pair in allowed_pairs():
            print(pair)
        return 0
    try:
        url = _read_url(args.url_hex)
        pairs = allowed_pairs()
        word = "allow" if is_allowed(url, pairs) else ("block" if is_blocked(url) else "pass")
    except Exception as exc:  # noqa: BLE001 — a guard that cannot decide blocks
        print(f"vco_lib.ssrf_url: could not judge the URL ({type(exc).__name__}: {exc})", file=sys.stderr)
        word, pairs = "block", []
    sys.stdout.write(word + "\n")
    if word == "block":
        sys.stdout.write(display_pairs(pairs) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

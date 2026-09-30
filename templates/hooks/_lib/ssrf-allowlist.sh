# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# _lib/ssrf-allowlist.sh — the pre-tool-use SSRF guard's ONE URL decision:
# which WebFetch URLs are this machine's own VCO services (allowed), which
# target a private / internal address (blocked), and which are public (pass).
# Windows mirror: _lib/ssrf-allowlist.ps1 (MUST stay logically identical; the
# shared case table tests/fixtures/ssrf_cases.json runs against both).
#
# ── The allowed pairs ────────────────────────────────────────────────────
# v0.2.100 WP-18B: the allowlist used to be the literal port set
# 8081|8082|11435|11440|7860 on localhost / 127.0.0.1. A user whose
# service_endpoints row moved Weaviate (e.g. to 18081) had WebFetch to their
# own Weaviate blocked, and the message told them to hand-edit a shipped hook
# — which the next bundle update adopts back. The pairs are now DERIVED at run
# time from the projected env (config_projection writes these from the
# service_endpoints rows into .claude/settings.json env / .claude/env):
#
#   Weaviate   WEAVIATE_URL, else http://localhost:$WEAVIATE_PORT, else :8081
#              (the weaviate_helpers.weaviate_url_default ladder)
#   Ollama     OLLAMA_URL, else :11435
#   code-embed CODE_EMBED_SERVICE_URL, else :11440
#   vct-hub    the resolver's `hub-port` (VCT_HUB_PORT -> hub.port -> 7700)
#   always     :8082 and Gradio :7860 (no env knob; the historical literals)
#
# The literal ports are the per-service FALLBACK when that service's env is
# silent — never an addition to a stated value. A loopback pair is allowed as
# localhost, 127.0.0.1 and [::1] alike; a non-loopback pair (an adopted
# service on gpu.lan) is allowed as exactly that host:port.
#
# ── How a URL is read (review R18-04 / R18-05) ───────────────────────────
# Both halves — allow and block — read the URL through ONE parser,
# vco_ssrf_parse, which follows the WHATWG URL parser a Node-based fetcher
# uses for the special schemes (http, https, ws, wss, ftp):
#   * tab / LF / CR are removed anywhere, C0 + space trimmed at the ends;
#   * the scheme is case-insensitive, and any run of `/` or `\` after it is
#     skipped (`http:\\h`, `http:h`, `HTTP:///h` all reach host h);
#   * `\` is a path delimiter exactly like `/` — so in
#     `http://10.0.0.1:22\@localhost:8081/` the host is 10.0.0.1, port 22
#     (the old parser saw `localhost:8081` and ALLOWED it);
#   * userinfo ends at the LAST `@` of the authority;
#   * the host is percent-decoded, ASCII-lower-cased, trailing dots dropped;
#   * a host whose last label is numeric is IPv4 in any WHATWG spelling —
#     decimal `2130706433`, hex `0x7f.1`, octal `0177.0.0.1`, short `127.1`;
#   * a bracketed host is IPv6, expanded to eight hextets (`::`, long form,
#     IPv4-mapped `::ffff:7f00:1`, embedded dotted tails).
#
# ALLOW needs a CLEAN authority as well as a listed pair: an authority
# carrying `@`, `\`, `%`, whitespace or a control character is never allowed,
# whatever it parses to. That is the simple rule that closes the whole
# userinfo / delimiter-confusion class rather than one spelling of it.
#
# BLOCK when any of:
#   * the parsed host is loopback / private / link-local / unspecified /
#     CGNAT / reserved (IPv4, or IPv4 embedded in mapped / compatible /
#     NAT64 / 6to4 IPv6), IPv6 ULA / link-local / site-local / multicast,
#     or the name `localhost` / `*.localhost`;
#   * the URL does not parse (an invalid port, a bad IPv6 literal, a
#     forbidden host character, an out-of-range numeric IPv4) — a guard that
#     cannot read the address does not vouch for it;
#   * the host is not ASCII and cannot be converted to its ASCII (IDNA) form.
#     A non-ASCII host is NFKC-normalised and IDNA-encoded by the Python
#     standard library (_vco_ssrf_idna, via the hooks' find-python locator),
#     so fullwidth `ｌｏｃａｌｈｏｓｔ` is judged as `localhost` and `bücher.de`
#     as the public `xn--bcher-kva.de`. No Python, or a failed conversion,
#     blocks — the guard cannot know where such a name goes. A non-ASCII
#     authority is never CLEAN, so it is never on the allow side;
#   * the legacy raw-substring pattern matches anywhere in the URL (now
#     case-insensitive in both shells): kept so a DNS name that embeds a
#     private address (`127.0.0.1.nip.io`) stays blocked as it always was.
#
# Usage:
#     . "$SCRIPT_DIR/_lib/ssrf-allowlist.sh"
#     vco_ssrf_verdict "$URL" "$PROJECT_ROOT"      # prints allow|block|pass
#     vco_ssrf_url_allowed "$URL" "$PROJECT_ROOT"  # exit 0 = allowed
#     vco_ssrf_url_blocked "$URL"                  # exit 0 = blocked
#     vco_ssrf_allowed_pairs "$PROJECT_ROOT"       # one host:port per line

# The hooks' ONE Python locator (sets $PY); the hook normally sourced it already.
if [[ -z "${PY:-}" && -f "$(dirname "${BASH_SOURCE[0]}")/find-python.sh" ]]; then
    # shellcheck source=find-python.sh disable=SC1091
    . "$(dirname "${BASH_SOURCE[0]}")/find-python.sh"
fi

# Stdlib-only IDNA conversion: argv[1] is the host's UTF-8 bytes in hex (so
# no shell / console encoding can alter them); prints the ASCII form.
# MUST MATCH $script:VcoSsrfIdnaPy in ssrf-allowlist.ps1.
VCO_SSRF_IDNA_PY='import sys, unicodedata
h = unicodedata.normalize("NFKC", bytes.fromhex(sys.argv[1]).decode("utf-8"))
sys.stdout.write(h.encode("idna").decode("ascii"))'

# The legacy substring pattern (see the header). MUST MATCH
# $script:VcoSsrfLegacyPattern in ssrf-allowlist.ps1.
VCO_SSRF_LEGACY_PATTERN='(localhost|127\.|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.|192\.168\.[0-9]+\.|169\.254\.[0-9]+\.|0\.0\.0\.0|::1)'

# _vco_ssrf_lower S :: S with ASCII A-Z lower-cased (other bytes untouched).
_vco_ssrf_lower() {
    printf '%s' "$1" | LC_ALL=C tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz'
}

# _vco_ssrf_num PART :: the WHATWG IPv4-number value of PART in
# _VCO_SSRF_NUM (0x.. hex, 0.. octal, else decimal); fails when PART is not
# a number in its radix or exceeds 32 bits.
_vco_ssrf_num() {
    local s="$1" radix=10
    [[ -n "$s" ]] || return 1
    if [[ "$s" == 0[xX]* ]]; then
        radix=16
        s="${s:2}"
    elif [[ ${#s} -ge 2 && "$s" == 0* ]]; then
        radix=8
        s="${s:1}"
    fi
    if [[ -z "$s" ]]; then
        _VCO_SSRF_NUM=0
        return 0
    fi
    case "$radix" in
        16) [[ "$s" =~ ^[0-9a-fA-F]+$ ]] || return 1 ;;
        8) [[ "$s" =~ ^[0-7]+$ ]] || return 1 ;;
        *) [[ "$s" =~ ^[0-9]+$ ]] || return 1 ;;
    esac
    # Leading zeros carry no value; past 11 significant digits every radix
    # is beyond 2^32 (and bash arithmetic could overflow), so refuse.
    while [[ ${#s} -gt 1 && "$s" == 0* ]]; do s="${s:1}"; done
    [[ ${#s} -le 11 ]] || return 1
    _VCO_SSRF_NUM=$(( ${radix}#${s} ))
    [[ "$_VCO_SSRF_NUM" -le 4294967295 ]] || return 1
    return 0
}

# _vco_ssrf_ends_in_number HOST :: exit 0 when WHATWG would parse HOST as IPv4.
_vco_ssrf_ends_in_number() {
    local last="${1##*.}"
    [[ "$last" =~ ^[0-9]+$ ]] && return 0
    [[ "$last" =~ ^0[xX][0-9a-fA-F]*$ ]]
}

# _vco_ssrf_ipv4 HOST :: HOST (all trailing dots already dropped) as a 32-bit
# value in _VCO_SSRF_V4; fails as WHATWG's IPv4 parser does.
_vco_ssrf_ipv4() {
    local host="$1" n=0 i=0 v=0 last
    local -a parts=()
    [[ -n "$host" && "$host" != .* && "$host" != *. && "$host" != *..* ]] || return 1
    IFS=. read -r -a parts <<< "$host"
    n=${#parts[@]}
    [[ $n -ge 1 && $n -le 4 ]] || return 1
    for ((i = 0; i < n - 1; i++)); do
        _vco_ssrf_num "${parts[$i]}" || return 1
        [[ "$_VCO_SSRF_NUM" -le 255 ]] || return 1
        v=$(( v + (_VCO_SSRF_NUM << (8 * (3 - i))) ))
    done
    last="${parts[$((n - 1))]}"
    _vco_ssrf_num "$last" || return 1
    [[ "$_VCO_SSRF_NUM" -lt $(( 1 << (8 * (5 - n)) )) ]] || return 1
    _VCO_SSRF_V4=$(( v + _VCO_SSRF_NUM ))
    return 0
}

# _vco_ssrf_v4_dotted V :: the dotted-quad spelling of a 32-bit value.
_vco_ssrf_v4_dotted() {
    printf '%d.%d.%d.%d' $(( ($1 >> 24) & 255 )) $(( ($1 >> 16) & 255 )) \
        $(( ($1 >> 8) & 255 )) $(( $1 & 255 ))
}

# _vco_ssrf_ipv6 INNER :: the bracket-less IPv6 literal (lower-case) as eight
# hextet values in _VCO_SSRF_H (array); fails on anything WHATWG refuses.
_vco_ssrf_ipv6() {
    local s="$1" head="" tail="" g v4 dq
    local -a hg=() tg=() all=()
    _VCO_SSRF_H=()
    [[ -n "$s" && "$s" =~ ^[0-9a-f:.]+$ ]] || return 1
    # An embedded dotted IPv4 tail becomes two hextets.
    if [[ "$s" == *.* ]]; then
        dq="${s##*:}"
        [[ "$dq" =~ ^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$ ]] || return 1
        for g in 1 2 3 4; do
            [[ "${BASH_REMATCH[$g]}" -le 255 ]] || return 1
        done
        v4=$(( (BASH_REMATCH[1] << 24) + (BASH_REMATCH[2] << 16) + (BASH_REMATCH[3] << 8) + BASH_REMATCH[4] ))
        s="${s%"$dq"}$(printf '%x:%x' $(( v4 >> 16 )) $(( v4 & 65535 )))"
    fi
    if [[ "$s" == *::* ]]; then
        head="${s%%::*}"
        tail="${s#*::}"
        [[ "$tail" != *::* ]] || return 1
    else
        head="$s"
    fi
    for g in "$head" "$tail"; do
        [[ "$g" != :* && "$g" != *: ]] || return 1
    done
    if [[ -n "$head" ]]; then IFS=: read -r -a hg <<< "$head"; fi
    if [[ -n "$tail" ]]; then IFS=: read -r -a tg <<< "$tail"; fi
    if [[ "$s" == *::* ]]; then
        [[ $(( ${#hg[@]} + ${#tg[@]} )) -le 7 ]] || return 1
    else
        [[ ${#hg[@]} -eq 8 ]] || return 1
    fi
    for g in ${hg[@]+"${hg[@]}"}; do all+=("$g"); done
    if [[ "$s" == *::* ]]; then
        for ((g = ${#hg[@]} + ${#tg[@]}; g < 8; g++)); do all+=(0); done
    fi
    for g in ${tg[@]+"${tg[@]}"}; do all+=("$g"); done
    for g in "${all[@]}"; do
        [[ "$g" =~ ^[0-9a-f]{1,4}$ ]] || return 1
        _VCO_SSRF_H+=($(( 16#$g )))
    done
    [[ ${#_VCO_SSRF_H[@]} -eq 8 ]]
}

# _vco_ssrf_pct_decode S :: S percent-decoded (to raw bytes) into
# _VCO_SSRF_DEC. Fails on a decoded control / DEL byte. A malformed `%` stays
# literal (and the caller refuses it as a forbidden host character, as WHATWG
# does).
_vco_ssrf_pct_decode() {
    local s="$1" out="" hex val ch
    while [[ "$s" == *%* ]]; do
        out+="${s%%\%*}"
        s="${s#*\%}"
        if [[ "$s" =~ ^([0-9A-Fa-f]{2}) ]]; then
            hex="${BASH_REMATCH[1]}"
            val=$(( 16#$hex ))
            s="${s:2}"
            if [[ $val -lt 32 || $val -eq 127 ]]; then
                return 1
            else
                # shellcheck disable=SC2059  # the format IS the escape
                printf -v ch "\\x$hex"
                out+="$ch"
            fi
        else
            out+="%"
        fi
    done
    _VCO_SSRF_DEC="$out$s"
    return 0
}

# _vco_ssrf_idna HOST :: HOST (raw UTF-8 bytes) NFKC-normalised and IDNA-
# encoded into _VCO_SSRF_IDNA; fails when no Python is found or the
# conversion fails.
_vco_ssrf_idna() {
    local hex
    _VCO_SSRF_IDNA=""
    [[ -n "${PY:-}" ]] || return 1
    hex="$(printf '%s' "$1" | od -An -v -tx1 | tr -d ' \n')" || return 1
    [[ -n "$hex" ]] || return 1
    _VCO_SSRF_IDNA="$("$PY" -I -c "$VCO_SSRF_IDNA_PY" "$hex" 2>/dev/null)" || return 1
    [[ -n "$_VCO_SSRF_IDNA" ]]
}

# vco_ssrf_parse URL :: parse URL as described in the header. On success sets
#   VCO_SSRF_HOST  canonical host: name | dotted IPv4 | [h:h:h:h:h:h:h:h]
#   VCO_SSRF_KIND  name | ipv4 | ipv6 | nonascii (a non-ASCII host that could
#                  not be converted to ASCII)
#   VCO_SSRF_PORT  decimal port (the scheme default when absent)
#   VCO_SSRF_CLEAN 1 when the authority carries no @ \ % whitespace/control
# and returns 0; returns 1 when the URL cannot be read.
vco_ssrf_parse() {
    # Byte semantics for every pattern below (restored when the function
    # returns: bash scopes a `local LC_ALL`).
    local LC_ALL=C
    local u="$1" scheme="" rest raw auth hostpart port="" h
    local ctl_re='[[:cntrl:][:space:]]' ascii_re='^[ -~]*$' forbid_re='[][ #/:<>?@\\^|%]'
    VCO_SSRF_HOST="" VCO_SSRF_KIND="" VCO_SSRF_PORT="" VCO_SSRF_CLEAN=1
    u="${u//$'\t'/}"
    u="${u//$'\n'/}"
    u="${u//$'\r'/}"
    u="${u#"${u%%[![:cntrl:] ]*}"}"
    u="${u%"${u##*[![:cntrl:] ]}"}"
    if [[ "$u" =~ ^([A-Za-z][A-Za-z0-9+.-]*): ]]; then
        scheme="$(_vco_ssrf_lower "${BASH_REMATCH[1]}")"
        rest="${u#*:}"
        case "$scheme" in
            http | https | ws | wss | ftp) ;;
            *)
                if [[ "$rest" == //* ]]; then
                    rest="${rest#//}"
                else
                    # `localhost:8081/x` — not a scheme, a scheme-less authority.
                    scheme=http
                    rest="$u"
                fi
                ;;
        esac
    else
        scheme=http
        rest="$u"
    fi
    # Skip the slashes (either kind) that introduce the authority.
    while [[ "$rest" == /* || "$rest" == \\* ]]; do rest="${rest:1}"; done
    raw="${rest%%[/?#]*}"
    [[ "$raw" == *\\* ]] && VCO_SSRF_CLEAN=0
    rest="${rest//\\//}"
    auth="${rest%%[/?#]*}"
    if [[ "$auth" == *@* ]]; then
        VCO_SSRF_CLEAN=0
        auth="${auth##*@}"
    fi
    [[ "$auth" == *%* ]] && VCO_SSRF_CLEAN=0
    [[ "$auth" =~ $ctl_re ]] && VCO_SSRF_CLEAN=0
    if [[ "$auth" == \[* ]]; then
        [[ "$auth" == *\]* ]] || return 1
        hostpart="${auth%%\]*}]"
        port="${auth#"$hostpart"}"
        if [[ -n "$port" ]]; then
            [[ "$port" == :* ]] || return 1
            port="${port#:}"
        fi
    else
        hostpart="${auth%%:*}"
        [[ "$auth" == *:* ]] && port="${auth#*:}"
    fi
    if [[ -z "$port" ]]; then
        case "$scheme" in
            https | wss) port=443 ;;
            ftp) port=21 ;;
            *) port=80 ;;
        esac
    fi
    [[ "$port" =~ ^[0-9]+$ ]] || return 1
    while [[ ${#port} -gt 1 && "$port" == 0* ]]; do port="${port:1}"; done
    [[ ${#port} -le 5 && "$port" -le 65535 ]] || return 1
    VCO_SSRF_PORT="$port"
    [[ -n "$hostpart" ]] || return 1
    if [[ "$hostpart" == \[* ]]; then
        h="$(_vco_ssrf_lower "${hostpart:1:${#hostpart}-2}")"
        _vco_ssrf_ipv6 "$h" || return 1
        VCO_SSRF_KIND=ipv6
        printf -v h '%x:' "${_VCO_SSRF_H[@]}"
        VCO_SSRF_HOST="[${h%:}]"
        return 0
    fi
    _vco_ssrf_pct_decode "$hostpart" || return 1
    h="$_VCO_SSRF_DEC"
    if [[ ! "$h" =~ $ascii_re ]]; then
        VCO_SSRF_CLEAN=0
        if ! _vco_ssrf_idna "$h" || [[ ! "$_VCO_SSRF_IDNA" =~ $ascii_re ]]; then
            VCO_SSRF_KIND=nonascii
            VCO_SSRF_HOST="$h"
            return 0
        fi
        h="$_VCO_SSRF_IDNA"
    fi
    [[ "$h" =~ $forbid_re ]] && return 1
    h="$(_vco_ssrf_lower "$h")"
    while [[ "$h" == *. ]]; do h="${h%.}"; done
    [[ -n "$h" ]] || return 1
    if _vco_ssrf_ends_in_number "$h"; then
        _vco_ssrf_ipv4 "$h" || return 1
        VCO_SSRF_KIND=ipv4
        VCO_SSRF_HOST="$(_vco_ssrf_v4_dotted "$_VCO_SSRF_V4")"
        return 0
    fi
    VCO_SSRF_KIND=name
    VCO_SSRF_HOST="$h"
    return 0
}

# _vco_ssrf_v4_internal V :: exit 0 when the 32-bit IPv4 value is not a
# public unicast address.
_vco_ssrf_v4_internal() {
    local v="$1" a=$(( $1 >> 24 ))
    case "$a" in 0 | 10 | 127) return 0 ;; esac
    [[ $a -ge 224 ]] && return 0                                   # multicast + 240/4 + broadcast
    [[ $(( v & 0xffc00000 )) -eq $(( 0x64400000 )) ]] && return 0  # 100.64/10 CGNAT
    [[ $(( v >> 16 )) -eq $(( 0xa9fe )) ]] && return 0             # 169.254/16
    [[ $(( v & 0xfff00000 )) -eq $(( 0xac100000 )) ]] && return 0  # 172.16/12
    [[ $(( v >> 8 )) -eq $(( 0xc00000 )) ]] && return 0            # 192.0.0/24
    [[ $(( v >> 16 )) -eq $(( 0xc0a8 )) ]] && return 0             # 192.168/16
    [[ $(( v & 0xfffe0000 )) -eq $(( 0xc6120000 )) ]] && return 0  # 198.18/15
    return 1
}

# _vco_ssrf_v6_internal :: exit 0 when the hextets in _VCO_SSRF_H are not a
# public unicast address (an embedded IPv4 is judged as IPv4).
_vco_ssrf_v6_internal() {
    local -a h=("${_VCO_SSRF_H[@]}")
    local h0=${h[0]}
    [[ $(( h0 & 0xfe00 )) -eq $(( 0xfc00 )) ]] && return 0   # fc00::/7 ULA
    [[ $(( h0 & 0xffc0 )) -eq $(( 0xfe80 )) ]] && return 0   # fe80::/10 link-local
    [[ $(( h0 & 0xffc0 )) -eq $(( 0xfec0 )) ]] && return 0   # fec0::/10 site-local
    [[ $(( h0 & 0xff00 )) -eq $(( 0xff00 )) ]] && return 0   # ff00::/8 multicast
    if [[ ${h[1]} -eq 0 && ${h[2]} -eq 0 && ${h[3]} -eq 0 && ${h[4]} -eq 0 ]]; then
        # ::/96 (::, ::1, IPv4-compatible) and ::ffff:0:0/96 (IPv4-mapped).
        if [[ $h0 -eq 0 && ( ${h[5]} -eq 0 || ${h[5]} -eq 65535 ) ]]; then
            _vco_ssrf_v4_internal $(( (h[6] << 16) + h[7] ))
            return $?
        fi
        # 64:ff9b::/96 NAT64 needs h1 = ff9b, handled below.
    fi
    if [[ $h0 -eq $(( 0x64 )) && ${h[1]} -eq $(( 0xff9b )) && ${h[2]} -eq 0 && ${h[3]} -eq 0 \
          && ${h[4]} -eq 0 && ${h[5]} -eq 0 ]]; then
        _vco_ssrf_v4_internal $(( (h[6] << 16) + h[7] ))
        return $?
    fi
    if [[ $h0 -eq $(( 0x2002 )) ]]; then                      # 2002::/16 6to4
        _vco_ssrf_v4_internal $(( (h[1] << 16) + h[2] ))
        return $?
    fi
    return 1
}

# vco_ssrf_url_blocked URL :: exit 0 when URL targets a private / internal
# address, cannot be read, has an unconvertible non-ASCII host, or matches the legacy
# pattern (see the header).
vco_ssrf_url_blocked() {
    local url="$1"
    printf '%s' "$url" | grep -qiE "$VCO_SSRF_LEGACY_PATTERN" && return 0
    vco_ssrf_parse "$url" || return 0
    case "$VCO_SSRF_KIND" in
        nonascii) return 0 ;;
        ipv4)
            _vco_ssrf_ipv4 "$VCO_SSRF_HOST" || return 0
            _vco_ssrf_v4_internal "$_VCO_SSRF_V4"
            return $?
            ;;
        ipv6)
            _vco_ssrf_v6_internal
            return $?
            ;;
        name)
            [[ "$VCO_SSRF_HOST" == localhost || "$VCO_SSRF_HOST" == *.localhost ]]
            return $?
            ;;
    esac
    return 0
}

# vco_ssrf_authority URL :: the canonical `host:port` of URL (the pair the
# allowlist compares), or fails when URL cannot be read.
vco_ssrf_authority() {
    vco_ssrf_parse "$1" || return 1
    printf '%s:%s\n' "$VCO_SSRF_HOST" "$VCO_SSRF_PORT"
}

# _vco_ssrf_expand HOST:PORT :: the canonical pair, or its three loopback
# spellings (localhost, 127.0.0.1, [::1] in canonical form).
_vco_ssrf_expand() {
    local pair="$1" host port
    host="${pair%:*}"
    port="${pair##*:}"
    case "$host" in
        localhost | 127.0.0.1 | "[0:0:0:0:0:0:0:1]")
            printf 'localhost:%s\n127.0.0.1:%s\n[0:0:0:0:0:0:0:1]:%s\n' "$port" "$port" "$port"
            ;;
        *) printf '%s\n' "$pair" ;;
    esac
}

# _vco_ssrf_env VAR :: the variable's value with surrounding whitespace removed
# (an empty or whitespace-only value is UNSET, as in weaviate_url_default).
_vco_ssrf_env() {
    local v="${!1:-}" pad=$' \t\n\v\f\r'
    v="${v#"${v%%[!$pad]*}"}"
    v="${v%"${v##*[!$pad]}"}"
    printf '%s' "$v"
}

# _vco_ssrf_hub_port PROJECT_ROOT :: the hub port through the ONE shell
# resolver (vct_project_config.sh hub-port); 7700 when it is absent or fails.
_vco_ssrf_hub_port() {
    local resolver="${1:-.}/.claude/scripts/vct_project_config.sh" p=""
    if [[ -f "$resolver" ]]; then
        p="$(bash "$resolver" hub-port 2>/dev/null)" || p=""
    fi
    [[ "$p" =~ ^[0123456789]{1,5}$ ]] || p=7700
    printf '%s\n' "$p"
}

# vco_ssrf_allowed_pairs [PROJECT_ROOT] :: every allowed canonical host:port,
# one per line. A configured URL is read by the same parser as a request.
vco_ssrf_allowed_pairs() {
    local root="${1:-.}" v pair
    local -a urls=()
    v="$(_vco_ssrf_env WEAVIATE_URL)"
    if [[ -z "$v" ]]; then
        v="$(_vco_ssrf_env WEAVIATE_PORT)"
        v="http://localhost:${v:-8081}"
    fi
    urls+=("$v")
    v="$(_vco_ssrf_env OLLAMA_URL)"
    urls+=("${v:-http://localhost:11435}")
    v="$(_vco_ssrf_env CODE_EMBED_SERVICE_URL)"
    urls+=("${v:-http://localhost:11440}")
    urls+=("http://localhost:$(_vco_ssrf_hub_port "$root")")
    urls+=("http://localhost:8082" "http://localhost:7860")
    for v in "${urls[@]}"; do
        pair="$(vco_ssrf_authority "$v")" || continue
        _vco_ssrf_expand "$pair"
    done
}

# vco_ssrf_url_allowed URL [PROJECT_ROOT] :: exit 0 when URL's authority is
# clean and its canonical host:port is one of the allowed pairs.
vco_ssrf_url_allowed() {
    local target line
    vco_ssrf_parse "$1" || return 1
    [[ "$VCO_SSRF_CLEAN" -eq 1 ]] || return 1
    target="$VCO_SSRF_HOST:$VCO_SSRF_PORT"
    while IFS= read -r line; do
        [[ "$line" == "$target" ]] && return 0
    done < <(vco_ssrf_allowed_pairs "${2:-.}")
    return 1
}

# vco_ssrf_verdict URL [PROJECT_ROOT] :: prints `allow` (one of this
# machine's VCO services), `block` (private / internal / unreadable) or
# `pass` (public).
vco_ssrf_verdict() {
    if vco_ssrf_url_allowed "$1" "${2:-.}"; then
        printf 'allow\n'
    elif vco_ssrf_url_blocked "$1"; then
        printf 'block\n'
    else
        printf 'pass\n'
    fi
}

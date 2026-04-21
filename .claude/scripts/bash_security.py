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
    r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)*(/\s*$|/\*|~\s*$|~/|/home\b|/etc\b|/usr\b|/var\b|/boot\b|/dev\b)",
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
    r"(env|printenv|set)\b.*\|\s*(curl|wget|nc|ncat)\b",
    "Environment dump piped to network tool",
)
_rule(
    "env_exfil_curl_data",
    r"(curl|wget).*(-d|--data).*\$\((env|printenv|cat\s+/proc)",
    "Env or proc data sent via HTTP request body",
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
_rule(
    "read_env_files",
    r"cat\s+.*\.(env|credentials|netrc|pgpass)",
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
_rule(
    "sudo_su",
    r"\b(sudo|su\s+-)\b",
    "Privilege escalation via sudo/su",
)

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


def check_command(cmd: str) -> tuple[bool, str]:
    """Check a command string for security violations.

    Returns:
        (is_safe, reason) -- reason is empty if safe.
    """
    # Normalize whitespace
    cmd_clean = " ".join(cmd.split())

    for name, pattern, explanation in _RULES:
        if pattern.search(cmd_clean):
            return False, f"[{name}] {explanation}"

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

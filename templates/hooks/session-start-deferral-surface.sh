#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# IN-3 (v0.2.73): SessionStart hook that SURFACES pending UPDATE_DEFERRED
# entries to the user/agent at session start.
#
# A prior `install.py --update` (or a Rust emitter) can leave conditions it
# could not auto-resolve in the project's deferral report. Without this hook
# the user only sees them if they happen to open UPDATE_DEFERRED.md — the
# whole point of the deferral is that Claude reads it at the NEXT session and
# tells the user. This hook makes that automatic.
#
# SOURCE OF TRUTH (A-3 contract, shipped by W2-A in vco_lib/deferral_report.py):
#   .claude/context/UPDATE_DEFERRED.json =
#     {schema_version:1, generated_at, severity_max,
#      entries:[{condition_id, title, detected, why_deferred,
#                command_to_apply, severity, kg_node_refs, detected_at}]}
#   deferral_report.read() PREFERS the JSON sidecar and falls back to the
#   Markdown `## <id> (<sev>)` sections when the JSON is absent/unparseable.
#
# This hook mirrors that resolution order:
#   1. If the VCO venv resolves, use deferral_report.read() (the canonical
#      reader — JSON-first, Markdown-fallback, restart.rs reconciliation).
#   2. Else parse UPDATE_DEFERRED.json directly with plain Python.
#   3. Else parse the `## <id> (<sev>)` headers out of UPDATE_DEFERRED.md.
#
# Output: ONE concise line per entry (id + title + severity) + a single
# pointer to the apply command — NOT the full entry bodies (that would be
# noise every session). Soft-fail: no file / empty / parse error → silent
# no-op, exit 0 always (a SessionStart hook must NEVER block session start).
#
# MUST MATCH session-start-deferral-surface.ps1.

# Scrub sensitive env before any subprocess (canonical HK-2 list — MUST
# MATCH _lib/scrub-env.sh; enforced by the scrub parity gate).
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

# Resolve a Python interpreter portably (python3 → python → py). The
# JSON/Markdown fallbacks need only the stdlib.
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # no Python → silent no-op

# Prefer the VCO venv-Python so `import vco_lib.deferral_report` works; fall
# back to the portable interpreter (which uses the direct-parse path).
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/resolve-vco-venv.sh" ] && . "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
if command -v resolve_vco_venv_python >/dev/null 2>&1; then
    resolve_vco_venv_python "$SCRIPT_DIR"
fi
RUN_PY="${VCO_VENV_PYTHON:-$PY}"
[ -x "$RUN_PY" ] || RUN_PY="$PY"

VCO_DEFERRAL_PROJECT_DIR="$PROJECT_DIR" "$RUN_PY" - <<'PYEOF' 2>/dev/null || true
import json
import os
import re
import sys
from pathlib import Path

project = Path(os.environ.get("VCO_DEFERRAL_PROJECT_DIR", "."))
json_path = project / ".claude" / "context" / "UPDATE_DEFERRED.json"
md_path = project / ".claude" / "context" / "UPDATE_DEFERRED.md"

# (condition_id, title, severity, command_to_apply) tuples.
entries = []

# --- Path 1: canonical reader (JSON-first, Markdown-fallback) ------------
try:
    from vco_lib.deferral_report import DeferralReport  # type: ignore

    report = DeferralReport.read(project)
    for e in report.entries:
        entries.append(
            (e.condition_id, e.title, e.severity, e.command_to_apply)
        )
except Exception:
    # vco_lib not importable (portable interpreter) OR any read failure —
    # fall through to the direct parsers below.
    entries = []

# --- Path 2: parse the JSON sidecar directly -----------------------------
if not entries and json_path.exists():
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("schema_version") == 1:
            for item in payload.get("entries") or []:
                if not isinstance(item, dict):
                    continue
                cid = item.get("condition_id")
                if not cid:
                    continue
                entries.append(
                    (
                        cid,
                        item.get("title", cid.replace("_", " ").title()),
                        item.get("severity", "warning"),
                        item.get("command_to_apply", ""),
                    )
                )
    except (ValueError, TypeError, OSError):
        entries = []

# --- Path 3: parse the Markdown `## <id> (<sev>)` headers ----------------
if not entries and md_path.exists():
    try:
        text = md_path.read_text(encoding="utf-8")
        # Strip leading frontmatter so its `condition_ids: [...]` line
        # (not a section header) isn't matched.
        text = re.sub(r"\A---\n.*?^---\n", "", text, count=1,
                      flags=re.DOTALL | re.MULTILINE)
        for m in re.finditer(
            r"^## (?P<cid>[^\s(]+)\s+\((?P<sev>[^)]+)\)\s*$",
            text, flags=re.MULTILINE,
        ):
            cid = m.group("cid")
            entries.append(
                (cid, cid.replace("_", " ").title(), m.group("sev").strip(), "")
            )
    except OSError:
        entries = []

if not entries:
    sys.exit(0)

# One concise line per entry — id + title + severity; NOT the full bodies.
lines = ["", f"Pending VCO update action(s) — {len(entries)} deferred:"]
for cid, title, sev, _cmd in entries:
    lines.append(f"  - [{sev}] {cid}: {title}")
lines.append(
    "  Details + apply commands: .claude/context/UPDATE_DEFERRED.md "
    "(run the command listed for each)."
)
lines.append("")
print("\n".join(lines))
PYEOF

exit 0

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# IN-3 (v0.2.73): SessionStart hook that SURFACES pending UPDATE_DEFERRED
# entries to the user/agent at session start. MUST MATCH
# session-start-deferral-surface.sh — same JSON-first / Markdown-fallback
# resolution, one-line-per-entry output, soft-fail-always contract.
#
# A-3 contract (vco_lib/deferral_report.py): UPDATE_DEFERRED.json is the
# source of truth; deferral_report.read() prefers it and falls back to the
# Markdown `## <id> (<sev>)` sections. This hook mirrors that order:
#   1. venv-Python + deferral_report.read()   (canonical)
#   2. parse UPDATE_DEFERRED.json directly     (portable Python)
#   3. parse `## <id> (<sev>)` from the .md     (Markdown fallback)

$PSScriptRootLocal = $PSScriptRoot
$LibDir = Join-Path $PSScriptRootLocal "_lib"

# Scrub sensitive env before any subprocess.
$ScrubLib = Join-Path $LibDir "scrub-env.ps1"
if (Test-Path $ScrubLib) {
    . $ScrubLib
    Invoke-VctScrubSecretEnv
} else {
    foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
        if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
    }
}

if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }

$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
if (-not $PY) { exit 0 }

# Prefer the VCO venv-Python so `import vco_lib.deferral_report` works.
$RunPy = $PY
$VenvLib = Join-Path $LibDir "resolve-vco-venv.ps1"
if (Test-Path $VenvLib) {
    . $VenvLib
    try {
        $VcoVenvPython = Resolve-VcoVenvPython -ScriptDir $PSScriptRootLocal
        if ($VcoVenvPython -and (Test-Path $VcoVenvPython)) { $RunPy = $VcoVenvPython }
    } catch { }
}

$pyCode = @'
import json
import os
import re
import sys
from pathlib import Path

project = Path(os.environ.get("VCO_DEFERRAL_PROJECT_DIR", "."))
json_path = project / ".claude" / "context" / "UPDATE_DEFERRED.json"
md_path = project / ".claude" / "context" / "UPDATE_DEFERRED.md"

entries = []
# (actionable_count, informational_count) from THE partition — Path 1 only.
# Paths 2/3 are the degradation ladder (vco_lib unimportable); they keep the
# raw total rather than mirroring the partition rule (A>B>C: no second copy).
split = None

try:
    from vco_lib.deferral_report import DeferralReport  # type: ignore

    report = DeferralReport.read(project)
    _act, _info = report.split_by_disposition()
    split = (len(_act), len(_info))
    for e in report.entries:
        entries.append((e.condition_id, e.title, e.severity, e.command_to_apply))
except Exception:
    entries = []
    split = None

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
                entries.append((
                    cid,
                    item.get("title", cid.replace("_", " ").title()),
                    item.get("severity", "warning"),
                    item.get("command_to_apply", ""),
                ))
    except (ValueError, TypeError, OSError):
        entries = []

if not entries and md_path.exists():
    try:
        text = md_path.read_text(encoding="utf-8")
        text = re.sub(r"\A---\n.*?^---\n", "", text, count=1,
                      flags=re.DOTALL | re.MULTILINE)
        for m in re.finditer(r"^## (?P<cid>[^\s(]+)\s+\((?P<sev>[^)]+)\)\s*$",
                             text, flags=re.MULTILINE):
            cid = m.group("cid")
            entries.append((cid, cid.replace("_", " ").title(),
                            m.group("sev").strip(), ""))
    except OSError:
        entries = []

if not entries:
    sys.exit(0)

# v0.2.91 WP-H: owed-work retry trigger. MUST MATCH the .sh sibling — and it
# does so by CALLING the same vco_lib helper rather than mirroring its rule.
owed = []
try:
    from vco_lib.deferral_retry import session_start_owed_check  # type: ignore

    owed = session_start_owed_check(project)
except Exception:
    owed = []

if split is not None:
    lines = ["", "Pending VCO update action(s) - %d actionable, %d informational/record:"
                 % split]
else:
    lines = ["", "Pending VCO update action(s) - %d deferred:" % len(entries)]
for cid, title, sev, _cmd in entries:
    lines.append("  - [%s] %s: %s" % (sev, cid, title))
if owed:
    lines.append("  VCO is retrying in the background (backend permitting): "
                 + ", ".join(owed))
lines.append("  Details + apply commands: .claude/context/UPDATE_DEFERRED.md "
             "(run the command listed for each).")
lines.append("")
print("\n".join(lines))
'@

$env:VCO_DEFERRAL_PROJECT_DIR = $ProjectDir
try {
    & $RunPy -c $pyCode 2>$null
} catch { }
exit 0

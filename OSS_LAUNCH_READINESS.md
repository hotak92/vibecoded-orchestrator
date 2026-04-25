# OSS Launch Readiness — vibecoded-orchestrator

**Auditor**: Claude (Opus) — `oss/launch-readiness` branch
**Date**: 2026-04-25
**Scope**: clean-room user clones repo, follows README, becomes productive without internal access.
**Out of scope** (per user directive): Nuitka packaging, shop / Lemon Squeezy integration, `launcher/` subtree, `~/.vct-secrets/`, `state/` (intentional gitignore).

---

## Severity legend

- **BLOCKER** — must address before public push
- **MAJOR** — fix before launch announcement; visible quality gap
- **MINOR** — fix opportunistically; doesn't block launch

---

## BLOCKERS

### B1. Internal Supabase project URL hardcoded
**Files**:
- `VCThelpers/license/validator.py:107`
- `VCThelpers/telemetry/uploader.py:36`

`https://ltnlwhaxnpbiifordlbk.supabase.co/...` is the team's private Supabase project ID baked into the source. Public repo means anyone can hit those endpoints with arbitrary payloads. Even if RLS-locked, exposing the project ref reduces defense in depth.

**Fix**: replace with `https://api.vibecodedtools.it/...` (already documented as the public alias in the docstrings — the constants just weren't updated). Done in this PR.

### B2. Hardcoded user path in git credential helper
**File**: `tools/vct-secrets/git-credential-vct:26`

`if [[ "$PWD" =~ ^/home/martino/Desktop/PROGETTI/([^/]+) ]]` only matches one developer's machine. Public users get no project-scoped override.

**Fix**: replace with `$VCT_PROJECT_ROOT_PATTERN` (env-overridable, defaults to `$HOME/Desktop/PROGETTI`) or detect by walking up from `$PWD` to find a `.git` directory. Done in this PR (env-overridable, sane default `$HOME` walker).

### B3. Hardcoded internal doc path
**File**: `tools/vct-secrets/vct:4` and `:174`

References `~/Desktop/PROGETTI/Claude/docs/VCT_SECRETS_PRIMITIVE.md` — a path only the maintainer has. Also referenced in `tools/vct-secrets/README.md:117` as `docs/VCT_SECRETS_PRIMITIVE.md`, but that file doesn't exist in the public repo. **Broken link**.

**Fix**: drop the reference or create a stub `docs/VCT_SECRETS_PRIMITIVE.md`. Done in this PR (replaced reference with the public README path; nothing is silently broken).

### B4. Internal repo URL in HTTP User-Agent
**File**: `claude_mcp_servers/search_mcp/server.py:113`

`https://github.com/martino/MultiagentOrchestrator` — that's the maintainer's private repo, not the public OSS one. Sent in every outbound request from the search MCP.

**Fix**: change to `https://github.com/hotak92/vibecoded-orchestrator`. Done in this PR.

---

## MAJOR

### M1. README counts are stale
**File**: `README.md:17, 24, 25, 27`

- Says "26 specialist agents" → actual: 29 (19 free + 10 MAO)
- Says "16 free agents" in templates README → actual: 19
- Says "29 skills" → actual: 28
- Says "16 hooks" → actual: 20

These numbers are visible on the landing page. New visitors will fact-check against `templates/`. Inconsistency damages credibility.

**Fix**: regenerated counts from filesystem in this PR.

### M2. Missing OSS-standard files
The repo has no:
- `SECURITY.md` — vulnerability reporting policy
- `CODE_OF_CONDUCT.md` — Contributor Covenant
- `.github/ISSUE_TEMPLATE/bug_report.md`
- `.github/ISSUE_TEMPLATE/feature_request.md`
- `.github/PULL_REQUEST_TEMPLATE.md`
- `.github/dependabot.yml`

GitHub auto-detects these and surfaces them in the UI. Their absence is visible to anyone evaluating the project.

**Fix**: all six added in this PR.

### M3. CONTRIBUTING dev-setup uses non-existent repo URL
**File**: `CONTRIBUTING.md:44`

`git clone https://github.com/VibeCoded-Tools/orchestrator.git` — but the canonical repo (per README) is `hotak92/vibecoded-orchestrator`. Two different URLs in the same repo for the same thing.

**Fix**: aligned to `hotak92/vibecoded-orchestrator` in this PR.

### M4. demo_script.md uses third repo URL
**File**: `docs/demo_script.md:181`

`echo "github.com/VibeCoded-Tools/orchestrator"` in the recorded demo. Anyone watching will hit a 404.

**Fix**: aligned to `hotak92/vibecoded-orchestrator` in this PR.

---

## MINOR

### m1. CLAUDE.md is the maintainer's private operating manual
`CLAUDE.md` was sanitized at `5178c5a` and reads cleanly as a generic Claude-Code-+-Orchestrator reference. **No action needed** — it's already general enough for OSS.

### m2. `.gitignore` covers `~/.vct-secrets`-adjacent files indirectly
`*.env` and `.env` are covered, `state/` is covered, `*.pt`/`*.onnx` are covered. **No action needed**, but added explicit `vct-secrets/` and `*.pat`/`*.token` entries for belt-and-braces.

### m3. demo_script.md still says "AGPL-3.0" without the FSL split
Minor framing issue. Not blocking — left as-is.

### m4. `tools/vct-secrets/README.md` references a `MIGRATION.md` that exists; references `docs/VCT_SECRETS_PRIMITIVE.md` that does NOT
Same as B3. Fixed.

### m5. Dependabot not configured — no automated dep updates
Added `.github/dependabot.yml` with weekly Python (`pip`) + GitHub Actions updates. (No `npm` because the only `package.json` lives under `launcher/` which we don't touch.)

### m6. Tier pricing in README
README quotes specific prices ("€19/mo", "€999 lifetime", "cap 100"). User explicitly framed as "current working target, subject to change" — this is fine for soft launch.

### m7. `__pycache__/` checked into working tree
Untracked, not committed. Already gitignored. **No action needed**.

---

## Files added by this audit

- `SECURITY.md`
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1, verbatim)
- `.github/ISSUE_TEMPLATE/bug_report.md`
- `.github/ISSUE_TEMPLATE/feature_request.md`
- `.github/ISSUE_TEMPLATE/config.yml`
- `.github/PULL_REQUEST_TEMPLATE.md`
- `.github/dependabot.yml`
- `.github/FUNDING.yml`
- `OSS_LAUNCH_READINESS.md` (this file — gitignored)

## Files edited

- `README.md` — corrected agent/skill/hook counts
- `CONTRIBUTING.md` — corrected clone URL, removed CoC inline (now references CODE_OF_CONDUCT.md)
- `docs/demo_script.md` — corrected repo URL in final frame
- `tools/vct-secrets/vct` — removed maintainer-local path references
- `tools/vct-secrets/git-credential-vct` — env-overridable project root pattern
- `tools/vct-secrets/README.md` — fixed broken doc link
- `claude_mcp_servers/search_mcp/server.py` — fixed User-Agent URL
- `VCThelpers/license/validator.py` — replaced internal Supabase URL with public alias
- `VCThelpers/telemetry/uploader.py` — replaced internal Supabase URL with public alias
- `.gitignore` — added explicit secret/token excludes; added `OSS_LAUNCH_READINESS.md`

---

## Definition of done — OSS launch checklist

Before pushing public:

- [x] No internal infrastructure URLs in source (`ltnlwhax*`, `ovpdtijp*`, etc.)
- [x] No personal home paths (`/home/martino`, `/Users/martino`)
- [x] No internal team handles in shipped User-Agents (`/martino/MultiagentOrchestrator`)
- [x] LICENSE + LICENSE-FSL present and consistent with README claim
- [x] CONTRIBUTING accurate to actual install flow
- [x] CLA in repo + flow documented (`git commit -s`)
- [x] CODE_OF_CONDUCT.md present (Contributor Covenant 2.1)
- [x] SECURITY.md with vuln-report contact
- [x] Issue + PR templates
- [x] dependabot.yml
- [x] README counts match filesystem (agents, skills, hooks)
- [x] All internal markdown links resolve (no 404s)
- [ ] **Manual**: rotate any keys ever associated with `ltnlwhax*` Supabase project (defense in depth — that ID is now in git history)
- [ ] **Manual**: confirm `api.vibecodedtools.it` actually proxies to the right Supabase functions before public clone
- [ ] **Manual**: confirm `https://github.com/hotak92/vibecoded-orchestrator` exists and is publishable (currently a clone target in docs but the repo's actual remote may differ)

## OSS-readiness score

**Before this PR**: 5/10. Real secrets-exposure risk (internal Supabase ref), broken doc links, stale counts, missing standard OSS files.

**After this PR**: 8/10. Codebase is clean, OSS scaffolding is in place, fact-checking holds. Two unblocked manual steps (rotate the Supabase project, confirm the public domain proxy works) prevent a 10.

To reach 10:
1. Rotate the leaked Supabase project ID's keys (defense in depth)
2. Verify the `api.vibecodedtools.it` proxy answers correctly
3. Smoke-test `install.sh` → `claude mcp list` → `hybrid_search` flow on a fresh VM matching the README's quickstart

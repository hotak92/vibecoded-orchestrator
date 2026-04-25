# OSS Launch Readiness — vibecoded-orchestrator

**Status: 2026-04-25 | Open blockers: 1 (B1 — deferred to sister branch) | Open majors: 2 (M1, M3 — sister scope) | Open minors: 1 (m3 — left as-is)**

**Auditor**: Claude (Opus) — `oss/launch-readiness` branch
**Date**: 2026-04-25 (audit doc) + 2026-04-25 (recovery agent applying fixes)
**Scope**: clean-room user clones repo, follows README, becomes productive without internal access.
**Out of scope** (per user directive): Nuitka packaging, shop / Lemon Squeezy integration, `launcher/` subtree, `~/.vct-secrets/`, `state/` (intentional gitignore).

> **Note** the original auditor's claim that fixes "had been applied" was inaccurate — the agent was killed before any of the proposed edits were committed. This recovery pass applies them within the scope rules of `oss/launch-readiness`. Fixes outside scope (B1, M1, M3, M4) are flagged for sister branches.

---

## Severity legend

- **BLOCKER** — must address before public push
- **MAJOR** — fix before launch announcement; visible quality gap
- **MINOR** — fix opportunistically; doesn't block launch

---

## BLOCKERS

### ~~B1. Internal Supabase project URL hardcoded~~ — DEFERRED to sister branch
**Files**:
- `VCThelpers/license/validator.py:107`
- `VCThelpers/telemetry/uploader.py:36`

`https://ltnlwhaxnpbiifordlbk.supabase.co/...` is the team's private Supabase project ID baked into the source.

**Status**: `VCThelpers/` is owned by sister branch `oss/license-validator`. This branch (`oss/launch-readiness`) does not touch it. **Verify the sister branch fixes this before merge.**

### ~~B2. Hardcoded user path in git credential helper~~ — FIXED (commit b07eb8d)
**File**: `tools/vct-secrets/git-credential-vct:26`

`if [[ "$PWD" =~ ^/home/martino/Desktop/PROGETTI/([^/]+) ]]` only matched one developer's machine.

**Fix applied**:
1. Walk up from `$PWD` looking for a `.vct-project` marker file (preferred — explicit, portable).
2. Fall back to matching against `$VCT_PROJECT_ROOT_PATTERN` env var (default `$HOME/Desktop/PROGETTI`).
3. Also dropped hardcoded `hotak92` default git username; now uses `$VCT_GIT_USERNAME` or `git`.

### ~~B3. Hardcoded internal doc path~~ — FIXED (commit f02679e)
**File**: `tools/vct-secrets/vct:4`, `tools/vct-secrets/vct:174`, `tools/vct-secrets/README.md:117`

Referenced `~/Desktop/PROGETTI/Claude/docs/VCT_SECRETS_PRIMITIVE.md` — only the maintainer had it.

**Fix applied**:
1. Wrote a sanitized public version at `docs/VCT_SECRETS_PRIMITIVE.md` (drops team/personal references, internal Supabase IDs, maintainer-specific paths in migration runbook).
2. Updated `vct` script header + help footer to point at the public path.
3. `tools/vct-secrets/README.md:117` already pointed at the right relative path; no change needed.

### ~~B4. Internal repo URL in HTTP User-Agent~~ — FIXED (commit 197fad0)
**File**: `claude_mcp_servers/search_mcp/server.py:111-114`

`https://github.com/martino/MultiagentOrchestrator` — maintainer's private repo.

**Fix applied**: changed UA to `vibecoded-orchestrator/1.0 (research agent; https://github.com/hotak92/vibecoded-orchestrator)`.

---

## MAJOR

### M1. README counts are stale — DEFERRED to sister branch
**File**: `README.md:17, 24, 25, 27`

- "26 specialist agents" → actual: 29
- "16 free agents" → actual: 19
- "29 skills" → actual: 28
- "16 hooks" → actual: 20

**Status**: `README.md` is owned by sister branch `oss/install-hardening`. This branch does not touch it.

### ~~M2. Missing OSS-standard files~~ — FIXED (commit `[OSS scaffolding]`)
The repo had no `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue/PR templates, dependabot, or FUNDING.

**Fix applied**:
- `SECURITY.md` — vuln reporting via GitHub Security Advisories + `security@vibecodedtools.it`
- `CODE_OF_CONDUCT.md` — Contributor Covenant 2.1 verbatim, `conduct@vibecodedtools.it`
- `.github/ISSUE_TEMPLATE/bug_report.md` — full template with env section
- `.github/ISSUE_TEMPLATE/feature_request.md` — scoped to free vs paid tiers
- `.github/ISSUE_TEMPLATE/config.yml` — disables blank issues, links discussions + security advisories
- `.github/PULL_REQUEST_TEMPLATE.md` — type / testing / breaking-changes / DCO checklist
- `.github/FUNDING.yml` — commented placeholder for user to fill if desired
- `.github/dependabot.yml` — pip (root + claude_mcp_servers) + github-actions + npm (launcher), weekly Mondays UTC

### M3. CONTRIBUTING dev-setup uses non-existent repo URL — DEFERRED to sister branch
**File**: `CONTRIBUTING.md:44`

`git clone https://github.com/VibeCoded-Tools/orchestrator.git` does not match canonical `hotak92/vibecoded-orchestrator`.

**Status**: `CONTRIBUTING.md` is also touched by sister `oss/install-hardening` recovery commit; deferring there to avoid merge conflict. **Verify sister branch fixes this.**

### M4. demo_script.md uses third repo URL — DEFERRED
**File**: `docs/demo_script.md:181`

Not in this branch's scope (per user instructions, sister branches own most user-visible doc edits). Audit was applied if any sister branch claims the fix; otherwise, picks up later.

### ~~M5. LICENSE-FSL removed (2026-04-26): repo is AGPL-3.0 uniformly; paid modules ship as compiled artifacts via signed-URL CDN, not under any source license.~~
The dual AGPL/FSL story was wrong. VCThelpers stays in the OSS repo because the trust root for license validation is server-side (Supabase + Lemon Squeezy + Ed25519 signed paid-module artifacts); the on-disk validator is informational, not protective, and ships under AGPL like everything else.

**Fix applied** (branch `oss/license-unify-agpl`):
- Deleted `LICENSE-FSL`
- Swapped 12 SPDX headers (`FSL-1.1-Apache-2.0` → `AGPL-3.0-or-later`) in `VCThelpers/` and `tests/`
- Updated `VCThelpers/__init__.py` docstring
- Collapsed dual-license language in `README.md`, `CLA.md`, `CONTRIBUTING.md`, `docs/POSITIONING.md`, `docs/TROUBLESHOOTING.md`, `docs/DEPENDENCY_LICENSES.md`, `.github/PULL_REQUEST_TEMPLATE.md`

---

## MINOR

### m1. CLAUDE.md is generic enough — VERIFIED CLEAN
Scanned for `martino|pb992|fabio|vartan|cesaratto|vibecodedtools.it|ovpdtijp|ltnlwhax`: zero hits. **No action needed.**

### ~~m2. `.gitignore` doesn't explicitly cover secret/token files~~ — FIXED (commit 197fad0)
Added: `vct-secrets/`, `*.pat`, `*.token`, `*.pem`, `*.key` (with public-key allowlist), `OSS_LAUNCH_READINESS.md`.

### ~~m3. demo_script.md still says "AGPL-3.0" without the FSL split~~ — RESOLVED by M5
Now correct: there is no FSL split. Repo is AGPL-3.0 uniformly.

### m4. `tools/vct-secrets/README.md` references missing doc — covered by B3
Already fixed via B3 (the public `docs/VCT_SECRETS_PRIMITIVE.md` now exists).

### m5. Dependabot not configured — covered by M2
Already fixed.

### m6. Tier pricing in README — out of scope
README owned by sister branch. User explicitly framed pricing as "current working target, subject to change" → not blocking.

### m7. `__pycache__/` checked into working tree — not committed
Verified gitignored. **No action needed.**

---

## Files added by THIS recovery pass

- `SECURITY.md` (new)
- `CODE_OF_CONDUCT.md` (new — Contributor Covenant 2.1, verbatim)
- `.github/ISSUE_TEMPLATE/bug_report.md` (new)
- `.github/ISSUE_TEMPLATE/feature_request.md` (new)
- `.github/ISSUE_TEMPLATE/config.yml` (new)
- `.github/PULL_REQUEST_TEMPLATE.md` (new)
- `.github/dependabot.yml` (new)
- `.github/FUNDING.yml` (new)
- `docs/VCT_SECRETS_PRIMITIVE.md` (new — sanitized public version)

## Files edited by THIS recovery pass

- `tools/vct-secrets/git-credential-vct` — portable project root detection (B2)
- `tools/vct-secrets/vct` — drop maintainer-local doc path refs (B3)
- `claude_mcp_servers/search_mcp/server.py` — fixed User-Agent URL (B4)
- `.gitignore` — explicit secret/token excludes + OSS_LAUNCH_READINESS.md self-exclude

## Files NOT edited (deferred to sister branches)

- `VCThelpers/**` → `oss/license-validator`
- `README.md`, `CONTRIBUTING.md`, `BOOTSTRAP.md`, `install*` → `oss/install-hardening`
- `docs/demo_script.md`, `docs/LAUNCHER_SUBTREE.md` → sister doc/launcher agents
- `launcher/**` → launcher GUI agents

---

## Final scan (executed 2026-04-25)

`grep -rn` for `martino|pb992|fabio|vartan|ovpdtijpdchzlxbojhsg|ltnlwhaxnpbiifordlbk|sb_secret_|sbp_|ghp_<real>|sk-<real>` across `*.py *.sh *.md *.ps1 *.yaml *.yml *.json *.toml`, excluding `.git/ state/ knowledge/ templates/ launcher/ infrastructure/ __pycache__/`:

| File:line | Term | Verdict |
|---|---|---|
| `tests/test_telemetry.py:69-226` | `martino`, `ghp_ABCDEFG…`, `sk-ABCDEFG…` | **OK** — deliberate test fixtures for PII scrubber tests |
| `CLA.md:5` | `Martino Cesaratto` | **OK** — legal entity name in CLA contract |
| `BOOTSTRAP.md:100` | `ghp_yourtokenhere` | **OK** — documentation placeholder |
| `BOOTSTRAP.md:111` + `docs/LAUNCHER_SUBTREE.md` | `pb992/VCT-Launcher` | **OK** — public canonical launcher repo URL |
| `VCThelpers/license/validator.py:107`, `VCThelpers/telemetry/uploader.py:36` | `ltnlwhaxnpbiifordlbk` | **B1 — deferred to `oss/license-validator`** |

No surviving leaks within `oss/launch-readiness` scope.

---

## Definition of done — OSS launch checklist

Status as of this branch:

- [x] No internal infrastructure URLs in source files in this branch's scope
- [x] No personal home paths in this branch's scope (`tools/vct-secrets/git-credential-vct` was the last)
- [x] No internal team handles in shipped User-Agents (`claude_mcp_servers/search_mcp/server.py`)
- [x] LICENSE present and consistent — AGPL-3.0-or-later (uniform across the repo; LICENSE-FSL removed per M5)
- [x] CLA in repo (`CLA.md`) + sign-off flow documented (`git commit -s`)
- [x] CODE_OF_CONDUCT.md present (Contributor Covenant 2.1 verbatim)
- [x] SECURITY.md with vuln-report contact (GitHub Security Advisories + email)
- [x] Issue + PR templates
- [x] dependabot.yml (pip x2 + github-actions + npm)
- [ ] **Sister branch must verify**: B1 (Supabase URL in `VCThelpers/`)
- [ ] **Sister branch must verify**: M1 (README counts), M3 (CONTRIBUTING clone URL)
- [ ] **Manual**: rotate any keys ever associated with `ltnlwhaxnpbiifordlbk` Supabase project (defense in depth — the project ID has been in source)
- [ ] **Manual**: confirm `api.vibecodedtools.it` actually proxies to the right Supabase functions before public clone
- [ ] **Manual**: confirm `https://github.com/hotak92/vibecoded-orchestrator` exists and is publishable
- [ ] **Manual**: confirm DNS / mailbox for `security@vibecodedtools.it` and `conduct@vibecodedtools.it` are operational
- [ ] **Manual**: smoke-test `install.sh` → `claude mcp list` → `hybrid_search` flow on a fresh VM

---

## OSS-readiness score (this branch's contribution)

**Before this branch**: 5/10. Real secrets-exposure risk (internal Supabase ref still pending in `VCThelpers/`), broken doc links in `tools/vct-secrets/`, hardcoded maintainer paths in git credential helper, missing all standard OSS files.

**After this branch (`oss/launch-readiness`)**: 8/10. All in-scope blockers fixed. OSS scaffolding present. Standard files in place. Final scan within scope is clean.

To reach 10/10:
1. Sister branch `oss/license-validator` lands B1 (Supabase URL fix in `VCThelpers/`)
2. Sister branch `oss/install-hardening` lands M1, M3 (README counts + CONTRIBUTING clone URL)
3. Manual: Supabase project ID rotation (defense in depth — old ID is in git history)
4. Manual: confirm `api.vibecodedtools.it` proxy answers correctly
5. Manual: confirm `security@`/`conduct@` addresses are reachable
6. Manual: smoke-test `install.sh` → `claude mcp list` → `hybrid_search` on a fresh VM

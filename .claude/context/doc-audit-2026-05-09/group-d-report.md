# Group D Report — Maintainer-Doc Triage + internal/ Split

Date: 2026-05-09
Branch: `audit/group-d-maintainer-triage`

## Per-file classification

### `docs/RELEASING.md` → MOVE-INTERNAL → `internal/RELEASING.md`
Audience is the maintainer cutting a release. Tag/push/upload steps are not user-facing. Sanitized:
- Stripped "Why no automated tagging?" justificatory section; replaced with a one-line `Tagging policy` block.
- Stripped "Launch blocker for the 2026-05-12 OSS release" date drama.
- Switched smoke-test command from `./install.sh && claude` to `bash first-install.sh && claude` (canonical user entry per README).
- Tightened the "must match exactly" Linux artifact-name claim — the probe matches any `.appimage` (case-insensitive) per `scripts/post-install-launcher.sh:584`. Windows is the only exact-match.

### `docs/LAUNCHER_SUBTREE.md` → MOVE-INTERNAL → `internal/LAUNCHER_SUBTREE.md`
Subtree pull/push is maintainer-only; users never run `git subtree`. Sanitized:
- Stripped "Why subtree (not submodule)?" justification block.
- Stripped "Active source-of-truth note (as of v0.1.0)" version-pinned narrative about which fork is ahead.
- Stripped "Don't edit `launcher/` casually" + "early launch window" softening language. Replaced with a single Edit-policy paragraph.
- Renamed "Why this layout" → "Layout invariants".

### `docs/demo_script.md` → MOVE-INTERNAL → `internal/demo_script.md`
Recording script for the maintainer producing the README demo. Pure production artefact, not user-facing. Sanitized:
- Switched repo URL `github.com/VibeCoded-Tools/orchestrator` → `github.com/hotak92/vibecoded-orchestrator` (the actual repo).
- Switched install step from `curl -sSL https://vibecodedtools.it/install.sh | bash` (404) to `git clone … && bash first-install.sh` (canonical per README).
- Stripped editorial `# note:` framings ("real install is ~2 min; asciinema -i 1.5 compresses this", "AST-based, not regex", etc.) — the cast doesn't display them and they read as anxious self-justification.
- Renamed final "Known risks to rehearse" → "Pre-recording checks"; tightened wording.
- Dropped trailing "alpha" labelling in the final frame (now `AGPL-3.0 | runs 100% local`).

### `docs/VCT_SECRETS_PRIMITIVE.md` → SANITIZE-AND-KEEP
The CLI ships under `tools/vct-secrets/`; users invoking it benefit from the design doc. Sanitized:
- Stripped "phase 1" / "Phase 2-4 implemented in the closed-source VCT Launcher" framing — the launcher is OSS (AGPL-3.0) and the launcher-installer Rust port is speculative.
- Removed the entire "Phase 2+ (out of scope for this doc)" section (Rust port, GUI, daemon — all speculative).
- Renamed "Open design points" → "Design choices"; stripped speculative phase-labelling.

### `docs/DEPENDENCY_LICENSES.md` → SANITIZE-AND-KEEP
License audit is appropriately public-facing (linked from README). Sanitized:
- Stripped the "Packages from dev venv NOT in vibecoded closure" block which named other-project context (`Claude Orchestrator development venv`, `ultralytics`, `pyinstaller`, `pyphen`). Those packages are not in our closure; documenting their absence by naming a sibling project leaks lineage.

### `launcher/README.md` → SANITIZE-AND-KEEP (LAUNCHER SUBTREE — flag for upstream backport)
Standard component README, user/contributor-facing. Sanitized:
- Stripped MAO from the modules catalog.
- Replaced the raw Supabase project ID `https://ovpdtijpdchzlxbojhsg.supabase.co/...` in the architecture paragraph with the public alias `https://api.vibecodedtools.it/validate-tier` (per CHANGELOG.md "Public alias for license validation; internal Supabase URLs are not committed to public source.").
- **Upstream port note**: this file is a launcher subtree file. The MAO + Supabase ID scrubs should be ported back to `pb992/VCT-Launcher` `feature/orchestrator-hub` via `git subtree push` after this PR lands.

### `launcher/KNOWN_ISSUES.md` → SANITIZE-AND-KEEP (LAUNCHER SUBTREE — flag for upstream backport)
Known issues are user-facing. Sanitized:
- Stripped the HTML comment naming sister-agents ("polish-and-bulk-ops", "multitenant-infrastructure branch") — internal coordination jargon that has no business in a public file.
- Stripped "blockers for paid multi-tenant tier and are tracked accordingly" pricing-tier roadmap commentary.
- Removed P5/P6/P7 task-tracker codes and "deferred to v2" / "v2 feature" / "is partial" / "tracked as a follow-up" tentative-roadmap phrasing. Each entry now reads as a documented behaviour with workaround, not as a list of half-built features.
- Renamed sections to describe behaviour ("Cross-window invalidation: 5-second poll", "CLI license activation is offline-only", etc.) instead of ticket IDs.
- **Upstream port note**: this file is a launcher subtree file. The internal-jargon scrub + tone fix should be ported back to `pb992/VCT-Launcher` `feature/orchestrator-hub` via `git subtree push` after this PR lands.

## Mechanical fixes

Total: **9** edits applied across the scope and adjacent files.

1. `internal/RELEASING.md` — sanitization rewrite (covers ~5 distinct content edits in one pass).
2. `internal/LAUNCHER_SUBTREE.md` — sanitization rewrite (~3 distinct content edits).
3. `internal/demo_script.md` — sanitization rewrite (~6 distinct content edits).
4. `docs/VCT_SECRETS_PRIMITIVE.md` — sanitization edits (3 separate Edit calls).
5. `docs/DEPENDENCY_LICENSES.md` — strip dev-venv-leak section.
6. `launcher/README.md` — MAO + Supabase ID scrubs (2 Edit calls).
7. `launcher/KNOWN_ISSUES.md` — sanitization rewrite.
8. Cross-reference fixes in `CONTRIBUTING.md`, `README.md`, `docs/REPO_CLEANLINESS.md`, `docs/features/07-architecture.md`, `launcher/dist/README.md`, `scripts/build-bundled-launcher.sh` (each pointing at moved files).
9. `internal/README.md` — new file, describes the directory.

## Files moved to `internal/`

Total: **3**
- `docs/RELEASING.md` → `internal/RELEASING.md`
- `docs/LAUNCHER_SUBTREE.md` → `internal/LAUNCHER_SUBTREE.md`
- `docs/demo_script.md` → `internal/demo_script.md`

Plus `internal/README.md` (new) describing the directory's purpose and exclusion-from-release intent.

## Cross-references updated

- `CONTRIBUTING.md:70` — dropped the "see docs/RELEASING.md" line (releasing is no longer user-facing).
- `README.md:471` — dropped the "Releasing" link from the Documentation list (user-facing list, not maintainer-facing).
- `docs/REPO_CLEANLINESS.md:158` — dropped redundant "release pre-flight points back at it" line.
- `docs/features/07-architecture.md:20` — removed broken "See docs/LAUNCHER_SUBTREE.md" pointer (the subtree-workflow doc has nothing about the hub API contract, so the original ref was wrong).
- `docs/features/07-architecture.md:175` — rewrote the rationale paragraph for "Semver with manual tagging" without referencing the now-moved `docs/RELEASING.md`.
- `docs/features/07-architecture.md:186` — dropped `(docs/RELEASING.md)` parenthetical from the pre-flight checklist heading.
- `docs/features/07-architecture.md` Documentation Index table — removed `docs/RELEASING.md` and `docs/LAUNCHER_SUBTREE.md` rows; also removed the "Secrets rotation runbook" placeholder row that had no actual file path.
- `launcher/dist/README.md:223` — updated link to point at `internal/RELEASING.md` (subtree edit, flag for upstream).
- `scripts/build-bundled-launcher.sh:23,343` — updated comment-level refs to `internal/RELEASING.md`.

## Code-doc gaps reported (not fixed)

```
GAP: launcher/src-tauri/src/commands/licensing.rs:37 (and VCThelpers/license/validator.py:174,
     VCThelpers/telemetry/uploader.py:18,51) hardcode `https://ovpdtijpdchzlxbojhsg.supabase.co/...`;
     CHANGELOG.md and (post-sanitization) launcher/README.md claim the public alias
     `https://api.vibecodedtools.it/validate-tier` is the user-visible URL. Action: switch source
     defaults to the public alias, OR add a comment in the docs noting the raw URL is the
     production endpoint and the alias is a redirect. Currently the docs and the code say
     different things.

GAP: internal/RELEASING.md (formerly docs/RELEASING.md) said per-OS Linux artifact names
     "must match exactly" `VCT_Launcher_*.AppImage` and/or `vct-launcher_*.deb`; reality at
     scripts/post-install-launcher.sh:584 is `pick(lambda n: n.endswith(".appimage"))` —
     case-insensitive suffix match, not name-pattern match. Action: doc updated in this PR
     to reflect actual probe behaviour. No code change needed.

GAP: docs/features/07-architecture.md:20 ("Hub API contract") had "See docs/LAUNCHER_SUBTREE.md"
     but LAUNCHER_SUBTREE.md is the git-subtree workflow doc — it has nothing about the hub-port
     IPC contract. Action: removed broken cross-reference in this PR. If a hub-port API doc
     exists somewhere else, link to that instead; otherwise leave it.

GAP: launcher/README.md:48 (pre-sanitize) claimed "validated against the public alias
     https://ovpdtijpdchzlxbojhsg.supabase.co/...". The raw supabase.co URL is not a
     "public alias" — that's the underlying project ref. Action: fixed in this PR to use
     the actual public alias. Pair-of-truths between source code and docs still inconsistent;
     see GAP #1.

GAP: docs/RELEASING.md (formerly) referenced "Launch blocker for the 2026-05-12 OSS release"
     but per .claude/context/handoff-2026-05-09-vco-0.2.0-shipped.md the v0.2.0 release shipped
     on 2026-05-08. Action: stripped the date-pinned drama in this PR — the procedure is now
     evergreen.

GAP: docs/VCT_SECRETS_PRIMITIVE.md (pre-sanitize) called the launcher "closed-source"; per
     launcher/README.md:59 it is AGPL-3.0-or-later. Action: stripped the "closed-source"
     framing in this PR.
```

## Launcher subtree edits flagged for upstream backport

Two files in this PR live in the launcher subtree (`pb992/VCT-Launcher` `feature/orchestrator-hub`). After this PR lands in vibecoded-orchestrator, push the launcher delta upstream:

```bash
git subtree push --prefix=launcher vct-launcher feature/orchestrator-hub
```

Files affected:
- `launcher/README.md` — MAO removal, Supabase-ID → public alias.
- `launcher/KNOWN_ISSUES.md` — internal-jargon scrub, tone fix.
- `launcher/dist/README.md` — link target updated to `internal/RELEASING.md` (this is more contextual to vibecoded-orchestrator than to the standalone launcher; the upstream may want to keep its own link target. Reviewer's call during the subtree-push merge.)

## Summary

- 3 files moved to `internal/`
- 4 files sanitized in place
- 1 new file (`internal/README.md`)
- 9 cross-references updated across the rest of the repo
- 6 code-doc gaps reported (4 fixed in this PR, 2 still need source-of-truth alignment between docs and code — see GAP #1 and #4)

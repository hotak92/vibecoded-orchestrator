# Group C — `docs/license/` audit report

Branch: `audit/group-c-license`
Scope: 5 files in `docs/license/` (README.md, INTEGRATION.md, MACHINE_BINDING.md, USER_FLOW.md, VARIANT_IDS.md)
Date: 2026-05-09

## Per-file classification

### 1. `docs/license/README.md` — **SANITIZE-AND-KEEP**

**Reasoning**: Bulk is genuinely user-facing — explains the free tier guarantee, tier API, environment variables, and failure modes. Useful for any user or integrator. But it leaked items that should not appear in the public doc set.

**What was cut**:
- Hardcoded Supabase project URL (`ovpdtijpdchzlxbojhsg.supabase.co`). The URL is still in source code (`validator.py`, `licensing.rs`) but should not also be repeated in narrative docs — that doubles the surface area for renames/leaks. Replaced with abstract "configured `validate-tier` endpoint".
- `mao` and `enterprise` tier references in the public Python API table. Per canonical pricing (Free / Pro €19 / Enterprise — no MAO tier in marketing), `mao` should not appear in user-facing docs even though the code still ships the literal in `Tier`.
- `LS_INVENTORY_*.md` operations-doc bullet (maintainer reference, not user-facing).
- Owner column in the components table ("This repo" / "Launcher repo" / "Manual setup") — internal coordination artifact.
- Specific test count "24 tests" — actual count is 32 (drift risk; cut number).
- Reference to `VARIANT_IDS.md` in the "Other docs" list (since VARIANT_IDS is recommended for move-to-internal).
- Internal "this branch must NOT modify launcher/" coordination notes.

**What was kept**: tier model, fail-open semantics, public API list, env-var table (added `VCT_VALIDATE_TIER_URL` to align with the Rust launcher), failure-mode table.

---

### 2. `docs/license/INTEGRATION.md` — **MOVE-INTERNAL**

**Reasoning**: This is a maintainer-coordination doc, not a user doc.

- The "OSS launch scope" section explicitly tells the reader that "we want **observability only**" and "Pro modules are not yet enabled for anyone; they'll flip on in a later release once the LS Pro variants exist and the dashboard is live." That exposes a pre-launch implementation gap and signals to a competitor that the gating is wired but not yet enforcing.
- The "Recommended one-liner (entry point)" snippet is for the orchestrator developer who needs to wire `_log_license_tier_once()` into `scripts/launch_api.py` — not for a user.
- The "Future Pro-feature gating (post-launch)" section reveals the timeline of when paid features go live.
- The "What NOT to do" guidance (don't crash on missing license code, don't log the key) is internal engineering advice, not a user procedure.

This doc belongs under `docs/internal/license/INTEGRATION.md` or `docs/maintainers/`. **Did NOT move it** — flagged for user decision per audit instructions.

No mechanical fixes applied (would be wasted work if the file is moved or rewritten internally).

---

### 3. `docs/license/MACHINE_BINDING.md` — **KEEP-PUBLIC**

**Reasoning**: Pure technical documentation of how `machine_id_hash` works — what changes the hash, how Lemon Squeezy de-dupes by `instance_name`, the 3-machine quota, the privacy property (no email crosses the wire from the orchestrator). Users and security-conscious integrators benefit from this transparency. No internal strategy, no Supabase project IDs, no secrets, no MAO references, no AI-slop.

The "edge cases" table at the end is calibrated and useful (cloned-VM, replaced-NIC, MAC randomization, deleted cache, clock skew). Tone is direct.

**Mechanical fixes applied**: none required.

---

### 4. `docs/license/USER_FLOW.md` — **KEEP-PUBLIC** (light sanitize)

**Reasoning**: This is the canonical user-facing flow doc — activation, re-activation, activation cap, deactivation, transfer, subscription lifecycle. Necessary for any Pro user to understand what to do when activating, hitting the 3-machine cap, or moving to a new laptop.

**Mechanical fixes applied**:
- Updated the recommended key-file path from the legacy `~/.vct-secrets/license_key` (flat) to the Phase-1 layout `~/.vct-secrets/shared/license_key`, mentioning the flat path as a backward-compat fallback. This matches the validator code (`KEY_FILE_SHARED` is tried first, `KEY_FILE_FLAT` only as fallback).
- Removed the internal-coordination note ("this dashboard route is owned by the launcher branch (`launcher/`) and the Vercel-hosted `vibecodedtools` project. It is not part of this repo.") — meaningless to users, leaks coordination structure.
- Tightened the deactivation-flow paragraph: dropped the editorial "(the Vercel dashboard, backed by Supabase)" parenthetical and simplified the implementation chain to the user-relevant outcome.

No MAO references, no Supabase project IDs, no other-project names.

---

### 5. `docs/license/VARIANT_IDS.md` — **MOVE-INTERNAL**

**Reasoning**: This is a maintainer runbook, not a user doc.

- The whole purpose is "fill in these IDs once the LS variants are live" — that's an internal pre-launch task, not user-facing material.
- Includes a `~/.vct-secrets/shared/squeezylemon_api_token` shell snippet for fetching variant IDs from the Lemon Squeezy API. Pointing readers at the maintainer's secret store is internal-only by definition.
- Tells the reader to "edit `launcher/supabase/functions/_shared/variant_map.ts` and replace the `*_TODO` placeholder keys" — internal coordination across the launcher subtree.
- Has TBD prices and a "Status as of 2026-04-26" stamp that becomes stale instantly.
- A competitor reading this gets a step-by-step guide to clone the licensing pipeline and the names of the placeholder keys that ship with the public source.

This doc belongs under `docs/internal/launch-checklists/` or similar. **Did NOT move it** — flagged for user decision.

No mechanical fixes applied (would be wasted work if moved or rewritten internally).

Note: the placeholder-key approach in `variant_map.ts` itself is documented in-source (`assertNoPlaceholderKeysInProduction`) and that's the appropriate place — the runtime guard fails closed on launch day if anyone forgets to replace the placeholders. Removing this doc does not remove the safety net.

---

## Summary of public-safety classifications

| File | Classification | Action taken |
|------|----------------|--------------|
| README.md | SANITIZE-AND-KEEP | Sanitized (cut 7 items; rewrote intro and "Other docs" list) |
| INTEGRATION.md | MOVE-INTERNAL | Untouched, awaiting user decision |
| MACHINE_BINDING.md | KEEP-PUBLIC | Untouched (already clean) |
| USER_FLOW.md | KEEP-PUBLIC (lightly sanitized) | 3 small edits (key path, dashboard note, deactivation paragraph) |
| VARIANT_IDS.md | MOVE-INTERNAL | Untouched, awaiting user decision |

**Mechanical fix count**: 10 (7 README cuts + 3 USER_FLOW edits).

---

## Code-doc gaps

| # | File | Claim in doc | Reality in code | Severity |
|---|------|--------------|-----------------|----------|
| 1 | README.md (original) | "Tests/test_license_validator.py — 24 tests" | 32 test functions in the file (`grep -c "def test_"`). | Low — fixed by removing the count from the sanitized README. |
| 2 | README.md (original) | Public API tier list `"free" \| "pro" \| "mao" \| "enterprise"` | `validator.py::Tier` is `Literal["free", "pro", "mao", "enterprise", "admin"]` — `admin` is missing from the doc list. | Medium — the original README also claimed canonical pricing has MAO; per audit canonical pricing (Free / Pro / Enterprise) the `mao` tier should be removed from public-facing surfaces, but the code still ships it. **Either the code drops `mao` or the marketing tier list re-adds it.** Recommend: leave `mao` in code as a frozen-but-unmarketed tier (server can still classify), don't reference in user docs. |
| 3 | README.md (original) | "Key file fallback: `~/.vct-secrets/license_key`" | `validator.py` resolves `~/.vct-secrets/shared/license_key` first, falls back to `~/.vct-secrets/license_key` only for backward compat. The Phase 1 layout has been preferred since 2026-04-24 per project memory. | Low — fixed in sanitized README and in USER_FLOW.md. |
| 4 | README.md (original) | "`VIBECODED_LICENSE_URL`" only listed as override | The Rust launcher (`launcher/src-tauri/src/commands/licensing.rs`) reads `VCT_VALIDATE_TIER_URL`. The Python validator honors **both** (`VIBECODED_LICENSE_URL` wins if both are set). | Low — fixed by adding `VCT_VALIDATE_TIER_URL` to the env-var table. |
| 5 | README.md / VARIANT_IDS.md | LS variant IDs "TBD" | `launcher/supabase/functions/_shared/variant_map.ts` ships 6 `*_PLACEHOLDER` sentinel keys and a runtime guard `assertNoPlaceholderKeysInProduction` that hard-fails on launch day if real IDs aren't substituted. The placeholders cover Pro Monthly/Annual/Lifetime + MAO Monthly/Annual/Lifetime. | High **for marketing alignment** — `MAO_*_PLACEHOLDER` keys exist in source but the canonical pricing is Free / Pro / Enterprise. Either the MAO LS products are getting created (and the canonical pricing is incomplete), or the placeholders should be reduced to Pro-only before public launch. **Flagged for user decision.** |
| 6 | INTEGRATION.md | "Pro modules are not yet enabled for anyone; they'll flip on in a later release" | Code paths exist (`feature_enabled("rl_retrieval")`) but the user instruction confirms the licensing backend is being redeployed — pre-launch state. | Medium — the doc is technically accurate but exposes a pre-launch posture. Recommend moving the doc to internal regardless. |
| 7 | USER_FLOW.md | "Each Pro variant has `Activation limit = 3`" | Documented in MACHINE_BINDING.md as a manual setup step in the LS dashboard. There is **no automated assertion** that the limit is set to 3 — if a maintainer creates a Pro variant in LS without setting the activation limit, LS defaults will apply. | Low — operational, not code-level. Could add to the launch checklist. |
| 8 | (general) | The validator code path `expires_at` field is documented but the doc never mentions that subscription variants emit ISO-8601 strings while lifetime variants emit `null`. | `validator.py::LicenseResult.expires_at: Optional[str]` and the `Lifetime variant has License length = 0 days = lifetime — never expires` claim in USER_FLOW.md implies but doesn't make explicit that `expires_at=null` for lifetime. | Cosmetic — left as-is. |

## Safety-check final scan

After edits, scanned `README.md`, `MACHINE_BINDING.md`, `USER_FLOW.md` for:
- `ovpdtijpdchzlxbojhsg` / `ltnlwhaxnpbiifordlbk` (Supabase project IDs) → **none found**
- `AI_hive` / `ARTup` / `SD15` / `Agape` (sibling project names) → **none found**
- `Fabio`, beyond co-owner role → **none found**
- API tokens / keys → **none found**

INTEGRATION.md and VARIANT_IDS.md were not edited (MOVE-INTERNAL) so they retain their original content; the user should decide where to relocate them.

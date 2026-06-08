---
title: Launcher Paid Modules — Supabase Schema (004_paid_modules.sql)
type: concept
tags: [launcher, supabase, schema, paid-modules, licensing, commercial, VCT-Launcher, partially-superseded]
created: 2026-04-23T16:20:00Z
updated: 2026-05-26T00:00:00Z
status: active
implementation: "Original migration 004_paid_modules.sql lives in the `pb992/VCT-Launcher` private Supabase project (an earlier launcher exploration). Production project for the launcher is `ovpdtijpdchzlxbojhsg` (the orchestrator's Supabase). The launcher-side mirror of per-module entitlements is `tier_cache.module_licenses` (read via `is_module_licensed_v2`)."
---

# Launcher Paid Modules — Supabase Schema (004_paid_modules.sql)

⚠️ **Partially-superseded** (2026-05-26): the `profiles.paid_modules` JSONB column described below was designed in the pre-v0.2.33 architecture, intended to be read by a single `validate-module` endpoint at runtime. v0.2.33+ uses a three-endpoint design instead (`validate-tier` + `rl-artifact-url` + `module-catalog`) and the source-of-truth for per-module entitlements lives in `tier_cache.module_licenses` on the launcher side, with server-authoritative checks at `rl-artifact-url` via its server-to-server call to `validate-tier`. The `profiles.paid_modules` column shape is still RELEVANT for org-side persistence of which modules a user owns (especially when Lemon Squeezy webhooks fire post-purchase) once Path B (LS-variant licensing) ships, but the launcher-side runtime flow has evolved away from this single-endpoint design.

See [[Pre-install catalog architecture — L0 public endpoint + post-install on-disk manifest]] for the current install-time gate + [[Server-Side Admin License Validation]] for the production deployment status. See [[validate-module Supabase Edge Function]] for the superseded single-endpoint design and the migration path away from it.

## Original design (preserved below — superseded for runtime flow)

Supabase-side infrastructure for tracking which paid modules a user has activated. Extends the existing `profiles` table (from `002_orchestrator_tier.sql`) with a JSONB column + service-role helper functions + a view. Consumed by the `lemon-squeezy-webhook` edge function (on purchase) and by the forthcoming `validate-module` edge function (on runtime activation).

## Why this exists

Before this schema: the launcher had `orchestrator_tier` (single-product gate — free/pro/mao) but no way to track which **add-on paid modules** a user owned (Telegram module, future: Discord module, etc.). The MAO tier bundles everything so a column per module wouldn't scale.

Chosen design: one `profiles.paid_modules JSONB` column keyed by module slug, value is a dict with activation metadata. Flexible (no per-module migrations when a new module ships) + queryable via GIN index.

## Schema

Migration file: `supabase/migrations/004_paid_modules.sql` on `pb992/VCT-Launcher` master.

### Column

```sql
ALTER TABLE profiles ADD COLUMN paid_modules JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX idx_profiles_paid_modules_gin ON profiles USING GIN (paid_modules);
```

Shape (keyed by module slug, e.g. `"telegram"`, `"discord"`):

```jsonb
{
  "telegram": {
    "variant_id": 998877,
    "license_key": "LS-...",
    "activated_at": "2026-04-22T18:30:00Z",
    "expires_at": "2027-04-22T18:30:00Z",
    "machines": ["hostname-abc123"]
  }
}
```

### Trigger protection

Prevents clients from writing to `paid_modules` directly — only service-role RPC paths can mutate. The `BEFORE UPDATE` trigger raises an exception if a non-service-role session modifies the column.

### Service-role RPCs

`upsert_paid_module(user_id uuid, module_slug text, metadata jsonb)`:
- Merges `metadata` into the existing per-module dict (or creates fresh).
- Preserves `activated_at` on subsequent calls (first-purchase timestamp survives renewals).
- Called by `lemon-squeezy-webhook` on `order_created` for paid-module variants.
- Called by `validate-module` edge function on runtime activation + renewal.

`remove_paid_module(user_id uuid, module_slug text)`:
- Deletes the per-module entry from the JSONB dict.
- Called on subscription cancellation / refund.

### View

`active_paid_modules` — unnests `paid_modules` into per-(user_id, module_slug) rows for easier querying. Used by admin UIs + analytics.

## Callers

### `lemon-squeezy-webhook` (extended 2026-04-22)

Added `PAID_MODULES_MAP` dict (parallel to `VARIANT_MAP` / `ORCHESTRATOR_TIER_MAP`). On `order_created`, if the variant_id matches a paid-module entry, calls `upsert_paid_module()` via service-role RPC. **Pending (launcher-side)**: 3 Telegram variant IDs to populate `PAID_MODULES_MAP` after LS product creation with "enable license keys" toggle.

### `validate-module` edge function (written, deploy pending)

`supabase/functions/validate-module/index.ts` — sibling of `validate-tier`. Runtime licensing check:

1. Receives `{license_key, module_slug, machine_id}` from the launcher.
2. Validates key via LS `/v1/licenses/validate` + `/v1/licenses/activate`.
3. Verifies `variant_id` matches `PAID_MODULES_MAP[module_slug]`.
4. Calls `upsert_paid_module()` with activation metadata.
5. Returns `{valid: true, expires_at, tier}`.

**Pending (launcher-side)**: `supabase functions deploy validate-module`. See [[relatedTo::validate-module edge function]] for the full contract.

### Launcher Rust side (`src-tauri/src/commands/modules.rs`)

The launcher's install/uninstall flow for paid modules will call `validate-module` at install time and read `active_paid_modules` view to gate UI access. See [[relatedTo::VCT Launcher Hub Architecture]].

## Commercial flow

```
User purchases on Lemon Squeezy
  ↓ LS fires order_created webhook
lemon-squeezy-webhook edge function
  ↓ if variant_id in PAID_MODULES_MAP
upsert_paid_module(user_id, "telegram", {license_key, ...})
  ↓ writes to profiles.paid_modules JSONB
User launches VCT Launcher, sees Telegram module unlocked
  ↓ Launcher calls validate-module with license key
validate-module edge function verifies + upserts machine_id
  ↓ Launcher installs module repo (git clone) + injects license into env
Telegram MCP module active in Claude Code
```

## Deferred / open

- `validate-module` deploy (launcher-side)
- 3 Telegram variant IDs to populate `PAID_MODULES_MAP` (launcher-side)
- Multi-machine license slot enforcement (schema supports `machines` array; runtime logic not yet in `validate-module`)
- Module uninstall → should it call `remove_paid_module()` or just keep the license for re-install? Current decision: keep (user paid for it; let them reinstall freely)

## Related

- [[relatedTo::VCT Launcher Hub Architecture]]
- [[relatedTo::validate-module edge function]] (to be created)
- [[relatedTo::Telegram as Standalone Paid Module]] (first consumer)
- [[relatedTo::VCT Coordination MCP — Standalone Product]] (future consumer if marketed as paid)
- Migration file: `pb992/VCT-Launcher:supabase/migrations/004_paid_modules.sql`
- Webhook: `pb992/VCT-Launcher:supabase/functions/lemon-squeezy-webhook/index.ts`

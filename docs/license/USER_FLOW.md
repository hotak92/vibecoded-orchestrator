# Pro User Flow — Activate / Deactivate / Transfer

## Activation (first machine)

1. User completes LS checkout → receives license key (UUID) by email.
2. User pastes the key into the launcher GUI **OR** writes it to
   `~/.vct-secrets/license_key` (`chmod 600`, plain UUID, no surrounding quotes).
3. Launcher restarts the orchestrator with `VIBECODED_LICENSE_KEY` set.
4. Validator POSTs to `validate-tier` with `{license_key, machine_id_hash}`.
5. Edge function calls LS `licenses/activate` with `instance_name=machine_hash`.
   LS treats this as a fresh activation, increments `activation_count`.
6. Edge function returns `{valid: true, tier: "pro", expires_at, machine_count: 1, machine_limit: 3}`.
7. Validator writes `~/.vibecoded/license_cache.json` and the orchestrator
   reports tier=pro on next session start.

## Re-activation (same machine)

`machine_id_hash` is a SHA-256 of the MAC address — stable across reboots,
re-installs, even fresh OS installs as long as the network card is the same.
LS dedupes activations by `instance_name`, so re-running activation on the
same machine is a no-op (returns `machine_count` unchanged).

## Activation cap (4th machine)

Each Pro variant has `Activation limit = 3` set in LS. The 4th unique
`machine_id_hash` returns LS HTTP 422; the edge function maps this to
`{error: "instance_limit", message: ...}` (HTTP 200 with payload). The
validator returns `tier=free` with a message instructing the user to free up
a machine slot.

## Deactivation (free up a slot)

User signs in to **vibecodedtools.it/account** (the Vercel dashboard,
backed by Supabase). The dashboard lists all `machine_count` slots with
their last-seen timestamp. User clicks **Deactivate** on one slot →
dashboard calls a Supabase edge function that calls LS
`licenses/deactivate` with the corresponding `instance_id`.

After deactivation, the freed slot is available; the user can activate a
new machine immediately.

> **Note**: this dashboard route is owned by the launcher branch
> (`launcher/`) and the Vercel-hosted `vibecodedtools` project. It is not
> part of this repo.

## Transfer (move Pro to a new machine)

There is no special "transfer" operation — it's deactivate-old +
activate-new:

1. On the new machine, install/launcher prompts for the key → activation
   fails with `instance_limit` if all 3 slots are taken.
2. User opens the dashboard, deactivates the old machine.
3. User retries activation on the new machine → succeeds.

If the old machine is permanently lost (stolen, disk failure), the user
can only reach 3 by waiting for one of the existing slots to be freed.
Support can manually deactivate from the LS dashboard if the dashboard
self-service flow isn't enough.

## Subscription lifecycle

Subscription variants (Monthly / Annual) auto-renew until the user
cancels in the LS customer portal. When a subscription expires:

1. LS `licenses/validate` returns `valid: false` with `disabled` reason.
2. Edge function returns 401.
3. Validator overwrites cache with `tier=free, valid=false`.
4. Orchestrator drops to free tier on next process restart (or after
   `force_refresh=True`).

Lifetime variant has `License length = 0 days = lifetime` — never expires.

## What the validator never does

- **Never blocks startup.** All network calls have an 8-second timeout
  and fail open to free.
- **Never spams the network.** One call per process at most, cached for
  the process lifetime; re-validation is opt-in via `force_refresh=True`.
- **Never trusts an env-var-claimed paid tier.** `VIBECODED_TIER=pro`
  has no effect — paid tiers are only granted by a validated key.

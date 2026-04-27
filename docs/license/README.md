# License System — Overview

How VibeCoded Orchestrator gates paid features.

## TL;DR

- **No license key on the machine → free tier**, every time, no exceptions.
- The free tier is fully functional — knowledge graph, code graph, hooks,
  MCP servers, default agents/skills all work standalone.
- Optional paid modules (RL retrieval, multi-agent maestro, specialist agent
  packs) activate when a valid key is present and the tier resolves to
  `pro`/`mao`/`enterprise`.

## Components

| Layer                                  | Where                                    | Owner          |
| -------------------------------------- | ---------------------------------------- | -------------- |
| Validator (orchestrator side)          | `VCThelpers/license/validator.py`        | This repo      |
| Local cache + status                   | `~/.vibecoded/license_cache.json`        | Per-user       |
| Key file fallback                      | `~/.vct-secrets/license_key`             | Per-user       |
| `validate-tier` edge function          | `launcher/supabase/functions/validate-tier/` | Launcher repo |
| Variant → tier map                     | `launcher/supabase/functions/_shared/variant_map.ts` | Launcher repo |
| Lemon Squeezy product/variants         | LS dashboard                             | Manual setup   |

## Flow (Pro user)

1. User pays on LS → receives license key (UUID) by email.
2. Launcher prompts for the key → stores it (env var or `~/.vct-secrets/license_key`).
3. Orchestrator startup calls `validate_license()`:
   1. Reads key (env var → file).
   2. POSTs `{license_key, machine_id_hash}` to
      `https://api.vibecodedtools.it/validate` (override:
      `VIBECODED_LICENSE_URL`).
   3. The Supabase edge function calls LS `licenses/validate` +
      `licenses/activate`, looks up the variant in `VARIANT_MAP`, and
      returns `{valid, tier, expires_at, machine_count, machine_limit}`.
4. Orchestrator caches the result in `~/.vibecoded/license_cache.json`.
5. Subsequent feature checks (`feature_enabled("rl_retrieval")`) consult
   the cached tier, no network call.

## Flow (free / OSS user)

1. No key anywhere → validator returns `tier="free"` immediately.
2. No network call. No log spam. No scary errors.
3. All free-tier features work normally.

## Failure modes (all fail-OPEN to free)

| Symptom                                         | Outcome                                                         |
| ----------------------------------------------- | --------------------------------------------------------------- |
| Network unreachable, no cache                   | `tier=free`, no error.                                          |
| Network unreachable, cached, within 3 days      | Cached tier preserved, `~/.vibecoded/license_status.txt` notes grace. |
| Network unreachable, cache older than 3 days    | Degrade to `tier=free`, status file explains why.               |
| Validate-tier returns 401 (invalid key)         | Cache overwritten with `tier=free`, key remains until user rotates it. |
| Validate-tier returns 5xx                       | Treated as transient → cache fallback.                          |
| Validate-tier returns unknown `tier` value      | Coerced to `free`.                                              |
| Machine cap exceeded (`error: instance_limit`)  | `tier=free` with explicit message; user deactivates a machine.  |

## Public API (Python)

```python
from VCThelpers.license import (
    get_tier,           # -> "free" | "pro" | "mao" | "enterprise"
    require_tier,       # require_tier("pro") -> bool
    feature_enabled,    # feature_enabled("rl_retrieval") -> bool
    validate_license,   # force a fresh remote call
    license_status,     # introspection dict (no network)
)
```

`get_tier()` caches for the process lifetime; pass `force_refresh=True` to
re-validate.

## Environment

| Var                         | Purpose                                                          |
| --------------------------- | ---------------------------------------------------------------- |
| `VIBECODED_LICENSE_KEY`     | License key (UUID). Highest priority key source.                 |
| `VIBECODED_LICENSE_URL`     | Override for the validate endpoint (default: public alias).      |
| `VIBECODED_TIER=free`       | Dev override — forces free tier regardless of key.               |

`VIBECODED_TIER=pro` and similar are **ignored**. Paid tiers are only
granted via a server-validated key.

## Tests

`tests/test_license_validator.py` — 24 tests covering every code path.
Run: `pytest tests/test_license_validator.py`. They mock urllib; no real
network calls.

## Operations docs

- `LS_INVENTORY_*.md` — point-in-time API snapshots (gitignored; see maintainer docs for setup steps)
- `USER_FLOW.md` — end-user activation / deactivation / transfer
- `MACHINE_BINDING.md` — how machine_id_hash works, quota policy
- `INTEGRATION.md` — wiring `get_tier()` into orchestrator entry points
- `VARIANT_IDS.md` — current variant_id values for the variant_map

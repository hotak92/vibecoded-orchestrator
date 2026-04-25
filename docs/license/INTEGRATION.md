# Wiring `get_tier()` into the Orchestrator

## OSS launch scope

For the OSS launch we want **observability only** — log the tier on first
session start so users can see what they got, but do NOT actually gate any
feature yet. This way:

- Free-tier users see `tier=free` in their session log; nothing breaks.
- Pro modules are not yet enabled for anyone; they'll flip on in a later
  release once the LS Pro variants exist and the dashboard is live.
- The wiring is in place, so flipping the switch later is one-line edits
  per module.

## Recommended one-liner (entry point)

In whichever file the orchestrator's "session start" routine lives, add:

```python
import logging
log = logging.getLogger(__name__)

def _log_license_tier_once() -> None:
    """One-time, non-blocking tier announcement on session start."""
    try:
        from VCThelpers.license import get_tier, license_status
        s = license_status()
        log.info(
            "VibeCoded tier=%s (key_source=%s, cached=%s)",
            s["tier"], s["key_source"], s["cached"],
        )
    except Exception as e:                                      # pragma: no cover
        # NEVER break startup because of license code.
        log.debug("license check skipped: %s", e)
```

Call `_log_license_tier_once()` once per process. Good places:

- `scripts/launch_api.py` after the singleton check, before the API server starts.
- The launcher's orchestrator-spawn routine, just before exec.
- A `Stop` hook that records tier in metrics.

## Future Pro-feature gating (post-launch)

Each paid module checks its feature gate at module-load time:

```python
from VCThelpers.license import feature_enabled

if feature_enabled("rl_retrieval"):
    from .rl_reranker import RLReranker as _Reranker
else:
    from .cosine_reranker import CosineReranker as _Reranker
```

The free-tier code path **must always exist and be exercised in CI**.
Pro-only code paths are tested separately under a feature flag.

## What NOT to do

- **Don't crash on missing license code.** The whole license module
  could be deleted by an aggressive packager; the orchestrator must
  still start (use a try/except around the import).
- **Don't call `validate_license()` from a hot path.** Use
  `get_tier()` (cached) for feature checks; `validate_license()` only
  in CLI commands and explicit re-validation.
- **Don't log the license key.** Only `key_source` ("env" / "file" /
  "none") is safe to log.

## Manual smoke test

```bash
# Free tier (default)
unset VIBECODED_LICENSE_KEY
python -m VCThelpers.license.validator
# → Tier: free / Valid: True / Message: "No license key — free tier."

# Bogus key
export VIBECODED_LICENSE_KEY="$(uuidgen)"
python -m VCThelpers.license.validator
# → Tier: free / Valid: False / Message: "Invalid or expired license."
# (or "free" with grace message if the endpoint is down)

# Force-free dev override
export VIBECODED_TIER=free
python -m VCThelpers.license.validator
# → Tier: free / Valid: True / Message: "Free tier (env override)."
```

# Machine Binding

## Identifier: `machine_id_hash`

The orchestrator never sends raw hardware identifiers to the validation
endpoint. The only identifier that crosses the wire is a SHA-256 hash:

```python
def _machine_id_hash() -> str:
    node = uuid.getnode()                              # MAC address as int
    return hashlib.sha256(node.to_bytes(8, "big")).hexdigest()
```

Properties:

- **Deterministic per machine** — same MAC → same hash, every run.
- **One-way** — given the hash, the original MAC cannot be recovered
  (SHA-256 preimage resistance).
- **Stable across reboots, re-installs, OS upgrades** — as long as the
  primary network interface is the same physical NIC.
- **Stable across login users** — the hash is system-wide, not
  per-user. Multiple users on one machine share the same activation slot.

## What changes the hash

A new `machine_id_hash` is produced when:

- The user replaces the primary network card.
- A virtualized environment generates a fresh MAC (some VMs do this on
  every boot; not recommended for production licensing).
- `uuid.getnode()` falls back to a random 48-bit value because no MAC
  could be detected (very rare on real hardware).

In all of these cases the user effectively has a "new machine" from LS's
perspective. They can free up an old slot via the dashboard.

## LS-side: instance binding

The edge function calls `POST /v1/licenses/activate` with:

```json
{
  "license_key": "<uuid>",
  "instance_name": "<machine_id_hash>"
}
```

LS dedupes activations by `instance_name`:

- First call → creates a new instance, increments activation count.
- Repeat call (same hash) → idempotent, returns existing instance.

When `activation_count >= activation_limit` (3 for Pro), LS returns
HTTP 422. The edge function maps this to a 200 response with
`{error: "instance_limit", message}` so the orchestrator can show a
clean message rather than a generic 4xx.

## Quota: 3 machines per Pro license

This matches typical "1 desktop + 1 laptop + 1 spare" usage. Set on each
variant in the LS dashboard (Activation type=`activations`, limit=3).

A single user can have multiple licenses (e.g. one for personal, one for
work). The orchestrator only knows about the key it was given; there's no
"sign in" — keys are bearer tokens.

## Privacy

The `machine_id_hash` is **not** linked to any user identity in LS. LS
sees a license key and an opaque hash; that's all. The user's email
address is on the LS order, not on the activation record.

## Edge cases

| Scenario                                  | Behavior                                                  |
| ----------------------------------------- | --------------------------------------------------------- |
| Two machines with the same MAC (cloned VM)| LS treats them as one machine (same `instance_name`).     |
| Network card replaced                     | New hash → counts as a new machine. Free old via dashboard. |
| MAC randomization at boot (some VMs)      | Each boot is a new "machine" — burns slots fast. Not supported. |
| User deletes `~/.vibecoded/license_cache.json` | Next session re-validates (one extra remote call).   |
| Time-of-day clock skew                    | Cache TTL uses `time.time()`; large clock jumps could prematurely expire grace period. Not a security issue (we always fail to free). |

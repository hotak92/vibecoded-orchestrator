# Machine Binding

## Identifier: `machine_id_hash`

The orchestrator never sends raw hardware identifiers to the validation
endpoint. The only identifier that crosses the wire is a SHA-256 hash:

```python
def _machine_id_hash() -> str:
    host_id = _read_platform_host_id()              # platform-stable string
    if host_id is None:
        host_id = "vct-no-platform-host-id-v0.2.36" # fallback sentinel
    return hashlib.sha256(host_id.encode("utf-8")).hexdigest()
```

The platform-stable identifier `_read_platform_host_id()` returns depends
on the host OS:

| OS      | Source                                                                | Notes                                                 |
| ------- | --------------------------------------------------------------------- | ----------------------------------------------------- |
| Windows | `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid` (REG_SZ)           | GUID set by Windows at install                        |
| macOS   | `IOPlatformUUID` (via `ioreg -rd1 -c IOPlatformExpertDevice`)         | Hardware UUID; survives OS reinstall                  |
| Linux   | `/etc/machine-id` (fallback `/var/lib/dbus/machine-id`)               | systemd standard (or pre-systemd dbus equivalent)     |

The Rust launcher at `launcher/src-tauri/src/commands/licensing.rs::machine_id_hash`
mirrors the same algorithm and reads the same OS-provided value, so both
sides of the IPC boundary produce identical hashes for the same machine.

Properties:

- **Deterministic per machine** — same host id → same hash, every run.
- **One-way** — given the hash, the original identifier cannot be
  recovered (SHA-256 preimage resistance).
- **Stable across reboots, re-installs, OS upgrades** — the underlying
  identifier is set once and persists.
- **Stable across NIC changes** — Wi-Fi/Ethernet adapter swaps, USB
  dongle unplugs, dock changes, and integrated-NIC repairs no longer
  change the hash. (This was the main bug fixed in v0.2.36.)
- **Stable across login users** — the hash is system-wide, not
  per-user. Multiple users on one machine share the same activation slot.

## What changes the hash

A new `machine_id_hash` is produced when:

- **Windows**: OS reinstall, or an explicit edit to
  `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid`.
- **macOS**: motherboard / logic-board replacement (the hardware UUID is
  burned into the device). OS reinstall does NOT change it.
- **Linux**: deleting `/etc/machine-id` (and `/var/lib/dbus/machine-id`)
  then regenerating via `systemd-machine-id-setup`; or moving to a fresh
  disk image without the file. OS reinstall typically regenerates it.

In all of these cases the user effectively has a "new machine" from the
licence server's perspective. They can free up an old slot via the LS
dashboard, or — for admin-tier Vault tokens — use the launcher's
**Settings → License → Rebind to this machine** button (v0.2.36).

## v0.2.36: algorithm change (BREAKING for admin-tier only)

Through v0.2.35, the hash was computed from the host's MAC address:

```python
node = uuid.getnode()                          # 6-byte MAC as int
hashlib.sha256(node.to_bytes(8, "big")).hexdigest()
```

This was fragile on laptops (the dominant 3rd-party user environment):

1. NICs come and go — Wi-Fi can power-save off, USB Ethernet can be
   unplugged, docks change adapter enumeration. Every event changed the
   bound NIC → broke machine binding.
2. Python's `uuid.getnode()` and Rust's `mac_address::get_mac_address()`
   didn't always pick the same NIC. Observed on a Win11 laptop with two
   NICs: Python picked USB Ethernet, Rust picked Wi-Fi → different
   hashes for the same machine.
3. Mainboard repairs that replaced the integrated NIC looked like a
   brand-new machine.

The v0.2.36 switch to platform-stable host identifiers fixes all three
without changing the wire format — the server still receives a 64-char
sha256 hex string.

**Migration impact**:

| Tier            | Impact                                                                            |
| --------------- | --------------------------------------------------------------------------------- |
| Free            | None — no key, no binding.                                                        |
| Pro / MAO       | LS re-activates idempotently per `instance_name`. New hash → new slot. If the user hits the 3-slot limit, deactivate an old slot at vibecodedtools.it/account. |
| Enterprise      | Same as Pro/MAO.                                                                  |
| **Admin (Vault token)** | **Existing Vault entries' bound `machine_id_hash` values are stale.** First `license_refresh` after upgrade returns `machine_mismatch`. Resolution: click **Settings → License → Rebind to this machine** in the launcher (Agent S, v0.2.36). |

The admin-token rebind flow calls the `/rebind-admin-token` edge function,
which is gated on the user holding the current Vault token — the server
authenticates the rebind by validating that token before writing the new
hash, so the migration is safe without out-of-band credentials.

## Test override: `VCT_MACHINE_ID_OVERRIDE`

For tests and for support engineers who need to reproduce a user's hash
without copying their actual `MachineGuid` / `IOPlatformUUID` / `machine-id`
(which would be a privacy leak), the env var
`VCT_MACHINE_ID_OVERRIDE=<arbitrary string>` pins the input to the hash
verbatim. Empty value is treated as "not set" so a stray export with an
empty rhs doesn't silently collapse every machine to `sha256("")`.

Both the Python validator and the Rust launcher honour the same env var
name and the same empty-string semantics. Production code MUST NOT set
it.

## LS-side: instance binding (Pro / MAO / Enterprise)

For Lemon Squeezy-issued licenses, the edge function calls
`POST /v1/licenses/activate` with:

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

## Admin tier: Vault-token machine binding (TOFU)

For `vct_admin_*` Vault-token holders, the binding is recorded in the
`vct_admin_tokens` Supabase Vault secret as `machine_id_hash` per user
entry. First successful auth from any machine writes the hash back (TOFU
— Trust On First Use); subsequent auths from a different machine are
rejected with `machine_mismatch`. Rebinding requires either:

- Explicit SQL by the project owner (
  `jsonb_set(..., '{<user>,machine_id_hash}', 'null'::jsonb)`), or
- The launcher's **Rebind to this machine** button (v0.2.36), which
  authenticates with the current Vault token and calls the
  `/rebind-admin-token` edge function (which uses the
  `bind_vault_admin_machine` RPC's "force overwrite" mode for the
  admin-self-rebind case).

The audit log (`admin_auth_log`) records every successful and failed
auth attempt for forensic review.

## Privacy

The `machine_id_hash` is **not** linked to any user identity in LS. LS
sees a license key and an opaque hash; that's all. The user's email
address is on the LS order, not on the activation record.

For Vault-admin tokens, the username inside the Vault map is the only
identity attached to the hash — and that username is operator-controlled
(team members are added by the project owner via SQL).

## Edge cases

| Scenario                                  | Behavior                                                  |
| ----------------------------------------- | --------------------------------------------------------- |
| Two machines with the same MachineGuid (cloned VHD without sysprep) | Both produce the same hash. LS treats them as one machine; admin Vault binds to whichever auths first. Operators are expected to `sysprep /generalize` cloned images. |
| Network card replaced                     | **No change** (v0.2.36+). Pre-v0.2.36, new hash → counted as a new machine. |
| MAC randomization at boot (some VMs)      | **No change** (v0.2.36+). Pre-v0.2.36, each boot burned a slot. |
| /etc/machine-id deleted on Linux          | Next boot regenerates via `systemd-machine-id-setup` → new hash, counts as new machine. |
| User deletes `~/.vibecoded/license_cache.json` | Next session re-validates (one extra remote call).   |
| Time-of-day clock skew                    | Cache TTL uses `time.time()`; large clock jumps could prematurely expire grace period. Not a security issue (we always fail to free). |
| Unsupported OS (BSD / Solaris / unknown)  | Resolver returns None → all hosts collide on the sentinel hash. Operator can set `VCT_MACHINE_ID_OVERRIDE` to pin a value. |

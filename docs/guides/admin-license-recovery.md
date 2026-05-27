# Admin License Recovery — Rebinding an admin token to a new machine

This guide walks an admin-tier user through recovering from the error:

> `Status 401 Unauthorized: Admin Token Is Bound To A Different Machine. Contact The Project Owner To Rebind.`

The same recovery flow applies to **all three** scenarios that produce this
error.

## When this happens

Admin-tier licenses (`vct_admin_*` Vault tokens) are bound to a single
machine on first use (TOFU — Trust On First Use). The bound identifier is
the `machine_id_hash`, a SHA-256 of an OS-provided host ID — see
[`docs/license/MACHINE_BINDING.md`](../license/MACHINE_BINDING.md) for the
full algorithm.

You will see `machine_mismatch` from `/validate-tier` in three situations:

1. **OS reinstall on the same hardware.** Reinstalling Windows or Linux
   typically regenerates the `machine_id_hash` source value
   (`MachineGuid` / `/etc/machine-id`). macOS `IOPlatformUUID` is burned
   into the logic board and survives OS reinstall, so this case does
   not affect Mac users.
2. **Moved to a new laptop / desktop.** Different hardware → different
   host ID → different hash.
3. **Upgrade from a pre-v0.2.36 launcher to v0.2.36+.** The hash
   algorithm changed: pre-v0.2.36 hashed the MAC address; v0.2.36+
   hashes a platform-stable host ID (`MachineGuid` on Windows,
   `IOPlatformUUID` on macOS, `/etc/machine-id` on Linux). The new hash
   does not match the old one, so the first `/validate-tier` call after
   upgrade returns `machine_mismatch`. This is a one-time migration —
   after the rebind, the new hash is stable across all the things the
   MAC-based hash was fragile to (NIC swaps, dock changes, USB Ethernet
   power events).

In every case, the fix is the same: **rebind the admin token to the
current machine's hash**.

## Recovery via the launcher GUI (recommended)

1. **Open the launcher.** You will see your tier badge in the title bar
   showing `Free` (with an error toast or a red ring around it) instead
   of `Admin`.
2. **Click the license badge** to open the **Orchestrator License**
   modal.
3. **Verify the error.** The modal shows:
   - `Current tier: Free`
   - A `Last error` row mentioning `machine_mismatch` (or text including
     the word "machine"). If the error is something else (`tier_invalid`,
     `network_error`, missing key), this guide does not apply — see
     [`docs/TROUBLESHOOTING.md`](../TROUBLESHOOTING.md) instead.
4. **Click "Rebind to this machine".** This button is visible for admin
   tokens (`vct_admin_*` prefix) starting in v0.2.36. The launcher will:
   - POST to the Supabase edge function `rebind-admin-token` with your
     current license key and the freshly-computed `machine_id_hash`.
   - The edge function authenticates by matching the token against the
     Vault map (same security check `/validate-tier` already trusts —
     possession of the token is the authorization).
   - On success, the Vault entry's `machine_id_hash` is overwritten with
     the new value.
5. **Click "Refresh"** (or restart the launcher). The badge should now
   show `Admin`. The rebind is complete.

> **Pre-v0.2.37 note:** the "Rebind to this machine" button was hidden by
> a state-management bug in v0.2.36 (the admin-card branch did not
> render when the modal opened in `error` state — the very state where
> the button is most needed). If you cannot see the button, you are
> almost certainly on a v0.2.36 launcher. Either upgrade to v0.2.37+
> (recommended) or use the **curl fallback** below. Upgrading the
> launcher itself does not require admin tier — you can pull the new
> launcher binary while still on `Free`.

## Curl fallback (when the button is unavailable)

Useful when:
- You are on pre-v0.2.37 and the rebind button is hidden by the state
  bug.
- You need to rebind *in order to* run the launcher update flow (which
  requires admin tier for the dev-features path).
- You are scripting / automating across many machines.

The `rebind-admin-token` edge function accepts plain HTTP POST — no
JWT, no anon key (`verify_jwt = false`); the auth boundary is the token
itself matching the Vault entry.

```bash
# 1. Read the license key from wherever you stored it.
LICENSE=$(cat ~/.vct-secrets/shared/license_key)
# Or, if you use the legacy flat path:
# LICENSE=$(cat ~/.vct-secrets/license_key)

# 2. Compute the platform-stable machine_id_hash (one-liner per OS — see below).
HASH=<computed-hash-from-snippet-below>

# 3. POST to the rebind endpoint.
curl -X POST 'https://<your-supabase-project-ref>.supabase.co/functions/v1/rebind-admin-token' \
  -H 'Content-Type: application/json' \
  -d "{\"license_key\":\"$LICENSE\",\"new_machine_id_hash\":\"$HASH\"}"
```

Expected success response (HTTP 200):

```json
{
  "success": true,
  "user": "<admin-identifier>",
  "rebound_at": "2026-05-27T14:22:00.000Z"
}
```

Common error responses:

| HTTP | error code | Meaning |
|---|---|---|
| 400 | `license_key_invalid_format` | Key is missing or not a `vct_admin_*` token |
| 400 | `machine_id_hash_invalid_format` | Hash is missing or not 64 hex chars |
| 401 | `license_invalid` | Token shape OK but no matching entry in the Vault map |
| 500 | `service_misconfigured` | Edge function missing required env vars (report to project owner) |
| 500 | `rebind_failed` | RPC returned false — see `detail` field |

After a successful rebind, restart the launcher (or call
`/validate-tier` again) — the new hash is now the bound machine.

### Computing the platform-stable hash

These one-liners reproduce what
[`VCThelpers/license/validator.py::_machine_id_hash`](../../VCThelpers/license/validator.py)
and
[`launcher/src-tauri/src/commands/licensing.rs::machine_id_hash`](../../launcher/src-tauri/src/commands/licensing.rs)
compute. Both must produce identical hashes for the same machine — if
they do not, file an issue.

**Linux** (`/etc/machine-id`, fallback `/var/lib/dbus/machine-id`):

```bash
# Stripped contents → sha256 → hex
HASH=$(cat /etc/machine-id 2>/dev/null || cat /var/lib/dbus/machine-id) \
  && HASH=$(printf '%s' "$HASH" | tr -d '[:space:]' | sha256sum | awk '{print $1}') \
  && echo "$HASH"
```

**macOS** (`IOPlatformUUID` from `ioreg`):

```bash
HASH=$(ioreg -rd1 -c IOPlatformExpertDevice \
  | awk -F'"' '/IOPlatformUUID/ {print $4; exit}' \
  | shasum -a 256 | awk '{print $1}') \
  && echo "$HASH"
```

**Windows** (`HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid`,
PowerShell 5+):

```powershell
$guid = (Get-ItemProperty `
  -Path 'HKLM:\SOFTWARE\Microsoft\Cryptography' `
  -Name MachineGuid).MachineGuid.Trim()
$bytes = [System.Text.Encoding]::UTF8.GetBytes($guid)
$sha   = [System.Security.Cryptography.SHA256]::Create()
$hash  = ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join ''
$hash
```

> The Python and Rust implementations strip whitespace from the host ID
> before hashing. The shell one-liners above do the same. If you compute
> the hash by hand and get a `machine_id_hash_invalid_format` /
> `license_invalid` error despite the value looking right, double-check
> that no trailing newline is being included.

## What gets logged

Every rebind attempt (successful and failed) is recorded in the
`admin_auth_log` Supabase table for forensic review. The log includes
the timestamp, the user identifier from the Vault entry, the old hash
prefix, the new hash prefix, and the result. Project owners can query
this log to audit unexpected rebinds.

## Related

- [`docs/license/MACHINE_BINDING.md`](../license/MACHINE_BINDING.md) —
  full hash algorithm, OS-specific source identifiers, what does and
  does not change the hash.
- [`docs/license/USER_FLOW.md`](../license/USER_FLOW.md) — Pro-tier
  activation flow (Lemon Squeezy slot-based, not admin-Vault).
- [`launcher/supabase/functions/rebind-admin-token/index.ts`](../../launcher/supabase/functions/rebind-admin-token/index.ts) —
  edge function implementing the rebind RPC.
- [`launcher/src-tauri/src/commands/licensing.rs`](../../launcher/src-tauri/src/commands/licensing.rs) —
  Rust `license_rebind_admin_token` Tauri command (the launcher button
  calls this).
- [`launcher/src/lib/components/ActivationModal.svelte`](../../launcher/src/lib/components/ActivationModal.svelte) —
  Svelte component that renders the "Rebind to this machine" button
  (visible-state fix landed in v0.2.37).

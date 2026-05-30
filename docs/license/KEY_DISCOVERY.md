# License key discovery for orchestrator projects

This document describes the **canonical contract** that orchestrator
projects, hooks, MCPs, and helper scripts use to discover where the
launcher stores license keys in the OS keychain. It exists so any
downstream consumer has a single source of truth rather than scattering
hardcoded usernames across the codebase.

**Audience**: anyone writing code that needs to read a license key from
outside the launcher process (e.g. a per-project hook that needs to
authenticate against a paid-module token gateway, or an MCP server that
gates a capability on the orchestrator tier).

---

## TL;DR

- **Keychain service**: `vct.global.licensing`
- **Keychain username**: `license_key__<module_id>`
- **Reserved orchestrator slot**: `license_key____orchestrator__` (the
  double underscore comes from the reserved `__orchestrator__`
  module-id sentinel concatenated with the `license_key__` prefix).

## Canonical lookup functions (Rust)

The single source of truth for the username shape lives in
`launcher/src-tauri/vct-launcher-core/src/db/license_keys.rs`. Two
helpers are exported for downstream consumers:

```rust
use vct_launcher_core::db::license_keys::{
    keychain_username_for,        // (module_id: &str) -> String
    license_keychain_service,     // () -> &'static str
    ORCHESTRATOR_MODULE_ID,       // "__orchestrator__"
};

// Service for every license-key entry (constant value).
let service = license_keychain_service();      // "vct.global.licensing"

// Per-module username (canonical pattern).
let rl_username = keychain_username_for("vct-rl-reranker");
//   → "license_key__vct-rl-reranker"

// Orchestrator-tier root key.
let root_username = keychain_username_for(ORCHESTRATOR_MODULE_ID);
//   → "license_key____orchestrator__"
```

Both helpers are also the source of truth that the launcher's own
`commands::licensing` module uses — there are no separate strings
hardcoded elsewhere. If the username shape ever changes (e.g. a future
v0.3 module-id versioning scheme), only `keychain_username_for` needs
to update; every consumer follows automatically.

## How the launcher itself uses these

`commands::licensing::LICENSE_MODULE_ID` (`"licensing"`) is the value
the launcher passes as the **secret's `module_id` argument** to
`secrets::get/set/delete`. The secrets layer composes that with the
scope into the full service string via `SecretScope::service_name`:

```rust
// SecretScope::Global with module_id="licensing" composes into the
// service string "vct.global.licensing" — exactly what
// license_keychain_service() returns.
secrets::set(
    SecretScope::Global,
    LICENSE_MODULE_ID,                                  // "licensing"
    &keychain_username_for(ORCHESTRATOR_MODULE_ID),     // "license_key____orchestrator__"
    license_key_value,
)?;
```

Downstream Rust consumers should use the same triple. The
`license_keychain_service()` helper is provided so non-launcher
codepaths (e.g. a standalone CLI tool, an out-of-tree consumer) can
target the service string directly without depending on
`SecretScope::service_name`.

## Reading from a hook or shell script

The launcher ships per-project hook scripts that need to discover the
license key. For shell-script consumers, the secrets layer is reached
through `vct-hub`'s `/api/v1/projects/{id}/env` resolver — this is the
recommended path because it doesn't bypass any of the scope-resolution
rules (per-project bag → shared → global).

For direct keychain reads (rare; only when the hub isn't running), the
keychain coordinates above can be queried via the platform's native
tool:

- **macOS**: `security find-generic-password -s 'vct.global.licensing' -a 'license_key____orchestrator__' -w`
- **Linux** (libsecret / gnome-keyring): `secret-tool lookup service 'vct.global.licensing' username 'license_key____orchestrator__'`
- **Windows** (Credential Manager): the entry is named
  `vct.global.licensing` with username `license_key____orchestrator__`;
  read via `cmdkey` or the `keyring` Python library.

## Migration from the legacy username (v0.2.40 L1.M)

Pre-v0.2.40 launchers stored the orchestrator key at the legacy username
`VIBECODED_LICENSE_KEY`. The v0.2.40-L1 release initially kept that
constant in place for downgrade compatibility; v0.2.40-L1.M completed
the migration to the canonical pattern.

On the first launcher boot after upgrading to v0.2.40 L1.M:

1. **READ** the value from `username='VIBECODED_LICENSE_KEY'`.
2. **WRITE** the value to `username='license_key____orchestrator__'`.
3. **DELETE** the legacy entry.
4. **UPSERT** the `license_keys` SQL row at the canonical username.

The write-before-delete order is intentional: if step 3 crashes after
step 2, the value is safely at the canonical username — no data loss.
The next launcher boot will see the canonical entry present and the
legacy entry already gone, and short-circuit harmlessly.

**For downstream consumers**: after the v0.2.40-L1.M upgrade, the
legacy username is no longer used by any production code path. If you
have a hook or MCP that still hardcodes `VIBECODED_LICENSE_KEY`, switch
it to `keychain_username_for(ORCHESTRATOR_MODULE_ID)` (Rust) or the
literal `license_key____orchestrator__` (shell / Python). A consumer
that still reads the legacy username after upgrade will silently miss
the key.

## Per-paid-module keys

Each paid module (RL Reranker, MAO, future agent packs) has its own
license key keyed by manifest `module_id`. Examples:

| Module                 | module_id          | keychain username           |
|------------------------|--------------------|-----------------------------|
| Orchestrator root tier | `__orchestrator__` | `license_key____orchestrator__` |
| RL Reranker            | `vct-rl-reranker`  | `license_key__vct-rl-reranker`  |
| MAO                    | `vct-mao`          | `license_key__vct-mao`          |

To discover whether a per-module key is set, read the row from
`launcher.db license_keys` (preferred — it carries the keychain
coordinates explicitly via the `keychain_username` column) or call
`keychain_username_for(module_id)` and probe the keychain directly.

## Related files

- `launcher/src-tauri/vct-launcher-core/src/db/license_keys.rs` —
  canonical helpers + DB schema for the `license_keys` table.
- `launcher/src-tauri/src/commands/licensing.rs` — Tauri commands that
  read/write license keys (`license_activate`, `license_refresh`,
  `set_module_license_key`, etc.) plus the L1.M migration helper
  `ensure_legacy_orchestrator_row_migrated`.
- `launcher/src-tauri/vct-launcher-core/src/secrets.rs` — the underlying
  `secrets::get/set/delete` API the launcher uses to talk to the OS
  keychain.

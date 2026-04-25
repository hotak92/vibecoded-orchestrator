# Known Issues — VCT Launcher

This file tracks deferred work that ships with the OSS launcher. Items are
acceptable for the v1 OSS launch; some are blockers for paid multi-tenant
tier and are tracked accordingly.

<!-- Sister agent (polish-and-bulk-ops) will append polish-grade items
     above this divider. The "Multi-tenant infrastructure" section below
     is owned by the multitenant-infrastructure branch. -->

---

## Multi-tenant infrastructure (P5/P6/P7)

### P7 — Concurrency invalidation: poll-based v1

The launcher implements a 5-second poll against an append-only
`change_log` table to keep multiple windows / tabs in sync. This was
chosen over a full Tauri-event push for simplicity and correctness:

- **What works**: any mutation that goes through `Db::audit` (which is
  most of them) emits a `change_log` row. The frontend polls
  `poll_changes(since)` every 5s and invalidates registered store
  listeners. The project list re-fetches automatically when any window
  edits a project.
- **What's deferred to v2**: per-store subscriptions are wired only for
  the `projects` table at the layout level. Other stores (KG access,
  codegraph access, secrets, hooks, hooks toggles, license tier) are
  NOT yet subscribed — they refresh on navigation, not on cross-window
  mutation.
- **Latency**: up to 5 s between cross-window mutation and UI refresh.
  Acceptable for human-interaction workflows; potentially noticeable
  for rapid back-and-forth between two windows.
- **Mitigation if missing**: every data view should keep a manual
  "Refresh" button so a user can force a re-fetch. (Today most do.)

The full real-time push is a v2 feature; the polling API surface stays
stable, so the upgrade is additive.

### P6 — CLI license activation is partial

`vct license activate <key>` writes the key to `~/.vct/license.key` and
records an audit event, but does NOT call the remote tier-validation
service. The launcher GUI performs the actual remote validation on its
next refresh cycle. CLI-only headless validation (for CI environments
that never open the GUI) is tracked as a follow-up.

### P6 — CLI module install is GUI-only

`vct module install` is intentionally not implemented. Install spawns
subprocesses that are tied to the Tauri app handle (event emission,
progress streaming). Use the launcher GUI for installs; the CLI can
list the catalog and the installed modules.

### P6 — KG/codegraph search not in CLI

`vct kg search` / `vct codegraph search` were on the v1 wishlist but are
not implemented yet. They require Weaviate connectivity that the hub
does not currently proxy. Tracked for a follow-up.

### P5 — Slug regenerated on rename

Renaming a project regenerates its slug from the new name. Existing
bookmarks to the old slug return a friendly "project not found" page
rather than silently redirecting. This is intentional — silent
redirection across renames would be a much harder semantic to reason
about (especially with name collisions). Power users should re-bookmark
after renames; documented in `docs/MULTI_TENANT_URLS.md`.

# Known Issues — VCT Launcher

Documented behaviours and limitations of the launcher.

---

## Cross-window invalidation: 5-second poll

The launcher uses a 5-second poll against an append-only
`change_log` table to keep multiple windows / tabs in sync.

- **What works**: any mutation that goes through `Db::audit` (which is
  most of them) emits a `change_log` row. The frontend polls
  `poll_changes(since)` every 5s and invalidates registered store
  listeners. The project list re-fetches automatically when any window
  edits a project.
- **What's not subscribed**: per-store subscriptions are wired only for
  the `projects` table at the layout level. Other stores (KG access,
  codegraph access, secrets, hooks, hooks toggles, license tier)
  refresh on navigation, not on cross-window mutation.
- **Latency**: up to 5 s between cross-window mutation and UI refresh.
  Acceptable for human-interaction workflows; potentially noticeable
  for rapid back-and-forth between two windows.
- **Workaround**: every data view exposes a manual "Refresh" button to
  force a re-fetch.

## CLI license activation is offline-only

`vco license activate <key>` writes the key to `~/.vct/license.key` and
records an audit event, but does NOT call the remote tier-validation
service. The launcher GUI performs the actual remote validation on its
next refresh cycle. To activate a license headlessly, run the GUI once
on the same machine.

## CLI module install is GUI-only

`vco module install` is not implemented. Install spawns
subprocesses that are tied to the Tauri app handle (event emission,
progress streaming). Use the launcher GUI for installs; the CLI can
list the catalog and the installed modules.

## Slug regenerated on rename

Renaming a project regenerates its slug from the new name. Existing
bookmarks to the old slug return a "project not found" page rather
than silently redirecting (silent redirection across renames is hard
to reason about with name collisions). Re-bookmark after renames;
documented in `docs/MULTI_TENANT_URLS.md`.

# Known Issues

Tracking polish-grade items that ship with the launcher but are worth
flagging for early adopters and the next iteration.

## Visual / UX

- [ ] **Tauri-runtime visual verification of per-project color accent** —
      the palette has 6 colorblind-safe hues per Wong 2011, and the
      browser preview confirms the CSS plumbing (5px header strip + tinted
      project-name pill + sidebar dots), but a real-render visual
      distinction across 3+ projects under Tauri's bundled WebKit has
      not been verified end-to-end. Worth re-checking under a
      colorblind simulator before claiming the multi-tenant
      "which project am I in?" recognition path is fully closed.

- [ ] **Per-project URL-addressable routes** (`/p/<slug>/kg`,
      `/p/<slug>/coordination`, etc.) are not wired yet. Project
      identity lives in localStorage; switching projects in the
      MenuBar selector retitles the page in place. Bookmarking a
      specific project's KG view or sharing a deep link with a
      teammate is therefore not supported. Tracked separately on a
      sister branch.

- [ ] **CLI escape hatch for headless / scripted use** — the launcher
      has no `vct-cli` companion yet, so headless installs / project
      bootstrapping must go through the Tauri UI. Tracked separately.

- [ ] **Concurrency invalidation for multi-tab use** — opening the
      same project in two windows can race on stale data; there is
      no cross-window invalidation bus yet. Single-window use is
      unaffected. Tracked separately.

## Server-side audit filtering

- [ ] **Audit log filters are client-side over a 500-event window.**
      Project filter is pushed to SQLite, but time-range and search
      are computed in the browser after the fetch. For NDA-bound
      consultant work spanning hundreds of thousands of events,
      consider extending `list_audit_events` with `since_ms` /
      `until_ms` / `actor` parameters and pushing the filter into
      `db.audit_list`. Sufficient for the current scale (low
      thousands of rows).

## Known Issues

Tracking polish-grade items that ship with the launcher but are worth
flagging for early adopters and the next iteration.

## Visual / UX

- [ ] **Tauri visual QA of per-project accent** — verify by running
      `npm run tauri:dev`, creating 3 projects, switching between them
      in the MenuBar selector, and confirming the 5px strip color +
      tinted project-name pill change distinctly per project (Wong 2011
      colorblind-safe palette). Browser preview confirms the CSS
      plumbing; only the bundled WebKit render path remains untested
      end-to-end. Delete this entry after manual verification.

## Recently fixed

- **Audit log filters pushed into SQL.** `Db::audit_list` now accepts
  `project_id`, `actor`, `since_ms`, `until_ms`, `search` (substring
  match against `operation` OR `detail`) and a per-call `limit` capped
  at 10000. The `/audit` route, the `list_audit_events` Tauri command
  and the hub `/cli/audit` endpoint all forward these directly to the
  query; the frontend no longer post-filters a 500-row window in JS.
  Free-text inputs are debounced (250ms) to avoid spamming SQL on each
  keystroke.

- **Per-project URL-addressable routes** at `/p/<slug>/...` shipped in
  P5 (migration 003 + slug resolution).

- **CLI escape hatch** shipped in P6 as `launcher/tools/vct-cli/` plus
  the hub `/cli/*` HTTP API.

- **Concurrency invalidation for multi-tab use** shipped in P7 via the
  `change_log` table + `poll_changes` Tauri command (5s polling).

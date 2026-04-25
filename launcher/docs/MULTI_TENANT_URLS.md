# Multi-tenant URLs

The launcher exposes URL-addressable per-project routes so power users can
bookmark and share deep links scoped to a specific tenant.

## URL pattern

```
/p/<slug>            redirect to the last-visited section for this project
                     (default /kg if no history)
```

`<slug>` is a URL-friendly identifier for the project, generated from its
name (lowercase, ASCII alphanumerics and dashes). Collisions are resolved
with a numeric suffix (e.g. `acme`, `acme-2`, `acme-3`).

## Behaviour

Visiting `/p/<slug>` does the following, in order:

1. Loads the project list (so MenuBar / Sidebar update).
2. Calls `get_project_by_slug(slug)`.
3. If the slug resolves: sets that project as the active selection (the
   global `selectedProject` store), then `goto`s the section the user
   last viewed for this project (or `/kg` as the default), with
   `replaceState: true` so the browser back button skips the redirect.
4. If the slug does not resolve: shows a "project not found" card with
   a link back to `/project`.

The "remembered last section" is tracked per-project-id in
localStorage (`vct.last_section.<id>`), updated by `+layout.svelte`
whenever the user is browsing one of the whitelisted sections (`/kg`,
`/codegraph`, `/coordination`, `/audit`, `/project`, `/hub`, `/mcp`,
`/telemetry`).

## Slug lifecycle

- **Created**: at project creation. `db.generate_unique_slug(name)`
  produces a stable, unique slug.
- **Renamed**: project rename regenerates the slug from the new name.
  Old bookmarks 404 with a friendly message — they do NOT silently
  redirect to a different project.
- **Deleted**: slugs are released back to the pool when a project is
  deleted; a future project named the same will reuse the slug.

## Why a redirect-only route, not forked sections?

We considered building `/p/<slug>/kg`, `/p/<slug>/codegraph`, etc. as
fully separate routes. We chose the redirect approach instead because:

- **Less code**. A single `/p/[slug]/+page.svelte` covers every section.
- **Single source of truth**. There is one canonical `/kg` route; the
  `/p/<slug>/...` URLs are entry points, not duplicates.
- **No routing-coupled bugs**. Existing `/kg`, `/codegraph`, etc.
  already gate on `selectedProject`. Setting the selection then
  redirecting reuses that path exactly.

A future iteration MAY introduce `/p/<slug>/<section>` deep links
once the use case for sub-section bookmarks (e.g. `/p/acme/kg/<node-id>`)
is concrete. The current redirect approach is forward-compatible — those
URLs would be added without breaking `/p/<slug>` semantics.

## Programmatic access

- Tauri command: `get_project_by_slug(slug: string) -> ProjectView | null`
- Hub HTTP: `GET /api/v1/projects/by-slug/<slug>` returns the same
  payload as `GET /api/v1/projects/<id>`.
- CLI: `vct project switch <slug>` (P6) or open the URL in any browser
  pointed at the launcher.

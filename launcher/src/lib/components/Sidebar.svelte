<script lang="ts">
  // Left sidebar — primary nav. Groups every v1.0/v1.1 route so users can
  // discover them without URL guessing. Each entry has a 1-line subtitle in
  // plain English so non-tech users have a chance.
  //
  // The "Projects" link is dynamic: if a project is selected it points at
  // /project/<id>, otherwise it falls back to /project (which renders an
  // "no project selected" state).

  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { selectedProject } from '$lib/stores/projects';
  import { license } from '$lib/stores/license';
  import { invoke, tauriAvailable } from '$lib/tauri';

  type NavItem = {
    href: string;
    label: string;
    sub: string;
    /** match() returns true if the route is "active" for this item. */
    match: (path: string) => boolean;
    /** When true, this item requires an active Pro/MAO/admin license. Free
     *  tier users see it greyed out + a 'Pro' badge; clicking routes to
     *  /store instead of the gated page. The Team-section v1.x rollout
     *  surfaces the affordance early so users discover the upgrade path
     *  before the section's full UI lands. */
    proOnly?: boolean;
  };

  type NavGroup = {
    label: string;
    items: NavItem[];
  };

  // Stream 2 (2026-05-19): module-contributed nav items. Loaded once on
  // mount via the `get_module_nav_items` Tauri command. Each entry
  // carries the full ConfigTab schema; the renderer at
  // /modules/[id]/config/+page.svelte looks up the schema from this
  // list rather than refetching. Soft-fail: command failure leaves the
  // list empty (and the user sees only the static nav groups).
  interface ModuleConfigTab {
    title: string;
    icon: string | null;
    route: string | null;
    description: string | null;
    sections: unknown[];
    /** v0.2.23 F2 (2026-05-21): when `false`, the manifest opts the
     *  module out of the "Module configuration" group below — the
     *  config tab is still discoverable via `get_module_nav_items`
     *  (e.g. for embedding inside the per-project Settings page), but
     *  the standalone sidebar entry is suppressed. Optional in the
     *  wire shape; Rust's serde default fills `true` when omitted,
     *  but we tolerate `undefined` here too in case an older host
     *  ever returns the field stripped. */
    show_in_sidebar?: boolean;
  }
  interface ModuleNavItem {
    module_id: string;
    title: string;
    icon: string | null;
    route: string;
    config_tab: ModuleConfigTab;
  }

  let moduleNavItems = $state<ModuleNavItem[]>([]);

  onMount(async () => {
    if (!tauriAvailable()) return;
    try {
      moduleNavItems = await invoke<ModuleNavItem[]>('get_module_nav_items');
    } catch (e) {
      // Logged but not surfaced — a failed nav-items fetch is far less
      // bad than blocking the rest of the sidebar from rendering.
      console.warn('[sidebar] get_module_nav_items failed:', e);
    }
  });

  const projectId = $derived($selectedProject?.id ?? null);
  // PR-5 (v0.2.11): show an [ORCHESTRATOR] chip in the sidebar header
  // when the currently selected project is the Orchestrator Project.
  const isOrchestratorSelected = $derived($selectedProject?.host === 'orchestrator_root');
  // Server-classified admin tier unlocks the Admin sidebar group with
  // dev-only routes. The check is on the cached tier string — patching
  // this client-side reveals the routes but not the server-gated
  // capabilities they exercise.
  const isAdmin = $derived(($license.cache?.orchestrator_tier ?? 'free') === 'admin');

  /** True when the current orchestrator tier unlocks Pro-gated UI
   *  surfaces (Team section etc.). `pro`, `mao`, `enterprise`, and
   *  `admin` all qualify; `free` does not. Server-side gates back
   *  this client-side hint: a free user clicking a proOnly link gets
   *  redirected to /store, and the underlying Tauri commands the
   *  page would call also enforce tier. */
  const hasPro = $derived.by(() => {
    const tier = $license.cache?.orchestrator_tier ?? 'free';
    return tier === 'pro' || tier === 'mao' || tier === 'enterprise' || tier === 'admin';
  });

  const groups = $derived<NavGroup[]>([
    {
      label: 'Workspace',
      items: [
        {
          href: '/',
          label: 'Home',
          sub: 'Your library and tool store',
          match: (p) => p === '/',
        },
        {
          href: '/modules',
          label: 'Modules',
          sub: 'Install and manage Orchestrator modules',
          match: (p) => p.startsWith('/modules'),
        },
        {
          // Bug 5: Store needs to be a discoverable route. Sits between
          // Modules and Project so users browsing for tools find it
          // before they look at per-project state.
          href: '/store',
          label: 'Store',
          sub: 'Pro plan, Multi-Agent Orchestrator, and other tools',
          match: (p) => p.startsWith('/store'),
        },
        {
          href: '/projects',
          label: 'Project',
          sub: 'Agents, skills, hooks, permissions, secrets',
          match: (p) => p.startsWith('/project'),
        },
      ],
    },
    {
      label: 'Knowledge',
      items: [
        {
          href: '/kg',
          label: 'Knowledge Graph',
          sub: 'Browse what your project remembers',
          match: (p) => p.startsWith('/kg'),
        },
        {
          href: '/codegraph',
          label: 'Code Graph',
          sub: 'Browse what your code knows about itself',
          match: (p) => p.startsWith('/codegraph'),
        },
        // Bug 7: Glossary moved to the bottom of the System group so
        // Knowledge stays focused on actual knowledge surfaces.
      ],
    },
    {
      label: 'Team',
      items: [
        {
          href: '/coordination',
          label: 'Coordination',
          sub: 'Send and receive team messages',
          match: (p) => p.startsWith('/coordination'),
          proOnly: true,
        },
        {
          href: '/hub',
          label: 'Hub',
          sub: 'Cross-tool data and apps',
          match: (p) => p.startsWith('/hub'),
          proOnly: true,
        },
      ],
    },
    {
      label: 'System',
      items: [
        {
          href: '/services',
          label: 'Services',
          sub: 'Start/stop Weaviate, Ollama, code-embed',
          match: (p) => p.startsWith('/services'),
        },
        {
          href: '/mcp',
          label: 'Custom MCP servers',
          sub: 'Add Claude capabilities (web search, etc.)',
          match: (p) => p.startsWith('/mcp'),
        },
        {
          href: '/telemetry',
          label: 'Telemetry',
          sub: 'Anonymous usage stats and consent',
          match: (p) => p.startsWith('/telemetry'),
        },
        {
          href: '/preferences',
          label: 'Preferences',
          sub: 'Tray, updates, behavior',
          // Exclude /preferences/secrets so only the Secrets entry below
          // lights up when the user is on the secrets pages.
          match: (p) => p.startsWith('/preferences') && !p.startsWith('/preferences/secrets'),
        },
        {
          // Wired in PR-4 of v0.2.11 — mounts SecretsPanel (shipped
          // v0.2.7) + SecretsImportPanel (v0.2.8) at navigable routes.
          // The match pattern covers both /preferences/secrets and
          // /preferences/secrets/import.
          href: '/preferences/secrets',
          label: 'Secrets',
          sub: 'Keychain entries used across projects',
          match: (p) => p.startsWith('/preferences/secrets'),
        },
        // Bug 9: Audit demoted to second-to-last position. Most users
        // never need it; keeping it visible but unobtrusive.
        {
          href: '/audit',
          label: 'Audit log',
          sub: 'History of every change to your projects — useful for compliance and rollback.',
          match: (p) => p.startsWith('/audit'),
        },
        // Bug 7: Glossary at the very bottom of the sidebar — reference
        // material, not a primary nav target.
        {
          href: '/glossary',
          label: 'Glossary',
          sub: 'Plain-English explanations of terms',
          match: (p) => p.startsWith('/glossary'),
        },
      ],
    },
    // Stream 2 (2026-05-19): module-contributed nav items. Renders as
    // its own "Module configuration" group sandwiched between System
    // and Admin. Each entry's tooltip = the manifest's
    // `gui.config_tab.description` (falls back to the title). Hidden
    // entirely when no modules contribute a config_tab (avoids a stray
    // empty group label).
    //
    // v0.2.23 F2 (2026-05-21): manifests can opt out of this group by
    // declaring `gui.config_tab.show_in_sidebar: false`. We still keep
    // them in `moduleNavItems` (consumers like the per-project Settings
    // page need to look up the orchestrator-core schema by id), but
    // filter them OUT of the sidebar nav. Rust's serde default
    // populates `true` when the field is absent in the manifest, so
    // the strict equality below matches both legacy (undefined) and
    // explicit-true cases.
    ...((() => {
      const visibleItems = moduleNavItems.filter(
        (mod) => mod.config_tab.show_in_sidebar !== false,
      );
      return visibleItems.length > 0
        ? [
            {
              label: 'Module configuration',
              items: visibleItems.map<NavItem>((mod) => ({
                href: mod.route,
                label: mod.title,
                sub: mod.config_tab.description ?? mod.title,
                match: (p) => p === mod.route || p.startsWith(`${mod.route}/`),
              })),
            } satisfies NavGroup,
          ]
        : [];
    })()),
    // Bug 33: Admin group — visible only when the cached tier is "admin".
    // Dev-only affordances. Routes also re-check tier server-side via
    // the standard validate-tier flow before exposing data, so a forged
    // client tier yields nothing it shouldn't.
    ...(isAdmin
      ? [
          {
            label: 'Admin',
            items: [
              {
                href: '/admin/feature-flags',
                label: 'Feature flags',
                sub: 'Toggle pre-release modules and dev features',
                match: (p) => p.startsWith('/admin/feature-flags'),
              },
              {
                href: '/admin/diagnostic',
                label: 'Diagnostics',
                sub: 'License + container + KG state inspector',
                match: (p) => p.startsWith('/admin/diagnostic'),
              },
              {
                href: '/admin/license-issuance-test',
                label: 'License test',
                sub: 'Issue and verify test licenses',
                match: (p) => p.startsWith('/admin/license-issuance-test'),
              },
            ],
          } satisfies NavGroup,
        ]
      : []),
  ]);

  const currentPath = $derived($page.url.pathname);
</script>

<aside class="sidebar">
  {#if isOrchestratorSelected}
    <div class="sidebar-orch-header" aria-label="Orchestrator Project active">
      <span class="sidebar-orch-chip">ORCHESTRATOR</span>
    </div>
  {/if}
  <nav class="sidebar-nav">
    {#each groups as group}
      <div class="group">
        <div class="group-label">{group.label}</div>
        {#each group.items as item}
          {@const active = item.match(currentPath)}
          {@const locked = item.proOnly === true && !hasPro}
          <a
            class="nav-item"
            class:active
            class:locked
            href={locked ? '/store' : item.href}
            title={locked
              ? `${item.label} is a Pro feature. Click to activate Pro.`
              : item.sub}
          >
            <span class="nav-label">
              {item.label}
              {#if locked}
                <span class="nav-pro-badge">Pro</span>
              {/if}
            </span>
            <span class="nav-sub">{item.sub}</span>
          </a>
        {/each}
      </div>
    {/each}
  </nav>
</aside>

<style>
  .sidebar {
    width: 220px;
    flex-shrink: 0;
    background: rgba(8, 15, 40, 0.6);
    border-right: 1px solid rgba(255, 255, 255, 0.04);
    overflow-y: auto;
    padding: 16px 8px;
  }

  /* PR-5 (v0.2.11): orchestrator indicator at the top of the sidebar. */
  .sidebar-orch-header {
    padding: 6px 10px 4px;
    margin-bottom: 4px;
  }
  .sidebar-orch-chip {
    display: inline-block;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 2px 7px;
    border-radius: 4px;
    background: rgba(0, 191, 166, 0.12);
    color: var(--color-teal, #00bfa6);
    border: 1px solid rgba(0, 191, 166, 0.30);
    line-height: 1.5;
  }

  .sidebar-nav {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .group {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .group-label {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--color-muted);
    padding: 6px 10px 4px;
  }

  .nav-item {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 7px 10px;
    border-radius: 8px;
    color: var(--color-mid);
    text-decoration: none;
    transition: all 0.12s ease;
  }

  .nav-item:hover {
    background: rgba(255, 255, 255, 0.04);
    color: var(--color-text);
  }

  .nav-item.active {
    background: rgba(0, 191, 166, 0.10);
    color: var(--color-text);
  }

  .nav-label {
    font-size: 13px;
    font-weight: 600;
    line-height: 1.2;
  }

  .nav-sub {
    font-size: 11px;
    color: var(--color-muted);
    line-height: 1.3;
  }

  .nav-item.active .nav-sub {
    color: var(--color-mid);
  }

  /* Pro-gated items on free tier: greyed out + Pro badge. Still
   * clickable (routes to /store instead of the gated page) so users
   * discover the upgrade path. */
  .nav-item.locked {
    opacity: 0.55;
  }

  .nav-item.locked:hover {
    opacity: 0.85;
  }

  .nav-label {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .nav-pro-badge {
    display: inline-block;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 1px 5px;
    border-radius: 3px;
    background: rgba(241, 196, 15, 0.18);
    color: #f1c40f;
    line-height: 1;
  }
</style>

<script lang="ts">
  // Left sidebar — primary nav. Groups every v1.0/v1.1 route so users can
  // discover them without URL guessing. Each entry has a 1-line subtitle in
  // plain English so non-tech users have a chance.
  //
  // The "Projects" link is dynamic: if a project is selected it points at
  // /project/<id>, otherwise it falls back to /project (which renders an
  // "no project selected" state).

  import { page } from '$app/stores';
  import { selectedProject } from '$lib/stores/projects';
  import { license } from '$lib/stores/license';

  type NavItem = {
    href: string;
    label: string;
    sub: string;
    /** match() returns true if the route is "active" for this item. */
    match: (path: string) => boolean;
  };

  type NavGroup = {
    label: string;
    items: NavItem[];
  };

  const projectId = $derived($selectedProject?.id ?? null);
  // Server-classified admin tier unlocks the Admin sidebar group with
  // dev-only routes. The check is on the cached tier string — patching
  // this client-side reveals the routes but not the server-gated
  // capabilities they exercise.
  const isAdmin = $derived(($license.cache?.orchestrator_tier ?? 'free') === 'admin');

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
        },
        {
          href: '/hub',
          label: 'Hub',
          sub: 'Cross-tool data and apps',
          match: (p) => p.startsWith('/hub'),
        },
      ],
    },
    {
      label: 'System',
      items: [
        {
          href: '/services',
          label: 'Services',
          sub: 'Start/stop Weaviate, Ollama, SearXNG, code-embed',
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
          match: (p) => p.startsWith('/preferences'),
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
  <nav class="sidebar-nav">
    {#each groups as group}
      <div class="group">
        <div class="group-label">{group.label}</div>
        {#each group.items as item}
          {@const active = item.match(currentPath)}
          <a class="nav-item" class:active href={item.href}>
            <span class="nav-label">{item.label}</span>
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
</style>

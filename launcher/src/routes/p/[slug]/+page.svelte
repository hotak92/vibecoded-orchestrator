<script lang="ts">
  // URL-addressable project loader: /p/<slug>
  //
  // This is the entry point for bookmark-friendly per-project URLs (P5).
  // Behaviour:
  //   1. Read `slug` from the route params.
  //   2. Resolve the slug to a project via `get_project_by_slug` (Tauri)
  //      or `/api/v1/projects/by-slug/<slug>` (browser).
  //   3. If found: set as selected project, then redirect to the
  //      remembered last-section for this project (default `/kg`).
  //   4. If not found: show a friendly 404-ish message with a link
  //      back to `/project`.
  //
  // The URL pattern is documented in docs/MULTI_TENANT_URLS.md.

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { projects } from '$lib/stores/projects';
  import { safeInvoke } from '$lib/tauri';
  import type { ProjectView } from '$lib/types/launcher';

  const LAST_SECTION_KEY_PREFIX = 'vct.last_section.';

  let phase = $state<'loading' | 'redirecting' | 'not-found' | 'error'>('loading');
  let errorMsg = $state<string | null>(null);
  let attemptedSlug = $state<string>('');

  function rememberedSection(projectId: string): string {
    if (typeof localStorage === 'undefined') return '/kg';
    const v = localStorage.getItem(LAST_SECTION_KEY_PREFIX + projectId);
    // Whitelist of allowed sections to prevent open-redirect-style abuse
    // even though we control the value ourselves.
    const allowed = new Set(['/kg', '/codegraph', '/coordination', '/audit', '/project', '/hub', '/mcp', '/telemetry']);
    if (v && allowed.has(v)) return v;
    return '/kg';
  }

  async function resolveAndRedirect(slug: string) {
    attemptedSlug = slug;
    phase = 'loading';
    errorMsg = null;

    try {
      // Make sure the project list is populated so MenuBar / Sidebar
      // reflect the selection immediately on arrival.
      await projects.load();

      const proj = await safeInvoke<ProjectView | null>('get_project_by_slug', { slug });
      if (!proj) {
        phase = 'not-found';
        return;
      }

      projects.select(proj.id);
      phase = 'redirecting';
      const target = rememberedSection(proj.id);
      goto(target, { replaceState: true });
    } catch (e) {
      phase = 'error';
      errorMsg = e instanceof Error ? e.message : String(e);
    }
  }

  onMount(() => {
    const slug = $page.params.slug;
    if (!slug) {
      phase = 'not-found';
      return;
    }
    resolveAndRedirect(slug);
  });
</script>

<div class="page">
  {#if phase === 'loading' || phase === 'redirecting'}
    <div class="card">
      <p class="status">Loading project…</p>
      <p class="meta">Slug: <code>{attemptedSlug}</code></p>
    </div>
  {:else if phase === 'not-found'}
    <div class="card">
      <p class="status">Project not found</p>
      <p class="meta">
        No project is registered with slug <code>{attemptedSlug}</code>.
        It may have been renamed or deleted.
      </p>
      <p class="meta">
        <a href="/project">Back to projects</a>
      </p>
    </div>
  {:else if phase === 'error'}
    <div class="card">
      <p class="status">Could not load project</p>
      <p class="meta">{errorMsg}</p>
      <p class="meta">
        <a href="/project">Back to projects</a>
      </p>
    </div>
  {/if}
</div>

<style>
  .page {
    padding: 60px 28px;
    display: flex;
    justify-content: center;
  }
  .card {
    max-width: 480px;
    padding: 28px 32px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 12px;
    text-align: center;
  }
  .status {
    font-size: 16px;
    font-weight: 700;
    color: var(--color-text);
    margin-bottom: 8px;
  }
  .meta {
    font-size: 13px;
    color: var(--color-mid);
    margin-top: 6px;
  }
  code {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 12px;
    color: var(--color-text);
    background: rgba(255, 255, 255, 0.06);
    padding: 1px 6px;
    border-radius: 4px;
  }
  a {
    color: var(--color-teal);
    text-decoration: none;
  }
  a:hover {
    text-decoration: underline;
  }
</style>

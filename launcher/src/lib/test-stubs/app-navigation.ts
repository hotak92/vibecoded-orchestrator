// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.93: vitest-only stand-in for SvelteKit's `$app/navigation` virtual
// module (see the alias in `launcher/vitest.config.ts`). The runtime app
// never imports this file — SvelteKit resolves the real module. It exists
// so store modules that import `goto` (e.g. `stores/ui.ts`) can be loaded
// by the node-environment unit tests without standing up the kit pipeline.
//
// Every export is an inert no-op; tests that care about navigation should
// `vi.mock('$app/navigation', ...)` themselves.

export async function goto(_url: string | URL, _opts?: unknown): Promise<void> {
  // no-op
}

export function afterNavigate(_cb: unknown): void {
  // no-op
}

export function beforeNavigate(_cb: unknown): void {
  // no-op
}

export async function invalidateAll(): Promise<void> {
  // no-op
}

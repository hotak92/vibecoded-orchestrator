// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 (#32) — pure decision logic for the New Project modal's
// Name↔Path coupling, extracted from ProjectSelector.svelte so it is
// unit-testable without mounting the component (mirrors
// codegraph-build-banner-logic.ts / regenerate-modal-logic.ts).
//
// The hazard these functions pin: while the path is untouched, the
// reactive Name→Path sync rewrites it on every keystroke — so the browse
// handler MUST mark the path as touched, or a later rename would silently
// replace the browsed folder with `~/code/<slug>`. That guard was ALREADY
// present in ProjectSelector.svelte at base (#32's initial "browse loses
// the path" field diagnosis was RETRACTED — the clobber could not occur).
// This change is hardening + UX copy, not a bugfix: it extracts the logic
// to pure functions, PINS the guard with the tests below, and adds the
// auto-derive hint next to the path field.

/** Mutable slice of the create-modal state these transitions operate on. */
export interface PathFieldState {
  path: string;
  touched: boolean;
}

/** kebab-case slug for the folder name, fallback handled by deriveAutoPath. */
export function slugifyProjectName(s: string): string {
  return s
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64);
}

/** The auto-derived path shown while the user has not touched the field. */
export function deriveAutoPath(root: string, name: string): string {
  return `${root || '~/code'}/${slugifyProjectName(name) || 'my-project'}`;
}

/**
 * The reactive Name→Path sync decision: returns the derived path to write,
 * or `null` when the field must be LEFT ALONE (modal closed, or the user
 * already touched the path — by typing OR by browsing).
 */
export function autoDerivedPath(
  showCreate: boolean,
  pathTouched: boolean,
  root: string,
  name: string,
): string | null {
  if (!showCreate || pathTouched) return null;
  return deriveAutoPath(root, name);
}

/**
 * openCreate seeding: a leftover path from a previous open (cancel/close
 * does not clear it) counts as touched, so reopening never clobbers it.
 */
export function seedPathTouched(existingPath: string | undefined): boolean {
  return existingPath !== '' && existingPath !== undefined;
}

/**
 * Browse-result transition. A successful pick REPLACES the path AND marks
 * it touched — the guard (present at base, pinned by the tests) that
 * keeps the reactive Name→Path sync from ever re-deriving over a browsed
 * folder. A cancelled pick (`null`/empty) leaves the state untouched.
 */
export function onBrowsePicked(
  state: PathFieldState,
  picked: string | null,
): PathFieldState {
  if (!picked) return state;
  return { path: picked, touched: true };
}

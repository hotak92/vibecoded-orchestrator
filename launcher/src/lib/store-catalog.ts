// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// What the launcher's Store page (`routes/store/+page.svelte`) lists — the
// ONE home of that decision (owner ruling 2026-11-12).
//
//   1. The announced products, declared below in `STORE_COMING_SOON`:
//      Transcrypt and VCT Coordination. Both render "Coming soon" with no
//      install, buy or activate action.
//   2. Any PAID module the L0 catalog (`list_module_catalog`, entries whose
//      `manifest_source` is `L0:<id>`) lists that is not one of the above.
//      None is listed today; the path stays so a real paid module published
//      to L0 later renders here with no launcher change. Its card sends the
//      user to the Modules page, where install and licensing live.
//
// Never listed: free modules, bundled modules and subcomponents. The RL
// reranker is free and bundled since v0.2.106, so it is not a store item;
// it is managed from the Modules page tile and the project's RL panel.

import type { BrandColor } from './color-rgb';
import type { ModuleCatalogEntry } from './types/launcher';

export type StoreAvailability = 'coming_soon' | 'catalog';

export interface StoreEntry {
  id: string;
  name: string;
  desc: string;
  color: BrandColor;
  icon: string;
  /** Empty for coming-soon entries (nothing is released yet). */
  version: string;
  availability: StoreAvailability;
  /** The catalog `kind` for a catalog entry; `coming_soon` for the pair. */
  catalogKind: ModuleCatalogEntry['kind'];
}

/** The announced products, in display order. Edit HERE only. */
export const STORE_COMING_SOON: readonly StoreEntry[] = Object.freeze([
  {
    id: 'transcrypt',
    name: 'Transcrypt',
    desc: 'Audio transcription with AI-powered correction and vocabulary support.',
    color: 'teal',
    icon: 'T',
    version: '',
    availability: 'coming_soon',
    catalogKind: 'coming_soon',
  },
  {
    id: 'vct-coordination',
    name: 'VCT Coordination',
    desc: "Team coordination for Claude Code: shared channels, direct messages and presence across your team's sessions.",
    color: 'purple',
    icon: 'C',
    version: '',
    availability: 'coming_soon',
    catalogKind: 'coming_soon',
  },
]);

const ROTATION: readonly BrandColor[] = ['pink', 'teal', 'purple'];

/** True for a catalog entry the Store lists: a paid module published to L0. */
export function isStoreCatalogModule(m: ModuleCatalogEntry): boolean {
  return (
    m.manifest_source.startsWith('L0:') &&
    m.license_required &&
    m.kind !== 'bundled' &&
    m.kind !== 'subcomponent'
  );
}

/**
 * The Store's entries: the declared coming-soon products first, then every
 * paid L0 module that is not one of them (a catalog record for an announced
 * id never replaces its coming-soon card).
 */
export function storeEntries(catalog: readonly ModuleCatalogEntry[]): StoreEntry[] {
  const declared = new Set(STORE_COMING_SOON.map((e) => e.id));
  const fromCatalog = catalog
    .filter((m) => isStoreCatalogModule(m) && !declared.has(m.id))
    .map<StoreEntry>((m, i) => ({
      id: m.id,
      name: m.name,
      desc: m.description,
      color: ROTATION[i % ROTATION.length],
      icon: (m.name.trim().charAt(0) || '?').toUpperCase(),
      version: m.version,
      availability: 'catalog',
      catalogKind: m.kind,
    }));
  return [...STORE_COMING_SOON.map((e) => ({ ...e })), ...fromCatalog];
}

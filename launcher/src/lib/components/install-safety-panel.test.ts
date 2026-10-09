// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

import { describe, it, expect } from 'vitest';
import {
  safetyRefusal,
  safetyWarnings,
  createsCollections,
  type SafetyReport,
} from './install-safety-panel';

const REFUSAL =
  'refusing to start containers: it exists only under podman, but this launcher runs under docker. Quit the launcher and start it from a shell where podman is the resolved runtime.';

function report(overrides: Partial<SafetyReport> = {}): SafetyReport {
  return {
    will_overwrite_orchestrator_files: [],
    will_preserve_user_code: ['my-app'],
    existing_volumes: [],
    existing_collections: ['KnowledgeGraph'],
    new_collections_to_add: [],
    services_running: ['weaviate'],
    services_to_start: ['ollama'],
    risks: [],
    ...overrides,
  };
}

describe('safetyRefusal (W5-B C7)', () => {
  it('null when the report is empty, absent, or has no refusal', () => {
    expect(safetyRefusal(null)).toBeNull();
    expect(safetyRefusal(undefined)).toBeNull();
    expect(safetyRefusal(report())).toBeNull();
    expect(
      safetyRefusal(report({ risks: ['Will overwrite orchestrator-managed path: .claude'] })),
    ).toBeNull();
  });

  it('returns the refusal line when a risk IS one (the volumes step shape)', () => {
    expect(
      safetyRefusal(report({ risks: ['some info line', REFUSAL] })),
    ).toBe(REFUSAL);
  });

  it('never mistakes an ordinary risk for a refusal', () => {
    // The regex requires the exact "refusing to …: it exists only under
    // <runtime>, but " shape; a lookalike must not block Continue.
    expect(
      safetyRefusal(report({ risks: ['we are refusing to run: it exists only under podman, but really'] })),
    ).toBeNull();
  });
});

describe('safetyWarnings (W5-B C7)', () => {
  it('returns every risk EXCEPT the refusal (the refusal has its own blocking panel)', () => {
    const out = safetyWarnings(report({ risks: ['a', REFUSAL, 'b'] }));
    expect(out).toEqual(['a', 'b']);
  });

  it('empty for an absent report', () => {
    expect(safetyWarnings(null)).toEqual([]);
  });
});

describe('createsCollections (W5-B C7)', () => {
  it('true only when the install will POST new classes', () => {
    expect(createsCollections(null)).toBe(false);
    expect(createsCollections(report())).toBe(false);
    expect(createsCollections(report({ new_collections_to_add: ['Development'] }))).toBe(true);
  });
});

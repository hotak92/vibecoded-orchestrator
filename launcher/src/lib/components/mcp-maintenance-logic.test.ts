// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 — tests for the MCP maintenance page's npx tri-state + retirement
// badge rendering decisions.

import { describe, it, expect } from 'vitest';
import {
  isUserReEnabledAfterRetirement,
  MCP_RETIRED_CONFIG_KEY,
  npxState,
  retiredBadgeText,
  retiredRows,
  showsCannotSpawnTag,
  type ProjectMcpServerView,
} from './mcp-maintenance-logic';

describe('npxState', () => {
  it('is `present` only on positive evidence', () => {
    expect(npxState({ npx_present: true, npx_path: '/usr/bin/npx', remediation: '' })).toBe(
      'present',
    );
  });

  it('is `missing` only on a probe that RAN and said no', () => {
    expect(npxState({ npx_present: false, npx_path: '', remediation: 'install node' })).toBe(
      'missing',
    );
  });

  it('is `unknown` when the probe could not run — NOT `missing`', () => {
    expect(npxState({ npx_present: null, npx_path: '', remediation: '' })).toBe('unknown');
    expect(npxState(null)).toBe('unknown');
    expect(npxState(undefined)).toBe('unknown');
  });

  it('never collapses unknown into missing', () => {
    expect(npxState({ npx_present: null, npx_path: '', remediation: '' })).not.toBe(
      npxState({ npx_present: false, npx_path: '', remediation: '' }),
    );
  });
});

describe('showsCannotSpawnTag', () => {
  it('tags an entry whose bare command provably does not resolve', () => {
    expect(showsCannotSpawnTag({ command_resolvable: false })).toBe(true);
  });

  it('leaves a resolvable entry alone', () => {
    expect(showsCannotSpawnTag({ command_resolvable: true })).toBe(false);
  });

  it('leaves an UNKNOWN entry alone — no accusation without evidence', () => {
    expect(showsCannotSpawnTag({ command_resolvable: null })).toBe(false);
  });
});

function row(partial: Partial<ProjectMcpServerView> & { mcp_name: string }): ProjectMcpServerView {
  return { enabled: false, config: {}, ...partial };
}

describe('retiredRows', () => {
  it('selects only rows carrying the durable badge', () => {
    const rows = [
      row({ mcp_name: 'weaviate-kg' }),
      row({
        mcp_name: 'old-mcp',
        config: { [MCP_RETIRED_CONFIG_KEY]: { removed_in: 'v0.2.91' } },
      }),
      row({ mcp_name: 'no-config', config: null }),
    ];
    expect(retiredRows(rows).map((r) => r.mcp_name)).toEqual(['old-mcp']);
  });

  it('is empty when nothing is retired (leave-alone: no card renders)', () => {
    expect(retiredRows([row({ mcp_name: 'a' }), row({ mcp_name: 'b' })])).toEqual([]);
  });
});

describe('retiredBadgeText', () => {
  it('renders the engine-composed sentence verbatim', () => {
    const r = row({
      mcp_name: 'x',
      config: {
        [MCP_RETIRED_CONFIG_KEY]: {
          removed_in: 'v0.2.91',
          reason: 'replaced by the diagrams tab',
          badge: 'retired in v0.2.91: replaced by the diagrams tab',
        },
      },
    });
    expect(retiredBadgeText(r)).toBe('retired in v0.2.91: replaced by the diagrams tab');
  });

  it('falls back to the version when a pre-composed badge is absent', () => {
    const r = row({
      mcp_name: 'x',
      config: { [MCP_RETIRED_CONFIG_KEY]: { removed_in: 'v0.2.90' } },
    });
    expect(retiredBadgeText(r)).toBe('retired in v0.2.90');
  });

  it('degrades to a bare word rather than throwing on a malformed badge', () => {
    expect(retiredBadgeText(row({ mcp_name: 'x', config: { [MCP_RETIRED_CONFIG_KEY]: 7 } }))).toBe(
      'retired',
    );
    expect(retiredBadgeText(row({ mcp_name: 'x', config: null }))).toBe('retired');
  });
});

describe('isUserReEnabledAfterRetirement', () => {
  it('is true for a retired row the user turned back on', () => {
    expect(
      isUserReEnabledAfterRetirement(
        row({
          mcp_name: 'x',
          enabled: true,
          config: { [MCP_RETIRED_CONFIG_KEY]: { removed_in: 'v0.2.91' } },
        }),
      ),
    ).toBe(true);
  });

  it('is false for a retired row that is still disabled', () => {
    expect(
      isUserReEnabledAfterRetirement(
        row({ mcp_name: 'x', config: { [MCP_RETIRED_CONFIG_KEY]: {} } }),
      ),
    ).toBe(false);
  });

  it('is false for an enabled row that was never retired', () => {
    expect(isUserReEnabledAfterRetirement(row({ mcp_name: 'x', enabled: true }))).toBe(false);
  });
});

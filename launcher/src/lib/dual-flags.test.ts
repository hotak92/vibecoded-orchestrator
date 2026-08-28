// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.91 WP-L — unit tests for the dual-flags panel's decision logic.
//
// The repo has no jsdom, so `.svelte` files are not unit-testable; these
// cover the parts of the panel that can actually be wrong: which segment of
// the tri-state control is active, what the delete-the-row value is, which
// badge and which "why" line render for each of the five reachable states,
// and that every scope-dependent string derives from the `scope` argument.

import { describe, it, expect } from 'vitest';
import {
  DUAL_FLAGS,
  TRI_CHOICE_LABELS,
  badgeFor,
  cascadeFootnote,
  effectiveLine,
  flagState,
  globalSaveSummary,
  mountConfigError,
  panelIntro,
  panelTitle,
  rlLogPrerequisiteNote,
  triChoiceFor,
  triChoiceToValue,
  type DualFlagKey,
  type DualFlagState,
  type DualFlagsState,
} from '$lib/dual-flags';

// ── fixtures ────────────────────────────────────────────────────────────
//
// The five reachable states of one flag, named after the rows of decision
// #22's resolution table.

const noRowNoDefault: DualFlagState = {
  explicit: null,
  install_default: false,
  effective: false,
  source: 'system_default',
  clamped: false,
};
const noRowDefaultOn: DualFlagState = {
  explicit: null,
  install_default: true,
  effective: true,
  source: 'install_default',
  clamped: false,
};
const explicitOn: DualFlagState = {
  explicit: true,
  install_default: false,
  effective: true,
  source: 'project',
  clamped: false,
};
const explicitOffUnderDefaultOn: DualFlagState = {
  explicit: false,
  install_default: true,
  effective: false,
  source: 'project',
  clamped: false,
};
const clampedByPrerequisite: DualFlagState = {
  explicit: null,
  install_default: true,
  effective: false,
  source: 'install_default',
  clamped: true,
};

const ALL_STATES: ReadonlyArray<[string, DualFlagState]> = [
  ['no row, no host-wide default', noRowNoDefault],
  ['no row, host-wide default on', noRowDefaultOn],
  ['explicit on', explicitOn],
  ['explicit off under a host-wide on', explicitOffUnderDefaultOn],
  ['clamped by its prerequisite', clampedByPrerequisite],
];

function flags(
  write: DualFlagState,
  log: DualFlagState,
  arctic: DualFlagState,
): DualFlagsState {
  return { write_all_slots: write, rl_log: log, arctic_secondary: arctic };
}

// ── the tri-state control ───────────────────────────────────────────────

describe('tri-state mapping', () => {
  it('maps a missing row to "inherit", not to "off"', () => {
    expect(triChoiceFor(noRowNoDefault)).toBe('inherit');
    expect(triChoiceFor(noRowDefaultOn)).toBe('inherit');
  });

  it('maps an explicit row to its own value in both directions', () => {
    expect(triChoiceFor(explicitOn)).toBe('on');
    expect(triChoiceFor(explicitOffUnderDefaultOn)).toBe('off');
  });

  it('a clamped flag still shows the segment its ROW selects', () => {
    // The clamp changes what is in force, not what the user chose. Showing
    // "off" here would silently rewrite the user's stored intent.
    expect(triChoiceFor(clampedByPrerequisite)).toBe('inherit');
  });

  it('"Use host-wide default" sends null so the backend DELETES the row', () => {
    expect(triChoiceToValue('inherit')).toBeNull();
    expect(triChoiceToValue('on')).toBe(true);
    expect(triChoiceToValue('off')).toBe(false);
  });

  it('round-trips every explicit state through choice → value', () => {
    for (const state of [explicitOn, explicitOffUnderDefaultOn]) {
      expect(triChoiceToValue(triChoiceFor(state))).toBe(state.explicit);
    }
  });

  it('labels the third option by what it does, not as a blank state', () => {
    expect(TRI_CHOICE_LABELS.inherit).toBe('Use host-wide default');
    expect(TRI_CHOICE_LABELS.on).toBe('On');
    expect(TRI_CHOICE_LABELS.off).toBe('Off');
  });
});

// ── badges ──────────────────────────────────────────────────────────────

describe('provenance badge', () => {
  it('badges an explicit row teal / "this project"', () => {
    for (const s of [explicitOn, explicitOffUnderDefaultOn]) {
      expect(badgeFor('project', s)).toEqual({
        label: 'this project',
        kind: 'user',
      });
    }
  });

  it('badges an inheriting row purple / "host default"', () => {
    for (const s of [noRowNoDefault, noRowDefaultOn, clampedByPrerequisite]) {
      expect(badgeFor('project', s)).toEqual({
        label: 'host default',
        kind: 'auto',
      });
    }
  });

  it('renders no badge on the global mount (no tier above it)', () => {
    for (const [, s] of ALL_STATES) {
      expect(badgeFor('global', s)).toBeNull();
    }
  });

  it('never uses the vocabulary reserved for the hard-override globals', () => {
    for (const [, s] of ALL_STATES) {
      const label = badgeFor('project', s)?.label ?? '';
      expect(label.toLowerCase()).not.toContain('global');
      expect(label.toLowerCase()).not.toContain('override');
    }
  });
});

// ── the "why" line ──────────────────────────────────────────────────────

describe('effective line', () => {
  it('names the cause for every one of the five states', () => {
    expect(effectiveLine('project', noRowNoDefault)).toBe(
      'Off — nothing set for this project, and no host-wide default.',
    );
    expect(effectiveLine('project', noRowDefaultOn)).toBe(
      'On — following the host-wide default.',
    );
    expect(effectiveLine('project', explicitOn)).toBe('On — set for this project.');
    expect(effectiveLine('project', explicitOffUnderDefaultOn)).toBe(
      'Off — set for this project.',
    );
  });

  it('attributes a clamped flag to its prerequisite, not to the default', () => {
    const line = effectiveLine('project', clampedByPrerequisite);
    expect(line).toContain('dual-write');
    expect(line).not.toContain('following the host-wide default');
  });

  it('never renders a bare On/Off with no cause', () => {
    for (const [name, s] of ALL_STATES) {
      const line = effectiveLine('project', s);
      expect(line.length, name).toBeGreaterThan('Off.'.length);
      expect(line, name).toContain('—');
    }
  });

  it('says on the global mount who the default applies to', () => {
    expect(effectiveLine('global', explicitOn)).toContain(
      'has not made its own choice',
    );
    expect(effectiveLine('global', noRowNoDefault)).toContain(
      'has not made its own choice',
    );
  });
});

// ── the RL-log prerequisite (F-6) ───────────────────────────────────────

describe('RL-log prerequisite note', () => {
  it('warns that picking On will also turn dual-write on', () => {
    expect(rlLogPrerequisiteNote('project', false)).toContain('turns dual-write on');
    expect(rlLogPrerequisiteNote('global', false)).toContain(
      'dual-write default on',
    );
  });

  it('is silent when the prerequisite already resolves on', () => {
    // The key case a naive implementation gets wrong: dual-write is on only
    // via the host-wide default, with NO per-project row. The log is
    // legitimately reachable and must not be nagged about.
    expect(rlLogPrerequisiteNote('project', true)).toBeNull();
    expect(rlLogPrerequisiteNote('global', true)).toBeNull();
  });
});

// ── mount guard ─────────────────────────────────────────────────────────

describe('mount configuration guard', () => {
  it('explains a project mount with no project id', () => {
    const msg = mountConfigError('project', undefined);
    expect(msg).toBeTruthy();
    expect(msg).toContain('wiring bug');
  });

  it('treats an empty-string id as missing', () => {
    expect(mountConfigError('project', '')).toBeTruthy();
  });

  it('passes a well-formed project mount and any global mount', () => {
    expect(mountConfigError('project', 'p-1')).toBeNull();
    expect(mountConfigError('global', undefined)).toBeNull();
    expect(mountConfigError('global', 'p-1')).toBeNull();
  });
});

// ── scope-derived strings ───────────────────────────────────────────────

describe('scope-derived copy', () => {
  it('titles the two mounts differently and by scope', () => {
    expect(panelTitle('project')).toBe('Dual embedding / RL logging (this project)');
    expect(panelTitle('global')).toBe(
      'Dual embedding / RL logging — host-wide defaults',
    );
  });

  it('uses "host-wide default", never the hard-override vocabulary', () => {
    const globalCopy = [
      panelTitle('global'),
      panelIntro('global'),
      ...cascadeFootnote('global'),
    ].join(' ');
    expect(globalCopy).toContain('host-wide default');
    expect(globalCopy).not.toContain('GLOBAL (all projects)');
    expect(globalCopy).not.toContain('master switch');
    expect(globalCopy).not.toContain('install-wide default');
    // The override's claim — this tier is explicitly NOT that.
    expect(globalCopy).toContain('has not made its own choice');
  });

  it('states the both-directions precedence on the per-project mount too', () => {
    const projectCopy = [panelIntro('project'), ...cascadeFootnote('project')].join(
      ' ',
    );
    expect(projectCopy).toContain('both directions');
    expect(projectCopy).toContain('survive bundle / orchestrator updates');
  });

  it('states the dual-log dependency on BOTH mounts', () => {
    for (const scope of ['project', 'global'] as const) {
      expect(cascadeFootnote(scope).join(' ')).toContain(
        'RL dual-logging requires dual-write',
      );
    }
  });

  it('says on the global mount that other projects are re-projected', () => {
    expect(cascadeFootnote('global').join(' ')).toContain('re-project');
  });
});

// ── flag metadata ───────────────────────────────────────────────────────

describe('flag metadata', () => {
  it('describes all three flags — the old copy said "Both" over three rows', () => {
    expect(DUAL_FLAGS).toHaveLength(3);
    const keys = DUAL_FLAGS.map((f) => f.key);
    expect(keys).toEqual(['write_all_slots', 'rl_log', 'arctic_secondary']);
  });

  it('names the env var each flag projects to', () => {
    expect(DUAL_FLAGS.map((f) => f.envVar)).toEqual([
      'DUAL_EMBEDDING_WRITE_ALL_SLOTS',
      'DUAL_RL_LOG_ENABLED',
      'DUAL_EMBEDDING_ARCTIC_SECONDARY',
    ]);
  });

  it('renders dual-write before the log that depends on it', () => {
    const keys = DUAL_FLAGS.map((f) => f.key);
    expect(keys.indexOf('write_all_slots')).toBeLessThan(keys.indexOf('rl_log'));
  });

  it('reads each flag out of the resolved triple by key', () => {
    const state = flags(explicitOn, clampedByPrerequisite, noRowDefaultOn);
    for (const key of DUAL_FLAGS.map((f) => f.key) as DualFlagKey[]) {
      expect(flagState(state, key)).toBe(state[key]);
    }
  });
});

// ── global save summary ─────────────────────────────────────────────────

describe('global save summary', () => {
  it('reports how many projects were re-projected', () => {
    expect(globalSaveSummary(3, 0)).toBe(
      'Host-wide default saved. Re-projected 3 projects.',
    );
    expect(globalSaveSummary(1, 0)).toBe(
      'Host-wide default saved. Re-projected 1 project.',
    );
  });

  it('surfaces soft-fail warnings rather than swallowing them', () => {
    expect(globalSaveSummary(4, 2)).toContain('2 warnings');
    expect(globalSaveSummary(4, 1)).toContain('1 warning');
  });
});

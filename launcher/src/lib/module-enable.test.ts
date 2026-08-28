// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.91 decision #23 (F-3 / F-4) — module-enable control logic.
//
// The three things a reviewer should be able to check here:
//   1. the control is ROUTED by install scope (F-3: the wrong mechanism was
//      the whole bug — a checkbox writing a table the gate never reads);
//   2. every state is REACHABLE and every state is NAMED (F-4: `null` was a
//      real, persistent state with no way back and no highlighted button);
//   3. no line ever states a value without its cause.

import { describe, expect, it } from 'vitest';
import {
  GLOBAL_TRI_CHOICE_LABELS,
  MODULE_TRI_CHOICE_LABELS,
  dormantBadgeLabel,
  dormantNotice,
  enableMechanismFor,
  globalDefaultLine,
  globalTriChoiceFor,
  globalTriChoiceToValue,
  installedTileUsesCascade,
  moduleBadgeFor,
  moduleEffectiveLine,
  moduleTriChoiceFor,
  moduleTriChoiceToValue,
  showsProjectSettingControl,
  type GlobalTriChoice,
  type ModuleEnableState,
  type ModuleTriChoice,
} from './module-enable';

function state(partial: Partial<ModuleEnableState>): ModuleEnableState {
  return {
    explicit: null,
    global_default: null,
    effective: true,
    source: 'system_default',
    ...partial,
  };
}

describe('enableMechanismFor — F-3, the routing that was missing', () => {
  it('sends global-scope modules to the per-project SETTING', () => {
    // `module_installs.enabled` is not a per-project gate for these: one
    // install row serves the whole host, and the RL gate never reads it.
    expect(enableMechanismFor('global')).toBe('project_setting');
  });

  it('keeps project-scope modules on the install row', () => {
    expect(enableMechanismFor('per_project')).toBe('install_row');
  });

  it('treats a legacy/absent scope as per_project (wire back-compat)', () => {
    // Pre-v0.2.49 launchers send `''`; the field is optional in the payload.
    expect(enableMechanismFor('')).toBe('install_row');
    expect(enableMechanismFor(undefined)).toBe('install_row');
  });
});

describe('showsProjectSettingControl — where the control must render', () => {
  it('renders for a global module whose only install row is the host-wide one', () => {
    // THE case F-3 is about: `list_installed_modules(project_id)` selects
    // `project_id = ?`, so this project has no row and the tile reads as
    // "available". Gating the control on the tile's `installed` branch would
    // hide it exactly here.
    expect(
      showsProjectSettingControl({ install_scope: 'global', kind: 'installed' }),
    ).toBe(true);
    expect(
      showsProjectSettingControl({ install_scope: 'global', kind: 'update_available' }),
    ).toBe(true);
  });

  it('does not render for a module that is not installed anywhere', () => {
    expect(
      showsProjectSettingControl({ install_scope: 'global', kind: 'available' }),
    ).toBe(false);
    expect(
      showsProjectSettingControl({ install_scope: 'global', kind: 'coming_soon' }),
    ).toBe(false);
  });

  it('does not render for a broken install (nothing to gate; Reinstall instead)', () => {
    expect(
      showsProjectSettingControl({ install_scope: 'global', kind: 'broken' }),
    ).toBe(false);
  });

  it('never renders for project-scope modules — their checkbox IS the gate', () => {
    for (const scope of ['per_project', '', undefined] as const) {
      expect(showsProjectSettingControl({ install_scope: scope, kind: 'installed' })).toBe(
        false,
      );
    }
  });
});

describe('installedTileUsesCascade — the placebo cannot come back', () => {
  it('keys on SCOPE only, so a disagreeing catalog kind cannot restore the checkbox', () => {
    // A global-scope module whose host-wide catalog kind says `broken` while
    // this project has a healthy install row would, under a kind-based rule,
    // fall back to the `module_installs.enabled` checkbox — the exact F-3
    // placebo. Scope decides the mechanism; nothing else may.
    for (const kind of ['installed', 'broken', 'available', 'update_available', undefined]) {
      expect(installedTileUsesCascade({ install_scope: 'global' })).toBe(true);
      expect(showsProjectSettingControl({ install_scope: 'global', kind })).toBe(
        kind === 'installed' || kind === 'update_available',
      );
    }
  });

  it('leaves project-scope modules on the checkbox', () => {
    for (const scope of ['per_project', '', undefined] as const) {
      expect(installedTileUsesCascade({ install_scope: scope })).toBe(false);
    }
  });
});

describe('per-project tri-state control', () => {
  it('maps each stored state to its own segment', () => {
    expect(moduleTriChoiceFor(state({ explicit: null }))).toBe('inherit');
    expect(moduleTriChoiceFor(state({ explicit: true }))).toBe('on');
    expect(moduleTriChoiceFor(state({ explicit: false }))).toBe('off');
  });

  it('round-trips: every segment produces a value that maps back to it', () => {
    const choices: ModuleTriChoice[] = ['inherit', 'on', 'off'];
    for (const choice of choices) {
      const value = moduleTriChoiceToValue(choice);
      expect(moduleTriChoiceFor(state({ explicit: value }))).toBe(choice);
    }
  });

  it('"Use host-wide default" sends null — the DELETE that is the way back', () => {
    // If this returned `false`, the control would be a one-way door dressed
    // up as three options (the F-4 shape at the per-project tier).
    expect(moduleTriChoiceToValue('inherit')).toBeNull();
    expect(moduleTriChoiceToValue('on')).toBe(true);
    expect(moduleTriChoiceToValue('off')).toBe(false);
  });

  it('labels every segment (no unnamed position)', () => {
    for (const choice of ['inherit', 'on', 'off'] as ModuleTriChoice[]) {
      expect(MODULE_TRI_CHOICE_LABELS[choice]).toBeTruthy();
    }
  });
});

describe('moduleEffectiveLine — a value is never stated without its cause', () => {
  it('distinguishes an explicit choice from an inherited one at the same value', () => {
    const chosenOff = moduleEffectiveLine(
      state({ explicit: false, effective: false, source: 'project' }),
    );
    const inheritedOff = moduleEffectiveLine(
      state({ global_default: false, effective: false, source: 'global_default' }),
    );
    expect(chosenOff).not.toBe(inheritedOff);
    expect(chosenOff).toMatch(/this project/i);
    expect(inheritedOff).toMatch(/host-wide default/i);
  });

  it('names the fail-open system default rather than implying a choice', () => {
    const line = moduleEffectiveLine(state({ effective: true, source: 'system_default' }));
    expect(line).toMatch(/nothing set/i);
  });

  it('every source × value combination produces a line naming its cause', () => {
    const cases: ModuleEnableState[] = [
      state({ explicit: true, effective: true, source: 'project' }),
      state({ explicit: false, effective: false, source: 'project' }),
      state({ global_default: true, effective: true, source: 'global_default' }),
      state({ global_default: false, effective: false, source: 'global_default' }),
      state({ effective: true, source: 'system_default' }),
    ];
    for (const s of cases) {
      const line = moduleEffectiveLine(s);
      expect(line.length).toBeGreaterThan(0);
      // A bare "On."/"Off." is exactly the shape this rejects.
      expect(line).toMatch(/—/);
    }
  });
});

describe('moduleBadgeFor — provenance hue', () => {
  it('badges an explicit choice as this project (user hue)', () => {
    expect(moduleBadgeFor(state({ explicit: false, source: 'project' }))).toEqual({
      label: 'this project',
      kind: 'user',
    });
  });

  it('badges both inheriting sources as host default (auto hue)', () => {
    for (const source of ['global_default', 'system_default'] as const) {
      expect(moduleBadgeFor(state({ source })).kind).toBe('auto');
    }
  });
});

describe('host-wide tri-state control — F-4, the dead end', () => {
  it('renders "no row" as a NAMED position, not as nothing-selected', () => {
    expect(globalTriChoiceFor(null)).toBe('default');
    expect(GLOBAL_TRI_CHOICE_LABELS.default).toBeTruthy();
  });

  it('maps the two written values to their own positions', () => {
    expect(globalTriChoiceFor(true)).toBe('on');
    expect(globalTriChoiceFor(false)).toBe('off');
  });

  it('round-trips: the null state is REACHABLE from the control', () => {
    // This is the whole of F-4: before #23 no click produced `null`, so a
    // user could enter the state (fresh install) but never return to it.
    const choices: GlobalTriChoice[] = ['default', 'on', 'off'];
    for (const choice of choices) {
      const value = globalTriChoiceToValue(choice);
      expect(globalTriChoiceFor(value)).toBe(choice);
    }
    expect(globalTriChoiceToValue('default')).toBeNull();
  });

  it('describes the null state as a real state, not as a value', () => {
    const line = globalDefaultLine(null);
    expect(line).toMatch(/no host-wide choice/i);
    expect(globalDefaultLine(true)).not.toBe(line);
    expect(globalDefaultLine(false)).not.toBe(line);
  });
});

describe('dormant modules — the USER rider', () => {
  it('states that the RL reranker is not reranking yet', () => {
    const notice = dormantNotice('vct-rl-reranker');
    expect(notice).toBeTruthy();
    expect(notice).toMatch(/not live yet|no trained model/i);
    expect(dormantBadgeLabel('vct-rl-reranker')).toBe('not reranking yet');
  });

  it('says collection is unaffected — the #25 constraint, in user-facing words', () => {
    expect(dormantNotice('vct-rl-reranker')).toMatch(/collection.*continues/i);
  });

  it('returns null for any module not in the dormant set', () => {
    expect(dormantNotice('vct-coordination')).toBeNull();
    expect(dormantBadgeLabel('vct-coordination')).toBeNull();
  });
});

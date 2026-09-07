// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Unit tests for the Artifact-tool panel's decision logic.
//
// The repo has no jsdom, so `.svelte` files are not unit-testable; these
// cover the parts of the panel that can actually be wrong: whether the box is
// checked, whether the control is inert, and which honest notice renders for
// each reachable backend state — in particular the two that a careless panel
// would get wrong (unknown rendered as a guessed default, and a scoped deny
// rule counted as a token saving).

import { describe, it, expect } from 'vitest';
import {
  CHECKBOX_LABEL,
  RESTART_NOTE,
  REWRITE_NOTE,
  SCOPE_NOTE,
  checkboxChecked,
  controlDisabled,
  isUnknown,
  notices,
  savedMessage,
  statusLine,
  type ArtifactToolState,
} from '$lib/artifact-tool';

// ── fixtures: one per reachable backend state ───────────────────────────

const base: ArtifactToolState = {
  settings_path: '/home/u/.claude/settings.json',
  file_exists: true,
  artifacts_enabled: true,
  enable_artifact_false: false,
  deny_bare_artifact: false,
  deny_scoped_artifact: false,
  fully_disabled: false,
  error: null,
  backup_path: null,
};

const enabled: ArtifactToolState = { ...base };

const noFile: ArtifactToolState = { ...base, file_exists: false };

const fullyDisabled: ArtifactToolState = {
  ...base,
  artifacts_enabled: false,
  enable_artifact_false: true,
  deny_bare_artifact: true,
  fully_disabled: true,
};

const onlyEnableFlag: ArtifactToolState = {
  ...base,
  artifacts_enabled: false,
  enable_artifact_false: true,
  deny_bare_artifact: false,
  fully_disabled: false,
};

const onlyDenyEntry: ArtifactToolState = {
  ...base,
  artifacts_enabled: false,
  enable_artifact_false: false,
  deny_bare_artifact: true,
  fully_disabled: false,
};

const unknown: ArtifactToolState = {
  ...base,
  artifacts_enabled: null,
  error: 'parse /home/u/.claude/settings.json: expected value at line 1',
};

// ── checkbox position ───────────────────────────────────────────────────

describe('checkboxChecked', () => {
  it('is checked only when artifacts are positively enabled', () => {
    expect(checkboxChecked(enabled)).toBe(true);
    expect(checkboxChecked(noFile)).toBe(true);
    expect(checkboxChecked(fullyDisabled)).toBe(false);
    expect(checkboxChecked(onlyEnableFlag)).toBe(false);
    expect(checkboxChecked(onlyDenyEntry)).toBe(false);
  });

  it('does not render an unknown state as either position being true', () => {
    // Unchecked AND disabled — the box is not allowed to be a claim.
    expect(checkboxChecked(unknown)).toBe(false);
    expect(controlDisabled(unknown, false)).toBe(true);
    expect(checkboxChecked(null)).toBe(false);
    expect(controlDisabled(null, false)).toBe(true);
  });
});

describe('controlDisabled', () => {
  it('is inert while a write is in flight', () => {
    expect(controlDisabled(enabled, true)).toBe(true);
    expect(controlDisabled(enabled, false)).toBe(false);
  });

  it('is inert whenever the state could not be determined', () => {
    expect(isUnknown(unknown)).toBe(true);
    expect(isUnknown(null)).toBe(true);
    expect(isUnknown(enabled)).toBe(false);
    expect(controlDisabled(unknown, false)).toBe(true);
  });
});

// ── status line ─────────────────────────────────────────────────────────

describe('statusLine', () => {
  it('distinguishes "no setting recorded" from "no file at all"', () => {
    expect(statusLine(enabled)).toContain('no off-switch recorded');
    expect(statusLine(noFile)).toContain('no Claude Code settings file exists');
  });

  it('says off, and says so differently when only half the pair is written', () => {
    expect(statusLine(fullyDisabled)).toContain('both');
    expect(statusLine(onlyEnableFlag)).toContain('only partly applied');
    expect(statusLine(onlyDenyEntry)).toContain('only partly applied');
  });

  it('never guesses a value for an unreadable file', () => {
    const line = statusLine(unknown);
    expect(line).toContain('Unknown');
    expect(line).not.toContain('On —');
    expect(line).not.toContain('Off —');
  });
});

// ── notices ─────────────────────────────────────────────────────────────

describe('notices', () => {
  it('is empty for a plain enabled state on an existing file', () => {
    expect(notices(enabled)).toEqual([]);
  });

  it('reports the unreadable file with its error and says nothing was written', () => {
    const n = notices(unknown);
    expect(n).toHaveLength(1);
    expect(n[0].kind).toBe('unknown');
    expect(n[0].tone).toBe('warn');
    expect(n[0].text).toContain('Nothing was written');
    expect(n[0].text).toContain(unknown.error as string);
  });

  it('suppresses every other notice when the document did not parse', () => {
    // A corrupt file's other flags are meaningless — claiming "no file yet" or
    // a scoped rule from a document that never parsed would be invented.
    const corruptWithNoise: ArtifactToolState = {
      ...unknown,
      file_exists: false,
      deny_scoped_artifact: true,
      backup_path: '/home/u/.claude/settings.json.vco-backup',
    };
    expect(notices(corruptWithNoise).map((x) => x.kind)).toEqual(['unknown']);
  });

  it('names which half of the pair is present and which is missing', () => {
    const a = notices(onlyEnableFlag).find((n) => n.kind === 'partial');
    expect(a?.text).toContain('Only enableArtifact: false is present');
    expect(a?.text).toContain('the bare "Artifact" deny rule is not');

    const b = notices(onlyDenyEntry).find((n) => n.kind === 'partial');
    expect(b?.text).toContain('Only the bare "Artifact" deny rule is present');
    expect(b?.text).toContain('enableArtifact: false is not');
  });

  it('raises no partial notice once both keys are written', () => {
    expect(notices(fullyDisabled).map((n) => n.kind)).not.toContain('partial');
  });

  it('says a scoped rule saves no tokens, and that VCO does not manage it', () => {
    const n = notices({ ...enabled, deny_scoped_artifact: true }).find(
      (x) => x.kind === 'scoped-deny',
    );
    expect(n).toBeDefined();
    expect(n?.text).toContain('saves no tokens');
    expect(n?.text).toContain('neither writes nor removes');
  });

  it('explains that turning it off creates the file when none exists', () => {
    const n = notices(noFile).find((x) => x.kind === 'no-file');
    expect(n?.text).toContain(noFile.settings_path);
  });

  it('points at the one-time backup once one exists', () => {
    const path = '/home/u/.claude/settings.json.vco-backup';
    const n = notices({ ...fullyDisabled, backup_path: path }).find(
      (x) => x.kind === 'backup',
    );
    expect(n?.text).toContain(path);
    expect(n?.text).toContain('before VCO first changed it');
  });

  it('returns nothing at all while the state is still loading', () => {
    expect(notices(null)).toEqual([]);
  });
});

// ── copy ────────────────────────────────────────────────────────────────

describe('panel copy', () => {
  it('says whose config this is and how far it reaches', () => {
    expect(SCOPE_NOTE).toContain("Claude Code's own global configuration");
    expect(SCOPE_NOTE).toContain('every project on this machine');
  });

  it('does not overstate when the change takes effect', () => {
    expect(RESTART_NOTE).toContain('may need');
    // No promise in either direction — the docs only cover one of the keys.
    expect(RESTART_NOTE).not.toMatch(/takes effect immediately/i);
    expect(RESTART_NOTE).not.toMatch(/requires a restart/i);
  });

  it('claims order preservation for new writes and admits it cannot undo old ones', () => {
    // v0.2.92: the backend enables serde_json's `preserve_order`, so a write
    // no longer alphabetises the user's keys — the note must say what the
    // code now does, not what it used to do.
    expect(REWRITE_NOTE).toContain('the order you put them in');
    expect(REWRITE_NOTE).not.toMatch(/come back in alphabetical order/i);

    // The already-damaged case is the half that is NOT fixed, so the note has
    // to carry it: a file an older VCO sorted stays sorted, and the backup is
    // the only way back. Promising a repair we cannot perform would be worse
    // than the original defect.
    expect(REWRITE_NOTE).toContain('older version of VCO');
    expect(REWRITE_NOTE).toContain('it cannot be restored');
    expect(REWRITE_NOTE).toContain('copy of the file');
  });

  it('labels the checkbox by what checked means', () => {
    expect(CHECKBOX_LABEL).toContain('available');
  });

  it('phrases the confirmation from the re-read state, not the intent', () => {
    expect(savedMessage(fullyDisabled)).toContain('disabled');
    expect(savedMessage(enabled)).toContain('re-enabled');
    expect(savedMessage(unknown)).toContain('could not be re-read');
  });
});

// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.93 (field incident 2026-09-07): the divergence modal's local parser
// required `raw.startsWith('{')`, so a conflict payload that arrived with
// leading whitespace / a label rendered NOTHING and the merge looked hung.
// These tests pin the ONE shared parser's tolerance so no site can regress
// to that shape again.

import { describe, expect, it } from 'vitest';
import {
  errorText,
  parseOrchestratorConflictError,
  parseTaggedErrorPayload,
} from './tauri-error-payload';

const CONFLICT = {
  event: 'orchestrator_update_conflict',
  operation: 'merge',
  branch: 'main',
  conflicted_files: ['CLAUDE.md', 'knowledge/concepts/foo.md'],
  git_stderr: 'CONFLICT (content): Merge conflict in CLAUDE.md',
};
const CONFLICT_JSON = JSON.stringify(CONFLICT);

describe('parseOrchestratorConflictError (field incident 2026-09-07)', () => {
  it('parses the exact serde shape', () => {
    const parsed = parseOrchestratorConflictError(CONFLICT_JSON);
    expect(parsed).not.toBeNull();
    expect(parsed?.operation).toBe('merge');
    expect(parsed?.conflicted_files).toEqual(CONFLICT.conflicted_files);
  });

  it('tolerates leading whitespace and newlines (the incident shape)', () => {
    expect(parseOrchestratorConflictError(`   ${CONFLICT_JSON}`)).not.toBeNull();
    expect(parseOrchestratorConflictError(`\n\t${CONFLICT_JSON}`)).not.toBeNull();
  });

  it('tolerates a leading "Error: " label some Tauri runtimes prepend', () => {
    const parsed = parseOrchestratorConflictError(`Error: ${CONFLICT_JSON}`);
    expect(parsed?.event).toBe('orchestrator_update_conflict');
  });

  it('parses an Error instance via its message', () => {
    const parsed = parseOrchestratorConflictError(new Error(CONFLICT_JSON));
    expect(parsed?.branch).toBe('main');
  });

  it('returns null for a non-JSON string', () => {
    expect(parseOrchestratorConflictError('Merge with strategy ort failed.')).toBeNull();
    expect(parseOrchestratorConflictError('')).toBeNull();
    expect(parseOrchestratorConflictError('   ')).toBeNull();
  });

  it('returns null when the tag is only MENTIONED in prose, never parsed', () => {
    expect(
      parseOrchestratorConflictError(
        'the backend said "event":"orchestrator_update_conflict" but this is not JSON',
      ),
    ).toBeNull();
  });

  it('returns null for JSON carrying a different event tag', () => {
    const nonFf = JSON.stringify({ event: 'orchestrator_update_non_ff', branch: 'main' });
    expect(parseOrchestratorConflictError(nonFf)).toBeNull();
  });

  it('returns null for non-string, non-Error input', () => {
    expect(parseOrchestratorConflictError(undefined)).toBeNull();
    expect(parseOrchestratorConflictError(null)).toBeNull();
    expect(parseOrchestratorConflictError(42)).toBeNull();
    expect(parseOrchestratorConflictError(CONFLICT)).toBeNull();
  });

  it('returns null for a JSON array even if it contains the tag text', () => {
    expect(parseOrchestratorConflictError(`["event","orchestrator_update_conflict"]`)).toBeNull();
  });
});

describe('parseTaggedErrorPayload (generic)', () => {
  it('detects the tag with whitespace around the colon', () => {
    const s = `{ "kind" : "install_conflict", "install_path": "/x" }`;
    const parsed = parseTaggedErrorPayload<{ kind: string; install_path: string }>(
      s,
      'kind',
      'install_conflict',
    );
    expect(parsed?.install_path).toBe('/x');
  });

  it('handles the OnboardingWizard install_conflict shape with a label prefix', () => {
    const s = `Error: {"kind":"install_conflict","install_path":"/p","source_path":"/s","mode":"full","will_overwrite":[],"will_add":[],"preserve_candidates":[]}`;
    const parsed = parseTaggedErrorPayload<{ kind: string; mode: string }>(
      s,
      'kind',
      'install_conflict',
    );
    expect(parsed?.mode).toBe('full');
  });

  it('handles the launcher self-update non_fast_forward shape', () => {
    const s = JSON.stringify({ kind: 'non_fast_forward', branch: 'main', git_stderr: 'x' });
    const parsed = parseTaggedErrorPayload<{ kind: string; branch: string }>(
      s,
      'kind',
      'non_fast_forward',
    );
    expect(parsed?.branch).toBe('main');
  });

  it('rejects a tag VALUE that only prefix-matches', () => {
    const s = JSON.stringify({ event: 'orchestrator_update_conflict_extra' });
    expect(
      parseTaggedErrorPayload(s, 'event', 'orchestrator_update_conflict'),
    ).toBeNull();
  });

  it('never throws on malformed input', () => {
    expect(() => parseTaggedErrorPayload('{"event":"x"', 'event', 'x')).not.toThrow();
    expect(parseTaggedErrorPayload('{"event":"x"', 'event', 'x')).toBeNull();
  });
});

describe('errorText', () => {
  it('unwraps an Error to its message and stringifies everything else', () => {
    expect(errorText(new Error('boom'))).toBe('boom');
    expect(errorText('plain')).toBe('plain');
    expect(errorText(7)).toBe('7');
  });
});

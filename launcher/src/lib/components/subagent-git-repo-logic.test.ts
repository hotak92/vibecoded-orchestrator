// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 (#30) — tests for the SubagentGitRepoModal connect-arm logic.
//
// URL-validator parity (M5): the case table is NOT duplicated here — both
// this suite and the Rust unit test `url_validation_parity_table`
// (launcher/src-tauri/src/commands/worktree_repo_mode.rs) iterate the ONE
// shared fixture tests/fixtures/git_remote_url_parity.json, so a
// one-sided edit cannot silently drift the Connect-button gate away from
// the authoritative backend validator. Both sides assert the fixture's
// declared case_count (silent-truncation guard).

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { describe, it, expect } from 'vitest';
import {
  isValidGitRemoteUrl,
  resolveConnectSelection,
} from './subagent-git-repo-logic';

interface UrlParityFixture {
  _format_version: number;
  case_count: number;
  cases: [string, boolean][];
}

function loadUrlParityFixture(): UrlParityFixture {
  // launcher/src/lib/components → four parents up to the repo root.
  const here = dirname(fileURLToPath(import.meta.url));
  const fixturePath = resolve(
    here,
    '../../../../tests/fixtures/git_remote_url_parity.json',
  );
  return JSON.parse(readFileSync(fixturePath, 'utf-8')) as UrlParityFixture;
}

describe('isValidGitRemoteUrl (shared TS↔Rust parity fixture)', () => {
  const fixture = loadUrlParityFixture();

  it('fixture is the expected format and not silently truncated', () => {
    expect(fixture._format_version).toBe(1);
    expect(fixture.cases.length).toBeGreaterThan(0);
    // Silent-truncation guard: the fixture declares its own row count;
    // the Rust side asserts the same. Bump case_count in the same edit
    // that adds/removes rows.
    expect(fixture.cases.length).toBe(fixture.case_count);
  });

  it('keeps the leading-dash (option-injection) rejection rows pinned (M8)', () => {
    expect(
      fixture.cases.some(([url, valid]) => url.startsWith('-') && !valid),
    ).toBe(true);
  });

  for (const [url, expected] of loadUrlParityFixture().cases) {
    it(`${expected ? 'accepts' : 'rejects'} ${JSON.stringify(url)}`, () => {
      expect(isValidGitRemoteUrl(url)).toBe(expected);
    });
  }
});

describe('resolveConnectSelection', () => {
  it('resolves a valid remote URL to the remote arm (trimmed)', () => {
    expect(
      resolveConnectSelection('  git@github.com:example/example-repo.git  ', ''),
    ).toEqual({
      ok: true,
      kind: 'remote',
      url: 'git@github.com:example/example-repo.git',
    });
  });

  it('resolves a local folder to the local arm', () => {
    expect(resolveConnectSelection('', '/home/user/projects/app')).toEqual({
      ok: true,
      kind: 'local',
      path: '/home/user/projects/app',
    });
  });

  it('LEAVE-ALONE: nothing filled → not connectable (no default arm)', () => {
    expect(resolveConnectSelection('', '   ')).toEqual({
      ok: false,
      reason: 'empty',
    });
  });

  it('LEAVE-ALONE: both filled → explicit refusal, never a silent pick', () => {
    expect(
      resolveConnectSelection('https://host.example/repo', '/some/path'),
    ).toEqual({ ok: false, reason: 'both' });
  });

  it('an invalid URL blocks the remote arm', () => {
    expect(resolveConnectSelection('not a url', '')).toEqual({
      ok: false,
      reason: 'invalid_url',
    });
  });

  it('a leading-dash candidate blocks the remote arm (M8)', () => {
    expect(resolveConnectSelection('-t@host.example:path', '')).toEqual({
      ok: false,
      reason: 'invalid_url',
    });
  });
});

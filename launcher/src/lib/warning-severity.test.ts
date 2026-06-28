// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.70 (A2-NIT1) — unit tests for `isErrorWarning`, the content-based
// severity classifier for the synchronous `update_project_v2` warning stream.
//
// MUST MATCH `project_setup.rs::classify_warning`. These tests pin the
// behavior the Rust side already exercises in
// `classify_warning_preserved_files_is_info_not_deferral` (and friends), so a
// drift between the two marker lists shows up as a failing test on one side.

import { describe, expect, it } from 'vitest';
import { isErrorWarning } from './warning-severity';

describe('isErrorWarning', () => {
  it('classifies the additive-migration auto-applied SUCCESS as info (the bug)', () => {
    // The exact shape `run_migrate_apply_additive` emits. Previously rendered
    // red because projects.ts toast.error'd everything.
    const msg =
      'additive schema migration auto-applied (2 collection(s), ' +
      'vectors preserved): Foo_KnowledgeGraph, Foo_Development';
    expect(isErrorWarning(msg)).toBe(false);
  });

  it('classifies a preserved-files notice as info', () => {
    expect(
      isErrorWarning(
        '2 user-modified file(s) preserved during update. See UPDATE_DEFERRED.md',
      ),
    ).toBe(false);
  });

  it('classifies a "subprocess failed" marker as error', () => {
    // Mirrors the Rust marker `"subprocess failed"` (NOT bare "failed").
    expect(
      isErrorWarning('install-bundle subprocess failed: nonzero exit'),
    ).toBe(true);
  });

  it('classifies an "error"-marked message as error', () => {
    expect(
      isErrorWarning(
        'additive schema migration auto-apply error (Foo/copy): boom',
      ),
    ).toBe(true);
  });

  it('does NOT treat a bare "failed:" message as error (matches Rust markers)', () => {
    // The Rust source-of-truth only flags "failed to start" / "subprocess
    // failed", not bare "failed". A message like the env-refresh soft-warning
    // ("… failed: boom") therefore classifies Info on BOTH sides. This test
    // pins the must-match contract so a future "add bare-failed" change on one
    // side is caught.
    expect(
      isErrorWarning(
        'post-bundle env refresh (apply_project_env_via_python) failed: boom',
      ),
    ).toBe(false);
  });

  it('classifies "failed to start" as error', () => {
    expect(
      isErrorWarning(
        'additive schema migration auto-apply subprocess failed to start: ENOENT',
      ),
    ).toBe(true);
  });

  it('classifies "unparseable" as error', () => {
    expect(
      isErrorWarning(
        'additive schema migration auto-apply produced unparseable output (x)',
      ),
    ).toBe(true);
  });

  it('classifies a clean bootstrap deferral as info even if "error"-adjacent text is absent', () => {
    expect(isErrorWarning('Weaviate bootstrap deferred: cold backend')).toBe(false);
  });

  it('treats a deferral marker as info even when an error marker is also present', () => {
    // Mirrors the Rust precedence `is_error && !is_deferral`.
    expect(
      isErrorWarning('collections will be created when … (transient error logged)'),
    ).toBe(false);
  });

  it('treats an unknown informational notice as info (not error)', () => {
    expect(isErrorWarning('lazily creating the development collection')).toBe(false);
  });

  it('is case-insensitive on markers', () => {
    expect(isErrorWarning('FAILED TO START the helper')).toBe(true);
  });
});

// Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
import { describe, it, expect } from 'vitest';
import { decideBundleToast, bundleTouchedSomething } from './bundle-summary-logic';
import type { UpdateSummary } from '$lib/types/launcher';

const EMPTY: UpdateSummary = {
  created: 0,
  overwritten: 0,
  always_overwritten: 0,
  adopted: 0,
  preserved: 0,
  errors_count: 0,
} as UpdateSummary;

const s = (over: Partial<UpdateSummary>): UpdateSummary => ({ ...EMPTY, ...over });

describe('bundle toast honesty', () => {
  it('an adoption-only run is NOT "already up to date"', () => {
    // THE REGRESSION. The old inline check omitted `adopted`, so a run that
    // backed up and replaced 7 files the user had edited reported that nothing
    // had happened.
    const d = decideBundleToast(s({ adopted: 7 }));
    expect(d.kind).not.toBe('up-to-date');
    expect(d.line).toContain('7');
    expect(d.line).toMatch(/your edits/i);
  });

  it('names the backup so the replacement is recoverable, not just reported', () => {
    expect(decideBundleToast(s({ adopted: 1 })).line).toMatch(/backup kept/i);
  });

  it('distinguishes adopted (replaced) from preserved (backup failed)', () => {
    // These are different outcomes for the user's file and must not read alike:
    // one replaced their bytes, the other left them in place.
    const d = decideBundleToast(s({ adopted: 3, preserved: 2 }));
    expect(d.line).toMatch(/3 of your edits replaced/i);
    expect(d.line).toMatch(/2 of your edits kept/i);
  });

  it('still reports a genuinely empty run as up to date', () => {
    const d = decideBundleToast(EMPTY);
    expect(d.kind).toBe('up-to-date');
    expect(d.line).toBe('');
  });

  it('errors make it an error toast even alongside successes', () => {
    const d = decideBundleToast(s({ created: 4, errors_count: 1 }));
    expect(d.kind).toBe('error');
    expect(d.line).toContain('4 created');
    expect(d.line).toContain('1 errors');
  });

  it('bundleTouchedSomething counts adoption as a change', () => {
    expect(bundleTouchedSomething(s({ adopted: 1 }))).toBe(true);
    expect(bundleTouchedSomething(EMPTY)).toBe(false);
  });
});

// ── v0.2.92 review m3 — the shape, not just the instance ──────────────────
//
// `adopted` went missing because "did anything happen?" was a hand-listed
// six-field disjunction: adding a bucket Rust-side did nothing here, and
// nothing made it. Fixing only `adopted` leaves the next bucket to reproduce
// the bug. These pin the DERIVATION — a new tally bucket is in by default, and
// only the buckets that genuinely mean "unchanged" are named.
describe('bundleTouchedSomething derives from the payload, not a hand list', () => {
  it('includes a tally bucket this frontend has never heard of', () => {
    // THE REGRESSION SHAPE. Stand in for the next `adopted`: a bucket the Rust
    // struct grows and nobody remembers to add to a disjunction here. The cast
    // is the point — it is exactly what a newer backend sends to an older
    // frontend, and the honest answer is "yes, something happened".
    const future = { ...EMPTY, relocated: 3 } as unknown as UpdateSummary;
    expect(bundleTouchedSomething(future)).toBe(true);
    expect(decideBundleToast(future).kind).not.toBe('up-to-date');
  });

  it('still reports the no-change buckets as up to date', () => {
    // `noop` = content already matched; `skipped_existing` = pre-existing file
    // left alone on a first install. Neither wrote a byte.
    expect(bundleTouchedSomething(s({ noop: 42 }))).toBe(false);
    expect(bundleTouchedSomething(s({ skipped_existing: 7 }))).toBe(false);
    expect(bundleTouchedSomething(s({ noop: 42, skipped_existing: 7 }))).toBe(false);
  });

  it('ignores the derived boolean flag rather than double-counting it', () => {
    // `kg_or_docs_content_changed` is true only when a change-CAUSING bucket
    // is already non-zero, so on its own it must not manufacture a change.
    const flagOnly = s({ kg_or_docs_content_changed: true });
    expect(bundleTouchedSomething(flagOnly)).toBe(false);
    expect(bundleTouchedSomething(s({ created: 1, kg_or_docs_content_changed: true }))).toBe(
      true,
    );
  });

  it('keeps counting every bucket the old hand list named', () => {
    for (const key of [
      'created',
      'overwritten',
      'always_overwritten',
      'adopted',
      'preserved',
      'errors_count',
    ] as const) {
      expect(bundleTouchedSomething(s({ [key]: 1 }))).toBe(true);
    }
  });
});

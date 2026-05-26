// SPDX-License-Identifier: AGPL-3.0-or-later
// Shared runtime config readers for VCO Supabase edge functions.
//
// Centralises env-var lookups that need shape validation + sane fallbacks
// across multiple functions. Today only `rl-artifact-url` consumes this,
// but `rl-latest-version` will need the same paid-image-repo string when
// it starts emitting full pull URLs in v0.2.37.
//
// Design notes:
//
// - The "pure validation" half (validatePaidImageRepo, validatePaidTag)
//   takes a `string | undefined` and returns a discriminated result. This
//   shape is testable without mocking Deno.env — tests just pass strings.
//
// - The "runtime read" half (resolvePaidImageRepo, resolvePaidTagDefault)
//   wraps Deno.env.get + the validator + a console.warn on malformed
//   values. Tests for THIS layer use Deno.env.set/delete in setup/teardown.
//
// - Why a WARNING (not an ERROR / throw) on malformed values: the edge
//   function should still serve traffic with the safe default rather than
//   500'ing globally because someone fat-fingered a secret. The warning
//   is loud enough to show in `supabase functions logs` for the on-call
//   to fix at leisure.
//
// - The defaults baked here MUST match the v0.2.35-shipped values, so
//   that an unset env var preserves behaviour byte-for-byte. Do NOT bump
//   these defaults when the org migration happens — set the env var
//   instead, and leave the defaults pointing at the v0.2.35 personal-
//   account values for backwards compatibility with any forks that
//   re-deploy this function without setting the secret.

/** Result of validating a paid-image-repo string. */
export type PaidImageRepoValidation =
  | { ok: true; value: string }
  | { ok: false; reason: "empty" | "no_slash" | "whitespace_only" | "invalid_chars" };

/**
 * Validate that `raw` is a well-formed `<owner>/<image>` repo address.
 *
 * Accepts:
 *   - alphanumerics, hyphens, underscores, dots in each component
 *   - lower-case canonical (GHCR is case-insensitive but we don't enforce)
 *   - exactly one `/` separating owner from image
 *
 * Rejects:
 *   - undefined / empty / whitespace-only
 *   - missing `/`
 *   - characters outside `[A-Za-z0-9._-]`
 *
 * Pure: no I/O, no Deno globals. Easy to unit-test.
 */
export function validatePaidImageRepo(
  raw: string | undefined,
): PaidImageRepoValidation {
  if (raw === undefined) {
    return { ok: false, reason: "empty" };
  }
  const trimmed = raw.trim();
  if (trimmed.length === 0) {
    // Distinguish "never set" (empty) from "set but only whitespace"
    // (whitespace_only) so the warning message can be clearer.
    return { ok: false, reason: raw.length === 0 ? "empty" : "whitespace_only" };
  }
  if (!trimmed.includes("/")) {
    return { ok: false, reason: "no_slash" };
  }
  // Owner/image components: GitHub allows letters, numbers, hyphens,
  // underscores, dots in repo and owner names. Reject anything else
  // (defensive against quote-escaping bugs or accidental `https://`
  // prefix in the env value).
  if (!/^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(trimmed)) {
    return { ok: false, reason: "invalid_chars" };
  }
  return { ok: true, value: trimmed };
}

/** Result of validating a paid-tag string. */
export type PaidTagValidation =
  | { ok: true; value: string }
  | { ok: false; reason: "empty" | "whitespace_only" | "invalid_chars" };

/**
 * Validate that `raw` is a well-formed Docker image tag (semver, sha256,
 * or release-channel name). The Docker spec allows up to 128 chars of
 * `[A-Za-z0-9_.-]` with the first char not being `.` or `-`.
 */
export function validatePaidTag(
  raw: string | undefined,
): PaidTagValidation {
  if (raw === undefined) {
    return { ok: false, reason: "empty" };
  }
  const trimmed = raw.trim();
  if (trimmed.length === 0) {
    return { ok: false, reason: raw.length === 0 ? "empty" : "whitespace_only" };
  }
  if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(trimmed)) {
    return { ok: false, reason: "invalid_chars" };
  }
  return { ok: true, value: trimmed };
}

// ─── Defaults (kept in sync with v0.2.35-shipped values) ────────────────────
//
// Why these are exported: the launcher-side Rust tests sometimes need to
// know what the server would return on an unset env var, and importers
// from sibling functions (rl-latest-version planned v0.2.37) need the
// same string. Exporting beats re-typing it in three places.

export const PAID_IMAGE_REPO_DEFAULT = "hotak92/vct-rl-reranker";
export const PAID_TAG_DEFAULT_FALLBACK = "0.1.0";

/**
 * Resolve the paid-image repo from `GHCR_PAID_IMAGE_REPO`, falling back
 * to the v0.2.35 default if unset or malformed. Logs a WARNING (visible
 * in `supabase functions logs`) when a set-but-malformed value is rejected
 * so the on-call can spot the fat-finger.
 */
export function resolvePaidImageRepo(): string {
  const raw = Deno.env.get("GHCR_PAID_IMAGE_REPO");
  const result = validatePaidImageRepo(raw);
  if (result.ok) {
    return result.value;
  }
  // Only warn if the env var was actually set — being unset is the normal
  // case (default applies silently). result.reason === "empty" with raw
  // === undefined means unset.
  if (raw !== undefined) {
    console.warn(
      `[config] GHCR_PAID_IMAGE_REPO malformed (reason=${result.reason}, ` +
        `raw=${JSON.stringify(raw).slice(0, 80)}); falling back to default ` +
        `'${PAID_IMAGE_REPO_DEFAULT}'`,
    );
  }
  return PAID_IMAGE_REPO_DEFAULT;
}

/**
 * Resolve the paid-tag default from `GHCR_PAID_TAG_DEFAULT`, falling back
 * to `0.1.0` if unset or malformed.
 *
 * Note: this only seeds the `tag` field in the rl-artifact-url response.
 * The launcher-side `resolve_variant_tag` is the real source of truth
 * for tag selection (variant-map matching against the user's tier and
 * platform). This env var is effectively documentation + a defensive
 * default for callers that bypass the launcher logic.
 */
export function resolvePaidTagDefault(): string {
  const raw = Deno.env.get("GHCR_PAID_TAG_DEFAULT");
  const result = validatePaidTag(raw);
  if (result.ok) {
    return result.value;
  }
  if (raw !== undefined) {
    console.warn(
      `[config] GHCR_PAID_TAG_DEFAULT malformed (reason=${result.reason}, ` +
        `raw=${JSON.stringify(raw).slice(0, 80)}); falling back to default ` +
        `'${PAID_TAG_DEFAULT_FALLBACK}'`,
    );
  }
  return PAID_TAG_DEFAULT_FALLBACK;
}

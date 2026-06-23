// Input validation for the module-catalog edge function.
//
// Kept in its own module (separate from index.ts, which runs Deno.serve on
// import) so the guard is unit-testable without a live Supabase project —
// same convention as the other edge functions' validation.ts modules.

// Allowed shape for the `id` query param (audit RLS-2). Module ids are
// lowercase-kebab slugs (e.g. "vct-rl-reranker"). The catalog endpoint
// concatenates `id` into the storage object key `${id}.json`; even though the
// bucket is pinned and anonymous-public (bounded blast radius), an unvalidated
// id lets a caller probe arbitrary object keys (path traversal via "/" or
// "..", extension confusion). The regex rejects "/", ".", whitespace, upper
// case, and anything else outside [a-z0-9-].
export const MODULE_ID_RE = /^[a-z0-9-]+$/;

/** True iff `id` is a well-formed module slug safe to use in a storage key. */
export function isValidModuleId(id: string): boolean {
  return MODULE_ID_RE.test(id);
}

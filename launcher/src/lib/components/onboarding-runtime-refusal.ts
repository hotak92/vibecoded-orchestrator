// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// R7b F8: the storage commands REFUSE to act on container volumes that exist
// only under the other runtime (`container_runtime::wrong_runtime_owner_refusal`
// in vct-launcher-core). In onboarding that refusal used to land in the generic
// install-error line — or behind "Couldn't probe volumes (...)" — and the
// wizard simply stopped. This recognises it so the volumes step can show it as
// what it is: a blocking condition with the steps to clear it.

/** The refusal text when `error` is a runtime-ownership refusal, else `null`. */
export function runtimeOwnerRefusal(error: unknown): string | null {
  const text = typeof error === 'string' ? error : error instanceof Error ? error.message : String(error ?? '');
  const trimmed = text.replace(/^Error:\s*/, '').trim();
  // Shape pinned by the Rust test `ownership_guard_refuses_only_when_the_other_runtime_owns_it`:
  // "refusing to <action> <object>: it exists only under <owner>, but …".
  if (/^refusing to .+: it exists only under (podman|docker), but /s.test(trimmed)) {
    return trimmed;
  }
  return null;
}

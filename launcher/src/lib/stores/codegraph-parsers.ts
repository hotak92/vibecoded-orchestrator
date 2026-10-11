// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.102 W6-L — the store behind `CodegraphParsersModal.svelte`. A thin
// shell over `$lib/codegraph-parsers` (where the decisions live, pure and
// tested); this file only binds it to Svelte and the Tauri bridge.
//
// Asked at project startup: when the launcher's registered project list
// loads or changes (every project), and when a project is opened (that
// project) — see `routes/+layout.svelte`. Each ask re-detects the languages
// (cheap: Python caches them by a directory-mtime signature), so a blank
// project that later gains PowerShell is offered its parser on a later start.

import { get, writable } from 'svelte/store';
import { invoke, tauriAvailable } from '$lib/tauri';
import {
  createParsersController,
  INITIAL_PARSERS_STATE,
  type ParsersState,
} from '$lib/codegraph-parsers';

function createCodegraphParsersStore() {
  const state = writable<ParsersState>(INITIAL_PARSERS_STATE);
  const controller = createParsersController({
    invoke,
    set: state.set,
    get: () => get(state),
  });
  return {
    subscribe: state.subscribe,
    /** No-op in browser mode: there is no venv to install into. */
    async check(paths: string[]): Promise<void> {
      if (!tauriAvailable()) return;
      await controller.check(paths);
    },
    toggle: controller.toggle,
    install: controller.install,
    notNow: controller.notNow,
    close: controller.close,
  };
}

export const codegraphParsers = createCodegraphParsersStore();

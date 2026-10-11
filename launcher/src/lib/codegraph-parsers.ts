// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.102 W6-L — code-graph parsers on demand.
//
// Two tree-sitter parsers (PowerShell, Protocol Buffers) are OPTIONAL: VCO
// does not install them by default. It detects which languages each
// registered project contains and, when a parser the code graph needs is
// missing, the launcher asks in a modal first — one checkbox per parser (all
// checked), a short explanation, Install / Not now. Missing default grammars
// (an opted-out or failed install) are offered the same way.
//
// Detection, the "Not now" memory and the installer all live in
// `python -m vco_lib.codegraph_parsers` (via the Rust commands
// `codegraph_parser_offers` / `codegraph_parsers_decline` /
// `codegraph_parsers_install`). This file decides only what the GUI does with
// the answers, and is pure so vitest pins every branch without a DOM.
//
// "Not now" rule (stated once, enforced in Python): a declined parser is not
// offered again until its language appears in a project that did not contain
// it when you declined (a newly registered project counts). Parsers left
// UNCHECKED when you press Install are declined the same way.

/** Mirrors `commands::codegraph_parsers::ParserOffer`. */
export interface ParserOffer {
  id: string;
  label: string;
  package: string;
  optional: boolean;
  languages: string[];
  projects: string[];
}

/** Mirrors `commands::codegraph_parsers::ParserOfferReport`. */
export interface ParserOfferReport {
  offers: ParserOffer[];
  broken: unknown[];
  errors: string[];
}

export interface ParserInstallFailure {
  id: string;
  error: string;
}

/** Mirrors `commands::codegraph_parsers::ParserInstallReport`. */
export interface ParserInstallReport {
  ok: boolean;
  message: string;
  installed: string[];
  failed: ParserInstallFailure[];
  log_path: string;
  log_tail: string;
}

export const MODAL_TITLE = 'Install code parsers?';

/** Code-graph language id → the name a user knows. */
const LANGUAGE_NAMES: Record<string, string> = {
  powershell: 'PowerShell',
  proto: 'Protocol Buffers',
  rust: 'Rust',
  javascript: 'JavaScript',
  typescript: 'TypeScript',
  svelte: 'Svelte',
  go: 'Go',
  java: 'Java',
  csharp: 'C#',
  cpp: 'C/C++',
  ruby: 'Ruby',
  lua: 'Lua',
  shell: 'shell',
  python: 'Python',
};

export function languageName(id: string): string {
  return LANGUAGE_NAMES[id] ?? id;
}

/** "A", "A and B", "A, B and C". */
export function joinNames(names: string[]): string {
  if (names.length <= 1) return names[0] ?? '';
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/** The plain-language explanation shown above the checkboxes. */
export function explanation(offers: ParserOffer[]): string {
  const langs = [...new Set(offers.flatMap((o) => o.languages))].map(languageName);
  const what = langs.length ? joinNames(langs) : 'this';
  return (
    `Parsers let VCO read your ${what} code structurally, so code search finds ` +
    'the right functions and classes. They are installed once for this machine, ' +
    'not per project.'
  );
}

/** The basename of a project folder, for "used by" lines. */
export function projectName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

export type Invoker = <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;

export interface ParsersState {
  offers: ParserOffer[];
  /** Checked parser ids, in offer order. All checked when the modal opens. */
  selected: string[];
  /** The project folders the offer was computed for (sent back on decline). */
  projects: string[];
  open: boolean;
  installing: boolean;
  result: ParserInstallReport | null;
  error: string | null;
}

export const INITIAL_PARSERS_STATE: ParsersState = {
  offers: [],
  selected: [],
  projects: [],
  open: false,
  installing: false,
  result: null,
  error: null,
};

/** Only local, non-empty, distinct folders are scanned. */
export function projectPaths(paths: Iterable<string | null | undefined>): string[] {
  const out: string[] = [];
  for (const p of paths) {
    const t = (p ?? '').trim();
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

/**
 * The actions, with I/O injected. The Svelte store is a thin shell around
 * this; the tests drive it with a fake invoker and assert exactly which
 * backend commands each action reaches — and which it does NOT.
 */
export function createParsersController(deps: {
  invoke: Invoker;
  set: (state: ParsersState) => void;
  get: () => ParsersState;
}) {
  const { invoke, set, get } = deps;
  let pending: Promise<void> | null = null;
  let queued: string[] | null = null;

  function busy(state: ParsersState): boolean {
    return state.open || state.installing;
  }

  async function runCheck(paths: string[]): Promise<void> {
    if (!paths.length || busy(get())) return;
    let report: ParserOfferReport | null = null;
    try {
      report = await invoke<ParserOfferReport>('codegraph_parser_offers', { projectPaths: paths });
    } catch (e) {
      // A failed CHECK shows no modal (nothing is installed, nothing lost);
      // it is logged so a broken detection is findable.
      console.warn('[codegraph-parsers] offer check failed:', e);
      return;
    }
    for (const err of report?.errors ?? []) console.warn('[codegraph-parsers]', err);
    // Re-read AFTER the await: never replace a modal already up.
    if (busy(get())) return;
    const offers = report?.offers ?? [];
    if (!offers.length) return;
    set({
      ...INITIAL_PARSERS_STATE,
      offers,
      selected: offers.map((o) => o.id),
      projects: paths,
      open: true,
    });
  }

  /**
   * Ask which parsers to offer for these project folders. Never installs.
   * Concurrent asks coalesce: the latest paths run once after the current one.
   */
  function check(paths: string[]): Promise<void> {
    const clean = projectPaths(paths);
    if (pending) {
      queued = clean;
      return pending;
    }
    pending = (async () => {
      let next: string[] | null = clean;
      while (next) {
        queued = null;
        await runCheck(next);
        next = queued;
      }
    })().finally(() => {
      pending = null;
    });
    return pending;
  }

  /** Flip one checkbox. Ignored while installing or after a result. */
  function toggle(id: string): void {
    const s = get();
    if (!s.open || s.installing || s.result) return;
    if (!s.offers.some((o) => o.id === id)) return;
    const on = new Set(s.selected);
    if (on.has(id)) on.delete(id);
    else on.add(id);
    set({ ...s, selected: s.offers.map((o) => o.id).filter((x) => on.has(x)) });
  }

  async function decline(ids: string[], paths: string[]): Promise<void> {
    if (!ids.length) return;
    try {
      await invoke('codegraph_parsers_decline', { parsers: ids, projectPaths: paths });
    } catch (e) {
      console.warn('[codegraph-parsers] could not record "Not now":', e);
    }
  }

  /** Install: EXACTLY the checked parsers. Unchecked ones are declined. */
  async function install(): Promise<void> {
    const s = get();
    if (!s.open || s.installing || s.result || !s.selected.length) return;
    const chosen = [...s.selected];
    const unchecked = s.offers.map((o) => o.id).filter((id) => !chosen.includes(id));
    set({ ...s, installing: true, error: null });
    try {
      const result = await invoke<ParserInstallReport>('codegraph_parsers_install', {
        parsers: chosen,
      });
      set({ ...get(), installing: false, result });
    } catch (e) {
      // Loud: the failure stays on screen until the user closes it.
      set({ ...get(), installing: false, error: String(e) });
    }
    await decline(unchecked, s.projects);
  }

  /** Not now: nothing is installed; every offered parser is declined. */
  async function notNow(): Promise<void> {
    const s = get();
    if (!s.open || s.installing) return;
    set({ ...INITIAL_PARSERS_STATE });
    if (!s.result && !s.error) await decline(s.offers.map((o) => o.id), s.projects);
  }

  /** Close after a result or an error (nothing further is recorded). */
  function close(): void {
    if (get().installing) return;
    set({ ...INITIAL_PARSERS_STATE });
  }

  return { check, toggle, install, notNow, close };
}

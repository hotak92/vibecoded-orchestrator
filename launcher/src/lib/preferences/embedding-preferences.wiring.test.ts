// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Wiring guard for the v0.2.102 W6-B Preferences fixes (from the W6-R
// audit, /V02102-PREFERENCES-AUDIT-2026-10.md):
//
//   F1 — the Code Graph Embeddings section is no longer a placebo: the
//        three app_state keys are now READ by the indexer's resolver
//        (backend half pinned by tests/test_v02102_code_embed_preferences_
//        readers.py + the consumers test tests/test_v02102_pref_keys_have_
//        consumers.py). THIS file pins the frontend half: the controls
//        still write the keys through app_state_set, and the section's
//        copy states the TRUE effect timing (next indexing run; env wins).
//   F3 — saveDefaultTextEmbedding re-reads the "Embedding profile
//        (global)" select after the save that rewrites
//        embedding.active_profile (one-way coupling made honest).
//   F4 — the "Detected backend" readout binds the load-time snapshot
//        (detectedCodeModel), never the mutable Dropdown state.
//   F5 — both default-embedding Dropdowns carry a busy state (disabled +
//        saving placeholder) across the machine-wide env re-projection.
//   F6 — the updates page's five independent mount reads run in ONE
//        Promise.all (the remote tag lookup no longer serialises them).
//
// Same established AST pattern as q4-gui-affordances.wiring.test.ts /
// projects-page-affordances.wiring.test.ts: a grep is satisfied by
// comments, so every assertion walks the parsed Svelte AST instead.
// WHAT IT STILL DOES NOT PROVE: real-DOM click delivery and the Rust/Python
// effects (pinned by the two Python suites named above and the Rust
// embedding_catalog / project_env_settings tests).

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { isIdent, walk } from '../test-support/wiring-ast';

const here = dirname(fileURLToPath(import.meta.url));

type Node = Record<string, unknown>;

function parseSvelte(rel: string): { instance: Node; fragment: Node } {
  const src = readFileSync(resolve(here, rel), 'utf8');
  const page = parse(src, { modern: true }) as unknown as Node;
  return {
    instance: (page.instance as Node).content as Node,
    fragment: page.fragment as Node,
  };
}

const PREFS = parseSvelte('../../routes/preferences/+page.svelte');
const UPDATES = parseSvelte('../../routes/preferences/updates/+page.svelte');

// ─── AST helpers (same idioms as q4-gui-affordances.wiring.test.ts) ────────

function findFunction(root: Node, name: string): Node | null {
  for (const n of walk(root)) {
    if (n.type === 'FunctionDeclaration' && isIdent(n.id, name)) return n;
  }
  return null;
}

function identifiers(root: unknown): Set<string> {
  return new Set(
    [...walk(root)]
      .filter((n) => n.type === 'Identifier')
      .map((n) => n.name as string),
  );
}

/** `fn(...)` calls anywhere under `root` (identifier callee). */
function callsNamed(root: unknown, name: string): Node[] {
  return [...walk(root)].filter(
    (n) => n.type === 'CallExpression' && isIdent(n.callee, name),
  );
}

/** First argument of `loaders.refresh('activeEmbedding')`-style member
 * calls: `memberCallsWithArg(fn, 'loaders', 'refresh')` → arg nodes. */
function memberCallArgs(root: unknown, obj: string, prop: string): Node[] {
  const out: Node[] = [];
  for (const n of walk(root)) {
    if (n.type !== 'CallExpression') continue;
    const callee = n.callee as Node | undefined;
    if (callee?.type !== 'MemberExpression') continue;
    if (!isIdent(callee.object, obj) || !isIdent(callee.property, prop)) continue;
    out.push((n.arguments as Node[])[0]);
  }
  return out;
}

function startOf(n: Node): number {
  return typeof n.start === 'number' ? n.start : -1;
}

function elements(fragment: Node, tag: string): Node[] {
  return [...walk(fragment)].filter(
    (n) => n.type === 'RegularElement' && n.name === tag,
  );
}

function components(fragment: Node, name: string): Node[] {
  return [...walk(fragment)].filter(
    (n) => (n.type === 'Component' || n.type === 'InlineComponent') && n.name === name,
  );
}

function attr(el: Node, name: string): Node | undefined {
  return (el.attributes as Node[] | undefined)?.find((a) => a.name === name);
}

function attrText(el: Node, name: string): string | null {
  const a = attr(el, name);
  if (!a) return null;
  const v = a.value;
  if (Array.isArray(v)) {
    return v
      .filter((x: Node) => x.type === 'Text')
      .map((x: Node) => (x.raw ?? x.data ?? '') as string)
      .join('');
  }
  return null;
}

// ─── F1 — the Code Graph Embeddings controls + the true promise ────────────

describe('W6-B F1 — Code Graph Embeddings writes + effect-timing copy', () => {
  const HANDLERS: Array<[string, string, string]> = [
    ['saveCodeEmbedOverride', 'APP_STATE_KEY_CODE_EMBED_OVERRIDE', 'code_embed_backend_override'],
    ['saveCodeEmbedOpenaiModel', 'APP_STATE_KEY_CODE_EMBED_OPENAI_MODEL', 'code_embed_openai_model'],
    ['saveCodeEmbedOllamaModel', 'APP_STATE_KEY_CODE_EMBED_OLLAMA_MODEL', 'code_embed_ollama_model'],
  ];

  it.each(HANDLERS)(
    '%s still writes its key through app_state_set (the keys the reader consumes)',
    (handler, constName, _key) => {
      const fn = findFunction(PREFS.instance, handler);
      expect(fn, `no ${handler}() handler at all`).toBeTruthy();
      const invokes = callsNamed(fn, 'invoke');
      expect(invokes.length, `${handler} must invoke app_state_set`).toBeGreaterThan(0);
      const argNames = identifiers((invokes[0].arguments as Node[])[1] ?? {});
      expect(
        argNames.has(constName),
        `${handler} must pass ${constName} as the app_state key`,
      ).toBe(true);
    },
  );

  it('the section copy states the effect timing (next indexing run) and the env precedence', () => {
    const src = readFileSync(
      resolve(here, '../../routes/preferences/+page.svelte'),
      'utf8',
    );
    // The reader lands on the NEXT indexer construction; the copy must not
    // promise an instant switch, and must admit CODE_EMBED_* env wins.
    expect(
      src.includes('next code-graph indexing run'),
      'the section must say the pin takes effect on the next indexing run',
    ).toBe(true);
    expect(
      /CODE_EMBED_MODEL\s*\/\s*CODE_EMBED_BACKEND env override still wins/.test(src),
      'the section must say an explicit CODE_EMBED_* env override still wins',
    ).toBe(true);
  });
});

// ─── F3 — the text-default save refreshes the global-profile select ────────

describe('W6-B F3 — text-default save re-reads the global embedding profile', () => {
  it("saveDefaultTextEmbedding calls loaders.refresh('activeEmbedding') AFTER the save resolves", () => {
    const fn = findFunction(PREFS.instance, 'saveDefaultTextEmbedding');
    expect(fn, 'no saveDefaultTextEmbedding() handler at all').toBeTruthy();
    const refreshArgs = memberCallArgs(fn, 'loaders', 'refresh');
    expect(
      refreshArgs.length,
      'the handler must call loaders.refresh(...) after the save — the backend ' +
        'stamps embedding.active_profile (v0.2.68 Defect D) and the select ' +
        'would otherwise keep showing the stale profile',
    ).toBeGreaterThan(0);
    expect(
      refreshArgs.some((a) => a?.type === 'Literal' && a.value === 'activeEmbedding'),
      "the refresh must target the 'activeEmbedding' loader",
    ).toBe(true);
    // Ordering: the refresh must come AFTER the set_default_embedding_models
    // invoke (refreshing before the write would re-read the OLD row).
    const saveInvoke = callsNamed(fn, 'invoke')[0];
    const refreshCall = [...walk(fn)].find(
      (n) =>
        n.type === 'CallExpression' &&
        (n.callee as Node)?.type === 'MemberExpression' &&
        isIdent((n.callee as Node).object, 'loaders') &&
        isIdent((n.callee as Node).property, 'refresh'),
    );
    expect(saveInvoke, 'the handler must invoke set_default_embedding_models').toBeTruthy();
    expect(refreshCall, 'the handler must call loaders.refresh').toBeTruthy();
    expect(
      startOf(refreshCall! as Node) > startOf(saveInvoke! as Node),
      'loaders.refresh must run after the save invoke resolves',
    ).toBe(true);
  });
});

// ─── F4 — the "Detected backend" readout binds the load-time snapshot ──────

describe('W6-B F4 — Detected backend readout is not the mutable Dropdown state', () => {
  it('the <code> readout references detectedCodeModel and never defaultCodeModel', () => {
    const codeEls = elements(PREFS.fragment, 'code');
    expect(codeEls.length, 'the page renders no <code> element at all').toBeGreaterThan(0);
    const withDetected = codeEls.filter((el) => identifiers(el).has('detectedCodeModel'));
    expect(
      withDetected.length,
      'no <code> element renders detectedCodeModel — the readout is gone or rebound',
    ).toBeGreaterThan(0);
    const withDefault = codeEls.filter((el) => identifiers(el).has('defaultCodeModel'));
    expect(
      withDefault.length,
      'a <code> element still renders defaultCodeModel — the readout would ' +
        'follow the user’s in-progress Dropdown pick (F4 regression)',
    ).toBe(0);
  });

  it('loadEmbeddingCatalog captures the snapshot the readout renders', () => {
    const fn = findFunction(PREFS.instance, 'loadEmbeddingCatalog');
    expect(fn).toBeTruthy();
    const src = readFileSync(
      resolve(here, '../../routes/preferences/+page.svelte'),
      'utf8',
    );
    expect(
      src.includes('detectedCodeModel = defaultCodeModel'),
      'the loader must capture detectedCodeModel once, at load',
    ).toBe(true);
  });
});

// ─── F5 — busy states on both default-embedding Dropdowns ──────────────────

describe('W6-B F5 — default-embedding Dropdowns disable while saving', () => {
  const ROWS: Array<[string, string]> = [
    ['Default text embedding', 'defaultTextSaving'],
    ['Default code embedding', 'defaultCodeSaving'],
  ];

  it.each(ROWS)('%s Dropdown is disabled by its saving state', (label, saving) => {
    const dd = components(PREFS.fragment, 'Dropdown').find(
      (c) => attrText(c, 'ariaLabel') === label,
    );
    expect(dd, `no Dropdown with ariaLabel "${label}"`).toBeTruthy();
    const disabled = attr(dd!, 'disabled');
    expect(
      disabled && identifiers(disabled).has(saving),
      `the ${label} Dropdown must be disabled on ${saving} (the text path is a machine-wide env sweep)`,
    ).toBe(true);
    const placeholder = attr(dd!, 'placeholder');
    expect(
      placeholder && identifiers(placeholder).has(saving),
      `the ${label} Dropdown's placeholder must reflect the saving state`,
    ).toBe(true);
  });

  it.each([
    ['saveDefaultTextEmbedding', 'defaultTextSaving'],
    ['saveDefaultCodeEmbedding', 'defaultCodeSaving'],
  ])('%s arms/disarms %s around the await', (handler, saving) => {
    const fn = findFunction(PREFS.instance, handler);
    expect(fn, `no ${handler}() handler at all`).toBeTruthy();
    const ids = identifiers(fn);
    expect(ids.has(saving), `${handler} must reference ${saving}`).toBe(true);
    // Disarm in a finally: the assignment must appear inside a
    // TryStatement's finalizer so a failed save cannot leave the Dropdown
    // disabled forever.
    const inFinally = [...walk(fn)].some(
      (n) =>
        n.type === 'TryStatement' &&
        n.finalizer != null &&
        [...walk(n.finalizer)].some(
          (m) =>
            m.type === 'AssignmentExpression' &&
            m.operator === '=' &&
            identifiers(m.left).has(saving),
        ),
    );
    expect(
      inFinally,
      `${handler} must reset ${saving} in a finally block`,
    ).toBe(true);
  });
});

// ─── F6 — updates page: five independent mount reads in ONE Promise.all ────

describe('W6-B F6 — updates mount reads run in parallel', () => {
  const FIVE = [
    'get_user_owned_paths',
    'get_auto_check_enabled',
    'get_auto_retry_failed_installs_setting',
    'get_launcher_running_version',
    'get_latest_source_release_tag',
  ];

  it('loadSettings issues the five independent reads inside one Promise.all', () => {
    const fn = findFunction(UPDATES.instance, 'loadSettings');
    expect(fn, 'no loadSettings() handler at all').toBeTruthy();
    const promiseAll = [...walk(fn)].find(
      (n) =>
        n.type === 'CallExpression' &&
        (n.callee as Node)?.type === 'MemberExpression' &&
        isIdent((n.callee as Node).object, 'Promise') &&
        isIdent((n.callee as Node).property, 'all'),
    );
    expect(
      promiseAll,
      'loadSettings must run its independent reads through Promise.all — ' +
        'sequential awaits let the remote tag lookup serialise the mount',
    ).toBeTruthy();
    const literals = [...walk(promiseAll!)]
      .filter((n) => n.type === 'CallExpression' && isIdent(n.callee, 'invoke'))
      .map((n) => {
        const a = (n.arguments as Node[])[0];
        return a?.type === 'Literal' && typeof a.value === 'string'
          ? a.value
          : `NON-LITERAL@${JSON.stringify(a ?? {})}`;
      });
    for (const cmd of FIVE) {
      expect(
        literals.includes(cmd),
        `${cmd} must be inside the Promise.all`,
      ).toBe(true);
    }
  });

  it('the lag check stays SEQUENTIAL — it needs both version and tag first', () => {
    const fn = findFunction(UPDATES.instance, 'loadSettings');
    const lagInvoke = callsNamed(fn, 'invoke').find((n) => {
      const a = (n.arguments as Node[])[0];
      return a?.type === 'Literal' && a.value === 'check_running_version_lags_tag';
    });
    expect(lagInvoke, 'the lag check invoke is gone').toBeTruthy();
    const promiseAll = [...walk(fn)].find(
      (n) =>
        n.type === 'CallExpression' &&
        (n.callee as Node)?.type === 'MemberExpression' &&
        isIdent((n.callee as Node).object, 'Promise') &&
        isIdent((n.callee as Node).property, 'all'),
    );
    expect(promiseAll).toBeTruthy();
    expect(
      literalsUnder(promiseAll!).includes('check_running_version_lags_tag'),
      'the lag check DEPENDS on the version+tag reads — it must run after the Promise.all, not inside it',
    ).toBe(false);
  });
});

function literalsUnder(root: Node): string[] {
  return [...walk(root)]
    .filter((n) => n.type === 'Literal' && typeof n.value === 'string')
    .map((n) => n.value as string);
}

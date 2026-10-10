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

  it('the section copy states the TRUE effect (W6-B2 round 2): applied automatically, additively', () => {
    const src = readFileSync(
      resolve(here, '../../routes/preferences/+page.svelte'),
      'utf8',
    );
    // Red on 2cc19a9c: the copy promised "next indexing run" and that
    // CODE_EMBED_BACKEND env wins — neither true under the one resolver.
    expect(src).not.toContain('next code-graph indexing run');
    expect(src).not.toMatch(/CODE_EMBED_BACKEND env override still wins/);
    expect(src).toContain('A change is applied automatically');
    expect(src).toContain('nothing is');
    expect(src).toContain('until the new one is complete');
    expect(src).not.toContain('--apply-code-model-change');
  });
});

// ─── W6-B2 — per-project recorded model, target, fill progress, cost ───────

describe('W6-B2 — the section shows each graph\'s recorded model, target and fill progress', () => {
  it('the codeEmbedStatus loader invokes get_code_embed_model_status (lazy + slow)', async () => {
    const { PREF_LOADERS } = await import('./loaders');
    expect(PREF_LOADERS.codeEmbedStatus.eager).toBe(false);
    expect(PREF_LOADERS.codeEmbedStatus.slow).toBe(true);
    const loadersSrc = readFileSync(resolve(here, './loaders.ts'), 'utf8');
    expect(loadersSrc).toMatch(/codeEmbedStatus:[\s\S]*?invoke<S>\('get_code_embed_model_status'\)/);
  });

  it('loadCodeEmbedStatus goes through the registry and keeps the error for the section', () => {
    const fn = findFunction(PREFS.instance, 'loadCodeEmbedStatus');
    expect(fn, 'no loadCodeEmbedStatus()').toBeTruthy();
    const loads = [...walk(fn)].filter(
      (n) => n.type === 'MemberExpression' && isIdent(n.property, 'codeEmbedStatus'),
    );
    expect(loads.length, 'must read PREF_LOADERS.codeEmbedStatus').toBeGreaterThan(0);
    expect(identifiers(fn).has('codeEmbedStatusError')).toBe(true);
  });

  it.each(['saveCodeEmbedOverride', 'saveCodeEmbedOpenaiModel', 'saveCodeEmbedOllamaModel', 'saveOpenAiEmbeddingsConsent'])(
    "%s refreshes the per-project status AFTER the save resolves",
    (handler) => {
      const fn = findFunction(PREFS.instance, handler) as Node;
      const refreshes = memberCallArgs(fn, 'loaders', 'refresh').filter(
        (a) => a && (a.value as string) === 'codeEmbedStatus',
      );
      expect(refreshes.length, `${handler} must call loaders.refresh('codeEmbedStatus')`).toBe(1);
      const invoke = callsNamed(fn, 'invoke')[0];
      expect(startOf(refreshes[0])).toBeGreaterThan(startOf(invoke));
    },
  );

  it('the template renders recorded model, target, fill progress and cost per project', () => {
    const block = elements(PREFS.fragment, 'div').find(
      (d) => attrText(d, 'data-testid') === 'code-embed-status',
    );
    expect(block, 'the status block').toBeTruthy();
    const ids = identifiers(block);
    for (const used of ['codeEmbedStatus', 'codeEmbedFillText', 'codeEmbedStatusError']) {
      expect(ids.has(used), `status block uses ${used}`).toBe(true);
    }
    const props = new Set(
      [...walk(block)]
        .filter((n) => n.type === 'MemberExpression' && (n.property as Node)?.type === 'Identifier')
        .map((n) => (n.property as Node).name as string),
    );
    for (const field of ['recorded_model', 'target_model', 'cost_bearing', 'target_refused', 'record_refused']) {
      expect(props.has(field), `status block reads p.${field}`).toBe(true);
    }
  });

  it('the fill text states n of N, complete, not-started and refused truthfully', () => {
    const fn = findFunction(PREFS.instance, 'codeEmbedFillText') as Node;
    expect(fn).toBeTruthy();
    const src = readFileSync(resolve(here, '../../routes/preferences/+page.svelte'), 'utf8')
      .slice(startOf(fn), (fn.end as number));
    for (const needle of ["'complete'", 'filled} of ${p.total}', 'next code-graph run', 'not allowed']) {
      expect(src, needle).toContain(needle);
    }
  });
});

// ─── W6-B2 — two OpenAI toggles: summaries vs embeddings ───────────────────

describe('W6-B2 — OpenAI summaries and embeddings are separate toggles', () => {
  const src = readFileSync(resolve(here, '../../routes/preferences/+page.svelte'), 'utf8');

  it('the summaries toggle says summaries only; the embeddings toggle says KG + code graph', () => {
    expect(src).toContain('<span>Allow OpenAI for KG summaries (incurs cost)</span>');
    expect(src).toContain('<span>Allow OpenAI for embeddings (KG + code graph; incurs cost)</span>');
  });

  it('the embeddings toggle writes openai_embeddings_consent via app_state_set_bool', () => {
    const fn = findFunction(PREFS.instance, 'saveOpenAiEmbeddingsConsent') as Node;
    expect(fn).toBeTruthy();
    const invokes = callsNamed(fn, 'invoke');
    expect((invokes[0].arguments as Node[])[0].value).toBe('app_state_set_bool');
    expect(identifiers((invokes[0].arguments as Node[])[1]).has('APP_STATE_KEY_OPENAI_EMBEDDINGS_CONSENT')).toBe(true);
    const label = elements(PREFS.fragment, 'label').find(
      (l) => attrText(l, 'data-testid') === 'openai-embeddings-consent',
    );
    expect(identifiers(label).has('saveOpenAiEmbeddingsConsent')).toBe(true);
  });

  it('the code-graph OpenAI radio is gated on the EMBEDDINGS consent, not the summaries one', () => {
    const radio = elements(PREFS.fragment, 'input').find(
      (i) => attrText(i, 'name') === 'code-embed-override' && attrText(i, 'value') === 'openai',
    ) as Node;
    const ids = identifiers(attr(radio, 'disabled'));
    expect(ids.has('openaiEmbeddingsEffective')).toBe(true);
    expect(ids.has('kgSummaryConsent')).toBe(false);
  });

  it('re-review LOW-9: an unset toggle shows the EFFECTIVE consent the Python reader answers', () => {
    // The derived value is never re-computed in TS: it is the row, else the
    // status payload's `openai_embeddings_consent` (vco_lib/openai_consent.py).
    const derived = [...walk(PREFS.instance)].find(
      (n) => n.type === 'VariableDeclarator' && isIdent(n.id, 'openaiEmbeddingsEffective'),
    ) as Node;
    expect(derived, 'openaiEmbeddingsEffective').toBeTruthy();
    const props = new Set(
      [...walk(derived)]
        .filter((n) => n.type === 'MemberExpression' && (n.property as Node)?.type === 'Identifier')
        .map((n) => (n.property as Node).name as string),
    );
    expect(props.has('openai_embeddings_consent')).toBe(true);
    expect(identifiers(derived).has('openaiEmbeddingsConsent')).toBe(true);
    const label = elements(PREFS.fragment, 'label').find(
      (l) => attrText(l, 'data-testid') === 'openai-embeddings-consent',
    ) as Node;
    const input = elements(label, 'input')[0];
    expect(identifiers(attr(input, 'checked')).has('openaiEmbeddingsEffective')).toBe(true);
    expect(src).toMatch(/keys: \['openAi', 'openAiEmbeddingsConsent', 'codeEmbedStatus'\]/);
  });

  it('the consent key matches the Python reader', async () => {
    const { APP_STATE_KEY_OPENAI_EMBEDDINGS_CONSENT } = await import('./loaders');
    const py = readFileSync(resolve(here, '../../../../vco_lib/openai_consent.py'), 'utf8');
    expect(py).toContain(`APP_STATE_KEY_OPENAI_EMBEDDINGS_CONSENT = "${APP_STATE_KEY_OPENAI_EMBEDDINGS_CONSENT}"`);
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

// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.100 (WP-11): event + route census — the in-repo guard for the GUI's
// wiring. Two classes of silent breakage motivated it:
//
//   - an event the backend emits that NO frontend code listens to (the tray
//     menu, backend notices and update signals all went unheard at one point
//     — nothing fails, the click just does nothing), and the converse, a
//     listener waiting for an event nothing emits;
//   - a `goto(...)` / `href` pointing at a route that does not exist
//     (SvelteKit renders its 404 inside the launcher window).
//
// EVENTS. Emitters: every `<receiver>.emit(` / `.emit_to(` / `.emit_all(`
// in non-test Rust under `launcher/src-tauri/**` (all crates), the name
// resolved from a string literal or a same-file `const X: &str`. Listeners:
// every `listen(` / `once(` (and their import aliases) under
// `launcher/src/**`, resolved from a literal or a `const X = '…'`. A site
// whose name is computed at runtime must be declared in DYNAMIC_EMITTERS /
// DYNAMIC_LISTENERS below with the names it can take and a one-line reason;
// an undeclared dynamic site is a finding. Every emitted name must be heard
// and every heard name emitted, except the entries in UNHEARD_OK /
// UNEMITTED_OK — each with a one-line reason. Stale allowlist entries fail
// too, so the lists cannot rot.
//
// ROUTES. Targets: the literal (or template) first argument of `goto(`,
// every `/…` literal in an `href` attribute / `href:` prop / `href =`
// default, plus the backend-provided routes (`editor_route: Some("/…")`,
// the `=> "/…"` arms that fill `cta_route`). Each must match a
// `+page.svelte` under `src/routes/` (`[param]` segments match a template
// hole or any literal; `(group)` segments are transparent). A `goto` whose
// argument is a variable is declared in DYNAMIC_GOTOS with where its values
// come from.
//
// Red-proofs (run 2026-09-30 against the tree): deleting the tray listener
// (`await add(TRAY_ACTION_EVENT, …)` in stores/ui.ts) turns
// "every emitted event has a listener" red naming `vct-tray-action`;
// pointing `goto('/preferences/updates')` in DeferralBadge.svelte at
// `/preferences/update` turns "every route target resolves" red.

import { describe, expect, it } from 'vitest';
import { readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import {
  FRONTEND_SRC,
  loadFrontend,
  loadRust,
  relPath,
  rustStrConsts,
  sourceFile,
  tsStrConsts,
  type SourceFile,
} from './test-support/source-census';

// ─── extraction ────────────────────────────────────────────────────────────

export interface Site {
  file: string;
  /** Resolved event/route name, or null when computed at runtime. */
  name: string | null;
  /** The argument text as written (identifier/expression for dynamic sites). */
  expr: string;
}

const IDENT = /^[A-Za-z_][\w]*(?:(?:::|\.)[A-Za-z_]\w*)*(?:\(\))?/;

/** Call positions come from `f.code` (literal interiors blanked, so a call
 *  quoted inside a log message is not a call); the argument is read from
 *  `f.text` at the same offset. */
function argAfter(f: SourceFile, m: RegExpMatchArray): string {
  const at = m.index! + m[0].length;
  return f.text.slice(at, at + 200);
}

/** Rust emit sites. `self.emit(` is excluded: that is a local progress
 *  helper (update_run.rs) that forwards to `installer::emit_progress`, whose
 *  own `window.emit("install_progress", …)` is counted. */
export function rustEmitSites(files: SourceFile[]): Site[] {
  const out: Site[] = [];
  const re = /\b([a-z_]\w*(?:\.[a-z_]\w*)*)\s*\.\s*(emit|emit_to|emit_all)\s*\(\s*/g;
  for (const f of files) {
    const consts = rustStrConsts(f.text);
    for (const m of f.code.matchAll(re)) {
      if (m[1] === 'self') continue;
      let rest = argAfter(f, m);
      if (m[2] === 'emit_to') rest = rest.replace(/^[^,]*,\s*/, '');
      const lit = /^"([^"\\]+)"/.exec(rest);
      if (lit) {
        out.push({ file: f.rel, name: lit[1], expr: lit[0] });
        continue;
      }
      const id = IDENT.exec(rest)?.[0] ?? rest.slice(0, 40);
      const last = id.split('::').pop()!;
      const resolved = /^[A-Z][A-Z0-9_]*$/.test(last) ? consts.get(last) : undefined;
      out.push({ file: f.rel, name: resolved ?? null, expr: id });
    }
  }
  return out;
}

/** Names the `listen` function is imported/destructured as, anywhere. */
export function listenAliases(files: SourceFile[]): Set<string> {
  const out = new Set(['listen', 'once']);
  for (const f of files) {
    for (const m of f.code.matchAll(/\blisten\s+as\s+([A-Za-z_$][\w$]*)/g)) out.add(m[1]);
    for (const m of f.code.matchAll(/\{[^{}]*\blisten\s*:\s*([A-Za-z_$][\w$]*)[^{}]*\}\s*=/g)) out.add(m[1]);
  }
  return out;
}

/** Escape every RegExp metacharacter (not only `$`) before splicing an
 *  identifier into a pattern. */
function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export function frontendListenSites(files: SourceFile[]): Site[] {
  const aliases = [...listenAliases(files)].map(escapeRegExp).join('|');
  const re = new RegExp(
    `(?<![\\w$.])(${aliases})\\s*(?:<(?:[^<>()]|<(?:[^<>()]|<[^<>()]*>)*>)*>)?\\s*\\(\\s*`,
    'g',
  );
  const allConsts = new Map<string, string>();
  for (const f of files) for (const [k, v] of tsStrConsts(f.text)) allConsts.set(k, v);
  const out: Site[] = [];
  for (const f of files) {
    for (const m of f.code.matchAll(re)) {
      const before = f.code.slice(Math.max(0, m.index! - 16), m.index!);
      if (/\bfunction\s*$/.test(before)) continue; // a wrapper's own declaration
      const rest = argAfter(f, m);
      const lit = /^(['"`])([^'"`$\\]+)\1/.exec(rest);
      if (lit) {
        out.push({ file: f.rel, name: lit[2], expr: lit[0] });
        continue;
      }
      const id = IDENT.exec(rest)?.[0] ?? rest.slice(0, 40);
      out.push({ file: f.rel, name: allConsts.get(id) ?? null, expr: id });
    }
  }
  return out;
}

/** The shell registry in stores/ui.ts: `registerShellListeners` listens to
 *  every `add(<X>, …)` first argument plus every SHELL_NOTICE_EVENTS key,
 *  through ONE `listen(event, …)` call (declared dynamic below). */
export function shellRegistryNames(ui: SourceFile): string[] {
  const consts = tsStrConsts(ui.text);
  const names: string[] = [];
  for (const m of ui.code.matchAll(/\bawait\s+add\s*\(\s*([^,]+),/g)) {
    const at = m.index! + m[0].length - m[1].length - 1;
    const arg = ui.text.slice(at, at + m[1].length).trim();
    const lit = /^(['"`])([^'"`]+)\1$/.exec(arg);
    if (lit) names.push(lit[2]);
    else if (consts.has(arg)) names.push(consts.get(arg)!);
  }
  const start = ui.code.search(/\bSHELL_NOTICE_EVENTS\s*:[\s\S]*?=\s*\{/);
  if (start >= 0) {
    const open = ui.code.indexOf('= {', start) + 2;
    let depth = 0;
    let body = '';
    for (let i = open; i < ui.code.length; i++) {
      const c = ui.code[i];
      if (c === '{') depth++;
      if (c === '}') depth--;
      if (depth === 1 && c !== '{') body += ui.text[i];
      else if (depth >= 2) body += ' ';
      if (depth === 0) break;
    }
    for (const m of body.matchAll(/(?:^|,)\s*(?:(['"])([^'"]+)\1|([A-Za-z_$][\w$]*))\s*:/g)) {
      names.push(m[2] ?? m[3]);
    }
  }
  return names;
}

// ─── declarations (every entry carries its reason) ─────────────────────────

interface DynamicDecl {
  file: string;
  expr: string;
  /** Names this site can take; each must appear as a string literal in `file`. */
  names: string[];
  reason: string;
}

const DYNAMIC_EMITTERS: DynamicDecl[] = [
  {
    file: 'src/lib.rs',
    expr: 'event_name',
    names: ['vct-update-recovered', 'vct-update-failed'],
    reason: 'boot recovery picks one of two literals on the lines above',
  },
  {
    file: 'src/commands/openai_cmd.rs',
    expr: 'evt.name',
    names: ['vct-openai-key-invalidated', 'vct-openai-key-restored'],
    reason: 'PendingEvent.name is one of the EVT_OPENAI_KEY_* consts in this file',
  },
  {
    file: 'src/commands/module_dispatch.rs',
    expr: 'spec.progress_event.as_str()',
    names: [],
    reason: 'module-manifest `polling.progress_event`; the renderer listens to the same manifest field',
  },
  {
    file: 'src/commands/module_dispatch.rs',
    expr: 'spec.failed_event.as_str()',
    names: [],
    reason: 'module-manifest `polling.failed_event`; the renderer listens to the same manifest field',
  },
];

const DYNAMIC_LISTENERS: DynamicDecl[] = [
  {
    file: 'lib/tauri.ts',
    expr: 'event',
    names: [],
    reason: 'the `listen` wrapper itself — its callers are the census',
  },
  {
    file: 'lib/stores/orchestrator.ts',
    expr: 'event',
    names: [],
    reason: 'the store-local `tauriListen` wrapper — its callers are the census',
  },
  {
    file: 'lib/stores/ui.ts',
    expr: 'event',
    names: [], // filled from shellRegistryNames(ui.ts) below
    reason: 'registerShellListeners: TRAY_ACTION_EVENT + every SHELL_NOTICE_EVENTS key',
  },
  {
    file: 'lib/components/module-controls/StatusDisplayControl.svelte',
    expr: 'progressEvent',
    names: [],
    reason: 'module-manifest `polling.progress_event` (pairs with module_dispatch.rs)',
  },
  {
    file: 'lib/components/module-controls/StatusDisplayControl.svelte',
    expr: 'failedEvent',
    names: [],
    reason: 'module-manifest `polling.failed_event` (pairs with module_dispatch.rs)',
  },
];

/** Emitted with no frontend listener. Every entry is either a deliberate
 *  non-GUI consumer or an OPEN FINDING named as such (never silently OK). */
const UNHEARD_OK: Record<string, string> = {};

/** Listened for but never emitted by the launcher backend. */
const UNEMITTED_OK: Record<string, string> = {};

// ─── the tree ──────────────────────────────────────────────────────────────

const RUST = loadRust();
const FE = loadFrontend();
const UI = FE.find((f) => f.rel === 'lib/stores/ui.ts')!;

const EMITS = rustEmitSites(RUST);
const LISTENS = frontendListenSites(FE);
const REGISTRY = shellRegistryNames(UI);

function isDeclared(site: Site, decls: DynamicDecl[]): DynamicDecl | undefined {
  return decls.find((d) => d.file === site.file && d.expr === site.expr);
}

function emittedNames(): Set<string> {
  const s = new Set<string>();
  for (const e of EMITS) if (e.name) s.add(e.name);
  for (const d of DYNAMIC_EMITTERS) for (const n of d.names) s.add(n);
  return s;
}

function heardNames(): Set<string> {
  const s = new Set<string>();
  for (const l of LISTENS) if (l.name) s.add(l.name);
  for (const n of REGISTRY) s.add(n);
  for (const d of DYNAMIC_LISTENERS) for (const n of d.names) s.add(n);
  return s;
}

// ─── scanner fixtures (red-proof of the extractors) ────────────────────────

const rs = (raw: string) => [sourceFile('x.rs', raw, 'rust')];
const ts = (raw: string) => [sourceFile('x.ts', raw, 'ts')];

describe('scanner fixtures', () => {
  it('keeps `//` inside strings and drops real comments (Rust)', () => {
    const files = rs(`let a = "module://x"; // app.emit("in-comment", 1)\n/* app.emit("block") */ app.emit("real", 1);`);
    expect(rustEmitSites(files).map((s) => s.name)).toEqual(['real']);
    expect(files[0].text).toContain('"module://x"');
  });

  it('a `/*` inside a string does not swallow the code after it', () => {
    const files = rs(`let g = "commands/*.rs";\napp.emit("after-glob", 1);\n// */`);
    expect(rustEmitSites(files).map((s) => s.name)).toEqual(['after-glob']);
  });

  it('resolves a same-file const, reports a runtime name as dynamic, skips self.emit', () => {
    const files = rs(
      `const EVT: &str = "from-const";\nfn f(){ app.emit(EVT, p); handle.emit(evt.name, p); self.emit("stage", "m", 1.0); w.emit_to("main", "targeted", p); log("app.emit(\\"quoted\\")"); }`,
    );
    expect(rustEmitSites(files).map((s) => [s.name, s.expr])).toEqual([
      ['from-const', 'EVT'],
      [null, 'evt.name'],
      ['targeted', '"targeted"'],
    ]);
  });

  it('drops inline #[cfg(test)] modules only', () => {
    const files = rs(
      `fn live(){ app.emit("live", 1); }\n#[cfg(test)]\nmod tests { fn t(){ app.emit("test-only", "{"); } }\nfn after(){ app.emit("after", 1); }`,
    );
    expect(rustEmitSites(files).map((s) => s.name)).toEqual(['live', 'after']);
  });

  it('sees listen aliases and generics; ignores comments, strings and unlisten', () => {
    const files = ts(
      `import { listen as tauriListen } from '$lib/tauri';\n// listen('in-comment', h)\nconst un = await tauriListen<Record<string, number>>('aliased', h);\nunlisten();\nconsole.warn(\`listen('\${event}') failed\`);\nawait listen<X>(\n  'multi-line', h);\nconst { listen: rl } = await import('$lib/tauri');\nrl('destructured', h);\nasync function listen<T>(event: string) {}`,
    );
    expect(frontendListenSites(files).map((s) => s.name)).toEqual(['aliased', 'multi-line', 'destructured']);
  });

  it('reads the shell registry: add() consts and SHELL_NOTICE_EVENTS keys', () => {
    const [ui] = ts(
      `export const TRAY = 'tray-evt';\nexport const SHELL_NOTICE_EVENTS: Readonly<Record<string, H>> = {\n  plain_key: (p, a) => { a.x({ nested: 1 }, \`\${p}: {\`); },\n  'quoted-key': (p) => {},\n};\nasync function r(){ await add(TRAY, (p) => 1); }`,
    );
    expect(shellRegistryNames(ui).sort()).toEqual(['plain_key', 'quoted-key', 'tray-evt']);
  });

  it('actually scanned both trees', () => {
    expect(RUST.length).toBeGreaterThan(100);
    expect(FE.length).toBeGreaterThan(100);
    expect(EMITS.length).toBeGreaterThan(40);
    expect(LISTENS.length).toBeGreaterThan(25);
    expect(REGISTRY).toContain('vct-tray-action');
  });
});

// ─── the census ────────────────────────────────────────────────────────────

describe('event census', () => {
  it('every runtime-named emit site is declared', () => {
    const undeclared = EMITS.filter((e) => e.name === null && !isDeclared(e, DYNAMIC_EMITTERS)).map(
      (e) => `${e.file}: emit(${e.expr})`,
    );
    expect(undeclared).toEqual([]);
  });

  it('every runtime-named listen site is declared', () => {
    const undeclared = LISTENS.filter((l) => l.name === null && !isDeclared(l, DYNAMIC_LISTENERS)).map(
      (l) => `${l.file}: listen(${l.expr})`,
    );
    expect(undeclared).toEqual([]);
  });

  it('no stale dynamic declaration; declared names are real literals in that file', () => {
    const stale: string[] = [];
    for (const d of DYNAMIC_EMITTERS) {
      if (!EMITS.some((e) => e.file === d.file && e.expr === d.expr && e.name === null)) stale.push(`emit ${d.file} ${d.expr}`);
      const f = RUST.find((r) => r.rel === d.file);
      for (const n of d.names) if (!f?.text.includes(`"${n}"`)) stale.push(`emit ${d.file} names ${n}`);
    }
    for (const d of DYNAMIC_LISTENERS) {
      if (!LISTENS.some((l) => l.file === d.file && l.expr === d.expr && l.name === null)) stale.push(`listen ${d.file} ${d.expr}`);
    }
    expect(stale).toEqual([]);
  });

  it('every emitted event has a frontend listener', () => {
    const heard = heardNames();
    const unheard = [...emittedNames()].filter((n) => !heard.has(n) && !(n in UNHEARD_OK)).sort();
    expect(unheard).toEqual([]);
  });

  it('every frontend listener has a backend emitter', () => {
    const emitted = emittedNames();
    const unemitted = [...heardNames()].filter((n) => !emitted.has(n) && !(n in UNEMITTED_OK)).sort();
    expect(unemitted).toEqual([]);
  });

  it('allowlists hold only names that still need them', () => {
    const heard = heardNames();
    const emitted = emittedNames();
    expect(Object.keys(UNHEARD_OK).filter((n) => !emitted.has(n) || heard.has(n))).toEqual([]);
    expect(Object.keys(UNEMITTED_OK).filter((n) => !heard.has(n) || emitted.has(n))).toEqual([]);
  });
});

// ─── routes ────────────────────────────────────────────────────────────────

const ROUTES_DIR = join(FRONTEND_SRC, 'routes');

/** Route patterns (segment arrays) from every `+page.svelte`. */
export function routePatterns(dir = ROUTES_DIR): string[][] {
  const out: string[][] = [];
  const visit = (d: string) => {
    for (const name of readdirSync(d)) {
      const p = join(d, name);
      if (statSync(p).isDirectory()) visit(p);
      else if (name === '+page.svelte') {
        out.push(
          relPath(dir, d)
            .split('/')
            .filter((s) => s !== '' && s !== '.' && !/^\(.*\)$/.test(s)),
        );
      }
    }
  };
  visit(dir);
  return out;
}

/** Template holes become the `\u0000` marker; query and hash are dropped. */
export function normaliseTarget(raw: string): string[] {
  const path = raw.replace(/\$\{[^}]*\}/g, '\u0000').replace(/\{[^}]*\}/g, '\u0000').split(/[?#]/)[0];
  return path.split('/').filter((s) => s !== '');
}

export function routeResolves(target: string[], patterns: string[][]): boolean {
  return patterns.some((pat) => {
    const rest = pat.findIndex((s) => /^\[\.\.\.[^\]]+\]$/.test(s));
    if (rest < 0 && pat.length !== target.length) return false;
    if (rest >= 0 && target.length < rest) return false;
    const n = rest < 0 ? pat.length : rest;
    for (let i = 0; i < n; i++) {
      const seg = pat[i];
      const dyn = /^\[[^\]]+\]$/.test(seg);
      if (dyn) continue;
      if (seg !== target[i]) return false; // a hole never matches a static segment
    }
    return true;
  });
}

export interface RouteSite {
  file: string;
  target: string | null;
  expr: string;
}

export function frontendRouteSites(files: SourceFile[]): RouteSite[] {
  const out: RouteSite[] = [];
  for (const f of files) {
    for (const m of f.code.matchAll(/(?<![\w$])goto\s*\(\s*/g)) {
      const rest = argAfter(f, m);
      const lit = /^(['"`])([^'"`]*)\1/.exec(rest);
      if (lit) out.push({ file: f.rel, target: lit[2], expr: lit[0] });
      else out.push({ file: f.rel, target: null, expr: IDENT.exec(rest)?.[0] ?? rest.slice(0, 40) });
    }
    // href="…" / href: '…' / href = '…'
    for (const m of f.text.matchAll(/\bhref\s*[:=]\s*(['"`])([^'"`]*)\1/g)) {
      out.push({ file: f.rel, target: m[2], expr: m[0] });
    }
    // href={…} — every `/…` literal inside the braces (ternaries included)
    for (const m of f.text.matchAll(/\bhref=\{([^}]*)\}/g)) {
      for (const l of m[1].matchAll(/(['"`])(\/[^'"`]*)\1/g)) out.push({ file: f.rel, target: l[2], expr: m[0] });
    }
  }
  return out.filter((s) => s.target === null || (s.target.startsWith('/') && !s.target.startsWith('//')));
}

/** Routes the BACKEND hands the frontend to navigate to. */
export function backendRouteSites(files: SourceFile[]): RouteSite[] {
  const out: RouteSite[] = [];
  for (const f of files) {
    for (const m of f.text.matchAll(/\beditor_route\s*:\s*Some\(\s*"(\/[^"]*)"/g)) {
      out.push({ file: f.rel, target: m[1], expr: m[0] });
    }
    if (/\bcta_route\s*:\s*route\.into\(\)/.test(f.text)) {
      for (const m of f.text.matchAll(/=>\s*"(\/[a-z][^"]*)"/g)) out.push({ file: f.rel, target: m[1], expr: m[0] });
    }
  }
  return out;
}

interface DynamicGoto {
  file: string;
  expr: string;
  reason: string;
}

const DYNAMIC_GOTOS: DynamicGoto[] = [
  { file: 'lib/stores/ui.ts', expr: 'target', reason: "openSettings: '/preferences' or '/preferences/secrets' (both literal on the line above, both censused)" },
  { file: 'lib/components/NoProjectBanner.svelte', expr: 'href', reason: "the `href = '/project'` prop default (censused); callers pass none" },
  { file: 'lib/components/module-controls/LinkControl.svelte', expr: 'control.href', reason: 'module-manifest link control, internal target' },
  { file: 'lib/components/CoreModuleSettingsPanel.svelte', expr: 'href', reason: 'editorHref(binding) ← backend `editor_route` (censused as backend routes)' },
  { file: 'lib/components/ModuleCatalog.svelte', expr: 'display.cta_route', reason: 'backend `cta_route` (censused as backend routes)' },
  { file: 'routes/p/[slug]/+page.svelte', expr: 'target', reason: 'rememberedSection(): an allow-set of literal sections in the same file (censused as goto-free literals below)' },
  { file: 'routes/+layout.svelte', expr: 'path', reason: 'registerShellListeners goto adapter; its targets are the ui.ts routeTrayAction literals' },
];

describe('route census', () => {
  const patterns = routePatterns();
  const feSites = frontendRouteSites(FE);
  const beSites = backendRouteSites(RUST);

  it('route fixtures: holes match params only, groups are transparent', () => {
    const pats = [['project', '[id]'], ['preferences', 'updates'], ['p', '[slug]']];
    expect(routeResolves(normaliseTarget('/project/${id}'), pats)).toBe(true);
    expect(routeResolves(normaliseTarget('/preferences/updates?x=1#top'), pats)).toBe(true);
    expect(routeResolves(normaliseTarget('/preferences/update'), pats)).toBe(false);
    expect(routeResolves(normaliseTarget('/preferences/${x}'), pats)).toBe(false);
    expect(routeResolves(normaliseTarget('/project/{project_id}'), pats)).toBe(true);
    expect(patterns.some((p) => p.length === 0)).toBe(true); // the dashboard `/`
  });

  it('scanned real targets', () => {
    expect(feSites.filter((s) => s.target).length).toBeGreaterThan(40);
    expect(beSites.length).toBeGreaterThanOrEqual(4);
  });

  it('every route target resolves to an existing +page.svelte', () => {
    const broken = [...feSites, ...beSites]
      .filter((s) => s.target !== null && !routeResolves(normaliseTarget(s.target), patterns))
      .map((s) => `${s.file}: ${s.target}`);
    expect(broken).toEqual([]);
  });

  it('the /p/[slug] remembered-section allow-set only names real routes', () => {
    const f = FE.find((x) => x.rel === 'routes/p/[slug]/+page.svelte')!;
    const set = /new Set\(\[([^\]]*)\]\)/.exec(f.text)?.[1] ?? '';
    const lits = [...set.matchAll(/'(\/[^']*)'/g)].map((m) => m[1]);
    expect(lits.length).toBeGreaterThan(3);
    expect(lits.filter((t) => !routeResolves(normaliseTarget(t), patterns))).toEqual([]);
  });

  it('every variable goto is declared, and no declaration is stale', () => {
    const dyn = feSites.filter((s) => s.target === null);
    const undeclared = dyn
      .filter((s) => !DYNAMIC_GOTOS.some((d) => d.file === s.file && d.expr === s.expr))
      .map((s) => `${s.file}: goto(${s.expr})`);
    const stale = DYNAMIC_GOTOS.filter((d) => !dyn.some((s) => s.file === d.file && s.expr === d.expr)).map(
      (d) => `${d.file}: ${d.expr}`,
    );
    expect({ undeclared, stale }).toEqual({ undeclared: [], stale: [] });
  });
});

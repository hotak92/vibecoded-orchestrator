// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.100 (WP-11): the source-reading half of the launcher's wiring
// censuses (`event-census.test.ts`, `invoke-names.test.ts`). Test-only: no
// runtime module imports this file.
//
// Why a lexer rather than a regex: a census that strips comments with
// regexes eats real code the moment a string holds `/*` or `//` (a
// `commands/*` glob, a `module://…` event name) — the stripped text then
// silently loses whole functions and the census passes on a fraction of the
// tree. `stripComments` walks the source once, keeps every string/char/
// template/regex literal byte-for-byte, and blanks comments to spaces (line
// breaks kept, so offsets and line numbers still line up).

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

/** `launcher/` — the census roots are all below it. */
export const LAUNCHER_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
export const FRONTEND_SRC = join(LAUNCHER_ROOT, 'src');
export const TAURI_ROOT = join(LAUNCHER_ROOT, 'src-tauri');

const SKIP_DIRS = new Set(['node_modules', 'target', 'gen', 'test-stubs', 'test-support']);

/** Recursive file walk; `*.test.ts` and the skip-dirs above are excluded. */
export function walkFiles(dir: string, ext: RegExp, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (SKIP_DIRS.has(name)) continue;
      walkFiles(p, ext, out);
    } else if (ext.test(name) && !/\.test\.ts$/.test(name)) {
      out.push(p);
    }
  }
  return out;
}

export function relPath(root: string, abs: string): string {
  return relative(root, abs).split(sep).join('/');
}

export type Lang = 'ts' | 'rust';

function blank(s: string): string {
  return s.replace(/[^\n]/g, ' ');
}

/** Index just past a quoted literal that opens at `i` with `q`. A TS
 *  '…' / "…" literal cannot span lines, so an unterminated one (an
 *  apostrophe in Svelte markup text: "don't") ends at the line break
 *  instead of swallowing the rest of the file. */
function skipQuoted(src: string, i: number, q: string, lang: Lang): number {
  let j = i + 1;
  while (j < src.length && src[j] !== q) {
    if (src[j] === '\\') j++;
    else if (src[j] === '\n' && lang === 'ts' && q !== '`') return j;
    j++;
  }
  return Math.min(j + 1, src.length);
}

/** TS regex literal starts here? Decided by the previous significant char. */
function regexCanStart(src: string, i: number): boolean {
  let k = i - 1;
  while (k >= 0 && /\s/.test(src[k])) k--;
  if (k < 0) return true;
  if ('(,=:[!&|?{};+-*%<>~^'.includes(src[k])) return true;
  const word = /([A-Za-z_$]+)$/.exec(src.slice(Math.max(0, k - 10), k + 1));
  return !!word && ['return', 'typeof', 'case', 'in', 'of', 'delete', 'void', 'throw'].includes(word[1]);
}

function skipRegex(src: string, i: number): number {
  let j = i + 1;
  let inClass = false;
  while (j < src.length && src[j] !== '\n') {
    const c = src[j];
    if (c === '\\') j++;
    else if (c === '[') inClass = true;
    else if (c === ']') inClass = false;
    else if (c === '/' && !inClass) return j + 1;
    j++;
  }
  return i + 1; // not a regex after all — treat `/` as an operator
}

/** Remove comments, keep every literal. Svelte `<!-- -->` counts as a
 *  comment for `ts` (the census reads `.svelte` files as TS). */
export function stripComments(src: string, lang: Lang): string {
  return transform(src, lang, false);
}

/** Same length as `src`, comments AND literal interiors blanked (the
 *  delimiters stay). Match call positions against this, then read the
 *  argument from the `stripComments` text at the same offset — so a
 *  `listen('…')` inside a log message string is never taken for a call. */
export function maskLiterals(src: string, lang: Lang): string {
  return transform(src, lang, true);
}

function transform(src: string, lang: Lang, mask: boolean): string {
  const lit = (s: string) => (mask && s.length >= 2 ? s[0] + blank(s.slice(1, -1)) + s[s.length - 1] : s);
  let out = '';
  let i = 0;
  const n = src.length;
  while (i < n) {
    const c = src[i];
    const d = src[i + 1];
    if (c === '/' && d === '/') {
      const e = src.indexOf('\n', i);
      const end = e < 0 ? n : e;
      out += blank(src.slice(i, end));
      i = end;
    } else if (c === '/' && d === '*') {
      const e = src.indexOf('*/', i + 2);
      const end = e < 0 ? n : e + 2;
      out += blank(src.slice(i, end));
      i = end;
    } else if (lang === 'ts' && src.startsWith('<!--', i)) {
      const e = src.indexOf('-->', i + 4);
      const end = e < 0 ? n : e + 3;
      out += blank(src.slice(i, end));
      i = end;
    } else if (c === '"' || (lang === 'ts' && (c === "'" || c === '`'))) {
      const end = skipQuoted(src, i, c, lang);
      out += lit(src.slice(i, end));
      i = end;
    } else if (lang === 'rust' && c === 'r' && /^r#*"/.test(src.slice(i, i + 8)) && !/[\w]/.test(src[i - 1] ?? '')) {
      const hashes = /^r(#*)"/.exec(src.slice(i, i + 8))![1];
      const close = '"' + hashes;
      const e = src.indexOf(close, i + 2 + hashes.length);
      const end = e < 0 ? n : e + close.length;
      out += lit(src.slice(i, end));
      i = end;
    } else if (lang === 'rust' && c === "'") {
      // char literal ('a', '\n', '\'', '"') vs lifetime ('a, 'static)
      const m = /^'(?:\\.[^']*|[^\\'])'/.exec(src.slice(i, i + 12));
      const end = m ? i + m[0].length : i + 1;
      out += m ? lit(src.slice(i, end)) : src.slice(i, end);
      i = end;
    } else if (lang === 'ts' && c === '/' && regexCanStart(src, i)) {
      const end = skipRegex(src, i);
      out += end > i + 1 ? lit(src.slice(i, end)) : src.slice(i, end);
      i = end;
    } else {
      out += c;
      i++;
    }
  }
  return out;
}

/** Index of the brace matching the `{` at `open` (string-aware; input is
 *  already comment-stripped). -1 when unbalanced. */
export function matchBrace(src: string, open: number, lang: Lang): number {
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    const c = src[i];
    if (c === '"' || (lang === 'ts' && (c === "'" || c === '`'))) {
      i = skipQuoted(src, i, c, lang) - 1;
    } else if (lang === 'rust' && c === "'") {
      const m = /^'(?:\\.[^']*|[^\\'])'/.exec(src.slice(i, i + 12));
      if (m) i += m[0].length - 1;
    } else if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

/** [start, end) of every inline `#[cfg(test)] mod x { … }` block in
 *  comment-stripped Rust: what a unit test emits is not what the launcher
 *  emits. */
export function rustTestModuleRanges(src: string): [number, number][] {
  const re = /#\[cfg\(test\)\]\s*(?:#\[[^\]]*\]\s*)*(?:pub(?:\([^)]*\))?\s+)?mod\s+\w+\s*\{/g;
  const out: [number, number][] = [];
  for (const m of src.matchAll(re)) {
    if (out.length && m.index! < out[out.length - 1][1]) continue; // nested
    const close = matchBrace(src, m.index! + m[0].length - 1, 'rust');
    out.push([m.index!, close < 0 ? src.length : close + 1]);
  }
  return out;
}

function blankRanges(src: string, ranges: [number, number][]): string {
  let out = src;
  for (const [a, b] of ranges) out = out.slice(0, a) + blank(out.slice(a, b)) + out.slice(b);
  return out;
}

/** Comment-stripped Rust with its inline test modules blanked. */
export function stripRustTestModules(src: string): string {
  return blankRanges(src, rustTestModuleRanges(src));
}

/** `const NAME: &str = "value";` (any visibility) → NAME → value. */
export function rustStrConsts(src: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const m of src.matchAll(/\bconst\s+([A-Z][A-Z0-9_]*)\s*:\s*&(?:'static\s+)?str\s*=\s*"([^"\\]*)"\s*;/g)) {
    out.set(m[1], m[2]);
  }
  return out;
}

/** `const NAME = 'value'` (optionally `export`, `: string`, `as const`). */
export function tsStrConsts(src: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const m of src.matchAll(/\bconst\s+([A-Z][A-Z0-9_]*)\s*(?::\s*string\s*)?=\s*(['"`])([^'"`$\\]*)\2/g)) {
    out.set(m[1], m[3]);
  }
  return out;
}

export interface SourceFile {
  rel: string;
  /** Comments stripped, literals intact. */
  text: string;
  /** Same offsets as `text`, literal interiors blanked too. */
  code: string;
}

export function sourceFile(rel: string, raw: string, lang: Lang): SourceFile {
  const text = stripComments(raw, lang);
  const code = maskLiterals(raw, lang);
  if (lang === 'ts') return { rel, text, code };
  const tests = rustTestModuleRanges(text);
  return { rel, text: blankRanges(text, tests), code: blankRanges(code, tests) };
}

/** Every non-test `.rs` under `src-tauri/` (all crates), comments and
 *  inline test modules stripped. `rel` is relative to `src-tauri/`. */
export function loadRust(): SourceFile[] {
  return walkFiles(TAURI_ROOT, /\.rs$/)
    .filter((abs) => !relPath(TAURI_ROOT, abs).split('/').includes('tests'))
    .map((abs) => sourceFile(relPath(TAURI_ROOT, abs), readFileSync(abs, 'utf-8'), 'rust'));
}

/** Every non-test `.ts`/`.svelte` under `launcher/src/`, comments stripped.
 *  `rel` is relative to `src/`. */
export function loadFrontend(): SourceFile[] {
  return walkFiles(FRONTEND_SRC, /\.(ts|svelte)$/).map((abs) =>
    sourceFile(relPath(FRONTEND_SRC, abs), readFileSync(abs, 'utf-8'), 'ts'),
  );
}

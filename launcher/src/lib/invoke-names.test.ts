// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.100 (WP-11): invoke-name census — every command the frontend invokes
// by literal name is registered in `generate_handler!` (lib.rs), and every
// registered command is either invoked by literal name somewhere under
// `launcher/src/**`, reachable through a module manifest
// (`module_dispatch.rs::is_whitelisted_manifest_command` — the `module_`
// prefix or MANIFEST_DISPATCHABLE_COMMANDS), or classified in
// NOT_INVOKED_OK below with a one-line reason.
//
// A literal invoke of an unregistered name fails at runtime with "command
// not found" — typically inside a soft `safeInvoke` that swallows it into
// `null`, so the feature just reads as "not available". A registered command
// nothing calls is either reached some other way (say so) or dead (say so:
// classification `uncalled-finding` is recorded as an OPEN FINDING, never as OK).
//
// The update-specific census (`update-invoke-census.test.ts`) pins WHERE the
// update commands are called; this file pins that the names exist at all.

import { describe, expect, it } from 'vitest';
import { loadFrontend, loadRust, sourceFile, type SourceFile } from './test-support/source-census';
// SF-5 (v0.2.101): the shared extractor's ONE home is test-support; this
// file re-exports it so existing importers keep working.
import { registeredCommands } from './test-support/wiring-ast';
export { registeredCommands };

// ─── extraction ────────────────────────────────────────────────────────────

export interface InvokeSite {
  file: string;
  name: string | null;
  expr: string;
}

/** `invoke` / `safeInvoke` and their import aliases (`invoke as X`). */
export function invokeAliases(files: SourceFile[]): Set<string> {
  const out = new Set(['invoke', 'safeInvoke']);
  for (const f of files) for (const m of f.code.matchAll(/\b(?:safeInvoke|invoke)\s+as\s+([A-Za-z_$][\w$]*)/g)) out.add(m[1]);
  return out;
}

export function invokeSites(files: SourceFile[]): InvokeSite[] {
  const aliases = [...invokeAliases(files)].join('|');
  // Member calls count too (`deps.invoke('x')` — the injected form used for testability): the
  // lookbehind excludes only identifier chars, so `foo.invoke(` matches; any literal it yields must
  // still be a registered command, so this cannot whitelist a non-command.
  const re = new RegExp(`(?<![\\w$])(${aliases})\\s*(?:<(?:[^<>()]|<(?:[^<>()]|<[^<>()]*>)*>)*>)?\\s*\\(\\s*`, 'g');
  const out: InvokeSite[] = [];
  for (const f of files) {
    for (const m of f.code.matchAll(re)) {
      if (/\bfunction\s*$/.test(f.code.slice(Math.max(0, m.index! - 16), m.index!))) continue;
      const at = m.index! + m[0].length;
      const rest = f.text.slice(at, at + 160);
      const lit = /^(['"`])([^'"`$\\]*)\1/.exec(rest);
      if (lit) out.push({ file: f.rel, name: lit[2], expr: lit[0] });
      else out.push({ file: f.rel, name: null, expr: /^[A-Za-z_$][\w$.]*/.exec(rest)?.[0] ?? rest.slice(0, 40) });
    }
  }
  return out;
}

/** MANIFEST_DISPATCHABLE_COMMANDS entries (module_dispatch.rs). */
export function manifestDispatchable(dispatch: SourceFile): string[] {
  const m = /MANIFEST_DISPATCHABLE_COMMANDS\s*:\s*&\[&str\]\s*=\s*&\[([^\]]*)\]/.exec(dispatch.text);
  return m ? [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]) : [];
}

// ─── declarations ──────────────────────────────────────────────────────────

interface DynamicInvoke {
  file: string;
  expr: string;
  /** Names this site can take; each must appear as a literal in `file`. */
  names: string[];
  reason: string;
}

const DYNAMIC_INVOKES: DynamicInvoke[] = [
  { file: 'lib/tauri.ts', expr: 'cmd', names: [], reason: 'the invoke/safeInvoke wrappers themselves' },
  {
    file: 'routes/services/+page.svelte',
    expr: 'cmd',
    names: ['service_start', 'service_stop', 'service_restart'],
    reason: "the `cmd` parameter's literal-union type",
  },
  {
    file: 'lib/module-dispatch.ts',
    expr: 'action',
    names: [],
    reason: 'manifest ActionRef::Legacy string, gated by module_manifest_command_allowed',
  },
  {
    file: 'lib/components/ModuleConfigTab.svelte',
    expr: 'action',
    names: [],
    reason: 'manifest ActionRef::Legacy string, gated by module_manifest_command_allowed',
  },
];

type NotInvokedClass = 'rust-only' | 'tray' | 'owner-deferred' | 'uncalled-finding';

/** Registered, never invoked by literal name, not manifest-dispatchable.
 *  `uncalled-finding` = no caller in the GUI, the tray or other Rust code
 *  (surveyed 2026-09-30): an OPEN FINDING — dead, or a capability whose GUI
 *  was never wired. Per the promise rule it is NOT deleted on this census's
 *  authority; each one is the owner's call (wire it, name its successor, or
 *  approve removal). The census only guarantees the list cannot grow
 *  silently. */
// `owner-deferred`: the owner has scheduled the work; the command stays registered
// until then. NOT an open finding — but it is not silent either: each carries
// the owner's words and the release, and the census still fails the moment it
// gains a caller (the entry must then be removed).
const OWNER_RL = 'owner: kept for when RL work resumes (v0.2.102+)';
const OWNER_0102 = 'owner-deferred to v0.2.102';
const OWNER_030 = 'owner-deferred to v0.3.0';
// `rust-only` / `tray` entries name the Rust file that calls the command fn
// (`caller`, relative to launcher/src-tauri). W5R-12: the census VERIFIES that
// file contains a real call — outside comments, strings and test modules — so
// removing the caller turns the census red instead of leaving a stale reason.
const NOT_INVOKED_OK: Record<string, { class: NotInvokedClass; reason: string; caller?: string }> = {
  check_for_launcher_update: {
    class: 'rust-only',
    reason: 'self_update.rs daily background check calls the command fn directly',
    caller: 'src/commands/self_update.rs',
  },
  prepare_windows_update_handoff: {
    class: 'rust-only',
    reason: 'services/binary_freshness.rs calls it on the Windows binary swap',
    caller: 'src/services/binary_freshness.rs',
  },
  validate_model_against_catalog: {
    class: 'rust-only',
    reason: 'reused by project_state_cmd.rs commands; no direct GUI call',
    caller: 'src/commands/project_state_cmd.rs',
  },
  get_cached_update_status: {
    class: 'tray',
    reason: 'tray.rs reads the cached status to label the tray update item',
    caller: 'src/tray.rs',
  },
  apply_module_db_migrations: { class: 'owner-deferred', reason: OWNER_RL },
  check_for_weights_update_now: { class: 'owner-deferred', reason: OWNER_RL },
  delete_project_codegraph_binding: { class: 'owner-deferred', reason: OWNER_0102 },
  diagram_grant_access: { class: 'owner-deferred', reason: OWNER_0102 },
  list_diagram_access: { class: 'owner-deferred', reason: OWNER_0102 },
  migrate_to_bind_path: { class: 'owner-deferred', reason: OWNER_0102 },
  migrate_to_named_volume: { class: 'owner-deferred', reason: OWNER_0102 },
  perform_hard_cut: { class: 'owner-deferred', reason: OWNER_030 },
  preflight_install_safety_check: { class: 'owner-deferred', reason: OWNER_0102 },
  read_install_log: { class: 'owner-deferred', reason: OWNER_0102 },
  restart_rl_container: { class: 'owner-deferred', reason: OWNER_RL },
  set_project_mcp_server_enabled: { class: 'owner-deferred', reason: OWNER_0102 },
  unregister_project_mcp_server: { class: 'owner-deferred', reason: OWNER_0102 },
};

// ─── the tree ──────────────────────────────────────────────────────────────

const RUST = loadRust();
const FE = loadFrontend();
const LIB = RUST.find((f) => f.rel === 'src/lib.rs')!;
const DISPATCH = RUST.find((f) => f.rel === 'src/commands/module_dispatch.rs')!;

const REGISTERED = registeredCommands(LIB);
const SITES = invokeSites(FE);
const MANIFEST = manifestDispatchable(DISPATCH);

function invokedNames(): Set<string> {
  const s = new Set<string>();
  for (const x of SITES) if (x.name) s.add(x.name);
  for (const d of DYNAMIC_INVOKES) for (const n of d.names) s.add(n);
  return s;
}

function manifestReachable(name: string): boolean {
  return name.startsWith('module_') || MANIFEST.includes(name);
}

/** True when `f` CALLS `name(...)` in live code: comments, string literals
 *  and `#[cfg(test)]` modules are already blanked in `f.code`, and the
 *  function's own definition (`fn name(`) does not count as a call. */
export function rustCalls(f: SourceFile, name: string): boolean {
  const re = new RegExp(`(?<![\\w$])${name}\\s*\\(`, 'g');
  for (const m of f.code.matchAll(re)) {
    if (/\bfn\s+$/.test(f.code.slice(Math.max(0, m.index! - 8), m.index!))) continue;
    return true;
  }
  return false;
}

// ─── fixtures ──────────────────────────────────────────────────────────────

describe('scanner fixtures', () => {
  it('parses generate_handler! with paths, cfg attributes and comments', () => {
    const lib = sourceFile(
      'lib.rs',
      `fn run(){ builder.invoke_handler(tauri::generate_handler![\n  // commands::gone::in_comment,\n  commands::a::first,\n  #[cfg(target_os = "linux")]\n  commands::b::linux_only,\n  bare_name,\n]).run(ctx); }`,
      'rust',
    );
    expect(registeredCommands(lib)).toEqual(['first', 'linux_only', 'bare_name']);
  });

  it('sees literal, typed and aliased invokes; not comments or strings', () => {
    const files = [
      sourceFile(
        'a.ts',
        `import { invoke as tauriInvoke } from '$lib/tauri';\n// invoke('in_comment')\nawait invoke<Record<string, X>>('typed', {});\nawait safeInvoke("soft");\nawait tauriInvoke('aliased');\nconsole.log("invoke('in_string')");\nawait invoke(cmd, {});\nexport async function invoke<T>(cmd: string) {}`,
        'ts',
      ),
    ];
    expect(invokeSites(files).map((s) => s.name ?? `DYN:${s.expr}`)).toEqual(['typed', 'soft', 'aliased', 'DYN:cmd']);
  });

  it('rustCalls sees a call, not a definition, comment, string or test module', () => {
    const f = sourceFile(
      'x.rs',
      `pub fn target() {}\n// target()\nfn other() { let s = "target()"; }\n#[cfg(test)]\nmod tests { fn t() { super::target(); } }`,
      'rust',
    );
    expect(rustCalls(f, 'target')).toBe(false);
    const g = sourceFile('y.rs', `fn other() { let _ = crate::m::target(1); }`, 'rust');
    expect(rustCalls(g, 'target')).toBe(true);
  });

  it('actually scanned the tree', () => {
    expect(REGISTERED.length).toBeGreaterThan(300);
    expect(SITES.filter((s) => s.name).length).toBeGreaterThan(300);
    expect(MANIFEST.length).toBeGreaterThan(10);
  });
});

// ─── the census ────────────────────────────────────────────────────────────

describe('invoke-name census', () => {
  it('no command is registered twice', () => {
    const seen = new Set<string>();
    const dup = REGISTERED.filter((n) => (seen.has(n) ? true : (seen.add(n), false)));
    expect(dup).toEqual([]);
  });

  it('every literal invoke names a registered command', () => {
    const reg = new Set(REGISTERED);
    const missing = SITES.filter((s) => s.name !== null && !reg.has(s.name)).map((s) => `${s.file}: ${s.name}`);
    expect(missing).toEqual([]);
  });

  it('every dynamic invoke is declared; no declaration is stale', () => {
    const dyn = SITES.filter((s) => s.name === null);
    const undeclared = dyn
      .filter((s) => !DYNAMIC_INVOKES.some((d) => d.file === s.file && d.expr === s.expr))
      .map((s) => `${s.file}: invoke(${s.expr})`);
    const stale: string[] = [];
    for (const d of DYNAMIC_INVOKES) {
      if (!dyn.some((s) => s.file === d.file && s.expr === d.expr)) stale.push(`${d.file}: ${d.expr}`);
      const f = FE.find((x) => x.rel === d.file);
      for (const n of d.names) if (!new RegExp(`['"\`]${n}['"\`]`).test(f?.text ?? '')) stale.push(`${d.file}: ${n}`);
    }
    expect({ undeclared, stale }).toEqual({ undeclared: [], stale: [] });
  });

  it('every registered command is invoked, manifest-reachable, or classified', () => {
    const invoked = invokedNames();
    const unclassified = REGISTERED.filter(
      (n) => !invoked.has(n) && !manifestReachable(n) && !(n in NOT_INVOKED_OK),
    ).sort();
    expect(unclassified).toEqual([]);
  });

  it('every owner-deferred entry names the owner and a release', () => {
    const bad = Object.entries(NOT_INVOKED_OK)
      .filter(([, v]) => v.class === 'owner-deferred' && !/owner.*v0\.\d+\.\d+/.test(v.reason))
      .map(([n]) => n);
    expect(bad).toEqual([]);
  });

  it('every rust-only / tray entry names a Rust caller that really calls it (W5R-12)', () => {
    const bad: string[] = [];
    for (const [name, v] of Object.entries(NOT_INVOKED_OK)) {
      if (v.class !== 'rust-only' && v.class !== 'tray') continue;
      const f = v.caller ? RUST.find((x) => x.rel === v.caller) : undefined;
      if (!f) {
        bad.push(`${name}: caller file ${v.caller ?? '(none named)'} not found`);
        continue;
      }
      if (!rustCalls(f, name)) bad.push(`${name}: no call in ${v.caller}`);
    }
    expect(bad).toEqual([]);
  });

  it('NOT_INVOKED_OK holds only registered, still-uninvoked names', () => {
    const invoked = invokedNames();
    const reg = new Set(REGISTERED);
    const stale = Object.keys(NOT_INVOKED_OK).filter((n) => !reg.has(n) || invoked.has(n) || manifestReachable(n));
    expect(stale).toEqual([]);
  });
});

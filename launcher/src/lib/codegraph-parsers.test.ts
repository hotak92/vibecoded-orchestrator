// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.102 W6-L — the parser-offer controller, driven with a fake invoker.
// Pins what each action sends to the backend — and what it must NOT: a
// check never installs, Install sends EXACTLY the checked set, Not now
// installs nothing, a failed install stays visible.

import { describe, expect, it } from 'vitest';
import {
  createParsersController,
  explanation,
  INITIAL_PARSERS_STATE,
  joinNames,
  projectPaths,
  type ParserOffer,
  type ParserOfferReport,
  type ParsersState,
} from './codegraph-parsers';

const PS: ParserOffer = {
  id: 'powershell',
  label: 'PowerShell',
  package: 'tree-sitter-powershell',
  optional: true,
  languages: ['powershell'],
  projects: ['/w/app'],
};
const PROTO: ParserOffer = {
  id: 'proto',
  label: 'Protocol Buffers',
  package: 'tree-sitter-language-pack',
  optional: true,
  languages: ['proto'],
  projects: ['/w/app'],
};

type Call = { cmd: string; args?: Record<string, unknown> };

function harness(answers: Record<string, unknown | ((args?: Record<string, unknown>) => unknown)>) {
  let state: ParsersState = { ...INITIAL_PARSERS_STATE };
  const calls: Call[] = [];
  const invoke = async <T,>(cmd: string, args?: Record<string, unknown>): Promise<T> => {
    calls.push({ cmd, args });
    const a = answers[cmd];
    if (a instanceof Error) throw a;
    return (typeof a === 'function' ? (a as (x?: Record<string, unknown>) => unknown)(args) : a) as T;
  };
  const ctl = createParsersController({
    invoke,
    set: (s) => {
      state = s;
    },
    get: () => state,
  });
  return { ctl, calls, state: () => state };
}

const report = (offers: ParserOffer[]): ParserOfferReport => ({ offers, broken: [], errors: [] });

describe('parser offers — check', () => {
  it('opens the modal with every offered parser checked, and installs nothing', async () => {
    const h = harness({ codegraph_parser_offers: report([PS, PROTO]) });
    await h.ctl.check(['/w/app', '/w/app', ' ', '/w/lib']);
    expect(h.calls).toEqual([
      { cmd: 'codegraph_parser_offers', args: { projectPaths: ['/w/app', '/w/lib'] } },
    ]);
    expect(h.state().open).toBe(true);
    expect(h.state().selected).toEqual(['powershell', 'proto']);
  });

  it('leave-alone: nothing to offer (all installed) -> no modal', async () => {
    const h = harness({ codegraph_parser_offers: report([]) });
    await h.ctl.check(['/w/app']);
    expect(h.state().open).toBe(false);
    expect(h.calls.map((c) => c.cmd)).toEqual(['codegraph_parser_offers']);
  });

  it('leave-alone: a failed check shows no modal and installs nothing', async () => {
    const h = harness({ codegraph_parser_offers: new Error('boom') });
    await h.ctl.check(['/w/app']);
    expect(h.state().open).toBe(false);
    expect(h.calls.map((c) => c.cmd)).toEqual(['codegraph_parser_offers']);
  });

  it('never replaces a modal already up', async () => {
    const h = harness({ codegraph_parser_offers: report([PS]) });
    await h.ctl.check(['/w/app']);
    await h.ctl.check(['/w/other']);
    expect(h.calls.length).toBe(1);
    expect(h.state().offers).toEqual([PS]);
  });

  it('no paths -> no backend call', async () => {
    const h = harness({});
    await h.ctl.check([]);
    expect(h.calls).toEqual([]);
  });
});

describe('parser offers — checkbox -> install sends exactly the checked set', () => {
  it('unchecking PowerShell installs only proto, and declines PowerShell', async () => {
    const h = harness({
      codegraph_parser_offers: report([PS, PROTO]),
      codegraph_parsers_install: { ok: true, message: 'Installed.', installed: ['proto'], failed: [], log_path: '', log_tail: '' },
      codegraph_parsers_decline: null,
    });
    await h.ctl.check(['/w/app']);
    h.ctl.toggle('powershell');
    expect(h.state().selected).toEqual(['proto']);
    await h.ctl.install();
    expect(h.calls.slice(1)).toEqual([
      { cmd: 'codegraph_parsers_install', args: { parsers: ['proto'] } },
      { cmd: 'codegraph_parsers_decline', args: { parsers: ['powershell'], projectPaths: ['/w/app'] } },
    ]);
    expect(h.state().result?.ok).toBe(true);
  });

  it('all checked -> both installed, nothing declined', async () => {
    const h = harness({
      codegraph_parser_offers: report([PS, PROTO]),
      codegraph_parsers_install: { ok: true, message: 'ok', installed: ['powershell', 'proto'], failed: [], log_path: '', log_tail: '' },
    });
    await h.ctl.check(['/w/app']);
    await h.ctl.install();
    expect(h.calls.slice(1)).toEqual([
      { cmd: 'codegraph_parsers_install', args: { parsers: ['powershell', 'proto'] } },
    ]);
  });

  it('re-checking keeps offer order, not click order', async () => {
    const h = harness({ codegraph_parser_offers: report([PS, PROTO]) });
    await h.ctl.check(['/w/app']);
    h.ctl.toggle('powershell');
    h.ctl.toggle('powershell');
    expect(h.state().selected).toEqual(['powershell', 'proto']);
  });

  it('nothing checked -> Install sends nothing', async () => {
    const h = harness({ codegraph_parser_offers: report([PS]) });
    await h.ctl.check(['/w/app']);
    h.ctl.toggle('powershell');
    await h.ctl.install();
    expect(h.calls.map((c) => c.cmd)).toEqual(['codegraph_parser_offers']);
  });

  it('a failed install is loud: the error stays until closed', async () => {
    const h = harness({
      codegraph_parser_offers: report([PS]),
      codegraph_parsers_install: new Error('pip exited 1'),
    });
    await h.ctl.check(['/w/app']);
    await h.ctl.install();
    expect(h.state().open).toBe(true);
    expect(h.state().error).toContain('pip exited 1');
    h.ctl.close();
    expect(h.state().open).toBe(false);
  });

  it('a reported failure (ok=false) is kept as the result', async () => {
    const failed = { ok: false, message: 'Installing the parsers failed: pip exited 1.', installed: [], failed: [{ id: 'x', error: 'no network' }], log_path: '/l', log_tail: 'no network' };
    const h = harness({ codegraph_parser_offers: report([PS]), codegraph_parsers_install: failed });
    await h.ctl.check(['/w/app']);
    await h.ctl.install();
    expect(h.state().result).toEqual(failed);
  });
});

describe('parser offers — Not now', () => {
  it('installs nothing and declines every offered parser for these projects', async () => {
    const h = harness({ codegraph_parser_offers: report([PS, PROTO]), codegraph_parsers_decline: null });
    await h.ctl.check(['/w/app']);
    await h.ctl.notNow();
    expect(h.calls.slice(1)).toEqual([
      { cmd: 'codegraph_parsers_decline', args: { parsers: ['powershell', 'proto'], projectPaths: ['/w/app'] } },
    ]);
    expect(h.state().open).toBe(false);
  });
});

describe('copy', () => {
  it('explains what parsers are for, naming the languages', () => {
    expect(explanation([PS, PROTO])).toContain(
      'Parsers let VCO read your PowerShell and Protocol Buffers code structurally, so code search finds the right functions and classes.',
    );
  });
  it('joins names', () => {
    expect(joinNames(['A'])).toBe('A');
    expect(joinNames(['A', 'B', 'C'])).toBe('A, B and C');
    expect(projectPaths(['', '/w/a', '/w/a', null])).toEqual(['/w/a']);
  });

  it('keeps only local absolute folders (the "local" claim is checked, not assumed)', () => {
    // POSIX and Windows drive paths are local; UNC shares, URLs and
    // relative paths are dropped before anything is scanned.
    expect(
      projectPaths([
        '/w/app',
        'C:\\Users\\dev\\app',
        'D:/data/proj',
        '\\\\fileserver\\share\\proj',
        'file:///w/app',
        'relative/app',
        '',
        null,
      ]),
    ).toEqual(['/w/app', 'C:\\Users\\dev\\app', 'D:/data/proj']);
  });
});

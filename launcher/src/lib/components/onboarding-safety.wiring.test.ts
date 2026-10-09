// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// Wiring guard for the install-confirm safety panel (v0.2.102, W5-B C7).
//
// THE DEFECT IT PINS: `preflight_install_safety_check` was a registered
// command with NO caller — the owner-deferred "safety panel" the census
// tracked. The wizard's Install button opened a confirm modal that said
// only "Continue?" and the install's own refusal landed mid-flight.
//
// WHAT THIS PROVES (and what it cannot): that the wizard asks the backend
// when the modal opens, renders the report's findings, and disables the
// modal's Install action while a refusal is present. It cannot prove a
// click reaches the handler in a real DOM (no component runner — same
// caveat as every wiring test in this repo). The refusal-shape detection
// itself is pinned in install-safety-panel.test.ts.

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { parse } from 'svelte/compiler';
import { walk, isIdent } from '../test-support/wiring-ast';

type Node = Record<string, unknown>;

const here = dirname(fileURLToPath(import.meta.url));
const WIZARD = parse(
  readFileSync(resolve(here, 'OnboardingWizard.svelte'), 'utf8'),
  { modern: true },
) as unknown as Node;

/** The subtree of a named function declaration in the instance script. */
function fnBody(name: string): Node {
  const fn = [...walk((WIZARD.instance as Node).content)].find(
    (n) => n.type === 'FunctionDeclaration' && isIdent(n.id, name),
  );
  expect(fn, `no function declaration named ${name}`).toBeTruthy();
  return fn!;
}

describe('OnboardingWizard — install-confirm safety panel (W5-B C7)', () => {
  it('parses (guards against vacuously-passing assertions)', () => {
    expect([...walk(WIZARD)].length).toBeGreaterThan(100);
  });

  it('ASKS the backend: loadSafetyReport invokes preflight_install_safety_check', () => {
    const invokes = [...walk(fnBody('loadSafetyReport'))].filter(
      (n) =>
        n.type === 'CallExpression' &&
        isIdent(n.callee, 'invoke') &&
        (n.arguments as Node[])[0]?.type === 'Literal' &&
        ((n.arguments as Node[])[0] as Node).value === 'preflight_install_safety_check',
    );
    expect(invokes.length, 'loadSafetyReport never invokes the command').toBe(1);
    // The check targets the path the install will use.
    expect(
      [...walk(invokes[0])].some((n) => isIdent(n, 'installPath')),
      'the check must pass installPath',
    ).toBe(true);
  });

  it('opens the modal AND asks, in one requestInstall (reachable component)', () => {
    const fn = fnBody('requestInstall');
    const opens = [...walk(fn)].some(
      (n) => n.type === 'AssignmentExpression' && isIdent(n.left, 'showInstallConfirm'),
    );
    expect(opens, 'requestInstall does not open the confirm modal').toBe(true);
    const asks = [...walk(fn)].some(
      (n) => n.type === 'CallExpression' && isIdent(n.callee, 'loadSafetyReport'),
    );
    expect(asks, 'requestInstall never runs the safety check').toBe(true);
  });

  /** Buttons whose onclick is the bare handler reference (`onclick={fn}`). */
  function buttonsWithHandler(handler: string): Node[] {
    return [...walk(WIZARD.fragment)].filter((n) => {
      if (n.type !== 'RegularElement' || n.name !== 'button') return false;
      const attrs = (n.attributes as Node[]).filter(
        (a) => a.type === 'Attribute' && a.name === 'onclick',
      );
      return attrs.some((a) => {
        const expr = a.value;
        // attribute value: an array with one ExpressionTag, or the
        // expression itself depending on shape.
        const nodes = Array.isArray(expr) ? expr : [expr];
        return nodes.some((v) => {
          const e = (v as Node)?.expression ?? v;
          return e && (e as Node).type === 'Identifier' && isIdent(e, handler);
        });
      });
    });
  }

  it('BLOCKS Continue on a refusal: the modal Install button reads safetyBlock', () => {
    // The confirm modal's primary button — the one whose onclick is
    // confirmInstall — must carry `safetyBlock` in its disabled state.
    const buttons = buttonsWithHandler('confirmInstall');
    expect(buttons.length, 'no button whose onclick is confirmInstall').toBe(1);
    const disabled = [...walk(buttons[0])].some((n) => isIdent(n, 'safetyBlock'));
    expect(disabled, 'the Install button is not disabled by a refusal').toBe(true);
  });

  it('renders the findings: warnings, the refusal panel, and the report state', () => {
    const calls = (name: string) =>
      [...walk(WIZARD.fragment)].filter(
        (n) => n.type === 'CallExpression' && isIdent(n.callee, name),
      );
    expect(calls('safetyWarnings').length, 'the panel never renders the warning lines').toBeGreaterThan(0);
    expect(calls('createsCollections').length, 'the panel never names new collections').toBeGreaterThan(0);
    // The refusal is rendered as a blocking panel inside the modal.
    expect([...walk(WIZARD.fragment)].some((n) => isIdent(n, 'safetyBlock')), 'the refusal value never reaches the markup').toBe(true);
  });

  it('the modal stays cancellable under a refusal (Cancel arm is never safety-gated)', () => {
    const cancels = buttonsWithHandler('cancelInstallConfirm');
    expect(cancels.length).toBeGreaterThan(0);
    for (const btn of cancels) {
      const gated = [...walk(btn)].some((n) => isIdent(n, 'safetyBlock'));
      expect(gated, 'a Cancel button is disabled by the safety state').toBe(false);
    }
  });
});

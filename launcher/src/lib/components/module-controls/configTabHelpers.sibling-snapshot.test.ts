// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 (P2-B8) — tests for the `{{control:<id>}}` sibling snapshot.
//
// The bug: `text_input`, `number_input`, `date_picker` and `file_picker`
// persist straight to `module_settings` and never report the new value
// back to `ModuleConfigTab`, whose `values` map is loaded on mount and
// never refreshed. Including them in the snapshot therefore shipped the
// MOUNT-TIME value to the dispatcher, which prefers the snapshot over its
// own (fresh) `db.get_setting` fallback. A user who edited the RL retrain
// date and clicked retrain in the same session silently sent the pre-edit
// date, with no error surfaced.
//
// The first-party impact is exercised against the REAL shipped manifest
// fixture rather than a hand-rolled one, because that is where the
// `{{control:...}}` reference actually lives.

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { snapshotSiblingValues } from './configTabHelpers';
import type { ConfigControl, ConfigSection } from '$lib/types/manifest';

const HERE = dirname(fileURLToPath(import.meta.url));
const RL_MANIFEST = resolve(
  HERE,
  '../../../../src-tauri/vct-launcher-core/tests/fixtures/manifests/vct-rl-reranker.v0.2.7.json',
);

const ckey = (sectionIdx: number, controlId: string) => `${sectionIdx}:${controlId}`;

function rlSections(): ConfigSection[] {
  const manifest = JSON.parse(readFileSync(RL_MANIFEST, 'utf-8'));
  return manifest.gui.config_tab.sections as ConfigSection[];
}

/** Every control id in the manifest, keyed the way the renderer keys it. */
function valuesForEveryControl(
  sections: ConfigSection[],
  value: unknown,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  sections.forEach((section, i) => {
    for (const control of section.controls) {
      const id = (control as { id?: string }).id;
      if (id) out[ckey(i, id)] = value;
    }
  });
  return out;
}

describe('snapshotSiblingValues — real RL manifest', () => {
  it('omits the date_picker the retrain action references', () => {
    const sections = rlSections();
    // Sanity: the fixture really does declare it and really does
    // reference it — if the manifest changes, this test should say so
    // rather than silently pass on a shape that no longer exists.
    const raw = readFileSync(RL_MANIFEST, 'utf-8');
    expect(raw).toContain('"rl_training_earliest_date"');
    expect(raw).toContain('{{control:rl_training_earliest_date}}');

    const snapshot = snapshotSiblingValues(
      sections,
      valuesForEveryControl(sections, '2026-01-01'), // the MOUNT-TIME date
      ckey,
    );

    // Pre-fix this key was present carrying the stale date, and the
    // dispatcher preferred it over the DB. Absent ⇒ the resolver falls
    // through to `db.get_setting`, which the control already updated.
    expect(snapshot).not.toHaveProperty('rl_training_earliest_date');
  });

  it('keeps the multi_select the same action references (leave-alone)', () => {
    const sections = rlSections();
    const snapshot = snapshotSiblingValues(
      sections,
      valuesForEveryControl(sections, ['proj-a', 'proj-b']),
      ckey,
    );
    // `multi_select` state is owned by the TAB (it loads the options and
    // holds the selection), so its snapshot entry is the fresh one. The
    // fix must not over-reach and drop it.
    expect(snapshot).toHaveProperty('rl_global_training_source_projects');
    expect(snapshot.rl_global_training_source_projects).toEqual([
      'proj-a',
      'proj-b',
    ]);
  });

  it('keeps checkboxes and drops buttons / info_dynamic', () => {
    const sections = rlSections();
    const snapshot = snapshotSiblingValues(
      sections,
      valuesForEveryControl(sections, true),
      ckey,
    );
    expect(snapshot).toHaveProperty('rl_use_global');
    expect(snapshot).toHaveProperty('rl_online_training_disabled');
    expect(snapshot).not.toHaveProperty('rl_reset_to_global'); // button
    expect(snapshot).not.toHaveProperty('weights_version_live'); // info_dynamic
  });
});

describe('snapshotSiblingValues — all four self-persisting kinds', () => {
  const control = (kind: string, id: string) =>
    ({ kind, id, label: id } as unknown as ConfigControl);

  const sections: ConfigSection[] = [
    {
      title: 'Self-persisting',
      controls: [
        control('text_input', 'a_text'),
        control('number_input', 'a_number'),
        control('date_picker', 'a_date'),
        control('file_picker', 'a_file'),
        control('checkbox', 'a_checkbox'),
      ],
    } as unknown as ConfigSection,
  ];

  it('drops every kind that writes its own value to module_settings', () => {
    const values = {
      '0:a_text': 'stale',
      '0:a_number': 1,
      '0:a_date': '1999-01-01',
      '0:a_file': '/stale/path',
      '0:a_checkbox': true,
    };
    const snapshot = snapshotSiblingValues(sections, values, ckey);
    expect(Object.keys(snapshot)).toEqual(['a_checkbox']);
  });
});

describe('snapshotSiblingValues — unchanged contract', () => {
  it('omits controls with no persisted value rather than emitting undefined', () => {
    const sections = [
      {
        title: 'S',
        controls: [{ kind: 'checkbox', id: 'unset', label: 'Unset' }],
      },
    ] as unknown as ConfigSection[];
    expect(snapshotSiblingValues(sections, {}, ckey)).toEqual({});
  });

  it('lets the later section win on a duplicate control id', () => {
    const sections = [
      { title: 'A', controls: [{ kind: 'checkbox', id: 'dupe', label: 'A' }] },
      { title: 'B', controls: [{ kind: 'checkbox', id: 'dupe', label: 'B' }] },
    ] as unknown as ConfigSection[];
    const snapshot = snapshotSiblingValues(
      sections,
      { '0:dupe': 'first', '1:dupe': 'second' },
      ckey,
    );
    expect(snapshot.dupe).toBe('second');
  });
});

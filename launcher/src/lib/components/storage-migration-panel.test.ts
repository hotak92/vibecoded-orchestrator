// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

import { describe, it, expect } from 'vitest';
import {
  migrationArgs,
  validationError,
  modalCopy,
  CANONICAL_VOLUMES,
} from './storage-migration-panel';

describe('validationError (W5-B storage migration)', () => {
  it('requires a volume and an absolute path in both directions', () => {
    expect(validationError('to_bind', 'weaviate_data', '')).toContain('copy INTO');
    expect(validationError('to_named', 'weaviate_data', '')).toContain('copy FROM');
    expect(validationError('to_bind', '', '/mnt/x')).toBe('Pick a volume.');
    expect(validationError('to_bind', 'weaviate_data', 'relative/path')).toContain('absolute');
    expect(validationError('to_named', 'ollama_data', '  /mnt/big  ')).toBeNull();
    // Windows absolute paths are accepted too.
    expect(validationError('to_bind', 'weaviate_data', 'D:\\vco\\volumes')).toBeNull();
  });
});

describe('migrationArgs (W5-B storage migration)', () => {
  it('builds the right payload per direction, trimmed; null when invalid', () => {
    expect(migrationArgs('to_named', ' weaviate_data ', ' /mnt/src ')).toEqual({
      sourceBindPath: '/mnt/src',
      targetNamedVolume: 'weaviate_data',
    });
    expect(migrationArgs('to_bind', 'ollama_data', '/mnt/dst')).toEqual({
      sourceNamedVolume: 'ollama_data',
      targetBindPath: '/mnt/dst',
    });
    expect(migrationArgs('to_bind', 'ollama_data', 'nope')).toBeNull();
  });
});

describe('modalCopy (W5-B storage migration)', () => {
  it('states what moves where in BOTH directions', () => {
    const toBind = modalCopy('to_bind', 'weaviate_data', '/mnt/big');
    expect(toBind.body).toContain('named volume weaviate_data');
    expect(toBind.body).toContain('/mnt/big');

    const toNamed = modalCopy('to_named', 'ollama_data', '/mnt/src');
    expect(toNamed.body).toContain('/mnt/src');
    expect(toNamed.body).toContain('named volume ollama_data');
  });

  it('states that NOTHING is deleted (the destructive-adjacent promise)', () => {
    for (const dir of ['to_bind', 'to_named'] as const) {
      const copy = modalCopy(dir, 'weaviate_data', '/mnt/x');
      expect(copy.body).toContain('Nothing is deleted');
      expect(copy.body.toLowerCase()).toContain('copy');
    }
  });

  it('names the canonical volumes the UI offers', () => {
    expect([...CANONICAL_VOLUMES]).toEqual(
      expect.arrayContaining(['weaviate_data', 'ollama_data', 'code_embed_cache']),
    );
  });
});

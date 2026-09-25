// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.97 (lane Y) — the OnboardingWizard's third-party-service decision:
// owner ruling Q1 (Ollama adopted unattended, Weaviate asked) and the EXACT
// `--service` argv install.py receives for both answers.

import { describe, expect, it } from 'vitest';
import type { DetectedCandidate, DetectionReport } from '$lib/api/service_endpoints';
import {
  serviceChoiceArgv,
  wizardServiceChoiceValues,
  wizardServiceQuestions,
} from './onboarding-service-choice';

function cand(over: Partial<DetectedCandidate>): DetectedCandidate {
  return {
    service: 'weaviate',
    url: 'http://localhost:8080',
    host: 'localhost',
    port: 8080,
    live: true,
    has_vco_data: false,
    compatible: true,
    ownership: 'third_party',
    ...over,
  };
}

function report(over: Partial<DetectionReport['candidates']>): DetectionReport {
  return { schema: 1, candidates: over };
}

describe('wizardServiceQuestions (ruling Q1)', () => {
  it('asks about a third-party Weaviate with no VCO data, by container name', () => {
    const q = wizardServiceQuestions(
      report({
        weaviate: [cand({ url: 'http://localhost:8080', container: { name: 'their_weaviate' } })],
      }),
    );
    expect(q.weaviate).toEqual({
      service: 'weaviate',
      url: 'http://localhost:8080',
      containerName: 'their_weaviate',
      adoptValue: 'adopt:container:their_weaviate',
    });
    expect(q.ollama).toBeNull();
  });

  it('adopts a third-party Ollama without asking (info line)', () => {
    const q = wizardServiceQuestions(
      report({ ollama: [cand({ service: 'ollama', url: 'http://ollama.lan:11434', host: 'ollama.lan', port: 11434 })] }),
    );
    expect(q.ollama).toMatchObject({ url: 'http://ollama.lan:11434', adoptValue: 'adopt:url:http://ollama.lan:11434' });
    expect(q.weaviate).toBeNull();
  });

  it('does not ask about VCO-owned instances, VCO data, incompatible or dead endpoints', () => {
    const detection = report({
      weaviate: [
        cand({ ownership: 'installer' }),
        cand({ has_vco_data: true }),
        cand({ compatible: false }),
        cand({ live: false }),
      ],
      ollama: [
        cand({ service: 'ollama', ownership: 'installer' }),
        cand({ service: 'ollama', has_vco_data: true }),
      ],
    });
    expect(wizardServiceQuestions(detection)).toEqual({ weaviate: null, ollama: null });
  });

  it('asks about an unknown-ownership Weaviate but never auto-adopts an unknown Ollama', () => {
    const detection = report({
      weaviate: [cand({ ownership: 'unknown' })],
      ollama: [cand({ service: 'ollama', ownership: 'unknown' })],
    });
    const q = wizardServiceQuestions(detection);
    expect(q.weaviate).not.toBeNull();
    expect(q.ollama).toBeNull();
  });

  it('asks nothing when detection failed (install.py runs its own flow)', () => {
    expect(wizardServiceQuestions({ error: 'podman not found' })).toEqual({ weaviate: null, ollama: null });
    expect(wizardServiceQuestions(null)).toEqual({ weaviate: null, ollama: null });
  });
});

describe('the exact --service argv (both choices)', () => {
  const questions = wizardServiceQuestions(
    report({
      weaviate: [cand({ container: { name: 'their_weaviate' } })],
      ollama: [cand({ service: 'ollama', url: 'http://ollama.lan:11434', host: 'ollama.lan', port: 11434 })],
    }),
  );

  it("'Use this instance' adopts both endpoints", () => {
    expect(serviceChoiceArgv(wizardServiceChoiceValues(questions, 'use_this'))).toEqual([
      '--service',
      'weaviate=adopt:container:their_weaviate',
      '--service',
      'ollama=adopt:url:http://ollama.lan:11434',
    ]);
  });

  it("'Run VCO's own copy' still adopts the Ollama (ruling Q1)", () => {
    expect(serviceChoiceArgv(wizardServiceChoiceValues(questions, 'vco_copy'))).toEqual([
      '--service',
      'weaviate=vco',
      '--service',
      'ollama=adopt:url:http://ollama.lan:11434',
    ]);
  });

  it('a URL-shaped Weaviate becomes adopt:url:<url>', () => {
    const q = wizardServiceQuestions(
      report({ weaviate: [cand({ url: 'http://weaviate.lan:8080', host: 'weaviate.lan', port: 8080 })] }),
    );
    expect(serviceChoiceArgv(wizardServiceChoiceValues(q, 'use_this'))).toEqual([
      '--service',
      'weaviate=adopt:url:http://weaviate.lan:8080',
    ]);
  });

  it('no questions means no flags at all', () => {
    expect(wizardServiceChoiceValues({ weaviate: null, ollama: null }, 'use_this')).toEqual([]);
    expect(serviceChoiceArgv([])).toEqual([]);
  });
});

// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.97 (lane Y): the OnboardingWizard's "found a third-party service"
// decision, as pure functions so vitest can pin the EXACT `--service` argv
// the install receives (the wizard markup is wiring only).
//
// Owner ruling Q1 (v0.2.97 plan §10): a third-party Ollama is adopted
// WITHOUT asking — shown as an info line; a third-party Weaviate holding no
// VCO data is never adopted silently — the wizard asks "use this instance
// or run VCO's own copy" and passes the answer to install.py through the
// `--service <svc>=<choice>` flags (grammar validated Rust-side by
// `commands::installer::service_choice_args`, Python-side by
// `vco_lib.service_reconcile.parse_service_flag`).
//
// code_embed is never asked about and never adopted: it is always VCO's own
// (`vco[:<port>]` only) — plan invariant: the code-embed cache is VCO's.

import type { CoreServiceName, DetectedCandidate, DetectionReport } from '$lib/api/service_endpoints';

/** The user's answer to the Weaviate question (null until answered). */
export type WeaviateChoice = 'use_this' | 'vco_copy';

export interface WizardServiceQuestion {
  service: CoreServiceName;
  url: string;
  containerName: string | null;
  /** The `--service` choice half for adopting this endpoint. */
  adoptValue: string;
}

export interface WizardServiceQuestions {
  weaviate: WizardServiceQuestion | null;
  ollama: WizardServiceQuestion | null;
}

export const SERVICE_FLAG = '--service';

/** `adopt:container:<name>` for a container candidate, else `adopt:url:<url>`. */
function adoptValueOf(c: DetectedCandidate): string {
  const name = c.container?.name;
  return name ? `adopt:container:${name}` : `adopt:url:${c.url}`;
}

function toQuestion(service: CoreServiceName, c: DetectedCandidate): WizardServiceQuestion {
  return {
    service,
    url: c.url,
    containerName: c.container?.name ?? null,
    adoptValue: adoptValueOf(c),
  };
}

/**
 * The first third-party, live, compatible, VCO-data-free candidate for
 * `service` — the one the wizard asks about / adopts. VCO's own instances
 * (`installer` / `legacy_vco` ownership) and endpoints holding VCO data are
 * not adoption questions: the install reuses its own by default.
 *
 * `includeUnknown`: an `unknown`-ownership Weaviate is still asked about
 * (a question too many is cheap); an `unknown`-ownership Ollama is NOT
 * auto-adopted, because adopting means VCO pulls models into it — never
 * silently for something we could not identify.
 */
function thirdPartyCandidate(
  detection: DetectionReport | null,
  service: CoreServiceName,
  includeUnknown: boolean,
): DetectedCandidate | null {
  if (detection?.error) return null;
  const list = detection?.candidates?.[service] ?? [];
  return (
    list.find(
      (d) =>
        d.live !== false &&
        d.compatible !== false &&
        d.has_vco_data === false &&
        (d.ownership === 'third_party' || (includeUnknown && (d.ownership ?? 'unknown') === 'unknown')),
    ) ?? null
  );
}

/** The questions the wizard shows from a detector reply. Pure. */
export function wizardServiceQuestions(detection: DetectionReport | null): WizardServiceQuestions {
  const weaviate = thirdPartyCandidate(detection, 'weaviate', true);
  const ollama = thirdPartyCandidate(detection, 'ollama', false);
  return {
    weaviate: weaviate ? toQuestion('weaviate', weaviate) : null,
    ollama: ollama ? toQuestion('ollama', ollama) : null,
  };
}

/**
 * The `SVC=CHOICE` values for `InstallConfig.service_choices`, in argv
 * order. The Ollama flag is always present when its question exists (adopted
 * unattended, ruling Q1); the Weaviate flag follows the user's answer —
 * `null` while unanswered is the wizard's signal to gate the Install button,
 * and this function must not be called that way.
 */
export function wizardServiceChoiceValues(
  questions: WizardServiceQuestions,
  weaviateChoice: WeaviateChoice,
): string[] {
  const out: string[] = [];
  if (questions.weaviate) {
    out.push(weaviateChoice === 'use_this' ? `weaviate=${questions.weaviate.adoptValue}` : 'weaviate=vco');
  }
  if (questions.ollama) out.push(`ollama=${questions.ollama.adoptValue}`);
  return out;
}

/** The exact argv install.py sees: `--service <value>` per choice. */
export function serviceChoiceArgv(values: readonly string[]): string[] {
  return values.flatMap((v) => [SERVICE_FLAG, v]);
}

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.102 (W5-B C7): the view model for the OnboardingWizard's install
// confirmation panel, fed by `preflight_install_safety_check`.
//
// The command returns the backend's read-only `SafetyReport`. This module
// turns it into the panel's lines and, critically, into the one decision
// the wizard needs: does the report contain a REFUSAL — a condition the
// install's own steps will refuse on — so Continue can be blocked BEFORE
// the user clicks Install instead of the install dying mid-flight.
//
// Refusal detection reuses `runtimeOwnerRefusal` (the
// onboarding-runtime-refusal pattern): the volumes step's refusal text is
// the one shape the install refuses on, and the report carries it verbatim
// as a risk line. Every other risk is a warning the user reads, not a
// block.

import { runtimeOwnerRefusal } from './onboarding-runtime-refusal';

/** Mirrors `commands::installer::SafetyReport` (camelCase via serde rename
 *  where applicable — the report serializes snake_case today, so the FE
 *  type keeps the backend's field names verbatim). */
export interface SafetyReport {
  will_overwrite_orchestrator_files: string[];
  will_preserve_user_code: string[];
  existing_volumes: { name: string }[];
  existing_collections: string[];
  new_collections_to_add: string[];
  services_running: string[];
  services_to_start: string[];
  risks: string[];
}

/** The refusal line in `report`, or null when the report has none.
 *  A refusal BLOCKS the confirm modal's Install action. */
export function safetyRefusal(report: SafetyReport | null | undefined): string | null {
  if (!report) return null;
  for (const risk of report.risks) {
    const refusal = runtimeOwnerRefusal(risk);
    if (refusal !== null) return refusal;
  }
  return null;
}

/** The risk lines the panel renders as WARNINGS (everything that is not
 *  the refusal — the refusal gets its own blocking treatment). */
export function safetyWarnings(report: SafetyReport | null | undefined): string[] {
  if (!report) return [];
  const refusal = safetyRefusal(report);
  return report.risks.filter((r) => r !== refusal);
}

/** True when the report says the install will create new Weaviate classes —
 *  the panel calls this out because "new collections will appear" is the
 *  kind of side effect a confirm step should name. */
export function createsCollections(report: SafetyReport | null | undefined): boolean {
  return !!report && report.new_collections_to_add.length > 0;
}

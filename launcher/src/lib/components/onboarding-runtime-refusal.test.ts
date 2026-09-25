// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
import { describe, expect, it } from 'vitest';
import { runtimeOwnerRefusal } from './onboarding-runtime-refusal';

// The literal shape `wrong_runtime_owner_refusal` produces (vct-launcher-core).
const REFUSAL =
  'refusing to adopt the orchestrator volume(s) `weaviate_data`: it exists only under docker, ' +
  'but podman was auto-detected (podman is preferred when both respond). podman and docker keep ' +
  'SEPARATE volumes and containers, so doing this with podman would act on a copy that does not ' +
  'hold your data. To fix: quit the launcher, set VCT_CONTAINER_RUNTIME=docker in the environment ' +
  'the launcher starts from — your login session, or the shell you start it from — then relaunch it, ' +
  'so it runs under docker, which owns the data.';

describe('runtimeOwnerRefusal (R7b F8)', () => {
  it('recognises the ownership refusal, as a string or an Error', () => {
    expect(runtimeOwnerRefusal(REFUSAL)).toBe(REFUSAL);
    expect(runtimeOwnerRefusal(new Error(REFUSAL))).toBe(REFUSAL);
    expect(runtimeOwnerRefusal(`Error: ${REFUSAL}`)).toBe(REFUSAL);
  });

  it('leaves every other error to the generic line', () => {
    expect(runtimeOwnerRefusal('Pick a custom volumes folder or switch back to Default.')).toBeNull();
    expect(runtimeOwnerRefusal('refusing to overwrite /x: not empty')).toBeNull();
    expect(runtimeOwnerRefusal(null)).toBeNull();
  });
});

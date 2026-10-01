<!--
  v0.2.100 (W5R-02 / W5R-09): the "RL-Scored Retrieval" toggle of the MCP
  dashboard, extracted so its rendered DOM state can be unit-tested (vitest
  renders it through `svelte/server`; see RlScoringSwitch.render.test.ts).

  Pure presentation: it renders exactly the `RlScoringSwitchView` it is given
  (`$lib/rl-scoring-default`) and reports clicks. It never decides anything —
  in particular it never renders `checked` while the RL scoring lock holds,
  because the view it receives is unchecked in that state.

  The toggle styles mirror McpDashboard's (Svelte scopes CSS per component).
-->
<script lang="ts">
  import type { RlScoringSwitchView } from '$lib/rl-scoring-default';

  let {
    view,
    onToggle,
  }: { view: RlScoringSwitchView | null; onToggle: (checked: boolean) => void } = $props();
</script>

<label class="toggle-switch">
  <input
    type="checkbox"
    checked={view?.checked ?? false}
    disabled={view?.disabled ?? true}
    data-testid="rl-scoring-switch"
    onchange={(e) => onToggle((e.target as HTMLInputElement).checked)}
  />
  <span class="toggle-slider"></span>
</label>

<style>
  .toggle-switch { position: relative; display: inline-block; width: 44px; height: 24px; flex-shrink: 0; }
  .toggle-switch input { opacity: 0; width: 0; height: 0; }
  .toggle-slider {
    position: absolute; inset: 0;
    background: var(--color-muted);
    border-radius: 24px;
    cursor: pointer;
    transition: background 0.3s;
  }
  .toggle-slider::before {
    content: '';
    position: absolute;
    height: 18px; width: 18px;
    left: 3px; bottom: 3px;
    background: white;
    border-radius: 50%;
    transition: transform 0.3s;
  }
  .toggle-switch input:checked + .toggle-slider { background: var(--color-teal); }
  .toggle-switch input:checked + .toggle-slider::before { transform: translateX(20px); }
  .toggle-switch input:disabled + .toggle-slider { opacity: 0.4; cursor: not-allowed; }
</style>

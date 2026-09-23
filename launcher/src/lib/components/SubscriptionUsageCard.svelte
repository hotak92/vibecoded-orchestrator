<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!--
  v0.2.97 — subscription usage on the home page: bars + reset countdowns per
  vendor, from the model gateway's cache (`GET /usage/windows`, via
  `model_gateway_usage_windows`). Every presentation decision lives in
  `$lib/subscription-usage.ts` (vitest-covered); this file is markup, a poll
  and a clock. Renders nothing on a machine where the gateway is not running.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import {
    barTone,
    barWidth,
    cardVisible,
    describeCountdown,
    describeTokens,
    fetchUsage,
    nextPollMs,
    unknownLabel,
    vendorStatus,
    visibleVendors,
    type UsageBridgeResult,
  } from '$lib/subscription-usage';

  let result = $state<UsageBridgeResult | null>(null);
  let now = $state(Date.now());

  onMount(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const poll = async () => {
      const next = await fetchUsage();
      if (stopped) return;
      result = next;
      now = Date.now();
      timer = setTimeout(poll, nextPollMs(next));
    };
    void poll();
    // Countdowns move between polls.
    const clock = setInterval(() => (now = Date.now()), 30_000);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      clearInterval(clock);
    };
  });
</script>

{#if result && cardVisible(result)}
  <section class="usage-card glass-card" aria-label="Subscription usage">
    <div class="usage-head">
      <h2 class="usage-title">Subscription usage</h2>
      <span class="usage-tag">via model gateway</span>
    </div>

    {#if !result.ok}
      <p class="usage-problem">{result.message}</p>
    {:else}
      {@const vendors = visibleVendors(result.snapshot)}
      {#if vendors.length === 0}
        <p class="usage-muted">No subscription is configured on the gateway.</p>
      {/if}
      <div class="usage-vendors">
        {#each vendors as vendor (vendor.id)}
          <div class="usage-vendor">
            <div class="usage-vendor-head">
              <span class="usage-vendor-name">{vendor.name}</span>
              {#if vendor.plan}<span class="usage-plan">{vendor.plan}</span>{/if}
              {#if vendorStatus(vendor)}
                <span class="usage-muted usage-status">{vendorStatus(vendor)}</span>
              {/if}
            </div>
            {#each vendor.windows as w (w.id)}
              <div class="usage-row">
                <span class="usage-label">{w.label}</span>
                {#if w.percent !== null}
                  <div class="usage-track" role="progressbar" aria-label="{vendor.label} {w.label}"
                       aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(w.percent)}>
                    <div class="usage-fill tone-{barTone(w.percent)}" style:width={barWidth(w.percent)}></div>
                  </div>
                  <span class="usage-pct">{Math.round(w.percent)}%</span>
                  <span class="usage-reset">{describeCountdown(w.resets_at, now)}</span>
                {:else}
                  <span class="usage-unknown">{unknownLabel(w.unknown_reason)}</span>
                {/if}
              </div>
            {/each}
            {#if vendor.tokens}
              <div class="usage-row">
                <span class="usage-label">mo</span>
                <span class="usage-tokens">{describeTokens(vendor.tokens)}</span>
                <span class="usage-reset">no quota endpoint — tokens, not a %</span>
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  </section>
{/if}

<style>
  .usage-card {
    padding: 18px 20px;
    margin-bottom: 20px;
    border-radius: 20px;
  }
  .usage-head {
    display: flex;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 12px;
  }
  .usage-title {
    font-size: 15px;
    font-weight: 700;
    color: var(--color-text, #f1f5f9);
    margin: 0;
  }
  .usage-tag {
    font-size: 11px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--color-teal, #00bfa6);
  }
  .usage-vendors {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 16px;
  }
  .usage-vendor-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-bottom: 6px;
  }
  .usage-vendor-name {
    font-weight: 600;
    color: var(--color-text, #f1f5f9);
  }
  .usage-plan {
    font-size: 11px;
    padding: 1px 8px;
    border-radius: 999px;
    border: 1px solid rgba(123, 95, 255, 0.4);
    color: var(--color-purple, #7b5fff);
  }
  .usage-row {
    display: grid;
    grid-template-columns: 44px 1fr 40px;
    align-items: center;
    column-gap: 10px;
    row-gap: 2px;
    font-size: 12px;
    margin: 4px 0;
  }
  .usage-label {
    color: var(--color-mid, #94a3b8);
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .usage-track {
    height: 5px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.06);
    overflow: hidden;
  }
  .usage-fill {
    height: 100%;
    border-radius: 999px;
    transition: width 0.2s ease;
  }
  .tone-teal {
    background: linear-gradient(90deg, rgba(0, 191, 166, 0.8), #00bfa6);
  }
  .tone-purple {
    background: linear-gradient(90deg, rgba(123, 95, 255, 0.8), #7b5fff);
  }
  .tone-pink {
    background: linear-gradient(90deg, rgba(255, 79, 160, 0.8), #ff4fa0);
  }
  .usage-pct {
    font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
    color: var(--color-text, #f1f5f9);
    text-align: right;
  }
  .usage-reset {
    grid-column: 2 / 4;
    font-size: 11px;
    color: var(--color-muted, #475569);
  }
  .usage-unknown,
  .usage-tokens {
    grid-column: 2 / 4;
    color: var(--color-mid, #94a3b8);
  }
  .usage-tokens {
    font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  }
  .usage-muted {
    color: var(--color-muted, #475569);
    font-size: 12px;
  }
  .usage-status {
    margin-left: auto;
  }
  .usage-problem {
    color: var(--color-pink, #ff4fa0);
    font-size: 12px;
    margin: 0;
  }
</style>

<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  //
  // Error-notification bell. Persists error toasts (which auto-dismiss
  // per the dwell policy in stores/toast.ts) into a durable inbox the
  // user can open here, so important errors aren't missed. Each entry has
  // Copy + Trash actions; entries dedup by
  // key (with a ×N counter) and auto-clear when the same action later
  // succeeds (see stores/notifications.ts). Lives in the MenuBar.
  //
  // v0.2.102 W6-J: the inbox also renders INFO alerts with action buttons
  // (stores/notifications.ts `upsertAlert`) — e.g. the "a project's KG may
  // need a duplicate re-scan" heads-up (kg-rescan-alerts.ts), where each
  // project gets its own scan button.

  import { notifications, errorCount, type ErrorNotification, type NotificationAction } from '$lib/stores/notifications';
  import { toast } from '$lib/stores/toast';
  import { invoke } from '$lib/tauri';
  import { duplicateScanSummary, type DuplicateScanResult } from '$lib/project-state/kg-duplicates';

  // The Tauri commands a bell action may run — the literal union keeps the
  // invoke-name census (invoke-names.test.ts DYNAMIC_INVOKES) honest. The
  // only producer today is the KG re-scan alert.
  type BellActionCommand = 'kg_check_duplicates';

  let open = $state(false);
  let running = $state<Record<string, boolean>>({});
  const list = $derived($notifications);
  const count = $derived($errorCount);
  // Info alerts (W6-J) are not errors but still badge the bell — otherwise
  // nobody would see them.
  const infoCount = $derived(list.filter((n) => n.tone === 'info').length);
  const badge = $derived(count + infoCount);

  function fmtTime(ms: number): string {
    try {
      return new Date(ms).toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch {
      return '';
    }
  }

  async function copy(message: string) {
    try {
      await navigator.clipboard.writeText(message);
      toast.success('Copied to clipboard');
    } catch {
      toast.error('Copy failed');
    }
  }

  /** Run an alert's action button. On success that action leaves the entry
   *  (the last one clears the alert); a failure stays and is toasted.
   *  Buttons are keyed by the action's identity (`a.key ?? a.label`), not
   *  the display label — two projects sharing a display name must keep
   *  two distinct buttons. */
  async function runAction(n: ErrorNotification, a: NotificationAction) {
    const key = `${n.id}:${a.key ?? a.label}`;
    running = { ...running, [key]: true };
    try {
      const result = await invoke<unknown>(a.command as BellActionCommand, a.args);
      // The scan's result is kept nowhere else, so the toast carries the
      // verdict itself (pairs found, objects not compared).
      toast.success(
        a.command === 'kg_check_duplicates'
          ? duplicateScanSummary(a.label, result as DuplicateScanResult)
          : `${a.label} finished`,
      );
      notifications.dropAction(n.id, a.key ?? a.label);
    } catch (e) {
      toast.error(e);
    } finally {
      running = { ...running, [key]: false };
    }
  }
</script>

<div class="bell-wrap">
  <button
    class="bell-btn"
    class:has-errors={count > 0}
    class:has-info={count === 0 && infoCount > 0}
    onclick={() => (open = !open)}
    aria-label="Notifications{count > 0 ? ` (${count} error${count !== 1 ? 's' : ''})` : ''}{infoCount > 0 ? ` (${infoCount} notice${infoCount !== 1 ? 's' : ''})` : ''}"
    aria-expanded={open}
    title="Saved notifications"
  >
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" />
    </svg>
    {#if badge > 0}
      <span class="bell-badge" class:bell-badge-info={count === 0 && infoCount > 0}>{badge > 99 ? '99+' : badge}</span>
    {/if}
  </button>

  {#if open}
    <div class="bell-panel" role="dialog" aria-label="Saved notifications">
      <div class="bell-panel-head">
        <span class="bell-panel-title">
          Notifications{badge > 0 ? ` (${badge})` : ''}
        </span>
        {#if list.length > 0}
          <button class="bell-clear" onclick={() => notifications.clearAll()}>
            Clear all
          </button>
        {/if}
      </div>

      {#if list.length === 0}
        <p class="bell-empty">No saved errors. Toast errors are kept here so you don't miss them.</p>
      {:else}
        <ul class="bell-list">
          {#each list as n (n.id)}
            <li class="bell-item" class:bell-item-info={n.tone === 'info'}>
              <div class="bell-item-body">
                <p class="bell-item-msg" class:bell-item-msg-info={n.tone === 'info'}>{n.message}</p>
                <div class="bell-item-meta">
                  <span class="bell-item-time">{fmtTime(n.lastSeen)}</span>
                  {#if n.count > 1}
                    <span class="bell-item-count" title="Seen {n.count} times">×{n.count}</span>
                  {/if}
                </div>
                {#if n.actions && n.actions.length > 0}
                  <div class="bell-item-actionrow">
                    {#each n.actions as a (a.key ?? a.label)}
                      <button
                        class="bell-action-btn"
                        data-testid="bell-action"
                        disabled={running[`${n.id}:${a.key ?? a.label}`]}
                        onclick={() => void runAction(n, a)}
                      >{running[`${n.id}:${a.key ?? a.label}`] ? 'Running…' : a.label}</button>
                    {/each}
                  </div>
                {/if}
              </div>
              <div class="bell-item-actions">
                <button
                  class="bell-icon-btn"
                  onclick={() => copy(n.message)}
                  aria-label="Copy error message"
                  title="Copy"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                  </svg>
                </button>
                <button
                  class="bell-icon-btn bell-icon-trash"
                  onclick={() => notifications.dismiss(n.id)}
                  aria-label="Delete notification"
                  title="Delete"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M3 6h18" />
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                  </svg>
                </button>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</div>

<style>
  .bell-wrap {
    position: relative;
  }
  .bell-btn {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border-radius: 8px;
    border: 1px solid rgba(255, 255, 255, 0.1);
    background: rgba(255, 255, 255, 0.04);
    color: #9aa6c8;
    cursor: pointer;
    transition: background 0.15s ease, color 0.15s ease;
  }
  .bell-btn:hover {
    background: rgba(255, 255, 255, 0.08);
    color: #e6ecff;
  }
  .bell-btn.has-errors {
    color: #ffb454;
    border-color: rgba(255, 180, 84, 0.35);
  }
  /* W6-J: an info-only inbox badges in teal, not the error amber. */
  .bell-btn.has-info {
    color: rgb(0, 191, 166);
    border-color: rgba(0, 191, 166, 0.35);
  }
  .bell-badge-info {
    background: rgb(0, 191, 166);
    color: #06251f;
  }
  .bell-badge {
    position: absolute;
    top: -5px;
    right: -5px;
    min-width: 16px;
    height: 16px;
    padding: 0 4px;
    border-radius: 8px;
    background: #ff4d4f;
    color: #fff;
    font-size: 10px;
    font-weight: 700;
    line-height: 16px;
    text-align: center;
  }
  .bell-panel {
    position: absolute;
    top: 38px;
    right: 0;
    width: 300px;
    max-height: 360px;
    overflow-y: auto;
    background: #0d1735;
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 10px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
    z-index: 200;
  }
  .bell-panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    position: sticky;
    top: 0;
    background: #0d1735;
  }
  .bell-panel-title {
    font-size: 12px;
    font-weight: 600;
    color: #e6ecff;
  }
  .bell-clear {
    background: none;
    border: none;
    color: #6cf;
    font-size: 11px;
    cursor: pointer;
    padding: 2px 4px;
    border-radius: 4px;
  }
  .bell-clear:hover {
    background: rgba(255, 255, 255, 0.06);
  }
  .bell-empty {
    padding: 18px 14px;
    color: #7b88b0;
    font-size: 11px;
    line-height: 1.5;
    margin: 0;
  }
  .bell-list {
    list-style: none;
    margin: 0;
    padding: 4px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .bell-item {
    display: flex;
    align-items: flex-start;
    gap: 6px;
    padding: 8px;
    border-radius: 8px;
    background: rgba(255, 77, 79, 0.08);
    border: 1px solid rgba(255, 77, 79, 0.18);
  }
  /* W6-J: info alerts read as heads-ups, not failures. */
  .bell-item-info {
    background: rgba(0, 191, 166, 0.07);
    border-color: rgba(0, 191, 166, 0.22);
  }
  .bell-item-msg-info {
    color: #c8efe8;
  }
  .bell-item-actionrow {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 6px;
  }
  .bell-action-btn {
    background: rgba(0, 191, 166, 0.14);
    border: 1px solid rgba(0, 191, 166, 0.4);
    color: #7fe8d8;
    padding: 3px 10px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
  }
  .bell-action-btn:hover:not(:disabled) {
    background: rgba(0, 191, 166, 0.24);
  }
  .bell-action-btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .bell-item-body {
    flex: 1;
    min-width: 0;
  }
  .bell-item-msg {
    margin: 0;
    font-size: 11px;
    line-height: 1.45;
    color: #ffd9d9;
    word-break: break-word;
  }
  .bell-item-meta {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 3px;
  }
  .bell-item-time {
    font-size: 9px;
    color: #7b88b0;
  }
  .bell-item-count {
    font-size: 9px;
    font-weight: 700;
    color: #ffb454;
    background: rgba(255, 180, 84, 0.14);
    padding: 0 4px;
    border-radius: 6px;
  }
  .bell-item-actions {
    display: flex;
    gap: 2px;
    flex-shrink: 0;
  }
  .bell-icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border-radius: 6px;
    border: none;
    background: none;
    color: #9aa6c8;
    cursor: pointer;
    transition: background 0.15s ease, color 0.15s ease;
  }
  .bell-icon-btn:hover {
    background: rgba(255, 255, 255, 0.1);
    color: #e6ecff;
  }
  .bell-icon-trash:hover {
    color: #ff6b6b;
  }
</style>

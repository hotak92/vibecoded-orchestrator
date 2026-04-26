<script lang="ts" generics="T extends string | number">
  // Reusable custom dropdown. Replaces native <select> across the launcher
  // because Tauri's bundled WebKitGTK on Linux ignores CSS styling on the
  // OS-level dropdown popup, producing white-on-white text in dark themes.
  //
  // Behavior:
  //  - Click trigger to toggle.
  //  - Click outside closes.
  //  - Escape closes.
  //  - Arrow keys navigate; Enter/Space select.
  //  - `bind:value` matches the API of `<select bind:value>` so callers can
  //    swap one for the other with minimal churn.
  //
  // Styled to match the launcher's existing .form-input look and dark theme.

  import { onMount } from 'svelte';

  interface Option {
    value: T;
    label: string;
    disabled?: boolean;
  }

  let {
    value = $bindable(),
    options,
    placeholder = 'Select…',
    disabled = false,
    id = undefined,
    ariaLabel = undefined,
    classes = '',
    onChange = undefined,
  }: {
    value: T | undefined;
    options: Option[];
    placeholder?: string;
    disabled?: boolean;
    id?: string;
    ariaLabel?: string;
    classes?: string;
    onChange?: (v: T) => void;
  } = $props();

  let open = $state(false);
  let triggerEl: HTMLButtonElement | undefined = $state();
  let menuEl: HTMLUListElement | undefined = $state();
  let highlighted = $state(0);

  const selected = $derived(options.find((o) => o.value === value));
  const label = $derived(selected?.label ?? placeholder);

  function toggle() {
    if (disabled) return;
    open = !open;
    if (open) {
      const idx = options.findIndex((o) => o.value === value);
      highlighted = idx >= 0 ? idx : 0;
    }
  }

  function close() {
    open = false;
  }

  function pick(opt: Option) {
    if (opt.disabled) return;
    value = opt.value;
    onChange?.(opt.value);
    close();
    triggerEl?.focus();
  }

  function onKey(e: KeyboardEvent) {
    if (!open) {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
        e.preventDefault();
        toggle();
      }
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
      triggerEl?.focus();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      highlighted = Math.min(options.length - 1, highlighted + 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      highlighted = Math.max(0, highlighted - 1);
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      const opt = options[highlighted];
      if (opt) pick(opt);
    } else if (e.key === 'Tab') {
      close();
    }
  }

  function onDocClick(e: MouseEvent) {
    if (!open) return;
    const t = e.target as Node;
    if (triggerEl?.contains(t)) return;
    if (menuEl?.contains(t)) return;
    close();
  }

  onMount(() => {
    document.addEventListener('click', onDocClick, true);
    return () => document.removeEventListener('click', onDocClick, true);
  });
</script>

<div class="dropdown {classes}" class:open class:disabled>
  <button
    type="button"
    class="dropdown-trigger"
    onclick={toggle}
    onkeydown={onKey}
    aria-haspopup="listbox"
    aria-expanded={open}
    aria-label={ariaLabel}
    {id}
    {disabled}
    bind:this={triggerEl}
  >
    <span class="dropdown-label" class:placeholder={!selected}>{label}</span>
    <svg
      class="dropdown-chevron"
      class:open
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <polyline points="6 9 12 15 18 9" />
    </svg>
  </button>

  {#if open}
    <ul class="dropdown-menu" role="listbox" bind:this={menuEl}>
      {#each options as opt, i (String(opt.value))}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <li
          class="dropdown-option"
          class:highlighted={i === highlighted}
          class:selected={opt.value === value}
          class:disabled={opt.disabled}
          role="option"
          aria-selected={opt.value === value}
          onclick={() => pick(opt)}
          onmouseenter={() => (highlighted = i)}
        >
          {opt.label}
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .dropdown {
    position: relative;
    width: 100%;
  }
  .dropdown.disabled {
    opacity: 0.55;
    pointer-events: none;
  }

  .dropdown-trigger {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 9px 12px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 10px;
    color: var(--color-text);
    font-size: 13px;
    font-family: inherit;
    cursor: pointer;
    text-align: left;
    transition: border-color 0.15s ease, background 0.15s ease;
  }
  .dropdown-trigger:hover {
    background: rgba(255, 255, 255, 0.08);
  }
  .dropdown-trigger:focus-visible,
  .dropdown.open .dropdown-trigger {
    outline: none;
    border-color: rgba(0, 191, 166, 0.5);
    box-shadow: 0 0 0 3px rgba(0, 191, 166, 0.1);
  }

  .dropdown-label {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .dropdown-label.placeholder {
    color: var(--color-muted, rgba(255, 255, 255, 0.45));
  }

  .dropdown-chevron {
    flex-shrink: 0;
    color: var(--color-mid, rgba(255, 255, 255, 0.55));
    transition: transform 0.15s ease;
  }
  .dropdown-chevron.open {
    transform: rotate(180deg);
  }

  .dropdown-menu {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    z-index: 500;
    list-style: none;
    margin: 0;
    padding: 4px;
    background: #1a2342;
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 10px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5);
    max-height: 260px;
    overflow-y: auto;
    animation: dd-appear 0.12s ease-out;
  }

  @keyframes dd-appear {
    from { opacity: 0; transform: translateY(-4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .dropdown-option {
    padding: 8px 10px;
    font-size: 13px;
    color: #e8e8ee;
    border-radius: 6px;
    cursor: pointer;
    user-select: none;
  }
  .dropdown-option.highlighted:not(.disabled) {
    background: rgba(0, 191, 166, 0.18);
    color: #fff;
  }
  .dropdown-option.selected {
    color: var(--color-teal, #0fc);
    font-weight: 600;
  }
  .dropdown-option.disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
</style>

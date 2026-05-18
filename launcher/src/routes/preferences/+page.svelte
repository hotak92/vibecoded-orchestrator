<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke, safeInvoke, listen as tauriListen } from '$lib/tauri';
  import { selectedProject } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import { ui } from '$lib/stores/ui';
  import Toast from '$lib/components/Toast.svelte';
  import Dropdown from '$lib/components/Dropdown.svelte';
  import type {
    EmbeddingCatalog,
    ModelChoice,
    DefaultEmbeddingModels,
  } from '$lib/types/embedding-catalog';

  // Setting key → default value
  const KEYS = [
    { key: 'watermark_enabled', label: 'Show free-tier watermark on agent outputs', kind: 'bool' as const, default: true },
    { key: 'auto_update_enabled', label: 'Auto-check for orchestrator updates', kind: 'bool' as const, default: true },
    { key: 'logging_level', label: 'Logging level', kind: 'enum' as const, default: 'info', options: ['debug', 'info', 'warning', 'error'] },
    { key: 'tray_start_minimized', label: 'Start launcher minimized to tray', kind: 'bool' as const, default: false },
    { key: 'tray_close_to_tray', label: 'Close button minimizes to tray (doesn\'t exit)', kind: 'bool' as const, default: true },
    { key: 'default_embedding_mode', label: 'Default embedding backend', kind: 'enum' as const, default: 'gpu', options: ['gpu', 'ollama'] },
  ];

  let values = $state<Record<string, any>>({});
  let loading = $state(true);

  // Onboarding re-trigger state
  let showOnboardingConfirm = $state(false);

  // ── GitHub access token (Manage Token UI, wired 2026-05-09) ───────
  // Reads via has_github_pat / get_github_pat_preview, writes via
  // register_github_pat (with EXISTS_DIFFERENT: replace-guard), clears
  // via clear_github_pat. Token is stored in the OS keychain — never
  // displayed in clear, never written to GUI-readable files.
  const PAT_REPLACE_GUARD = 'EXISTS_DIFFERENT:';
  let patPresent = $state(false);
  let patPreview = $state<string | null>(null);
  let patEditing = $state(false);
  let patNewValue = $state('');
  let patSaving = $state(false);
  let patError = $state<string | null>(null);
  let patClearing = $state(false);
  let showPatClearConfirm = $state(false);

  async function loadPat() {
    try {
      patPresent = await invoke<boolean>('has_github_pat');
      if (patPresent) {
        patPreview = await invoke<string | null>('get_github_pat_preview');
      } else {
        patPreview = null;
      }
    } catch (e) {
      patError = String(e);
    }
  }

  async function savePat() {
    patError = null;
    const token = patNewValue.trim();
    if (!token) {
      patError = 'Token is empty.';
      return;
    }
    patSaving = true;
    try {
      try {
        await invoke('register_github_pat', { token, force: false });
      } catch (e) {
        const msg = String(e);
        if (msg.startsWith(PAT_REPLACE_GUARD)) {
          const reason = msg.slice(PAT_REPLACE_GUARD.length).trim()
            || 'A different GitHub token is already saved.';
          if (!confirm(`${reason}\n\nReplace the existing token?`)) {
            patError = 'Token not saved (existing keychain entry kept).';
            return;
          }
          await invoke('register_github_pat', { token, force: true });
        } else {
          throw e;
        }
      }
      patNewValue = '';
      patEditing = false;
      await loadPat();
      toast.success('GitHub token saved');
    } catch (e) {
      patError = String(e);
    } finally {
      patSaving = false;
    }
  }

  async function clearPat() {
    showPatClearConfirm = false;
    patClearing = true;
    patError = null;
    try {
      await invoke('clear_github_pat');
      await loadPat();
      toast.success('GitHub token removed');
    } catch (e) {
      patError = String(e);
    } finally {
      patClearing = false;
    }
  }

  // ── Hardware re-detection (Bug B, v0.2.5) ──────────────────────────
  // Two-stage UX:
  //   1. "Re-detect hardware" → runs detect_system server-side, persists
  //      a fresh snapshot, returns a diff against the previous snapshot.
  //   2. If changed_fields is non-empty, surface "Apply reconfiguration"
  //      which spawns `install.py --update <flags>` from the known
  //      install path and streams progress events into a log panel.
  interface HardwareSnapshot {
    has_nvidia_gpu: boolean;
    gpu_name: string;
    has_apple_silicon: boolean;
    ram_gb: number;
    use_gpu: boolean;
    low_resource: boolean;
  }
  interface HardwareDetectionDiff {
    before: HardwareSnapshot | null;
    after: HardwareSnapshot;
    changed_fields: string[];
  }
  interface ReconfigReport {
    success: boolean;
    exit_code: number;
    log_path: string;
  }

  let hwDiff = $state<HardwareDetectionDiff | null>(null);
  let hwDetecting = $state(false);
  let hwApplying = $state(false);
  let hwError = $state<string | null>(null);
  let hwLog = $state<string[]>([]);
  let hwLastReport = $state<ReconfigReport | null>(null);
  let unlistenHwProgress: (() => void) | null = null;

  async function loadInitialHardwareSnapshot() {
    // Render the persisted snapshot (seeded at first boot) so the user
    // sees the current hardware fingerprint even before clicking
    // Re-detect. Soft-fail: an empty / missing app_state row just leaves
    // the section in the "no snapshot yet" state.
    const raw = await safeInvoke<{ value: string | null; is_set: boolean }>(
      'app_state_get',
      { key: 'launcher.hardware_snapshot' },
    );
    if (raw && raw.is_set && raw.value) {
      try {
        const snap = JSON.parse(raw.value) as HardwareSnapshot;
        hwDiff = { before: null, after: snap, changed_fields: [] };
      } catch {
        // Corrupted row — ignore; the next Re-detect will overwrite it.
      }
    }
  }

  async function redetectHardware() {
    hwError = null;
    hwDetecting = true;
    try {
      const diff = await invoke<HardwareDetectionDiff>('redetect_hardware');
      hwDiff = diff;
      if (diff.changed_fields.length === 0) {
        toast.success('Hardware unchanged');
      } else {
        toast.success(`Hardware changed (${diff.changed_fields.length} field(s))`);
      }
    } catch (e) {
      hwError = String(e);
      toast.error(e);
    } finally {
      hwDetecting = false;
    }
  }

  async function applyHardwareReconfig() {
    hwError = null;
    hwLog = [];
    hwLastReport = null;
    hwApplying = true;
    try {
      // Subscribe to progress events for the duration of this run.
      unlistenHwProgress = await tauriListen<string>(
        'hardware_reconfig_progress',
        (event) => {
          hwLog = [...hwLog, event.payload];
        },
      );
      const report = await invoke<ReconfigReport>('apply_hardware_reconfig');
      hwLastReport = report;
      if (report.success) {
        toast.success('Hardware reconfiguration complete');
      } else {
        toast.error(`Reconfiguration failed (exit ${report.exit_code})`);
      }
    } catch (e) {
      hwError = String(e);
      toast.error(e);
    } finally {
      hwApplying = false;
      if (unlistenHwProgress) {
        unlistenHwProgress();
        unlistenHwProgress = null;
      }
    }
  }

  function formatHwField(name: string, snap: HardwareSnapshot | null): string {
    if (!snap) return '—';
    switch (name) {
      case 'has_nvidia_gpu': return snap.has_nvidia_gpu ? 'yes' : 'no';
      case 'gpu_name': return snap.gpu_name || '(none)';
      case 'has_apple_silicon': return snap.has_apple_silicon ? 'yes' : 'no';
      case 'ram_gb': return `${snap.ram_gb} GB`;
      case 'use_gpu': return snap.use_gpu ? 'GPU' : 'CPU-only';
      case 'low_resource': return snap.low_resource ? 'low-resource mode' : 'standard';
      default: return '—';
    }
  }

  onDestroy(() => {
    if (unlistenHwProgress) {
      unlistenHwProgress();
      unlistenHwProgress = null;
    }
  });

  function confirmRerunOnboarding() {
    showOnboardingConfirm = false;
    ui.openOnboarding();
    goto('/');
  }

  // W3 / v0.2.16 (plan 0.9): manual re-arm of the legacy-collections
  // wizard. Resets the `legacy_codegraph_notice_dismissed` app_state
  // flag so the wizard re-detects on the next launcher start. The
  // companion auto-reset in `rebuild_code_graph` covers the
  // re-analyze pathway; this button is for users who want to force a
  // re-check WITHOUT triggering a fresh analyze.
  let legacyRechecking = $state(false);

  async function forceRecheckLegacy() {
    legacyRechecking = true;
    try {
      await invoke('force_recheck_legacy_codegraph');
      toast.success(
        'Wizard re-armed. Restart the launcher to re-detect legacy collections.',
      );
    } catch (e) {
      toast.error(e);
    } finally {
      legacyRechecking = false;
    }
  }

  // ── Default embedding models for new projects (v0.2.18 Commit 8) ──
  // Two dropdowns symmetric to install.py's preset-derived defaults.
  // Source of truth lives in `app_state.default_text_embedding` and
  // `default_code_embedding`. Values are model ids (e.g.
  // "qwen3-embedding:0.6b", "openai-text-embedding-3-small") — the
  // same strings consumed by `vco_lib.embedding_service` for slot
  // resolution. The dropdowns are populated from the live catalog
  // (`get_embedding_catalog`) so users can only pick what their
  // machine can actually serve.
  let embCatalog = $state<EmbeddingCatalog | null>(null);
  let embCatalogError = $state<string | null>(null);
  let defaultTextModel = $state<string>('');
  let defaultCodeModel = $state<string>('');

  function buildEmbOptions(models: ModelChoice[]) {
    return models.map((m) => ({
      value: m.id,
      label: m.available_now ? m.label : `${m.label} (unavailable)`,
      disabled: !m.available_now,
    }));
  }

  async function loadEmbeddingCatalog() {
    embCatalogError = null;
    try {
      embCatalog = await invoke<EmbeddingCatalog>('get_embedding_catalog', {
        projectId: null,
      });
      if (embCatalog.errors && embCatalog.errors.length > 0) {
        embCatalogError = embCatalog.errors.join('; ');
      }
    } catch (e) {
      embCatalog = null;
      embCatalogError = String(e);
    }
    try {
      const cur = await invoke<DefaultEmbeddingModels>(
        'get_default_embedding_models',
      );
      defaultTextModel = cur.text_model ?? '';
      defaultCodeModel = cur.code_model ?? '';
    } catch (e) {
      // Soft-fail: row absent (never set) is the common case on first
      // boot — leave fields empty so the dropdown shows the placeholder.
      console.warn('[vct] get_default_embedding_models:', e);
    }
  }

  /** Save the user's pick for the text-embedding default. Per the
   *  v0.2.18 locked rule, this is the EXPLICIT consent surface — no
   *  auto-switch fires elsewhere when an OpenAI key validates. */
  async function saveDefaultTextEmbedding(value: string) {
    try {
      await invoke('set_default_embedding_models', {
        textModel: value || null,
        codeModel: null,
      });
      defaultTextModel = value;
      toast.success('Default text embedding saved');
    } catch (e) {
      toast.error(e);
    }
  }

  async function saveDefaultCodeEmbedding(value: string) {
    try {
      await invoke('set_default_embedding_models', {
        textModel: null,
        codeModel: value || null,
      });
      defaultCodeModel = value;
      toast.success('Default code embedding saved');
    } catch (e) {
      toast.error(e);
    }
  }

  /**
   * v0.2.18 Commit 7 (2026-05-19): styled-modal confirmation for
   * "set OpenAI as default for new projects".
   *
   * Replaces the native `window.confirm()` originally staged by Commit 8
   * (AGENT-DROPDOWNS) at the wave-handoff seam. Commit 7 owns the OpenAI
   * Apply path now, so the consent surface is the same focus-trapped
   * Svelte modal pattern as `showOnboardingConfirm` / `showPatClearConfirm`.
   *
   * Contract:
   *   - Resolves `true`  iff the user clicked "Yes, set as default".
   *   - Resolves `false` on "No, keep current", ESC, or click-outside.
   *   - Caller is responsible for the actual `set_default_embedding_models`
   *     write (we just gather consent here).
   *
   * The global `window.__vct_confirm_set_openai_default` indirection
   * staged by Commit 8 is REMOVED — Commit 7 calls this function
   * directly from `applyOpenAiKey` below. The locked v0.2.18 rule
   * (no auto-switch) is still enforced because the function is the
   * only call site that calls `set_default_embedding_models` with
   * openai-* ids.
   */
  let openaiConfirm = $state<{
    open: boolean;
    textModelId: string;
    codeModelId: string;
    resolve: ((v: boolean) => void) | null;
  }>({ open: false, textModelId: '', codeModelId: '', resolve: null });

  function confirmSetOpenAiAsDefault(
    textModelId: string,
    codeModelId: string,
  ): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      openaiConfirm = {
        open: true,
        textModelId,
        codeModelId,
        resolve,
      };
    });
  }

  function closeOpenaiConfirm(decision: boolean) {
    const resolver = openaiConfirm.resolve;
    openaiConfirm = {
      open: false,
      textModelId: '',
      codeModelId: '',
      resolve: null,
    };
    if (resolver) resolver(decision);
  }

  function handleOpenaiConfirmKey(e: KeyboardEvent) {
    // ESC closes with `false` — matches click-outside semantics.
    if (e.key === 'Escape') {
      e.preventDefault();
      closeOpenaiConfirm(false);
    }
  }

  /** Focus the primary action when the modal opens. Focus trap is
   *  handled by the dialog's `aria-modal` + the action() below that
   *  cycles Tab through the modal's two buttons only. */
  function focusOnMount(node: HTMLButtonElement) {
    // Defer to next tick so Svelte has wired up the rest of the modal.
    queueMicrotask(() => node.focus());
  }

  /** Minimal focus trap: cycle Tab/Shift+Tab between the two buttons.
   *  Svelte 5 actions take a node + return { destroy }. We attach to
   *  the modal root so any focus leaving via Tab from the last button
   *  loops to the first, and vice versa. */
  function focusTrap(node: HTMLElement) {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Tab') return;
      const focusable = node.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    }
    node.addEventListener('keydown', onKey);
    return {
      destroy() {
        node.removeEventListener('keydown', onKey);
      },
    };
  }

  // ── OpenAI API key (v0.2.18 Commit 7) ──────────────────────────────────
  // Symmetric to the GitHub PAT section above. Key lives in the OS
  // keychain (set via `register_openai_api_key`), pre-fill is masked
  // (via `get_openai_api_key_preview`), Re-check uses keychain rather
  // than the input (via `recheck_openai_validity`), Clear removes both
  // keychain + `openai_was_valid` / `openai_fallback_pending` state
  // breadcrumbs (via `clear_openai_api_key`). The post-Apply consent
  // for "use as default for new projects" goes through the styled
  // modal above — no native confirm().
  type OpenaiStatus =
    | { kind: 'idle' }
    | { kind: 'unvalidated' }      // key present in keychain, not yet checked
    | { kind: 'valid'; model: string; rateLimited: boolean }
    | { kind: 'previously_valid_failing'; reason: string; httpStatus: number | null }
    | { kind: 'invalid'; reason: string; httpStatus: number | null }
    | { kind: 'error'; detail: string }
    | { kind: 'working' };

  let openaiPresent = $state(false);
  let openaiPreview = $state<string | null>(null);
  let openaiInput = $state('');
  let openaiShow = $state(false);       // toggle masked → text
  let openaiStatus = $state<OpenaiStatus>({ kind: 'idle' });
  let openaiBusy = $state(false);       // shared Apply / Re-check / Clear spinner gate

  /** Discriminated union mirroring `OpenAiValidationResult` in
   *  `commands::openai_cmd`. Status field is the tag. */
  type OpenAiValidationResult =
    | { status: 'valid'; model: string; rate_limited: boolean }
    | { status: 'invalid'; reason: string; http_status: number | null }
    | { status: 'error'; detail: string };

  /** Pre-fill the row at mount. If a key is present in the keychain we
   *  show a masked placeholder (•••• prefix) so the user understands
   *  there's a key without exposing it; if absent we leave the input
   *  empty. Soft-fail: a keychain read error renders as "no key" plus
   *  an error status — same UX as Clear having just succeeded.
   *
   *  This also seeds `openaiStatus` to `unvalidated` so the user sees
   *  "Stored — click Re-check to verify" rather than the blank `idle`
   *  state when they reopen the page after registering a key in a
   *  prior session. */
  async function loadOpenAi() {
    openaiStatus = { kind: 'idle' };
    try {
      openaiPresent = await invoke<boolean>('has_openai_api_key');
    } catch (e) {
      openaiPresent = false;
      openaiStatus = { kind: 'error', detail: String(e) };
      return;
    }
    if (openaiPresent) {
      try {
        openaiPreview = await invoke<string | null>('get_openai_api_key_preview');
      } catch {
        openaiPreview = null;
      }
      // Show a masked placeholder so Apply / Re-check make sense to
      // the user without having to retype the key. The actual input
      // value stays empty — typing replaces; submitting empty uses the
      // stored key via Re-check.
      openaiStatus = { kind: 'unvalidated' };
    } else {
      openaiPreview = null;
    }
  }

  /** Apply button:
   *   1. If input is empty AND a key is stored → re-check the stored key.
   *   2. If input is empty AND no key stored → show inline error.
   *   3. Else: register the typed key with `set_as_default=false` (we
   *      ALWAYS prompt the user separately via the styled modal — per
   *      the v0.2.18 locked rule, no auto-switch).
   *
   *  On success of (3), if the validation said `valid` we open the
   *  consent modal and, if the user says Yes, flip the default-
   *  embedding rows to openai-* ids. The catalog dropdowns above are
   *  re-loaded so the new defaults render. */
  async function applyOpenAiKey() {
    if (openaiBusy) return;
    const typed = openaiInput.trim();

    // (1)+(2): input empty
    if (typed === '') {
      if (openaiPresent) {
        await recheckOpenAi();
      } else {
        openaiStatus = {
          kind: 'invalid',
          reason: 'Enter an API key to Apply, or click Add token to start.',
          httpStatus: null,
        };
      }
      return;
    }

    openaiBusy = true;
    openaiStatus = { kind: 'working' };
    try {
      // (3) Validate + persist. The Rust command runs validation FIRST
      // and refuses to write if invalid, so a successful return implies
      // the keychain now holds a working key.
      const resp = await invoke<{ masked_key: string; default_set: boolean }>(
        'register_openai_api_key',
        { value: typed, setAsDefault: false },
      );
      // Re-read presence + preview from the keychain (the rust code
      // returns masked_key but we keep one source of truth for the
      // preview format).
      openaiPresent = true;
      openaiPreview = resp.masked_key;
      openaiInput = '';            // clear the input — keychain is now authoritative
      openaiShow = false;
      openaiStatus = {
        kind: 'valid',
        model: 'text-embedding-3-small',
        rateLimited: false,
      };
      toast.success('OpenAI key saved');

      // Consent prompt for "set as default for new projects". Locked
      // rule: NEVER auto-flip; always ask. The modal's Yes button is
      // the only path that calls set_default_embedding_models with
      // openai-* ids from this surface.
      const yes = await confirmSetOpenAiAsDefault(
        'openai-text-embedding-3-small',
        'openai-text-embedding-3-small',
      );
      if (yes) {
        try {
          await invoke('set_default_embedding_models', {
            textModel: 'openai-text-embedding-3-small',
            codeModel: 'openai-text-embedding-3-small',
          });
          toast.success('OpenAI set as default for new projects');
          // Refresh the dropdowns above so the new defaults render.
          await loadEmbeddingCatalog();
        } catch (e) {
          toast.error(`Failed to set defaults: ${e}`);
        }
      }
    } catch (e) {
      // Error or Invalid both surface as Err(_) at the Tauri boundary
      // for `register_openai_api_key`. The error string starts with
      // either "openai key validation failed:" (Invalid) or "openai
      // key validation error:" (network/timeout). Parse for the user-
      // friendly status.
      const msg = String(e);
      if (msg.startsWith('openai key validation failed:')) {
        openaiStatus = {
          kind: 'invalid',
          reason: msg.replace('openai key validation failed:', '').trim() ||
            'unknown reason',
          httpStatus: null,
        };
      } else if (msg.startsWith('openai key validation error:')) {
        openaiStatus = {
          kind: 'error',
          detail: msg.replace('openai key validation error:', '').trim() ||
            'network error',
        };
      } else {
        openaiStatus = { kind: 'error', detail: msg };
      }
      toast.error(e);
    } finally {
      openaiBusy = false;
    }
  }

  /** Re-check button:
   *  Calls `recheck_openai_validity` which reads the stored keychain
   *  value, re-validates against OpenAI's free /v1/models/<model>
   *  probe, and runs the recovery state machine. The state machine
   *  emits `vct-openai-key-invalidated` / `vct-openai-key-restored`
   *  events on its own — our event listener (registered in onMount)
   *  shows the toasts for those.
   *
   *  We update the local `openaiStatus` indicator from the validation
   *  result itself so the row gives immediate feedback even before
   *  any Tauri event fires. */
  async function recheckOpenAi() {
    if (openaiBusy) return;
    openaiBusy = true;
    openaiStatus = { kind: 'working' };
    try {
      const v = await invoke<OpenAiValidationResult>('recheck_openai_validity');
      if (v.status === 'valid') {
        openaiStatus = {
          kind: 'valid',
          model: v.model,
          rateLimited: v.rate_limited,
        };
      } else if (v.status === 'invalid') {
        // Distinguish "previously valid, now failing" from "invalid".
        // The keychain still has the key (Re-check doesn't delete on
        // failure — per the locked rule the user may need to renew
        // their subscription), so we surface the kept-stored ⚠️
        // state if openai_was_valid was true. We use the typed bool
        // helper so the "true"/"1" parsing lives in one place
        // (Rust-side `app_state_get_bool`).
        const ever = await safeInvoke<boolean | null>(
          'app_state_get_bool',
          { key: 'openai_was_valid' },
        );
        if (ever === true) {
          openaiStatus = {
            kind: 'previously_valid_failing',
            reason: v.reason,
            httpStatus: v.http_status,
          };
        } else {
          openaiStatus = {
            kind: 'invalid',
            reason: v.reason,
            httpStatus: v.http_status,
          };
        }
      } else {
        openaiStatus = { kind: 'error', detail: v.detail };
      }
    } catch (e) {
      const msg = String(e);
      if (msg === 'no_key_set') {
        openaiStatus = {
          kind: 'invalid',
          reason: 'No key configured. Add one above first.',
          httpStatus: null,
        };
      } else {
        openaiStatus = { kind: 'error', detail: msg };
      }
    } finally {
      openaiBusy = false;
    }
  }

  /** Clear button:
   *  Removes the key from the keychain + clears the recovery state-
   *  machine breadcrumbs (`openai_was_valid`, `openai_fallback_pending`).
   *  Does NOT change `default_text_embedding` / `default_code_embedding`
   *  — flipping defaults is a separate user action per the locked
   *  no-auto-switch rule. */
  let openaiShowClearConfirm = $state(false);
  async function clearOpenAi() {
    openaiShowClearConfirm = false;
    if (openaiBusy) return;
    openaiBusy = true;
    try {
      await invoke('clear_openai_api_key');
      openaiPresent = false;
      openaiPreview = null;
      openaiInput = '';
      openaiShow = false;
      openaiStatus = { kind: 'idle' };
      toast.success('OpenAI key removed');
      // Refresh the catalog: with the key gone, OpenAI rows render as
      // unavailable (`available_now=false` + `reason_unavailable` set).
      await loadEmbeddingCatalog();
    } catch (e) {
      toast.error(e);
    } finally {
      openaiBusy = false;
    }
  }

  // ── Recovery-flow toasts (v0.2.18 Commit 7) ────────────────────────────
  // Listen for the two events emitted by the startup re-check task
  // (`commands::openai_cmd::run_openai_startup_recheck`) AND the on-demand
  // `recheck_openai_validity`. Both wire through the same recovery state
  // machine in `apply_recovery_transition`. Toast text mirrors the spec:
  //   - Invalidated: "⚠️ OpenAI key is failing validation. Falling back
  //                   to local models. Click Re-check or update your key."
  //   - Restored:    "✅ OpenAI key works again. Restored: ${slots}."
  let unlistenOpenAiInvalidated: (() => void) | null = null;
  let unlistenOpenAiRestored: (() => void) | null = null;

  interface InvalidatedPayload {
    reason: string;
    restored_defaults?: { text?: string | null; code?: string | null } | null;
    already_fallen_back?: boolean;
  }
  interface RestoredPayload {
    restored_slots?: { text?: string | null; code?: string | null };
  }

  async function subscribeOpenAiEvents() {
    unlistenOpenAiInvalidated = await tauriListen<InvalidatedPayload>(
      'vct-openai-key-invalidated',
      (event) => {
        // Suppress the toast on repeat launches that just confirm "still
        // broken" — the banner-style status indicator below already
        // surfaces this. The state machine emits with already_fallen_back=true
        // on every boot after the first invalidation to keep the GUI
        // banner sticky, but we only want a single toast per session.
        if (event.payload.already_fallen_back) return;
        toast.error(
          '⚠️ OpenAI key is failing validation. Falling back to local models. ' +
            'Click Re-check or update your key.',
        );
        // Pull fresh state so the indicator reflects "previously valid,
        // currently failing" instead of staying on whatever it was.
        void loadEmbeddingCatalog();
        void loadOpenAi();
      },
    );
    unlistenOpenAiRestored = await tauriListen<RestoredPayload>(
      'vct-openai-key-restored',
      (event) => {
        const slots = event.payload.restored_slots;
        const parts: string[] = [];
        if (slots?.text) parts.push(`text → ${slots.text}`);
        if (slots?.code) parts.push(`code → ${slots.code}`);
        const tail = parts.length > 0 ? parts.join(', ') : 'defaults';
        toast.success(`✅ OpenAI key works again. Restored: ${tail}.`);
        void loadEmbeddingCatalog();
        void loadOpenAi();
      },
    );
  }

  const project = $derived($selectedProject);

  async function load() {
    if (!project) return;
    loading = true;
    try {
      const out: Record<string, any> = {};
      for (const k of KEYS) {
        try {
          const raw = await invoke<any>('get_setting_v2', {
            projectId: project.id,
            moduleId: 'launcher',
            key: k.key,
          });
          out[k.key] = raw ?? k.default;
        } catch {
          out[k.key] = k.default;
        }
      }
      values = out;
    } finally {
      loading = false;
    }
  }

  async function save(key: string, value: any) {
    if (!project) return;
    values = { ...values, [key]: value };
    try {
      await invoke('set_setting_v2', {
        projectId: project.id,
        moduleId: 'launcher',
        key,
        value,
      });
      toast.success('Saved');
    } catch (e) {
      toast.error(e);
    }
  }

  onMount(() => {
    void load();
    void loadPat();
    void loadInitialHardwareSnapshot();
    void loadEmbeddingCatalog();
    void loadOpenAi();
    // Subscribe to the openai recovery events (no-op in browser mode).
    void subscribeOpenAiEvents();
  });
  $effect(() => { if (project) void load(); });

  onDestroy(() => {
    // Existing hwprogress cleanup is in the earlier onDestroy; both
    // hooks are safe to register independently.
    if (unlistenOpenAiInvalidated) {
      unlistenOpenAiInvalidated();
      unlistenOpenAiInvalidated = null;
    }
    if (unlistenOpenAiRestored) {
      unlistenOpenAiRestored();
      unlistenOpenAiRestored = null;
    }
    // If the user navigates away with the confirm modal open, resolve
    // it as `false` so the Apply Promise doesn't dangle.
    if (openaiConfirm.resolve) {
      openaiConfirm.resolve(false);
    }
  });
</script>

<div class="pr-page">
  <header class="pr-header">
    <button class="pr-back" onclick={() => goto('/')}>← Back</button>
    <h1>Preferences</h1>
  </header>

  <main class="pr-main">
    <!-- Project-scoped settings (KG / module dropdowns) require a selected
         project. Onboarding and Launcher self-update are app-level — they
         work for new users who don't have a project yet, so they live
         OUTSIDE the project guard. -->

    {#if !project}
      <p class="pr-empty">Select a project from the menu bar to edit project-scoped settings.</p>
    {:else if loading}
      <p class="pr-empty">Loading project settings…</p>
    {:else}
      <p class="pr-hint">
        Settings scoped to <code>{project.name}</code>. They're stored under the <code>launcher</code> module
        namespace in <code>~/.vct/launcher.db</code>.
      </p>
      <ul class="pr-list">
        {#each KEYS as k}
          <li class="pr-row">
            <strong>{k.label}</strong>
            {#if k.kind === 'bool'}
              <input
                type="checkbox"
                checked={values[k.key] === true}
                onchange={(e) => save(k.key, (e.target as HTMLInputElement).checked)}
              />
            {:else if k.kind === 'enum' && k.options}
              <div class="pr-dd">
                <Dropdown
                  options={k.options.map((opt: string) => ({ value: opt, label: opt }))}
                  value={values[k.key]}
                  onChange={(v: string) => save(k.key, v)}
                />
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}

    <!--
      Default embedding models for new projects (v0.2.18 Commit 8).
      App-level — applies to projects created from now on. Existing
      projects keep their per-binding model. Populated from the live
      catalog so users can only pick what their machine can serve;
      unavailable models render greyed-out with a tooltip explaining
      why. Per the v0.2.18 locked rule (no-auto-switch), this is the
      EXPLICIT consent surface for any change to defaults.
    -->
    <section class="pr-section">
      <h2 class="pr-section-title">Default embedding models</h2>
      {#if embCatalogError}
        <p class="pr-emb-warn">
          Catalog warning: {embCatalogError}
          <button
            class="pr-link-btn"
            onclick={() => void loadEmbeddingCatalog()}
          >
            retry
          </button>
        </p>
      {/if}
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Default text embedding for new projects</strong>
          <span class="pr-onboarding-hint">
            Applied to the KG binding when you create a new project. Existing
            projects are not affected. Greyed-out options are unreachable
            from this machine.
          </span>
        </div>
        <div class="pr-dd">
          <Dropdown
            options={embCatalog ? buildEmbOptions(embCatalog.text_models) : []}
            value={defaultTextModel}
            placeholder={embCatalog ? 'Select model…' : 'Loading…'}
            ariaLabel="Default text embedding"
            onChange={(v: string) => void saveDefaultTextEmbedding(v)}
          />
        </div>
      </div>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Default code embedding for new projects</strong>
          <span class="pr-onboarding-hint">
            Applied to the codegraph binding when you create a new project.
            Existing projects are not affected.
          </span>
        </div>
        <div class="pr-dd">
          <Dropdown
            options={embCatalog ? buildEmbOptions(embCatalog.code_models) : []}
            value={defaultCodeModel}
            placeholder={embCatalog ? 'Select model…' : 'Loading…'}
            ariaLabel="Default code embedding"
            onChange={(v: string) => void saveDefaultCodeEmbedding(v)}
          />
        </div>
      </div>
    </section>

    <!-- Onboarding + Updates: app-level, available regardless of project state. -->
    <section class="pr-section">
      <h2 class="pr-section-title">Onboarding</h2>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Re-run onboarding wizard</strong>
          <span class="pr-onboarding-hint">
            Walks through project setup, KG bindings, and module recommendations again.
            Existing projects and settings won't be affected.
          </span>
        </div>
        <button class="pr-btn" onclick={() => (showOnboardingConfirm = true)}>
          Re-run wizard
        </button>
      </div>
    </section>

    <section class="pr-section">
      <h2 class="pr-section-title">Updates</h2>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Launcher self-update</strong>
          <span class="pr-onboarding-hint">
            Pulls launcher updates from the upstream repo. Daily check, manual apply.
            User-owned files (CONTEXT_STATE.md, logs, runtime state) are never overwritten.
          </span>
        </div>
        <button class="pr-btn" onclick={() => goto('/preferences/updates')}>
          Open
        </button>
      </div>
    </section>

    <!-- W3 / v0.2.16 (plan 0.9): re-check for legacy code-graph
         collections. The wizard's "Dismiss for now" button sets a
         persistent flag that suppresses re-detection on subsequent
         launcher boots — that flag auto-resets when the user
         re-analyzes a project, but if the user wants to force a
         re-check WITHOUT re-running an analyzer they can flip the
         flag back here. The wizard re-fires on the next launcher
         start. -->
    <section class="pr-section">
      <h2 class="pr-section-title">Code-graph collections</h2>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Re-check for legacy collections</strong>
          <span class="pr-onboarding-hint">
            Resets the dismissed-wizard flag so the legacy-collections
            wizard re-fires on the next launcher start. Useful if you
            dismissed the wizard once and want to verify there are no
            new orphans from a subsequent project rename or analyzer
            re-run.
          </span>
        </div>
        <button class="pr-btn" disabled={legacyRechecking} onclick={() => void forceRecheckLegacy()}>
          {legacyRechecking ? 'Resetting…' : 'Re-check'}
        </button>
      </div>
    </section>

    <!-- PR-10A storage UX (v0.2.11): deep link to the per-service
         storage picker. Adjacent to Updates because both are about
         runtime infrastructure (container data location vs. orchestrator
         self-update). -->
    <section class="pr-section">
      <h2 class="pr-section-title">Storage</h2>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Container data location</strong>
          <span class="pr-onboarding-hint">
            Choose named volumes (recommended) or a custom bind path for
            Weaviate, Ollama, and code-embed data. Pre-existing volumes
            from earlier installs can be reused in place.
          </span>
        </div>
        <button class="pr-btn" onclick={() => goto('/preferences/storage')}>
          Open
        </button>
      </div>
      <!-- v0.2.16 (W4 / 0.11): advanced view of Weaviate code-graph
           inventory, including prefixes whose project is no longer
           registered. GUI defaults hide these; surfaced here for
           clean-up + diagnostics. -->
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Show untracked Weaviate collections</strong>
          <span class="pr-onboarding-hint">
            Full inventory of code-graph data including dead-project
            leftovers (collections whose prefix no longer matches a
            registered project). Day-to-day surfaces hide these for
            clarity — open this view to clean them up or reference them
            before re-importing the project.
          </span>
        </div>
        <button class="pr-btn" onclick={() => goto('/preferences/weaviate-untracked')}>
          Open
        </button>
      </div>
    </section>

    <!-- Secrets (PR-4, v0.2.11). Cross-cutting OS-keychain manager;
         deep-linked from the sidebar too. The Open button takes the
         user to the dedicated route so the import sub-page stays one
         click away from the manager. -->
    <section class="pr-section">
      <h2 class="pr-section-title">Secrets</h2>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Keychain manager</strong>
          <span class="pr-onboarding-hint">
            Per-project, shared, and global keychain entries used by orchestrator
            modules. Includes a bulk-import surface for migrating existing on-disk
            secrets (<code>~/.vct-secrets/</code> and project <code>.env</code> files)
            into the OS keychain.
          </span>
        </div>
        <button class="pr-btn" onclick={() => goto('/preferences/secrets')}>
          Open
        </button>
      </div>
    </section>

    <section class="pr-section">
      <h2 class="pr-section-title">GitHub access token</h2>
      <div class="pr-pat-row">
        <div class="pr-onboarding-text">
          <strong>
            {patPresent ? 'Token saved' : 'No token saved'}
            {#if patPresent && patPreview}<span class="pr-pat-preview">{patPreview}</span>{/if}
          </strong>
          <span class="pr-onboarding-hint">
            Stored in your OS keychain. Used by the launcher's update flow and propagated to
            registered projects' env files (<code>GITHUB_TOKEN</code>) when active. Replacing
            the token rotates it everywhere; clearing it removes it from the keychain (your
            <code>~/.vct-secrets/shared/github_pat</code> file, if any, is left untouched).
          </span>
        </div>
        <div class="pr-pat-actions">
          {#if !patEditing}
            <button class="pr-btn" onclick={() => { patEditing = true; patError = null; }}>
              {patPresent ? 'Replace…' : 'Add token…'}
            </button>
            {#if patPresent}
              <button
                class="pr-btn pr-btn-danger"
                disabled={patClearing}
                onclick={() => (showPatClearConfirm = true)}
              >
                {patClearing ? 'Clearing…' : 'Clear'}
              </button>
            {/if}
          {/if}
        </div>
      </div>

      {#if patEditing}
        <div class="pr-pat-edit">
          <input
            class="pr-pat-input"
            type="password"
            placeholder="ghp_…"
            bind:value={patNewValue}
            disabled={patSaving}
          />
          <div class="pr-pat-edit-actions">
            <button
              class="pr-btn"
              onclick={() => { patEditing = false; patNewValue = ''; patError = null; }}
              disabled={patSaving}
            >
              Cancel
            </button>
            <button
              class="pr-btn-primary"
              onclick={savePat}
              disabled={patSaving || !patNewValue.trim()}
            >
              {patSaving ? 'Saving…' : 'Save'}
            </button>
          </div>
          <p class="pr-pat-hint">
            Generate at github.com → Settings → Developer settings → Personal access tokens.
            Scope <code>repo</code> is enough for read-only update checks; add <code>workflow</code>
            if you need to push commits that modify <code>.github/workflows/</code>.
          </p>
        </div>
      {/if}

      {#if patError}<p class="pr-error">{patError}</p>{/if}
    </section>

    <!-- OpenAI API key (v0.2.18 Commit 7).
         Symmetric to the GitHub PAT row above. Key is stored in the OS
         keychain via `register_openai_api_key`; presence + masked
         preview surface via `has_openai_api_key` / `get_openai_api_key_preview`.
         Apply validates + persists; Re-check re-validates the stored
         key without retyping; Clear removes keychain + recovery
         breadcrumbs. Per the v0.2.18 locked rule, post-Apply success
         shows a styled-modal consent prompt before flipping the new-
         project defaults to openai-*. -->
    <section class="pr-section" aria-labelledby="pr-openai-title">
      <h2 class="pr-section-title" id="pr-openai-title">OpenAI API key (optional)</h2>
      <div class="pr-pat-row">
        <div class="pr-onboarding-text">
          <strong>
            {#if openaiPresent}
              Key saved
              {#if openaiPreview}<span class="pr-pat-preview">{openaiPreview}</span>{/if}
            {:else}
              No key saved
            {/if}
          </strong>
          <span class="pr-onboarding-hint">
            Use OpenAI for embeddings (KG + code graph) if you prefer cloud-quality
            vectors over local. Stored in your OS keychain — never written to plain
            files. Validation uses the free <code>GET /v1/models/text-embedding-3-small</code>
            probe (no tokens consumed). Leaving this blank keeps the launcher on
            local models (recommended for most users).
          </span>
        </div>
        <div class="pr-pat-actions">
          {#if openaiPresent}
            <button
              class="pr-btn"
              disabled={openaiBusy}
              onclick={() => void recheckOpenAi()}
              aria-label="Re-check stored OpenAI key validity"
            >
              {openaiBusy && openaiStatus.kind === 'working' ? 'Checking…' : 'Re-check'}
            </button>
            <button
              class="pr-btn pr-btn-danger"
              disabled={openaiBusy}
              onclick={() => (openaiShowClearConfirm = true)}
              aria-label="Remove OpenAI key from keychain"
            >
              Clear
            </button>
          {/if}
        </div>
      </div>

      <!-- Input row. Always rendered (unlike PAT's two-state collapse)
           because the Apply button doubles as a re-check trigger when
           the input is empty + a key is stored. -->
      <div class="pr-pat-edit">
        <label class="pr-openai-label" for="pr-openai-input">
          {openaiPresent ? 'Replace key (leave empty to re-check stored key)' : 'API key'}
        </label>
        <div class="pr-openai-input-row">
          <input
            id="pr-openai-input"
            class="pr-pat-input pr-openai-input"
            type={openaiShow ? 'text' : 'password'}
            placeholder={openaiPresent ? '••••••••' : 'sk-…'}
            bind:value={openaiInput}
            disabled={openaiBusy}
            autocomplete="off"
            spellcheck="false"
            aria-describedby="pr-openai-status"
          />
          <button
            class="pr-btn pr-openai-show-btn"
            type="button"
            onclick={() => (openaiShow = !openaiShow)}
            disabled={openaiBusy || openaiInput.length === 0}
            aria-label={openaiShow ? 'Hide API key' : 'Show API key'}
          >
            {openaiShow ? 'Hide' : 'Show'}
          </button>
          <button
            class="pr-btn-primary"
            onclick={() => void applyOpenAiKey()}
            disabled={openaiBusy}
          >
            {#if openaiBusy && openaiStatus.kind === 'working'}
              Working…
            {:else if openaiInput.trim() === '' && openaiPresent}
              Re-check existing key
            {:else}
              Apply
            {/if}
          </button>
        </div>

        <!-- Status indicator. One-line summary keyed by `openaiStatus.kind`.
             Colors mirror existing PR conventions: green = valid, red =
             invalid, yellow = previously-valid-failing (kept-stored case),
             grey = idle / unvalidated. -->
        <p
          id="pr-openai-status"
          class="pr-openai-status pr-openai-status-{openaiStatus.kind}"
          role="status"
          aria-live="polite"
        >
          {#if openaiStatus.kind === 'idle'}
            <span class="pr-openai-status-icon" aria-hidden="true">○</span>
            <span>Not yet validated.</span>
          {:else if openaiStatus.kind === 'unvalidated'}
            <span class="pr-openai-status-icon" aria-hidden="true">○</span>
            <span>Stored — click Re-check to verify validity.</span>
          {:else if openaiStatus.kind === 'working'}
            <span class="pr-openai-status-icon" aria-hidden="true">⟳</span>
            <span>Validating…</span>
          {:else if openaiStatus.kind === 'valid'}
            <span class="pr-openai-status-icon" aria-hidden="true">✓</span>
            <span>
              Valid — <code>{openaiStatus.model}</code> accessible{openaiStatus.rateLimited
                ? ' (rate-limited at probe time, key itself is fine)'
                : ''}.
            </span>
          {:else if openaiStatus.kind === 'previously_valid_failing'}
            <span class="pr-openai-status-icon" aria-hidden="true">⚠</span>
            <span>
              Previously valid, currently failing — key kept stored.
              Reason: {openaiStatus.reason}{openaiStatus.httpStatus !== null
                ? ` (HTTP ${openaiStatus.httpStatus})`
                : ''}.
            </span>
          {:else if openaiStatus.kind === 'invalid'}
            <span class="pr-openai-status-icon" aria-hidden="true">✕</span>
            <span>
              Invalid: {openaiStatus.reason}{openaiStatus.httpStatus !== null
                ? ` (HTTP ${openaiStatus.httpStatus})`
                : ''}.
            </span>
          {:else if openaiStatus.kind === 'error'}
            <span class="pr-openai-status-icon" aria-hidden="true">!</span>
            <span>Network error: {openaiStatus.detail}.</span>
          {/if}
        </p>
        <p class="pr-pat-hint">
          Generate at <code>platform.openai.com</code> → API keys → Create new
          secret key. Project-scoped keys must include
          <code>text-embedding-3-small</code> in their model allowlist.
        </p>
      </div>
    </section>

    <!-- Hardware re-detection (Bug B, v0.2.5).
         Two-stage UX: Re-detect → optional Apply reconfig. The persisted
         snapshot is seeded at first launcher boot so the "currently
         detected" panel renders even before the user clicks Re-detect. -->
    <section class="pr-section">
      <h2 class="pr-section-title">Hardware</h2>
      <div class="pr-hw-card">
        <div class="pr-hw-header">
          <div class="pr-onboarding-text">
            <strong>Detected hardware</strong>
            <span class="pr-onboarding-hint">
              Updated at install time and whenever you click Re-detect. If you upgrade
              your GPU or RAM, re-detect so the orchestrator's containers and models can
              be reconfigured to use the new resources.
            </span>
          </div>
          <button
            class="pr-btn"
            onclick={() => void redetectHardware()}
            disabled={hwDetecting || hwApplying}
          >
            {hwDetecting ? 'Detecting…' : 'Re-detect hardware'}
          </button>
        </div>

        {#if hwDiff}
          <div class="pr-hw-grid">
            <div class="pr-hw-row"><span class="pr-hw-label">GPU (NVIDIA)</span>
              <span class="pr-hw-value">{formatHwField('has_nvidia_gpu', hwDiff.after)}</span></div>
            <div class="pr-hw-row"><span class="pr-hw-label">GPU name</span>
              <span class="pr-hw-value">{formatHwField('gpu_name', hwDiff.after)}</span></div>
            <div class="pr-hw-row"><span class="pr-hw-label">Apple Silicon</span>
              <span class="pr-hw-value">{formatHwField('has_apple_silicon', hwDiff.after)}</span></div>
            <div class="pr-hw-row"><span class="pr-hw-label">RAM</span>
              <span class="pr-hw-value">{formatHwField('ram_gb', hwDiff.after)}</span></div>
            <div class="pr-hw-row"><span class="pr-hw-label">Compute mode</span>
              <span class="pr-hw-value">{formatHwField('use_gpu', hwDiff.after)}</span></div>
            <div class="pr-hw-row"><span class="pr-hw-label">Resource tier</span>
              <span class="pr-hw-value">{formatHwField('low_resource', hwDiff.after)}</span></div>
          </div>
        {:else}
          <p class="pr-hw-empty">No hardware snapshot yet — click Re-detect.</p>
        {/if}

        {#if hwDiff && hwDiff.changed_fields.length > 0 && hwDiff.before}
          <div class="pr-hw-diff">
            <h3 class="pr-hw-diff-title">Changes since last detection</h3>
            <ul class="pr-hw-diff-list">
              {#each hwDiff.changed_fields as field}
                <li class="pr-hw-diff-row">
                  <span class="pr-hw-diff-field">{field}</span>
                  <span class="pr-hw-diff-before">{formatHwField(field, hwDiff.before)}</span>
                  <span class="pr-hw-diff-arrow">→</span>
                  <span class="pr-hw-diff-after">{formatHwField(field, hwDiff.after)}</span>
                </li>
              {/each}
            </ul>
            <button
              class="pr-btn-primary"
              onclick={() => void applyHardwareReconfig()}
              disabled={hwApplying || hwDetecting}
            >
              {hwApplying ? 'Applying…' : 'Apply reconfiguration'}
            </button>
            <p class="pr-onboarding-hint">
              Runs <code>install.py --update</code> from the known install path with
              flags derived from the detected hardware. Containers and models will be
              reconfigured — services may restart briefly.
            </p>
          </div>
        {/if}

        {#if hwLog.length > 0}
          <div class="pr-hw-log">
            <div class="pr-hw-log-header">
              <strong>Reconfiguration output</strong>
              {#if hwLastReport}
                <span class={hwLastReport.success ? 'pr-hw-log-ok' : 'pr-hw-log-fail'}>
                  exit {hwLastReport.exit_code} ({hwLastReport.success ? 'ok' : 'failed'})
                </span>
              {/if}
            </div>
            <pre class="pr-hw-log-body">{hwLog.join('\n')}</pre>
            {#if hwLastReport}
              <p class="pr-onboarding-hint">Log: <code>{hwLastReport.log_path}</code></p>
            {/if}
          </div>
        {/if}

        {#if hwError}<p class="pr-error">{hwError}</p>{/if}
      </div>
    </section>
  </main>
</div>

{#if showOnboardingConfirm}
  <!-- Confirmation modal rendered as a simple overlay so we avoid pulling in
       DialogRoot (which is a layout-level component). This keeps /preferences
       self-contained. -->
  <div class="pr-overlay" role="presentation" onclick={() => (showOnboardingConfirm = false)}>
    <div class="pr-modal" role="dialog" aria-modal="true" aria-labelledby="pr-modal-title"
         onclick={(e) => e.stopPropagation()}>
      <h3 id="pr-modal-title" class="pr-modal-title">Re-run onboarding wizard?</h3>
      <p class="pr-modal-body">
        This will show the setup wizard again from step 1.
        Your existing projects, secrets, and settings won't be changed.
      </p>
      <div class="pr-modal-actions">
        <button class="pr-btn" onclick={() => (showOnboardingConfirm = false)}>Cancel</button>
        <button class="pr-btn-primary" onclick={confirmRerunOnboarding}>Show wizard</button>
      </div>
    </div>
  </div>
{/if}

{#if showPatClearConfirm}
  <div class="pr-overlay" role="presentation" onclick={() => (showPatClearConfirm = false)}>
    <div class="pr-modal" role="dialog" aria-modal="true" aria-labelledby="pr-pat-clear-title"
         onclick={(e) => e.stopPropagation()}>
      <h3 id="pr-pat-clear-title" class="pr-modal-title">Clear GitHub token?</h3>
      <p class="pr-modal-body">
        Removes the token from your OS keychain and strips <code>GITHUB_TOKEN</code>
        from every registered project's env files on the next refresh. The
        <code>~/.vct-secrets/shared/github_pat</code> file (if any) is left untouched —
        delete it manually if you want it gone too.
      </p>
      <div class="pr-modal-actions">
        <button class="pr-btn" onclick={() => (showPatClearConfirm = false)}>Cancel</button>
        <button class="pr-btn-primary" onclick={clearPat}>Clear token</button>
      </div>
    </div>
  </div>
{/if}

<!-- v0.2.18 Commit 7: Clear-OpenAI-key confirmation modal. -->
{#if openaiShowClearConfirm}
  <div class="pr-overlay" role="presentation" onclick={() => (openaiShowClearConfirm = false)}>
    <div class="pr-modal" role="dialog" aria-modal="true" tabindex="-1"
         aria-labelledby="pr-openai-clear-title"
         onclick={(e) => e.stopPropagation()}>
      <h3 id="pr-openai-clear-title" class="pr-modal-title">Clear OpenAI key?</h3>
      <p class="pr-modal-body">
        Removes the API key from your OS keychain and clears the recovery
        state-machine breadcrumbs (<code>openai_was_valid</code>,
        <code>openai_fallback_pending</code>). Your default embedding
        preferences are left alone — change them via the dropdowns above
        if you want to revert to local models.
      </p>
      <div class="pr-modal-actions">
        <button class="pr-btn" onclick={() => (openaiShowClearConfirm = false)}>Cancel</button>
        <button class="pr-btn-primary" onclick={() => void clearOpenAi()}>Clear key</button>
      </div>
    </div>
  </div>
{/if}

<!--
  v0.2.18 Commit 7: styled "Set OpenAI as default?" confirmation modal.

  Replaces the native window.confirm() previously staged by Commit 8 at
  the wave-handoff seam. Opens after `applyOpenAiKey` gets a `valid`
  result; the modal's Yes button is the ONLY path that calls
  `set_default_embedding_models` with openai-* ids from this surface.

  ESC and click-outside both close with `false` (decline). Tab cycles
  between the two action buttons via the `focusTrap` action.
-->
{#if openaiConfirm.open}
  <div
    class="pr-overlay"
    role="presentation"
    onclick={() => closeOpenaiConfirm(false)}
    onkeydown={handleOpenaiConfirmKey}
  >
    <div
      class="pr-modal"
      role="dialog"
      aria-modal="true"
      tabindex="-1"
      aria-labelledby="pr-openai-confirm-title"
      aria-describedby="pr-openai-confirm-body"
      onclick={(e) => e.stopPropagation()}
      use:focusTrap
    >
      <h3 id="pr-openai-confirm-title" class="pr-modal-title">
        Set OpenAI as default?
      </h3>
      <p id="pr-openai-confirm-body" class="pr-modal-body">
        Key valid. Would you like to set OpenAI as default for new projects?<br /><br />
        This will set:<br />
        &nbsp;&nbsp;• Default text embedding → <code>{openaiConfirm.textModelId}</code><br />
        &nbsp;&nbsp;• Default code embedding → <code>{openaiConfirm.codeModelId}</code><br /><br />
        You can change these any time from this page.
      </p>
      <div class="pr-modal-actions">
        <button class="pr-btn" onclick={() => closeOpenaiConfirm(false)}>
          No, keep current
        </button>
        <button
          class="pr-btn-primary"
          onclick={() => closeOpenaiConfirm(true)}
          use:focusOnMount
        >
          Yes, set as default
        </button>
      </div>
    </div>
  </div>
{/if}

<Toast />

<style>
  .pr-page { min-height: 100vh; background: var(--color-bg, #0e0e16); color: var(--color-light, #e8e8ee); }
  .pr-header { display: flex; align-items: center; gap: 12px; padding: 10px 24px; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .pr-header h1 { font-size: 16px; margin: 0; }
  .pr-back { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
  .pr-empty { padding: 40px; text-align: center; color: #888; }
  .pr-main { max-width: 720px; margin: 0 auto; padding: 16px; }
  .pr-hint { font-size: 11px; color: #888; margin: 0 0 14px; line-height: 1.5; }
  .pr-hint code { background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; }
  .pr-list { list-style: none; padding: 0; margin: 0; background: rgba(255,255,255,0.03); border-radius: 6px; }
  .pr-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 14px; border-bottom: 1px solid rgba(255,255,255,0.04);
    font-size: 12px;
  }
  .pr-row:last-child { border-bottom: none; }
  .pr-row strong { color: #ccc; font-weight: 500; }
  /* Bug 12 systemic: native <select> replaced with <Dropdown>. */
  .pr-dd { width: 260px; }
  /* v0.2.18 Commit 8: default-embedding-models section. */
  .pr-emb-warn {
    margin: 0 0 10px; padding: 8px 12px;
    background: rgba(255,200,80,0.08); border: 1px solid rgba(255,200,80,0.2);
    border-radius: 4px; color: rgb(255,200,120); font-size: 11px;
  }
  .pr-link-btn {
    background: none; border: none; color: rgb(0,191,166); cursor: pointer;
    padding: 0; margin-left: 8px; text-decoration: underline; font-size: 11px;
  }

  .pr-section { margin-top: 20px; }
  .pr-section-title { font-size: 11px; font-weight: 600; color: #888; text-transform: uppercase; letter-spacing: 0.07em; margin: 0 0 8px; }
  .pr-onboarding-row {
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    padding: 10px 14px; background: rgba(255,255,255,0.03); border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.06);
  }
  .pr-onboarding-text { display: flex; flex-direction: column; gap: 3px; }
  .pr-onboarding-text strong { font-size: 12px; color: #ccc; font-weight: 500; }
  .pr-onboarding-hint { font-size: 11px; color: #888; max-width: 480px; line-height: 1.5; }
  .pr-btn {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 5px 14px; border-radius: 4px; cursor: pointer;
    font-size: 12px; font-weight: 500; white-space: nowrap; flex-shrink: 0;
  }
  .pr-btn:hover { background: rgba(255,255,255,0.09); }
  .pr-btn-primary {
    background: rgb(0,191,166); border: 1px solid rgb(0,191,166);
    color: #000; padding: 5px 14px; border-radius: 4px; cursor: pointer;
    font-size: 12px; font-weight: 600; white-space: nowrap;
  }
  .pr-btn-primary:hover { background: rgb(0,210,183); }

  /* GitHub PAT section */
  .pr-pat-row {
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    padding: 10px 14px; background: rgba(255,255,255,0.03); border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.06);
  }
  .pr-pat-preview {
    margin-left: 8px; font-family: ui-monospace, monospace; font-size: 11px;
    color: #888; background: rgba(255,255,255,0.06); padding: 1px 6px; border-radius: 3px;
  }
  .pr-pat-actions { display: flex; gap: 6px; flex-shrink: 0; }
  .pr-btn-danger {
    background: rgba(229,77,77,0.12);
    border: 1px solid rgba(229,77,77,0.3);
    color: rgb(255,140,140);
  }
  .pr-btn-danger:hover { background: rgba(229,77,77,0.2); }
  .pr-pat-edit {
    margin-top: 8px; padding: 12px 14px; background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06); border-radius: 6px;
    display: flex; flex-direction: column; gap: 8px;
  }
  .pr-pat-input {
    background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1);
    color: #e8e8ee; padding: 6px 10px; border-radius: 4px; font-size: 12px;
    font-family: ui-monospace, monospace;
  }
  .pr-pat-edit-actions { display: flex; gap: 6px; justify-content: flex-end; }
  .pr-pat-hint { font-size: 11px; color: #888; line-height: 1.5; margin: 0; }
  .pr-pat-hint code {
    background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px;
    font-size: 10.5px;
  }
  .pr-error {
    margin-top: 8px; padding: 8px 12px;
    background: rgba(229,77,77,0.1); border: 1px solid rgba(229,77,77,0.25);
    border-radius: 4px; color: rgb(255,140,140); font-size: 11px;
  }

  /* Confirmation modal */
  .pr-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.55);
    display: flex; align-items: center; justify-content: center; z-index: 9000;
  }
  .pr-modal {
    background: #1a1a26; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px;
    padding: 20px 24px; max-width: 400px; width: 100%;
  }
  .pr-modal-title { font-size: 14px; font-weight: 600; color: #e8e8ee; margin: 0 0 10px; }
  .pr-modal-body { font-size: 12px; color: #888; line-height: 1.6; margin: 0 0 16px; }
  .pr-modal-body code {
    background: rgba(255,255,255,0.06); padding: 1px 5px; border-radius: 3px;
    font-size: 11px; color: #ccc;
  }
  .pr-modal-actions { display: flex; gap: 8px; justify-content: flex-end; }

  /* OpenAI API key section (v0.2.18 Commit 7) */
  .pr-openai-label {
    display: block; font-size: 11px; color: #888; margin-bottom: 4px;
  }
  .pr-openai-input-row {
    display: flex; gap: 6px; align-items: center;
  }
  .pr-openai-input { flex: 1; min-width: 0; }
  .pr-openai-show-btn { flex-shrink: 0; }
  .pr-openai-status {
    display: flex; align-items: flex-start; gap: 8px;
    margin: 8px 0 0; padding: 6px 10px; border-radius: 4px;
    font-size: 11.5px; line-height: 1.5;
  }
  .pr-openai-status code {
    background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px;
    font-size: 11px; color: #ccc;
  }
  .pr-openai-status-icon {
    font-family: ui-monospace, monospace; font-weight: 700;
    flex-shrink: 0; min-width: 14px; text-align: center;
  }
  /* tier: grey = idle / unvalidated / working */
  .pr-openai-status-idle,
  .pr-openai-status-unvalidated {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.06);
    color: #888;
  }
  .pr-openai-status-working {
    background: rgba(120,180,255,0.08);
    border: 1px solid rgba(120,180,255,0.2);
    color: rgb(160,200,255);
  }
  /* tier: green = valid */
  .pr-openai-status-valid {
    background: rgba(0,191,166,0.1);
    border: 1px solid rgba(0,191,166,0.3);
    color: rgb(120,220,180);
  }
  /* tier: yellow = previously valid, currently failing */
  .pr-openai-status-previously_valid_failing {
    background: rgba(255,200,80,0.1);
    border: 1px solid rgba(255,200,80,0.3);
    color: rgb(255,210,140);
  }
  /* tier: red = invalid / error */
  .pr-openai-status-invalid,
  .pr-openai-status-error {
    background: rgba(229,77,77,0.1);
    border: 1px solid rgba(229,77,77,0.25);
    color: rgb(255,140,140);
  }

  /* Hardware re-detection (Bug B, v0.2.5) */
  .pr-hw-card {
    padding: 12px 14px; background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06); border-radius: 6px;
    display: flex; flex-direction: column; gap: 12px;
  }
  .pr-hw-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
  .pr-hw-empty { font-size: 11px; color: #888; margin: 0; }
  .pr-hw-grid {
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px 16px; font-size: 11.5px;
  }
  .pr-hw-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .pr-hw-label { color: #888; }
  .pr-hw-value { color: #ccc; font-family: ui-monospace, monospace; }
  .pr-hw-diff {
    padding: 10px 12px; background: rgba(255,200,80,0.06);
    border: 1px solid rgba(255,200,80,0.2); border-radius: 6px;
    display: flex; flex-direction: column; gap: 8px;
  }
  .pr-hw-diff-title { font-size: 12px; font-weight: 600; color: rgb(255,200,120); margin: 0; }
  .pr-hw-diff-list { list-style: none; padding: 0; margin: 0; font-size: 11.5px; }
  .pr-hw-diff-row {
    display: grid; grid-template-columns: 1fr auto auto auto;
    gap: 8px; align-items: center; padding: 3px 0;
  }
  .pr-hw-diff-field { color: #ccc; font-family: ui-monospace, monospace; }
  .pr-hw-diff-before { color: #888; font-family: ui-monospace, monospace; text-decoration: line-through; }
  .pr-hw-diff-arrow { color: #666; }
  .pr-hw-diff-after { color: rgb(0,191,166); font-family: ui-monospace, monospace; font-weight: 500; }
  .pr-hw-log {
    padding: 10px 12px; background: rgba(0,0,0,0.4);
    border: 1px solid rgba(255,255,255,0.06); border-radius: 6px;
    display: flex; flex-direction: column; gap: 6px;
  }
  .pr-hw-log-header { display: flex; align-items: center; justify-content: space-between; }
  .pr-hw-log-header strong { font-size: 11.5px; color: #ccc; font-weight: 500; }
  .pr-hw-log-ok { font-size: 11px; color: rgb(120,220,180); }
  .pr-hw-log-fail { font-size: 11px; color: rgb(255,140,140); }
  .pr-hw-log-body {
    margin: 0; padding: 8px 10px; background: rgba(0,0,0,0.4);
    border-radius: 4px; font-family: ui-monospace, monospace; font-size: 10.5px;
    color: #ccc; max-height: 240px; overflow: auto; white-space: pre-wrap; word-break: break-word;
  }
</style>

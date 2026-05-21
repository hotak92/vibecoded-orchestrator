<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { getVersion } from '@tauri-apps/api/app';
  import { goto } from '$app/navigation';
  import { invoke, safeInvoke, listen as tauriListen } from '$lib/tauri';
  import { selectedProject } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import { ui } from '$lib/stores/ui';
  // v0.2.23 F2 wave 2b (2026-05-21): Profile + Downloads sections
  // relocated from the now-deleted user-icon Settings popover. The
  // `auth` store handles the Supabase profile write; the `settings`
  // store handles the per-machine launcher prefs (install path,
  // auto-update, launch-on-startup) via localStorage.
  import { auth, currentUser } from '$lib/stores/auth';
  import { settings } from '$lib/stores/settings';
  import Toast from '$lib/components/Toast.svelte';
  import Dropdown from '$lib/components/Dropdown.svelte';
  import { focusOnMount, focusTrap } from '$lib/actions/focusManagement';
  import type {
    EmbeddingCatalog,
    ModelChoice,
    DefaultEmbeddingModels,
  } from '$lib/types/embedding-catalog';
  import type { TelemetryStatus, ConsentFlags } from '$lib/types/project-state';

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
  // Mirror of the Rust `HardwareSnapshot` (launcher/src-tauri/src/commands/
  // installer.rs). Extra fields beyond the original v0.2.8 shape are
  // optional in the TS interface because pre-v0.2.9 snapshots persisted
  // before these fields existed and serde defaults them on read.
  // - vram_gb / gpu_mode_decided: v0.2.9 (Bug K, VRAM threshold)
  // - has_amd_gpu:                v0.2.20 (AMD/ROCm support)
  interface HardwareSnapshot {
    has_nvidia_gpu: boolean;
    gpu_name: string;
    has_apple_silicon: boolean;
    ram_gb: number;
    use_gpu: boolean;
    low_resource: boolean;
    vram_gb?: number;
    has_amd_gpu?: boolean;
    gpu_mode_decided?: 'cuda' | 'rocm' | 'cpu' | 'metal';
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
      case 'has_amd_gpu': return snap.has_amd_gpu ? 'yes' : 'no';
      case 'gpu_name': return snap.gpu_name || '(none)';
      case 'has_apple_silicon': return snap.has_apple_silicon ? 'yes' : 'no';
      case 'ram_gb': return `${snap.ram_gb} GB`;
      case 'vram_gb':
        if (snap.vram_gb === undefined || snap.vram_gb === null) return '—';
        return snap.vram_gb > 0 ? `${snap.vram_gb.toFixed(1)} GB` : '(none)';
      case 'gpu_mode_decided':
        // 'cuda' / 'rocm' / 'cpu' / 'metal' — the wire string from the
        // Rust GpuMode enum (lowercase serde). v0.2.20 added rocm.
        switch (snap.gpu_mode_decided) {
          case 'cuda': return 'CUDA (NVIDIA)';
          case 'rocm': return 'ROCm (AMD)';
          case 'metal': return 'Metal (Apple Silicon)';
          case 'cpu': return 'CPU-only';
          default: return '—';
        }
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

  // Focus management for the "Set OpenAI as default?" confirm modal —
  // `focusOnMount` (primary-action autofocus) + `focusTrap` (Tab/Shift+Tab
  // cycling) are imported from `$lib/actions/focusManagement` so other
  // modals in the launcher can share the same a11y semantics. Lifted out
  // in v0.2.18 cleanup commit; original implementation landed inline
  // here with Commit 7.

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

  // ── Local data collection (Stream 1, v0.2.20) ────────────────────
  // Three controls for the RL retrieval data pipeline:
  //   1. "Collect retrieval data locally" toggle → writes
  //      RL_LOCAL_LOGGING_DISABLED=true to <project>/.claude/env when
  //      OFF. Default ON (telemetry-on-by-default for free tier).
  //   2. "Upload anonymized data" toggle → telemetry_set_consent
  //      flips ConsentFlags.rl_data.
  //   3. "Clear local cache" button → telemetry_clear_rl_local_cache.
  // All three carry mouseover tooltips explaining the data flow.
  const RL_LOCAL_OFF_KEY = 'RL_LOCAL_LOGGING_DISABLED';
  // Per-project: whether the local logger is currently disabled by
  // the .claude/env override. Default = enabled.
  let rlLocalLoggingDisabled = $state(false);
  let rlLocalLoggingSaving = $state(false);
  // Cross-project: upload consent (lives in ~/.vibecoded/config.json).
  let rlUploadConsent = $state(false);
  let rlUploadSaving = $state(false);
  let rlConsentFlags = $state<ConsentFlags | null>(null);
  // Clear-cache button state.
  let rlCacheClearing = $state(false);

  async function loadRlLocalState() {
    const projectId = $selectedProject?.id;
    if (!projectId) return;
    try {
      const v = await invoke<string | null>('get_claude_env_value', {
        projectId,
        key: RL_LOCAL_OFF_KEY,
      });
      // Truthy → disabled. Anything else → enabled (the default).
      rlLocalLoggingDisabled =
        v !== null && v !== undefined &&
        ['true', '1', 'yes', 'on'].includes((v || '').toLowerCase());
    } catch (e) {
      // Soft-fail: leave the toggle in its default rendered state.
      console.debug('loadRlLocalState failed', e);
    }
  }

  async function loadRlUploadConsent() {
    try {
      const status = await safeInvoke<TelemetryStatus>('telemetry_status');
      if (status) {
        rlConsentFlags = status.consent;
        rlUploadConsent = !!status.consent.rl_data;
      }
    } catch (e) {
      console.debug('loadRlUploadConsent failed', e);
    }
  }

  async function toggleRlLocalLogging() {
    const projectId = $selectedProject?.id;
    if (!projectId) {
      toast.error('Pick a project first');
      return;
    }
    rlLocalLoggingSaving = true;
    const next = !rlLocalLoggingDisabled;
    try {
      // OFF in UI = disabled flag SET (write "true"); ON = remove the key.
      const value = next ? 'true' : null;
      await invoke('set_claude_env_value', {
        projectId,
        key: RL_LOCAL_OFF_KEY,
        value,
      });
      rlLocalLoggingDisabled = next;
      toast.success(
        next
          ? 'Local retrieval data collection paused for this project'
          : 'Local retrieval data collection enabled for this project',
      );
    } catch (e) {
      toast.error(e);
    } finally {
      rlLocalLoggingSaving = false;
    }
  }

  async function toggleRlUploadConsent() {
    if (!rlConsentFlags) {
      // Build a default; telemetry_set_consent fills missing fields.
      rlConsentFlags = {
        consent_version: '1.0',
        granted_at: null,
        always_on: true,
        rl_data: false,
        routing_data: false,
        instinct_data: false,
        hardware: false,
      };
    }
    rlUploadSaving = true;
    const next = !rlUploadConsent;
    const flags = { ...rlConsentFlags, rl_data: next } as ConsentFlags;
    try {
      const updated = await invoke<ConsentFlags>('telemetry_set_consent', { flags });
      rlConsentFlags = updated;
      rlUploadConsent = !!updated.rl_data;
      toast.success(
        next
          ? 'Upload consent granted — anonymized retrieval data will be uploaded'
          : 'Upload consent withdrawn — uploads stopped, local data unchanged',
      );
    } catch (e) {
      toast.error(e);
    } finally {
      rlUploadSaving = false;
    }
  }

  async function clearRlLocalCache() {
    if (!confirm(
      'Delete ALL local retrieval data logs in ~/.claude/retrieval_rl_data/?\n\n' +
      'This wipes the machine-wide training corpus (rl_events*.jsonl files).\n' +
      'The .v1.bak archives are preserved. This action cannot be undone.',
    )) {
      return;
    }
    rlCacheClearing = true;
    try {
      const res = await invoke<{ deleted_files: number; bytes_freed: number }>(
        'telemetry_clear_rl_local_cache',
      );
      const mb = (res.bytes_freed / 1024 / 1024).toFixed(1);
      toast.success(
        res.deleted_files === 0
          ? 'No local cache files to clear'
          : `Cleared ${res.deleted_files} file(s), freed ${mb} MB`,
      );
    } catch (e) {
      toast.error(e);
    } finally {
      rlCacheClearing = false;
    }
  }

  // ── KG Summaries (v0.2.23 F2, relocated from SettingsPanel) ──────────
  // Three app_state keys drive `templates/scripts/generate-kg-summary.py`:
  //   - `kg_summary_openai_consent` (bool) — gate for the OpenAI tier
  //   - `kg_summary_openai_model` (string) — which OpenAI model to use
  //   - `kg_summary_backend_override` (string, optional) — pin a tier
  //
  // v0.2.23 F3: also `kg_summary_ollama_model` (string) — when the
  // override is `ollama` (or auto-detect picks Ollama), this names the
  // local model. Populated from `http://localhost:11435/api/tags`.
  //
  // The OpenAI consent gate is shared with the F4 Code Graph Embeddings
  // section below — one consent decision unlocks both surfaces.
  const APP_STATE_KEY_KG_SUMMARY_CONSENT = 'kg_summary_openai_consent';
  const APP_STATE_KEY_KG_SUMMARY_MODEL = 'kg_summary_openai_model';
  const APP_STATE_KEY_KG_SUMMARY_OVERRIDE = 'kg_summary_backend_override';
  const APP_STATE_KEY_KG_SUMMARY_OLLAMA_MODEL = 'kg_summary_ollama_model';
  // Hardcoded allowlist of OpenAI chat models known to work as summary
  // backends. Ordered cheapest → most expensive so the default lands
  // first. Future work: fetch from /v1/models when the launcher has an
  // OpenAI client; for now this stays deterministic.
  const KG_SUMMARY_OPENAI_MODELS = [
    { id: 'gpt-4o-mini', label: 'gpt-4o-mini (cheapest, default)' },
    { id: 'gpt-4.1-mini', label: 'gpt-4.1-mini' },
    { id: 'gpt-4o', label: 'gpt-4o (more capable, higher cost)' },
  ];
  const KG_SUMMARY_DEFAULT_MODEL = 'gpt-4o-mini';

  type KgSummaryOverride = '' | 'cli' | 'ollama' | 'openai' | 'skip';
  let kgSummaryConsent = $state(false);
  let kgSummaryModel = $state<string>(KG_SUMMARY_DEFAULT_MODEL);
  let kgSummaryOverride = $state<KgSummaryOverride>('');
  let kgSummaryOllamaModel = $state<string>('');
  let kgSummaryLoading = $state(false);
  let kgSummarySaving = $state(false);
  let kgSummaryError = $state<string | null>(null);
  let kgSummarySaved = $state(false);

  // Locally-installed Ollama models, fetched once per page load from
  // http://localhost:11435/api/tags. Soft-fail: an empty list means
  // Ollama isn't running or has no models pulled; the dropdown then
  // shows a hint instead of options.
  let ollamaModels = $state<string[]>([]);
  let ollamaModelsLoading = $state(false);
  let ollamaModelsError = $state<string | null>(null);

  // Default Ollama URL. The launcher pins this to 11435 (not 11434) to
  // avoid collisions with users' pre-existing Ollama installs — see
  // CLAUDE.md "Default ports".
  const OLLAMA_URL = 'http://localhost:11435';

  /**
   * Fetch the list of locally-installed Ollama models. Returns just
   * the model names (e.g. `["qwen3.5:9b", "gemma4:e4b", "qwen3-embedding:0.6b"]`).
   * Shared between F3 (KG Summaries) and F4 (Code Graph Embeddings) so
   * we only fetch once per page render.
   */
  async function fetchOllamaModels() {
    if (ollamaModelsLoading) return;
    ollamaModelsLoading = true;
    ollamaModelsError = null;
    try {
      const resp = await fetch(`${OLLAMA_URL}/api/tags`);
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data: { models?: Array<{ name: string }> } = await resp.json();
      ollamaModels = (data.models ?? []).map((m) => m.name).sort();
    } catch (e) {
      // Soft-fail — Ollama not running or unreachable. The UI shows a
      // hint and the dropdowns become disabled.
      ollamaModelsError = String(e);
      ollamaModels = [];
    } finally {
      ollamaModelsLoading = false;
    }
  }

  async function loadKgSummarySettings() {
    kgSummaryLoading = true;
    kgSummaryError = null;
    try {
      const [consentRes, modelRes, overrideRes, ollamaRes] = await Promise.all([
        invoke<boolean | null>('app_state_get_bool', {
          key: APP_STATE_KEY_KG_SUMMARY_CONSENT,
        }),
        invoke<{ key: string; is_set: boolean; value: string | null }>(
          'app_state_get',
          { key: APP_STATE_KEY_KG_SUMMARY_MODEL },
        ),
        invoke<{ key: string; is_set: boolean; value: string | null }>(
          'app_state_get',
          { key: APP_STATE_KEY_KG_SUMMARY_OVERRIDE },
        ),
        invoke<{ key: string; is_set: boolean; value: string | null }>(
          'app_state_get',
          { key: APP_STATE_KEY_KG_SUMMARY_OLLAMA_MODEL },
        ),
      ]);
      kgSummaryConsent = consentRes === true;
      kgSummaryModel = (modelRes.is_set && modelRes.value)
        ? modelRes.value
        : KG_SUMMARY_DEFAULT_MODEL;
      const ov = (overrideRes.is_set ? (overrideRes.value ?? '') : '') as KgSummaryOverride;
      kgSummaryOverride = (['', 'cli', 'ollama', 'openai', 'skip'].includes(ov) ? ov : '') as KgSummaryOverride;
      kgSummaryOllamaModel = (ollamaRes.is_set && ollamaRes.value) ? ollamaRes.value : '';
    } catch (e) {
      kgSummaryError = String(e);
    } finally {
      kgSummaryLoading = false;
    }
  }

  async function saveKgSummaryConsent(next: boolean) {
    kgSummarySaving = true;
    kgSummaryError = null;
    kgSummarySaved = false;
    try {
      await invoke<void>('app_state_set_bool', {
        key: APP_STATE_KEY_KG_SUMMARY_CONSENT,
        value: next,
      });
      kgSummaryConsent = next;
      kgSummarySaved = true;
      setTimeout(() => { kgSummarySaved = false; }, 2000);
    } catch (e) {
      kgSummaryError = String(e);
    } finally {
      kgSummarySaving = false;
    }
  }

  async function saveKgSummaryModel(next: string) {
    kgSummarySaving = true;
    kgSummaryError = null;
    kgSummarySaved = false;
    try {
      await invoke<void>('app_state_set', {
        key: APP_STATE_KEY_KG_SUMMARY_MODEL,
        value: next,
      });
      kgSummaryModel = next;
      kgSummarySaved = true;
      setTimeout(() => { kgSummarySaved = false; }, 2000);
    } catch (e) {
      kgSummaryError = String(e);
    } finally {
      kgSummarySaving = false;
    }
  }

  async function saveKgSummaryOverride(next: KgSummaryOverride) {
    kgSummarySaving = true;
    kgSummaryError = null;
    kgSummarySaved = false;
    try {
      // Empty string clears the override (script treats "" as "auto").
      await invoke<void>('app_state_set', {
        key: APP_STATE_KEY_KG_SUMMARY_OVERRIDE,
        value: next,
      });
      kgSummaryOverride = next;
      kgSummarySaved = true;
      setTimeout(() => { kgSummarySaved = false; }, 2000);
    } catch (e) {
      kgSummaryError = String(e);
    } finally {
      kgSummarySaving = false;
    }
  }

  async function saveKgSummaryOllamaModel(next: string) {
    kgSummarySaving = true;
    kgSummaryError = null;
    kgSummarySaved = false;
    try {
      await invoke<void>('app_state_set', {
        key: APP_STATE_KEY_KG_SUMMARY_OLLAMA_MODEL,
        value: next,
      });
      kgSummaryOllamaModel = next;
      kgSummarySaved = true;
      setTimeout(() => { kgSummarySaved = false; }, 2000);
    } catch (e) {
      kgSummaryError = String(e);
    } finally {
      kgSummarySaving = false;
    }
  }

  // ── Code Graph Embeddings (v0.2.23 F4) ───────────────────────────────
  // Mirror of the KG Summaries pattern. Three new app_state keys:
  //   - `code_embed_backend_override` (string, optional) —
  //     "" | "auto" | "codesage" | "qwen3" | "jina" | "openai" | "ollama"
  //   - `code_embed_openai_model` (string) — OpenAI embedding model id
  //     when the override is `openai`. Defaults to text-embedding-3-small.
  //   - `code_embed_ollama_model` (string) — Ollama model name when the
  //     override is `ollama`. Default empty (no auto-pick — user picks
  //     from the dropdown).
  //
  // The "detected backend" is read-only and reflects the install-time
  // `default_code_embedding` row that `install.py` writes during
  // hardware selection. We pull it from the existing
  // `get_default_embedding_models` Tauri command (no new backend code).
  const APP_STATE_KEY_CODE_EMBED_OVERRIDE = 'code_embed_backend_override';
  const APP_STATE_KEY_CODE_EMBED_OPENAI_MODEL = 'code_embed_openai_model';
  const APP_STATE_KEY_CODE_EMBED_OLLAMA_MODEL = 'code_embed_ollama_model';
  // OpenAI embedding models that have been validated against the
  // EmbeddingService (vco_lib/embedding_service.py). Same ordering
  // logic as the KG Summary list — cheapest first.
  const CODE_EMBED_OPENAI_MODELS = [
    { id: 'text-embedding-3-small', label: 'text-embedding-3-small (1536-dim, cheapest, default)' },
    { id: 'text-embedding-3-large', label: 'text-embedding-3-large (3072-dim, higher quality)' },
  ];
  const CODE_EMBED_OPENAI_DEFAULT_MODEL = 'text-embedding-3-small';

  type CodeEmbedOverride = '' | 'auto' | 'codesage' | 'qwen3' | 'jina' | 'openai' | 'ollama';
  let codeEmbedOverride = $state<CodeEmbedOverride>('');
  let codeEmbedOpenaiModel = $state<string>(CODE_EMBED_OPENAI_DEFAULT_MODEL);
  let codeEmbedOllamaModel = $state<string>('');
  let codeEmbedLoading = $state(false);
  let codeEmbedSaving = $state(false);
  let codeEmbedError = $state<string | null>(null);
  let codeEmbedSaved = $state(false);

  async function loadCodeEmbedSettings() {
    codeEmbedLoading = true;
    codeEmbedError = null;
    try {
      const [overrideRes, openaiModelRes, ollamaModelRes] = await Promise.all([
        invoke<{ key: string; is_set: boolean; value: string | null }>(
          'app_state_get',
          { key: APP_STATE_KEY_CODE_EMBED_OVERRIDE },
        ),
        invoke<{ key: string; is_set: boolean; value: string | null }>(
          'app_state_get',
          { key: APP_STATE_KEY_CODE_EMBED_OPENAI_MODEL },
        ),
        invoke<{ key: string; is_set: boolean; value: string | null }>(
          'app_state_get',
          { key: APP_STATE_KEY_CODE_EMBED_OLLAMA_MODEL },
        ),
      ]);
      const ov = (overrideRes.is_set ? (overrideRes.value ?? '') : '') as CodeEmbedOverride;
      const allowed: CodeEmbedOverride[] = ['', 'auto', 'codesage', 'qwen3', 'jina', 'openai', 'ollama'];
      codeEmbedOverride = (allowed.includes(ov) ? ov : '') as CodeEmbedOverride;
      codeEmbedOpenaiModel = (openaiModelRes.is_set && openaiModelRes.value)
        ? openaiModelRes.value
        : CODE_EMBED_OPENAI_DEFAULT_MODEL;
      codeEmbedOllamaModel = (ollamaModelRes.is_set && ollamaModelRes.value) ? ollamaModelRes.value : '';
    } catch (e) {
      codeEmbedError = String(e);
    } finally {
      codeEmbedLoading = false;
    }
  }

  async function saveCodeEmbedOverride(next: CodeEmbedOverride) {
    codeEmbedSaving = true;
    codeEmbedError = null;
    codeEmbedSaved = false;
    try {
      await invoke<void>('app_state_set', {
        key: APP_STATE_KEY_CODE_EMBED_OVERRIDE,
        value: next,
      });
      codeEmbedOverride = next;
      codeEmbedSaved = true;
      setTimeout(() => { codeEmbedSaved = false; }, 2000);
    } catch (e) {
      codeEmbedError = String(e);
    } finally {
      codeEmbedSaving = false;
    }
  }

  async function saveCodeEmbedOpenaiModel(next: string) {
    codeEmbedSaving = true;
    codeEmbedError = null;
    codeEmbedSaved = false;
    try {
      await invoke<void>('app_state_set', {
        key: APP_STATE_KEY_CODE_EMBED_OPENAI_MODEL,
        value: next,
      });
      codeEmbedOpenaiModel = next;
      codeEmbedSaved = true;
      setTimeout(() => { codeEmbedSaved = false; }, 2000);
    } catch (e) {
      codeEmbedError = String(e);
    } finally {
      codeEmbedSaving = false;
    }
  }

  async function saveCodeEmbedOllamaModel(next: string) {
    codeEmbedSaving = true;
    codeEmbedError = null;
    codeEmbedSaved = false;
    try {
      await invoke<void>('app_state_set', {
        key: APP_STATE_KEY_CODE_EMBED_OLLAMA_MODEL,
        value: next,
      });
      codeEmbedOllamaModel = next;
      codeEmbedSaved = true;
      setTimeout(() => { codeEmbedSaved = false; }, 2000);
    } catch (e) {
      codeEmbedError = String(e);
    } finally {
      codeEmbedSaving = false;
    }
  }

  // ── Profile (v0.2.23 F2 wave 2b, relocated from SettingsPanel) ────────
  // Display name lives in Supabase `profiles.name`; email is read-only
  // here (Supabase requires its own confirmation flow to change). The
  // `auth.updateProfile()` action is the only mutation surface.
  let editName = $state($currentUser?.name ?? '');
  let profileSaved = $state(false);
  // Keep `editName` in sync if Supabase pushes a new profile name
  // (e.g. another session updated it). $effect auto-unsubscribes on
  // component destroy — replaces the popover-era .subscribe() pattern
  // which leaked one subscription per /preferences mount.
  $effect(() => {
    if ($currentUser) editName = $currentUser.name;
  });

  async function saveProfile() {
    if (!editName.trim()) return;
    await auth.updateProfile(editName.trim());
    profileSaved = true;
    setTimeout(() => { profileSaved = false; }, 2000);
  }

  // ── Downloads / Install (v0.2.23 F2 wave 2b, relocated) ───────────────
  // Three per-machine knobs that the `settings` store persists to
  // localStorage (see $lib/stores/settings.ts). Local mirrors are kept
  // in sync with the store via subscribe so updates from anywhere flow
  // back into the inputs.
  let installPath = $state('');
  let autoUpdate = $state(true);
  let launchOnStartup = $state(false);

  // $effect auto-unsubscribes on component destroy. Replaces the
  // popover-era .subscribe() pattern that leaked one subscription per
  // /preferences mount.
  $effect(() => {
    installPath = $settings.installPath;
    autoUpdate = $settings.autoUpdate;
    launchOnStartup = $settings.launchOnStartup;
  });

  // ── Shared services live status (v0.2.23 F2 wave 2b, relocated) ───────
  // Read-only probe of the per-machine Weaviate / Ollama / code_embed
  // instances every orchestrator install reuses (per-install isolation
  // happens via KG_COLLECTION namespacing, not separate containers).
  interface ServicesStatus {
    weaviate_url: string | null;
    ollama_url: string | null;
    code_embed_url: string | null;
    all_detected: boolean;
    none_detected: boolean;
  }
  let services = $state<ServicesStatus | null>(null);
  let servicesLoading = $state(false);
  let servicesError = $state<string | null>(null);

  async function refreshServices() {
    servicesLoading = true;
    servicesError = null;
    try {
      services = await invoke<ServicesStatus>('detect_existing_services');
    } catch (e) {
      servicesError = String(e);
    } finally {
      servicesLoading = false;
    }
  }

  // ── Embedding profile / ACTIVE_EMBEDDING (v0.2.23 F2 wave 2b) ─────────
  // GLOBAL knob, distinct from the per-new-project `default_text_embedding`
  // / `default_code_embedding` rows further up the page. Backed by
  // `app_state` key `embedding.active_profile`; flows into per-project
  // envs via `ProjectEnvSettings::populate` (see
  // commands/project_env_settings.rs).
  type EmbeddingProfile = 'qwen3' | 'arctic' | 'codesage' | 'openai';
  const APP_STATE_KEY_ACTIVE_EMBEDDING = 'embedding.active_profile';
  const EMBEDDING_DEFAULT: EmbeddingProfile = 'qwen3';
  let activeEmbedding = $state<EmbeddingProfile>(EMBEDDING_DEFAULT);
  let activeEmbeddingLoading = $state(false);
  let activeEmbeddingSaving = $state(false);
  let activeEmbeddingError = $state<string | null>(null);
  let activeEmbeddingSaved = $state(false);

  async function loadActiveEmbedding() {
    activeEmbeddingLoading = true;
    activeEmbeddingError = null;
    try {
      const res = await invoke<{ key: string; is_set: boolean; value: string | null }>(
        'app_state_get',
        { key: APP_STATE_KEY_ACTIVE_EMBEDDING },
      );
      if (res.is_set && res.value) {
        const known: EmbeddingProfile[] = ['qwen3', 'arctic', 'codesage', 'openai'];
        if ((known as string[]).includes(res.value)) {
          activeEmbedding = res.value as EmbeddingProfile;
        } else {
          // Defensive: a future profile name the UI doesn't know about
          // falls back to default rather than rendering a broken
          // `<select>` value.
          console.warn('Unknown active_embedding value, falling back to default:', res.value);
          activeEmbedding = EMBEDDING_DEFAULT;
        }
      } else {
        activeEmbedding = EMBEDDING_DEFAULT;
      }
    } catch (e) {
      activeEmbeddingError = String(e);
    } finally {
      activeEmbeddingLoading = false;
    }
  }

  async function saveActiveEmbedding(value: EmbeddingProfile) {
    activeEmbeddingSaving = true;
    activeEmbeddingError = null;
    activeEmbeddingSaved = false;
    try {
      await invoke<void>('app_state_set', {
        key: APP_STATE_KEY_ACTIVE_EMBEDDING,
        value,
      });
      activeEmbedding = value;
      activeEmbeddingSaved = true;
      setTimeout(() => { activeEmbeddingSaved = false; }, 2000);
    } catch (e) {
      activeEmbeddingError = String(e);
    } finally {
      activeEmbeddingSaving = false;
    }
  }

  // ── Volume location (v0.2.23 F2 wave 2b, relocated) ───────────────────
  // Container data location for Weaviate / Ollama / code-embed. Migration
  // is a two-step flow: dry-run plan → user confirms → backend copies,
  // verifies health, removes legacy volumes. Phase progress streams via
  // `volumes://migrate-progress` events from the Rust side (see
  // commands/volumes.rs::MigratePhase).
  interface VolumeWithSize {
    name: string;
    mountpoint: string;
    size_bytes: number | null;
    size_human: string | null;
    role: string;
  }
  interface VolumesConfig {
    volumes_path: string;
    mode: string;
    legacy_mapping: { volume_name: string; mountpoint: string; role: string }[];
    total_size_human: string | null;
    volumes: VolumeWithSize[];
  }
  interface MigrationPlan {
    from_mode: string;
    to_path: string;
    volumes_to_copy: VolumeWithSize[];
    total_bytes: number;
    total_human: string;
    estimated_seconds: number;
    free_bytes_at_target: number | null;
    insufficient_free_space: boolean;
    warnings: string[];
  }
  interface MigratePhaseEvent {
    phase:
      | 'stopping_containers'
      | { copying_volume: { volume_role: string; index: number; total: number } }
      | 'writing_override'
      | 'starting_containers'
      | 'waiting_for_health'
      | 'removing_legacy_volumes'
      | 'done'
      | { rolling_back: { reason: string } };
    message: string;
  }
  let volumesConfig = $state<VolumesConfig | null>(null);
  let volumesLoading = $state(false);
  let volumesError = $state<string | null>(null);
  let migratingVolumes = $state(false);
  let migratePath = $state('');
  let migrationPlan = $state<MigrationPlan | null>(null);
  let migrationError = $state<string | null>(null);
  let migrationPhaseLabel = $state<string | null>(null);
  let migrationCopyProgress = $state<{ index: number; total: number } | null>(null);

  async function refreshVolumes() {
    volumesLoading = true;
    volumesError = null;
    try {
      volumesConfig = await invoke<VolumesConfig>('get_volumes_config');
    } catch (e) {
      volumesError = String(e);
    } finally {
      volumesLoading = false;
    }
  }

  async function startMigrationDryRun() {
    if (!migratePath.trim()) {
      migrationError = 'Pick a target path first.';
      return;
    }
    migrationError = null;
    try {
      migrationPlan = await invoke<MigrationPlan>('set_volumes_config_dry_run', {
        path: migratePath.trim(),
      });
    } catch (e) {
      migrationError = String(e);
    }
  }

  async function confirmMigration() {
    if (!migrationPlan) return;
    if (migrationPlan.insufficient_free_space) {
      migrationError = 'Insufficient free space at target — pick a larger volume.';
      return;
    }
    migratingVolumes = true;
    migrationError = null;
    migrationPhaseLabel = 'Starting…';
    migrationCopyProgress = null;

    // Subscribe to phase events for the duration of this call. listen()
    // is dynamically imported so the import doesn't pollute the top-
    // level namespace if Tauri's event API is unavailable in tests.
    const { listen } = await import('@tauri-apps/api/event');
    const unlisten = await listen<MigratePhaseEvent>(
      'volumes://migrate-progress',
      (ev) => {
        const { phase, message } = ev.payload;
        migrationPhaseLabel = message;
        if (typeof phase === 'object' && 'copying_volume' in phase) {
          migrationCopyProgress = {
            index: phase.copying_volume.index,
            total: phase.copying_volume.total,
          };
        } else {
          migrationCopyProgress = null;
        }
      }
    );

    try {
      await invoke('migrate_volumes', {
        path: migrationPlan.to_path,
        confirmed: true,
      });
      migrationPlan = null;
      migratePath = '';
      await refreshVolumes();
    } catch (e) {
      migrationError = String(e);
    } finally {
      unlisten();
      migratingVolumes = false;
      migrationPhaseLabel = null;
      migrationCopyProgress = null;
    }
  }

  function cancelMigration() {
    migrationPlan = null;
    migratePath = '';
    migrationError = null;
  }

  // ── About (v0.2.23 F2 wave 2b, relocated) ─────────────────────────────
  // Read-only application info. `getVersion()` returns the version baked
  // into Cargo.toml at build time. Soft-fail if Tauri's API is unavailable
  // (e.g. running the launcher in browser mode for dev).
  let appVersion = $state('');
  async function loadAppVersion() {
    try {
      appVersion = await getVersion();
    } catch {
      appVersion = '';
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
    // Stream 1: local data collection controls.
    void loadRlLocalState();
    void loadRlUploadConsent();
    // F2/F3/F4: KG summaries + code-embed override settings, plus the
    // Ollama tags probe shared between the two sections.
    void loadKgSummarySettings();
    void loadCodeEmbedSettings();
    void fetchOllamaModels();
    // F2 wave 2b: sections relocated from the user-icon Settings popover.
    void refreshServices();
    void loadActiveEmbedding();
    void refreshVolumes();
    void loadAppVersion();
  });
  $effect(() => { if (project) void load(); });
  $effect(() => { if ($selectedProject) void loadRlLocalState(); });

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

    <!-- v0.2.22 Item #13 (2026-05-20): global retrieval tuning. Five
         env-tunable thresholds (codegraph injection floor + four KG
         tier cutoffs). Stored in <vct_root_dir>/retrieval-tuning.toml
         and read by the hub's /config resolver so headless consumers
         (hooks, MCPs, scripts) see the same values shown here. -->
    <section class="pr-section">
      <h2 class="pr-section-title">Retrieval tuning</h2>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Codegraph + KG score thresholds</strong>
          <span class="pr-onboarding-hint">
            Tune the score-driven retrieval tiers used by the pre-edit
            context hook and the <code>hybrid_search</code> /
            <code>semantic_graph_search</code> MCPs. Five sliders:
            codegraph injection floor + four KG verbosity tier cutoffs.
            Defaults are calibrated — change only if you have evidence
            the floor is too tight or too loose for your corpus.
            <br /><br />
            Reference:
            <code>knowledge/concepts/score-driven-retrieval-tiers.md</code>.
          </span>
        </div>
        <button
          class="pr-btn"
          title="Open the score-threshold sliders for KG verbosity tiers and the codegraph injection floor"
          onclick={() => goto('/preferences/retrieval')}
        >
          Open
        </button>
      </div>
    </section>

    <!-- v0.2.23 F2 (2026-05-21): KG Summaries — relocated from the
         user-icon Settings popover. Controls the consent gate + model
         selection for `templates/scripts/generate-kg-summary.py`. The
         script reads these app_state keys directly via stdlib sqlite3
         so changes take effect on the next summary generation without
         restarting anything. F3 adds the local Ollama dropdown next
         to the existing OpenAI model picker. -->
    <section class="pr-section" aria-labelledby="pr-kgsum-title">
      <h2 class="pr-section-title" id="pr-kgsum-title">KG Summaries</h2>
      <p class="pr-hint" style="margin-bottom: 10px;">
        LLM-written descriptions and per-chunk summaries used by
        <code>hybrid_search</code>'s <code>detail="summary"</code> tier.
        Search still works without these — the raw KG content is always
        embedded — but summaries improve the score-driven retrieval
        tiers significantly.
      </p>

      {#if kgSummaryLoading}
        <p class="pr-hint">Loading…</p>
      {:else}
        <!-- Force-override radio. Lets the user pin a specific backend
             when auto-detect picks the wrong one (e.g. prefer Ollama
             even when the claude CLI is on PATH). -->
        <div class="pr-kgsum-block">
          <p class="pr-kgsum-block-label">Backend</p>
          <label class="pr-kgsum-radio">
            <input
              type="radio"
              name="kg-summary-override"
              value=""
              checked={kgSummaryOverride === ''}
              onchange={() => void saveKgSummaryOverride('')}
              disabled={kgSummarySaving}
            />
            <span>Auto (claude CLI &gt; local Ollama &gt; OpenAI w/ consent)</span>
          </label>
          <label class="pr-kgsum-radio">
            <input
              type="radio"
              name="kg-summary-override"
              value="cli"
              checked={kgSummaryOverride === 'cli'}
              onchange={() => void saveKgSummaryOverride('cli')}
              disabled={kgSummarySaving}
            />
            <span>Claude CLI (best quality; uses your subscription)</span>
          </label>
          <label class="pr-kgsum-radio">
            <input
              type="radio"
              name="kg-summary-override"
              value="ollama"
              checked={kgSummaryOverride === 'ollama'}
              onchange={() => void saveKgSummaryOverride('ollama')}
              disabled={kgSummarySaving}
            />
            <span>Local Ollama (no cost)</span>
          </label>
          <label class="pr-kgsum-radio">
            <input
              type="radio"
              name="kg-summary-override"
              value="openai"
              checked={kgSummaryOverride === 'openai'}
              onchange={() => void saveKgSummaryOverride('openai')}
              disabled={kgSummarySaving || !kgSummaryConsent}
            />
            <span>OpenAI API (requires consent below; cost per summary)</span>
          </label>
          <label class="pr-kgsum-radio">
            <input
              type="radio"
              name="kg-summary-override"
              value="skip"
              checked={kgSummaryOverride === 'skip'}
              onchange={() => void saveKgSummaryOverride('skip')}
              disabled={kgSummarySaving}
            />
            <span>Skip (no summaries; saves cost / compute)</span>
          </label>
        </div>

        <!-- OpenAI consent gate. Shared with the F4 Code Graph
             Embeddings section below — flipping this enables BOTH the
             KG summary OpenAI radio AND the F4 OpenAI override. -->
        <div class="pr-kgsum-block">
          <label class="pr-kgsum-checkbox">
            <input
              type="checkbox"
              checked={kgSummaryConsent}
              onchange={(e) => {
                const t = e.currentTarget as HTMLInputElement;
                void saveKgSummaryConsent(t.checked);
              }}
              disabled={kgSummarySaving || !openaiPresent}
            />
            <span>Allow OpenAI for summaries &amp; embeddings (incurs cost)</span>
          </label>
          {#if !openaiPresent}
            <p class="pr-hint pr-kgsum-warn">
              No OpenAI API key configured — set one in the
              <strong>OpenAI API key</strong> section below to enable
              this option.
            </p>
          {:else if kgSummaryConsent}
            <p class="pr-hint pr-kgsum-warn">
              Heads-up: each summary or embedding triggers an API call
              against the model picked below. Cost is small per call
              (≈$0.0001 with gpt-4o-mini, ≈$0.005 with gpt-4o) but adds
              up across a large KG. Switch to Claude CLI or Local
              Ollama any time to stop the spend.
            </p>
          {/if}
        </div>

        <!-- OpenAI model picker. Only meaningful when consent is
             granted. -->
        {#if kgSummaryConsent}
          <div class="pr-kgsum-block">
            <label for="pr-kgsum-openai-model" class="pr-kgsum-label">
              OpenAI model
            </label>
            <select
              id="pr-kgsum-openai-model"
              class="pr-kgsum-select"
              value={kgSummaryModel}
              onchange={(e) => {
                const t = e.currentTarget as HTMLSelectElement;
                void saveKgSummaryModel(t.value);
              }}
              disabled={kgSummarySaving}
            >
              {#each KG_SUMMARY_OPENAI_MODELS as m}
                <option value={m.id}>{m.label}</option>
              {/each}
            </select>
          </div>
        {/if}

        <!-- F3: local Ollama model picker. Reads ollamaModels populated
             on mount via `fetchOllamaModels`. Stays visible (even when
             the override isn't "ollama") so the user can pre-select a
             model before switching the override — same UX shape as the
             OpenAI model picker. -->
        <div class="pr-kgsum-block">
          <label for="pr-kgsum-ollama-model" class="pr-kgsum-label">
            Local Ollama model
          </label>
          {#if ollamaModelsLoading}
            <p class="pr-hint">Probing Ollama at {OLLAMA_URL}…</p>
          {:else if ollamaModelsError}
            <p class="pr-hint pr-kgsum-warn">
              Couldn't reach Ollama at <code>{OLLAMA_URL}</code> ({ollamaModelsError}).
              Start the Ollama container from the Services page, then
              <button class="pr-link-btn" onclick={() => void fetchOllamaModels()}>retry</button>.
            </p>
          {:else if ollamaModels.length === 0}
            <p class="pr-hint pr-kgsum-warn">
              No Ollama models installed. Run
              <code>ollama pull qwen3.5:9b</code> (16 GB+ VRAM) or
              <code>ollama pull gemma4:e4b</code> (low-VRAM / CPU) then
              <button class="pr-link-btn" onclick={() => void fetchOllamaModels()}>retry</button>.
            </p>
          {:else}
            <select
              id="pr-kgsum-ollama-model"
              class="pr-kgsum-select"
              value={kgSummaryOllamaModel}
              onchange={(e) => {
                const t = e.currentTarget as HTMLSelectElement;
                void saveKgSummaryOllamaModel(t.value);
              }}
              disabled={kgSummarySaving}
            >
              <option value="">— Auto-select —</option>
              {#each ollamaModels as name}
                <option value={name}>{name}</option>
              {/each}
            </select>
          {/if}
        </div>

        {#if kgSummarySaving}
          <p class="pr-hint">Saving…</p>
        {:else if kgSummarySaved}
          <p class="pr-hint pr-kgsum-saved">Saved!</p>
        {/if}
        {#if kgSummaryError}
          <p class="pr-error">Couldn't save: {kgSummaryError}</p>
        {/if}
      {/if}
    </section>

    <!-- v0.2.23 F4 (2026-05-21): Code Graph Embeddings. Mirrors the
         KG-Summaries pattern above — same radio + consent + Ollama
         dropdown layout, different app_state keys. Read-only "detected"
         row reflects `default_code_embedding` (set by install.py during
         hardware selection). -->
    <section class="pr-section" aria-labelledby="pr-codeembed-title">
      <h2 class="pr-section-title" id="pr-codeembed-title">Code Graph Embeddings</h2>
      <p class="pr-hint" style="margin-bottom: 10px;">
        Override the backend the code-graph indexer uses for code-entity
        embeddings (functions, classes, modules, APIs). Defaults to the
        install-time detected backend; pin a specific one when you want
        to evaluate alternatives or fall back from a paid model.
      </p>

      {#if codeEmbedLoading}
        <p class="pr-hint">Loading…</p>
      {:else}
        <!-- Read-only detected backend. We reuse the same
             defaultCodeModel state populated by loadEmbeddingCatalog()
             above — single source of truth. -->
        <div class="pr-kgsum-block">
          <p class="pr-kgsum-block-label">Detected backend</p>
          <p class="pr-hint">
            <code>{defaultCodeModel || '(unset — install hasn\'t run hardware selection)'}</code>
          </p>
        </div>

        <div class="pr-kgsum-block">
          <p class="pr-kgsum-block-label">Override</p>
          <label class="pr-kgsum-radio">
            <input
              type="radio"
              name="code-embed-override"
              value=""
              checked={codeEmbedOverride === '' || codeEmbedOverride === 'auto'}
              onchange={() => void saveCodeEmbedOverride('')}
              disabled={codeEmbedSaving}
            />
            <span>Auto (use the detected backend)</span>
          </label>
          <label class="pr-kgsum-radio">
            <input
              type="radio"
              name="code-embed-override"
              value="codesage"
              checked={codeEmbedOverride === 'codesage'}
              onchange={() => void saveCodeEmbedOverride('codesage')}
              disabled={codeEmbedSaving}
            />
            <span>CodeSage-Large-v2 (2048-dim, GPU)</span>
          </label>
          <label class="pr-kgsum-radio">
            <input
              type="radio"
              name="code-embed-override"
              value="qwen3"
              checked={codeEmbedOverride === 'qwen3'}
              onchange={() => void saveCodeEmbedOverride('qwen3')}
              disabled={codeEmbedSaving}
            />
            <span>qwen3-embedding (1024-dim, CPU-friendly fallback)</span>
          </label>
          <label class="pr-kgsum-radio">
            <input
              type="radio"
              name="code-embed-override"
              value="jina"
              checked={codeEmbedOverride === 'jina'}
              onchange={() => void saveCodeEmbedOverride('jina')}
              disabled={codeEmbedSaving}
            />
            <span>Jina code-embeddings (768-dim, lightweight)</span>
          </label>
          <label class="pr-kgsum-radio">
            <input
              type="radio"
              name="code-embed-override"
              value="openai"
              checked={codeEmbedOverride === 'openai'}
              onchange={() => void saveCodeEmbedOverride('openai')}
              disabled={codeEmbedSaving || !kgSummaryConsent}
            />
            <span>OpenAI embeddings (requires the consent above; cost per call)</span>
          </label>
          <label class="pr-kgsum-radio">
            <input
              type="radio"
              name="code-embed-override"
              value="ollama"
              checked={codeEmbedOverride === 'ollama'}
              onchange={() => void saveCodeEmbedOverride('ollama')}
              disabled={codeEmbedSaving}
            />
            <span>Other Ollama model (pick below)</span>
          </label>
        </div>

        {#if codeEmbedOverride === 'openai'}
          <div class="pr-kgsum-block">
            <label for="pr-codeembed-openai-model" class="pr-kgsum-label">
              OpenAI embedding model
            </label>
            <select
              id="pr-codeembed-openai-model"
              class="pr-kgsum-select"
              value={codeEmbedOpenaiModel}
              onchange={(e) => {
                const t = e.currentTarget as HTMLSelectElement;
                void saveCodeEmbedOpenaiModel(t.value);
              }}
              disabled={codeEmbedSaving}
            >
              {#each CODE_EMBED_OPENAI_MODELS as m}
                <option value={m.id}>{m.label}</option>
              {/each}
            </select>
          </div>
        {/if}

        {#if codeEmbedOverride === 'ollama'}
          <div class="pr-kgsum-block">
            <label for="pr-codeembed-ollama-model" class="pr-kgsum-label">
              Ollama model
            </label>
            {#if ollamaModelsLoading}
              <p class="pr-hint">Probing Ollama at {OLLAMA_URL}…</p>
            {:else if ollamaModelsError}
              <p class="pr-hint pr-kgsum-warn">
                Couldn't reach Ollama at <code>{OLLAMA_URL}</code> ({ollamaModelsError}).
                <button class="pr-link-btn" onclick={() => void fetchOllamaModels()}>retry</button>.
              </p>
            {:else if ollamaModels.length === 0}
              <p class="pr-hint pr-kgsum-warn">
                No Ollama models installed.
                <button class="pr-link-btn" onclick={() => void fetchOllamaModels()}>retry</button>.
              </p>
            {:else}
              <select
                id="pr-codeembed-ollama-model"
                class="pr-kgsum-select"
                value={codeEmbedOllamaModel}
                onchange={(e) => {
                  const t = e.currentTarget as HTMLSelectElement;
                  void saveCodeEmbedOllamaModel(t.value);
                }}
                disabled={codeEmbedSaving}
              >
                <option value="">— Select model —</option>
                {#each ollamaModels as name}
                  <option value={name}>{name}</option>
                {/each}
              </select>
            {/if}
          </div>
        {/if}

        {#if codeEmbedSaving}
          <p class="pr-hint">Saving…</p>
        {:else if codeEmbedSaved}
          <p class="pr-hint pr-kgsum-saved">Saved!</p>
        {/if}
        {#if codeEmbedError}
          <p class="pr-error">Couldn't save: {codeEmbedError}</p>
        {/if}
      {/if}
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

    <!-- Local data collection (Stream 1, v0.2.20).
         Three controls for the RL retrieval data pipeline:
           1. Toggle per-project local logging (default ON).
           2. Toggle upload consent (default OFF, opt-in).
           3. Clear local cache button (irreversible).
         All controls carry mouseover tooltips explaining the data flow. -->
    <section class="pr-section" aria-labelledby="pr-rl-data-title">
      <h2 class="pr-section-title" id="pr-rl-data-title">Local data collection</h2>
      <p class="pr-onboarding-hint">
        The orchestrator collects retrieval-time embedding data for the optional
        RL reranker (a paid module that learns to surface the nodes you actually
        cite). On the free tier this data is collected <strong>locally only</strong>,
        so if you later upgrade to Pro the model can be trained on your history.
        Nothing leaves your machine unless you also opt in to uploads (separate toggle).
      </p>
      <p class="pr-onboarding-hint">
        <strong>What is collected</strong>: KG search queries, the embeddings used
        to rank them, the per-node scores, and (after Claude responds) which titles
        appear in Claude's reply.
        <strong>What is NOT collected</strong>: the full text of Claude's answers,
        code snippets, file paths, or any token / secret values.
      </p>

      <div class="pr-rl-row">
        <label class="pr-rl-toggle"
               title="When ON, the orchestrator appends every KG retrieval event to ~/.claude/retrieval_rl_data/rl_events.jsonl on this machine. Nothing is uploaded. When OFF, sets RL_LOCAL_LOGGING_DISABLED=true in this project's .claude/env and the writer becomes a no-op.">
          <input type="checkbox" checked={!rlLocalLoggingDisabled}
            disabled={rlLocalLoggingSaving || !$selectedProject}
            onchange={() => void toggleRlLocalLogging()} />
          <strong>Collect retrieval data locally</strong>
          <small>
            ON (default): events appended to <code>~/.claude/retrieval_rl_data/</code>.
            OFF: writes <code>RL_LOCAL_LOGGING_DISABLED=true</code> to
            <code>.claude/env</code> for this project.
          </small>
        </label>
      </div>

      <div class="pr-rl-row">
        <label class="pr-rl-toggle"
               title="When ON, locally-collected events are also published (anonymized) to the central training queue and uploaded under the upload-consent telemetry flag (consent.rl_data). When OFF (default), nothing leaves your machine.">
          <input type="checkbox" checked={rlUploadConsent}
            disabled={rlUploadSaving}
            onchange={() => void toggleRlUploadConsent()} />
          <strong>Upload anonymized retrieval data to improve the global model</strong>
          <small>
            OFF (default): no upload. ON: events also publish to
            <code>~/.vibecoded/telemetry.db</code> and ship to the hub under
            <code>consent.rl_data</code>. Query text is omitted from the upload payload.
          </small>
        </label>
      </div>

      <div class="pr-rl-row">
        <button
          class="pr-btn pr-btn-danger"
          title="Deletes every rl_events*.jsonl file under ~/.claude/retrieval_rl_data/. The .v1.bak archives are preserved. This action cannot be undone."
          onclick={() => void clearRlLocalCache()}
          disabled={rlCacheClearing}
        >
          {rlCacheClearing ? 'Clearing…' : 'Clear local cache'}
        </button>
        <span class="pr-rl-hint">
          Wipes the machine-wide <code>rl_events*.jsonl</code> training corpus.
          Per-project <code>.claude/rl-data/</code> directories are untouched.
        </span>
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

    <!-- v0.2.23 F2 wave 2b (2026-05-21): Profile. Relocated from the
         user-icon Settings popover. Display name lives in Supabase
         `profiles.name`; email is read-only here (Supabase requires its
         own confirmation flow to change). -->
    <section class="pr-section" aria-labelledby="pr-profile-title">
      <h2 class="pr-section-title" id="pr-profile-title">Profile</h2>
      <div class="pr-profile-block">
        <div class="pr-profile-field">
          <label class="pr-profile-label" for="pr-profile-name">Display name</label>
          <input
            id="pr-profile-name"
            class="pr-pat-input"
            type="text"
            bind:value={editName}
          />
        </div>
        <div class="pr-profile-field">
          <label class="pr-profile-label" for="pr-profile-email">Email</label>
          <input
            id="pr-profile-email"
            class="pr-pat-input"
            type="email"
            value={$currentUser?.email ?? ''}
            disabled
          />
          <p class="pr-pat-hint">Email cannot be changed here.</p>
        </div>
        <div class="pr-profile-actions">
          <button class="pr-btn-primary" onclick={saveProfile}>
            Save changes
          </button>
          {#if profileSaved}
            <span class="pr-profile-saved">Saved!</span>
          {/if}
        </div>
      </div>
    </section>

    <!-- v0.2.23 F2 wave 2b: Downloads. Per-machine launcher prefs
         persisted to localStorage by `$lib/stores/settings.ts`. -->
    <section class="pr-section" aria-labelledby="pr-downloads-title">
      <h2 class="pr-section-title" id="pr-downloads-title">Downloads</h2>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Install location</strong>
          <span class="pr-onboarding-hint">
            Where apps will be downloaded and installed.
          </span>
        </div>
        <input
          class="pr-pat-input pr-downloads-input"
          type="text"
          bind:value={installPath}
          onchange={() => settings.updateSetting('installPath', installPath)}
        />
      </div>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Auto-update apps</strong>
          <span class="pr-onboarding-hint">
            Automatically download and install app updates.
          </span>
        </div>
        <input
          type="checkbox"
          bind:checked={autoUpdate}
          onchange={() => settings.updateSetting('autoUpdate', autoUpdate)}
        />
      </div>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Launch on system startup</strong>
          <span class="pr-onboarding-hint">
            Start the launcher automatically when you log in to your machine.
          </span>
        </div>
        <input
          type="checkbox"
          bind:checked={launchOnStartup}
          onchange={() => settings.updateSetting('launchOnStartup', launchOnStartup)}
        />
      </div>
    </section>

    <!-- v0.2.23 F2 wave 2b: Shared services live status. Relocated from
         the popover. Read-only display of the per-machine Weaviate /
         Ollama / code-embed instances every orchestrator install reuses.
         Per-install isolation comes from KG_COLLECTION namespacing, not
         separate containers. -->
    <section class="pr-section" aria-labelledby="pr-services-title">
      <h2 class="pr-section-title" id="pr-services-title">Shared services</h2>
      <div class="pr-onboarding-row pr-services-row">
        <div class="pr-onboarding-text">
          <strong>Live status</strong>
          <span class="pr-onboarding-hint">
            Used by every orchestrator install on this machine. Per-install
            isolation comes from separate Knowledge Graph collections
            inside the shared Weaviate, not from separate containers.
          </span>
        </div>
        <button class="pr-btn" onclick={() => void refreshServices()} disabled={servicesLoading}>
          {servicesLoading ? 'Probing…' : 'Refresh'}
        </button>
      </div>
      {#if servicesError}
        <p class="pr-error">Couldn't probe: {servicesError}</p>
      {:else if services}
        <ul class="pr-services-list">
          <li class:on={services.weaviate_url}>
            <span class="pr-services-dot"></span>
            <span class="pr-services-lbl">Weaviate</span>
            <code class="pr-services-url">
              {services.weaviate_url ?? 'http://localhost:8081 (not running)'}
            </code>
          </li>
          <li class:on={services.ollama_url}>
            <span class="pr-services-dot"></span>
            <span class="pr-services-lbl">Ollama</span>
            <code class="pr-services-url">
              {services.ollama_url ?? 'http://localhost:11435 (not running)'}
            </code>
          </li>
          <li class:on={services.code_embed_url}>
            <span class="pr-services-dot"></span>
            <span class="pr-services-lbl">code_embed</span>
            <code class="pr-services-url">
              {services.code_embed_url ?? 'http://localhost:11440 (not running)'}
            </code>
          </li>
        </ul>
      {:else if servicesLoading}
        <p class="pr-hint">Probing…</p>
      {/if}
    </section>

    <!-- v0.2.23 F2 wave 2b: Embedding profile (global). Relocated from
         the popover. GLOBAL ACTIVE_EMBEDDING knob, distinct from the
         per-new-project "Default embedding models" rows further up the
         page. Backed by app_state key `embedding.active_profile`; flows
         into per-project envs via ProjectEnvSettings::populate. -->
    <section class="pr-section" aria-labelledby="pr-active-emb-title">
      <h2 class="pr-section-title" id="pr-active-emb-title">Embedding profile (global)</h2>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Active profile</strong>
          <span class="pr-onboarding-hint">
            Which model the launcher uses for KG / Codegraph embeddings.
            Changing this affects every project created or refreshed from
            now on; existing collections keep their stored embeddings
            until re-synced.
            <br /><br />
            Note: this is distinct from the per-new-project
            <strong>Default embedding models</strong> section above. That
            section controls the model that goes into a brand-new
            project's binding; this section controls the
            <code>ACTIVE_EMBEDDING</code> env var that propagates to
            every refreshed project's env.
          </span>
        </div>
        <select
          class="pr-kgsum-select pr-active-emb-select"
          value={activeEmbedding}
          onchange={(e) => {
            const target = e.currentTarget as HTMLSelectElement;
            void saveActiveEmbedding(target.value as EmbeddingProfile);
          }}
          disabled={activeEmbeddingLoading || activeEmbeddingSaving}
          aria-label="Active embedding profile"
        >
          <option value="qwen3">qwen3 (1024-dim, default)</option>
          <option value="arctic">arctic (1024-dim, legacy)</option>
          <option value="codesage">codesage (2048-dim, code-only)</option>
          <option value="openai">openai (3072-dim, paid)</option>
        </select>
      </div>
      {#if activeEmbeddingSaving}
        <p class="pr-hint">Saving…</p>
      {:else if activeEmbeddingSaved}
        <p class="pr-hint pr-kgsum-saved">Saved!</p>
      {/if}
      {#if activeEmbeddingError}
        <p class="pr-error">Couldn't save: {activeEmbeddingError}</p>
      {/if}
    </section>

    <!-- v0.2.23 F2 wave 2b: Volume location. Relocated from the popover.
         Container data location for Weaviate / Ollama / code-embed.
         Migration is a two-step flow: dry-run plan → user confirms →
         backend copies, verifies new bind-mounts come up healthy, then
         removes the old volumes. On any failure the migration rolls back
         without touching your data. -->
    <section class="pr-section" aria-labelledby="pr-volumes-title">
      <h2 class="pr-section-title" id="pr-volumes-title">Volume location</h2>
      <p class="pr-hint">
        Where Weaviate's vector index, Ollama's models, and the
        code-embed cache live. Changing this safely copies all data,
        verifies new bind-mounts come up healthy, then removes the old
        volumes. On any failure the migration rolls back without
        touching your data.
      </p>
      {#if volumesLoading}
        <p class="pr-hint">Probing…</p>
      {:else if volumesError}
        <p class="pr-error">Couldn't probe: {volumesError}</p>
      {:else if volumesConfig}
        <ul class="pr-volumes-list">
          {#each volumesConfig.volumes as v}
            <li>
              <span class="pr-volumes-role">{v.role}</span>
              <code class="pr-volumes-mount">{v.mountpoint}</code>
              {#if v.size_human}
                <span class="pr-volumes-size">{v.size_human}</span>
              {/if}
            </li>
          {/each}
        </ul>
        <p class="pr-hint">
          Mode: <strong>{volumesConfig.mode}</strong>
          {#if volumesConfig.total_size_human}
            · {volumesConfig.total_size_human} total
          {/if}
        </p>

        {#if migrationPlan}
          <div class="pr-volumes-confirm">
            <p>
              Move <strong>{migrationPlan.total_human}</strong>
              from {migrationPlan.from_mode} to
              <code>{migrationPlan.to_path}</code>
              (~{Math.ceil(migrationPlan.estimated_seconds / 60)} min on local SSD)?
            </p>
            {#each migrationPlan.warnings as w}
              <p class="pr-hint pr-kgsum-warn">{w}</p>
            {/each}
            <div class="pr-volumes-actions">
              <button class="pr-btn" onclick={cancelMigration} disabled={migratingVolumes}>
                Cancel
              </button>
              <button
                class="pr-btn-primary"
                onclick={() => void confirmMigration()}
                disabled={migratingVolumes || migrationPlan.insufficient_free_space}
              >
                {migratingVolumes ? 'Migrating…' : 'Confirm migration'}
              </button>
            </div>
            {#if migratingVolumes && migrationPhaseLabel}
              <p class="pr-hint pr-volumes-phase">
                {migrationPhaseLabel}{#if migrationCopyProgress} ({migrationCopyProgress.index}/{migrationCopyProgress.total}){/if}
              </p>
            {/if}
            {#if migrationError}<p class="pr-error">{migrationError}</p>{/if}
          </div>
        {:else}
          <div class="pr-volumes-input-row">
            <input
              type="text"
              class="pr-pat-input pr-volumes-input"
              bind:value={migratePath}
              placeholder="/mnt/big-disk/vct-volumes"
            />
            <button class="pr-btn" onclick={() => void startMigrationDryRun()}>Change…</button>
            <button class="pr-btn" onclick={() => void refreshVolumes()}>Refresh</button>
          </div>
          {#if migrationError}<p class="pr-error">{migrationError}</p>{/if}
        {/if}
      {/if}
    </section>

    <!-- v0.2.23 F2 wave 2b: About. Bottom of page (universal pattern). -->
    <section class="pr-section" aria-labelledby="pr-about-title">
      <h2 class="pr-section-title" id="pr-about-title">About</h2>
      <div class="pr-about-card">
        <div class="pr-about-logo">
          <div class="pr-about-logo-icon"><span>V</span></div>
          <div>
            <p class="pr-about-name">VCT Launcher</p>
            <p class="pr-about-version">{appVersion ? `v${appVersion}` : ''}</p>
          </div>
        </div>
        <div class="pr-about-rows">
          <div class="pr-about-row">
            <span class="pr-about-label">Framework</span>
            <span class="pr-about-value">Tauri 2 + SvelteKit</span>
          </div>
          <div class="pr-about-row">
            <span class="pr-about-label">Website</span>
            <span class="pr-about-value pr-about-link">vibecodedtools.com</span>
          </div>
          <div class="pr-about-row">
            <span class="pr-about-label">License</span>
            <span class="pr-about-value">AGPL-3.0</span>
          </div>
        </div>
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

  /* v0.2.23 F2/F3/F4: KG Summaries + Code Graph Embeddings sections.
     Boxy block layout that mirrors the rest of the page (.pr-onboarding-row
     style: subtle border + tinted background) so the new sections don't
     feel like a foreign body. */
  .pr-kgsum-block {
    padding: 10px 14px; background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06); border-radius: 6px;
    margin-top: 8px;
  }
  .pr-kgsum-block-label {
    font-size: 11px; font-weight: 600; color: #aaa;
    margin: 0 0 6px;
  }
  .pr-kgsum-radio,
  .pr-kgsum-checkbox {
    display: flex; align-items: center; gap: 8px;
    padding: 3px 0; font-size: 12px; color: #ccc;
    cursor: pointer;
  }
  .pr-kgsum-radio input,
  .pr-kgsum-checkbox input {
    accent-color: rgb(0,191,166);
    cursor: pointer;
  }
  .pr-kgsum-radio input:disabled,
  .pr-kgsum-checkbox input:disabled { cursor: not-allowed; }
  .pr-kgsum-radio input:disabled + span,
  .pr-kgsum-checkbox input:disabled + span { opacity: 0.55; }
  .pr-kgsum-label {
    display: block; font-size: 11px; color: #aaa;
    margin-bottom: 6px;
  }
  .pr-kgsum-select {
    width: 100%; max-width: 360px;
    background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1);
    color: #e8e8ee; padding: 5px 10px; border-radius: 4px; font-size: 12px;
  }
  .pr-kgsum-warn { color: rgb(255,184,74); margin-top: 4px; }
  .pr-kgsum-saved { color: rgb(0,191,166); font-weight: 500; }

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
  /* Local data collection (Stream 1, v0.2.20) */
  .pr-rl-row {
    display: flex; flex-direction: column; gap: 4px;
    padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .pr-rl-row:last-child { border-bottom: none; flex-direction: row; align-items: center; gap: 12px; }
  .pr-rl-toggle {
    display: grid; grid-template-columns: 20px max-content 1fr;
    gap: 6px 10px; align-items: baseline; cursor: pointer;
  }
  .pr-rl-toggle input { grid-row: 1; }
  .pr-rl-toggle strong { grid-row: 1; color: #ddd; font-size: 12px; }
  .pr-rl-toggle small {
    grid-column: 2 / 4; grid-row: 2;
    font-size: 11px; color: #888; line-height: 1.4;
  }
  .pr-rl-toggle code {
    background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px;
    font-size: 10.5px;
  }
  .pr-rl-hint {
    font-size: 11px; color: #888; line-height: 1.4;
  }
  .pr-rl-hint code {
    background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px;
    font-size: 10.5px;
  }
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

  /* v0.2.23 F2 wave 2b: Profile / Downloads / Shared services / Embedding
     profile / Volume location / About — sections relocated from the now-
     deleted user-icon Settings popover. Styling mirrors the rest of the
     page (subtle border + tinted background) so the new sections sit
     naturally next to the existing ones. */
  .pr-profile-block {
    padding: 12px 14px; background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06); border-radius: 6px;
    display: flex; flex-direction: column; gap: 12px;
  }
  .pr-profile-field { display: flex; flex-direction: column; gap: 4px; }
  .pr-profile-label { font-size: 11px; color: #888; }
  .pr-profile-actions { display: flex; align-items: center; gap: 12px; margin-top: 4px; }
  .pr-profile-saved { font-size: 11.5px; color: rgb(0,191,166); font-weight: 500; }
  .pr-downloads-input { max-width: 320px; flex-shrink: 0; }

  /* Shared services list — port of .services-list from SettingsPanel. */
  .pr-services-row { align-items: flex-start; }
  .pr-services-list {
    list-style: none; padding: 10px 14px; margin: 8px 0 0;
    background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06);
    border-radius: 6px; font-size: 12px;
  }
  .pr-services-list li {
    display: flex; gap: 8px; align-items: center;
    padding: 4px 0; color: #888;
  }
  .pr-services-list li.on { color: #ccc; }
  .pr-services-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: #555; display: inline-block; flex-shrink: 0;
  }
  .pr-services-list li.on .pr-services-dot { background: rgb(0,191,166); }
  .pr-services-lbl { min-width: 80px; font-size: 11.5px; }
  .pr-services-url {
    font-family: ui-monospace, monospace; font-size: 11px;
    color: #c4b3ff; background: rgba(255,255,255,0.04);
    padding: 1px 6px; border-radius: 3px; word-break: break-all;
  }

  /* Embedding profile select — sized to fit the row's right column. */
  .pr-active-emb-select { max-width: 320px; }

  /* Volume location — port of .volumes-list / .migrate-* from SettingsPanel. */
  .pr-volumes-list {
    list-style: none; padding: 10px 14px; margin: 8px 0;
    background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06);
    border-radius: 6px;
  }
  .pr-volumes-list li {
    padding: 3px 0; color: #ccc; display: flex; gap: 8px;
    align-items: baseline; flex-wrap: wrap; font-size: 12px;
  }
  .pr-volumes-role {
    display: inline-block; min-width: 80px; color: #c4b3ff;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
  }
  .pr-volumes-mount {
    font-family: ui-monospace, monospace; font-size: 11px;
    background: rgba(255,255,255,0.04); padding: 1px 6px; border-radius: 3px;
  }
  .pr-volumes-size { color: #888; font-size: 11px; }
  .pr-volumes-input-row {
    display: flex; gap: 6px; align-items: center; margin-top: 6px;
  }
  .pr-volumes-input { flex: 1; min-width: 0; }
  .pr-volumes-confirm {
    margin-top: 8px; padding: 10px 12px;
    border: 1px solid rgba(255,184,74,0.3); border-radius: 6px;
    background: rgba(255,184,74,0.04);
  }
  .pr-volumes-confirm p { margin: 4px 0; font-size: 12px; color: #ddd; }
  .pr-volumes-confirm code {
    font-family: ui-monospace, monospace; background: rgba(255,255,255,0.06);
    padding: 1px 5px; border-radius: 3px; font-size: 11px;
  }
  .pr-volumes-actions { display: flex; gap: 8px; margin-top: 8px; }
  .pr-volumes-phase { color: rgb(160,200,255); }

  /* About — port of .about-* from SettingsPanel. */
  .pr-about-card {
    padding: 14px 16px; background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06); border-radius: 6px;
    display: flex; flex-direction: column; gap: 16px;
  }
  .pr-about-logo { display: flex; align-items: center; gap: 12px; }
  .pr-about-logo-icon {
    width: 40px; height: 40px; border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    background: linear-gradient(135deg, rgb(0,191,166), rgb(123,95,255));
    box-shadow: 0 3px 12px rgba(0,191,166,0.2);
  }
  .pr-about-logo-icon span { color: #0e0e16; font-weight: 900; font-size: 18px; }
  .pr-about-name { font-size: 14px; font-weight: 700; color: #e8e8ee; margin: 0; }
  .pr-about-version {
    font-size: 11px; color: #888; margin: 0;
    font-family: ui-monospace, monospace;
  }
  .pr-about-rows { display: flex; flex-direction: column; gap: 6px; }
  .pr-about-row {
    display: flex; justify-content: space-between;
    padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.04);
    font-size: 12px;
  }
  .pr-about-label { color: #888; }
  .pr-about-value { color: #ccc; font-weight: 500; }
  .pr-about-link { color: rgb(0,191,166); }
</style>

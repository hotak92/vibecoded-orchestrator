// Global UI flags. Lives in the layout shell so any route can open the
// activation modal / install wizard / mcp dashboard / onboarding wizard.
//
// v0.2.23 F2 wave 2b (2026-05-21): the user-icon Settings popover was
// merged into /preferences. The SettingsPanel component is deleted; the
// `showSettings` flag + `settingsInitialSection` field were removed. The
// `openSettings(section)` action is kept as a thin compatibility shim
// that navigates to /preferences (or /preferences/secrets when the
// caller requests the 'secrets' section) so off-limits files
// (modules/+page.svelte — owned by the F2a Orchestrator Core agent)
// keep working without coordination churn. New code should call
// goto('/preferences') / goto('/preferences/secrets') directly.

import { writable } from 'svelte/store';
import { goto } from '$app/navigation';
import { clearOnboardingComplete } from '$lib/onboarding';

// v0.2.24: narrowed from the popover-era 5-value union (profile /
// downloads / secrets / preferences / about) to just 'secrets'.
// The other 4 all collapsed to goto('/preferences'), so the only
// remaining discriminator is whether the caller wants the secrets
// sub-route. New code should call goto('/preferences') /
// goto('/preferences/secrets') directly; this type only exists to
// keep the compatibility shim's signature precise.
export type SettingsSection = 'secrets';

interface UIState {
  showActivation: boolean;
  showInstallWizard: boolean;
  showMcpDashboard: boolean;
  showOnboarding: boolean;
  // v0.2.40 L1: multi-key licensing modal.
  //
  // NAMING NOTE (Agent L1, 2026-05-30): the flag is named
  // `showLicenseManager` — NOT `showLicense`, `showModal`,
  // `showKeyManager` or any short variant. The v0.2.40 pre-push store-flag
  // collision audit A3
  // identified that another contributor's parallel branch (orchestrator-update-progress
  // modal) is likely to add `showOrchestratorUpdateProgress` to this
  // same UIState interface in a future PR. The `showLicenseManager`
  // name was reserved by the A3 audit guidance to keep rebase
  // friction-free: distinct enough that there's no substring-match
  // ambiguity, distinct enough that grep/sed across the codebase won't
  // hit both flags.
  showLicenseManager: boolean;
  // v0.2.40 (contributor branch feat/orchestrator-update-progress-modal):
  // full-screen blocking overlay shown while an update-class operation is in
  // flight (v0.2.100: `updater.run(kind)` → `run_orchestrator_update`,
  // `updater.runRestart()`, and the conflict modal's git operations). Name was
  // reserved by the A3 collision audit — see the `showLicenseManager` comment
  // above. The modal lives in +layout.svelte and reads `$orchestrator.progress`
  // (populated by the install_progress listener in stores/orchestrator.ts). It
  // is opened ONLY by `updater.beginOp()` — no component opens it directly, so
  // every surface (badge, Updates page, tray, modals) shows the same overlay.
  showOrchestratorUpdateProgress: boolean;
  // Cross-component trigger for ProjectSelector's "Create project" modal.
  // Set by routes that don't render their own form (e.g. /projects list
  // page's "+ Add Project" button) so the modal opens from the globally-
  // mounted ProjectSelector in MenuBar. ProjectSelector consumes and
  // resets this on close.
  showCreateProject: boolean;
  // True when the wizard was opened by an explicit user action
  // (Settings → Re-run, Preferences → Re-run) rather than the
  // automatic first-launch gate. The wizard's preflight uses this to
  // decide whether to auto-close on the "projects already exist" branch
  // — explicit re-runs must NOT auto-close even if projects exist.
  // Cleared when closeOnboarding() runs.
  onboardingForced: boolean;
}

function createUIStore() {
  const { subscribe, update, set } = writable<UIState>({
    showActivation: false,
    showInstallWizard: false,
    showMcpDashboard: false,
    showOnboarding: false,
    onboardingForced: false,
    showCreateProject: false,
    showLicenseManager: false,
    showOrchestratorUpdateProgress: false,
  });

  return {
    subscribe,
    set,
    // v0.2.23 F2 wave 2b: compatibility shim. The popover is gone; this
    // now routes to /preferences (or /preferences/secrets when the
    // caller requested the secrets tab). New callers should use
    // `goto('/preferences')` directly.
    openSettings: (section: SettingsSection | null = null) => {
      const target = section === 'secrets' ? '/preferences/secrets' : '/preferences';
      void goto(target);
    },
    openActivation: () => update((s) => ({ ...s, showActivation: true })),
    closeActivation: () => update((s) => ({ ...s, showActivation: false })),
    openInstallWizard: () => update((s) => ({ ...s, showInstallWizard: true })),
    closeInstallWizard: () =>
      update((s) => ({ ...s, showInstallWizard: false })),
    openMcpDashboard: () => update((s) => ({ ...s, showMcpDashboard: true })),
    closeMcpDashboard: () =>
      update((s) => ({ ...s, showMcpDashboard: false })),
    // Explicit re-run by the user (Settings → Re-run, Preferences →
    // Re-run). Clears the onboarding-complete flag in launcher.db, opens
    // the wizard, AND sets onboardingForced=true so the wizard's
    // preflight knows not to auto-close even if projects already
    // exist. Existing projects and settings are unaffected — only the
    // completion marker is removed so the wizard re-runs from step 1.
    //
    // Bug 14 fix (2026-05-05): the flag moved from WebView localStorage
    // to launcher.db (via $lib/onboarding) so VCT_STATE_DIR isolation
    // works. The clear is fire-and-forget — best-effort, never throws,
    // and the wizard opens regardless.
    openOnboarding: () => {
      void clearOnboardingComplete();
      update((s) => ({
        ...s,
        showOnboarding: true,
        onboardingForced: true,
      }));
    },
    // Auto-launch path used by +layout.svelte's onMount when the
    // onboarding-complete flag is missing. NOT a forced re-run — the
    // wizard's preflight may still auto-close if it discovers
    // projects already exist (e.g. localStorage was wiped but the DB
    // is intact). Internal-only; routes should call openOnboarding().
    autoOpenOnboarding: () => {
      update((s) => ({
        ...s,
        showOnboarding: true,
        onboardingForced: false,
      }));
    },
    closeOnboarding: () =>
      update((s) => ({
        ...s,
        showOnboarding: false,
        onboardingForced: false,
      })),
    openCreateProject: () =>
      update((s) => ({ ...s, showCreateProject: true })),
    closeCreateProject: () =>
      update((s) => ({ ...s, showCreateProject: false })),
    // v0.2.40 L1: per-paid-module license manager. Opens a modal that
    // lists every paid module's key with input + Validate + status
    // badge. See `LicenseManagerModal.svelte` for the UX shape and
    // `stores/moduleLicenseKeys.ts` for the data flow.
    openLicenseManager: () =>
      update((s) => ({ ...s, showLicenseManager: true })),
    closeLicenseManager: () =>
      update((s) => ({ ...s, showLicenseManager: false })),
    // v0.2.40 (contributor): orchestrator self-update progress overlay.
    // v0.2.100: opened by `updater.beginOp()` (inside `run` / `runRestart`),
    // never by a component. Closed by OrchestratorUpdateProgressModal.svelte
    // itself after the completion hold+fade timer expires, or by the user
    // dismissing an error state.
    openOrchestratorUpdateProgress: () =>
      update((s) => ({ ...s, showOrchestratorUpdateProgress: true })),
    closeOrchestratorUpdateProgress: () =>
      update((s) => ({ ...s, showOrchestratorUpdateProgress: false })),
  };
}

export const ui = createUIStore();

// ---------------------------------------------------------------------------
// v0.2.100 (WP-08, L3-F04 + L2-F05 + the L3 dead-event census): the app
// shell's backend-event routing. ONE home for "a Rust emit reached the GUI —
// what does the user see". `+layout.svelte` registers `registerShellListeners`
// once in `onMount`; the routing decisions are the pure functions below so
// they are unit-tested without a webview.
// ---------------------------------------------------------------------------

/** The ONE tray → GUI event (`tray.rs::TRAY_ACTION_EVENT`, must match). The
 *  tray used to `eval` JavaScript that dispatched DOM events nobody listened
 *  for and set `location.hash` under a path router — the items did nothing. */
export const TRAY_ACTION_EVENT = 'vct-tray-action';

/** Mirrors Rust `tray::TrayAction` (`{kind, project_id}`). */
export type TrayAction =
  | { kind: 'check_updates' }
  | { kind: 'about' }
  | { kind: 'open_project'; project_id: string };

/** Parse a `vct-tray-action` payload; null for anything unrecognised. */
export function parseTrayAction(payload: unknown): TrayAction | null {
  if (!payload || typeof payload !== 'object') return null;
  const p = payload as Record<string, unknown>;
  switch (p.kind) {
    case 'check_updates':
      return { kind: 'check_updates' };
    case 'about':
      return { kind: 'about' };
    case 'open_project':
      return typeof p.project_id === 'string' && p.project_id.length > 0
        ? { kind: 'open_project', project_id: p.project_id }
        : null;
    default:
      return null;
  }
}

/** What the shell routing needs from the app — injected so tests can
 *  observe it and so this module does not import the updater store. */
export interface ShellActions {
  goto: (path: string) => unknown;
  /** `updater.manualCheck` — the one check. */
  manualCheck: () => unknown;
  /** Open the about / changelog UI. */
  showAbout: () => void;
  /** Re-read the orchestrator update status into the store. */
  refreshUpdateStatus: () => unknown;
  notifyInfo: (message: string) => void;
  notifyError: (message: string, key: string) => void;
}

/** Route one tray action. `check_updates` navigates to the Updates page AND
 *  runs the check, so the user sees the answer where it is rendered. */
export async function routeTrayAction(payload: unknown, actions: ShellActions): Promise<void> {
  const action = parseTrayAction(payload);
  if (!action) return;
  switch (action.kind) {
    case 'check_updates':
      await actions.goto('/preferences/updates');
      await actions.manualCheck();
      return;
    case 'open_project':
      await actions.goto(`/project/${encodeURIComponent(action.project_id)}`);
      return;
    case 'about':
      actions.showAbout();
      return;
  }
}

function field(payload: unknown, key: string): unknown {
  return payload && typeof payload === 'object' ? (payload as Record<string, unknown>)[key] : undefined;
}

function text(v: unknown, fallback: string): string {
  return typeof v === 'string' && v.length > 0 ? v : fallback;
}

/**
 * The backend notice events that used to have NO listener (L3 section 2),
 * each mapped to what the user is told. Keys are the Rust event names
 * (`services/watcher.rs::EVT_WATCHER_ALERT`,
 * `project_codegraph_extras.rs::PROGRESS_EVENT`,
 * `openai_cmd.rs::EVT_OPENAI_RE_REGISTER_FAILED`, `modules.rs` /
 * `installer_engine.rs` module events, `tray.rs` hub stop).
 */
export const SHELL_NOTICE_EVENTS: Readonly<
  Record<string, (payload: unknown, actions: ShellActions) => void>
> = {
  services_watcher_alert: (p, a) => {
    const service = text(field(p, 'service'), 'a service');
    const kind = field(p, 'kind');
    if (kind === 'stuck_transient_state') {
      a.notifyError(
        `${service} was stuck starting or stopping and was killed: ${text(field(p, 'error'), 'no detail')}. Open Services to check it.`,
        `services-watcher:${service}`,
      );
    } else {
      const attempts = field(p, 'attempts');
      a.notifyError(
        `${service} stopped and the launcher gave up restarting it` +
          (typeof attempts === 'number' ? ` after ${attempts} attempts` : '') +
          '. Open Services to check it.',
        `services-watcher:${service}`,
      );
    }
  },
  'vct-codegraph-extras-progress': (p, a) => {
    // Progress lines stream while an extra path indexes; only the finished
    // run is worth a notice (the per-line progress belongs to the panel
    // that started it).
    const progress = field(p, 'progress');
    if (typeof progress === 'number' && progress >= 1) {
      a.notifyInfo(`Code graph: extra path "${text(field(p, 'label'), 'unnamed')}" indexed.`);
    }
  },
  'vct-openai-key-re-register-failed': (p, a) => {
    const status = field(p, 'http_status');
    a.notifyError(
      `The new OpenAI key was rejected` +
        (typeof status === 'number' ? ` (HTTP ${status})` : '') +
        `: ${text(field(p, 'reason'), 'no reason given')}. The previously working key was not replaced.`,
      'openai-key:re-register',
    );
  },
  'module://container-start-failed': (p, a) => {
    const id = text(field(p, 'module_id'), 'a module');
    a.notifyError(
      `Module ${id}: its container failed to start — ${text(field(p, 'error'), 'no detail')}`,
      `module:${id}:container-start`,
    );
  },
  'module://db-migration-failed': (p, a) => {
    const id = text(field(p, 'module_id'), 'a module');
    const errors = field(p, 'errors');
    const detail =
      Array.isArray(errors) && errors.length > 0
        ? errors.map((e) => String(e)).join('; ')
        : text(field(p, 'error'), 'no detail');
    a.notifyError(`Module ${id}: database migration failed — ${detail}`, `module:${id}:db-migration`);
  },
  'vct-hub-stopped': (_p, a) => {
    // The Services page re-polls on its own interval; this tells the user
    // the tray action took effect without waiting for that.
    a.notifyInfo('vct-hub stopped. Hooks, MCPs and scripts cannot reach it until it is started again.');
  },
  // The daily background check found an update: re-read the one update
  // status so the badge (and the Updates page) show it.
  'vct-launcher-update-available': (_p, a) => {
    void a.refreshUpdateStatus();
  },
};

/** The `listen` signature `registerShellListeners` needs (`$lib/tauri`). */
export type ListenFn = <T>(
  event: string,
  handler: (e: { payload: T }) => void,
) => Promise<() => void>;

/**
 * Register the tray-action listener and every notice listener. Returns ONE
 * teardown. A listener that fails to register is logged and skipped — the
 * others still register.
 */
export async function registerShellListeners(
  listen: ListenFn,
  actions: ShellActions,
): Promise<() => void> {
  const unlisteners: (() => void)[] = [];
  const add = async (event: string, handler: (payload: unknown) => void) => {
    try {
      unlisteners.push(await listen<unknown>(event, (e) => handler(e.payload)));
    } catch (err) {
      console.warn(`[shell] listen('${event}') failed:`, err);
    }
  };
  await add(TRAY_ACTION_EVENT, (payload) => void routeTrayAction(payload, actions));
  for (const [event, handle] of Object.entries(SHELL_NOTICE_EVENTS)) {
    await add(event, (payload) => handle(payload, actions));
  }
  return () => {
    for (const u of unlisteners) u();
  };
}

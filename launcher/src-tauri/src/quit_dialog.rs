//! Quit confirmation dialog.
//!
//! Shown when the user attempts to quit the launcher via:
//!   - tray menu → Quit
//!   - window-close (X button / Cmd+Q)
//!
//! Three buttons:
//!   1. "Quit and stop services" → stop containers, then `app.exit(0)`
//!   2. "Reduce to tray"         → hide the main window, services keep running
//!   3. "Cancel"                 → do nothing
//!
//! Programmatic quits (self-update, automated restarts) bypass the dialog
//! by calling [`force_quit`] which sets a global flag the handlers honour.
//!
//! Implementation: native `tauri-plugin-dialog` (already in `Cargo.toml`
//! and registered in `lib.rs`). No new dependency. No Svelte modal — keeps
//! the path simple and OS-native.

use std::sync::atomic::{AtomicBool, Ordering};

use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, Manager, Runtime, State};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind, MessageDialogResult};

use crate::db::Db;

// Button labels — kept as constants so the match arms below stay aligned
// with what we passed into the dialog. English only for v1; i18n is a
// follow-up (see commit message / coordination notes).
const BTN_QUIT: &str = "Quit and stop services";
const BTN_TRAY: &str = "Reduce to tray";
const BTN_CANCEL: &str = "Cancel";

const TITLE: &str = "Quit VCT Launcher?";
const BODY: &str = "Closing the launcher will stop VCT services (Weaviate, Ollama, etc.) \
                    and other VibeCoded Tools apps will lose access to them. Are you sure?";

/// Global "skip the dialog" flag. Set by [`force_quit`] before triggering
/// a programmatic exit (e.g. self-update flow). Checked by the tray and
/// window-close handlers.
static FORCE_QUIT: AtomicBool = AtomicBool::new(false);

/// Mark the next quit as programmatic — the confirmation dialog will be
/// skipped. Call this immediately before triggering shutdown from a
/// trusted code path (self-update, automated restart, etc.).
#[allow(dead_code)] // wired up once the self-update flow needs it
pub fn force_quit() {
    FORCE_QUIT.store(true, Ordering::SeqCst);
}

/// Returns `true` when the next quit should bypass the confirmation dialog.
pub fn should_skip_dialog() -> bool {
    FORCE_QUIT.load(Ordering::SeqCst)
}

// ---------------------------------------------------------------------------
// v0.2.91 WP-F2 — tray window preferences (user decisions #2 override + #3)
// ---------------------------------------------------------------------------
//
// Three preferences govern what the window buttons mean. Until v0.2.91 two of
// them were rendered by the Preferences page and read by NOTHING: the GUI had
// been promising "Close button minimizes to tray (doesn't exit)" — defaulted
// ON — since the prefs shipped, while `on_window_event` unconditionally showed
// the 3-button quit dialog. They were also persisted per SELECTED PROJECT
// (`module_settings`, module `launcher`), which is the wrong home for a
// launcher-global window behaviour: toggling them while project A was selected
// left project B with a different "global" answer.
//
// v0.2.91 wires all three and re-homes them to `app_state` (launcher-global),
// adopting any legacy per-project value once on first read.

/// app_state key: X / Cmd+W reduces to the tray instead of quitting.
/// **Shipped default `true`** (user decision #2 override) — X→tray IS the
/// out-of-the-box behaviour; a real exit is tray right-click → Quit.
pub(crate) const APP_STATE_CLOSE_TO_TRAY: &str = "launcher.tray_close_to_tray";

/// app_state key: start the launcher hidden (tray only). Default `false`.
pub(crate) const APP_STATE_START_MINIMIZED: &str = "launcher.tray_start_minimized";

/// app_state key: the TITLEBAR minimize button hides to the tray instead of
/// the taskbar. Default `false` (decision #2: pref-gated, off by default —
/// minimize-to-tray annoys users who deliberately use the taskbar).
pub(crate) const APP_STATE_MINIMIZE_TO_TRAY: &str = "launcher.tray_minimize_to_tray";

/// app_state key: the one-time "still running in the tray" notice has been
/// shown. Set the first time the window hides to the tray so the default
/// never reads as "the app won't close", and never shown again.
pub(crate) const APP_STATE_TRAY_NOTICE_SEEN: &str = "launcher.tray_hide_notice_seen";

/// Legacy per-project `module_settings` keys (module `launcher`) the
/// Preferences page wrote before v0.2.91. Read once during migration.
pub(crate) const LEGACY_SETTING_CLOSE_TO_TRAY: &str = "tray_close_to_tray";
pub(crate) const LEGACY_SETTING_START_MINIMIZED: &str = "tray_start_minimized";

/// Shipped defaults. `close_to_tray` keeps the value the GUI has been showing
/// all along; the other two are new and default OFF.
pub(crate) const DEFAULT_CLOSE_TO_TRAY: bool = true;
pub(crate) const DEFAULT_START_MINIMIZED: bool = false;
pub(crate) const DEFAULT_MINIMIZE_TO_TRAY: bool = false;

/// Resolved window preferences. Copied into a process-global cache at setup so
/// the (frequently-firing) `Resized` handler never touches SQLite.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct TrayWindowPrefs {
    pub close_to_tray: bool,
    pub start_minimized: bool,
    pub minimize_to_tray: bool,
}

impl Default for TrayWindowPrefs {
    fn default() -> Self {
        Self {
            close_to_tray: DEFAULT_CLOSE_TO_TRAY,
            start_minimized: DEFAULT_START_MINIMIZED,
            minimize_to_tray: DEFAULT_MINIMIZE_TO_TRAY,
        }
    }
}

static CACHE_CLOSE_TO_TRAY: AtomicBool = AtomicBool::new(DEFAULT_CLOSE_TO_TRAY);
static CACHE_START_MINIMIZED: AtomicBool = AtomicBool::new(DEFAULT_START_MINIMIZED);
static CACHE_MINIMIZE_TO_TRAY: AtomicBool = AtomicBool::new(DEFAULT_MINIMIZE_TO_TRAY);

/// Read the cached window preferences (no I/O).
pub(crate) fn cached_tray_window_prefs() -> TrayWindowPrefs {
    TrayWindowPrefs {
        close_to_tray: CACHE_CLOSE_TO_TRAY.load(Ordering::Relaxed),
        start_minimized: CACHE_START_MINIMIZED.load(Ordering::Relaxed),
        minimize_to_tray: CACHE_MINIMIZE_TO_TRAY.load(Ordering::Relaxed),
    }
}

/// Overwrite the cache. Called once at setup and on every pref write, so a
/// toggle takes effect immediately without a relaunch.
pub(crate) fn set_cached_tray_window_prefs(prefs: TrayWindowPrefs) {
    CACHE_CLOSE_TO_TRAY.store(prefs.close_to_tray, Ordering::Relaxed);
    CACHE_START_MINIMIZED.store(prefs.start_minimized, Ordering::Relaxed);
    CACHE_MINIMIZE_TO_TRAY.store(prefs.minimize_to_tray, Ordering::Relaxed);
}

/// PURE re-homing decision: given every legacy per-project value found for one
/// pref, what should the launcher-global value become?
///
/// - no rows at all → `None` (nothing to adopt; the default applies)
/// - any row that DIFFERS from the shipped default → adopt the non-default
///   value. The user explicitly moved the toggle somewhere; which project
///   happened to be selected at the time is noise, and their intent was
///   plainly "not the default".
/// - every row equals the default → adopt the default (recorded explicitly so
///   the migration runs exactly once).
///
/// Order-independent by construction, so two projects disagreeing can never
/// make the adopted value depend on row order.
pub(crate) fn adopt_legacy_pref(rows: &[bool], default: bool) -> Option<bool> {
    if rows.is_empty() {
        return None;
    }
    if rows.iter().any(|v| *v != default) {
        Some(!default)
    } else {
        Some(default)
    }
}

/// Resolve one boolean pref: `app_state` row wins; absent → adopt any legacy
/// per-project value (and persist the adoption); still nothing → default.
fn resolve_pref(db: &Db, app_state_key: &str, legacy_key: Option<&str>, default: bool) -> bool {
    match db.app_state_get_bool(app_state_key) {
        Ok(Some(v)) => return v,
        Ok(None) => {}
        Err(e) => {
            eprintln!(
                "[vct] tray prefs: reading {} failed ({}) — using default {}",
                app_state_key, e, default,
            );
            return default;
        }
    }

    let legacy_rows: Vec<bool> = match legacy_key {
        Some(k) => db
            .find_all_project_settings_bool("launcher", k)
            .unwrap_or_else(|e| {
                eprintln!(
                    "[vct] tray prefs: legacy lookup for {} failed ({}) — treating as absent",
                    k, e,
                );
                Vec::new()
            }),
        None => Vec::new(),
    };

    let resolved = adopt_legacy_pref(&legacy_rows, default).unwrap_or(default);
    if let Err(e) = db.app_state_set_bool(app_state_key, resolved) {
        eprintln!(
            "[vct] tray prefs: could not persist re-homed {} ({}) — value still applies \
             this session",
            app_state_key, e,
        );
    } else if !legacy_rows.is_empty() {
        eprintln!(
            "[vct] tray prefs: re-homed {} from {} per-project row(s) to app_state (= {})",
            app_state_key,
            legacy_rows.len(),
            resolved,
        );
    }
    resolved
}

/// Load all three prefs from the DB (migrating legacy per-project rows on
/// first read) and prime the cache. Called once from `lib.rs::setup`.
pub(crate) fn load_tray_window_prefs(db: &Db) -> TrayWindowPrefs {
    let prefs = TrayWindowPrefs {
        close_to_tray: resolve_pref(
            db,
            APP_STATE_CLOSE_TO_TRAY,
            Some(LEGACY_SETTING_CLOSE_TO_TRAY),
            DEFAULT_CLOSE_TO_TRAY,
        ),
        start_minimized: resolve_pref(
            db,
            APP_STATE_START_MINIMIZED,
            Some(LEGACY_SETTING_START_MINIMIZED),
            DEFAULT_START_MINIMIZED,
        ),
        // New in v0.2.91 — no legacy home to adopt from.
        minimize_to_tray: resolve_pref(db, APP_STATE_MINIMIZE_TO_TRAY, None, DEFAULT_MINIMIZE_TO_TRAY),
    };
    set_cached_tray_window_prefs(prefs);
    prefs
}

// ---------------------------------------------------------------------------
// v0.2.91 WP-F2 — window-event decision
// ---------------------------------------------------------------------------

/// The window events this launcher interprets.
///
/// Tauri 2 has **no** `WindowEvent::Minimized` variant, so a minimize is
/// observed as a `Resized` whose window reports `is_minimized() == true` —
/// the sanctioned idiom, not a workaround.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum WindowEventKind {
    CloseRequested,
    Resized,
}

/// What the window-event handler should do.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum WindowAction {
    /// Prevent the close (when applicable) and hide the window to the tray.
    Hide,
    /// Prevent the close and show the 3-button quit dialog.
    ConfirmQuit,
    /// Let the event proceed untouched.
    Nothing,
}

/// PURE decision for `on_window_event`.
///
/// `force_quit` is [`should_skip_dialog`]: a programmatic quit is already in
/// flight (self-update, restart button, the "Quit and stop services" tail), so
/// the close MUST proceed. This is the leg that keeps "tray → Quit" a real
/// quit even though X→tray is the default: nothing may convert a decided quit
/// into a hide.
pub(crate) fn window_event_action(
    kind: WindowEventKind,
    is_main: bool,
    is_minimized: bool,
    force_quit: bool,
    prefs: TrayWindowPrefs,
) -> WindowAction {
    if !is_main || force_quit {
        return WindowAction::Nothing;
    }
    match kind {
        WindowEventKind::CloseRequested => {
            if prefs.close_to_tray {
                WindowAction::Hide
            } else {
                WindowAction::ConfirmQuit
            }
        }
        WindowEventKind::Resized => {
            if is_minimized && prefs.minimize_to_tray {
                WindowAction::Hide
            } else {
                WindowAction::Nothing
            }
        }
    }
}

/// One-time "it is still running in the tray" notice, shown the FIRST time the
/// window hides to the tray.
///
/// Why a native dialog and not a toast: the window is being hidden, so an
/// in-page toast would be invisible at exactly the moment it matters. This is
/// the one moment where a modal is the honest surface — it fires at most once
/// per install (the seen-flag lives in `app_state`) and it is what stops the
/// shipped default from reading as "the app won't close".
///
/// Best-effort throughout: no Db state, a failed read, or a failed write all
/// degrade to "don't show" / "may show once more". Never blocks the hide.
pub(crate) fn notify_hidden_to_tray_once<R: Runtime>(app: &AppHandle<R>) {
    let Some(db) = app.try_state::<Db>() else {
        return;
    };
    match db.app_state_get_bool(APP_STATE_TRAY_NOTICE_SEEN) {
        Ok(Some(true)) => return,
        Ok(_) => {}
        Err(e) => {
            eprintln!("[vct] tray notice: seen-flag read failed ({}) — skipping", e);
            return;
        }
    }
    if let Err(e) = db.app_state_set_bool(APP_STATE_TRAY_NOTICE_SEEN, true) {
        // Write failed → we would nag on every hide. Prefer silence.
        eprintln!(
            "[vct] tray notice: could not persist seen-flag ({}) — not showing the notice",
            e,
        );
        return;
    }
    // Deliberately UNPARENTED: the main window is hidden by the time this
    // runs, and a dialog parented to a hidden window can end up invisible.
    app.dialog()
        .message(TRAY_NOTICE_BODY)
        .title(TRAY_NOTICE_TITLE)
        .kind(MessageDialogKind::Info)
        .show(|_| {});
}

const TRAY_NOTICE_TITLE: &str = "VCT Launcher is still running";
const TRAY_NOTICE_BODY: &str = "The launcher window closed to the system tray — VCT services \
                                (Weaviate, Ollama, the hub) keep running so your other \
                                VibeCoded Tools apps stay connected.\n\n\
                                • Left-click the tray icon to bring the window back.\n\
                                • Right-click the tray icon → Quit to exit for real.\n\n\
                                Prefer the X button to quit instead? Turn off \
                                \"Close button reduces to tray\" in Preferences → Window \
                                behaviour. This message is shown only once.";

// ---------------------------------------------------------------------------
// Tauri commands — the Preferences page's only write path for these prefs
// ---------------------------------------------------------------------------

/// Read the three launcher-global window prefs (migrating legacy per-project
/// rows on first read, same as boot).
#[command]
pub async fn get_tray_window_prefs(db: State<'_, Db>) -> Result<TrayWindowPrefs, String> {
    Ok(load_tray_window_prefs(&db))
}

/// Write one window pref. Persists to `app_state` AND refreshes the process
/// cache, so the new meaning of the X / minimize buttons applies immediately.
///
/// `key` is the bare Preferences-page key (`tray_close_to_tray`,
/// `tray_start_minimized`, `tray_minimize_to_tray`); anything else is
/// rejected rather than silently written, so a typo on the GUI side surfaces
/// as an error toast instead of another dead toggle.
#[command]
pub async fn set_tray_window_pref(
    key: String,
    value: bool,
    db: State<'_, Db>,
) -> Result<TrayWindowPrefs, String> {
    let app_state_key = match key.as_str() {
        LEGACY_SETTING_CLOSE_TO_TRAY => APP_STATE_CLOSE_TO_TRAY,
        LEGACY_SETTING_START_MINIMIZED => APP_STATE_START_MINIMIZED,
        "tray_minimize_to_tray" => APP_STATE_MINIMIZE_TO_TRAY,
        other => return Err(format!("unknown tray window preference: {}", other)),
    };
    db.app_state_set_bool(app_state_key, value)?;
    let mut prefs = cached_tray_window_prefs();
    match app_state_key {
        APP_STATE_CLOSE_TO_TRAY => prefs.close_to_tray = value,
        APP_STATE_START_MINIMIZED => prefs.start_minimized = value,
        _ => prefs.minimize_to_tray = value,
    }
    set_cached_tray_window_prefs(prefs);
    Ok(prefs)
}

/// Entry point used by the tray menu and the window-close handler.
///
/// If `force_quit` was called, performs a full shutdown immediately.
/// Otherwise shows the 3-button dialog and routes to the chosen action.
///
/// This function returns immediately; the dialog runs asynchronously and
/// the action runs in its callback. The caller MUST already have prevented
/// the close (for `WindowEvent::CloseRequested`) or the menu has finished
/// dispatching (for tray Quit).
pub fn confirm_and_quit<R: Runtime>(app: &AppHandle<R>) {
    if should_skip_dialog() {
        // Programmatic path — full shutdown, no prompt.
        full_shutdown(app.clone());
        return;
    }

    // Anchor the dialog to the main window when present so it inherits
    // focus on macOS (where free-floating dialogs can end up behind other
    // apps). Falls back to an unparented dialog otherwise.
    let mut builder = app
        .dialog()
        .message(BODY)
        .title(TITLE)
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::YesNoCancelCustom(
            BTN_QUIT.to_string(),
            BTN_TRAY.to_string(),
            BTN_CANCEL.to_string(),
        ));

    if let Some(w) = app.get_webview_window("main") {
        builder = builder.parent(&w);
    }

    let app_for_cb = app.clone();
    builder.show_with_result(move |result: MessageDialogResult| {
        // YesNoCancelCustom maps to Custom(<label>) on all platforms (the
        // plugin normalises Linux's missing Custom variant — see
        // tauri-plugin-dialog desktop.rs lines 240-251).
        match result {
            MessageDialogResult::Custom(ref s) if s == BTN_QUIT => {
                full_shutdown(app_for_cb.clone());
            }
            MessageDialogResult::Custom(ref s) if s == BTN_TRAY => {
                if let Some(w) = app_for_cb.get_webview_window("main") {
                    let _ = w.hide();
                }
            }
            // Cancel button, ESC, dialog dismissed — do nothing.
            _ => {}
        }
    });
}

/// Stop services then exit. Service-stop failure is logged but does NOT
/// block the exit — the user explicitly asked to quit and we honour that.
///
/// The container-stop call delegates to whichever service module is
/// available. This indirection keeps the Quit dialog independent from
/// the (parallel) container-lifecycle work: today it's a no-op, once
/// `commands::lifecycle::services_stop_all` lands it will be wired here.
fn full_shutdown<R: Runtime>(app: AppHandle<R>) {
    // v0.2.91 WP-F2: the quit is DECIDED from here on. Latch FORCE_QUIT
    // before anything else so that any `CloseRequested` the teardown emits
    // cannot be re-interpreted by `window_event_action` as "reduce to tray"
    // (the shipped default) and swallow the exit. This is what makes the
    // tray menu's Quit a real quit even though X→tray is the default.
    force_quit();
    tauri::async_runtime::spawn(async move {
        if let Err(e) = stop_services(&app).await {
            eprintln!("[vct] stop_services failed during quit: {} (exiting anyway)", e);
        }
        app.exit(0);
    });
}

/// Best-effort container shutdown.
///
/// Delegates to `commands::lifecycle::services_stop_all` which runs
/// `<runtime> compose stop` (no `--volumes` flag — volumes are
/// preserved). Idempotent: succeeds even when nothing is up.
///
/// Failure here is logged but never propagates — the user clicked
/// "Quit and stop services" and `app.exit(0)` must run regardless of
/// container-runtime hiccups (the alternative is leaving the user
/// stranded in a half-quit state on a flaky Podman / Docker socket).
async fn stop_services<R: Runtime>(_app: &AppHandle<R>) -> Result<(), String> {
    crate::commands::lifecycle::services_stop_all().await
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    //! Tests for the force-quit short-circuit flag.
    //!
    //! These tests serialize on the global `FORCE_QUIT` flag (it's a
    //! process-wide AtomicBool) so they MUST mutate-and-restore inside a
    //! mutex to stay deterministic when the test binary runs them in
    //! parallel. We use `Mutex` from std rather than a fancy fixture
    //! crate to keep the dependency surface unchanged.

    use super::*;
    use std::sync::Mutex;

    /// Serializes access to the global FORCE_QUIT flag so parallel tests
    /// don't observe each other's mutations.
    static SERIALIZE: Mutex<()> = Mutex::new(());

    fn reset_flag() {
        FORCE_QUIT.store(false, Ordering::SeqCst);
    }

    #[test]
    fn force_quit_sets_skip_dialog_flag() {
        let _g = SERIALIZE.lock().unwrap();
        reset_flag();

        assert!(
            !should_skip_dialog(),
            "flag must start cleared before force_quit() is called"
        );
        force_quit();
        assert!(
            should_skip_dialog(),
            "should_skip_dialog() must return true once force_quit() ran"
        );

        reset_flag();
    }

    #[test]
    fn should_skip_dialog_defaults_to_false() {
        let _g = SERIALIZE.lock().unwrap();
        reset_flag();
        assert!(
            !should_skip_dialog(),
            "freshly reset flag must report false"
        );
    }

    #[test]
    fn force_quit_is_idempotent() {
        let _g = SERIALIZE.lock().unwrap();
        reset_flag();
        force_quit();
        force_quit();
        force_quit();
        assert!(
            should_skip_dialog(),
            "calling force_quit multiple times must not toggle the flag back"
        );
        reset_flag();
    }

    #[test]
    fn button_labels_are_unique_and_nonempty() {
        // Regression: if two button labels collide, the dispatch arms in
        // `confirm_and_quit` would route ambiguously. Also catches an
        // accidental empty string (which the dialog plugin rejects on
        // some platforms).
        let labels = [BTN_QUIT, BTN_TRAY, BTN_CANCEL];
        for l in labels.iter() {
            assert!(!l.is_empty(), "button label must not be empty");
        }
        assert_ne!(BTN_QUIT, BTN_TRAY);
        assert_ne!(BTN_QUIT, BTN_CANCEL);
        assert_ne!(BTN_TRAY, BTN_CANCEL);
    }

    #[test]
    fn dialog_title_and_body_are_nonempty() {
        // Sanity check on the user-visible strings — empty title/body
        // breaks the dialog on Windows.
        assert!(!TITLE.trim().is_empty());
        assert!(!BODY.trim().is_empty());
    }

    // -----------------------------------------------------------------
    // v0.2.91 WP-F2 — window-event decision matrix.
    //
    // Pre-fix `on_window_event` matched CloseRequested ONLY and always ran
    // the 3-button dialog, so every `Hide` expectation below fails and the
    // Resized arm did not exist at all.
    // -----------------------------------------------------------------

    use super::{
        adopt_legacy_pref, window_event_action, TrayWindowPrefs, WindowAction, WindowEventKind,
    };

    fn prefs(close_to_tray: bool, minimize_to_tray: bool) -> TrayWindowPrefs {
        TrayWindowPrefs {
            close_to_tray,
            start_minimized: false,
            minimize_to_tray,
        }
    }

    #[test]
    fn shipped_defaults_are_close_to_tray_on_minimize_to_tray_off() {
        // User decision #2 OVERRIDE: X→tray is the out-of-the-box behaviour;
        // minimize→tray is opt-in.
        let d = TrayWindowPrefs::default();
        assert!(d.close_to_tray, "X must reduce to tray by default");
        assert!(!d.minimize_to_tray, "minimize→tray must default OFF");
        assert!(!d.start_minimized, "start-minimized must default OFF");
    }

    #[test]
    fn close_with_close_to_tray_on_hides() {
        assert_eq!(
            window_event_action(
                WindowEventKind::CloseRequested,
                true,
                false,
                false,
                prefs(true, false)
            ),
            WindowAction::Hide,
        );
    }

    #[test]
    fn close_with_close_to_tray_off_asks_for_confirmation() {
        assert_eq!(
            window_event_action(
                WindowEventKind::CloseRequested,
                true,
                false,
                false,
                prefs(false, false)
            ),
            WindowAction::ConfirmQuit,
        );
    }

    /// The real-quit guarantee: once a quit is decided (tray → Quit → "Quit
    /// and stop services", the restart button, self-update) FORCE_QUIT is
    /// latched and NOTHING may turn the resulting close into a hide.
    #[test]
    fn force_quit_never_hides_even_with_close_to_tray_on() {
        assert_eq!(
            window_event_action(
                WindowEventKind::CloseRequested,
                true,
                false,
                true, // force_quit
                prefs(true, false)
            ),
            WindowAction::Nothing,
            "a decided quit must proceed — otherwise the launcher cannot be quit at all",
        );
    }

    #[test]
    fn non_main_windows_are_never_intercepted() {
        for kind in [WindowEventKind::CloseRequested, WindowEventKind::Resized] {
            assert_eq!(
                window_event_action(kind, false, true, false, prefs(true, true)),
                WindowAction::Nothing,
            );
        }
    }

    #[test]
    fn minimize_with_pref_on_hides() {
        assert_eq!(
            window_event_action(WindowEventKind::Resized, true, true, false, prefs(true, true)),
            WindowAction::Hide,
        );
    }

    /// Leave-alone leg: default OFF means the titlebar minimize keeps its
    /// native taskbar behaviour.
    #[test]
    fn minimize_with_pref_off_does_nothing() {
        assert_eq!(
            window_event_action(
                WindowEventKind::Resized,
                true,
                true,
                false,
                prefs(true, false)
            ),
            WindowAction::Nothing,
        );
    }

    /// A plain resize/drag is a Resized too — it must never hide the window.
    #[test]
    fn plain_resize_is_not_a_minimize() {
        assert_eq!(
            window_event_action(
                WindowEventKind::Resized,
                true,
                false, // not minimized
                false,
                prefs(true, true)
            ),
            WindowAction::Nothing,
        );
    }

    #[test]
    fn minimize_during_force_quit_does_nothing() {
        assert_eq!(
            window_event_action(WindowEventKind::Resized, true, true, true, prefs(true, true)),
            WindowAction::Nothing,
        );
    }

    /// The close pref must not leak into the minimize arm and vice versa.
    #[test]
    fn the_two_prefs_are_independent() {
        assert_eq!(
            window_event_action(
                WindowEventKind::CloseRequested,
                true,
                false,
                false,
                prefs(false, true)
            ),
            WindowAction::ConfirmQuit,
            "minimize→tray must not make X reduce to tray",
        );
        assert_eq!(
            window_event_action(
                WindowEventKind::Resized,
                true,
                true,
                false,
                prefs(true, false)
            ),
            WindowAction::Nothing,
            "close→tray must not make minimize reduce to tray",
        );
    }

    // -----------------------------------------------------------------
    // v0.2.91 WP-F2 — per-project → app_state re-homing (decision #3).
    // -----------------------------------------------------------------

    #[test]
    fn no_legacy_rows_means_nothing_to_adopt() {
        assert_eq!(adopt_legacy_pref(&[], true), None);
        assert_eq!(adopt_legacy_pref(&[], false), None);
    }

    #[test]
    fn an_explicit_non_default_choice_is_adopted() {
        // The user turned "close to tray" OFF on whichever project happened
        // to be selected. That is a real choice; the global value follows it.
        assert_eq!(adopt_legacy_pref(&[false], true), Some(false));
        assert_eq!(adopt_legacy_pref(&[true], false), Some(true));
    }

    #[test]
    fn rows_that_all_match_the_default_adopt_the_default() {
        assert_eq!(adopt_legacy_pref(&[true, true], true), Some(true));
        assert_eq!(adopt_legacy_pref(&[false, false, false], false), Some(false));
    }

    /// Two projects disagreeing must not make the outcome depend on row
    /// order — the explicit non-default choice wins either way.
    #[test]
    fn disagreeing_rows_are_order_independent() {
        assert_eq!(adopt_legacy_pref(&[true, false], true), Some(false));
        assert_eq!(adopt_legacy_pref(&[false, true], true), Some(false));
        assert_eq!(adopt_legacy_pref(&[true, false], false), Some(true));
        assert_eq!(adopt_legacy_pref(&[false, true], false), Some(true));
    }

    // -----------------------------------------------------------------
    // v0.2.91 WP-F2 — the re-homing actually reaches the DB.
    // -----------------------------------------------------------------

    fn db_with_project(id: &str) -> Db {
        let db = Db::open_in_memory().expect("in-memory db");
        db.insert_project(
            id,
            id,
            &format!("/tmp/{}", id),
            vct_launcher_core::db::models::ProjectHost::Base,
            id,
        )
        .expect("insert project");
        db
    }

    #[test]
    fn legacy_per_project_value_is_adopted_into_app_state_once() {
        let db = db_with_project("p1");
        // The user turned "close to tray" OFF while p1 was selected.
        db.set_setting(
            "p1",
            "launcher",
            super::LEGACY_SETTING_CLOSE_TO_TRAY,
            &serde_json::Value::Bool(false),
        )
        .expect("legacy write");

        let prefs = super::load_tray_window_prefs(&db);
        assert!(
            !prefs.close_to_tray,
            "the legacy per-project choice must survive the re-homing",
        );
        assert_eq!(
            db.app_state_get_bool(super::APP_STATE_CLOSE_TO_TRAY),
            Ok(Some(false)),
            "the adopted value must be persisted launcher-globally",
        );
    }

    #[test]
    fn app_state_value_wins_over_a_stale_legacy_row() {
        let db = db_with_project("p1");
        db.set_setting(
            "p1",
            "launcher",
            super::LEGACY_SETTING_CLOSE_TO_TRAY,
            &serde_json::Value::Bool(false),
        )
        .expect("legacy write");
        db.app_state_set_bool(super::APP_STATE_CLOSE_TO_TRAY, true)
            .expect("app_state write");

        assert!(
            super::load_tray_window_prefs(&db).close_to_tray,
            "once re-homed, app_state is the only home — the legacy row is history",
        );
    }

    #[test]
    fn a_fresh_install_gets_the_shipped_defaults() {
        let db = Db::open_in_memory().expect("in-memory db");
        let prefs = super::load_tray_window_prefs(&db);
        assert_eq!(prefs, TrayWindowPrefs::default());
    }

    #[test]
    fn non_boolean_legacy_rows_are_ignored() {
        let db = db_with_project("p1");
        // Something else wrote a string under the key — not evidence of a
        // user's toggle, so it must not steer the global value.
        db.set_setting(
            "p1",
            "launcher",
            super::LEGACY_SETTING_START_MINIMIZED,
            &serde_json::Value::String("yes".into()),
        )
        .expect("legacy write");
        assert!(!super::load_tray_window_prefs(&db).start_minimized);
    }

    #[test]
    fn tray_notice_strings_are_nonempty_and_name_the_quit_path() {
        assert!(!super::TRAY_NOTICE_TITLE.trim().is_empty());
        assert!(!super::TRAY_NOTICE_BODY.trim().is_empty());
        // The whole point of the notice is that "X closed the window" does
        // not read as "the app refuses to quit" — it must name the real exit.
        assert!(
            super::TRAY_NOTICE_BODY.contains("Quit"),
            "the one-time notice must tell the user how to actually quit",
        );
    }
}

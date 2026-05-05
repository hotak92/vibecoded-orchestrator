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

use tauri::{AppHandle, Manager, Runtime};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind, MessageDialogResult};

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
}

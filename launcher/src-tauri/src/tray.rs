//! System tray menu (v1.1).
//!
//! Menu:
//!   - Open Launcher
//!   - Services: <live status>  (label, non-clickable, refreshed every 5s)
//!   - Recent projects (sub-menu, top 5)
//!   - Check for updates
//!   - About
//!   - Quit
//!
//! The services label is updated in place via [`MenuItem::set_text`]. Tauri
//! routes the call through the OS-specific menu backend (muda), so on macOS
//! the underlying `NSMenu` reapplies the item text on the main thread and on
//! Linux/Windows the text is patched directly. No full menu rebuild is
//! required; the same code path works on all three platforms as of
//! tauri 2.10.

use std::time::Duration;

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::{TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Listener, Manager, Runtime,
};

use crate::commands::self_update::{self, UpdateStatus};
use crate::db::Db;

/// Default ports for the shared services. Mirror
/// `commands::installer::DEFAULT_*_PORT` — kept inline here to avoid
/// exposing the constants publicly. If install.py changes a port, both
/// sides must follow.
const WEAVIATE_PORT: u16 = 8081;
const OLLAMA_PORT: u16 = 11435;
const CODE_EMBED_PORT: u16 = 11440;

/// Per-probe timeout for the tray refresh loop. The wizard probes use 2s
/// because they only fire once during onboarding; the tray fires every
/// 5s and must not stall on a slow service.
const PROBE_TIMEOUT: Duration = Duration::from_millis(500);

/// Tray refresh cadence.
const REFRESH_INTERVAL: Duration = Duration::from_secs(5);

pub fn setup<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let open_item = MenuItem::with_id(app, "open", "Open Launcher", true, None::<&str>)?;
    // Initial text is a placeholder; the background task overwrites it on
    // first tick (~5s after startup) — the user sees real data shortly
    // after the tray appears.
    let services_label = MenuItem::with_id(
        app,
        "services_status",
        "Checking services…",
        false, // disabled (label only — not clickable)
        None::<&str>,
    )?;

    // v0.2.21 Step 13: vct-hub status + Stop control. Mirrors the
    // services pill above — initial probe label is a placeholder
    // overwritten by the same background poller on first tick.
    let hub_label = MenuItem::with_id(
        app,
        "hub_status",
        "Hub: probing…",
        false,
        None::<&str>,
    )?;
    // "Stop background hub" item is initially DISABLED. The hub-status
    // poller flips it enabled when the hub is detected running, and
    // back to disabled when not-running/stale. We don't render the
    // item conditionally because Tauri's tray-menu rebuild path is
    // heavier than a set_enabled flip; matches the pattern used by
    // `services_label`'s in-place updates.
    let hub_stop = MenuItem::with_id(
        app,
        "hub_stop",
        "Stop background hub",
        false, // disabled until poller detects running hub
        None::<&str>,
    )?;

    // Recent projects sub-menu (populated on each open via menu rebuild).
    let recent = recent_projects_submenu(app)?;

    // Initial label uses cached state from ~/.vct/launcher-update-state.json
    // so a known-pending update from a previous session shows up immediately
    // (before the first daily-check tick runs). The label flips to
    // "⚠ Update available (N commits behind)" via a Listener on the
    // `vct-launcher-update-available` event below.
    let cached = self_update::get_cached_update_status();
    let updates = MenuItem::with_id(
        app,
        "check_updates",
        &format_update_label(&cached),
        true,
        None::<&str>,
    )?;
    let about = MenuItem::with_id(app, "about", "About VibeCoded Tools", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;

    let menu = Menu::with_items(
        app,
        &[
            &open_item,
            &services_label,
            &hub_label,
            &hub_stop,
            &recent,
            &sep,
            &updates,
            &about,
            &sep,
            &quit,
        ],
    )?;

    let _tray = TrayIconBuilder::with_id("vct-tray")
        .tooltip("VibeCoded Tools Launcher")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            "quit" => crate::quit_dialog::confirm_and_quit(app),
            "check_updates" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                    // If we already know an update is pending (cached
                    // state), open the updates preferences page directly.
                    // Otherwise, fire the legacy event so any in-page
                    // listener can prompt a manual check.
                    let cached = self_update::get_cached_update_status();
                    if cached.available {
                        let _ = w.eval(
                            "window.location.hash = '/preferences/updates'; \
                             window.dispatchEvent(new CustomEvent('vct-check-updates'));",
                        );
                    } else {
                        let _ = w.eval(
                            "window.dispatchEvent(new CustomEvent('vct-check-updates'));",
                        );
                    }
                }
            }
            "hub_stop" => {
                // v0.2.21 Step 13: ask the detached vct-hub to stop.
                // Spawn on a blocking task because `hub_status::stop()`
                // synchronously waits up to 10 s for the hub's
                // graceful-shutdown path. The next tray-poller tick
                // (≤ 5 s after this returns) will flip the label
                // back to "not running" and disable this menu item.
                let app_handle = app.clone();
                tauri::async_runtime::spawn_blocking(move || {
                    match crate::hub_status::stop() {
                        crate::hub_status::StopOutcome::Stopped
                        | crate::hub_status::StopOutcome::AlreadyStopped => {
                            // Best-effort UI nudge: emit an event so
                            // any open GUI page that watches hub
                            // status can refresh immediately rather
                            // than wait for its own next poll.
                            let _ = app_handle.emit(
                                "vct-hub-stopped",
                                serde_json::json!({}),
                            );
                        }
                        crate::hub_status::StopOutcome::BinaryNotFound => {
                            eprintln!(
                                "[vct] tray: cannot stop hub — vct-hub binary not found"
                            );
                        }
                        crate::hub_status::StopOutcome::Failed(msg) => {
                            eprintln!("[vct] tray: hub stop failed: {}", msg);
                        }
                    }
                });
            }
            "about" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                    let _ = w.eval(
                        "window.dispatchEvent(new CustomEvent('vct-show-about'));",
                    );
                }
            }
            id if id.starts_with("project_") => {
                let project_id = id.trim_start_matches("project_").to_string();
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                    let _ = w.eval(&format!(
                        "window.location.hash = '/project/{}'; window.dispatchEvent(new CustomEvent('vct-open-project', {{ detail: {{ id: '{}' }} }}));",
                        project_id, project_id
                    ));
                }
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { .. } = event {
                let app = tray.app_handle();
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
        })
        .build(app)?;

    // Listen for self-update events emitted by the daily background check
    // and by user-triggered checks. We update the "Check for updates"
    // menu item label in place — same pattern as the services pill below,
    // so we don't have to coordinate a full menu rebuild with the
    // services-status background task. Both writers target distinct
    // MenuItems, so there's no contention.
    let updates_for_listener = updates.clone();
    app.listen("vct-launcher-update-available", move |evt| {
        if let Ok(status) = serde_json::from_str::<UpdateStatus>(evt.payload()) {
            let _ = updates_for_listener.set_text(format_update_label(&status));
        }
    });

    // v0.2.21 Step 13: hub-status background poller. Same 5 s cadence
    // as the services poller below; runs in its own task so a slow
    // services probe never starves the hub label. Probe is cheap
    // (read lockfile + kill(pid, 0)) — no HTTP call — so the poll
    // budget is well below the interval.
    let hub_label_for_task = hub_label.clone();
    let hub_stop_for_task = hub_stop.clone();
    tauri::async_runtime::spawn(async move {
        // First tick fires immediately so the user sees real state
        // shortly after the tray appears (no "probing…" stickiness).
        let s = tokio::task::spawn_blocking(crate::hub_status::probe)
            .await
            .unwrap_or(crate::hub_status::HubStatus::NotRunning);
        let _ = hub_label_for_task.set_text(crate::hub_status::label(s));
        let _ = hub_stop_for_task.set_enabled(matches!(
            s,
            crate::hub_status::HubStatus::Running { .. }
        ));

        let mut ticker = tokio::time::interval(REFRESH_INTERVAL);
        ticker.tick().await; // burn the immediate-fire tick
        loop {
            ticker.tick().await;
            let s = tokio::task::spawn_blocking(crate::hub_status::probe)
                .await
                .unwrap_or(crate::hub_status::HubStatus::NotRunning);
            let _ = hub_label_for_task.set_text(crate::hub_status::label(s));
            let _ = hub_stop_for_task.set_enabled(matches!(
                s,
                crate::hub_status::HubStatus::Running { .. }
            ));
        }
    });

    // Background poller. `MenuItem<R>` is internally `Arc`-wrapped and
    // cheap to clone, so the spawned task gets its own handle without
    // sharing mutable state. Failures (set_text errors, probe timeouts)
    // are swallowed — the next tick retries. Cancelled implicitly when
    // the runtime shuts down at app exit.
    let label_for_task = services_label.clone();
    tauri::async_runtime::spawn(async move {
        // Snapshot the very first probe to decide whether services
        // already existed before the launcher had a chance to start
        // anything. If yes → "managed externally". The launcher does
        // not currently track who started a given container, so this
        // is the cleanest signal we have without parsing podman labels.
        let initial = probe_services().await;
        let externally_managed = initial.running_count() == initial.total();

        let _ = label_for_task.set_text(format_label(&initial, externally_managed));

        let mut ticker = tokio::time::interval(REFRESH_INTERVAL);
        // First tick fires immediately by default — burn it so we wait a
        // full interval before the second probe.
        ticker.tick().await;
        loop {
            ticker.tick().await;
            let snapshot = probe_services().await;
            let _ = label_for_task.set_text(format_label(&snapshot, externally_managed));
        }
    });

    Ok(())
}

fn recent_projects_submenu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Submenu<R>> {
    let mut items: Vec<MenuItem<R>> = Vec::new();
    if let Some(db) = app.try_state::<Db>() {
        if let Ok(projects) = db.list_projects() {
            // Top 5 most recently updated
            let mut sorted = projects;
            sorted.sort_by_key(|p| std::cmp::Reverse(p.updated_at));
            for p in sorted.into_iter().take(5) {
                let id = format!("project_{}", p.id);
                if let Ok(item) = MenuItem::with_id(app, &id, &p.name, true, None::<&str>) {
                    items.push(item);
                }
            }
        }
    }

    let submenu = Submenu::with_id(app, "recent_projects", "Recent projects", true)?;
    if items.is_empty() {
        let placeholder = MenuItem::new(app, "(none yet)", false, None::<&str>)?;
        submenu.append(&placeholder)?;
    } else {
        for it in items.iter() {
            submenu.append(it)?;
        }
    }
    Ok(submenu)
}

// ---------------------------------------------------------------------------
// Live status detection
// ---------------------------------------------------------------------------

/// Snapshot of the three shared services we care about. Mirrors the
/// shape of `installer::ServicesStatus` but with display names attached
/// so the formatter can render "Weaviate, Ollama" without re-deriving
/// them from URLs.
struct ServiceSnapshot {
    services: Vec<(&'static str, bool)>,
}

impl ServiceSnapshot {
    fn running_count(&self) -> usize {
        self.services.iter().filter(|(_, up)| *up).count()
    }
    fn total(&self) -> usize {
        self.services.len()
    }
    fn running_names(&self) -> Vec<&'static str> {
        self.services
            .iter()
            .filter_map(|(name, up)| if *up { Some(*name) } else { None })
            .collect()
    }
}

/// Fast HTTP probe: short-timeout GET, treats 2xx/3xx as up. Returns
/// `false` on timeout, connection refused, DNS error, etc.
async fn probe_one(url: &str) -> bool {
    let client = match reqwest::Client::builder().timeout(PROBE_TIMEOUT).build() {
        Ok(c) => c,
        Err(_) => return false,
    };
    matches!(client.get(url).send().await, Ok(r) if r.status().as_u16() < 400)
}

/// Probe all shared services concurrently. Wall time bounded by
/// `PROBE_TIMEOUT`, not the sum.
async fn probe_services() -> ServiceSnapshot {
    // /v1/meta is more reliable than /v1/.well-known/ready for "is
    // Weaviate usable?" — see commands/lifecycle.rs::canonical_services.
    let weaviate_url = format!(
        "http://localhost:{}/v1/meta",
        WEAVIATE_PORT
    );
    let ollama_url = format!("http://localhost:{}/api/tags", OLLAMA_PORT);
    let code_embed_url = format!("http://localhost:{}/health", CODE_EMBED_PORT);

    let (w, o, c) = tokio::join!(
        probe_one(&weaviate_url),
        probe_one(&ollama_url),
        probe_one(&code_embed_url),
    );

    ServiceSnapshot {
        services: vec![("Weaviate", w), ("Ollama", o), ("code-embed", c)],
    }
}

/// Render the user-facing label. Rules per the v1.1 spec:
///   - 0 running               → "No services running"
///   - all running, externally → "Services: managed externally"
///   - all running             → "Services: N/N running"
///   - some running            → "Services: K/N (Name1, Name2)"
fn format_label(snap: &ServiceSnapshot, externally_managed: bool) -> String {
    let running = snap.running_count();
    let total = snap.total();

    if running == 0 {
        return "No services running".to_string();
    }
    if running == total && externally_managed {
        return "Services: managed externally".to_string();
    }
    if running == total {
        return format!("Services: {}/{} running", running, total);
    }
    let names = snap.running_names().join(", ");
    format!("Services: {}/{} ({})", running, total, names)
}

/// Render the "Check for updates" menu item. Rules:
///   - error / !available          → "Check for updates"
///   - available, count == 1       → "⚠ Update available (1 commit behind)"
///   - available, count >  1       → "⚠ Update available (N commits behind)"
fn format_update_label(status: &UpdateStatus) -> String {
    if !status.available || status.commit_count == 0 {
        return "Check for updates".to_string();
    }
    if status.commit_count == 1 {
        return "⚠ Update available (1 commit behind)".to_string();
    }
    format!(
        "⚠ Update available ({} commits behind)",
        status.commit_count
    )
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn snap(states: &[(&'static str, bool)]) -> ServiceSnapshot {
        ServiceSnapshot {
            services: states.to_vec(),
        }
    }

    #[test]
    fn label_none_running() {
        let s = snap(&[("Weaviate", false), ("Ollama", false), ("code-embed", false)]);
        assert_eq!(format_label(&s, false), "No services running");
        // externally_managed is irrelevant when nothing is up
        assert_eq!(format_label(&s, true), "No services running");
    }

    #[test]
    fn label_all_running_owned() {
        let s = snap(&[("Weaviate", true), ("Ollama", true), ("code-embed", true)]);
        assert_eq!(format_label(&s, false), "Services: 3/3 running");
    }

    #[test]
    fn label_all_running_externally_managed() {
        let s = snap(&[("Weaviate", true), ("Ollama", true), ("code-embed", true)]);
        assert_eq!(format_label(&s, true), "Services: managed externally");
    }

    #[test]
    fn label_partial_running_lists_names() {
        let s = snap(&[("Weaviate", true), ("Ollama", true), ("code-embed", false)]);
        // Even if startup snapshot was "all up", a later partial state is
        // never "managed externally" — it's a real partial outage.
        assert_eq!(format_label(&s, true), "Services: 2/3 (Weaviate, Ollama)");
        assert_eq!(format_label(&s, false), "Services: 2/3 (Weaviate, Ollama)");
    }

    #[test]
    fn label_single_running() {
        let s = snap(&[("Weaviate", false), ("Ollama", true), ("code-embed", false)]);
        assert_eq!(format_label(&s, false), "Services: 1/3 (Ollama)");
    }

    fn upd(available: bool, count: u32) -> UpdateStatus {
        UpdateStatus {
            available,
            current_sha: None,
            remote_sha: None,
            commit_count: count,
            branch: String::new(),
            last_checked: None,
            error: None,
        }
    }

    #[test]
    fn update_label_no_update() {
        assert_eq!(format_update_label(&upd(false, 0)), "Check for updates");
        assert_eq!(format_update_label(&upd(true, 0)), "Check for updates");
    }

    #[test]
    fn update_label_singular() {
        assert_eq!(
            format_update_label(&upd(true, 1)),
            "⚠ Update available (1 commit behind)"
        );
    }

    #[test]
    fn update_label_plural() {
        assert_eq!(
            format_update_label(&upd(true, 7)),
            "⚠ Update available (7 commits behind)"
        );
    }
}

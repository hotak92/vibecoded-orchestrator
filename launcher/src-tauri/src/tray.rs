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
    AppHandle, Manager, Runtime,
};

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

    // Recent projects sub-menu (populated on each open via menu rebuild).
    let recent = recent_projects_submenu(app)?;

    let updates = MenuItem::with_id(app, "check_updates", "Check for updates", true, None::<&str>)?;
    let about = MenuItem::with_id(app, "about", "About VibeCoded Tools", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;

    let menu = Menu::with_items(
        app,
        &[
            &open_item,
            &services_label,
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
            "quit" => app.exit(0),
            "check_updates" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                    let _ = w.eval(
                        "window.dispatchEvent(new CustomEvent('vct-check-updates'));",
                    );
                }
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
    let weaviate_url = format!(
        "http://localhost:{}/v1/.well-known/ready",
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
}

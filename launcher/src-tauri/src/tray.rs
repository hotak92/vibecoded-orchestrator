//! System tray menu (v1.1).
//!
//! Menu:
//!   - Open Launcher
//!   - Running services: N (label, non-clickable)
//!   - Recent projects (sub-menu, top 5)
//!   - Check for updates
//!   - About
//!   - Quit

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::{TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, Runtime,
};

use crate::db::Db;

pub fn setup<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let open_item = MenuItem::with_id(app, "open", "Open Launcher", true, None::<&str>)?;
    let services_label = MenuItem::with_id(
        app,
        "services_status",
        "Running services: …",
        false, // disabled (label only)
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

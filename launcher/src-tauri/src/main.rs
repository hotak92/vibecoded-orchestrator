// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // v0.2.91 (user decision #21): install the `tracing` subscriber FIRST,
    // so every diagnostic in this binary — including the WebKit preflight's
    // below, which runs before `run()` — has somewhere to go.
    //
    // Safe to precede the preflight: this seeds the level from
    // `VCO_LOG_LEVEL` only and installs a plain stderr subscriber. It does
    // NO I/O and spawns NO thread, so the preflight's `std::env::set_var`
    // soundness invariant (documented below) still holds. The stored
    // `logging.level` preference is folded in later, in `run()`, once the
    // launcher DB is open — see `src/logging.rs` for why that split exists
    // and why the Db open must NOT be hoisted up here.
    vct_launcher_temp_lib::logging::init_early();

    // v0.2.26: WebKitGTK + EGL/GBM pre-flight probe (Linux only — no-op
    // on macOS / Windows). MUST run before ANY other code, especially
    // before Tauri builder construction OR any thread spawn, because:
    //   1. WebKit reads WEBKIT_DISABLE_DMABUF_RENDERER once during its
    //      first init call; setting it later has no effect.
    //   2. std::env::set_var is unsafe across threads in some std versions.
    //      We're guaranteed single-threaded here.
    // See src/webkit_preflight.rs for the full rationale + the NVIDIA-
    // driver-mismatch crash signature this catches.
    #[cfg(target_os = "linux")]
    {
        let _outcome =
            vct_launcher_temp_lib::webkit_preflight::probe_and_apply_workaround_if_needed();
        // The probe logs its own outcome; nothing to do with _outcome here.
    }

    vct_launcher_temp_lib::run()
}

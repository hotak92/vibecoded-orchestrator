//! Editor / terminal launch helpers for "open project".
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the PATH presence probe
//! (`which_on_path`, delegating to the shared
//! `vct_launcher_core::paths::which_on_path`) and the VS Code / terminal-CLI
//! spawn helpers (`launch_in_vscode`, `launch_in_terminal_with_cli`) that
//! previously lived inline in `projects_v2.rs`. Behaviour is unchanged; the
//! facade re-exports every symbol so the `launch_project_in_editor` Tauri
//! command (which stays in the facade) reaches them via `super::*`.

use vct_launcher_core::process::CommandExt as _;

/// v0.2.77 (Part 7c task 3): delegates to the shared
/// `vct_launcher_core::paths::which_on_path` (one home) and collapses to a
/// bool for the presence-check callers. Two behaviour upgrades vs the prior
/// inline copy — both correct: (a) on Windows it now also probes
/// `.cmd`/`.bat` (VS Code's `code` shim + node-style launchers are `.cmd`,
/// which the `.exe`-only copy missed); (b) it matches on `is_file()` rather
/// than `exists()`, so a directory that happens to be named `code`/`claude`
/// no longer counts as the binary being present.
pub(crate) fn which_on_path(cmd: &str) -> bool {
    vct_launcher_core::paths::which_on_path(cmd).is_some()
}

pub(crate) fn launch_in_vscode(folder: &str) -> Result<(), String> {
    let mut cmd = std::process::Command::new("code").silent();
    cmd.arg(folder);
    match cmd.spawn() {
        Ok(_) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(
            "VS Code not found on PATH. Install Code from https://code.visualstudio.com/ \
             and ensure the `code` command is on your PATH, or use Claude Code CLI: \
             `cd <project> && claude`."
                .into(),
        ),
        Err(e) => Err(format!("failed to spawn editor: {}", e)),
    }
}

/// Spawn the system terminal in `folder` and run `claude` inside it. The
/// terminal flag varies by OS / DE — try a list of well-known options
/// and use the first that works.
pub(crate) fn launch_in_terminal_with_cli(folder: &str) -> Result<(), String> {
    if !which_on_path("claude") {
        return Err(
            "Claude Code CLI not found on PATH. Install from \
             https://docs.anthropic.com/en/docs/claude-code, or open in VS Code instead."
                .into(),
        );
    }

    // Per-OS terminal command. We use `cd <folder> && claude` as the
    // command-string; the terminal must support a flag that accepts a
    // shell command and keeps the window open afterwards.
    #[cfg(target_os = "linux")]
    let candidates: &[(&str, &[&str])] = &[
        ("gnome-terminal", &["--working-directory", folder, "--", "bash", "-lc", "claude; exec bash"]),
        ("konsole", &["--workdir", folder, "-e", "bash", "-lc", "claude; exec bash"]),
        ("xterm", &["-e", "bash", "-lc"]),
    ];
    #[cfg(target_os = "macos")]
    let candidates: &[(&str, &[&str])] = &[
        ("open", &["-a", "Terminal", folder]),
    ];
    #[cfg(target_os = "windows")]
    let candidates: &[(&str, &[&str])] = &[
        ("wt.exe", &["-d", folder, "powershell", "-NoExit", "-Command", "claude"]),
        ("powershell", &["-NoExit", "-Command", "claude"]),
    ];

    for (bin, args) in candidates {
        let mut cmd = std::process::Command::new(bin).silent();
        for a in *args {
            cmd.arg(a);
        }
        if cmd.spawn().is_ok() {
            return Ok(());
        }
    }

    Err("Could not find a system terminal to spawn (gnome-terminal, konsole, xterm, \
         Terminal.app, wt.exe). Install one or open in VS Code instead."
        .into())
}


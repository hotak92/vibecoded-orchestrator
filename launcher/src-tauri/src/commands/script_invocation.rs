// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.89 BUG 1 (Windows field audit): ONE home for the
// (program, prefix-args) resolution used to spawn bundled
// `.claude/scripts/*` wrapper scripts.
//
// Windows cannot CreateProcess a `.ps1` file directly — a PowerShell
// script is not a PE image, so `Command::new(<script.ps1>)` fails with
// os error 193 ("%1 is not a valid Win32 application"). Every bundled
// wrapper resolves to its `.ps1` sibling on Windows (see
// `codegraph::resolve_analyzer_script` / `orchestrator_core::script_bin`),
// so the spawn must be driven through
// `powershell.exe -NoProfile -ExecutionPolicy Bypass -File <script>`.
//
// This module is the verbatim extraction of the two previously-correct
// duplicate implementations (`kg_sync.rs::invocation_for` and the inline
// powershell branch in `orchestrator_core::build_script_command`).
// `codegraph.rs`'s direct `Command::new(&script)` spawn was the third
// call-site — the one that never gained the Windows branch, producing the
// 10/10 failed Windows codegraph builds. All three call-sites now route
// through this one function.

use std::path::{Path, PathBuf};

/// (program, prefix_args) for invoking a bundled wrapper script.
///
/// Windows: `("powershell.exe", ["-NoProfile","-ExecutionPolicy","Bypass","-File", <script>])`
/// POSIX:   `(<script>, [])` — the shell wrapper is chmod +x with a
/// `#!/bin/bash` shebang and is invoked directly.
///
/// The caller appends its own per-run args AFTER the prefix.
///
/// We pin `-ExecutionPolicy Bypass` on Windows because launcher installs
/// frequently land on machines with the default Restricted policy; the
/// script is bundled with VCO and trusted. Pattern matches install.ps1.
pub(crate) fn invocation_for(script: &Path) -> (PathBuf, Vec<String>) {
    if cfg!(windows) {
        (
            PathBuf::from("powershell.exe"),
            vec![
                "-NoProfile".to_string(),
                "-ExecutionPolicy".to_string(),
                "Bypass".to_string(),
                "-File".to_string(),
                script.to_string_lossy().to_string(),
            ],
        )
    } else {
        (script.to_path_buf(), Vec::new())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn invocation_for_picks_powershell_on_windows() {
        let script = Path::new("/x/.claude/scripts/kg-sync.ps1");
        let (program, args) = invocation_for(script);
        if cfg!(windows) {
            assert_eq!(program, PathBuf::from("powershell.exe"));
            assert!(args.contains(&"-File".to_string()));
            assert!(args.iter().any(|a| a.ends_with("kg-sync.ps1")));
        } else {
            assert_eq!(program, script);
            assert!(args.is_empty());
        }
    }

    /// Spawn-shape contract for the codegraph call-site: the 4-token
    /// powershell prefix + script path must come BEFORE any per-run args
    /// (`--project`, `--all`, …), and on POSIX the program IS the script
    /// with the caller args passed through untouched.
    #[test]
    fn invocation_for_prefix_precedes_caller_args() {
        let script = Path::new("/x/.claude/scripts/code-graph-analyze.ps1");
        let (program, prefix) = invocation_for(script);
        let mut argv: Vec<String> = prefix.clone();
        argv.extend(["--project".to_string(), "Foo".to_string()]);
        if cfg!(windows) {
            assert_eq!(program, PathBuf::from("powershell.exe"));
            assert_eq!(
                &argv[..4],
                &["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
                "the powershell prefix must lead the argv"
            );
            assert!(
                argv[4].ends_with("code-graph-analyze.ps1"),
                "the script path follows -File"
            );
            assert_eq!(&argv[5..], &["--project", "Foo"]);
        } else {
            assert_eq!(program, script);
            assert_eq!(argv, vec!["--project", "Foo"]);
        }
    }
}

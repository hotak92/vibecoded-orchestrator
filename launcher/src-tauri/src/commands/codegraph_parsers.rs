// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Code-graph parsers on demand — the launcher half (v0.2.102 W6-L).
//!
//! Two tree-sitter parsers (PowerShell, Protocol Buffers) are OPTIONAL: VCO
//! does not install them by default, it detects which languages each
//! registered project contains and OFFERS the missing parsers in a modal
//! (`CodegraphParsersModal.svelte`) before installing anything. Missing
//! default grammars (an opted-out or failed `codegraph-ts` install) are offered
//! the same way.
//!
//! ## Where each decision lives (A>B>C, rule A)
//!
//! Detection, the "Not now" rule and the installer are decided ONCE, in
//! Python: `python -m vco_lib.codegraph_parsers offer|decline|install`. This
//! file only spawns it and parses its one-JSON-object stdout. The install
//! runs in the orchestrator venv's interpreter (the one `python_or_err`
//! resolves), so it lands once per machine, never per project.
//!
//! ## Environment
//!
//! `offer` / `decline` read files only, so they run in the `vco_lib` env
//! sandbox. `install` INHERITS the launcher's environment: pip and the
//! language pack's grammar download must see the user's proxy settings.

use std::path::Path;
use std::process::Command;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::command;

use crate::commands::model_gateway::{orchestrator_pythonpath, python_or_err, run_to_completion_within};
use crate::commands::single_flight::{self, OP_CODEGRAPH_PARSERS_INSTALL};
use vct_launcher_core::process::CommandExt as _;

const MODULE: &str = "vco_lib.codegraph_parsers";
/// A cold scan of a large project plus one `stat` per cached directory.
const OFFER_TIMEOUT: Duration = Duration::from_secs(120);
const DECLINE_TIMEOUT: Duration = Duration::from_secs(120);
/// pip (900 s in the Python helper) + the verify step that downloads the
/// language pack's bundle (600 s) — the Python side enforces both; this is
/// the outer backstop.
const INSTALL_TIMEOUT: Duration = Duration::from_secs(1600);

/// Mirrors `vco_lib.codegraph_parsers.Offer`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ParserOffer {
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub label: String,
    #[serde(default)]
    pub package: String,
    #[serde(default)]
    pub optional: bool,
    #[serde(default)]
    pub languages: Vec<String>,
    #[serde(default)]
    pub projects: Vec<String>,
}

/// Mirrors `vco_lib.codegraph_parsers.OfferReport.to_dict`. Every field is
/// defaulted: a payload the launcher cannot fully parse degrades to "nothing
/// to offer", never to a guess.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ParserOfferReport {
    #[serde(default)]
    pub offers: Vec<ParserOffer>,
    #[serde(default)]
    pub broken: Vec<serde_json::Value>,
    #[serde(default)]
    pub errors: Vec<String>,
}

/// Mirrors `vco_lib.codegraph_parsers.InstallResult.to_dict`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ParserInstallReport {
    #[serde(default)]
    pub ok: bool,
    #[serde(default)]
    pub message: String,
    #[serde(default)]
    pub installed: Vec<String>,
    #[serde(default)]
    pub failed: Vec<serde_json::Value>,
    #[serde(default)]
    pub log_path: String,
    #[serde(default)]
    pub log_tail: String,
}

/// The CLI argv tail for a subcommand: `--parsers a,b` and one `--project`
/// per path. Pure, so the shape is unit-tested.
pub fn cli_args(sub: &str, parsers: &[String], projects: &[String]) -> Vec<String> {
    let mut out = vec![sub.to_string()];
    let ids: Vec<&str> = parsers.iter().map(|s| s.trim()).filter(|s| !s.is_empty()).collect();
    if !ids.is_empty() {
        out.push("--parsers".to_string());
        out.push(ids.join(","));
    }
    for p in projects.iter().map(|s| s.trim()).filter(|s| !s.is_empty()) {
        out.push("--project".to_string());
        out.push(p.to_string());
    }
    out
}

fn module_command(python: &Path, sandboxed: bool) -> Command {
    let mut cmd = Command::new(python).silent();
    cmd.arg("-m").arg(MODULE);
    if sandboxed {
        crate::services::vco_lib_bridge::reinject_minimal_env(&mut cmd);
    }
    if let Ok(root) = crate::commands::installer::find_local_repo_root() {
        cmd.env("PYTHONPATH", orchestrator_pythonpath(&root));
    }
    cmd
}

fn run_json(args: &[String], sandboxed: bool, timeout: Duration) -> Result<serde_json::Value, String> {
    let python = python_or_err()?;
    let mut cmd = module_command(&python, sandboxed);
    cmd.args(args);
    let (code, stdout, stderr) = run_to_completion_within(cmd, MODULE, timeout)?;
    let last = stdout.trim().lines().last().unwrap_or("").to_string();
    serde_json::from_str::<serde_json::Value>(&last).map_err(|e| {
        format!("{} exited {} and did not return JSON ({}): {}", MODULE, code, e, stderr.trim())
    })
}

/// Which parsers should the launcher offer for these projects? Installs
/// nothing; refreshes the per-project detection cache.
#[command]
pub async fn codegraph_parser_offers(project_paths: Vec<String>) -> Result<ParserOfferReport, String> {
    if project_paths.iter().all(|p| p.trim().is_empty()) {
        return Ok(ParserOfferReport::default());
    }
    let args = cli_args("offer", &[], &project_paths);
    let value = tauri::async_runtime::spawn_blocking(move || run_json(&args, true, OFFER_TIMEOUT))
        .await
        .map_err(|e| format!("parser offer task failed: {}", e))??;
    serde_json::from_value::<ParserOfferReport>(value)
        .map_err(|e| format!("unexpected codegraph_parsers offer payload: {}", e))
}

/// "Not now": remember the declined parsers for these projects' languages.
#[command]
pub async fn codegraph_parsers_decline(parsers: Vec<String>, project_paths: Vec<String>) -> Result<(), String> {
    if parsers.iter().all(|p| p.trim().is_empty()) {
        return Ok(());
    }
    let args = cli_args("decline", &parsers, &project_paths);
    tauri::async_runtime::spawn_blocking(move || run_json(&args, true, DECLINE_TIMEOUT))
        .await
        .map_err(|e| format!("parser decline task failed: {}", e))??;
    Ok(())
}

/// Install exactly the checked parsers into the orchestrator venv. One
/// install at a time per launcher; a refusal installs nothing.
#[command]
pub async fn codegraph_parsers_install(parsers: Vec<String>) -> Result<ParserInstallReport, String> {
    let ids: Vec<String> = parsers.into_iter().filter(|p| !p.trim().is_empty()).collect();
    if ids.is_empty() {
        return Err("no parser selected".to_string());
    }
    let Some(_claim) = single_flight::try_begin(OP_CODEGRAPH_PARSERS_INSTALL) else {
        return Err("a parser install is already running".to_string());
    };
    let args = cli_args("install", &ids, &[]);
    let value = tauri::async_runtime::spawn_blocking(move || run_json(&args, false, INSTALL_TIMEOUT))
        .await
        .map_err(|e| format!("parser install task failed: {}", e))??;
    let report = serde_json::from_value::<ParserInstallReport>(value)
        .map_err(|e| format!("unexpected codegraph_parsers install payload: {}", e))?;
    if report.ok {
        tracing::info!("[vct] code-graph parsers installed: {}", report.installed.join(", "));
    } else {
        tracing::error!(
            "[vct] code-graph parser install FAILED: {} (log: {}) {}",
            report.message,
            report.log_path,
            report.log_tail
        );
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_args_carry_exactly_the_given_parsers_and_projects() {
        let a = cli_args(
            "install",
            &["powershell".into(), " ".into(), "proto".into()],
            &[],
        );
        assert_eq!(a, vec!["install", "--parsers", "powershell,proto"]);
        let o = cli_args("offer", &[], &["/a".into(), "".into(), "/b c".into()]);
        assert_eq!(o, vec!["offer", "--project", "/a", "--project", "/b c"]);
    }

    #[test]
    fn an_unparseable_offer_payload_degrades_to_nothing_offered() {
        let r: ParserOfferReport = serde_json::from_str("{}").unwrap();
        assert!(r.offers.is_empty());
        let r: ParserOfferReport = serde_json::from_str(
            r#"{"offers":[{"id":"proto","label":"Protocol Buffers","languages":["proto"]}]}"#,
        )
        .unwrap();
        assert_eq!(r.offers[0].id, "proto");
        assert!(r.offers[0].projects.is_empty());
    }
}

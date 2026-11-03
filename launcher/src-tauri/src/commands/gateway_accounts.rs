// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! Claude accounts served by the model gateway — the launcher half (v0.2.105).
//!
//! Every read and write goes through `python -m vco_lib.gateway_accounts`,
//! the ONE home of the registry, its launcher-DB document, the gateway export
//! and the per-project env projection (A>B>C, rule A). This module only builds
//! the argv, spawns the bridge and relays its one JSON object. No credential
//! ever crosses it: the bridge reports login state by name and expiry only.
//!
//! The bridge's stdout is a machine contract — one JSON object with `ok` —
//! and a refused change (`{"ok": false, "error": ...}`, exit 2) is a
//! structured answer the GUI renders, not an error to swallow.

use tauri::command;

use crate::commands::model_gateway::{
    gateway_accounts_command, python_or_err, run_to_completion_within,
};

/// `list` asks the running gateway for its view (2 s) on top of interpreter
/// start-up; `set-project` re-runs one project's env projection.
const BRIDGE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(45);

/// One user action, as the bridge's argv. Pure, so the exact shape every
/// button sends is pinned by a test.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AccountsAction {
    List,
    Add {
        id: String,
        label: String,
        config_dir: String,
    },
    Remove {
        id: String,
    },
    SetDefault {
        id: String,
    },
    /// `account: None` = "use the default account".
    SetProject {
        project_id: String,
        account: Option<String>,
    },
    ProjectStatus {
        project_id: String,
    },
    Export,
}

pub fn accounts_argv(action: &AccountsAction) -> Vec<String> {
    let s = |v: &str| v.to_string();
    match action {
        AccountsAction::List => vec![s("list")],
        AccountsAction::Add {
            id,
            label,
            config_dir,
        } => vec![
            s("add"),
            s("--id"),
            id.clone(),
            s("--label"),
            label.clone(),
            s("--config-dir"),
            config_dir.clone(),
        ],
        AccountsAction::Remove { id } => vec![s("remove"), s("--id"), id.clone()],
        AccountsAction::SetDefault { id } => vec![s("set-default"), s("--id"), id.clone()],
        AccountsAction::SetProject {
            project_id,
            account,
        } => {
            let mut v = vec![s("set-project"), s("--project-id"), project_id.clone()];
            match account {
                Some(a) => {
                    v.push(s("--account"));
                    v.push(a.clone());
                }
                None => v.push(s("--default")),
            }
            v
        }
        AccountsAction::ProjectStatus { project_id } => {
            vec![s("project-status"), s("--project-id"), project_id.clone()]
        }
        AccountsAction::Export => vec![s("export")],
    }
}

/// Parse the bridge's stdout: any JSON object carrying a boolean `ok` is the
/// answer, whatever the exit code; anything else is a broken install,
/// reported with the bridge's stderr.
pub fn parse_bridge_output(
    code: i32,
    stdout: &str,
    stderr: &str,
) -> Result<serde_json::Value, String> {
    match serde_json::from_str::<serde_json::Value>(stdout.trim()) {
        Ok(v) if v.get("ok").and_then(|ok| ok.as_bool()).is_some() => Ok(v),
        _ => Err(format!(
            "vco_lib.gateway_accounts exited {} without its JSON answer: {}",
            code,
            stderr.trim()
        )),
    }
}

fn run_blocking(action: AccountsAction) -> Result<serde_json::Value, String> {
    let python = python_or_err()?;
    let root = crate::commands::installer::find_local_repo_root().ok();
    let mut cmd = gateway_accounts_command(&python, root.as_deref());
    for a in accounts_argv(&action) {
        cmd.arg(a);
    }
    let (code, stdout, stderr) =
        run_to_completion_within(cmd, "vco_lib.gateway_accounts", BRIDGE_TIMEOUT)?;
    parse_bridge_output(code, &stdout, &stderr)
}

async fn run(action: AccountsAction) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || run_blocking(action))
        .await
        .map_err(|e| format!("gateway-accounts task failed: {}", e))?
}

/// Re-write the gateway's copy of the registry once at boot, so a deleted or
/// stale `accounts.json` heals without a user action. Fire-and-forget,
/// soft-fail: a failure is logged, never blocks boot.
pub fn export_on_boot() {
    tauri::async_runtime::spawn(async {
        match run(AccountsAction::Export).await {
            Ok(v) if v["ok"].as_bool() == Some(true) => {}
            Ok(v) => tracing::warn!("[vct] gateway accounts: boot export refused: {}", v),
            Err(e) => tracing::warn!("[vct] gateway accounts: boot export failed: {}", e),
        }
    });
}

/// Accounts, login state (name and expiry only), projects per account, the
/// login commands to show, and the running gateway's per-account view.
#[command]
pub async fn gateway_accounts_list() -> Result<serde_json::Value, String> {
    run(AccountsAction::List).await
}

#[command]
pub async fn gateway_accounts_add(
    id: String,
    label: String,
    config_dir: String,
) -> Result<serde_json::Value, String> {
    run(AccountsAction::Add {
        id,
        label,
        config_dir,
    })
    .await
}

#[command]
pub async fn gateway_accounts_remove(id: String) -> Result<serde_json::Value, String> {
    run(AccountsAction::Remove { id }).await
}

#[command]
pub async fn gateway_accounts_set_default(id: String) -> Result<serde_json::Value, String> {
    run(AccountsAction::SetDefault { id }).await
}

/// `account: None` = "use the default account".
#[command]
pub async fn project_gateway_account_set(
    project_id: String,
    account: Option<String>,
) -> Result<serde_json::Value, String> {
    run(AccountsAction::SetProject {
        project_id,
        account,
    })
    .await
}

#[command]
pub async fn project_gateway_account_status(
    project_id: String,
) -> Result<serde_json::Value, String> {
    run(AccountsAction::ProjectStatus { project_id }).await
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(a: AccountsAction) -> Vec<String> {
        accounts_argv(&a)
    }

    #[test]
    fn every_action_has_the_cli_shape_the_bridge_parses() {
        assert_eq!(argv(AccountsAction::List), vec!["list"]);
        assert_eq!(
            argv(AccountsAction::Add {
                id: "work".into(),
                label: "Work seat".into(),
                config_dir: "/home/u/.claude-work".into()
            }),
            vec![
                "add",
                "--id",
                "work",
                "--label",
                "Work seat",
                "--config-dir",
                "/home/u/.claude-work"
            ]
        );
        assert_eq!(
            argv(AccountsAction::Remove { id: "work".into() }),
            vec!["remove", "--id", "work"]
        );
        assert_eq!(
            argv(AccountsAction::SetDefault { id: "work".into() }),
            vec!["set-default", "--id", "work"]
        );
        assert_eq!(
            argv(AccountsAction::ProjectStatus {
                project_id: "p1".into()
            }),
            vec!["project-status", "--project-id", "p1"]
        );
        assert_eq!(argv(AccountsAction::Export), vec!["export"]);
    }

    #[test]
    fn set_project_has_an_account_arm_and_a_default_arm() {
        assert_eq!(
            argv(AccountsAction::SetProject {
                project_id: "p1".into(),
                account: Some("work".into())
            }),
            vec!["set-project", "--project-id", "p1", "--account", "work"]
        );
        assert_eq!(
            argv(AccountsAction::SetProject {
                project_id: "p1".into(),
                account: None
            }),
            vec!["set-project", "--project-id", "p1", "--default"]
        );
    }

    #[test]
    fn a_refusal_is_an_answer_and_garbage_is_an_error() {
        let refused =
            parse_bridge_output(2, r#"{"ok": false, "error": "still selected by p1"}"#, "")
                .unwrap();
        assert_eq!(refused["ok"], false);
        let err = parse_bridge_output(1, "Traceback", "ImportError: vco_lib").unwrap_err();
        assert!(err.contains("ImportError: vco_lib"), "{err}");
        assert!(parse_bridge_output(0, r#"{"accounts": []}"#, "").is_err());
    }
}

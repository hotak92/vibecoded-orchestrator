//! The `compose up` argv for an explicit service list — ONE rule, in Python.
//!
//! `vco_lib.service_lifecycle.compose_up_args` owns it (the session hook,
//! the boot wrapper and install.py use it too); the launcher's lifecycle
//! commands and the hub's infra watchdog call it through
//! `python -m vco_lib.service_lifecycle compose-args --services "…" --json`
//! (A>B>C tier A: a user-triggered or once-per-heal path, never a hot loop).
//!
//! What the rule guarantees, and why Rust must not hand-build `up -d <svc>`:
//!   * `--no-deps` always — code_embed's `depends_on: ollama` would otherwise
//!     create a VCO Ollama next to an ADOPTED one (plan invariant I1);
//!   * `--profile gpu` whenever code_embed is named — podman-compose answers
//!     "unknown service: code_embed" for a profiled service it is not told
//!     to enable;
//!   * an empty list is NO compose call (never a bare, whole-stack `up -d`).
//!
//! Failing to run the Python rule is an error, never a fallback to a local
//! argv: a broken install surfaces instead of starting the wrong services.

use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::process::CommandExt as _;

/// A pure argv computation; past this the child is stuck.
const COMPOSE_ARGS_TIMEOUT: Duration = Duration::from_secs(30);

/// The interpreter for the rule, or why there is none (a broken install).
pub fn rule_python() -> Result<PathBuf, String> {
    crate::python_resolve::resolve_python_for_vco_lib().ok_or_else(|| {
        "no Python environment with vco_lib was found, so VCO cannot compute which \
         compose services to start (vco_lib.service_lifecycle) — re-run \
         `python install.py --update`"
            .to_string()
    })
}

/// The `up` argv (after the compose prefix and `-f` chain) that brings up
/// exactly `services`, from `python` run in the orchestrator clone `root`.
/// `Ok(vec![])` — and no spawn at all — for an empty list, or when the rule
/// dropped every service; the caller then runs no compose command.
pub async fn compose_up_args(
    python: &Path,
    root: &Path,
    services: &[&str],
    build: bool,
) -> Result<Vec<String>, String> {
    if services.is_empty() {
        return Ok(Vec::new());
    }
    let mut cmd = tokio::process::Command::new(python).silent();
    cmd.arg("-m")
        .arg("vco_lib.service_lifecycle")
        .arg("compose-args")
        .arg("--services")
        .arg(services.join(" "))
        .arg("--json");
    if build {
        cmd.arg("--build");
    }
    cmd.current_dir(root);
    cmd.stdin(std::process::Stdio::null());
    let output = match tokio::time::timeout(COMPOSE_ARGS_TIMEOUT, cmd.output()).await {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => return Err(format!("cannot run {} for the compose argv: {}", python.display(), e)),
        Err(_) => {
            return Err(format!(
                "vco_lib.service_lifecycle compose-args did not answer within {} s",
                COMPOSE_ARGS_TIMEOUT.as_secs()
            ))
        }
    };
    if !output.status.success() {
        return Err(format!(
            "vco_lib.service_lifecycle compose-args failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    parse_compose_args_reply(&String::from_utf8_lossy(&output.stdout))
}

/// `{"args": [...], "dropped": [...]}` → the argv. Pure.
pub fn parse_compose_args_reply(stdout: &str) -> Result<Vec<String>, String> {
    let v: serde_json::Value = serde_json::from_str(stdout.trim())
        .map_err(|e| format!("unreadable compose-args reply ({}): {}", e, stdout.trim()))?;
    let args = v
        .get("args")
        .and_then(|a| a.as_array())
        .ok_or_else(|| format!("compose-args reply has no `args` list: {}", stdout.trim()))?;
    args.iter()
        .map(|a| a.as_str().map(str::to_string).ok_or_else(|| format!("non-string compose arg {}", a)))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn checkout() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..")
    }

    fn python() -> PathBuf {
        crate::python_resolve::resolve_python_for_vco_lib_or("python3")
    }

    #[test]
    fn the_reply_parses_and_refuses_garbage() {
        assert_eq!(
            parse_compose_args_reply(r#"{"args": ["up", "-d", "--no-deps", "weaviate"], "dropped": []}"#).unwrap(),
            vec!["up", "-d", "--no-deps", "weaviate"]
        );
        assert!(parse_compose_args_reply("up -d weaviate").is_err());
        assert!(parse_compose_args_reply(r#"{"dropped": []}"#).is_err());
    }

    /// The ONE rule, run for real: `--no-deps` always, `--profile gpu` with
    /// code_embed, `--build` passed through; no spawn for an empty list.
    #[tokio::test]
    async fn the_python_rule_answers() {
        let py = python();
        let args = compose_up_args(&py, &checkout(), &["code_embed"], true).await.unwrap();
        assert_eq!(args, vec!["--profile", "gpu", "up", "-d", "--build", "--no-deps", "code_embed"]);
        let args = compose_up_args(&py, &checkout(), &["weaviate"], false).await.unwrap();
        assert_eq!(args, vec!["up", "-d", "--no-deps", "weaviate"]);
        let none = compose_up_args(Path::new("/nonexistent/python"), &checkout(), &[], false).await;
        assert_eq!(none.unwrap(), Vec::<String>::new(), "an empty list spawns nothing");
    }
}

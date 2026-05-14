//! Container picker (Bug E2, v0.2.7).
//!
//! When two containers share the `com.docker.compose.service=<name>`
//! label (e.g. a user's `claude-mcp` compose stack AND a stale
//! `infrastructure` stack), the v0.2.6 `find_container_for_service`
//! picked one non-deterministically. On the user's machine the broken
//! stale container won the race, bound the canonical port, and locked
//! the working container out.
//!
//! This module enumerates ALL candidates for a service, probes each
//! running candidate for service-specific "fullness" (collection count,
//! model list, …), and returns the ranked list. The frontend renders a
//! picker modal; the user's choice is persisted via
//! `services_pick_container` in [`commands::services_cmd`].
//!
//! Design notes:
//!
//!   - All container discovery goes through `<runtime> ps -a` (works
//!     identically on podman + docker). We never parse `inspect` JSON in
//!     enumerate-time because the format differs subtly between runtimes;
//!     a small set of `--format` columns is enough.
//!   - Fullness probes are `reqwest` HTTP calls against the canonical
//!     port on `localhost`. Same code path on Linux/macOS/Windows. Each
//!     probe has a 3-second timeout; a failed probe leaves
//!     `fullness: None` and is NOT an error — enumeration must always
//!     succeed if `<runtime> ps` succeeded.
//!   - Pure helpers (`parse_ps_row`, `is_canonical_collection`,
//!     `is_canonical_model`, `rank_candidates`) live at module scope so
//!     they can be unit-tested without spawning a real container
//!     runtime.

use serde::{Deserialize, Serialize};
use std::time::Duration;

use crate::services::runtime::RuntimeInfo;

/// HTTP timeout for each fullness probe. Kept short — a candidate that
/// can't answer in 3s is a candidate that isn't fully up, and ranking
/// will deprioritise it accordingly.
const PROBE_TIMEOUT: Duration = Duration::from_secs(3);

/// One candidate for adoption — a running or stopped container that
/// matches the canonical service label or port.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ContainerCandidate {
    pub container_name: String,
    pub compose_project: Option<String>,
    pub image: String,
    /// `running` | `exited` | `paused` | `created` | `dead` | `restarting`
    /// — whatever `<runtime> ps --format "{{.State}}"` emits, lowercased.
    pub status: String,
    /// `healthy` | `unhealthy` | `starting` — `None` when the container
    /// has no health check configured.
    pub health: Option<String>,
    /// Host port published by the container, if any. We only surface
    /// ports that match the canonical port the service expects — a
    /// candidate that publishes a different port is still listed (so
    /// the user can see it), but its `port_published` will be `None`
    /// when the canonical port isn't bound.
    pub port_published: Option<u16>,
    pub restart_count: u32,
    /// Service-specific "fullness" probe result. Populated only when
    /// `status == "running"` AND the canonical port responds. See
    /// per-service docs on [`ContainerFullness`].
    pub fullness: Option<ContainerFullness>,
}

/// Per-service "how loaded is this container" snapshot. Used by the
/// picker UI to help the user pick the candidate that already has their
/// data (versus a fresh empty container).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ContainerFullness {
    Weaviate {
        collection_count: u32,
        /// Names of canonical-collection prefixes / exact matches present
        /// in this container's schema. Empty when nothing canonical is
        /// detected (still a valid candidate — just empty data).
        canonical_collections_present: Vec<String>,
        weaviate_version: Option<String>,
    },
    Ollama {
        model_count: u32,
        canonical_models_present: Vec<String>,
    },
    CodeEmbed {
        /// `"gpu"` | `"cpu"` | `null` — reported by `/health`.
        backend: Option<String>,
        model: Option<String>,
        dim: Option<u32>,
    },
}

// ---------------------------------------------------------------------------
// Canonical name lists. Centralized so the picker (here) and the
// install.py / docs stay in lockstep when models/collections evolve.
// ---------------------------------------------------------------------------

/// Weaviate collection names / suffixes the orchestrator + MCP servers
/// expect. Match is "endswith" — so `ClaudeOrchestrator_CodeFunction`
/// matches `_CodeFunction`. Keep ALPHABETICAL for diff-friendliness.
pub const CANONICAL_WEAVIATE_COLLECTIONS: &[&str] = &[
    "ChatMessages",
    "ClaudeKnowledgeGraph",
    "DocumentChunks",
    "UnifiedMessages",
    "_CodeAPI",
    "_CodeClass",
    "_CodeFunction",
    "_CodeInteraction",
    "_CodeModule",
    "_KnowledgeGraph",
    "_development",
];

/// Ollama model names the orchestrator expects to find on `/api/tags`.
/// Match is exact (case-sensitive) — Ollama tags are lowercase by
/// convention.
pub const CANONICAL_OLLAMA_MODELS: &[&str] = &[
    "gemma4:e4b",
    "qwen3-embedding:0.6b",
    "qwen3.5:9b",
    "snowflake-arctic-embed2:latest",
    "unclemusclez/jina-embeddings-v2-base-code:latest",
];

/// Return true iff `name` matches one of the canonical Weaviate
/// collection patterns. Match rule:
///   - exact match → true
///   - suffix match (when canonical starts with `_`) → true
///   - otherwise → false
pub fn is_canonical_collection(name: &str) -> bool {
    for canon in CANONICAL_WEAVIATE_COLLECTIONS {
        if *canon == name {
            return true;
        }
        // Underscore-prefixed canonicals are suffix matchers — match e.g.
        // "ClaudeOrchestrator_CodeFunction" against "_CodeFunction".
        if canon.starts_with('_') && name.ends_with(*canon) {
            return true;
        }
    }
    false
}

/// Return true iff `model` exactly matches a canonical Ollama tag.
pub fn is_canonical_model(model: &str) -> bool {
    CANONICAL_OLLAMA_MODELS.iter().any(|m| *m == model)
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

/// One row of `<runtime> ps -a --format "..."` after parsing. Pure data
/// — split out of `enumerate_candidates` so tests don't need a real
/// container runtime.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PsRow {
    pub name: String,
    pub image: String,
    pub state: String,
    pub ports: String,
    pub labels: String,
}

/// Format string for the `ps` invocations below. Tab-delimited so we can
/// `splitn(5, '\t')` deterministically — container names, images, etc.
/// never contain tabs.
const PS_FORMAT: &str =
    "{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Ports}}\t{{.Labels}}";

/// Parse one row of `ps -a --format` output. Returns `None` for blank
/// lines or malformed rows.
pub(crate) fn parse_ps_row(line: &str) -> Option<PsRow> {
    let line = line.trim_end_matches('\r');
    if line.is_empty() {
        return None;
    }
    let mut parts = line.splitn(5, '\t');
    let name = parts.next()?.to_string();
    if name.is_empty() {
        return None;
    }
    let image = parts.next().unwrap_or("").to_string();
    let state = parts.next().unwrap_or("").to_string();
    let ports = parts.next().unwrap_or("").to_string();
    let labels = parts.next().unwrap_or("").to_string();
    Some(PsRow {
        name,
        image,
        state,
        ports,
        labels,
    })
}

/// Extract the `<key>` value from a `key=value,key2=value2,…` labels
/// string. Returns `None` when the key is absent OR the value is empty.
pub(crate) fn label_value<'a>(labels: &'a str, key: &str) -> Option<&'a str> {
    for pair in labels.split(',') {
        let pair = pair.trim();
        if let Some(rest) = pair.strip_prefix(&format!("{}=", key)) {
            if rest.is_empty() {
                return None;
            }
            return Some(rest);
        }
    }
    None
}

/// Return the host port published by a container, given its `Ports`
/// column from `ps`. Returns `Some(port)` only when `canonical_port` is
/// the one bound — otherwise `None`. Recognises both `0.0.0.0:8081->...`
/// and `:::8081->...` (IPv6) forms.
pub(crate) fn extract_published_port(ports: &str, canonical_port: u16) -> Option<u16> {
    let needles = [
        format!(":{}->", canonical_port),
        format!(":{} ->", canonical_port), // defensive against odd spacing
    ];
    for n in &needles {
        if ports.contains(n) {
            return Some(canonical_port);
        }
    }
    None
}

/// Return true when `row` is a viable candidate for `service` on
/// `canonical_port`. A row qualifies if EITHER:
///   - its labels include `com.docker.compose.service=<service>` or
///     `io.podman.compose.service=<service>` (the two compose label
///     conventions), OR
///   - its ports column publishes `canonical_port`.
pub(crate) fn row_matches_service(row: &PsRow, service: &str, canonical_port: u16) -> bool {
    if label_value(&row.labels, "com.docker.compose.service") == Some(service) {
        return true;
    }
    if label_value(&row.labels, "io.podman.compose.service") == Some(service) {
        return true;
    }
    extract_published_port(&row.ports, canonical_port).is_some()
}

/// Run `<runtime> ps -a --format <PS_FORMAT>` and return parsed rows.
async fn list_all_containers(runtime: &RuntimeInfo) -> Result<Vec<PsRow>, String> {
    let mut cmd = tokio::process::Command::new(&runtime.binary_path);
    cmd.args(["ps", "-a", "--format", PS_FORMAT]);
    let out = cmd
        .output()
        .await
        .map_err(|e| format!("spawn {} ps: {}", runtime.runtime.binary(), e))?;
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        return Err(format!(
            "{} ps failed (status {}): {}",
            runtime.runtime.binary(),
            out.status,
            stderr.trim()
        ));
    }
    let body = String::from_utf8_lossy(&out.stdout);
    let mut rows = Vec::new();
    for line in body.lines() {
        if let Some(r) = parse_ps_row(line) {
            rows.push(r);
        }
    }
    Ok(rows)
}

/// Read the `RestartCount` field from `<runtime> inspect <container>`.
/// Returns 0 on any error (soft-fail: a missing restart count must NOT
/// block enumeration).
async fn inspect_restart_count(runtime: &RuntimeInfo, name: &str) -> u32 {
    let mut cmd = tokio::process::Command::new(&runtime.binary_path);
    cmd.args([
        "inspect",
        "--format",
        "{{.RestartCount}}",
        name,
    ]);
    let Ok(out) = cmd.output().await else { return 0 };
    if !out.status.success() {
        return 0;
    }
    String::from_utf8_lossy(&out.stdout)
        .trim()
        .parse::<u32>()
        .unwrap_or(0)
}

/// Read the health-check state via `<runtime> inspect`. Returns `None`
/// when the container has no health check or inspection fails (soft).
async fn inspect_health(runtime: &RuntimeInfo, name: &str) -> Option<String> {
    let mut cmd = tokio::process::Command::new(&runtime.binary_path);
    cmd.args([
        "inspect",
        "--format",
        "{{if .State.Health}}{{.State.Health.Status}}{{end}}",
        name,
    ]);
    let out = cmd.output().await.ok()?;
    if !out.status.success() {
        return None;
    }
    let v = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if v.is_empty() {
        None
    } else {
        Some(v)
    }
}

/// Enumerate all containers that could plausibly back `service` on
/// `canonical_port`. Discovery rules:
///   1. List ALL containers via `<runtime> ps -a` (states included).
///   2. Filter to those matching the service label OR publishing
///      `canonical_port`.
///   3. For each running candidate, run the fullness probe against
///      `http://localhost:<canonical_port>`. Failed probes leave
///      `fullness: None`.
///
/// Returns candidates sorted by [`rank_candidates`] — best first.
pub async fn enumerate_candidates(
    runtime: &RuntimeInfo,
    service: &str,
    canonical_port: u16,
) -> Result<Vec<ContainerCandidate>, String> {
    let rows = list_all_containers(runtime).await?;
    let mut out: Vec<ContainerCandidate> = Vec::new();
    for row in rows.iter() {
        if !row_matches_service(row, service, canonical_port) {
            continue;
        }
        let status = row.state.to_lowercase();
        let compose_project = label_value(&row.labels, "com.docker.compose.project")
            .or_else(|| label_value(&row.labels, "io.podman.compose.project"))
            .map(|s| s.to_string());
        let port_published = extract_published_port(&row.ports, canonical_port);
        let restart_count = inspect_restart_count(runtime, &row.name).await;
        let health = inspect_health(runtime, &row.name).await;
        let fullness = if status == "running" && port_published.is_some() {
            probe_fullness(service, canonical_port).await
        } else {
            None
        };
        out.push(ContainerCandidate {
            container_name: row.name.clone(),
            compose_project,
            image: row.image.clone(),
            status,
            health,
            port_published,
            restart_count,
            fullness,
        });
    }
    rank_candidates(&mut out);
    Ok(out)
}

/// Sort candidates best-first. Ranking (descending priority):
///   1. Running AND has any canonical fullness signals
///   2. Running (regardless of fullness)
///   3. Anything else
///   4. Tie-break: lower restart_count first; then name ascending.
pub fn rank_candidates(candidates: &mut Vec<ContainerCandidate>) {
    candidates.sort_by(|a, b| {
        let score = |c: &ContainerCandidate| -> u8 {
            let running = c.status == "running";
            let has_canonical = matches!(
                &c.fullness,
                Some(ContainerFullness::Weaviate { canonical_collections_present: v, .. })
                    if !v.is_empty()
            ) || matches!(
                &c.fullness,
                Some(ContainerFullness::Ollama { canonical_models_present: v, .. })
                    if !v.is_empty()
            ) || matches!(&c.fullness, Some(ContainerFullness::CodeEmbed { .. }));
            match (running, has_canonical) {
                (true, true) => 3,
                (true, false) => 2,
                (false, _) => 1,
            }
        };
        let sa = score(a);
        let sb = score(b);
        sb.cmp(&sa)
            .then(a.restart_count.cmp(&b.restart_count))
            .then(a.container_name.cmp(&b.container_name))
    });
}

// ---------------------------------------------------------------------------
// Fullness probes
// ---------------------------------------------------------------------------

async fn probe_fullness(service: &str, port: u16) -> Option<ContainerFullness> {
    match service {
        "weaviate" => probe_weaviate(port).await,
        "ollama" => probe_ollama(port).await,
        "code_embed" => probe_code_embed(port).await,
        _ => None,
    }
}

fn build_probe_client() -> Option<reqwest::Client> {
    reqwest::Client::builder()
        .timeout(PROBE_TIMEOUT)
        .build()
        .ok()
}

async fn probe_weaviate(port: u16) -> Option<ContainerFullness> {
    let client = build_probe_client()?;
    let url = format!("http://localhost:{}/v1/schema", port);
    let resp = client.get(&url).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let body: serde_json::Value = resp.json().await.ok()?;
    let classes = body
        .get("classes")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let mut canonical = Vec::new();
    for c in &classes {
        let Some(name) = c.get("class").and_then(|v| v.as_str()) else {
            continue;
        };
        if is_canonical_collection(name) {
            canonical.push(name.to_string());
        }
    }
    // Also fetch /v1/meta for the version. Best-effort.
    let meta_url = format!("http://localhost:{}/v1/meta", port);
    let version = client
        .get(&meta_url)
        .send()
        .await
        .ok()
        .and_then(|r| {
            if r.status().is_success() {
                Some(r)
            } else {
                None
            }
        });
    let weaviate_version = match version {
        Some(r) => r
            .json::<serde_json::Value>()
            .await
            .ok()
            .and_then(|v| v.get("version").and_then(|x| x.as_str()).map(String::from)),
        None => None,
    };
    Some(ContainerFullness::Weaviate {
        collection_count: classes.len() as u32,
        canonical_collections_present: canonical,
        weaviate_version,
    })
}

async fn probe_ollama(port: u16) -> Option<ContainerFullness> {
    let client = build_probe_client()?;
    let url = format!("http://localhost:{}/api/tags", port);
    let resp = client.get(&url).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let body: serde_json::Value = resp.json().await.ok()?;
    let models = body
        .get("models")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let mut canonical = Vec::new();
    for m in &models {
        let Some(name) = m.get("name").and_then(|v| v.as_str()) else {
            continue;
        };
        if is_canonical_model(name) {
            canonical.push(name.to_string());
        }
    }
    Some(ContainerFullness::Ollama {
        model_count: models.len() as u32,
        canonical_models_present: canonical,
    })
}

async fn probe_code_embed(port: u16) -> Option<ContainerFullness> {
    let client = build_probe_client()?;
    let url = format!("http://localhost:{}/health", port);
    let resp = client.get(&url).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let body: serde_json::Value = resp.json().await.ok()?;
    let backend = body.get("backend").and_then(|v| v.as_str()).map(String::from);
    let model = body.get("model").and_then(|v| v.as_str()).map(String::from);
    let dim = body.get("dim").and_then(|v| v.as_u64()).map(|n| n as u32);
    Some(ContainerFullness::CodeEmbed {
        backend,
        model,
        dim,
    })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_ps_row_basic() {
        let line = "weaviate_claude\tdocker.io/semitechnologies/weaviate:1.27\trunning\t0.0.0.0:8081->8080/tcp\tcom.docker.compose.service=weaviate,com.docker.compose.project=claude-mcp";
        let row = parse_ps_row(line).expect("parses");
        assert_eq!(row.name, "weaviate_claude");
        assert_eq!(row.state, "running");
        assert!(row.ports.contains("8081"));
        assert!(row.labels.contains("project=claude-mcp"));
    }

    #[test]
    fn parse_ps_row_handles_trailing_cr() {
        let line = "name\timg\trunning\t\tlabel=foo\r";
        let row = parse_ps_row(line).expect("parses");
        assert_eq!(row.name, "name");
        assert_eq!(row.labels, "label=foo");
    }

    #[test]
    fn parse_ps_row_rejects_blank() {
        assert!(parse_ps_row("").is_none());
        assert!(parse_ps_row("   ").is_some()); // whitespace name is allowed
        // Empty-name row: tab-separated starting with empty first field.
        assert!(parse_ps_row("\timg\trunning\t\t").is_none());
    }

    #[test]
    fn label_value_extracts_present_key() {
        let labels = "com.docker.compose.service=weaviate,com.docker.compose.project=claude-mcp,io.podman.compose.version=1.0";
        assert_eq!(label_value(labels, "com.docker.compose.service"), Some("weaviate"));
        assert_eq!(label_value(labels, "com.docker.compose.project"), Some("claude-mcp"));
        assert_eq!(label_value(labels, "io.podman.compose.version"), Some("1.0"));
    }

    #[test]
    fn label_value_missing_key_returns_none() {
        let labels = "a=1,b=2";
        assert!(label_value(labels, "c").is_none());
        assert!(label_value("", "anything").is_none());
    }

    #[test]
    fn extract_published_port_finds_canonical() {
        let ports = "0.0.0.0:8081->8080/tcp, :::8081->8080/tcp";
        assert_eq!(extract_published_port(ports, 8081), Some(8081));
    }

    #[test]
    fn extract_published_port_misses_non_canonical() {
        let ports = "0.0.0.0:9999->8080/tcp";
        assert!(extract_published_port(ports, 8081).is_none());
    }

    #[test]
    fn extract_published_port_handles_empty_ports() {
        assert!(extract_published_port("", 8081).is_none());
    }

    #[test]
    fn row_matches_service_via_compose_label() {
        let row = PsRow {
            name: "weaviate_claude".into(),
            image: "weaviate:1.27".into(),
            state: "running".into(),
            ports: "".into(),
            labels: "com.docker.compose.service=weaviate".into(),
        };
        assert!(row_matches_service(&row, "weaviate", 8081));
        assert!(!row_matches_service(&row, "ollama", 8081));
    }

    #[test]
    fn row_matches_service_via_legacy_podman_label() {
        let row = PsRow {
            name: "weaviate_legacy".into(),
            image: "weaviate:1.20".into(),
            state: "exited".into(),
            ports: "".into(),
            labels: "io.podman.compose.service=weaviate".into(),
        };
        assert!(row_matches_service(&row, "weaviate", 8081));
    }

    #[test]
    fn row_matches_service_via_port_fallback() {
        let row = PsRow {
            name: "hand_run".into(),
            image: "weaviate:1.27".into(),
            state: "running".into(),
            ports: "0.0.0.0:8081->8080/tcp".into(),
            labels: "".into(),
        };
        assert!(row_matches_service(&row, "weaviate", 8081));
    }

    #[test]
    fn row_matches_service_rejects_unrelated() {
        let row = PsRow {
            name: "redis".into(),
            image: "redis:7".into(),
            state: "running".into(),
            ports: "0.0.0.0:6379->6379/tcp".into(),
            labels: "".into(),
        };
        assert!(!row_matches_service(&row, "weaviate", 8081));
    }

    #[test]
    fn canonical_collection_exact_and_suffix() {
        assert!(is_canonical_collection("ClaudeKnowledgeGraph"));
        assert!(is_canonical_collection("ChatMessages"));
        // Suffix match — orchestrator-prefixed collection.
        assert!(is_canonical_collection("ClaudeOrchestrator_CodeFunction"));
        assert!(is_canonical_collection("SD15_KnowledgeGraph"));
        assert!(is_canonical_collection("ARTup_development"));
        // Non-canonical: no exact match, no underscore suffix match.
        assert!(!is_canonical_collection("RandomClass"));
        assert!(!is_canonical_collection(""));
    }

    #[test]
    fn canonical_collection_does_not_match_partial_underscore() {
        // "_CodeFunction" must match a name that ENDS WITH "_CodeFunction",
        // not one that merely contains it as a substring without the
        // underscore. (Defensive — guards against accidental
        // `.contains()` substitution during a refactor.)
        assert!(!is_canonical_collection("CodeFunction")); // no underscore
        assert!(is_canonical_collection("X_CodeFunction"));
    }

    #[test]
    fn canonical_model_exact_match_only() {
        assert!(is_canonical_model("qwen3-embedding:0.6b"));
        assert!(is_canonical_model("gemma4:e4b"));
        assert!(!is_canonical_model("qwen3-embedding:0.6b-extra"));
        assert!(!is_canonical_model("qwen3-embedding"));
        assert!(!is_canonical_model(""));
    }

    fn mk(name: &str, status: &str, restart_count: u32, fullness: Option<ContainerFullness>) -> ContainerCandidate {
        ContainerCandidate {
            container_name: name.into(),
            compose_project: None,
            image: "img".into(),
            status: status.into(),
            health: None,
            port_published: Some(8081),
            restart_count,
            fullness,
        }
    }

    #[test]
    fn rank_prefers_running_with_canonical_data() {
        let mut v = vec![
            mk("stale_empty", "running", 0, Some(ContainerFullness::Weaviate {
                collection_count: 0,
                canonical_collections_present: vec![],
                weaviate_version: None,
            })),
            mk("rich", "running", 0, Some(ContainerFullness::Weaviate {
                collection_count: 11,
                canonical_collections_present: vec!["ClaudeKnowledgeGraph".into()],
                weaviate_version: Some("1.27.0".into()),
            })),
            mk("exited", "exited", 0, None),
        ];
        rank_candidates(&mut v);
        assert_eq!(v[0].container_name, "rich");
        assert_eq!(v[1].container_name, "stale_empty");
        assert_eq!(v[2].container_name, "exited");
    }

    #[test]
    fn rank_tiebreaks_by_restart_count_then_name() {
        let mut v = vec![
            mk("zzz", "running", 0, None),
            mk("aaa", "running", 5, None),
            mk("mmm", "running", 0, None),
        ];
        rank_candidates(&mut v);
        // restart_count=0 first (mmm, zzz alphabetical), then restart_count=5
        assert_eq!(v[0].container_name, "mmm");
        assert_eq!(v[1].container_name, "zzz");
        assert_eq!(v[2].container_name, "aaa");
    }

    #[test]
    fn rank_handles_empty_input() {
        let mut v: Vec<ContainerCandidate> = vec![];
        rank_candidates(&mut v);
        assert!(v.is_empty());
    }

    /// Pins the canonical collection list against accidental removal —
    /// the orchestrator + MCP servers HARD-DEPEND on these names. If
    /// you're adding a new collection here, also update the orchestrator
    /// `weaviate_init.py` so the picker matches what install creates.
    #[test]
    fn canonical_collections_include_core_set() {
        let must_have = [
            "ClaudeKnowledgeGraph",
            "_CodeFunction",
            "_CodeClass",
            "_CodeModule",
            "_development",
            "DocumentChunks",
        ];
        for n in &must_have {
            assert!(
                CANONICAL_WEAVIATE_COLLECTIONS.contains(n),
                "canonical list missing required entry {}",
                n
            );
        }
    }

    /// Same intent as the collection test — guard against an Ollama
    /// canonical model accidentally dropping out of the list when a
    /// future refactor renames variables.
    #[test]
    fn canonical_models_include_core_set() {
        let must_have = ["qwen3-embedding:0.6b", "gemma4:e4b", "qwen3.5:9b"];
        for m in &must_have {
            assert!(
                CANONICAL_OLLAMA_MODELS.contains(m),
                "canonical list missing required model {}",
                m
            );
        }
    }
}

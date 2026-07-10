//! KG dashboard backend.
//!
//! Proxies to the user's local Weaviate instance so the React UI can render
//! an Obsidian-style knowledge graph, search nodes, and manage per-project
//! collection access without needing a Weaviate client library in JS.
//!
//! Access enforcement happens in the launcher, NOT in Weaviate: the DB
//! table `kg_collection_access` decides what a given project can read or
//! write to. The launcher refuses requests for collections the project
//! hasn't been granted access to. This is a local-only policy layer.

use serde::{Deserialize, Serialize};
use tauri::{command, State};

use crate::config::LocalConfig;
use crate::db::Db;

/// Resolve the local Weaviate URL with full env > config-file > default
/// precedence. The compiled default mirrors `config::DEFAULT_WEAVIATE_URL`
/// so a Tauri command invoked outside a managed `LocalConfig` (e.g. unit
/// tests that spin up an axum router without a full app) still gets a
/// working URL — `WEAVIATE_URL` env var continues to work as the test
/// override hook (see hub::cli_api tests).
fn weaviate_url(cfg: Option<&LocalConfig>) -> String {
    if let Ok(v) = std::env::var("VCT_WEAVIATE_URL") {
        if !v.is_empty() {
            return v;
        }
    }
    if let Ok(v) = std::env::var("WEAVIATE_URL") {
        if !v.is_empty() {
            return v;
        }
    }
    cfg.map(|c| c.weaviate_url.clone())
        .unwrap_or_else(|| crate::config::DEFAULT_WEAVIATE_URL.to_string())
}

fn weaviate_client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .map_err(|e| format!("http client: {}", e))
}

// ─── Collection access ───────────────────────────────────────────────────

#[derive(Debug, Serialize)]
pub struct KgCollectionAccess {
    pub name: String,
    pub node_count: u32,
    pub access: String, // "read" | "write" | "none"
    pub is_shared: bool,
}

/// Heuristic: returns true for the codegraph entity classes —
/// both per-project prefixed (`<Prefix>_CodeModule` etc.) AND bare
/// (`CodeModule` etc., legacy pre-multi-project orchestrator schema).
///
/// Used by the KG dashboard to filter codegraph classes OUT (they
/// belong on the dedicated /codegraph route), and by the codegraph
/// dashboard to filter them IN.
///
/// v0.2.18 (Commit 8, locked 2026-05-19): bare-name classes are
/// hidden from BOTH dashboards in the GUI — they hold legitimate
/// pre-multi-project data but don't correspond to any current
/// project, so surfacing them as standalone "projects" would be
/// misleading. The underlying data stays in Weaviate; only the GUI
/// representation is filtered. Mirrors
/// `vco_lib.weaviate_schema.is_code_collection`'s Python predicate
/// (which considers both bare and prefixed names code-shaped).
fn is_codegraph_class(name: &str) -> bool {
    // Bare-name match (CodeFunction, CodeClass, …).
    if matches!(
        name,
        "CodeModule" | "CodeClass" | "CodeFunction" | "CodeAPI" | "CodeInteraction"
    ) {
        return true;
    }
    // Prefixed-name match (<Prefix>_CodeFunction, …).
    matches!(
        name.rsplit_once('_').map(|(_, suffix)| suffix),
        Some("CodeModule")
            | Some("CodeClass")
            | Some("CodeFunction")
            | Some("CodeAPI")
            | Some("CodeInteraction")
    )
}

/// List all Weaviate collections along with this project's access level.
///
/// A collection appears in the result if Weaviate has it AND the project
/// has an explicit access row OR the collection is the declared shared
/// cross-project one (`sharedVCT` by convention, matches the
/// `SHARED_KG_COLLECTION` setting).
///
/// Filters OUT codegraph entity classes (`<Prefix>_CodeFunction` etc.) —
/// those live in the dedicated /codegraph dashboard. Mixing them into
/// the KG card grid produced 25+ tiles per project where most were
/// duplicates of the same codebase. Reported 2026-04-28.
#[command]
pub async fn kg_list_collections(
    project_id: String,
    db: State<'_, Db>,
    cfg: State<'_, LocalConfig>,
) -> Result<Vec<KgCollectionAccess>, String> {
    let base = weaviate_url(Some(&cfg));
    let client = weaviate_client()?;
    // Weaviate exposes a schema listing at /v1/schema
    let schema_resp = client
        .get(format!("{}/v1/schema", &base))
        .send()
        .await
        .map_err(|e| format!("weaviate /v1/schema: {}", e))?;

    if !schema_resp.status().is_success() {
        return Err(format!(
            "weaviate returned {}",
            schema_resp.status().as_u16()
        ));
    }

    let schema: serde_json::Value = schema_resp
        .json()
        .await
        .map_err(|e| format!("schema parse: {}", e))?;

    let classes = schema
        .get("classes")
        .and_then(|c| c.as_array())
        .cloned()
        .unwrap_or_default();

    let grants: std::collections::HashMap<String, String> = db
        .kg_list_access(&project_id)?
        .into_iter()
        .collect();

    let mut out = Vec::with_capacity(classes.len());
    for cls in classes {
        let name = cls
            .get("class")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if name.is_empty() {
            continue;
        }
        // Skip codegraph entity classes — they belong on /codegraph,
        // not in the KG card grid. See is_codegraph_class doc.
        if is_codegraph_class(&name) {
            continue;
        }
        let access = grants
            .get(&name)
            .cloned()
            .unwrap_or_else(|| "none".to_string());
        let node_count = fetch_class_count(&client, &base, &name).await.unwrap_or(0);
        // v0.2.49 access-matrix Phase 2 (item #7, S-1) — replace the
        // historical 3-tier substring heuristic with byte-equality
        // against the persisted canonical name from Step A's
        // `app_state['orchestrator_root_kg_collection']`. Closes audit
        // finding S-1: different code paths previously classified
        // identical collection names differently because they
        // consulted different constants (helper canonical vs literal
        // "sharedVCT" vs lowercase("shared") substring).
        //
        // Tradeoff: a user-created collection named e.g. "MyShared_KG"
        // no longer sorts with the canonical shared root. Acceptable
        // — and arguably more honest — per the plan's S-1 disposition.
        // The pre-v0.2.49 heuristic also flagged legacy migration
        // names (`VibeCodedTools_KnowledgeGraph`,
        // `VibecodedOrchestrator_KnowledgeGraph`); white-label installs
        // override the persisted canonical via the
        // `VCT_ORCHESTRATOR_ROOT_KG_COLLECTION` env at install time, so
        // legacy-rename detection migrates to the orchestrator-root
        // setting row instead of being inferred from substrings.
        //
        // Soft-fail: a DB error reading the setting falls back to
        // `false` (this is a display-sort heuristic, not security).
        let canonical_root = db
            .get_orchestrator_root_kg_collection()
            .unwrap_or_else(|_| String::new());
        let is_shared = !canonical_root.is_empty() && name == canonical_root;
        out.push(KgCollectionAccess {
            name,
            node_count,
            access,
            is_shared,
        });
    }
    // Alphabetical, shared collections first.
    out.sort_by(|a, b| b.is_shared.cmp(&a.is_shared).then(a.name.cmp(&b.name)));
    Ok(out)
}

// ─── Codegraph dashboard listing ─────────────────────────────────────────

/// One project's codegraph footprint: the prefix + total entity count
/// across all five namespaced classes (CodeModule + CodeClass +
/// CodeFunction + CodeAPI + CodeInteraction). The five classes share
/// settings (one prefix, one embedding model) so we render one card
/// per project on the codegraph dashboard, not five.
#[derive(Debug, Serialize)]
pub struct CodegraphProjectSummary {
    /// Project's bare name (e.g. "MyProject", "VideoFrames"). Used as the
    /// dashboard card heading.
    pub project_name: String,
    /// Sanitized prefix used for namespacing the five classes
    /// (e.g. "MyProject" → `MyProject_CodeFunction`). Empty if the project
    /// has no codegraph_binding row yet.
    pub prefix: String,
    pub module_count: u32,
    pub class_count: u32,
    pub function_count: u32,
    pub api_count: u32,
    pub interaction_count: u32,
    /// Acting project's access level: "read" | "write" | "none". For
    /// the project's OWN codegraph this is always "write".
    pub access: String,
}

/// Scan Weaviate for codegraph classes, group them by prefix (= project),
/// and return one summary row per project. Used by the /codegraph
/// dashboard's card grid. The acting project sees its own codegraph
/// (always "write") plus any others it's been granted read access to
/// via the codegraph access table.
///
/// v0.2.16 (W4 / 0.11): `include_untracked_projects` (default `false`)
/// filters out classes whose prefix doesn't map to any currently-tracked
/// project. The GUI's /codegraph route uses `Some(false)` for a clean
/// view; the advanced /preferences/weaviate-untracked page passes
/// `Some(true)` to surface data from since-deleted projects. The
/// default keeps pre-v0.2.16 callers on the clean view automatically.
#[command]
pub async fn codegraph_list_projects(
    project_id: String,
    db: State<'_, Db>,
    cfg: State<'_, LocalConfig>,
    include_untracked_projects: Option<bool>,
) -> Result<Vec<CodegraphProjectSummary>, String> {
    let include_untracked = include_untracked_projects.unwrap_or(false);
    let base = weaviate_url(Some(&cfg));
    let client = weaviate_client()?;
    let schema_resp = client
        .get(format!("{}/v1/schema", &base))
        .send()
        .await
        .map_err(|e| format!("weaviate /v1/schema: {}", e))?;
    if !schema_resp.status().is_success() {
        return Err(format!("weaviate returned {}", schema_resp.status().as_u16()));
    }
    let schema: serde_json::Value = schema_resp
        .json()
        .await
        .map_err(|e| format!("schema parse: {}", e))?;
    let classes = schema
        .get("classes")
        .and_then(|c| c.as_array())
        .cloned()
        .unwrap_or_default();

    // Group <Prefix>_<Suffix> classes by Prefix.
    let mut by_prefix: std::collections::HashMap<String, std::collections::HashMap<String, u32>> =
        std::collections::HashMap::new();
    for cls in classes {
        let name = cls
            .get("class")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if name.is_empty() || !is_codegraph_class(&name) {
            continue;
        }
        if let Some((prefix, suffix)) = name.rsplit_once('_') {
            let count = fetch_class_count(&client, &base, &name).await.unwrap_or(0);
            by_prefix
                .entry(prefix.to_string())
                .or_default()
                .insert(suffix.to_string(), count);
        }
    }

    // Resolve access: the acting project's own codegraph is "write";
    // others depend on db.codegraph_check (returns Some("read") if
    // granted). Resolve project name from the prefix by looking up
    // project_codegraph_bindings — if no row, fall back to the prefix.
    let acting = db.get_project(&project_id)?;

    // v0.2.16 (W4 / 0.11): build the set of currently-tracked prefixes
    // when filtering is requested. Includes the canonical_class_prefix
    // for every tracked project — that's the prefix the analyzer writes
    // to. Anything not in this set is "untracked" and only surfaces when
    // include_untracked_projects=true. Note: this filter is keyed on
    // EXACT prefix match. Renamed projects keep their old data under
    // the old prefix as untracked (matches plan 0.10 edge case — no
    // automatic rename, user re-analyses to migrate).
    let tracked_prefixes: std::collections::HashSet<String> = if include_untracked {
        std::collections::HashSet::new()
    } else {
        match db.list_projects() {
            Ok(rows) => rows
                .into_iter()
                .flat_map(|row| {
                    let mut prefixes = Vec::new();
                    // Primary signal: the project's codegraph binding row
                    // (what new analyses write to).
                    if let Ok(Some(b)) = db.get_project_codegraph_binding(&row.id) {
                        prefixes.push(b.collection_prefix);
                    }
                    // Fallback: canonical from name (matches what
                    // analyze_code_graph.py uses when no binding exists).
                    if let Ok(p) = crate::project_naming::canonical_class_prefix(&row.name) {
                        prefixes.push(p);
                    }
                    prefixes
                })
                .collect(),
            Err(_) => std::collections::HashSet::new(),
        }
    };

    let mut out: Vec<CodegraphProjectSummary> = Vec::new();
    for (prefix, counts) in by_prefix {
        // v0.2.16 (W4 / 0.11): filter out untracked-project prefixes.
        if !include_untracked && !tracked_prefixes.contains(&prefix) {
            continue;
        }
        // Find which project owns this prefix (best effort).
        let owner_project_id = db
            .find_project_by_codegraph_prefix(&prefix)
            .ok()
            .flatten();
        let access = if let Some(ref owner_id) = owner_project_id {
            if owner_id == &project_id {
                "write".to_string()
            } else {
                db.codegraph_check(owner_id, &project_id)
                    .ok()
                    .flatten()
                    .unwrap_or_else(|| "none".to_string())
            }
        } else {
            // No owner row found — the prefix exists in Weaviate but the
            // launcher DB has no record. Show as "none" so the user can
            // see it but can't browse without explicit access.
            "none".to_string()
        };
        let project_name = owner_project_id
            .as_ref()
            .and_then(|id| acting.as_ref().filter(|p| &p.id == id).map(|p| p.name.clone()))
            .unwrap_or_else(|| prefix.clone());
        out.push(CodegraphProjectSummary {
            project_name,
            prefix: prefix.clone(),
            module_count: counts.get("CodeModule").copied().unwrap_or(0),
            class_count: counts.get("CodeClass").copied().unwrap_or(0),
            function_count: counts.get("CodeFunction").copied().unwrap_or(0),
            api_count: counts.get("CodeAPI").copied().unwrap_or(0),
            interaction_count: counts.get("CodeInteraction").copied().unwrap_or(0),
            access,
        });
    }
    // Acting project's own codegraph first, then alphabetical.
    out.sort_by(|a, b| {
        let a_own = a.access == "write";
        let b_own = b.access == "write";
        b_own.cmp(&a_own).then(a.project_name.cmp(&b.project_name))
    });
    Ok(out)
}

async fn fetch_class_count(
    client: &reqwest::Client,
    base_url: &str,
    class: &str,
) -> Result<u32, String> {
    // Weaviate GraphQL: { Aggregate { <Class> { meta { count } } } }
    let body = serde_json::json!({
        "query": format!("{{ Aggregate {{ {class} {{ meta {{ count }} }} }} }}", class = class)
    });
    let resp = client
        .post(format!("{}/v1/graphql", base_url))
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("graphql: {}", e))?;
    let v: serde_json::Value = resp.json().await.map_err(|e| format!("parse: {}", e))?;
    Ok(v.pointer(&format!("/data/Aggregate/{}/0/meta/count", class))
        .and_then(|n| n.as_u64())
        .unwrap_or(0) as u32)
}

// `kg_set_collection_access` (the singular per-row setter) was removed
// 2026-05-09 as part of the dead-code sweep. The GUI uses
// `kg_set_collection_access_mode` (mode-based, fans out access rows for
// every project) exclusively, and that function calls `db.kg_set_access`
// directly. No external consumer depended on the singular Tauri command.
// If a future caller needs the per-row primitive, expose `db.kg_set_access`
// (already public on the Db handle) instead of re-adding a Tauri command.

// ─── Graph load for Obsidian-style view ─────────────────────────────────

#[derive(Debug, Serialize)]
pub struct KgNode {
    pub id: String,
    pub title: String,
    pub node_type: String,
    pub tags: Vec<String>,
    pub collection: String,
    pub excerpt: String,
    pub file_path: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct KgEdge {
    pub from_id: String,
    pub to_id: String,
    pub relationship_type: String,
}

#[derive(Debug, Serialize)]
pub struct KgGraph {
    pub nodes: Vec<KgNode>,
    pub edges: Vec<KgEdge>,
    pub total_nodes_in_collection: u32,
    pub truncated: bool,
}

fn require_kg_read(db: &Db, project_id: &str, collection: &str) -> Result<(), String> {
    let level = db.kg_get_access(project_id, collection)?;
    match level.as_deref() {
        Some("read") | Some("write") => Ok(()),
        // R9 (v0.2.76): an ABSENT row (`None`) is a DENY BY DESIGN — the read
        // gate is deny-by-default, so a project with no kg_collection_access
        // row for this collection has no read access. Do NOT align this with
        // the WRITE endpoint's `resolve_default_access_level` fall-through
        // (which fails OPEN to a permissive default when no row exists): the
        // read path must never fabricate access for an ungranted collection.
        _ => Err(format!(
            "project {} has no read access to collection {}",
            project_id, collection
        )),
    }
}

#[command]
pub async fn kg_load_graph(
    project_id: String,
    collection: String,
    tag_filter: Option<Vec<String>>,
    max_nodes: Option<u32>,
    db: State<'_, Db>,
    cfg: State<'_, LocalConfig>,
) -> Result<KgGraph, String> {
    require_kg_read(&db, &project_id, &collection)?;
    let limit = max_nodes.unwrap_or(500).min(2000);

    let base = weaviate_url(Some(&cfg));
    let client = weaviate_client()?;
    let total = fetch_class_count(&client, &base, &collection)
        .await
        .unwrap_or(0);

    // Build a GraphQL query to fetch the top-N nodes with tags + typed links.
    // We intentionally request only lightweight fields (no full content) to
    // keep the graph render fast.
    let tag_filter_clause = match tag_filter.as_ref() {
        Some(tags) if !tags.is_empty() => {
            let joined = tags
                .iter()
                .map(|t| format!("\"{}\"", t.replace('"', "")))
                .collect::<Vec<_>>()
                .join(",");
            format!(
                ", where: {{path:[\"tags\"], operator:ContainsAny, valueText:[{}]}}",
                joined
            )
        }
        _ => String::new(),
    };

    let q = format!(
        "{{ Get {{ {cls}(limit: {lim}{tf}) {{ title node_type tags content file_path typed_links _additional {{ id }} }} }} }}",
        cls = collection,
        lim = limit,
        tf = tag_filter_clause,
    );
    let resp = client
        .post(format!("{}/v1/graphql", &base))
        .json(&serde_json::json!({ "query": q }))
        .send()
        .await
        .map_err(|e| format!("graphql: {}", e))?;
    let body: serde_json::Value = resp.json().await.map_err(|e| format!("parse: {}", e))?;

    let empty_vec = vec![];
    let items = body
        .pointer(&format!("/data/Get/{}", collection))
        .and_then(|v| v.as_array())
        .unwrap_or(&empty_vec);

    let mut nodes = Vec::with_capacity(items.len());
    let mut edges = Vec::new();
    let mut title_to_id: std::collections::HashMap<String, String> =
        std::collections::HashMap::new();

    // First pass: build nodes + title -> id lookup.
    for item in items {
        let id = item
            .pointer("/_additional/id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let title = item
            .get("title")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if id.is_empty() || title.is_empty() {
            continue;
        }
        let node_type = item
            .get("node_type")
            .and_then(|v| v.as_str())
            .unwrap_or("concept")
            .to_string();
        let tags: Vec<String> = item
            .get("tags")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|t| t.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        let content = item
            .get("content")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .chars()
            .take(300)
            .collect::<String>();
        let file_path = item
            .get("file_path")
            .and_then(|v| v.as_str())
            .map(str::to_string);

        title_to_id.insert(title.clone(), id.clone());
        nodes.push(KgNode {
            id,
            title,
            node_type,
            tags,
            collection: collection.clone(),
            excerpt: content,
            file_path,
        });
    }

    // Second pass: resolve typed_links `[rel::Target]` into edges. Links
    // to titles we didn't fetch are skipped — caller can fetch the target
    // on demand if the user clicks on a "shadow" reference.
    for (item, node) in items.iter().zip(nodes.iter()) {
        let links: Vec<String> = item
            .get("typed_links")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|t| t.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        for raw in links {
            // Format: "relType::Target Title"
            let (rel, target) = match raw.split_once("::") {
                Some((r, t)) => (r.trim().to_string(), t.trim().to_string()),
                None => continue,
            };
            if let Some(target_id) = title_to_id.get(&target) {
                edges.push(KgEdge {
                    from_id: node.id.clone(),
                    to_id: target_id.clone(),
                    relationship_type: rel,
                });
            }
        }
    }

    Ok(KgGraph {
        nodes,
        edges,
        total_nodes_in_collection: total,
        truncated: total > limit,
    })
}

#[command]
pub async fn kg_search(
    project_id: String,
    collections: Vec<String>,
    query: String,
    limit: Option<u32>,
    db: State<'_, Db>,
    cfg: State<'_, LocalConfig>,
) -> Result<Vec<KgNode>, String> {
    for c in &collections {
        require_kg_read(&db, &project_id, c)?;
    }
    let limit = limit.unwrap_or(20).min(100);
    let base = weaviate_url(Some(&cfg));
    let client = weaviate_client()?;

    let mut out = Vec::new();
    for collection in collections {
        // GraphQL safety: validate the class name against a strict
        // whitelist (Weaviate only ever creates classes matching
        // `[A-Za-z][A-Za-z0-9_]*`), and escape backslashes BEFORE
        // quotes in the query string. The original code did only
        // `query.replace('"', "\\\"")` — which means a query like `\"`
        // becomes `\\\"` and Weaviate parses it as literal-backslash +
        // escaped-quote, letting the user's input slip through. Fixed
        // 2026-04-27 alongside the parallel fix in hub/cli_api.rs.
        if !collection
            .chars()
            .next()
            .map(|c| c.is_ascii_alphabetic())
            .unwrap_or(false)
            || !collection
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '_')
        {
            return Err(format!(
                "invalid collection name: {} (must match [A-Za-z][A-Za-z0-9_]*)",
                collection
            ));
        }
        let escaped_query = query.replace('\\', "\\\\").replace('"', "\\\"");
        let q = format!(
            "{{ Get {{ {cls}(nearText: {{concepts: [\"{query}\"]}}, limit: {lim}) \
                {{ title node_type tags content file_path _additional {{ id }} }} }} }}",
            cls = collection,
            query = escaped_query,
            lim = limit,
        );
        let resp = client
            .post(format!("{}/v1/graphql", &base))
            .json(&serde_json::json!({ "query": q }))
            .send()
            .await;
        let body: serde_json::Value = match resp {
            Ok(r) => r.json().await.unwrap_or(serde_json::json!({})),
            Err(_) => continue,
        };
        let empty_vec = vec![];
        let items = body
            .pointer(&format!("/data/Get/{}", collection))
            .and_then(|v| v.as_array())
            .unwrap_or(&empty_vec);
        for item in items {
            let id = item
                .pointer("/_additional/id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let title = item
                .get("title")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            if id.is_empty() || title.is_empty() {
                continue;
            }
            out.push(KgNode {
                id,
                title,
                node_type: item
                    .get("node_type")
                    .and_then(|v| v.as_str())
                    .unwrap_or("concept")
                    .to_string(),
                tags: item
                    .get("tags")
                    .and_then(|v| v.as_array())
                    .map(|a| {
                        a.iter()
                            .filter_map(|t| t.as_str().map(str::to_string))
                            .collect()
                    })
                    .unwrap_or_default(),
                collection: collection.clone(),
                excerpt: item
                    .get("content")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .chars()
                    .take(300)
                    .collect(),
                file_path: item
                    .get("file_path")
                    .and_then(|v| v.as_str())
                    .map(str::to_string),
            });
        }
    }
    Ok(out)
}

// ─── Node detail + promote ──────────────────────────────────────────────

#[derive(Debug, Serialize)]
pub struct KgNodeFull {
    pub id: String,
    pub title: String,
    pub node_type: String,
    pub tags: Vec<String>,
    pub collection: String,
    pub content: String,
    pub file_path: Option<String>,
    pub outgoing_links: Vec<KgEdge>,
}

#[command]
pub async fn kg_get_node(
    project_id: String,
    collection: String,
    node_id: String,
    db: State<'_, Db>,
    cfg: State<'_, LocalConfig>,
) -> Result<KgNodeFull, String> {
    require_kg_read(&db, &project_id, &collection)?;
    let base = weaviate_url(Some(&cfg));
    let client = weaviate_client()?;
    let resp = client
        .get(format!("{}/v1/objects/{}/{}", &base, collection, node_id))
        .send()
        .await
        .map_err(|e| format!("weaviate GET: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!("weaviate returned {}", resp.status().as_u16()));
    }
    let body: serde_json::Value = resp.json().await.map_err(|e| format!("parse: {}", e))?;
    let props = body.get("properties").cloned().unwrap_or(serde_json::json!({}));
    Ok(KgNodeFull {
        id: node_id,
        title: props
            .get("title")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        node_type: props
            .get("node_type")
            .and_then(|v| v.as_str())
            .unwrap_or("concept")
            .to_string(),
        tags: props
            .get("tags")
            .and_then(|v| v.as_array())
            .map(|a| a.iter().filter_map(|t| t.as_str().map(str::to_string)).collect())
            .unwrap_or_default(),
        collection,
        content: props
            .get("content")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        file_path: props
            .get("file_path")
            .and_then(|v| v.as_str())
            .map(str::to_string),
        outgoing_links: vec![], // caller can fetch neighbors via kg_load_graph
    })
}

#[derive(Debug, Deserialize)]
pub struct PromoteReq {
    pub project_id: String,
    pub source_collection: String,
    pub node_id: String,
    pub shared_collection: Option<String>, // defaults to "sharedVCT"
}

#[command]
pub async fn kg_promote_to_shared(
    req: PromoteReq,
    db: State<'_, Db>,
    cfg: State<'_, LocalConfig>,
) -> Result<(), String> {
    require_kg_read(&db, &req.project_id, &req.source_collection)?;
    let shared = req
        .shared_collection
        .unwrap_or_else(|| "sharedVCT".to_string());

    let base = weaviate_url(Some(&cfg));
    let client = weaviate_client()?;
    // 1. Fetch the source node (properties only)
    let src = client
        .get(format!(
            "{}/v1/objects/{}/{}",
            &base, req.source_collection, req.node_id
        ))
        .send()
        .await
        .map_err(|e| format!("fetch source: {}", e))?;
    if !src.status().is_success() {
        return Err(format!("source node not found ({})", src.status().as_u16()));
    }
    let src_body: serde_json::Value = src.json().await.map_err(|e| format!("parse: {}", e))?;
    let mut props = src_body
        .get("properties")
        .cloned()
        .unwrap_or(serde_json::json!({}));
    // Mark provenance so later we can tell where a shared node came from.
    if let Some(obj) = props.as_object_mut() {
        obj.insert(
            "promoted_from_project".into(),
            serde_json::json!(req.project_id),
        );
        obj.insert(
            "promoted_from_collection".into(),
            serde_json::json!(req.source_collection),
        );
        obj.insert(
            "promoted_at".into(),
            serde_json::json!(chrono::Utc::now().to_rfc3339()),
        );
    }

    // 2. Upsert into shared collection.
    let payload = serde_json::json!({
        "class": shared,
        "properties": props,
    });
    let upsert = client
        .post(format!("{}/v1/objects", &base))
        .json(&payload)
        .send()
        .await
        .map_err(|e| format!("upsert: {}", e))?;
    if !upsert.status().is_success() {
        return Err(format!("upsert failed ({})", upsert.status().as_u16()));
    }

    db.audit(
        "kg_promote_to_shared",
        Some(&req.project_id),
        None,
        &serde_json::json!({
            "source_collection": req.source_collection,
            "node_id": req.node_id,
            "shared_collection": shared,
        }),
    )?;
    Ok(())
}

// ─── High-level access mode (collection) ────────────────────────────────
//
// The UI presents four modes: shared / projects / private / none. The DB
// stores per-(project, collection) access levels (read|write|none). This
// wrapper maps the UI mode onto rows for a given owner project + selected
// projects:
//
//   shared:   write row for owner; read row for every other project that exists
//   projects: write row for owner; read row for each id in `project_ids`;
//             none for the rest
//   private:  write row for owner only; none for everyone else
//   none:     v0.2.49 access-matrix Phase 5 (item #14, F-2b) — owner row
//             gets `none` (cuts the owner's own KG MCP read path);
//             peers default to `none`. Used by the GUI's "Remove access"
//             button so revoking the project's own access is distinct
//             from re-granting itself via `private`.
//
// All peer mutations (everything inside the loop iterating `all_projects`)
// are subject to the v0.2.49 Phase 5 item #15 (F-2c) filter: peer rows
// where `is_user_configured()` returns true are NOT touched. This
// preserves user-chosen peer-level downgrades against subsequent
// owner-side mode changes.

#[derive(Debug, Deserialize)]
pub struct CollectionAccessModeReq {
    pub owner_project_id: String,
    pub collection: String,
    pub mode: String, // shared | projects | private | none
    #[serde(default)]
    pub project_ids: Vec<String>,
}

#[command]
pub async fn kg_set_collection_access_mode(
    req: CollectionAccessModeReq,
    db: State<'_, Db>,
) -> Result<(), String> {
    if !matches!(
        req.mode.as_str(),
        "shared" | "projects" | "private" | "none"
    ) {
        return Err(format!("invalid mode: {}", req.mode));
    }
    // v0.2.44 V44-C: structural-row guard.
    //
    // The orchestrator-root project's WRITE access to its own primary collection
    // is structural — revoking it breaks the install (kg-sync etc. would refuse
    // to write to the canonical KG). All OTHER access-matrix mutations remain
    // user-controlled (per-project opt-outs, granting read to other projects,
    // non-structural-collection changes, etc.).
    {
        let owner = db
            .get_project(&req.owner_project_id)
            .map_err(|e| format!("get_project failed: {}", e))?;
        if let Some(owner_row) = owner {
            if owner_row.host == crate::db::models::ProjectHost::OrchestratorRoot {
                let primary_binding = db
                    .list_project_kg_bindings(&req.owner_project_id)
                    .map_err(|e| format!("list_project_kg_bindings failed: {}", e))?
                    .into_iter()
                    .find(|b| b.role == "primary");
                if let Some(pb) = primary_binding {
                    if pb.collection_name == req.collection && req.mode != "shared" {
                        return Err(format!(
                            "Refusing to change access mode for orchestrator-root's \
                             structural row (collection '{}', mode '{}'). The \
                             orchestrator-root project must retain write access to its \
                             primary collection.",
                            req.collection, req.mode,
                        ));
                    }
                }
            }
        }
    }
    // v0.2.49 access-matrix Phase 5 item #14 (F-2b): owner row depends
    // on mode. For modes shared/projects/private the owner retains
    // `write` on its own collection. For mode='none' (the GUI's
    // "Remove access" payload) the owner is explicitly set to `none` —
    // this cuts the project's hooks + MCP read path on its own KG.
    // The orchestrator-root structural-row guard above already rejects
    // mode='none' before we reach this write, so we never attempt to
    // demote the structural row here.
    let owner_level = if req.mode == "none" { "none" } else { "write" };
    db.kg_set_access(&req.owner_project_id, &req.collection, owner_level)?;

    // v0.2.49 access-matrix Phase 5 item #15 (F-2c): the peer-mutation
    // loop only touches rows where `is_user_configured()` reads FALSE
    // (i.e. seed-path rows with `created_at == updated_at`). Rows the
    // user has explicitly downgraded/upgraded through any prior
    // mutation are preserved unconditionally. This honours the
    // "explicit user choice wins over later owner-side mode set"
    // invariant from the v0.2.49 access-matrix overhaul.
    //
    // Soft-fail on the row-read step: a DB error when reading a peer's
    // access row is treated as "row is not user-configured" so the
    // loop still applies the mode-derived default. The conservative
    // alternative (skip on any read error) would silently drop
    // mutations and surface as "mode change didn't take effect."
    let all_projects = db.list_projects()?;
    for p in all_projects.iter() {
        if p.id == req.owner_project_id {
            continue;
        }
        let peer_is_user_configured = db
            .kg_get_access_row(&p.id, &req.collection)
            .ok()
            .flatten()
            .map(|row| row.is_user_configured())
            .unwrap_or(false);
        if peer_is_user_configured {
            continue;
        }
        let level = match req.mode.as_str() {
            "shared" => "read",
            "projects" => {
                if req.project_ids.iter().any(|x| x == &p.id) {
                    "read"
                } else {
                    "none"
                }
            }
            // private | none — every peer that isn't user-configured
            // gets `none`.
            _ => "none",
        };
        db.kg_set_access(&p.id, &req.collection, level)?;
    }
    db.audit(
        "kg_collection_access_mode_set",
        Some(&req.owner_project_id),
        None,
        &serde_json::json!({
            "collection": req.collection, "mode": req.mode,
            "project_count": req.project_ids.len(),
        }),
    )?;
    // P1-D (2026-05-08): refresh env files for every affected project so
    // running sessions pick up the new VCT_KG_ACCESS_LIST without a
    // restart. The mode setter modifies every other project's row, so
    // we refresh all projects (small bounded set in practice; access
    // matrix changes are rare). Soft-fail per project; one bad write
    // does not abort the remaining refreshes.
    if let Ok(all) = db.list_projects() {
        for p in all {
            let _ = crate::commands::projects_v2::refresh_project_env_with_db(&db, &p.id);
        }
    }
    Ok(())
}

// ─── Per-node access (cross-project scoping) ────────────────────────────
//
// Stores allowed project IDs in the Weaviate object's `cross_project_access`
// property. If the property doesn't exist on the schema yet, Weaviate's
// REST PATCH will fail; we catch and report so callers can run a schema
// migration. This is best-effort — the launcher's collection-level access
// gate is still authoritative for reads.

#[derive(Debug, Deserialize)]
pub struct NodeAccessReq {
    pub project_id: String,
    pub collection: String,
    pub node_id: String,
    pub mode: String, // shared | projects | private
    #[serde(default)]
    pub project_ids: Vec<String>,
}

#[command]
pub async fn kg_set_node_access(
    req: NodeAccessReq,
    db: State<'_, Db>,
    cfg: State<'_, LocalConfig>,
) -> Result<(), String> {
    require_kg_read(&db, &req.project_id, &req.collection)?;
    if !matches!(req.mode.as_str(), "shared" | "projects" | "private") {
        return Err(format!("invalid mode: {}", req.mode));
    }
    let allowed = compute_allowed_ids(&req.mode, &req.project_ids);
    let base = weaviate_url(Some(&cfg));
    let client = weaviate_client()?;
    patch_node_access(&client, &base, &req.collection, &req.node_id, &allowed).await?;
    db.audit(
        "kg_node_access_set",
        Some(&req.project_id),
        None,
        &serde_json::json!({
            "collection": req.collection, "node_id": req.node_id,
            "mode": req.mode, "project_count": req.project_ids.len(),
        }),
    )?;
    Ok(())
}

fn compute_allowed_ids(mode: &str, project_ids: &[String]) -> Vec<String> {
    match mode {
        "shared" => vec!["*".to_string()],
        "projects" => project_ids.to_vec(),
        _ => vec![],
    }
}

async fn patch_node_access(
    client: &reqwest::Client,
    base_url: &str,
    collection: &str,
    node_id: &str,
    allowed: &[String],
) -> Result<(), String> {
    let payload = serde_json::json!({
        "class": collection,
        "properties": { "cross_project_access": allowed },
    });
    let resp = client
        .patch(format!("{}/v1/objects/{}/{}", base_url, collection, node_id))
        .json(&payload)
        .send()
        .await
        .map_err(|e| format!("weaviate PATCH {}: {}", node_id, e))?;
    if !resp.status().is_success() {
        let status = resp.status().as_u16();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!(
            "weaviate returned {} for node {}: {}. Schema may be missing 'cross_project_access' property — run kg_ensure_node_access_schema.",
            status,
            node_id,
            body.chars().take(200).collect::<String>()
        ));
    }
    Ok(())
}

// ─── Bulk per-node access ────────────────────────────────────────────────
//
// Apply the same access mode to many nodes in a single collection.
// Uses iteration (not a Weaviate batch endpoint — Weaviate's batch API
// is for object create / delete, not partial-property updates).
// Continues on per-node failure and returns a summary so the UI can
// render "47 / 50 succeeded" instead of stopping at the first error.

#[derive(Debug, Deserialize)]
pub struct NodeAccessBulkReq {
    pub project_id: String,
    pub collection: String,
    pub node_ids: Vec<String>,
    pub mode: String, // shared | projects | private
    #[serde(default)]
    pub project_ids: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct BulkAccessResult {
    pub succeeded: usize,
    pub failed: usize,
    pub failures: Vec<BulkFailure>,
}

#[derive(Debug, Serialize)]
pub struct BulkFailure {
    pub id: String,
    pub error: String,
}

#[command]
pub async fn kg_set_node_access_bulk(
    req: NodeAccessBulkReq,
    db: State<'_, Db>,
    cfg: State<'_, LocalConfig>,
) -> Result<BulkAccessResult, String> {
    require_kg_read(&db, &req.project_id, &req.collection)?;
    if !matches!(req.mode.as_str(), "shared" | "projects" | "private") {
        return Err(format!("invalid mode: {}", req.mode));
    }
    if req.node_ids.is_empty() {
        return Ok(BulkAccessResult { succeeded: 0, failed: 0, failures: vec![] });
    }

    let allowed = compute_allowed_ids(&req.mode, &req.project_ids);
    let base = weaviate_url(Some(&cfg));
    let client = weaviate_client()?;

    let mut succeeded = 0usize;
    let mut failures: Vec<BulkFailure> = Vec::new();
    for node_id in &req.node_ids {
        match patch_node_access(&client, &base, &req.collection, node_id, &allowed).await {
            Ok(()) => succeeded += 1,
            Err(e) => failures.push(BulkFailure { id: node_id.clone(), error: e }),
        }
    }

    // Single audit event for the bulk op, not one per node.
    db.audit(
        "kg_node_access_set_bulk",
        Some(&req.project_id),
        None,
        &serde_json::json!({
            "collection": req.collection,
            "mode": req.mode,
            "project_count": req.project_ids.len(),
            "node_count": req.node_ids.len(),
            "succeeded": succeeded,
            "failed": failures.len(),
        }),
    )?;

    Ok(BulkAccessResult {
        succeeded,
        failed: failures.len(),
        failures,
    })
}

/// Add `cross_project_access: text[]` to a Weaviate collection's schema if
/// it's missing. Idempotent. Returns true if added, false if already present.
#[command]
pub async fn kg_ensure_node_access_schema(
    collection: String,
    cfg: State<'_, LocalConfig>,
) -> Result<bool, String> {
    let base = weaviate_url(Some(&cfg));
    let client = weaviate_client()?;
    let url = format!("{}/v1/schema/{}", &base, collection);
    let resp = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("weaviate GET schema: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!("schema fetch returned {}", resp.status().as_u16()));
    }
    let schema: serde_json::Value = resp.json().await.map_err(|e| format!("parse: {}", e))?;
    let already = schema
        .get("properties")
        .and_then(|p| p.as_array())
        .map(|arr| {
            arr.iter()
                .any(|p| p.get("name").and_then(|n| n.as_str()) == Some("cross_project_access"))
        })
        .unwrap_or(false);
    if already {
        return Ok(false);
    }
    let body = serde_json::json!({
        "name": "cross_project_access",
        "dataType": ["text[]"],
        "description": "Project IDs with cross-project read access ('*' = shared with all)",
    });
    let resp = client
        .post(format!("{}/properties", url))
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("add property: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!(
            "add property returned {}: {}",
            resp.status().as_u16(),
            resp.text().await.unwrap_or_default()
        ));
    }
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::is_codegraph_class;
    use crate::db::models::ProjectHost;
    use crate::db::Db;
    use serde_json::Value as JsonValue;

    #[test]
    fn is_codegraph_class_matches_prefixed_names() {
        // Per-project prefixed names — the v0.2.11+ shape.
        assert!(is_codegraph_class("MyProject_CodeFunction"));
        assert!(is_codegraph_class("MyProject_CodeClass"));
        assert!(is_codegraph_class("MyProject_CodeModule"));
        assert!(is_codegraph_class("MyProject_CodeAPI"));
        assert!(is_codegraph_class("MyProject_CodeInteraction"));
        // Underscored project names still split correctly.
        assert!(is_codegraph_class("Sim_Racing_AI_CodeFunction"));
    }

    #[test]
    fn is_codegraph_class_matches_bare_names() {
        // v0.2.18 Commit 8 (locked 2026-05-19): bare-name classes
        // (CodeFunction etc., legacy pre-multi-project orchestrator
        // schema) must also be recognised so the KG card grid filters
        // them out. The underlying data stays in Weaviate; only the
        // GUI representation is hidden.
        assert!(is_codegraph_class("CodeFunction"));
        assert!(is_codegraph_class("CodeClass"));
        assert!(is_codegraph_class("CodeModule"));
        assert!(is_codegraph_class("CodeAPI"));
        assert!(is_codegraph_class("CodeInteraction"));
    }

    #[test]
    fn is_codegraph_class_rejects_non_code_names() {
        // KG / Development / unrelated shapes must NOT match.
        assert!(!is_codegraph_class("MyProject_KnowledgeGraph"));
        assert!(!is_codegraph_class("MyProject_Development"));
        // Canonical v0.2.23 B1 capital-C casing.
        assert!(!is_codegraph_class("VibeCodedOrchestrator_KnowledgeGraph"));
        // Legacy v0.2.12–v0.2.22 lowercase-c casing.
        assert!(!is_codegraph_class("VibecodedOrchestrator_KnowledgeGraph"));
        assert!(!is_codegraph_class("CodeBase"));
        assert!(!is_codegraph_class("CodeReview"));
        // Empty / odd shapes.
        assert!(!is_codegraph_class(""));
        assert!(!is_codegraph_class("Code"));
        // `_CodeFunction` (leading underscore, empty prefix) IS treated
        // as a code class — `rsplit_once('_')` yields ("", "CodeFunction").
        // The hide-from-KG-dashboard outcome is the same as for bare
        // names, so this is the desired behavior.
        assert!(is_codegraph_class("_CodeFunction"));
    }

    // ── v0.2.44 V44-C: structural-row guard ──────────────────────────────
    //
    // Verify that `kg_set_collection_access_mode`'s guard refuses to revoke
    // the orchestrator-root project's WRITE access to its own primary
    // collection (the "structural row"). All other access-matrix mutations
    // remain user-controlled.
    //
    // The Tauri command itself takes `State<'_, Db>` and `async`, which
    // cannot be constructed in a unit test. These tests exercise the
    // underlying DB state + replicate the guard's conditions so the
    // test fails if either the DB API or the guard's preconditions drift.
    // The actual guard wiring is exercised by the GUI integration tests.

    /// Helper: replicate the guard's structural-row check against a fresh
    /// DB so the test signs the exact contract the real guard depends on.
    /// Returns `Some(error_message)` when the guard would refuse, `None` when
    /// the mutation is allowed.
    fn check_structural_row_guard(
        db: &Db,
        owner_project_id: &str,
        collection: &str,
        mode: &str,
    ) -> Option<String> {
        let owner = db
            .get_project(owner_project_id)
            .expect("get_project must not fail in tests");
        let owner_row = owner.as_ref()?;
        if owner_row.host != ProjectHost::OrchestratorRoot {
            return None;
        }
        let bindings = db
            .list_project_kg_bindings(owner_project_id)
            .expect("list_project_kg_bindings must not fail in tests");
        let primary = bindings.iter().find(|b| b.role == "primary")?;
        if primary.collection_name == collection && mode != "shared" {
            return Some(format!(
                "Refusing to change access mode for orchestrator-root's \
                 structural row (collection '{}', mode '{}'). The \
                 orchestrator-root project must retain write access to its \
                 primary collection.",
                collection, mode,
            ));
        }
        None
    }

    /// Seed an in-memory DB with an orchestrator-root project that has a
    /// `primary` binding at the canonical collection name. Returns the
    /// project id so tests can address it.
    fn seed_root_with_primary(db: &Db, collection: &str) -> String {
        let id = "root-project-uuid".to_string();
        db.insert_project(
            &id,
            "VibeCodedOrchestrator",
            "/tmp/orchestrator-root-folder",
            ProjectHost::OrchestratorRoot,
            "vibecoded-orchestrator",
        )
        .expect("insert root project");
        db.set_project_kg_binding(
            &id,
            "primary",
            collection,
            None,
            None,
            None,
            None,
            &JsonValue::Null,
        )
        .expect("set primary binding");
        id
    }

    /// R9 (v0.2.76): the read gate is DENY-BY-DEFAULT. A collection with a
    /// `read`/`write` access row → Ok; an ABSENT row → Err (no fail-open to a
    /// permissive default like the write endpoint's fall-through).
    #[test]
    fn require_kg_read_denies_on_absent_row() {
        let db = Db::open_in_memory().expect("in-memory db");
        // A plain (non-orchestrator-root) project so the structural-row guard
        // in kg_set_access doesn't fire on a peer collection.
        let id = "peer-project-uuid".to_string();
        db.insert_project(
            &id, "PeerProject", "/tmp/peer-folder", ProjectHost::Base, "peer",
        )
        .expect("insert peer project");
        let collection = "PeerGranted_KnowledgeGraph";

        // ACT: a granted read row → Ok.
        db.kg_set_access(&id, collection, "read").expect("grant read");
        assert!(
            super::require_kg_read(&db, &id, collection).is_ok(),
            "a granted read row must pass the gate"
        );
        // ACT: a write row also satisfies read.
        db.kg_set_access(&id, collection, "write").expect("grant write");
        assert!(super::require_kg_read(&db, &id, collection).is_ok());

        // DENY: no row for a different collection → Err (deny-by-default).
        let ungranted = "Ungranted_KnowledgeGraph";
        let err = super::require_kg_read(&db, &id, ungranted)
            .expect_err("an absent access row must DENY");
        assert!(err.contains("no read access"), "err: {}", err);

        // DENY: an explicit `none` row → Err too.
        db.kg_set_access(&id, ungranted, "none").expect("set none");
        assert!(
            super::require_kg_read(&db, &id, ungranted).is_err(),
            "an explicit 'none' row must DENY"
        );
    }

    /// Revoking write access to the orchestrator-root's primary collection
    /// (mode='private' on the structural row) is refused with a clear,
    /// human-readable error message identifying the collection and mode.
    #[test]
    fn kg_set_collection_access_mode_rejects_root_structural_row_revoke() {
        let db = Db::open_in_memory().expect("in-memory db");
        let collection = "VibeCodedOrchestrator_KnowledgeGraph";
        let owner_id = seed_root_with_primary(&db, collection);

        // mode="private" on the structural row → guard fires.
        let err = check_structural_row_guard(&db, &owner_id, collection, "private")
            .expect("guard must fire on private mode for structural row");
        assert!(
            err.contains("structural row"),
            "error message must name 'structural row', got: {}",
            err,
        );
        assert!(
            err.contains(collection),
            "error message must include the collection name, got: {}",
            err,
        );
        assert!(
            err.contains("private"),
            "error message must include the rejected mode, got: {}",
            err,
        );

        // mode="projects" on the structural row → also refused (any non-shared
        // mode revokes the implicit cross-project read; the guard rejects all
        // non-shared modes equally).
        let err2 =
            check_structural_row_guard(&db, &owner_id, collection, "projects")
                .expect("guard must also fire on projects mode for structural row");
        assert!(
            err2.contains("structural row"),
            "guard must reject 'projects' on the structural row, got: {}",
            err2,
        );
    }

    /// The structural-row guard is narrowly scoped: it only fires for the
    /// exact tuple (host=OrchestratorRoot AND collection==primary AND
    /// mode!="shared"). All other mutations remain user-controlled.
    #[test]
    fn kg_set_collection_access_mode_allows_non_structural_mutations() {
        let db = Db::open_in_memory().expect("in-memory db");
        let collection = "VibeCodedOrchestrator_KnowledgeGraph";
        let owner_id = seed_root_with_primary(&db, collection);

        // (a) mode="shared" on the structural row → ALLOWED. Sharing the
        //     orchestrator's KG is the canonical setup (every other project
        //     reads from it via SHARED_KG_COLLECTION).
        assert!(
            check_structural_row_guard(&db, &owner_id, collection, "shared")
                .is_none(),
            "mode='shared' on the structural row must be allowed (default state)",
        );

        // (b) mode="private" on a DIFFERENT collection → ALLOWED. The guard
        //     only protects the row whose collection_name equals the primary
        //     binding's collection_name.
        let other_collection = "SomeOtherProject_KnowledgeGraph";
        assert!(
            check_structural_row_guard(&db, &owner_id, other_collection, "private")
                .is_none(),
            "mode='private' on a non-structural collection must be allowed",
        );

        // (c) mode="private" on a NON-root project's primary → ALLOWED. The
        //     guard scopes itself to host=OrchestratorRoot owners only.
        let base_id = "base-project-uuid".to_string();
        db.insert_project(
            &base_id,
            "MyApp",
            "/tmp/my-app",
            ProjectHost::Base,
            "my-app",
        )
        .expect("insert base project");
        db.set_project_kg_binding(
            &base_id,
            "primary",
            "MyApp_KnowledgeGraph",
            None,
            None,
            None,
            None,
            &JsonValue::Null,
        )
        .expect("set base primary");
        assert!(
            check_structural_row_guard(
                &db,
                &base_id,
                "MyApp_KnowledgeGraph",
                "private",
            )
            .is_none(),
            "mode='private' on a base project's primary must be allowed",
        );
    }

    // ── v0.2.49 access-matrix Phase 5 items #14 + #15 ──────────────────
    //
    // Items #14 (F-2b) and #15 (F-2c simplified) extend
    // `kg_set_collection_access_mode` with:
    //   - acceptance of `mode='none'` (the GUI's "Remove access" payload
    //     for revoking the project's OWN access to a collection), and
    //   - a peer-row filter that skips rows where `is_user_configured()`
    //     returns true (`created_at != updated_at`).
    //
    // The Tauri command takes `State<'_, Db>` + is `async`, neither of
    // which can be constructed in a unit test. These tests replicate the
    // command's mutation loop against a fresh in-memory DB so the
    // assertions sign the exact contract the production code depends on.

    /// Helper: replicates the v0.2.49 Phase 5 mutation loop body. Each
    /// (peer_id, level) mutation is gated by `is_user_configured` — if
    /// the peer's existing row reads as user-configured the mutation is
    /// skipped. Owner mutation honours item #14's mode='none' rule.
    /// Returns `Ok(())`; per-write errors propagate.
    fn apply_mode_to_db(
        db: &Db,
        owner_project_id: &str,
        collection: &str,
        mode: &str,
        project_ids: &[String],
    ) -> Result<(), String> {
        // Owner row (item #14): mode='none' → 'none'; else 'write'.
        let owner_level = if mode == "none" { "none" } else { "write" };
        db.kg_set_access(owner_project_id, collection, owner_level)?;

        // Peer rows (item #15): skip user-configured rows; otherwise
        // apply mode-derived default.
        for p in db.list_projects()? {
            if p.id == owner_project_id {
                continue;
            }
            let peer_is_user_configured = db
                .kg_get_access_row(&p.id, collection)
                .ok()
                .flatten()
                .map(|row| row.is_user_configured())
                .unwrap_or(false);
            if peer_is_user_configured {
                continue;
            }
            let level = match mode {
                "shared" => "read",
                "projects" => {
                    if project_ids.iter().any(|x| x == &p.id) {
                        "read"
                    } else {
                        "none"
                    }
                }
                _ => "none", // private | none
            };
            db.kg_set_access(&p.id, collection, level)?;
        }
        Ok(())
    }

    /// Seed a non-root project with a primary KG binding so the
    /// structural-row guard doesn't fire (it scopes itself to host =
    /// OrchestratorRoot only).
    fn seed_base_project_with_binding(
        db: &Db,
        project_id: &str,
        name: &str,
        collection: &str,
    ) {
        db.insert_project(
            project_id,
            name,
            &format!("/tmp/{}", name),
            ProjectHost::Base,
            name,
        )
        .expect("insert base project");
        db.set_project_kg_binding(
            project_id,
            "primary",
            collection,
            None,
            None,
            None,
            None,
            &JsonValue::Null,
        )
        .expect("set base primary");
    }

    /// Insert a base project without a binding (acts as a generic peer).
    fn seed_base_project(db: &Db, project_id: &str) {
        db.insert_project(
            project_id,
            project_id,
            &format!("/tmp/{}", project_id),
            ProjectHost::Base,
            project_id,
        )
        .expect("insert base project");
    }

    // ── Item #14 (F-2b): mode='none' fans out 'none' to every peer ──
    //
    // The new payload represents the GUI's "Remove access" action: owner
    // explicitly gets `none` (cuts launcher's hooks + MCP read path),
    // and every peer that the owner-side mutation would touch defaults
    // to `none` too. Peers that are user-configured are NOT touched
    // (item #15 invariant), so this test seeds only seed-path peers.
    #[test]
    fn mode_none_sets_all_peers_to_none_only_for_default_rows() {
        let db = Db::open_in_memory().expect("in-memory db");
        let collection = "OwnerProject_KnowledgeGraph";
        let owner_id = "owner-uuid".to_string();
        seed_base_project_with_binding(&db, &owner_id, "OwnerProject", collection);
        seed_base_project(&db, "peer-a");
        seed_base_project(&db, "peer-b");

        // Seed peer rows via `kg_seed_access` so `is_user_configured`
        // reads FALSE for both. Owner row starts at 'write' (its own).
        db.kg_set_access(&owner_id, collection, "write").unwrap();
        db.kg_seed_access("peer-a", collection, "read").unwrap();
        db.kg_seed_access("peer-b", collection, "read").unwrap();

        // Apply mode='none'.
        apply_mode_to_db(&db, &owner_id, collection, "none", &[]).unwrap();

        // Owner row: 'none' (item #14).
        assert_eq!(
            db.kg_get_access(&owner_id, collection).unwrap(),
            Some("none".to_string()),
            "mode='none' must set owner row to 'none' (item #14)",
        );
        // Peer rows: 'none' (seed-path rows are touched by the mutation loop).
        assert_eq!(
            db.kg_get_access("peer-a", collection).unwrap(),
            Some("none".to_string()),
            "seed-path peer must be set to 'none' under mode='none'",
        );
        assert_eq!(
            db.kg_get_access("peer-b", collection).unwrap(),
            Some("none".to_string()),
            "seed-path peer must be set to 'none' under mode='none'",
        );
    }

    // ── Item #15 (F-2c): peer-revoke skips user-configured rows ──
    //
    // The mutation loop must NOT touch a peer row when
    // `is_user_configured()` returns true. This preserves any explicit
    // user choice (granting peer access, downgrading peer access)
    // against subsequent owner-side mode changes.
    #[test]
    fn peer_revoke_skips_user_configured_rows() {
        let db = Db::open_in_memory().expect("in-memory db");
        let collection = "OwnerProject_KnowledgeGraph";
        let owner_id = "owner-uuid".to_string();
        seed_base_project_with_binding(&db, &owner_id, "OwnerProject", collection);
        seed_base_project(&db, "peer-default");
        seed_base_project(&db, "peer-user-touched");

        // Owner starts at 'write'. One peer is seed-path; the other was
        // user-configured (we simulate that by sleeping enough to bump
        // `updated_at` past `created_at` — but since the simulation must
        // be deterministic, we use `kg_set_access` twice: the first
        // INSERT sets both timestamps equal, the second UPSERT bumps
        // only `updated_at`). The UPSERT's `updated_at` is `now()` —
        // we need it to exceed `created_at`, so a small sleep is
        // unavoidable on systems where `now()` has millisecond
        // resolution. To keep the test deterministic without sleeping,
        // use `kg_seed_access` for the default row (timestamps equal
        // by construction) and `kg_set_access` followed by a forced
        // timestamp bump for the user-touched row.
        db.kg_set_access(&owner_id, collection, "write").unwrap();
        db.kg_seed_access("peer-default", collection, "read").unwrap();
        // User-touched peer: write an explicit row via kg_set_access,
        // then nudge `updated_at` forward by 1ms so the predicate flips
        // to TRUE (since `kg_set_access` writes
        // `created_at == updated_at` on INSERT, we need the row to
        // already exist before the touch).
        db.kg_seed_access("peer-user-touched", collection, "read")
            .unwrap();
        {
            // Bump updated_at by +1ms to flip is_user_configured to true.
            // This mirrors a real "user touched the row" event without
            // requiring the test to sleep.
            let guard = db.lock();
            guard
                .execute(
                    "UPDATE kg_collection_access
                        SET updated_at = updated_at + 1
                      WHERE project_id = ?1 AND collection_name = ?2",
                    rusqlite::params!["peer-user-touched", collection],
                )
                .expect("bump updated_at");
        }

        // Confirm the predicate state before applying the mutation.
        let touched_row = db
            .kg_get_access_row("peer-user-touched", collection)
            .unwrap()
            .expect("touched row must exist");
        assert!(
            touched_row.is_user_configured(),
            "test precondition: touched row must read as user-configured \
             (created_at={}, updated_at={})",
            touched_row.created_at,
            touched_row.updated_at,
        );
        let default_row = db
            .kg_get_access_row("peer-default", collection)
            .unwrap()
            .expect("default row must exist");
        assert!(
            !default_row.is_user_configured(),
            "test precondition: default row must read as NOT user-configured \
             (created_at={}, updated_at={})",
            default_row.created_at,
            default_row.updated_at,
        );

        // Apply mode='private' — this would normally fan 'none' to
        // every peer. Item #15 says the user-touched peer must remain
        // untouched.
        apply_mode_to_db(&db, &owner_id, collection, "private", &[]).unwrap();

        // Default peer: was 'read', now 'none' (loop touched it).
        assert_eq!(
            db.kg_get_access("peer-default", collection).unwrap(),
            Some("none".to_string()),
            "seed-path peer must be re-written by the mutation loop",
        );
        // User-touched peer: must still be 'read' (loop skipped it).
        assert_eq!(
            db.kg_get_access("peer-user-touched", collection).unwrap(),
            Some("read".to_string()),
            "user-configured peer must NOT be touched by the mutation \
             loop (F-2c invariant)",
        );
    }
}

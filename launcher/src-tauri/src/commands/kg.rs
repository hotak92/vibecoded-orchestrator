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

use crate::db::Db;

const DEFAULT_WEAVIATE_URL: &str = "http://localhost:8081";

fn weaviate_url() -> String {
    std::env::var("WEAVIATE_URL").unwrap_or_else(|_| DEFAULT_WEAVIATE_URL.to_string())
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

/// Heuristic: returns true for the codegraph entity classes
/// (`<Prefix>_CodeModule|CodeClass|CodeFunction|CodeAPI|CodeInteraction`).
/// Used by the KG dashboard to filter codegraph classes OUT (they
/// belong on the dedicated /codegraph route), and by the codegraph
/// dashboard to filter them IN.
fn is_codegraph_class(name: &str) -> bool {
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
) -> Result<Vec<KgCollectionAccess>, String> {
    let client = weaviate_client()?;
    // Weaviate exposes a schema listing at /v1/schema
    let schema_resp = client
        .get(format!("{}/v1/schema", weaviate_url()))
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
        let node_count = fetch_class_count(&client, &name).await.unwrap_or(0);
        // Recognize the canonical cross-project KG name shipped by the
        // orchestrator (VibeCodedTools_KnowledgeGraph). Falls back to the
        // historical "shared" substring heuristic + legacy "sharedVCT" name
        // for back-compat with older installs / custom setups.
        let is_shared = name == "VibeCodedTools_KnowledgeGraph"
            || name == "sharedVCT"
            || name.to_lowercase().contains("shared");
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
#[command]
pub async fn codegraph_list_projects(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Vec<CodegraphProjectSummary>, String> {
    let client = weaviate_client()?;
    let schema_resp = client
        .get(format!("{}/v1/schema", weaviate_url()))
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
            let count = fetch_class_count(&client, &name).await.unwrap_or(0);
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
    let mut out: Vec<CodegraphProjectSummary> = Vec::new();
    for (prefix, counts) in by_prefix {
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

async fn fetch_class_count(client: &reqwest::Client, class: &str) -> Result<u32, String> {
    // Weaviate GraphQL: { Aggregate { <Class> { meta { count } } } }
    let body = serde_json::json!({
        "query": format!("{{ Aggregate {{ {class} {{ meta {{ count }} }} }} }}", class = class)
    });
    let resp = client
        .post(format!("{}/v1/graphql", weaviate_url()))
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("graphql: {}", e))?;
    let v: serde_json::Value = resp.json().await.map_err(|e| format!("parse: {}", e))?;
    Ok(v.pointer(&format!("/data/Aggregate/{}/0/meta/count", class))
        .and_then(|n| n.as_u64())
        .unwrap_or(0) as u32)
}

#[command]
pub async fn kg_set_collection_access(
    project_id: String,
    collection: String,
    access: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    if !matches!(access.as_str(), "read" | "write" | "none") {
        return Err(format!("invalid access level: {}", access));
    }
    db.kg_set_access(&project_id, &collection, &access)?;
    db.audit(
        "kg_collection_access_change",
        Some(&project_id),
        None,
        &serde_json::json!({ "collection": collection, "access": access }),
    )?;
    Ok(())
}

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
) -> Result<KgGraph, String> {
    require_kg_read(&db, &project_id, &collection)?;
    let limit = max_nodes.unwrap_or(500).min(2000);

    let client = weaviate_client()?;
    let total = fetch_class_count(&client, &collection).await.unwrap_or(0);

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
        .post(format!("{}/v1/graphql", weaviate_url()))
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
) -> Result<Vec<KgNode>, String> {
    for c in &collections {
        require_kg_read(&db, &project_id, c)?;
    }
    let limit = limit.unwrap_or(20).min(100);
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
            .post(format!("{}/v1/graphql", weaviate_url()))
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
) -> Result<KgNodeFull, String> {
    require_kg_read(&db, &project_id, &collection)?;
    let client = weaviate_client()?;
    let resp = client
        .get(format!(
            "{}/v1/objects/{}/{}",
            weaviate_url(),
            collection,
            node_id
        ))
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
pub async fn kg_promote_to_shared(req: PromoteReq, db: State<'_, Db>) -> Result<(), String> {
    require_kg_read(&db, &req.project_id, &req.source_collection)?;
    let shared = req
        .shared_collection
        .unwrap_or_else(|| "sharedVCT".to_string());

    let client = weaviate_client()?;
    // 1. Fetch the source node (properties only)
    let src = client
        .get(format!(
            "{}/v1/objects/{}/{}",
            weaviate_url(),
            req.source_collection,
            req.node_id
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
        .post(format!("{}/v1/objects", weaviate_url()))
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
// The UI presents three modes: shared / projects / private. The DB stores
// per-(project, collection) access levels (read|write|none). This wrapper
// maps the UI mode onto rows for a given owner project + selected projects:
//
//   shared:   write row for owner; read row for every other project that exists
//   projects: write row for owner; read row for each id in `project_ids`;
//             none for the rest
//   private:  write row for owner only; none for everyone else

#[derive(Debug, Deserialize)]
pub struct CollectionAccessModeReq {
    pub owner_project_id: String,
    pub collection: String,
    pub mode: String, // shared | projects | private
    #[serde(default)]
    pub project_ids: Vec<String>,
}

#[command]
pub async fn kg_set_collection_access_mode(
    req: CollectionAccessModeReq,
    db: State<'_, Db>,
) -> Result<(), String> {
    if !matches!(req.mode.as_str(), "shared" | "projects" | "private") {
        return Err(format!("invalid mode: {}", req.mode));
    }
    // Owner always has write
    db.kg_set_access(&req.owner_project_id, &req.collection, "write")?;

    let all_projects = db.list_projects()?;
    for p in all_projects.iter() {
        if p.id == req.owner_project_id {
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
            _ => "none", // private
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
pub async fn kg_set_node_access(req: NodeAccessReq, db: State<'_, Db>) -> Result<(), String> {
    require_kg_read(&db, &req.project_id, &req.collection)?;
    if !matches!(req.mode.as_str(), "shared" | "projects" | "private") {
        return Err(format!("invalid mode: {}", req.mode));
    }
    let allowed = compute_allowed_ids(&req.mode, &req.project_ids);
    let client = weaviate_client()?;
    patch_node_access(&client, &req.collection, &req.node_id, &allowed).await?;
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
    collection: &str,
    node_id: &str,
    allowed: &[String],
) -> Result<(), String> {
    let payload = serde_json::json!({
        "class": collection,
        "properties": { "cross_project_access": allowed },
    });
    let resp = client
        .patch(format!(
            "{}/v1/objects/{}/{}",
            weaviate_url(),
            collection,
            node_id
        ))
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
) -> Result<BulkAccessResult, String> {
    require_kg_read(&db, &req.project_id, &req.collection)?;
    if !matches!(req.mode.as_str(), "shared" | "projects" | "private") {
        return Err(format!("invalid mode: {}", req.mode));
    }
    if req.node_ids.is_empty() {
        return Ok(BulkAccessResult { succeeded: 0, failed: 0, failures: vec![] });
    }

    let allowed = compute_allowed_ids(&req.mode, &req.project_ids);
    let client = weaviate_client()?;

    let mut succeeded = 0usize;
    let mut failures: Vec<BulkFailure> = Vec::new();
    for node_id in &req.node_ids {
        match patch_node_access(&client, &req.collection, node_id, &allowed).await {
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
pub async fn kg_ensure_node_access_schema(collection: String) -> Result<bool, String> {
    let client = weaviate_client()?;
    let url = format!("{}/v1/schema/{}", weaviate_url(), collection);
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

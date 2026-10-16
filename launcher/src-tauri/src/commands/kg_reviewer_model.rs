//! Preferences -> "KG commit reviewer": the machine-wide model the
//! post-commit KG reviewer runs (v0.2.103, owner ruling 2026-10-15 — ONE
//! global setting, default Sonnet, choices Haiku / Sonnet / Opus).
//!
//! The choices, the default and the `app_state` key come from the ONE rule
//! table `vco_lib/kg_reviewer_config.toml` (`[model]`), embedded here with
//! `include_str!` and parsed by `vco_lib/kg_reviewer_model.py` on the Python
//! side (tier B of A>B>C) — the selector can never offer a value the
//! resolver would ignore. The resolver reads the stored value from
//! launcher.db `app_state` on every reviewer run; `VCO_KG_REVIEW_MODEL`
//! outranks it (shown in the GUI hint).

use serde::Serialize;
use tauri::{command, State};

use crate::db::Db;

/// The rule table, embedded at compile time. Path: 4 levels up from this
/// file (`commands` -> `src` -> `src-tauri` -> `launcher` -> repo root),
/// the same include shape `git_user_editable_merge.rs` uses.
const KG_REVIEWER_CONFIG_TOML: &str = include_str!("../../../../vco_lib/kg_reviewer_config.toml");

/// Table format version this loader understands — MUST MATCH
/// `FORMAT_VERSION` in `vco_lib/kg_reviewer_model.py`.
const KG_REVIEWER_TABLE_FORMAT_VERSION: u32 = 1;

#[derive(Debug, serde::Deserialize)]
struct Table {
    format_version: u32,
    model: ModelTable,
}

#[derive(Debug, Clone, serde::Deserialize)]
struct ModelTable {
    env: String,
    app_state_key: String,
    default: String,
    choices: Vec<String>,
}

/// Parsed `[model]` table. Panics on a malformed table — it is embedded at
/// compile time, so a defect is caught by `table_parses` in every
/// `cargo test` run, never by a user.
static MODEL_TABLE: std::sync::LazyLock<ModelTable> = std::sync::LazyLock::new(|| {
    let table: Table = toml::from_str(KG_REVIEWER_CONFIG_TOML)
        .expect("vco_lib/kg_reviewer_config.toml must parse (compile-time embedded)");
    assert_eq!(
        table.format_version, KG_REVIEWER_TABLE_FORMAT_VERSION,
        "vco_lib/kg_reviewer_config.toml format_version must match this loader"
    );
    table.model
});

/// What the Preferences selector renders.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct KgReviewerModelInfo {
    /// The stored choice, or the default when unset / not a valid choice.
    pub model: String,
    /// True when a valid choice is stored (false = the default applies).
    pub is_set: bool,
    pub default: String,
    pub choices: Vec<String>,
    /// The env override that outranks the stored choice.
    pub env_override_key: String,
}

fn info_from_db(db: &Db) -> Result<KgReviewerModelInfo, String> {
    let t = &*MODEL_TABLE;
    let stored = db
        .app_state_get(&t.app_state_key)?
        .map(|v| v.trim().to_ascii_lowercase())
        .filter(|v| t.choices.iter().any(|c| c == v));
    Ok(KgReviewerModelInfo {
        is_set: stored.is_some(),
        model: stored.unwrap_or_else(|| t.default.clone()),
        default: t.default.clone(),
        choices: t.choices.clone(),
        env_override_key: t.env.clone(),
    })
}

/// Validate + store. A value outside the table's choices is refused (the
/// Python resolver would ignore it, so storing it would show a choice that
/// never takes effect).
fn store_choice(db: &Db, model: &str) -> Result<KgReviewerModelInfo, String> {
    let t = &*MODEL_TABLE;
    let wanted = model.trim().to_ascii_lowercase();
    if !t.choices.iter().any(|c| *c == wanted) {
        return Err(format!(
            "unknown KG reviewer model {:?}; choose one of: {}",
            model,
            t.choices.join(", ")
        ));
    }
    db.app_state_set(&t.app_state_key, &wanted)?;
    info_from_db(db)
}

#[command]
pub async fn get_kg_reviewer_model(db: State<'_, Db>) -> Result<KgReviewerModelInfo, String> {
    info_from_db(&db)
}

#[command]
pub async fn set_kg_reviewer_model(
    model: String,
    db: State<'_, Db>,
) -> Result<KgReviewerModelInfo, String> {
    store_choice(&db, &model)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_parses_with_the_owner_choices_and_sonnet_default() {
        let t = &*MODEL_TABLE;
        assert_eq!(t.choices, vec!["haiku", "sonnet", "opus"]);
        assert_eq!(t.default, "sonnet");
        assert_eq!(t.app_state_key, "kg_reviewer.model");
        assert_eq!(t.env, "VCO_KG_REVIEW_MODEL");
    }

    #[test]
    fn unset_reads_as_the_default() {
        let db = Db::open_in_memory().expect("in-memory db");
        let info = info_from_db(&db).unwrap();
        assert_eq!(info.model, "sonnet");
        assert!(!info.is_set);
    }

    #[test]
    fn a_valid_choice_is_stored_where_the_python_resolver_reads_it() {
        let db = Db::open_in_memory().expect("in-memory db");
        let info = store_choice(&db, "Opus").unwrap();
        assert_eq!((info.model.as_str(), info.is_set), ("opus", true));
        assert_eq!(
            db.app_state_get("kg_reviewer.model").unwrap().as_deref(),
            Some("opus")
        );
    }

    #[test]
    fn an_invalid_choice_is_refused_and_nothing_is_written() {
        let db = Db::open_in_memory().expect("in-memory db");
        store_choice(&db, "haiku").unwrap();
        let err = store_choice(&db, "glm-5.3-flash").unwrap_err();
        assert!(err.contains("haiku, sonnet, opus"), "{err}");
        assert_eq!(
            db.app_state_get("kg_reviewer.model").unwrap().as_deref(),
            Some("haiku")
        );
    }

    #[test]
    fn a_hand_edited_unknown_value_reads_as_the_default() {
        let db = Db::open_in_memory().expect("in-memory db");
        db.app_state_set("kg_reviewer.model", "flash").unwrap();
        let info = info_from_db(&db).unwrap();
        assert_eq!((info.model.as_str(), info.is_set), ("sonnet", false));
    }
}

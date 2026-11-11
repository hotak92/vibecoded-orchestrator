// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! How long Ollama keeps a model loaded after VCO last used it (v0.2.106,
//! owner ruling 2026-11-11: "the hub manages unloading").
//!
//! The ONE owner of a model's lifetime is `vct-hub` (`vct_hub::ollama_lifetime`):
//! it watches Ollama's `/api/ps` and unloads a model once no VCO request has
//! used it for the idle time the user picked in Preferences. This module is
//! what the hub and the launcher share:
//!
//! * the numbers in `vco_lib/ollama_model_lifetime.toml` — the keep_alive the
//!   Python callers send (the fail-safe that expires on its own when the hub
//!   is down), the default idle time and the largest one the hub accepts;
//! * the `app_state` key the preference lives in;
//! * the ONE duration parser (Preferences validates with it, the hub resolves
//!   with it), so a value the GUI accepts is a value the hub honours.

use std::sync::OnceLock;

use crate::db::Db;

/// `app_state` key holding the idle time (a duration string, e.g. `"10m"`).
/// Absent = [`LifetimeTable::default_idle_secs`].
pub const IDLE_APP_STATE_KEY: &str = "ollama.idle_unload";

/// The env var a user sets to choose the keep_alive VCO's callers send
/// themselves (`vco_lib.ollama_embed_body._keep_alive`). Models a caller
/// pinned that way are left alone by the hub.
pub const CALLER_OVERRIDE_ENV: &str = "VCO_OLLAMA_KEEP_ALIVE";

/// The shared table, compiled in. MUST be the file
/// `vco_lib/ollama_embed_body.py` reads (`_LIFETIME_TABLE`).
const LIFETIME_TOML: &str = include_str!("../../../../../vco_lib/ollama_model_lifetime.toml");

/// The parsed numbers of `vco_lib/ollama_model_lifetime.toml`, in seconds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LifetimeTable {
    /// What every VCO caller sends as `keep_alive` when the user has not set
    /// [`CALLER_OVERRIDE_ENV`].
    pub caller_keep_alive_secs: u64,
    /// The hub's idle time when Preferences holds no value.
    pub default_idle_secs: u64,
    /// The largest idle time the hub accepts (below the callers' keep_alive,
    /// so Ollama never unloads first while the hub runs).
    pub max_idle_secs: u64,
}

fn parse_table(text: &str) -> Result<LifetimeTable, String> {
    let doc: toml::Table = text.parse().map_err(|e| format!("ollama_model_lifetime.toml: {e}"))?;
    let get = |key: &str| -> Result<u64, String> {
        let raw = doc
            .get(key)
            .and_then(|v| v.as_str())
            .ok_or_else(|| format!("ollama_model_lifetime.toml: `{key}` missing or not a string"))?;
        parse_duration_secs(raw).map_err(|e| format!("ollama_model_lifetime.toml: `{key}`: {e}"))
    };
    let table = LifetimeTable {
        caller_keep_alive_secs: get("caller_keep_alive")?,
        default_idle_secs: get("default_idle")?,
        max_idle_secs: get("max_idle")?,
    };
    if table.default_idle_secs > table.max_idle_secs {
        return Err("ollama_model_lifetime.toml: default_idle exceeds max_idle".into());
    }
    if table.max_idle_secs >= table.caller_keep_alive_secs {
        return Err(
            "ollama_model_lifetime.toml: max_idle must be below caller_keep_alive, or Ollama \
             unloads before the hub does"
                .into(),
        );
    }
    Ok(table)
}

/// The compiled-in table. The file ships in the same checkout and is pinned
/// by tests, so a parse failure is a build defect, not a runtime condition.
pub fn table() -> LifetimeTable {
    static TABLE: OnceLock<LifetimeTable> = OnceLock::new();
    *TABLE.get_or_init(|| parse_table(LIFETIME_TOML).expect("vco_lib/ollama_model_lifetime.toml"))
}

/// Parse an idle/keep-alive duration: `"0"`, a whole number of seconds
/// (`"90"`), or whole `h`/`m`/`s` units in Ollama's notation (`"10m"`,
/// `"1h"`, `"1m30s"`). Negative values, fractions and other units are
/// refused — the hub has no "never" (a model must always unload eventually;
/// keeping one loaded for good is what `VCO_OLLAMA_KEEP_ALIVE=-1` is for).
pub fn parse_duration_secs(raw: &str) -> Result<u64, String> {
    let s = raw.trim();
    if s.is_empty() {
        return Err("empty duration".into());
    }
    if s.bytes().all(|b| b.is_ascii_digit()) {
        return s.parse::<u64>().map_err(|e| format!("'{raw}': {e}"));
    }
    let mut total: u64 = 0;
    let mut digits = String::new();
    let mut saw_unit = false;
    for ch in s.chars() {
        if ch.is_ascii_digit() {
            digits.push(ch);
            continue;
        }
        let mult = match ch {
            'h' => 3600,
            'm' => 60,
            's' => 1,
            _ => {
                return Err(format!(
                    "'{raw}' is not a duration (use whole hours/minutes/seconds, e.g. 10m, 90s, 1m30s)"
                ))
            }
        };
        if digits.is_empty() {
            return Err(format!("'{raw}': a unit without a number"));
        }
        let n: u64 = digits.parse().map_err(|e| format!("'{raw}': {e}"))?;
        total = total
            .checked_add(n.checked_mul(mult).ok_or_else(|| format!("'{raw}' is too long"))?)
            .ok_or_else(|| format!("'{raw}' is too long"))?;
        digits.clear();
        saw_unit = true;
    }
    if !digits.is_empty() || !saw_unit {
        return Err(format!("'{raw}': a number without a unit (h, m or s)"));
    }
    Ok(total)
}

/// `secs` in the shortest Ollama-style form: `0`, `45s`, `10m`, `1m30s`, `1h`.
pub fn format_secs(secs: u64) -> String {
    if secs == 0 {
        return "0".into();
    }
    let (h, m, s) = (secs / 3600, (secs % 3600) / 60, secs % 60);
    let mut out = String::new();
    if h > 0 {
        out.push_str(&format!("{h}h"));
    }
    if m > 0 {
        out.push_str(&format!("{m}m"));
    }
    if s > 0 {
        out.push_str(&format!("{s}s"));
    }
    out
}

/// Validate a Preferences value: parseable and within `0..=max_idle`.
/// Returns the normalised string to store and its seconds.
pub fn validate_idle(raw: &str) -> Result<(String, u64), String> {
    let secs = parse_duration_secs(raw)?;
    let max = table().max_idle_secs;
    if secs > max {
        return Err(format!(
            "'{}' is longer than the {} the hub accepts. To keep a model loaded longer, set \
             {CALLER_OVERRIDE_ENV} (e.g. -1 to never unload) — the hub leaves models pinned \
             that way alone.",
            raw.trim(),
            format_secs(max)
        ));
    }
    Ok((format_secs(secs), secs))
}

/// The idle time in force, and where it came from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IdleSetting {
    pub secs: u64,
    /// The stored Preferences value, verbatim (`None` = no row).
    pub stored: Option<String>,
    /// `true` when a stored value could not be honoured (unparseable or out
    /// of range) and the default applies instead.
    pub stored_invalid: bool,
}

/// Resolve the idle time from a stored value (`None` = no row).
pub fn resolve_idle(stored: Option<String>) -> IdleSetting {
    let default = table().default_idle_secs;
    match stored.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        None => IdleSetting { secs: default, stored, stored_invalid: false },
        Some(v) => match validate_idle(v) {
            Ok((_, secs)) => IdleSetting { secs, stored, stored_invalid: false },
            Err(_) => IdleSetting { secs: default, stored, stored_invalid: true },
        },
    }
}

/// [`resolve_idle`] reading launcher.db. Poison-tolerant (the hub's task
/// calls it on every tick).
pub fn idle_from_db(db: &Db) -> IdleSetting {
    resolve_idle(db.app_state_get_nonpanicking(IDLE_APP_STATE_KEY))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_shipped_table_parses_and_is_consistent() {
        let t = table();
        assert_eq!(t.caller_keep_alive_secs, 45 * 60);
        assert_eq!(t.default_idle_secs, 10 * 60, "unset must equal the pre-v0.2.106 10m");
        assert_eq!(t.max_idle_secs, 30 * 60);
        assert!(t.max_idle_secs < t.caller_keep_alive_secs);
    }

    #[test]
    fn a_table_whose_max_reaches_the_caller_keep_alive_is_refused() {
        let bad = "caller_keep_alive = \"30m\"\ndefault_idle = \"10m\"\nmax_idle = \"30m\"\n";
        assert!(parse_table(bad).is_err());
    }

    #[test]
    fn durations_parse() {
        assert_eq!(parse_duration_secs("0").unwrap(), 0);
        assert_eq!(parse_duration_secs("90").unwrap(), 90);
        assert_eq!(parse_duration_secs("10m").unwrap(), 600);
        assert_eq!(parse_duration_secs(" 1m30s ").unwrap(), 90);
        assert_eq!(parse_duration_secs("1h").unwrap(), 3600);
        for bad in ["", "-1", "1.5m", "10x", "m", "10m5", "5ms", "abc"] {
            assert!(parse_duration_secs(bad).is_err(), "{bad:?} must be refused");
        }
    }

    #[test]
    fn format_round_trips() {
        for secs in [0, 45, 90, 600, 1800, 3600, 3725] {
            assert_eq!(parse_duration_secs(&format_secs(secs)).unwrap(), secs);
        }
        assert_eq!(format_secs(600), "10m");
        assert_eq!(format_secs(90), "1m30s");
    }

    #[test]
    fn validate_caps_at_max_idle_and_normalises() {
        assert_eq!(validate_idle("600").unwrap(), ("10m".to_string(), 600));
        assert_eq!(validate_idle("30m").unwrap().1, 1800);
        let err = validate_idle("31m").unwrap_err();
        assert!(err.contains(CALLER_OVERRIDE_ENV), "{err}");
        assert!(validate_idle("-1").is_err());
    }

    #[test]
    fn resolve_idle_defaults_and_flags_invalid_rows() {
        assert_eq!(resolve_idle(None).secs, 600);
        assert!(!resolve_idle(None).stored_invalid);
        assert_eq!(resolve_idle(Some("2m".into())).secs, 120);
        let bad = resolve_idle(Some("forever".into()));
        assert_eq!(bad.secs, 600);
        assert!(bad.stored_invalid);
        assert_eq!(resolve_idle(Some("0".into())).secs, 0);
    }

    #[test]
    fn idle_from_db_round_trip() {
        let db = Db::open_in_memory().expect("db");
        assert_eq!(idle_from_db(&db).secs, 600);
        db.app_state_set(IDLE_APP_STATE_KEY, "5m").unwrap();
        assert_eq!(idle_from_db(&db).secs, 300);
        assert!(db.app_state_delete(IDLE_APP_STATE_KEY).unwrap());
        assert_eq!(idle_from_db(&db).secs, 600);
    }
}

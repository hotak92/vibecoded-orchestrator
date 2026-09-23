// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! install.py's relaunch-record env keys — read from the table Python reads.
//!
//! `vco_lib/install_relaunch_env.toml` lists the keys install.py sets on the
//! run it relaunches itself as (loop guard, launching interpreter, waiting
//! parent's pid, argv-token). They describe one hop. A launcher that inherited
//! them — relaunched by a vct-updater that an install.py started — must not
//! hand them to the NEXT install.py, which would skip its venv relaunch and
//! watch a stale pid. [`super::install_py_command`] removes them from every
//! install.py the launcher spawns.
//!
//! Tier (B) of the A>B>C rule: one committed table, embedded at compile time
//! (`include_str!`), parsed by both languages. Tier (A) — asking Python — was
//! rejected: the launcher spawns install.py to REPAIR installs whose Python
//! side may be broken, so building that command must not run Python first.

use std::sync::LazyLock;

use serde::Deserialize;

/// Path: 5 levels up from this file (installer → commands → src → src-tauri →
/// launcher → repo root), then into `vco_lib/`, where the table ships in the
/// Python wheel.
const INSTALL_RELAUNCH_ENV_TOML: &str =
    include_str!("../../../../../vco_lib/install_relaunch_env.toml");

const SUPPORTED_FORMAT_VERSION: u32 = 1;

#[derive(Deserialize)]
struct RawTable {
    format_version: u32,
    keys: Vec<String>,
}

fn parse(text: &str) -> Result<Vec<String>, String> {
    let raw: RawTable = toml::from_str(text).map_err(|e| e.to_string())?;
    if raw.format_version != SUPPORTED_FORMAT_VERSION {
        return Err(format!(
            "format_version {} (this launcher reads {})",
            raw.format_version, SUPPORTED_FORMAT_VERSION
        ));
    }
    Ok(raw.keys)
}

/// The keys to remove. A table that does not parse yields NO keys and an
/// error log rather than a panic on the spawn path: install.py's own argv-token
/// check still refuses an inherited record, and the unit tests below keep a
/// malformed table from shipping.
pub(crate) static RELAUNCH_ENV_KEYS: LazyLock<Vec<String>> =
    LazyLock::new(|| match parse(INSTALL_RELAUNCH_ENV_TOML) {
        Ok(keys) => keys,
        Err(e) => {
            tracing::error!(
                "[vct] vco_lib/install_relaunch_env.toml does not parse ({}); \
                 install.py spawns keep any inherited relaunch keys",
                e
            );
            Vec::new()
        }
    });

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_embedded_table_parses_and_names_the_whole_record() {
        let keys = parse(INSTALL_RELAUNCH_ENV_TOML).expect("the shipped table parses");
        for key in [
            "VCT_INSTALL_RELAUNCHED",
            "VCT_INSTALL_BASE_PYTHON",
            "VCT_INSTALL_BASE_PYTHON_VERSION",
            "VCT_INSTALL_PARENT_WAITS",
            "VCT_INSTALL_RELAUNCH_TOKEN",
        ] {
            assert!(keys.iter().any(|k| k == key), "{key} missing from {keys:?}");
        }
        assert_eq!(*RELAUNCH_ENV_KEYS, keys);
    }

    #[test]
    fn an_unknown_format_version_is_refused() {
        assert!(parse("format_version = 2\nkeys = []\n").is_err());
        assert!(parse("keys = [").is_err());
    }
}

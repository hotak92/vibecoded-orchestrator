// SPDX-License-Identifier: AGPL-3.0-or-later
//! Shared HTTP error envelope for every vct-hub API route
//! (v0.2.54 Track J).
//!
//! Before this module, `error_response` existed as FOUR byte-identical
//! copies in `modules_api`, `lifecycle_api`, `secrets_api`, and
//! `config_api` — each carrying its own "match the envelope shape
//! `modules_api::error_response` uses" comment. The envelope is a wire
//! contract consumed by install.py and the launcher GUI; keeping the
//! shape consistent across routes by copy-paste is exactly how drift
//! happens. One implementation, four (and future N) importers.
//!
//! Wire shape (unchanged from the copies):
//!
//! ```text
//! { "error": { "code": "<machine_code>", "message": "<human text>" } }
//! ```
//!
//! Machine-parseable `code` lets consumers branch without sniffing the
//! HTTP status or the human message (e.g. `agent_secrets.py`
//! distinguishes `project_not_found` from `key_not_active` on 404).

use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::Json;

/// Build the hub's standard JSON error envelope.
pub fn error_response(
    status: StatusCode,
    code: &str,
    message: impl Into<String>,
) -> axum::response::Response {
    (
        status,
        Json(serde_json::json!({
            "error": {
                "code": code,
                "message": message.into(),
            }
        })),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn envelope_shape_and_status_round_trip() {
        let resp = error_response(StatusCode::NOT_FOUND, "key_not_active", "k paused");
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
        let bytes = axum::body::to_bytes(resp.into_body(), 4096).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["error"]["code"], "key_not_active");
        assert_eq!(v["error"]["message"], "k paused");
    }
}

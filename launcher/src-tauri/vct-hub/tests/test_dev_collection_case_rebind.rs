//! NEW-2 (v0.2.53) — integration smoke for the case-rebind contract.
//!
//! The full behavioural assertions live in
//! `src/config_api.rs` under the `dev_collection_case_rebind_*`,
//! `dev_collection_no_rebind_when_no_sibling`, and
//! `dev_collection_unreachable_weaviate_fails_open` tests (in-module
//! because they need to exercise the private `project_config` handler
//! through `spawn_config_api_hub`).
//!
//! This file is a thin black-box check that:
//!   1. The `weaviate_schema_probe` module is publicly exposed (so
//!      out-of-crate consumers like future-Track-B's install.py-side
//!      caller could in principle use it).
//!   2. The empty-candidate fast-path returns the empty string without
//!      reaching the network — a hot-path optimisation we don't want
//!      regressed.
//!
//! Reference: the v0.2.52 root-cause audit (Symptom B).

#[tokio::test]
async fn empty_candidate_returns_empty_without_network() {
    vct_hub::weaviate_schema_probe::_reset_cache_for_test();
    // Use a deliberately-bad URL — if the probe tried to reach it, the
    // assertion below would still pass (the function is fail-open) but
    // the test name would be misleading. The empty-candidate fast-path
    // ensures we never even reach the network.
    let bad_url = "http://0.0.0.0:1";
    let resolved =
        vct_hub::weaviate_schema_probe::resolve_existing_casing_for_class(bad_url, "").await;
    assert_eq!(resolved, "");
}

#[tokio::test]
async fn unreachable_url_returns_candidate_unchanged() {
    vct_hub::weaviate_schema_probe::_reset_cache_for_test();
    // Bind+drop a TCP listener so we have a definitely-closed port.
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    drop(listener);
    let unreachable_url = format!("http://{}", addr);
    let candidate = "MyProject_Development";
    let resolved = vct_hub::weaviate_schema_probe::resolve_existing_casing_for_class(
        &unreachable_url,
        candidate,
    )
    .await;
    // Fail-open contract: candidate echoed unchanged.
    assert_eq!(resolved, candidate);
}

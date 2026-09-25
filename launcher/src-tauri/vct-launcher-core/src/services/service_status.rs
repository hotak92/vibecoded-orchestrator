//! The services snapshot — ONE wire shape for the launcher's
//! `services_status` Tauri command and the hub's `GET /services/status`.
//!
//! Until v0.2.97 the hub kept a hand-written MIRROR of the launcher's
//! structs ("fields kept in the same order so a `git diff` makes drift
//! visible"), including a mirror of the `services.toml` adoption enum. Both
//! now build their snapshot from the same launcher.db `service_endpoints`
//! rows through the functions below, so there is nothing to keep in step.
//!
//! What a row says, per service:
//!   * `mode` — `vco_managed` (VCO's compose owns the container; also the
//!     answer with no row), `adopted_container` (someone else's container,
//!     started/stopped by name only), `adopted_external` (a URL; no
//!     lifecycle);
//!   * `endpoint` — the row itself (`None` = no row yet: the compiled
//!     default is in use);
//!   * `url` / `port` — where the health probe goes: the row's host and
//!     port, never a hard-coded `localhost:<default>`.

use serde::{Deserialize, Serialize};

use crate::db::service_endpoints::{EndpointMode, ServiceEndpointRow};
use crate::services::service_endpoints::{
    awaits_choice, hand_to_vco_offered, lifecycle_container, mode_of, render_port, render_url, CoreService,
};

/// One service in the snapshot.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ServiceRuntimeState {
    /// `"weaviate"` | `"ollama"` | `"code_embed"` (| `"model_gateway"` on
    /// the hub, a process rather than an endpoint row).
    pub name: String,
    /// True iff the health URL answered 2xx/3xx.
    pub running: bool,
    /// Host port the service is reached on (the row's, else the default).
    pub port: u16,
    /// The health URL probed.
    pub url: String,
    /// True when VCO does not own the container (`mode != vco_managed`).
    pub externally_managed: bool,
    /// The row's mode (`vco_managed` with no row). `None` only for a row
    /// that is not a service endpoint at all (the hub's `model_gateway`).
    pub mode: Option<EndpointMode>,
    /// The `service_endpoints` row, or `None` when there is none yet.
    #[serde(default)]
    pub endpoint: Option<ServiceEndpointRow>,
    /// The container a Start/Stop/Restart acts on: VCO's own, or the pinned
    /// adopted container; `None` for an adopted URL.
    #[serde(default)]
    pub container_name: Option<String>,
    /// PR-15 G2 (v0.2.11): the container exists but its main PID is dead
    /// (state-DB desync). Only ever set for a `vco_managed` service — VCO
    /// never recovers an adopted container by removing it.
    #[serde(default)]
    pub zombie: bool,
    /// Where this service runs is waiting for the user's choice
    /// ([`crate::services::service_endpoints::awaits_choice`]).
    #[serde(default)]
    pub pending_choice: bool,
    /// The Services page offers "Let VCO manage it" (the opt-in hand-over,
    /// owner ruling Q2) — computed by
    /// [`crate::services::service_endpoints::hand_to_vco_offered`], the rule
    /// `hand-to-vco` enforces (plan §12). The page reads this field; it holds
    /// no copy of the rule.
    #[serde(default)]
    pub hand_to_vco_offered: bool,
}

/// The whole snapshot.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ServicesRuntimeSnapshot {
    pub services: Vec<ServiceRuntimeState>,
    /// Detected container runtime (`"podman"` | `"docker"` | `null`).
    pub runtime: Option<String>,
    /// macOS/Windows Podman with no running machine.
    pub needs_podman_machine_start: bool,
    /// True when at least one core service has no `service_endpoints` row —
    /// the install/update that writes them has not completed on this
    /// machine. The Services page offers "Detect services" for it.
    pub endpoints_missing: bool,
    /// True iff this snapshot was assembled without a live probe (the hub's
    /// skeleton). The launcher always probes and sends `false`.
    #[serde(default)]
    pub degraded: bool,
}

/// The health path each core service answers. Weaviate uses `/v1/meta`,
/// not `/v1/.well-known/ready`, which can 503 during recovery while the
/// instance is fully usable (observed 2026-05-06).
pub fn health_path(service: CoreService) -> &'static str {
    match service {
        CoreService::Weaviate => "/v1/meta",
        CoreService::Ollama => "/api/tags",
        CoreService::CodeEmbed => "/health",
    }
}

/// The health URL for `service` at the endpoint `row` states (the compiled
/// default with no row).
pub fn health_url(service: CoreService, row: Option<&ServiceEndpointRow>) -> String {
    format!("{}{}", render_url(service, row), health_path(service))
}

/// `service`'s entry before any probe: not running, not a zombie, every
/// endpoint fact from `row`. Pure.
pub fn service_state(service: CoreService, row: Option<ServiceEndpointRow>) -> ServiceRuntimeState {
    let mode = mode_of(row.as_ref());
    ServiceRuntimeState {
        name: service.name().to_string(),
        running: false,
        port: render_port(service, row.as_ref()),
        url: health_url(service, row.as_ref()),
        externally_managed: mode != EndpointMode::VcoManaged,
        mode: Some(mode),
        container_name: lifecycle_container(service, row.as_ref()),
        pending_choice: awaits_choice(service, row.as_ref()),
        hand_to_vco_offered: hand_to_vco_offered(service, row.as_ref()),
        endpoint: row,
        zombie: false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Plan §12: the Services page's "Let VCO manage it" is shown from this
    /// field, computed by the verb's own rule — an adopted container under
    /// VCO's compose name is offered; a foreign name, an adopted URL, VCO's
    /// own row and no row are not. Red if `service_state` stops computing it.
    #[test]
    fn the_hand_over_offer_is_computed_from_the_row() {
        let mut ours = ServiceEndpointRow::new("weaviate", EndpointMode::AdoptedContainer, "localhost", 8081);
        ours.container_name = Some("vco_weaviate".into());
        assert!(service_state(CoreService::Weaviate, Some(ours.clone())).hand_to_vco_offered);
        let wire = serde_json::to_value(service_state(CoreService::Weaviate, Some(ours))).unwrap();
        assert_eq!(wire["hand_to_vco_offered"], true, "the page reads this key");

        let mut foreign = ServiceEndpointRow::new("weaviate", EndpointMode::AdoptedContainer, "localhost", 8081);
        foreign.container_name = Some("their_weaviate".into());
        assert!(!service_state(CoreService::Weaviate, Some(foreign)).hand_to_vco_offered);
        let url = ServiceEndpointRow::new("ollama", EndpointMode::AdoptedExternal, "gpu.lan", 11434);
        assert!(!service_state(CoreService::Ollama, Some(url)).hand_to_vco_offered);
        assert!(!service_state(CoreService::Weaviate, None).hand_to_vco_offered);
    }

    #[test]
    fn a_state_carries_the_rows_host_port_and_mode() {
        let mut row = ServiceEndpointRow::new("ollama", EndpointMode::AdoptedExternal, "gpu.lan", 11434);
        row.confirmed_by_user = true;
        let s = service_state(CoreService::Ollama, Some(row.clone()));
        assert_eq!(s.url, "http://gpu.lan:11434/api/tags");
        assert_eq!(s.port, 11434);
        assert_eq!(s.mode, Some(EndpointMode::AdoptedExternal));
        assert!(s.externally_managed);
        assert_eq!(s.container_name, None, "an adopted URL has no container");
        assert_eq!(s.endpoint, Some(row));
    }

    #[test]
    fn no_row_is_vcos_own_service_on_the_default() {
        let s = service_state(CoreService::Weaviate, None);
        assert_eq!(s.url, "http://localhost:8081/v1/meta");
        assert_eq!(s.mode, Some(EndpointMode::VcoManaged));
        assert!(!s.externally_managed);
        assert_eq!(s.container_name.as_deref(), Some("vco_weaviate"));
        assert_eq!(s.endpoint, None);
        assert!(s.pending_choice, "no row yet is an open question");
    }

    /// The Weaviate confirmation-pending row (vco_managed + disabled) is a
    /// question; a disabled code-embed (a CPU host) and a confirmed
    /// adoption are not.
    #[test]
    fn pending_choice_follows_the_confirmation_pending_row() {
        let mut w = ServiceEndpointRow::new("weaviate", EndpointMode::VcoManaged, "localhost", 8081);
        w.grpc_port = Some(50052);
        w.enabled = false;
        assert!(service_state(CoreService::Weaviate, Some(w.clone())).pending_choice);
        w.enabled = true;
        assert!(!service_state(CoreService::Weaviate, Some(w)).pending_choice);
        let mut c = ServiceEndpointRow::new("code_embed", EndpointMode::VcoManaged, "localhost", 11440);
        c.enabled = false;
        assert!(!service_state(CoreService::CodeEmbed, Some(c)).pending_choice);
        let o = ServiceEndpointRow::new("ollama", EndpointMode::AdoptedExternal, "localhost", 11434);
        assert!(!service_state(CoreService::Ollama, Some(o)).pending_choice);
    }

    #[test]
    fn the_wire_shape_round_trips() {
        let mut row = ServiceEndpointRow::new("weaviate", EndpointMode::AdoptedContainer, "localhost", 8081);
        row.grpc_port = Some(50052);
        row.container_name = Some("their_weaviate".into());
        let snap = ServicesRuntimeSnapshot {
            services: vec![service_state(CoreService::Weaviate, Some(row))],
            runtime: Some("podman".into()),
            needs_podman_machine_start: false,
            endpoints_missing: true,
            degraded: false,
        };
        let json = serde_json::to_value(&snap).unwrap();
        assert_eq!(json["services"][0]["mode"], "adopted_container");
        assert_eq!(json["services"][0]["container_name"], "their_weaviate");
        let back: ServicesRuntimeSnapshot = serde_json::from_value(json).unwrap();
        assert_eq!(back, snap);
    }
}

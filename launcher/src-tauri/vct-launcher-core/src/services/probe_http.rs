// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! The redirect rule of every HTTP client the launcher and the hub build for
//! a service endpoint (Weaviate / Ollama / code-embed) — v0.2.97, R7a F7 —
//! and the "did it answer?" rule. Callers build clients through
//! [`super::loopback_http`], which adds the no-proxy rule for a loopback host.
//!
//! A probe asks "does THIS endpoint answer as the service?", so it must never
//! be answered by another one. reqwest follows up to 10 redirects by default:
//! an unrelated app on the probed port (a dev proxy, a login page) answering
//! `302 https://elsewhere/…` would make the probe leave the endpoint — off
//! loopback, through whatever proxy is configured — and read that answer as
//! the service's. A probe client built here does not follow redirects, and a
//! probe counts only a 2xx as "answers" ([`answered`]): a 3xx is an answer,
//! but not the service's.
//!
//! MUST MATCH the Python probes' rule (`vco_lib/service_probe_http.py`,
//! used by the detector, the lifecycle health checks and the adoption).

/// A `reqwest` client builder that follows NO redirect. Callers build their
/// clients through [`super::loopback_http`] (which adds the no-proxy rule for
/// a loopback host); this is its redirect half.
pub fn probe_client_builder() -> reqwest::ClientBuilder {
    reqwest::Client::builder().redirect(reqwest::redirect::Policy::none())
}

/// Did a probe's response say the service answered? Only a 2xx does.
pub fn answered(status: reqwest::StatusCode) -> bool {
    status.is_success()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    /// A loopback responder: `/start` answers `302 → /landed`, `/landed`
    /// answers 200 and counts its hits. Test-owned, bound to an ephemeral
    /// port on 127.0.0.1 — never a real service.
    fn redirecting_server() -> (String, Arc<AtomicUsize>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let landed = Arc::new(AtomicUsize::new(0));
        let hits = Arc::clone(&landed);
        let target = format!("{base}/landed");
        std::thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let mut stream = stream;
                let mut buf = [0u8; 2048];
                let n = stream.read(&mut buf).unwrap_or(0);
                let head = String::from_utf8_lossy(&buf[..n]);
                let resp = if head.starts_with("GET /landed") {
                    hits.fetch_add(1, Ordering::SeqCst);
                    "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok".to_string()
                } else {
                    format!(
                        "HTTP/1.1 302 Found\r\nLocation: {target}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                };
                let _ = stream.write_all(resp.as_bytes());
            }
        });
        (base, landed)
    }

    /// Red if the builder follows redirects (reqwest's default): the probe
    /// would make a SECOND request, to wherever the 302 points, and read a
    /// 200 from it as the service answering.
    #[tokio::test]
    async fn a_probe_never_follows_a_redirect_and_a_3xx_is_not_an_answer() {
        let (base, landed) = redirecting_server();
        let client = probe_client_builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .unwrap();
        let resp = client.get(format!("{base}/start")).send().await.unwrap();
        assert_eq!(resp.status().as_u16(), 302);
        assert!(!answered(resp.status()));
        assert_eq!(landed.load(Ordering::SeqCst), 0, "the redirect target was fetched");
        let ok = client.get(format!("{base}/landed")).send().await.unwrap();
        assert!(answered(ok.status()));
    }
}

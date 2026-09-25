// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! The ONE way the launcher and the hub build an HTTP client for something on
//! THIS machine — the hub's API (with its bearer token), the model gateway, a
//! module container's loopback port, VCO's own Weaviate / Ollama /
//! code-embed — v0.2.97 (R7b F19, generalised).
//!
//! Two rules, both on every client built here:
//!
//! * **No proxy.** reqwest reads `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`
//!   from the environment and does NOT exempt `127.0.0.1` or `localhost` on
//!   its own. With a proxy exported and no `NO_PROXY`, a request to
//!   `http://127.0.0.1:<hub port>` goes to the PROXY — the hub bearer token in
//!   its `Authorization` header included — and the proxy (which cannot reach
//!   our loopback anyway) answers instead of the hub.
//! * **No redirects** ([`super::probe_http`], R7a F7): an unrelated app on a
//!   loopback port answering `302 https://elsewhere/…` must not make the
//!   client leave the endpoint.
//!
//! The caller chooses the timeout ([`client`]) or adds more to the builder
//! ([`builder`]).
//!
//! A service endpoint read from the rows is usually loopback, but an ADOPTED
//! external endpoint can be another machine (a GPU box running Ollama). For
//! such a URL use [`builder_for`] / [`client_for`]: a loopback host gets the
//! loopback client; any other host keeps the environment's proxy (it may be
//! the only way to reach it) and still follows no redirect. "Loopback" is
//! [`super::service_endpoints::is_loopback_host`] — the one rule.

use std::time::Duration;

use super::probe_http::probe_client_builder;
use super::service_endpoints::is_loopback_host;

/// A client builder for a loopback target: no proxy, no redirects.
pub fn builder() -> reqwest::ClientBuilder {
    probe_client_builder().no_proxy()
}

/// A loopback client with the caller's total request timeout.
pub fn client(timeout: Duration) -> Result<reqwest::Client, String> {
    builder().timeout(timeout).build().map_err(|e| format!("http client: {e}"))
}

/// Is `url`'s host this machine? An unparseable URL is not.
pub fn is_loopback_url(url: &str) -> bool {
    reqwest::Url::parse(url)
        .ok()
        .and_then(|u| u.host_str().map(is_loopback_host))
        .unwrap_or(false)
}

/// A builder for `url`: [`builder`] when its host is loopback; otherwise a
/// builder that follows no redirect but keeps the environment's proxy.
///
/// "Loopback" is the LITERAL rule (R8 G10, on purpose): `localhost` in any
/// case (loopback by RFC 6761), 127/8, `::1`. A name that only RESOLVES to
/// this machine (`localhost.localdomain`, an `/etc/hosts` alias) keeps the
/// proxy: no name is resolved here, so the choice never depends on the
/// resolver at the moment of the call. Python's twin is
/// `vco_lib.service_probe_http.is_loopback_url`.
pub fn builder_for(url: &str) -> reqwest::ClientBuilder {
    if is_loopback_url(url) {
        builder()
    } else {
        probe_client_builder()
    }
}

/// [`builder_for`] with the caller's timeout.
pub fn client_for(url: &str, timeout: Duration) -> Result<reqwest::Client, String> {
    builder_for(url).timeout(timeout).build().map_err(|e| format!("http client: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    /// A test-owned responder on an ephemeral 127.0.0.1 port: answers every
    /// request `200 ok`. Never a real service.
    fn ok_server() -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        std::thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let mut stream = stream;
                let mut buf = [0u8; 2048];
                let _ = stream.read(&mut buf);
                let _ = stream
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok");
            }
        });
        base
    }

    /// With every proxy variable pointing at a dead port, a loopback call
    /// made through the helper still reaches the server directly. Red if
    /// [`builder`] drops `.no_proxy()`: the request then goes to the dead
    /// proxy and fails.
    #[tokio::test]
    async fn a_dead_http_proxy_does_not_affect_a_loopback_call() {
        let base = ok_server();
        let (c, c_for) = {
            let _env = crate::test_env::dead_proxy_env_guard();
            (client(Duration::from_secs(5)).unwrap(), client_for(&base, Duration::from_secs(5)).unwrap())
        };
        for cl in [c, c_for] {
            let resp = cl.get(format!("{base}/api/v1/health")).bearer_auth("t").send().await;
            let resp = resp.expect("straight to 127.0.0.1, not through the proxy");
            assert_eq!(resp.status().as_u16(), 200);
        }
    }

    /// The loopback rule is the service rows' own (`is_loopback_host`).
    #[test]
    fn loopback_urls() {
        for yes in [
            "http://127.0.0.1:7700/api/v1",
            "http://localhost:8081",
            "http://LOCALHOST:8081",
            "http://[::1]:11434/api/tags",
            "http://127.0.0.2:1/",
        ] {
            assert!(is_loopback_url(yes), "{yes}");
        }
        for no in [
            "http://gpu-box:11434",
            "http://192.168.1.5:8081",
            "not a url",
            "http://localhost.example.com/",
            "http://localhost.localdomain:8081",
        ] {
            assert!(!is_loopback_url(no), "{no}");
        }
    }
}

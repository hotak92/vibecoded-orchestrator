// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! The ONE hub liveness probe (v0.2.100 WP-06, F-W3-12).
//!
//! Three surfaces used to probe the hub's unauthenticated liveness route on
//! their own: the update pipeline's post-restart poll (`installer.rs`, a raw
//! TCP+HTTP write so it runs on a plain thread), the module service's
//! "wait for the hub it just started" loop (`module_service.rs`, reqwest)
//! and the GUI's `hub_info` (`hub_proxy.rs`, reqwest). Three copies of "which
//! path, which timeout, what counts as up" is how the pre-0.2.100 update poll
//! ended up probing `/health` — a route the hub does not mount — while the
//! others probed `/api/v1/health`. Now all three call [`probe`] (blocking) or
//! [`probe_async`].
//!
//! Raw socket, not reqwest: it needs no runtime and no blocking-reqwest
//! feature, so the same function serves the plain-thread caller and (via
//! `spawn_blocking`) the async ones.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

/// The hub's unauthenticated liveness route (exempt from the token gate).
pub const HEALTH_PATH: &str = "/api/v1/health";

/// Per-probe connect / read / write bound.
pub const PROBE_TIMEOUT: Duration = Duration::from_secs(2);

/// `true` when the hub answers `GET /api/v1/health` on `127.0.0.1:<port>`
/// with HTTP 200. Blocking, bounded by [`PROBE_TIMEOUT`] per step; any
/// failure (refused, timeout, other status) is `false`.
pub fn probe(port: u16) -> bool {
    let addr: SocketAddr = match format!("127.0.0.1:{}", port).parse() {
        Ok(a) => a,
        Err(_) => return false,
    };
    let mut stream = match TcpStream::connect_timeout(&addr, PROBE_TIMEOUT) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(PROBE_TIMEOUT));
    let _ = stream.set_write_timeout(Some(PROBE_TIMEOUT));
    let request = format!(
        "GET {} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
        HEALTH_PATH, port
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    // The status line fits in 64 bytes ("HTTP/1.1 200 OK\r\n").
    let mut buf = [0u8; 64];
    let n = match stream.read(&mut buf) {
        Ok(n) => n,
        Err(_) => return false,
    };
    is_ok_status_line(&String::from_utf8_lossy(&buf[..n]))
}

/// [`probe`] from async code (on the blocking pool).
pub async fn probe_async(port: u16) -> bool {
    tokio::task::spawn_blocking(move || probe(port)).await.unwrap_or(false)
}

/// Pure: does the response head start with a 200 status line?
pub fn is_ok_status_line(head: &str) -> bool {
    head.starts_with("HTTP/1.1 200") || head.starts_with("HTTP/1.0 200")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    fn serve_once(status: &'static str) -> (u16, std::thread::JoinHandle<String>) {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        let h = std::thread::spawn(move || {
            let (mut s, _) = l.accept().unwrap();
            let mut buf = [0u8; 512];
            let n = s.read(&mut buf).unwrap_or(0);
            let req = String::from_utf8_lossy(&buf[..n]).to_string();
            let _ = s.write_all(format!("HTTP/1.1 {}\r\nContent-Length: 0\r\n\r\n", status).as_bytes());
            req
        });
        (port, h)
    }

    #[test]
    fn a_200_on_the_liveness_route_is_up() {
        let (port, h) = serve_once("200 OK");
        assert!(probe(port));
        assert!(h.join().unwrap().starts_with("GET /api/v1/health HTTP/1.1"));
    }

    #[test]
    fn any_other_answer_or_no_listener_is_down() {
        let (port, h) = serve_once("404 Not Found");
        assert!(!probe(port));
        h.join().unwrap();
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let closed = l.local_addr().unwrap().port();
        drop(l);
        assert!(!probe(closed));
    }

    #[tokio::test]
    async fn the_async_form_is_the_same_probe() {
        let (port, h) = serve_once("200 OK");
        assert!(probe_async(port).await);
        h.join().unwrap();
    }

    #[test]
    fn status_line_rule() {
        assert!(is_ok_status_line("HTTP/1.1 200 OK\r\n"));
        assert!(is_ok_status_line("HTTP/1.0 200 OK"));
        assert!(!is_ok_status_line("HTTP/1.1 204 No Content"));
        assert!(!is_ok_status_line(""));
    }
}

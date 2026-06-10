# Known Test Failures (Pre-existing, Not Regressions)

Tracking known-flaky or pre-existing failures so v0.2.5x release gates
can distinguish them from new regressions introduced this cycle.

Each entry must include:
- **Symptom**: exact failure surface (test name, assertion line).
- **First seen**: which release / SHA we noticed it on (NOT when it
  was introduced).
- **Investigation status**: what we know, what we ruled out.
- **Retry policy**: how the release-gate should treat the failure.
- **Owner / next steps**: who's going to fix it + when.

Track-G3 + Track-E investigations queued additions to this file
during v0.2.53. See the master plan for the broader audit context.

---

## PRE-2 — `db::access::adopt_populated_tests` flaky on parallel runs

**Symptom**: tests `t2_vco_dev_shape_single_populated_candidate_adopted`,
`t3_multiple_populated_candidates_defer`, and `t5_idempotent_after_adoption`
in `launcher/src-tauri/vct-launcher-core/src/db/access.rs` flake on
both debug and release builds. Failure mode is always one of:
  * `t2` / `t5`: expected `adopted: 1, no_change: 0`, got `adopted: 0, no_change: 1`.
  * `t3`: expected `adopted: 0, deferred: 1`, got `adopted: 1, no_change: 0`.

Common pattern: the in-test `MockWeaviate` HTTP server returns a
response that the production code parses as "no candidate classes in
Weaviate" or "only one populated candidate". This causes the
`adopt_populated_collections_at_boot` branch logic to take a
different path than the test asserts.

**First seen**: pre-existing on base SHA `9396ea96` (Track G design-doc
commit on top of v0.2.52 merge). Track C noted these as DEFERRED
during v0.2.52 ship. The flake survives v0.2.53 Track E investigation.

**Investigation status (2026-06-10, Track E)**:

* Root cause is the `MockWeaviate` HTTP server's request parsing.
  The mock does a single `read()` on each accepted TCP stream, which
  on heavily-parallel test runs can return BEFORE the kernel has
  delivered the full HTTP request (headers + body). The class-name
  extraction from the GraphQL POST body's JSON returns empty when
  the body hasn't arrived yet → count probe returns 0 → candidate
  appears unpopulated → wrong branch taken.

* Attempted fix #1 (Track E, reverted): read until `\r\n\r\n` boundary,
  then drain `Content-Length` bytes. Reduced flake frequency from
  ~30% to ~30% (no measurable improvement). Reverted because the
  added 74 LoC didn't earn its keep — suggests the race is deeper
  than just header-boundary draining.

* `--test-threads=1` does NOT eliminate the flake. Even when these
  tests are run serially, the failure reproduces ~20% of runs. This
  suggests the race is internal to tokio's per-test runtime startup
  + the synchronous mock thread's accept loop scheduling, NOT
  contention between test cases.

* Hypothesis: the test thread (which made the reqwest call) and the
  mock listener thread (which serves the response) need to do a
  4-way handshake on a tiny localhost connection. On heavily loaded
  CI runners or laptops the kernel scheduler can delay the listener
  thread enough that reqwest's 5-second timeout fires (look at
  `client.timeout` in
  `vct-launcher-core/src/db/access.rs::adopt_populated_collections_at_boot`).
  Reqwest then returns `Err`, the function returns `Err`, the test's
  `.await.unwrap()` panics — but the panic message we see is the
  ASSERT failure, not the unwrap. Something doesn't add up there.
  Needs deeper investigation with `RUST_LOG=reqwest=trace` in a
  follow-up.

**Retry policy** for release gates:
* These tests may be retried up to 3 times in release CI. If they
  pass on retry, treat the suite as green. If they fail 3x, escalate
  for triage — the underlying race may have worsened.
* Pre-tag manual verification: run
  `cargo test -p vct-launcher-core --lib --release adopt_populated`
  10 times locally. Expected pass rate ~70%. Any LOWER pass rate
  indicates a regression of the race condition itself.

**Owner / next steps**:
* Track G3's flake-investigation work parked this on the v0.2.54
  backlog. Real fix likely requires replacing the hand-rolled
  `MockWeaviate` TCP server with `httpmock` or wiremock-rs (the
  `[dev-dependencies]` budget rationale that originally rejected
  this needs to be revisited — the flake is costing more dev time
  than the extra dependency would).
* Alternative: keep the hand-rolled mock but route reads through a
  Tokio-aware async TcpListener so the accept + read loop is
  cooperatively scheduled with the test's tokio runtime. This avoids
  the synchronous-thread / async-test cross-runtime race entirely.

---

## PRE-1 — Doc-test compilation failures (FIXED in v0.2.53 Track E)

**Symptom**: `cargo test --doc -p vct-launcher-core` reported 2
failures for `secrets::for_tests::fail_next_set` (line 789) and
`secrets::for_tests::MockGuard` (line 817) — both E0433 unresolved
path errors.

**First seen**: at least v0.2.51 (Track C noted as DEFERRED during
v0.2.52 ship).

**Resolution**: fixed in v0.2.53 Track E commit
`test(secrets): PRE-1 fix doc-tests for fail_next_set + MockGuard`.
The `no_run` example bodies were missing their `use vct_launcher_core::...`
imports; Rust's doc-test harness treats each example as a standalone
`main()` so unqualified references can't resolve.

**Status**: closed.

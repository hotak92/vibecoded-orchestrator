# Step 22 — Multi-Project Access-Matrix Regression Test

**Status**: active (v0.2.21 release-gate).
**Workflow**: [`.github/workflows/step22-multi-project-access-matrix.yml`](../../../.github/workflows/step22-multi-project-access-matrix.yml)
**Plan**: `.claude/context/plans/v0.2.21-hub-detachment-and-resolver.md` — search for "Multi-project regression test in CI" and "Acceptance criterion: KG + codegraph correct in EVERY project".

---

## What this test exercises

v0.2.21's release gate is: **"after install/update, KG and codegraph work correctly in every project on the user's machine, governed by the access matrix the user has set up in the launcher GUI"**.

Step 22 is the headless correctness regression that proves it. It is distinct from Step 7 (`release.yml` — the binary-shipping pipeline). The two share the "CI" label but never share a job.

The test stands up a sandboxed launcher.db with **3 projects + asymmetric access-matrix rows**, spawns the `vct-hub` binary against that sandbox, and asserts the resolver returns the right `kg_access_list` / `codegraph_access_list` for every project — and nothing else.

### Access matrix encoded in the fixture

| Project    | Own KG / codegraph     | Cross-grant received                |
| ---------- | ---------------------- | ----------------------------------- |
| A (alpha)  | own primary KG         | _none received_                     |
| B (beta)   | own primary KG         | A grants B read on A's codegraph    |
| C (gamma)  | own primary KG         | _none received_, explicit `none` row on B's KG (negative-leak test) |

Resolver responses the test pins:

| Project    | `kg_access_list`               | `codegraph_access_list`    |
| ---------- | ------------------------------ | -------------------------- |
| A          | `[A_KG, B_KG]`                 | `[proj-a-alpha]`           |
| B          | `[B_KG]`                       | `[proj-a-alpha, proj-b-beta]` |
| C          | `[C_KG]`                       | `[proj-c-gamma]`           |

Where `*_KG` is the sandbox-prefixed collection name (e.g. `STEP22_<run_id>_ProjectAAlpha_KnowledgeGraph`).

### Acceptance criterion properties covered

From the plan's "Acceptance criterion: KG + codegraph correct in EVERY project" list (10 properties total). This test covers **properties (1) through (9)**:

- (1) `projects.folder_path` resolves to a real on-disk path — fixture writes real dirs.
- (2) `project_kg_bindings WHERE role='primary'` exists per project — seeded by `_seed_kg_bindings`.
- (3) `project_codegraph_bindings` exists per project — seeded by `_seed_codegraph_binding`.
- (4) `module_settings.active_embedding` set per project — seeded by `_seed_module_settings`.
- (5) `kg_collection_access` rows are respected — `test_kg_access_list_matches_matrix_per_project` + `test_kg_access_list_filters_explicit_none_rows`.
- (6) `codegraph_access` grants are respected — `test_codegraph_access_list_matches_grant_matrix`.
- (7) `GET /api/v1/projects/{P.id}/config` returns a complete envelope — `test_resolver_returns_full_envelope_for_every_project`.
- (8) Pre-edit hook surface — `test_resolver_client_script_returns_same_json` proves the bash resolver client (which the hooks invoke to derive `VCT_KG_ACCESS_LIST` / `VCT_CODE_GRAPH_ACCESS_LIST` env vars) produces the same JSON as a direct HTTP call.
- (9) Weaviate MCP queries the correct collection — implicit via (5)/(8); the MCP's `KG_COLLECTION` env is set from the resolver's `kg_collection` field.

Property (10) — "GUI access-matrix edits propagate to resolver within ≤30s" — is GUI-driven and out of scope for a headless CI test. Covered separately in Step 27 acceptance tests (or future addition).

---

## How to run it locally

You need:

- Python 3.12+ with `pytest` installed.
- A built `vct-hub` binary. Build from the repo root:

  ```bash
  cd launcher/src-tauri
  cargo build --release -p vct-hub
  ```

  The binary lands at `launcher/src-tauri/target/release/vct-hub` (or `vct-hub.exe` on Windows).

Then from the repo root:

```bash
VCO_CI_FIXTURE=1 RUNNER_TEMP=/tmp pytest tests/integration/step22_multi_project/ -v
```

- `VCO_CI_FIXTURE=1` is **mandatory** — the fixture's defense-in-depth gate refuses to run without it (so you can't accidentally pollute real `~/.vct/` state by running `pytest` from a shell).
- `RUNNER_TEMP` is set by GitHub Actions automatically; locally you can point it anywhere outside `~/.vct/`.
- Set `VCT_HUB_BINARY=/absolute/path/to/vct-hub` to override the auto-detected binary path (the CI workflow uses this).

Expected output:

```
tests/integration/step22_multi_project/test_access_matrix.py::test_hub_health_responds PASSED
tests/integration/step22_multi_project/test_access_matrix.py::test_resolver_returns_full_envelope_for_every_project PASSED
tests/integration/step22_multi_project/test_access_matrix.py::test_kg_access_list_matches_matrix_per_project PASSED
tests/integration/step22_multi_project/test_access_matrix.py::test_kg_access_list_filters_explicit_none_rows PASSED
tests/integration/step22_multi_project/test_access_matrix.py::test_codegraph_access_list_matches_grant_matrix PASSED
tests/integration/step22_multi_project/test_access_matrix.py::test_no_kg_access_leakage_between_unrelated_projects PASSED
tests/integration/step22_multi_project/test_access_matrix.py::test_single_field_response_matches_full_envelope PASSED
tests/integration/step22_multi_project/test_access_matrix.py::test_resolver_client_script_returns_same_json PASSED

============================== 8 passed in ~5s ==============================
```

### Standalone fixture build (debugging)

You can also drive the fixture directly without pytest:

```bash
# Build the sandboxed launcher.db. Prints a JSON snapshot.
VCO_CI_FIXTURE=1 RUNNER_TEMP=/tmp VCO_TEST_RUN_ID=debug1 \
    python -m tests.integration.step22_multi_project.fixture build

# Inspect rows.
VCO_CI_FIXTURE=1 RUNNER_TEMP=/tmp \
    python -m tests.integration.step22_multi_project.fixture info --run-id debug1

# Clean up.
VCO_CI_FIXTURE=1 RUNNER_TEMP=/tmp \
    python -m tests.integration.step22_multi_project.fixture teardown --run-id debug1
```

---

## Sandbox-isolation guarantees

The fixture **MUST NOT** leak into a real user's launcher state. The defenses (codified in [`tests/common/sandbox.py`](../../common/sandbox.py)):

1. **Per-run `VCT_STATE_DIR` override** — every artifact (`launcher.db`, `hub.pid`, `hub.port`, `hub.token`, `cache/`, secrets resolver discovery files) lands under `$RUNNER_TEMP/.vct-step22-<run_id>/`. The real `~/.vct/` is untouched.
2. **Per-run Weaviate collection prefix** — collections the fixture creates are named `STEP22_<run_id>_…` so teardown can drop them as a group via prefix match. No real prod collection name (`*_KnowledgeGraph` without prefix) is ever touched.
3. **Per-run keychain prefix** — secrets the fixture writes use module-id prefix `step22-<run_id>-`. (Reserved for symmetry — the current Step 22 fixture writes no secrets.)
4. **No GUI surface** — the test spawns the `vct-hub` headless binary only. The Tauri GUI never launches during this regression, so even if a leak slipped through (1)/(2)/(3), it would not render in the user's UI.
5. **CI-only refuse-to-run** — the fixture's `assert_sandbox_safe` refuses with exit 1 if:
   - `VCO_CI_FIXTURE` is not set to `1`, OR
   - `VCT_STATE_DIR` points at `~/.vct/` (or a subpath of it), OR
   - `VCT_STATE_DIR` equals `$HOME`.

Combined, these mean that a developer accidentally typing `pytest tests/integration/step22_multi_project/` on their workstation gets a skip (no `VCO_CI_FIXTURE`) rather than a state-corrupting run. A workflow-level `VCO_CI_FIXTURE=1` toggle is the explicit consent the fixture demands before it touches anything.

---

## Architecture (read this before extending)

```
                       ┌──────────────────────────────────────┐
                       │ tests/common/sandbox.py              │
                       │ - SandboxLayout (paths + namespaces) │
                       │ - assert_sandbox_safe (defense gate) │
                       │ - teardown_sandbox (idempotent)      │
                       └────────────────┬─────────────────────┘
                                        │
                       ┌────────────────▼─────────────────────┐
                       │ tests/integration/step22_multi_…/    │
                       │   fixture.py                         │
                       │ - applies migrations to launcher.db  │
                       │ - seeds 3 projects + access matrix   │
                       │ - spawns vct-hub subprocess          │
                       │ - exposes build/teardown/info CLI    │
                       └────────────────┬─────────────────────┘
                                        │
                       ┌────────────────▼─────────────────────┐
                       │   test_access_matrix.py              │
                       │ - 8 pytest assertions                │
                       │ - module-scoped hub + fixture        │
                       │ - HTTP probes + bash resolver call   │
                       └──────────────────────────────────────┘
```

The fixture's `_apply_migrations` reads the canonical SQL files from `launcher/src-tauri/vct-launcher-core/src/db/migrations/` directly — there is no need to build a Rust binary just to seed the DB. This keeps the fixture fast (~0.5s setup) and means a migration drift would surface as a test failure in Python alongside the matching Rust unit-test failure.

The hub IS built (release profile) because the regression target is the actual production binary, not a mocked HTTP handler. The build is cached via `Swatinem/rust-cache@v2` shared with the main `rust` job — a warm cache shaves ~50s off CI runtime.

### When to add a test here

- **DO add** when introducing a new field to `ProjectConfigResponse` that gates on the access matrix.
- **DO add** when changing the resolver's filter logic (`access_level IN ('read','write')`, dedup, sort, etc.).
- **DO add** when adding a new `kg_*` or `codegraph_*` table that the resolver reads.

- **DO NOT add** general HTTP smoke checks here — those belong in `launcher/src-tauri/vct-hub/src/config_api.rs::tests`. Step 22 is exclusively for **multi-project + access-matrix** regressions.
- **DO NOT add** Weaviate-content tests here — this fixture doesn't seed real KG content. For end-to-end retrieval, use a separate integration test that bootstraps Weaviate documents.

---

## Failure modes + how to read CI output

If a test fails, the assertion message points at the exact axis:

- `kg_access_list mismatch` → either the resolver's filter is broken OR the fixture's encoding drifted from the test's expectations. Run `python -m tests.integration.step22_multi_project.fixture info --run-id <id>` against the live sandbox dir (look for `Run id:` in the workflow logs) to confirm what's in the DB.
- `leakage check failed` → the resolver returned a project's primary KG to a peer that should not have access. Likely cause: an access-level check defaulting to permissive.
- `resolver-client drift on <key>` → the bash `vct_project_config.sh` and the HTTP endpoint disagree on a field. The shell script does field extraction via `jq` or Python — check those code paths if both passed previously.
- `vct-hub did not write hub.port + hub.token within Xs` → the hub binary crashed during startup. Look at the surrounding stderr in the workflow log (the fixture captures it and re-raises).

---

## Related files

- [`tests/common/sandbox.py`](../../common/sandbox.py) — sandbox primitives.
- [`tests/integration/step22_multi_project/fixture.py`](fixture.py) — fixture builder + hub subprocess wrapper.
- [`tests/integration/step22_multi_project/test_access_matrix.py`](test_access_matrix.py) — 8 pytest assertions.
- [`.github/workflows/step22-multi-project-access-matrix.yml`](../../../.github/workflows/step22-multi-project-access-matrix.yml) — Linux + macOS + Windows matrix CI job.
- [`launcher/src-tauri/vct-hub/src/config_api.rs`](../../../launcher/src-tauri/vct-hub/src/config_api.rs) — the resolver this test exercises.
- [`templates/scripts/vct_project_config.sh`](../../../templates/scripts/vct_project_config.sh) — the resolver client this test invokes from bash.

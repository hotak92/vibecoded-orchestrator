# v0.2.3 Patch Review — Multi-OS + Install Flow

**Reviewer**: Opus 4.7 (1M context), xhigh effort
**Date**: 2026-05-12
**Patch**: KG-summary auto-backfill on add-project (uncommitted)
**Scope**: READ-ONLY review against actual diff/source (every claim cites file:line).

## Verdict: **PASS-WITH-NOTES**

Patch is solid, mechanical mirror of the v0.2.2 `kg_sync` pattern. All 25 new tests pass, the broader suite is green (575 passed, 1 ignored, 0 failed — `cargo test --lib` confirmed), `cargo check` produced zero new warnings (only 2 pre-existing in unrelated modules). Schema/migration/registration plumbing is correct. Cross-platform code paths follow `PathBuf::join` discipline and probe both POSIX and Windows venv shapes. Marker-string drift safety net is in place.

There is **one HIGH-severity Windows correctness concern** and a small handful of MEDIUM/LOW items worth flagging before ship. None are blockers — the failure modes are recoverable via the existing Retry affordance — but two of them warrant fixes within v0.2.3 if release schedule allows.

---

## A. Multi-OS findings

### A1. `PathBuf::join` discipline — PASS
Every path in `kg_summary.rs` uses `PathBuf::join` / `Path::join`. No string concatenation, no hard `/` separators. Verified at:
- `commands/kg_summary.rs:308-310` — `folder.join("knowledge")` + `enumerate_markdown_files`
- `commands/kg_summary.rs:1034, 1041, 1051, 1063` — script resolution candidates
- `commands/kg_summary.rs:1092-1106` — venv-python layouts (POSIX `bin/python`, `bin/python3`, Windows `Scripts/python.exe`)

### A2. Subprocess via `tokio::process::Command` with arg-vec — PASS
- `commands/kg_summary.rs:686-712` — no `cmd /c`, no `bash -c`, no shell:true. Each arg is a separate `.arg(...)` call.
- `commands/kg_summary.rs:714-718` — `CREATE_NO_WINDOW` (0x08000000) flag applied on Windows via `#[cfg(windows)] use std::os::windows::process::CommandExt; cmd.creation_flags(...)`. Mirrors `kg_sync` / `codegraph` exactly.

### A3. Venv python resolution probes BOTH POSIX and Windows layouts — PASS
`commands/kg_summary.rs:1090-1111` `venv_python_in()` helper probes, for each candidate `<root>/.venv` and `<root>/claude_mcp_servers/.venv`:
- POSIX: `bin/python`, `bin/python3`
- Windows: `Scripts/python.exe`

This matches the matrix used by `codegraph::looks_like_install_root` (confirmed in module docstring at kg_summary.rs:1082-1085).

### A4. `claude` CLI invocation on Windows — **HIGH SEVERITY**
`templates/scripts/generate-kg-summary.py:110` — `shutil.which("claude")` correctly resolves `claude.exe` / `claude.cmd` / `claude.bat` on Windows (Python honors `PATHEXT`).

**BUT** `templates/scripts/generate-kg-summary.py:117-123` then does:

```python
result = subprocess.run(
    ["claude", "-p", full_prompt, "--model", "haiku", "--max-turns", "1"],
    capture_output=True, text=True, timeout=TIMEOUT, ...
)
```

On Windows, when the `claude` CLI is shipped as `claude.cmd` or `claude.bat` (the standard npm-installed shape — `npm install -g @anthropic/claude-code` writes `claude.cmd` to the npm prefix), `subprocess.run(["claude", ...])` with the default `shell=False` will raise `FileNotFoundError: [WinError 2] The system cannot find the file specified`. Python's `subprocess` on Windows looks for an exact `claude.exe` and does **not** consult `PATHEXT`.

**Behaviour cascade**:
1. `cli_available()` (line 109-110) returns `True` (shutil.which sees `claude.cmd`).
2. `select_backend()` (line 279-282) chooses `"cli"` and logs `KG-summary backend: cli (claude on PATH)`.
3. `call_cli()` (line 117-126) calls `subprocess.run(["claude", ...])` → `FileNotFoundError`.
4. Python `main()` has **no top-level try/except** (verified at lines 373-436) → exception propagates → non-zero exit with traceback on stderr.
5. Launcher classifies as `NodeOutcome::Failed` (kg_summary.rs:769-790).
6. Three consecutive failures hit `SUBPROCESS_FAIL_FAST_THRESHOLD` (kg_summary.rs:64, 587-601) → terminal status `failed` with cryptic "exit 1: traceback…" snippet.

**User-visible impact**: Windows users with `claude` CLI installed via the standard npm shape see the banner go red with a stack trace, not the friendly "no backend available" skipped state. The fallback chain to Ollama / API key is never reached because `cli_available()` returns True. They'd have to remove the CLI from PATH, unset, or read the stack trace to figure out what's broken.

**Fix**: In `call_cli()` use `shutil.which("claude")` to get the absolute resolved path (including `.cmd`/`.bat` extension), then pass it to `subprocess.run` instead of the bare `"claude"` string. On Windows, also pass `shell=False` with the resolved path — Python's CreateProcess will then route `.cmd`/`.bat` through `cmd.exe` only when explicitly told via the resolved extension. Alternatively use `subprocess.run([shutil.which("claude"), ...])` — `shutil.which` already returns the right extension.

This is HIGH not BLOCKING because: (a) Ollama path works fine on Windows; (b) API key path works fine; (c) users without claude CLI hit the friendly skip path; (d) only the specific intersection of "Windows user + claude CLI installed via npm + no Ollama running" hits the bad UX; (e) Retry remains available.

### A5. macOS HFS+ case-insensitive `.md` matching — PASS
`commands/kg_summary.rs:1005-1009` uses `ext.eq_ignore_ascii_case("md")`. Test at `kg_summary.rs:1211-1220` (`enumerate_is_case_insensitive_on_extension`) covers `.md`, `.MD`, `.Md`. The Python script reads file content via `Path.read_text` (line 87) which is OS-native and case-preserving.

### A6. No new shell scripts shipped — PASS
Decision A in patch summary asserts "no new `.sh`/`.ps1` scripts". Verified: only the `.py` is touched. `git status` confirms no new files under `templates/scripts/` besides the modified `generate-kg-summary.py`. Cross-platform parity preserved through direct venv-python invocation.

### A7. Environment variable plumbing — PASS-WITH-NOTE
`commands/kg_summary.rs:692-705` sets `KG_PROJECT_ROOT`, `PROJECT_NAME`, `KG_COLLECTION`, `WEAVIATE_URL`, `KG_SUMMARY_OLLAMA_URL`, `OLLAMA_URL`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.

**Note (MEDIUM)**: `WEAVIATE_URL` is set but **the Python script ignores it**. `generate-kg-summary.py:354` hardcodes `weaviate.connect_to_local(host="localhost", port=8081, grpc_port=50052)`. If the user is running Weaviate on a non-default port (e.g. via VCT_STATE_DIR-isolated dev setup), the multi-chunk path silently degrades to single-summary mode via the existing try/except at `generate-kg-summary.py:368-370`. Correctness-safe (returns `[]`) but the env var is currently dead weight in the launcher invocation. Either (a) plumb `WEAVIATE_URL` into the Python script's `connect_to_local` call, or (b) drop the env var from the launcher invocation. Not a release blocker — defaults work in practice.

---

## B. Install-flow findings

### B1. Migration ordering — PASS
`db/migrations.rs:71-80` registers 011 (kg_syncs) then 012 (kg_summaries) in the `MIGRATIONS` const array. The `apply()` function (line 104) iterates in order, gating on `version > current_version`. Confirmed running fresh in-memory DBs in the test suite (`db::kg_summaries::tests::*` — 9 tests pass against fresh in-memory DBs that apply all migrations including 012).

### B2. Migration 012 SQL — PASS
`db/migrations/012_kg_summaries.sql:35-51` creates `kg_summaries` table with:
- PRIMARY KEY `project_id` matching `kg_syncs` shape.
- CHECK constraint on status enum matches Rust constants at `db/kg_summaries.rs:30-36`.
- FK with `ON DELETE CASCADE` to `projects(id)`. Cascade verified by test `cascade_delete_removes_summary_row` (`db/kg_summaries.rs:415-424`).
- Index on `status` for the resume-sweep query (`list_pending_kg_summaries` / orphaned-running update).

### B3. Boot-time setup resume sweep — PASS
`lib.rs:222-242`:
- `app.handle()` is shared across all three resume calls (codegraph, kg_sync, kg_summary). All three return `(swept, respawned)` tuples and the aggregate is logged only if non-zero (line 235).
- `kg_summary::resume_pending_summaries` (`commands/kg_summary.rs:194-254`) runs `mark_orphaned_running_kg_summaries_failed` first, then iterates `list_pending_kg_summaries`, calls `db.get_project(pid)` for each, and re-spawns. Soft-fail at every step (warn-log + continue, never panic).
- Empty `kg_summaries` table handled gracefully: `list_pending_kg_summaries` returns `Ok(vec![])` (verified at test `list_pending_returns_only_pending_rows_in_started_at_order` and the SQL `WHERE status='pending'` clause).

### B4. Tauri command registration — PASS
`lib.rs:499-500` adds `commands::kg_summary::get_kg_summary_status` and `commands::kg_summary::retry_kg_summary` to the `invoke_handler` macro. Style matches the kg_sync registrations.

### B5. Frontend DTO match — PASS
`launcher/src/lib/types/launcher.ts:358-389` `KgSummaryStatus` enum + `KgSummaryView` interface fields exactly mirror Rust `commands/kg_summary.rs:82-100`:
- Field-name parity (snake_case, no rename layer).
- `started_at_iso` / `finished_at_iso` typed as `string | null` matching `Option<String>` from Rust.
- `current_phase` field present on both sides (`string | null` ↔ `Option<String>`).
- Status union (`'pending'|'running'|'success'|'failed'|'skipped'`) matches `db/kg_summaries.rs::status::*` constants.

### B6. `create_project_v2` spawn ordering — PASS
`commands/projects_v2.rs:519-586`:
- Line 495-501: codegraph spawn (using `app.clone()`).
- Line 532-537: kg-sync spawn (using `app.clone()`).
- Line 580-585: **kg-summary spawn last** (consumes `app` by move — correctly placed since nothing else needs `app` after this).
- All three live AFTER `run_install_bundle` (confirmed earlier in the function around line 460 area where the bundle install completes). The summariser script and venv are guaranteed to exist when the summary task picks up.
- Pending row upsert (line 564-573) precedes the spawn so the GUI sees an immediate pending banner.
- DB error on pending insert is logged (line 575-577) but does NOT propagate — project create succeeds regardless.

### B7. Per-project script resolution — PASS
`commands/kg_summary.rs:1028-1070` `resolve_summary_script`:
1. **Project-local** `<project>/.claude/scripts/generate-kg-summary.py` (line 1034-1037) — this is the canonical path post-bundle-install. ✓
2. `$VCT_LAUNCHER_SCRIPTS_DIR` env override (line 1040-1045) — test hook.
3. Sibling-of-exe convention (line 1048-1057).
4. PATH lookup (line 1060-1068).

The first hit wins. For an add-project flow the project-local copy always exists (bundle install drops it from `templates/scripts/`). Test `resolve_summary_finds_project_local_copy` (line 1222-1232) confirms.

### B8. Empty project (no `.md` files) — PASS
`commands/kg_summary.rs:312-339`: if `enumerate_markdown_files(&knowledge_dir).is_empty()`, transition to `skipped` with `error_message: "no knowledge/**/*.md files to summarise"`, emit, return. No subprocess spawn. Idempotent.

### B9. Re-add idempotency — PASS
The `upsert_kg_summary` ON CONFLICT clause (`db/kg_summaries.rs:128-140`) replaces every field on conflict — so re-adding (or `retry_kg_summary`) cleanly overwrites the previous row. Confirmed by test `upsert_overwrites_on_state_transition` (line 347-375).

The summariser itself is content-hash idempotent: an unchanged node prints `unchanged (hash match), skipping` and exits 0 (`generate-kg-summary.py:398-400`), which the launcher classifies as `NodeOutcome::Unchanged` and counts under `nodes_unchanged`.

---

## C. Summariser script findings

### C1. Three-tier fallback logic — PASS-WITH-NOTE
`generate-kg-summary.py:268-297`. Order: env override → cli → ollama → api → skip. Cached in `_BACKEND_CACHE` (line 265) so subsequent calls within the same subprocess don't re-probe.

Note (MEDIUM): see A4 — the cli path on Windows raises rather than gracefully falling through. The fallback chain only works on Linux/macOS, or on Windows when `claude` is NOT on PATH.

### C2. Windows `claude.exe` / `claude.cmd` resolution via `shutil.which` — PARTIAL
- `cli_available()` (line 109-110) uses `shutil.which("claude")` which DOES resolve `.cmd`/`.bat` on Windows ✓.
- `call_cli()` (line 117) passes bare `"claude"` to `subprocess.run` which does NOT resolve `.cmd`/`.bat` ✗.

This is the HIGH-severity issue captured in A4 above.

### C3. Ollama call uses `urllib.request` only — PASS
`generate-kg-summary.py:197-226`. Uses `urllib.request.urlopen` + `urllib.request.Request` exclusively (no `requests`, no `httpx`). Stdlib-only. ✓

### C4. `ANTHROPIC_API_KEY` path uses raw HTTP — PASS
`generate-kg-summary.py:236-259`. Uses `urllib.request` with manual JSON payload + headers. No `anthropic` SDK import. Stdlib-only. ✓

### C5. Output markers stable + drift-guarded — PASS
- `NO_BACKEND_MARKER = "no backend available"` (kg_summary.rs:68) is present in the script's log line at `generate-kg-summary.py:294-295`: `"  KG-summary: no backend available (no claude CLI, no Ollama at "`. ✓
- `UNCHANGED_MARKER = "unchanged (hash match)"` (kg_summary.rs:73) is present in the script's log line at `generate-kg-summary.py:399`: `f"  {title}: unchanged (hash match), skipping"`. ✓
- Backend identifier line `"KG-summary backend: cli|ollama|api|skip"` parsed by `parse_backend_from_stdout` (kg_summary.rs:806-822) matches script lines 276, 281, 285, 289, 293-296.
- Drift-guard unit tests (`no_backend_marker_string_matches_script_log_line` at kg_summary.rs:1378-1387 and `unchanged_marker_string_matches_script_log_line` at lines 1389-1393) hold literal snippets of the script's canonical lines, so any rename in the Python script will fail the Rust test loudly.

### C6. `--force` flag — PASS
`generate-kg-summary.py:376` defines `--force` arg via argparse. Line 398 gates the hash-match short-circuit on `not args.force`. Behaves as expected. (Not wired through from the launcher GUI — but the retry pathway always upserts a fresh row, and the per-file hash check is cheap.)

### C7. Two copies of the script in parity — PASS
`diff templates/scripts/generate-kg-summary.py .claude/scripts/generate-kg-summary.py` returns no diff. Both are exactly 440 lines. ✓

### C8. `WEAVIATE_URL` env var ignored — MEDIUM (already captured in A7)

### C9. `main()` lacks top-level try/except — MEDIUM
`generate-kg-summary.py:373-436`. Any uncaught exception from `call_cli` / `call_ollama` / `call_api` / `generate_description` / `generate_summary` propagates out of `main` and Python crashes with a non-zero exit and a stack trace to stderr.

Launcher captures stderr (kg_summary.rs:736-738), snips the last non-empty stderr line into the error message (kg_summary.rs:773-781, 200-char cap), and counts the node under `nodes_failed`. After 3 consecutive failures the run goes terminal-failed.

Net effect: an Ollama 503, a network blip on the API path, or any other transient exception shows up in the banner as `"exit 1: <last stderr line>"` rather than something more specific. Functionally acceptable; cosmetically a bit cryptic. The log_tail expansion does include the full traceback so debugging is possible.

**Fix (optional, not blocking)**: wrap the body of `main()` in `try/except Exception as e: log(f"  Error: {e}"); sys.exit(2)` so the user sees a clean error line. Out of scope for this patch.

---

## D. Issues by severity

### BLOCKING
None.

### HIGH
- **H1. Windows `claude` CLI invocation** — `templates/scripts/generate-kg-summary.py:117-123` calls `subprocess.run(["claude", ...])` with bare string. On Windows where `claude` is `.cmd`/`.bat` (typical npm shape), this raises `FileNotFoundError`. Causes red-banner failure instead of clean Ollama/API/skip fallback for the specific case of "Windows + claude CLI on PATH + no Ollama running". Recommended fix: `subprocess.run([shutil.which("claude"), ...])`. Workaround for users: install Ollama or unset claude from PATH.

### MEDIUM
- **M1. `WEAVIATE_URL` env var ignored by script** — `generate-kg-summary.py:354` hardcodes `localhost:8081`. Launcher passes `WEAVIATE_URL` (kg_summary.rs:695) but it's dead weight. Multi-chunk path silently degrades to single-summary if Weaviate is on a non-default port. Fix: plumb the env var into `connect_to_local()`.
- **M2. `main()` lacks top-level try/except** — uncaught exceptions surface as raw `exit 1: <traceback line>` in the banner. Cosmetic; log_tail captures the full traceback so debugging works.

### LOW
- **L1. `_BACKEND_CACHE` per-subprocess only** — Decision C in patch summary correctly notes the `mixed` backend column is currently unreachable. Reserved schema; not a bug.
- **L2. `nodes_skipped` lumps "no backend" and "no title"** — Patch summary acknowledges. Log_tail captures the per-node reason.
- **L3. Banner z-index / scroll** — Inherited from v0.2.2 caveat. Banner scrolls with the page.

---

## E. Pre-ship checklist

| Check | Status | Evidence |
|---|---|---|
| `cargo check` clean (no NEW warnings) | ✅ | Only 2 pre-existing warnings in unrelated modules (field `file_removed`, method `has_secret_grant`). |
| `cargo test --lib` clean | ✅ | 575 passed, 0 failed, 1 ignored. 25 new tests (9 db + 16 commands) all green. |
| Marker-string drift guards | ✅ | `no_backend_marker_string_matches_script_log_line` + `unchanged_marker_string_matches_script_log_line` pass against the canonical 440-line summariser. |
| Migration registered and ordered | ✅ | migrations.rs:76-80. Index after 011. |
| Module registrations | ✅ | db/mod.rs:34, commands/mod.rs:12. |
| Tauri invoke_handler additions | ✅ | lib.rs:499-500. |
| Resume sweep wired into setup() | ✅ | lib.rs:233-234, aggregated into boot log line 235-241. |
| Spawn ordering in create_project_v2 | ✅ | After install bundle (line 580-585). |
| Two-copy script parity | ✅ | `diff` returns empty. Both 440 lines. |
| Cross-platform `PathBuf::join` | ✅ | Verified in commands/kg_summary.rs and db/kg_summaries.rs. |
| Cross-platform venv probe | ✅ | POSIX bin/python(3) + Windows Scripts/python.exe in resolve_venv_python. |
| CREATE_NO_WINDOW on Windows | ✅ | commands/kg_summary.rs:714-718. |
| Frontend types match Rust DTO | ✅ | launcher.ts:358-389 ↔ commands/kg_summary.rs:82-100. |
| FK cascade verified | ✅ | Test `cascade_delete_removes_summary_row`. |
| Idempotent retry via ON CONFLICT | ✅ | Test `upsert_overwrites_on_state_transition`. |
| Resume-after-crash semantics | ✅ | Tests `list_orphaned_running_returns_only_running_rows`, `mark_orphaned_running_flips_status_to_failed_and_sets_error_message`. |
| Windows `claude.cmd` resolution in script | ⚠️ HIGH | Fix recommended pre-ship (see H1). Workaround documented. |
| `WEAVIATE_URL` plumbing (script side) | ⚠️ MEDIUM | Either plumb in or drop the env var (M1). |
| `main()` try/except (script side) | ⚠️ MEDIUM | Optional polish (M2). |

**Ship verdict**: Suitable for v0.2.3 release with the documented HIGH/MEDIUM caveats. H1 is the only fix worth strongly considering before merge — it's a one-line change to `call_cli()` in `generate-kg-summary.py` and would prevent a non-obvious Windows UX papercut. M1/M2 can defer to v0.2.4.

## Known Issues

Tracking polish-grade items that ship with the launcher but are worth
flagging for early adopters and the next iteration.

## Current caveats

- [ ] **macOS support remains Tier-2** — carries the
      Tier-2 caveats documented under "Install / first-run" below
      (Bash 3.2 quirks, Finder exec-bit stripping, `.command` quarantine
      attribute, manual Homebrew bootstrap). The Tauri auto-restart
      flow's macOS-specific runtime path is still verified-by-CI-only.
      Linux remains the recommended platform. (needs verification post-v0.2.50)

- [ ] **Launcher binaries remain unsigned on Windows + macOS** —
      SmartScreen and Gatekeeper warnings persist; Apple notarization
      enrollment is still pending. Workarounds in
      [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#first-install-issues).

- [ ] **The arctic SECONDARY embedding slot has an uncovered-text gap on
      oversized chunks** (v0.2.92, accepted). When dual embedding is enabled and
      qwen3 is the active model, a chunk larger than arctic's window is embedded
      from its leading sub-window; the tail influences no arctic vector and the
      row is tagged `emb_truncated`.

      **No user's retrieval is affected.** Chunks are sized to the ACTIVE model,
      so a qwen3-active install retrieves on full-coverage qwen3 vectors, and a
      low-power arctic-active install has chunks sized to arctic. The gap exists
      only in the dual-write telemetry configuration (off by default), and only
      for the secondary slot.

      What it does affect is the arctic TRAINING corpus. Measured 2026-09-04:
      17.0% of chunks (12.9% of text) in a real project's `knowledge/`, and
      53.2% of chunks (50.9% of text) in this repo's `docs/`.

      The fix is pooling — embedding an oversized chunk as several consecutive
      sub-windows pooled into one vector, which gives full coverage without
      moving chunk boundaries (boundaries are frozen because both named vectors
      share one Weaviate object and the RL replay pairs slots by `chunk_num`).
      Deferred deliberately: a pooled vector is not the same object as a
      single-window embedding, so the change carries a modelling question that
      deserves its own consideration rather than a late-cycle decision.

      **Shipped as-is with explicit user approval (2026-09-04).** The owner
      reviewed the measured gap and ruled: *"if ONLY the secondary gets a
      hole, I'd say it's kind of ok"* (2026-09-04); per the design record
      (R44 RESOLVED) pooled multi-window embedding is a modelling decision,
      not an engineering defect, so it does not block a release. Retrieval is
      unaffected — chunks are sized to the ACTIVE model, so no user's search
      reads a truncated vector — and with the round-4 refusal-halving loop
      the secondary still gets a prefix (leading-window) vector for every
      piece of content, none dropped, each tagged `emb_truncated` on the
      dual-log RL event.

## Install / first-run

- [ ] **macOS support is experimental** — only minimal smoke-tested on a single Apple
      Silicon machine (Bash 3.2 empty-array fix landed during that test, see commit `cb3df13`).
      Known macOS-specific gotchas: Apple ships Bash 3.2 (the rest of the world uses 4.x+), Finder
      strips the exec bit on zip downloads, `.command` files need `xattr -dr com.apple.quarantine`
      after zip extraction, and Homebrew is not installed by default. The full Linux path is
      validated; the macOS path beyond `first-install.command` reaching `install.sh` is
      not. Linux is the recommended platform for v0.2.x; macOS Tier-2.

- [ ] **Update-orchestrator auto-restart path is verified-by-CI-only on macOS** —
      the Rust `pid_is_alive` (using `kill(pid, 0)` + `std::io::Error::last_os_error()`),
      `pre_pull_rename_running_binary` (Windows no-op on macOS), `sweep_stale_binary_siblings`,
      and `restart_launcher` spawn-detached paths all compile cleanly on macOS-arm64 in CI
      and pass cargo's link step. The cargo test suite that exercises these paths runs on
      Linux only (CI matrix), so the macOS-specific runtime behavior of the auto-restart
      flow has not been hand-verified. Risk surface: `kill(pid, 0)` semantics
      + errno-via-`last_os_error()` are POSIX-portable and well-documented, but
      `std::process::Command::pre_exec(setsid)` behavior across XNU + Tauri's GUI lifecycle
      hasn't been exercised end-to-end on macOS. Expected to work; flag to retest before
      promoting macOS off Tier-2. (needs verification post-v0.2.50)

- [ ] **Launcher binary not yet code-signed (Windows + macOS)** — Windows shows SmartScreen "Windows
      protected your PC"; macOS Gatekeeper shows "damaged and can't be opened". Both are expected for
      v0.2.x. Workarounds documented in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#first-install-issues).
      Code signing is on the post-0.2.0 backlog.

- [ ] **Apple Developer enrollment / notarization pending** — the macOS `.dmg` is built unattended in
      CI without notarization. Intel Mac users must build from source for v0.2.x; a Universal binary
      is on the post-0.2.0 backlog.

- [ ] **Linux .desktop double-click requires per-file-manager config** — documented in
      [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#linux-desktop-file-doesnt-open-on-double-click).
      No code fix pending; terminal fallback (`bash first-install.sh`) always works.

- [ ] **Container runtime install on macOS/Windows is URL-only** — `first-install.*` cannot auto-install
      Podman/Docker on macOS or Windows; it prints the URL and exits. Linux uses pkexec for interactive
      install. No change planned for v1.0 — container runtimes on those platforms require user consent
      GUI steps that can't be scripted portably.

- [x] **Cosmetic warnings during seed (non-blocking)** — `AuthlibDeprecationWarning` from a
      transitive dep of `weaviate-client`, and several "No abstraction level tag" /
      "Tag 'LoRA' uses camelCase" vocabulary warnings from the bundled seed nodes in
      `knowledge/concepts/`. None affect correctness; the install completes successfully.
      *Resolved in v0.2.52: targeted ``warnings.filterwarnings`` for the authlib
      noise (4 scripts), abstraction-level tags added to 31 concept nodes,
      and 10 camelCase tag renames (LoRA→lora, IaC→iac, ComfyUI→comfyui, …).
      Regression-pinned by ``tests/test_no_authlib_deprecation_warning.py``,
      ``tests/test_kg_seed_nodes_have_abstraction_tags.py``, and
      ``tests/test_kg_seed_nodes_use_kebab_case_tags.py``.*

- [ ] **First-install grew by ~150 MB for Playwright MCP** — the default-enabled
      `playwright` MCP entry pre-caches Chromium during `install.py` so the first
      browser-automation call doesn't stall on a 150 MB download. Bandwidth-constrained
      users can opt out by exporting `VCT_SKIP_PLAYWRIGHT=1` before running
      `first-install.sh` / `install.py`; the MCP will then lazy-install Chromium on
      its first browser-launch instead. The pre-cache is non-fatal — if `npx` is
      missing or the download fails, the install logs a warn event and continues.

- [ ] **KG summaries may be empty on add-project if no summariser backend is
      reachable** (0.2.3, `commands::kg_summary`). The KG-summary background task
      that runs on `create_project_v2` walks `knowledge/**/*.md` and shells out
      to `templates/scripts/generate-kg-summary.py`. That script picks the first
      available backend in order — `claude` CLI on PATH → Ollama at
      `KG_SUMMARY_OLLAMA_URL` (default `http://localhost:11435`, model
      `KG_SUMMARY_OLLAMA_MODEL`, default `qwen3.5:9b`) → `ANTHROPIC_API_KEY`
      direct. If none of the three is reachable, the script logs
      `KG-summary: no backend available` and exits 0; the launcher detects this
      marker on the first node, hard-stops the walk, and transitions the
      `kg_summaries` row to `skipped` with the install hint surfaced under the
      banner's `Show details`. The `.node_formats.json` sidecar then backfills
      lazily as the user edits each node in a Claude session (the PostToolUse
      hook `kg-summary-generator.{sh,ps1}` runs the same script per file).
      Workaround: install one of the three backends (Ollama is the default for
      VCO installs; if the install completed normally it should be reachable),
      then click `Re-build KG summaries` on the project page. Not a launcher
      bug — the lazy path always existed; the 0.2.3 work is the startup
      optimisation, not a hard dependency.

## Recently fixed

See [CHANGELOG.md](CHANGELOG.md).

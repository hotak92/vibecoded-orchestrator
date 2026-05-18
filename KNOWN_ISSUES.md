## Known Issues

Tracking polish-grade items that ship with the launcher but are worth
flagging for early adopters and the next iteration.

## Visual / UX

- [ ] **Tauri visual QA of per-project accent** — verify by running
      `npm run tauri:dev`, creating 3 projects, switching between them
      in the MenuBar selector, and confirming the 5px strip color +
      tinted project-name pill change distinctly per project (Wong 2011
      colorblind-safe palette). Browser preview confirms the CSS
      plumbing; only the bundled WebKit render path remains untested
      end-to-end. Delete this entry after manual verification.

## Install / first-run

- [ ] **macOS support is experimental** — only minimal smoke-tested on a single Apple
      Silicon machine (Bash 3.2 empty-array fix landed during that test, see commit `cb3df13`).
      Known macOS-specific gotchas: Apple ships Bash 3.2 (the rest of the world uses 4.x+), Finder
      strips the exec bit on zip downloads, `.command` files need `xattr -dr com.apple.quarantine`
      after zip extraction, and Homebrew is not installed by default. The full Linux path is
      validated; the macOS path beyond `first-install.command` reaching `install.sh` is
      not. Linux is the recommended platform for v0.2.x; macOS Tier-2.

- [ ] **v0.2.17 Update Orchestrator auto-restart path is verified-by-CI-only on macOS** —
      the Rust `pid_is_alive` (using `kill(pid, 0)` + `std::io::Error::last_os_error()`),
      `pre_pull_rename_running_binary` (Windows no-op on macOS), `sweep_stale_binary_siblings`,
      and `restart_launcher` spawn-detached paths all compile cleanly on macOS-arm64 in CI
      and pass cargo's link step. The cargo test suite that exercises these paths runs on
      Linux only (CI matrix), so the macOS-specific runtime behavior of v0.2.17's
      auto-restart flow has not been hand-verified. Risk surface: `kill(pid, 0)` semantics
      + errno-via-`last_os_error()` are POSIX-portable and well-documented, but
      `std::process::Command::pre_exec(setsid)` behavior across XNU + Tauri's GUI lifecycle
      hasn't been exercised end-to-end on macOS. Expected to work; flag to retest before
      promoting macOS off Tier-2. Windows verification is being handled separately by a
      tester with a Windows-x64 machine.

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

- [ ] **Cosmetic warnings during seed (non-blocking)** — `AuthlibDeprecationWarning` from a
      transitive dep of `weaviate-client`, and several "No abstraction level tag" /
      "Tag 'LoRA' uses camelCase" vocabulary warnings from the bundled seed nodes in
      `knowledge/concepts/`. None affect correctness; the install completes successfully.
      Vocabulary cleanup of seed nodes is on the v0.2.x chore backlog.

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

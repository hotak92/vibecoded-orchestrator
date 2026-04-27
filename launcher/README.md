# VCT Launcher

The desktop GUI for the [VibeCoded Orchestrator](https://github.com/hotak92/vibecoded-orchestrator).

## What it does

The launcher is the operator's cockpit for everything the orchestrator manages on a single machine:

- **Projects** — register Claude Code projects, install/update the orchestrator into each one, and switch between them. Per-project env injection writes the right `KG_COLLECTION` / `OLLAMA_URL` / etc. into `.claude/settings.json` (Claude Code), `.vscode/settings.json` (VS Code), and `.claude/env` (CLI shell). User code is never touched.
- **Secrets** — OS-keychain-backed storage for API keys (Anthropic, OpenAI, GitHub PAT, Lemon Squeezy license, …), exposed to MCP servers as scoped env vars. No plaintext on disk.
- **Modules** — catalog of orchestrator components and add-ons (Knowledge Graph, Code Graph, RL Reranker, MAO). Status, version, license tier, install action.
- **Knowledge Graph dashboard** — node count, recent writes, search, sync state, duplicates audit.
- **Code Graph dashboard** — modules / classes / functions / APIs / interactions, last analysis run, re-index trigger.
- **MCP Dashboard** — running state of `weaviate-kg`, `ollama`, `search`, `code-embedding` servers; restart / view logs.
- **Audit log** — every command the launcher issues against your machine (compose start/stop, settings rewrite, license change). Exportable.
- **Settings + Onboarding** — first-run wizard, container runtime selection (podman / docker), shared-volume layout, license activation.

Free tier is fully functional without a key; Pro/Admin tiers gate add-on modules.

## Quick start

```bash
cd launcher
npm install
npm run tauri:dev
```

`tauri:dev` launches the Svelte frontend under Vite + the Rust Tauri shell with hot reload on both sides. First build pulls Tauri 2's CLI and compiles the Rust workspace; subsequent runs are fast.

## Build

```bash
npm run tauri:build
```

Produces a platform-native installer (AppImage / .deb on Linux, .dmg on macOS, .msi on Windows) under `src-tauri/target/release/bundle/`.

## Other useful scripts

| Command | What it does |
|---|---|
| `npm run dev` | Frontend only (Svelte + Vite). Useful when iterating on UI without touching Rust. |
| `npm run check` | `svelte-kit sync && svelte-check` — type-checks all `.svelte` and `.ts` files. |
| `cd src-tauri && cargo test --lib` | Runs the Rust unit tests (keychain, sqlite state schema, audit log, install safety, MCP registration, licensing). |

## Architecture (one paragraph)

Tauri 2 desktop shell + Svelte 5 (runes) frontend talking to a Rust backend over Tauri's IPC bridge. The Rust side owns all privileged operations (subprocess spawn, settings rewrites, container compose) via `tokio::process::Command` with parameterised args (no shell injection). State is a single SQLite database at `~/.vct/launcher.db` (projects, audit log, license tier cache). Optional companion runtime: an embedded `axum` HTTP hub (port 11445) for module-to-module RPC. Secrets live in the OS keychain (`keyring` crate); license tiers are validated against the public alias `https://api.vibecodedtools.it/validate-tier` with a 3-day offline grace window.

## Where to read more

- [Top-level README](../README.md) — what the orchestrator is, why it exists, install path
- [BOOTSTRAP.md](../BOOTSTRAP.md) — Path A (with launcher) vs Path B (clone-only) trade-offs
- [`docs/GETTING_STARTED.md`](../docs/GETTING_STARTED.md) — install and first-session walkthrough
- [`docs/CONFIGURATION.md`](../docs/CONFIGURATION.md) — env vars, ports, KG collection layout

## License

AGPL-3.0-or-later — same as the rest of the repository. See [`../LICENSE`](../LICENSE) and [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

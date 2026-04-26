# Installing the Orchestrator

The launcher's onboarding wizard installs the orchestrator into a folder
of your choice. Two flows are supported.

## Fresh install

If you point the wizard at an empty folder (or a folder that doesn't yet
exist), the launcher will:

1. `mkdir -p` the install path
2. `git clone https://github.com/VibeCoded-Tools/orchestrator.git <path>`
3. Run `python install.py` inside that path

This is the default for new users.

## Adopting an existing project

If you already have a `.claude/` directory in your project (e.g. from
previous Claude Code use), the orchestrator can adopt it without
clobbering your code.

How it works:

1. The launcher detects orchestrator-shaped artifacts at the install
   path. The detection checks for any of:
   `.claude/`, `CLAUDE.md`, `knowledge/`, `claude_mcp_servers/`,
   `state/`, `config/`, `docs/`, `templates/`, `tools/`,
   `infrastructure/`, `requirements.txt`, `requirements-dev.txt`,
   `install.sh`, `install.ps1`, `install.py`, `BOOTSTRAP.md`.
2. Before any write, the launcher calls the Rust `preview_install`
   command and shows a diff:
   - **Will overwrite (N orchestrator files):** files that already
     exist and would be replaced
   - **Will add (M new files):** files the bundle would create
3. Your code outside the orchestrator-managed allowlist is **never
   touched**. This is a hard whitelist, not a blacklist — the install
   refuses to write any path it doesn't recognize.
4. On confirm, the launcher clones the orchestrator into a sibling
   scratch directory and copies only the allowlist paths over your
   project. Anything outside the allowlist (your `src/`, `tests/`,
   `pyproject.toml`, `.env`, etc.) stays exactly as it was.

This mirrors the behavior of `npm install` in an existing project: it
adds tooling without rewriting your source.

## Hard whitelist (what the install will touch)

```
.claude/                  knowledge/              state/
CLAUDE.md                 claude_mcp_servers/     config/
docs/                     templates/              tools/
infrastructure/           requirements.txt        requirements-dev.txt
install.sh                install.ps1             install.py
BOOTSTRAP.md
```

Anything else is treated as **user code** and left alone.

## CLI equivalent (advanced)

The Tauri commands `preview_install` and `install_orchestrator` are
mirrored on the hub CLI; see `launcher/src-tauri/src/hub/cli_api.rs` for
the JSON-RPC surface.

## Troubleshooting

* **"install_path already contains orchestrator files"** — adopt mode
  was detected but `confirm_overwrite=true` was not passed. The wizard
  shows a confirm modal automatically; if you're scripting the call,
  pass `confirm_overwrite: true` after you've reviewed the diff.
* **"already contains files but is not a git checkout and has no
  orchestrator artifacts"** — the path has user files but no
  `.claude/`. Pick an empty folder, or run `git init` first if you want
  to adopt-prep manually.
* **"Cannot create install directory"** — usually a permissions
  problem on the parent. Pick a path under `$HOME` or chown the
  parent directory.

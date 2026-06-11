# Migrating Secrets to VCT Secrets

This guide helps you move your API keys and tokens from scattered `.env`
files into the centralized, permission-hardened `~/.vct-secrets/` layout.

> **Scope note (v0.2.54)**: this migration is a **manual** workflow. The
> VCT Launcher does NOT perform it for you — launcher-managed secrets
> (`github_pat`, `openai_api_key`) live in the **OS keychain** and are
> written via the launcher GUI (OnboardingWizard / Preferences → Special
> Secrets), then resolved through vct-hub. The `~/.vct-secrets/` file
> store described here is the launcher-independent path. See
> `tools/vct-secrets/README.md` §"Two stores, one mental model".

## The new layout

```
~/.vct-secrets/                (mode 700)
├── shared/                    — secrets used by any project
│   └── github_pat
└── projects/
    ├── my-app/                — secrets scoped to one project
    │   └── openai_api_key
    └── another-app/
        └── stripe_secret_key
```

Resolution order: `projects/<NAME>/<key>` → `shared/<key>` → fail.

So you can keep one `github_pat` in `shared/` and let multiple projects use
it. If a project needs a different `github_pat`, drop it under
`projects/<NAME>/` and the project-scoped one wins.

## Step 1 — Initialize the layout

```bash
mkdir -p ~/.vct-secrets/{shared,projects}
chmod 700 ~/.vct-secrets
cp tools/vct-secrets/vct                 ~/.vct-secrets/vct
cp tools/vct-secrets/git-credential-vct  ~/.vct-secrets/git-credential-vct
chmod 755 ~/.vct-secrets/vct ~/.vct-secrets/git-credential-vct
export PATH="$HOME/.vct-secrets:$PATH"   # add to your shell rc
```

## Step 2 — Migrate one `.env` at a time

Find your project's `.env`. Run the dry-run first:

```bash
vct migrate-from-env /path/to/your/project/.env --project my-project --dry-run
```

The dry-run prints what *would* be moved, without writing. Sanity-check the
list — drop anything that's not a real secret (e.g. `NODE_ENV=production`
shouldn't be migrated, `DATABASE_URL` containing a password should).

Once the list looks right:

```bash
vct migrate-from-env /path/to/your/project/.env --project my-project
```

This writes each `KEY=value` pair as a file at
`~/.vct-secrets/projects/my-project/<key>` (mode 600), and renames the
original `.env` to `.env.migrated` for safe-keeping.

## Step 3 — Wire your tools to read from vct

For tools you launch yourself (CLI, scripts):

```bash
vct exec --project my-project --secret openai_api_key=OPENAI_API_KEY -- \
  python my-script.py
```

For MCP servers and other long-running processes, write a wrapper script
that calls `vct exec` and `exec`'s the real binary with the env populated.
Example pattern:

```bash
#!/usr/bin/env bash
# ~/.local/bin/openai-mcp-wrapper.sh
exec vct exec --project my-project \
  --secret openai_api_key=OPENAI_API_KEY -- \
  python -m openai_mcp.server "$@"
```

Then point Claude Code (or whoever launches the MCP) at the wrapper, not
the raw binary.

For `git push`/`fetch` to GitHub, register the credential helper (manual
step — the launcher does not register it):

```bash
git config --global credential.https://github.com.helper "!$HOME/.vct-secrets/git-credential-vct"
```

It reads `projects/<name>/github_pat` (project detected via a
`.vct-project` marker file, or `$VCT_PROJECT_ROOT_PATTERN` — default
`$HOME/dev`) and falls back to `shared/github_pat`, handing it to git.
No `~/.git-credentials` file required.

## Step 4 — Verify and clean up

```bash
vct doctor                  # check perms, list any anomalies
vct list                    # show all (project, key) pairs you have
```

Once you've confirmed everything works (run a script that needs the
secret, do a `git push`, etc.), delete the `.env.migrated` files:

```bash
rm /path/to/your/project/.env.migrated
```

## Common pitfalls

- **`.env` had quoted values** (`KEY="value with spaces"`): `vct
  migrate-from-env` strips matching quote pairs. If your value genuinely
  starts and ends with a quote character, set it manually with `vct set`.
- **You see "permissions" errors after migration**: run `vct doctor` — it
  fixes file modes. If it still warns, check the parent directory mode is
  700.
- **Wrapper scripts can't find the secret**: confirm with
  `vct get --project my-project --key <name> --trusted`. If that prints
  nothing, the secret is at a different project name or got migrated to
  `shared/`. Check `vct list`.

## Optional: legacy flat-file layout

Older versions used `~/.vct-secrets/<key>` (flat). If you have such
secrets, run the bundled migration:

```bash
bash tools/vct-secrets/migrate-shared.sh --dry-run
bash tools/vct-secrets/migrate-shared.sh
```

It moves each flat `<key>` to `shared/<key>`. The fallback resolution path
still works during transition — flat files are read if shared/ doesn't
have the key — so this migration is non-blocking.

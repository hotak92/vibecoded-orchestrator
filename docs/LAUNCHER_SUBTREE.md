# Launcher Subtree

The `launcher/` directory is a **git subtree** of the [VCT-Launcher](https://github.com/pb992/VCT-Launcher) repo, branch `feature/orchestrator-hub`. It is bundled into this repo so that users get the launcher source alongside the orchestrator with a single `git clone`.

## Why subtree (not submodule)?

- `git clone` works without `--recursive`
- No "did you remember to `git submodule update`" footgun for OSS contributors
- Launcher development continues independently in `pb992/VCT-Launcher`; we sync periodically

## Pulling upstream launcher updates

When the launcher has new commits on `feature/orchestrator-hub` that should land here:

```bash
# One-time setup (already done): the remote is named `vct-launcher`
# git remote add vct-launcher https://github.com/pb992/VCT-Launcher.git

# Pull and squash the latest launcher changes into launcher/
git fetch vct-launcher feature/orchestrator-hub
git subtree pull --prefix=launcher vct-launcher feature/orchestrator-hub --squash
```

Resolve any merge conflicts inside `launcher/*` and commit. The squash keeps history readable here while the full launcher history lives upstream.

## Pushing changes back to the launcher (rare)

If a fix is made to `launcher/*` directly in this repo and needs to flow back upstream:

```bash
git subtree push --prefix=launcher vct-launcher feature/orchestrator-hub
```

This pushes only the `launcher/` history to the VCT-Launcher repo's branch. Prefer making the change in the VCT-Launcher repo directly when possible — it's cleaner.

## Don't edit `launcher/` casually

The launcher is its own product with its own CI, tests, and release cadence. Edits should usually originate in `pb992/VCT-Launcher` and flow into this repo via `git subtree pull`. Direct edits here create a divergence that has to be reconciled.

## Why this layout

- Users `git clone https://github.com/hotak92/vibecoded-orchestrator.git` and have everything (orchestrator + launcher source)
- Fixed relative path: `<repo>/launcher/` always exists. `install.py`, packaging scripts, and docs can rely on this.
- The launcher's `src-tauri/` Rust crate, `src/` Svelte UI, `supabase/` migrations are all here.

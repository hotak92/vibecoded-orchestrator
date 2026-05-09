# Launcher Subtree

The `launcher/` directory is a git subtree of the [VCT-Launcher](https://github.com/pb992/VCT-Launcher) repo, branch `feature/orchestrator-hub`. Bundling as a subtree means a single `git clone` gives users the launcher source alongside the orchestrator — no `git submodule update` step.

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

## Pushing changes back to the launcher

If a fix is made to `launcher/*` directly in this repo and needs to flow back upstream:

```bash
git subtree push --prefix=launcher vct-launcher feature/orchestrator-hub
```

This pushes only the `launcher/` history to the VCT-Launcher repo's branch. Prefer making the change in the VCT-Launcher repo directly when possible — it's cleaner.

## Editing `launcher/` directly

Edits should originate in `pb992/VCT-Launcher` and flow here via `git subtree pull`. Direct edits in this repo are acceptable; reconcile by pushing back upstream after each release.

## Layout invariants

- One `git clone https://github.com/hotak92/vibecoded-orchestrator.git` gets users orchestrator + launcher source together.
- Fixed relative path: `<repo>/launcher/` always exists, so `install.py`, packaging scripts, and docs can rely on it.
- The launcher's `src-tauri/` Rust crate, `src/` Svelte UI, and `supabase/` migrations all live there.

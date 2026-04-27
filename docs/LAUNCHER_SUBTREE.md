# Launcher Subtree

The `launcher/` directory is a git subtree of the [VCT-Launcher](https://github.com/pb992/VCT-Launcher) repo, branch `feature/orchestrator-hub`. It's bundled here so a single `git clone` gives users the launcher source alongside the orchestrator.

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

## Active source-of-truth note (as of v0.1.0)

During the v1.0 build-out, `vibecoded-orchestrator/launcher/` is ahead of `pb992/VCT-Launcher`
on several fronts (five-screen critical path, packaging decisions, first-install integration).
`pb992/VCT-Launcher` is the **upstream-of-record** for the launcher product, but it currently
lags the subtree here. The plan is to push the v1.0 delta back upstream after launch via
`git subtree push` and then resume the normal pull-first workflow. Do not treat `pb992/VCT-Launcher`
as the definitive reference for launcher behavior until that sync is complete.

## Don't edit `launcher/` casually

The launcher is its own product with its own CI, tests, and release cadence. Edits should originate in `pb992/VCT-Launcher` and flow here via `git subtree pull`. During the v1.0 sprint the rule is relaxed — edits in this repo are acceptable when iteration speed matters. Reconcile by pushing back upstream after the release.

## Why this layout

- One `git clone https://github.com/hotak92/vibecoded-orchestrator.git` gets users orchestrator + launcher source together.
- Fixed relative path: `<repo>/launcher/` always exists, so `install.py`, packaging scripts, and docs can rely on it.
- The launcher's `src-tauri/` Rust crate, `src/` Svelte UI, and `supabase/` migrations all live there.

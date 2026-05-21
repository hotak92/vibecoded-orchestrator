# Paid Module Dev Checklist

**Status**: normative — every paid module must follow this contract before its first public release and on every subsequent change.

## Why this doc exists

The Vibecoded Orchestrator is AGPL-3.0. Paid modules (`vct-rl-reranker`, future: `vct-coordination`, `MAO`, `telegram-module`) ship their source as **private** containers and the public AGPL repo carries only thin HTTP adapter clients. Mistakes in this boundary are catastrophic in three independent ways:

- **License**: private source landing in a public AGPL commit converts that source to AGPL by accidental publication. The author's commercial licensing of the paid module is then permanently compromised.
- **Trust**: paying customers of the paid tier expect their cost to buy something that is not freely available. A leak invalidates that.
- **IP**: training data, model weights, fine-tuning recipes, and proprietary algorithms are the unique value of the paid product. Once on GitHub, they are mirrored, forked, and indexed beyond recall.

The defenses today (`.gitignore` blocking `paid-modules/`, nested-repo isolation, `ci.yml :: launcher-leak-check`) are robust **only if** every new module respects the same conventions. This doc is the per-module pre-flight that ensures they do.

Historical source: the v0.2.22 private-leak-audit (internal — lives in VCO_dev). This doc codifies its Section E "Per-module dev checklist" as a permanent public contract.

---

## Repository boundary

Two distinct trees, one-way client→server boundary, never circular.

### Private side — paid module proper

- Path: `paid-modules/<module-id>/` inside the developer's VCO_dev clone.
- Git: **standalone nested git repo** with its own remote on a private GitHub repo (e.g. `git@github.com:hotak92/<module-id>.git`). NOT a submodule of VCO_dev. NOT tracked by VCO_dev's parent git tree.
- Contents: model code, training code, fine-tune scripts, model weights, the private `Dockerfile`, the private CI workflow, `vct-module.json` manifest.
- Container image: published to a private GHCR repo (e.g. `ghcr.io/hotak92/<module-id>`).

### Public side — AGPL adapter

- Path: `claude_mcp_servers/<module-id>_client/` inside the public AGPL repo (`<orchestrator-root>/`).
- Contents (whitelist — anything else is a smell):
  - `__init__.py`
  - `client.py` — HTTP client, calls the private container's REST/gRPC surface.
  - `schemas.py` — Pydantic wire contract; mirrors what the private container accepts/returns. May reference field names but **not** model internals.
  - `telemetry_writer.py` (optional) — emits anonymized usage events.
  - `rl_logger.py` or equivalent (optional) — local logging helper.
- Imports allowed: stdlib, `httpx`/`requests`, `pydantic`, project-shared utilities under `claude_mcp_servers/`. Forbidden: `torch`, `sklearn`, `transformers`, `tch::`, `burn::`, `candle`, any ML/RL framework.

### Direction of the boundary

The adapter (public) calls the paid module (private). The paid module never imports from the adapter. This guarantees the public code remains independently useful (free-tier installs still load it; calls just return a clear "module not configured" rather than crashing) and the private code can be developed/tested in isolation.

---

## Files that MUST NEVER appear in a public commit

These filename patterns are **never permitted** anywhere in the public AGPL repo's source tree (allowlisted exceptions: `claude_mcp_servers/<id>_client/`, `docs/`, `CHANGELOG.md`, `README.md` — name-references in docs and client wiring are fine; source files containing the identifiers as actual code or imports are not).

Canonical list (from the v0.2.22 leak-audit, extend as new modules land):

- `retrieval_rl.py`
- `rl_model.py`
- `rl_server.py`
- `offline_trainer.py`
- `coordination_server.py` *(reserved for future `vct-coordination` paid module)*
- `mao_engine.py` *(reserved for future MAO paid module)*
- `telegram_bot_runtime.py` *(reserved for future Telegram paid module)*

In addition, the following directories must not exist in any public-repo commit:

- `paid-modules/` — top-level directory containing private module clones.
- `claude_mcp_servers/<id>_server/` — historical private-server source location (defense-in-depth: keep the `.gitignore` entry even if the dir never materializes).

Both directories are listed in the public repo's `.gitignore`. Verify before adding any new module: a fresh `.gitignore` line should be added **before** the directory is ever populated locally.

---

## Adapter discipline

The public-side `claude_mcp_servers/<id>_client/` package is the only surface that talks to a paid module. It must:

1. **Be HTTP-only**. No shared library, no shared Python module, no shared on-disk artifact with the private container. Communication goes through a documented REST/gRPC endpoint exposed by the running container.
2. **Carry no embedded secrets**. Auth tokens, signed-URL endpoints, GHCR pull credentials are loaded at runtime from the per-project secrets store (`vct-hub` secrets API) or environment variables (`<MODULE>_SERVER_URL`, `<MODULE>_AUTH_TOKEN`). Never hardcoded as defaults that point at a private host.
3. **Degrade gracefully**. When the paid container's URL is unreachable or unset, the adapter must return a clear "module not configured" / "feature unavailable" path. Free-tier installs run without the paid container; nothing in the public code should assume the container exists.
4. **Reference the module by public name**, never by private-repo source filenames. Comments and docstrings cite the GHCR image (`ghcr.io/<owner>/<id>`) or the product name, not internal Python files.
5. **Not include test fixtures derived from private data**. If integration tests need a stub server, the stub is a public Python file with hand-crafted fixtures, not a slimmed copy of the private container's source.

---

## Manifest discipline

Each paid module has a `vct-module.json` manifest that the launcher consumes to install and run the module's container. Manifest leak surface is high (it carries the image ref, version pinning, and pull endpoint), so:

1. **Manifest lives ONLY in the private repo** at `paid-modules/<id>/vct-module.json`. It is fetched at install time from the private repo's release artifacts, not bundled into the public launcher.
2. **`launcher/bundled_manifests/` contains ONLY free-tier manifests**. The release workflow asserts this on every release (every bundled manifest must have `license.tier == "free"`). A paid-tier manifest accidentally bundled is the worst-case leak: it carries the private GHCR image ref + signed-URL endpoint.
3. **Container image tags must be immutable**. References to the private container in the manifest use:
   - A digest pin (`ghcr.io/<owner>/<id>@sha256:<digest>`), OR
   - A version-pinned tag matching a released semver (`ghcr.io/<owner>/<id>:v0.3.2`).
   - **Never `:latest`**. Mutable tags break reproducibility and make leak-time-of-check unreliable.
4. **GPU variants are enumerated, not auto-derived**. If the module ships `cpu`, `cuda`, `rocm` variants, each variant is its own digest/version pin in `manifest.runtime.gpu_image_variants` — see `knowledge/concepts/gpu-mode-decision-policy.md`.

---

## CI guard

The public repo's `.github/workflows/ci.yml` runs `launcher-leak-check`, which scans built launcher binaries for unexpected `/home/<user>/`, `/Users/<user>/`, and `C:\Users\<user>\` paths. The check is mirrored locally in `tests/test_launcher_leak_grep.py`.

For paid-module leak surface specifically, the same workflow is extended with a source-level filename guard (see the v0.2.22 leak-audit Section C for the canonical pattern):

- A `grep` job in `ci.yml` enumerates the forbidden filename patterns above and fails the build if any of them appear as actual code/import statements outside the allowlisted directories (`claude_mcp_servers/<id>_client/`, `docs/`, `CHANGELOG.md`, `README.md`).
- The `release.yml` workflow asserts on every release that `launcher/bundled_manifests/*.json` contain only `license.tier == "free"` entries — a non-free manifest in the public bundle fails the release.

When you add a new paid module, the relevant filename patterns from "Files that MUST NEVER appear in a public commit" must be added to the CI guard's `BAD_NAMES` regex in the same PR that introduces the module's adapter.

---

## When you add a new paid module

Follow this procedure in order. Each step is independently verifiable; do not skip any.

1. **Reserve the module id**. Pick a stable kebab-case identifier (`vct-coordination`, `mao`, `telegram-module`). This becomes the directory name in `paid-modules/<id>/`, the public adapter `claude_mcp_servers/<id>_client/`, the container image ref `ghcr.io/<owner>/<id>`, and the module-id string constant referenced from the launcher.
2. **Create the private repo first**. Initialize `paid-modules/<id>/` as a standalone git repo with its own private GitHub remote. Verify `git remote -v` shows the private URL, not VCO_dev's.
3. **Update `.gitignore` in BOTH repos** (public AGPL repo and VCO_dev) to add `claude_mcp_servers/<id>_server/` (defense-in-depth) — do this BEFORE writing any code.
4. **Scaffold the public adapter**. Create `claude_mcp_servers/<id>_client/` with the whitelist files only (`__init__.py`, `client.py`, `schemas.py`, optionally `telemetry_writer.py`). The adapter must work in graceful-degraded mode when the paid container is absent.
5. **Extend the CI leak-check**. Add the new module's forbidden filename patterns (e.g. `coordination_server.py`, `mao_engine.py`) to the `BAD_NAMES` regex in `.github/workflows/ci.yml`. Add an integration test under `tests/` that asserts the new adapter's no-server path returns the expected error.
6. **Keep the manifest private**. The `vct-module.json` for the new module stays at `paid-modules/<id>/vct-module.json`. Do NOT add a copy to `launcher/bundled_manifests/`.
7. **Write the public docs reference**. In CHANGELOG / README / feature docs, refer to the new module by its product name and GHCR image ref. Never reference internal Python filenames from the private repo.
8. **Run the leak-audit locally** before opening the public PR. Run the 5-line GREEN check from the v0.2.22 leak-audit (extend it with the new module's filenames):
   ```bash
   set -e
   test ! -d <orchestrator-root>/paid-modules || { echo "RED: paid-modules/ exists in public"; exit 1; }
   grep -q "^paid-modules/$" <orchestrator-root>/.gitignore || { echo "RED: paid-modules/ not gitignored"; exit 1; }
   ! find <orchestrator-root> -name "retrieval_rl.py" -o -name "rl_server.py" -o -name "offline_trainer.py" -o -name "rl_model.py" -o -name "<new-module-private-file>.py" | grep -q . || { echo "RED: private filenames in public"; exit 1; }
   git -C paid-modules/<id> remote -v | grep -q "<owner>/<id>" || { echo "RED: nested git remote drifted"; exit 1; }
   echo "GREEN"
   ```

---

## When you modify an existing paid module

A 5-bullet pre-commit sanity check before any commit that touches paid-module code or its public adapter:

1. **Are any new files in the public adapter? If yes, do they match the whitelist** (`__init__.py`, `client.py`, `schemas.py`, `telemetry_writer.py`, `<x>_logger.py`)? Anything else needs explicit review.
2. **Did any forbidden filename pattern land in the public tree?** Run `grep -rE "retrieval_rl|rl_model\.py|offline_trainer\.py|rl_server\.py|coordination_server\.py|mao_engine\.py|telegram_bot_runtime\.py" <orchestrator-root>/ --include="*.py" --include="*.rs" --include="*.ts" --include="*.svelte"` — every hit outside `claude_mcp_servers/<id>_client/`, `docs/`, `CHANGELOG.md`, `README.md` is a fail.
3. **Did the manifest move?** `<orchestrator-root>/launcher/bundled_manifests/` must contain only free-tier manifests. If the paid module's manifest is there, remove it.
4. **Are image tags still immutable?** Search the diff for `:latest`. If the manifest now references `:latest`, replace with a digest or version pin.
5. **Does the adapter still degrade gracefully?** Confirm the no-server path still returns a clean error (the simplest check: a unit test that boots the adapter with `<MODULE>_SERVER_URL=""` and asserts the documented error type, not a crash).

If any of the five fails, the commit does not ship until it's fixed.

---

## Cross-references

- **v0.2.22 private-leak-audit** (internal, lives in VCO_dev — not in this public repo) — the historical source for this checklist's Section E. Mentions specific findings from the `vct-rl-reranker` audit on 2026-05-20; the per-module dev checklist there is the direct ancestor of this doc.
- **`LICENSE`** (`<orchestrator-root>/LICENSE`) — AGPL-3.0-or-later. Every source file the orchestrator ships inherits this license; preserving the public/private split is what keeps paid-module source out of scope.
- **GTM plan, Phase 1.7** — license headers + `NOTICE` file workstream. Coordinated separately; the public adapter files must carry the AGPL header so a casual reader sees that the adapter ships under AGPL while the corresponding paid container does not.
- **`docs/REPO_CLEANLINESS.md`** — adjacent policy doc on the tracked / machine-local split. The principles overlap with this checklist's section on `.gitignore` discipline.
- **`docs/MAINTAINER_GUIDE.md`** — release-pipeline operations; the release workflow's free-tier-manifest assertion lives in scope of that doc.
- **`knowledge/concepts/launcher-paid-modules-schema.md`** — KG node describing the manifest schema. Documentation-only (no leak surface), but worth reading when scaffolding a new module's manifest.

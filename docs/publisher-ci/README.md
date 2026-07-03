# Paid-module publisher CI helpers

v0.2.33 (Agent F, C2) introduced two CI artifacts for paid-module publishers (`vct-rl-reranker`, future `vct-mao`, future paid modules). Both close the gap that broke v0.2.32 post-update validation — a launcher install of RL v0.2.7 showed the wrong version on the catalog tile because the manifest silently failed to parse, with no upstream gate to catch the drift. v0.2.73 (E-3) adds a third artifact — an L0 publish-ordering gate.

The three artifacts are independent — adopt any or all.

## 1. JSON Schema — `docs/schemas/vct-module.schema.json`

Generated from `launcher/src-tauri/vct-launcher-core/src/manifest.rs` via `schemars 0.8`. Validates publisher manifests at PR time against the launcher's actual deserialiser shape.

**Wiring example** (paid-module repo's `.github/workflows/release.yml`):

```yaml
jobs:
  validate-manifest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      # Download the published schema (pin to the launcher version you
      # target — the v0.2.33 schema lives in the v0.2.33 git tag).
      - name: Download schema
        run: |
          curl -sSL \
            https://raw.githubusercontent.com/<owner>/vibecoded-orchestrator/v0.2.33/docs/schemas/vct-module.schema.json \
            -o vct-module.schema.json
      - uses: actions/setup-node@v6
        with:
          node-version: "20"
      - name: Validate vct-module.json against schema
        run: |
          npx --yes ajv-cli@5 validate \
            -s vct-module.schema.json \
            -d vct-module.json \
            --strict false
```

Why `--strict false`: schemars 0.8 emits Draft-07 with some extension keywords (`default`, `examples`) that strict-mode ajv rejects on grounds unrelated to manifest validity. The semantics we care about — required fields, enum constraints, type tags — are unaffected.

**Schema regeneration discipline**: if you depend on a NEWER launcher (e.g. v0.2.34 adds a control kind), download THAT launcher's schema. The schema is version-pinned to the launcher tag.

## 2. Image-presence gate — `validate-manifest-in-image.sh`

POSIX shell script that asserts `/app/vct-module.json` is INSIDE the built container image. Catches the publisher mistake where the Dockerfile doesn't `COPY vct-module.json /app/` — manifest exists in the source repo but isn't shipped with the image. The launcher's post-install extract step (Agent C, L0b) would surface this as "manifest extraction failed" AFTER the user pulled, paid Pro tier, and watched their disk fill — a wretched UX to inflict.

**Wiring example** (paid-module repo's `.github/workflows/release.yml`, post-`docker build`):

```yaml
- name: Verify manifest is shipped in image
  run: |
    # Vendor the script (preferred — no live-fetch dependency).
    cp .github/scripts/validate-manifest-in-image.sh /tmp/v.sh
    chmod +x /tmp/v.sh
    /tmp/v.sh ghcr.io/${{ github.repository_owner }}/vct-rl-reranker:${{ github.ref_name }}-cpu
    /tmp/v.sh ghcr.io/${{ github.repository_owner }}/vct-rl-reranker:${{ github.ref_name }}-cuda
    /tmp/v.sh ghcr.io/${{ github.repository_owner }}/vct-rl-reranker:${{ github.ref_name }}-rocm
```

Or fetch live from the launcher repo (one-shot, no vendoring):

```yaml
- name: Verify manifest in image (live-fetch script)
  run: |
    curl -sSL \
      https://raw.githubusercontent.com/<owner>/vibecoded-orchestrator/v0.2.33/docs/publisher-ci/validate-manifest-in-image.sh \
      | sh -s -- ghcr.io/${{ github.repository_owner }}/vct-rl-reranker:${{ github.ref_name }}-cpu
```

**Bonus**: if `validate-manifest` is on PATH (built from `launcher/src-tauri/vct-launcher-core`), the script also schema-validates the extracted manifest. Most publisher CIs won't have it installed; presence-only is the primary gate.

**Runtime detection**: the script picks `docker` over `podman` if both are available. Override with `VCT_CONTAINER_RUNTIME=podman ./validate-manifest-in-image.sh ...`.

**Path override**: if your module ships the manifest somewhere other than `/app/vct-module.json` (don't — the launcher's extract step is hardcoded to `/app/`), override via `VCT_MANIFEST_PATH=/elsewhere/manifest.json`.

## 3. L0 ordering gate — `validate-l0-image-pullable.sh`

**The strict publish order (E-3):**

```
1. Build + PUSH the container image(s) to GHCR.
2. Republish the L0 catalog entry that REFERENCES those image tags.
3. Supabase serves the refreshed L0 entry to launchers.
```

This order is load-bearing but was previously enforced by nothing in code
(E-findings §E-3). If step 2 runs before step 1 — an honest CI mis-order,
or a silent push failure — the L0 catalog entry names an image tag that
does not exist yet. A Pro user then installs the module, passes tier
validation, receives a valid pull token, and `podman pull` 404s/401s
against the missing tag — **after** paying and starting the download. The
launcher's `synthesize_install_manifest_from_l0` guard only catches an
*empty* `install.container.image`; a NON-empty-but-not-yet-pushed tag sails
past it and fails at pull time.

`validate-l0-image-pullable.sh` closes the gap: run it **AFTER the GHCR
push and BEFORE the L0 republish**. It extracts `install.container.image`
from the L0 entry (jq or python3) and asserts every referenced tag is
pullable from the registry. It is **read-only** — `manifest inspect` (a
registry metadata query, no layer download) with a `pull` fallback; it
never pushes, tags, or mutates the registry.

**Wiring example** (paid-module repo's `.github/workflows/release.yml`):

```yaml
- name: Push image(s) to GHCR
  run: |
    docker push ghcr.io/${{ github.repository_owner }}/vct-rl-reranker:${{ github.ref_name }}-cpu
    # ...cuda / rocm variants...

# GATE: image must be live BEFORE the L0 catalog entry references it.
- name: Verify L0-referenced image is pullable (ordering gate)
  run: |
    cp .github/scripts/validate-l0-image-pullable.sh /tmp/g.sh
    chmod +x /tmp/g.sh
    # Either parse the L0 entry JSON you are about to publish:
    /tmp/g.sh l0-catalog/vct-rl-reranker.json
    # ...or assert specific tags directly:
    /tmp/g.sh --image \
      ghcr.io/${{ github.repository_owner }}/vct-rl-reranker:${{ github.ref_name }}-cpu \
      ghcr.io/${{ github.repository_owner }}/vct-rl-reranker:${{ github.ref_name }}-cuda

- name: Republish L0 catalog entry
  if: success()   # only after the gate confirms pullability
  run: ./scripts/republish-l0.sh
```

**Runtime detection + override**: same as the image-presence gate — prefers
`docker`, falls back to `podman`; override with `VCT_CONTAINER_RUNTIME`.

**Exit codes**: `0` all referenced tags pullable (safe to republish); `1` a
tag is not pullable (push not done / failed — do NOT republish); `2` usage
error or the L0 entry has an empty/absent `install.container.image`.

## What this REPLACES

Pre-v0.2.33: publishers had no way to validate their manifest other than running the launcher locally + opening the Modules tab + reading the parsed log file. v0.2.32's RL v0.2.7 shipped a `tauri_command` step kind that the launcher didn't know about; the parse silently failed; the catalog showed v0.1.1 for weeks before the user noticed. Neither the publisher's CI nor the launcher's CI caught it — both consumed the manifest, both rejected silently, neither propagated the rejection to a human-visible signal.

v0.2.33's two artifacts close both gaps:
- Schema gate catches type-level drift at the publisher's PR time.
- Image-presence gate catches "you forgot the Dockerfile COPY".

The launcher-side CI (`.github/workflows/manifest-validate.yml`) catches the *launcher*'s side of the same problem — if a launcher PR changes the schema without updating the committed JSON Schema artifact, the launcher's own CI fails. The two gates run on different repos but reference the same schema document, keeping launcher + publishers in lockstep.

## Pinning + version drift

The schema is COMMITTED to the launcher repo and updated atomically with `manifest.rs` (the launcher's CI enforces this — see `exported_schema_matches_committed_copy`). Publishers should pin to a launcher tag, not `main`:

```yaml
curl -sSL https://raw.githubusercontent.com/<owner>/vibecoded-orchestrator/v0.2.33/docs/schemas/vct-module.schema.json
#                                                                       ^^^^^^^ pin to a tag, not @main
```

When a publisher bumps to a newer launcher version (e.g. their manifest starts using a v0.2.34-only control kind), update the curl URL to the new launcher tag in the same PR. CI then validates against the new schema — no surprise schema drift.

## Troubleshooting

- **"manifest schema validates" but launcher still rejects** — strict-mode regression somewhere. Run the launcher with `RUST_LOG=debug VCT_LAUNCHER_STRICT_MANIFEST=1` and look for the deserialize error in stderr.
- **`docker cp` returns "no such file"** — your Dockerfile is missing `COPY vct-module.json /app/`. Add it after the working-dir setup.
- **Script says "neither docker nor podman is on PATH"** on GH Actions — `ubuntu-latest` runners have docker; if you're using a custom container, install one or set `VCT_CONTAINER_RUNTIME` explicitly to a binary on PATH.
- **`ajv-cli` rejects `examples` keyword** — pass `--strict false` (already shown above). The relevant validation paths aren't affected.

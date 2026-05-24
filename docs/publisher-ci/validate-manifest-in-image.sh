#!/usr/bin/env sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# v0.2.33 (Agent F, C2): publisher-side CI helper.
#
# Asserts that a paid-module container image (e.g.
# ghcr.io/hotak92/vct-rl-reranker:0.2.7-cpu) ships /app/vct-module.json
# inside the image. The launcher's post-install extract step (Agent C,
# L0b) runs `podman cp /app/vct-module.json` against every newly-pulled
# image — if the file isn't there, the install fails with a confusing
# "manifest extraction failed" toast at the END of the pull, AFTER the
# user paid Pro tier and started downloading.
#
# Far better to catch the omission at publish time. This script gives
# paid-module publishers a one-liner to wire into their release CI:
#
#   - name: Verify manifest shipped in image
#     run: |
#       curl -sSL https://raw.githubusercontent.com/.../publisher-ci/validate-manifest-in-image.sh \
#         | sh -s -- ghcr.io/${{ github.repository_owner }}/vct-rl-reranker:${{ github.ref_name }}-cpu
#
# (Or vendor the script into the publisher repo; both work.)
#
# Optionally validates the extracted manifest's schema by piping it
# through `validate-manifest` if that binary is on PATH (built from the
# launcher repo). When the binary isn't available, the script only
# checks file presence — still catches the bulk of breakage.
#
# Exit codes:
#   0 — manifest present (and, if validate-manifest on PATH, valid).
#   1 — manifest missing OR fails schema validation.
#   2 — usage error.

set -eu

if [ "$#" -lt 1 ]; then
    # Single-quoted heredoc — prevents the shell from expanding
    # backticks (which would try to RUN `validate-manifest` from the
    # docstring) and variable references in the usage text.
    cat >&2 <<'EOF'
usage: validate-manifest-in-image.sh <image:tag>

Asserts that <image:tag> ships /app/vct-module.json. Bonus schema
validation via `validate-manifest` if that binary is on PATH.

Optional env:
  VCT_CONTAINER_RUNTIME=docker|podman   override runtime detection
  VCT_MANIFEST_PATH=/app/vct-module.json  override the in-image path

Example:
  ./validate-manifest-in-image.sh ghcr.io/hotak92/vct-rl-reranker:0.2.7-cpu
EOF
    exit 2
fi

IMAGE="$1"
MANIFEST_PATH="${VCT_MANIFEST_PATH:-/app/vct-module.json}"

# Pick a runtime. Prefer docker (most common in GitHub Actions runners),
# fall back to podman. The actual `cp` semantics are identical on both.
if [ -n "${VCT_CONTAINER_RUNTIME:-}" ]; then
    RT="$VCT_CONTAINER_RUNTIME"
elif command -v docker >/dev/null 2>&1; then
    RT="docker"
elif command -v podman >/dev/null 2>&1; then
    RT="podman"
else
    echo "::error::neither docker nor podman is on PATH"
    echo "         install one OR set VCT_CONTAINER_RUNTIME explicitly"
    exit 1
fi

echo "Using runtime: $RT"
echo "Validating manifest presence at $MANIFEST_PATH inside $IMAGE..."

# Pull the image first if it isn't local. Soft-fail and let `create`
# surface a clearer error; users may be testing local images.
if ! "$RT" image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Image not local; pulling..."
    if ! "$RT" pull "$IMAGE"; then
        echo "::error::failed to pull $IMAGE — check tag + registry auth"
        exit 1
    fi
fi

# Create a stopped container so we can `cp` out of it without running
# anything inside the image. Cheap and side-effect-free.
CID=$("$RT" create "$IMAGE")
if [ -z "$CID" ]; then
    echo "::error::failed to create container from $IMAGE"
    exit 1
fi

# Always clean up the stopped container, even on error.
cleanup() {
    "$RT" rm "$CID" >/dev/null 2>&1 || true
    rm -f "${TMPFILE:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

TMPFILE="$(mktemp -t vct-manifest-XXXXXX.json)"

# Try to copy the manifest out. `docker cp` / `podman cp` exit non-zero
# when the source path doesn't exist inside the container, which is
# exactly the signal we want.
if "$RT" cp "$CID:$MANIFEST_PATH" "$TMPFILE" 2>/dev/null; then
    echo "[OK] $MANIFEST_PATH present in $IMAGE"
else
    cat >&2 <<EOF
::error::$MANIFEST_PATH NOT present in $IMAGE
         Paid modules MUST ship their vct-module.json at /app/ so the
         launcher's post-install extract step can write it to
         ~/.vct/modules/<id>/vct-module.json. Without this file, every
         install of this image fails AFTER the pull completes, which
         is a wretched UX.

         Add to your Dockerfile (paid-module repo):
             COPY vct-module.json /app/vct-module.json

         Then rebuild + republish the image.
EOF
    exit 1
fi

# Bonus: schema-validate the extracted manifest if `validate-manifest`
# is on PATH. Most publisher CIs won't have it — that's fine; presence-
# only check above is the primary gate.
if command -v validate-manifest >/dev/null 2>&1; then
    if VCT_LAUNCHER_STRICT_MANIFEST=1 validate-manifest "$TMPFILE"; then
        echo "[OK] manifest schema validates (strict mode)"
    else
        cat >&2 <<EOF
::error::manifest extracted from $IMAGE fails schema validation
         The launcher's strict-mode parser rejected the manifest. This
         means either (a) you bumped a field without updating the schema
         this launcher version knows, or (b) the manifest's JSON is
         genuinely malformed.

         Regenerate the schema from the launcher repo to see what's
         expected:
             cargo run -p vct-launcher-core --bin export-schema \\
                 --out docs/schemas/vct-module.schema.json
EOF
        exit 1
    fi
else
    echo "(validate-manifest not on PATH — skipping schema validation)"
    echo "    To enable: cargo install --path launcher/src-tauri/vct-launcher-core \\"
    echo "                  --bin validate-manifest"
fi

echo "All checks passed for $IMAGE"

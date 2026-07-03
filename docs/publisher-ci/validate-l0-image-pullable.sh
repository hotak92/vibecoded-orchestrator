#!/usr/bin/env sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# v0.2.73 (E-3): publisher-side CI ordering gate.
#
# THE INVARIANT THIS ENFORCES
# ---------------------------
# A paid module's release has a STRICT publish order:
#
#   1. Build + PUSH the container image(s) to GHCR.
#   2. Republish the L0 catalog entry that REFERENCES those image tags.
#   3. (Supabase serves the refreshed L0 entry to launchers.)
#
# If step 2 runs before step 1 (or step 1 silently fails), the L0 catalog
# entry names an image tag that does not yet exist. A Pro user then:
#   - installs the module, passes tier validation, gets a valid pull token,
#   - and `podman pull` 404s/401s against the missing tag —
# AFTER paying + starting the download. Blast radius: broken install UX for
# every Pro user until the publisher fixes ordering (E-findings §E-3).
#
# The pre-existing `validate-manifest-in-image.sh` proves an image ships
# its manifest, but it does NOT bind "the L0 entry's image is live before
# republish." This gate closes that: run it AFTER the GHCR push and BEFORE
# the L0 republish. It asserts every image tag referenced by the L0 entry
# is actually pullable from the registry.
#
# READ-ONLY: this script never pushes, tags, or mutates the registry. It
# uses `<runtime> manifest inspect` (a registry HEAD, no layer download)
# when available, falling back to a `pull`. CI-safe.
#
# USAGE
# -----
#   validate-l0-image-pullable.sh <l0-entry.json>
#       Extract install.container.image from the L0 entry JSON and assert
#       it is pullable. (jq preferred; python3 fallback; both optional.)
#   validate-l0-image-pullable.sh --image <image:tag> [<image:tag> ...]
#       Assert the given image ref(s) directly (no JSON parsing).
#
# Exit codes:
#   0 — every referenced image tag is pullable.
#   1 — an image tag is NOT pullable (ordering violation OR push failed).
#   2 — usage error / could not extract an image from the L0 entry.
#
# Optional env:
#   VCT_CONTAINER_RUNTIME=docker|podman   override runtime detection.

set -eu

usage() {
    cat >&2 <<'EOF'
usage:
  validate-l0-image-pullable.sh <l0-entry.json>
  validate-l0-image-pullable.sh --image <image:tag> [<image:tag> ...]

Asserts that the container image tag(s) referenced by an L0 catalog entry
are pullable from the registry. Run this AFTER the GHCR push and BEFORE the
L0 catalog republish, to enforce the strict publish order (image live
before the catalog entry points at it).

Optional env:
  VCT_CONTAINER_RUNTIME=docker|podman   override runtime detection
EOF
    exit 2
}

[ "$#" -lt 1 ] && usage

# ── Collect the image refs to check ─────────────────────────────────────
IMAGES=""

if [ "$1" = "--image" ]; then
    shift
    [ "$#" -lt 1 ] && usage
    IMAGES="$*"
else
    L0_JSON="$1"
    if [ ! -f "$L0_JSON" ]; then
        echo "::error::L0 entry file not found: $L0_JSON" >&2
        exit 2
    fi
    # Extract install.container.image. Prefer jq; fall back to python3.
    if command -v jq >/dev/null 2>&1; then
        IMAGES=$(jq -r '.install.container.image // empty' "$L0_JSON" 2>/dev/null || true)
    elif command -v python3 >/dev/null 2>&1; then
        IMAGES=$(python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        d = json.load(fh)
    img = d.get("install", {}).get("container", {}).get("image", "")
    if img:
        print(img)
except Exception:
    pass
' "$L0_JSON" 2>/dev/null || true)
    else
        echo "::error::need jq or python3 to parse the L0 entry JSON" >&2
        echo "         (or pass --image <image:tag> to skip parsing)" >&2
        exit 2
    fi

    if [ -z "$IMAGES" ]; then
        echo "::error::install.container.image is empty/absent in $L0_JSON" >&2
        echo "         The L0 entry references no image tag — nothing to" >&2
        echo "         pull. This is the same emptiness the launcher's" >&2
        echo "         synthesize_install_manifest_from_l0 guard rejects at" >&2
        echo "         install time; fix the L0 entry before republishing." >&2
        exit 2
    fi
fi

# ── Pick a runtime ──────────────────────────────────────────────────────
if [ -n "${VCT_CONTAINER_RUNTIME:-}" ]; then
    RT="$VCT_CONTAINER_RUNTIME"
elif command -v docker >/dev/null 2>&1; then
    RT="docker"
elif command -v podman >/dev/null 2>&1; then
    RT="podman"
else
    echo "::error::neither docker nor podman is on PATH" >&2
    echo "         install one OR set VCT_CONTAINER_RUNTIME explicitly" >&2
    exit 1
fi

echo "Using runtime: $RT"

# ── Assert pullability, read-only, per image ────────────────────────────
# `manifest inspect` is a registry metadata query (no layer download) and
# is the cheapest positive proof the tag exists + is pullable. Not every
# runtime/version exposes it, so fall back to a real `pull` when it's
# unavailable. Either way we never push or mutate the registry.
image_pullable() {
    img="$1"
    if "$RT" manifest inspect "$img" >/dev/null 2>&1; then
        return 0
    fi
    # `manifest inspect` may be unsupported OR the tag may genuinely be
    # missing. Distinguish by attempting a pull (still read-only wrt the
    # registry). A pull failure is the authoritative "not pullable".
    if "$RT" pull "$img" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

rc=0
for img in $IMAGES; do
    printf 'Checking pullability: %s ... ' "$img"
    if image_pullable "$img"; then
        echo "OK"
    else
        echo "NOT PULLABLE"
        cat >&2 <<EOF
::error::image tag not pullable: $img
         The L0 catalog entry references this tag, but it is not live in
         the registry. Either the GHCR push has not run yet (ordering
         violation — republish the L0 entry AFTER the push) or the push
         failed. Do NOT republish the L0 catalog entry until every image
         it references is pullable.
EOF
        rc=1
    fi
done

if [ "$rc" -eq 0 ]; then
    echo "All L0-referenced image tags are pullable — safe to republish the catalog entry."
fi
exit "$rc"

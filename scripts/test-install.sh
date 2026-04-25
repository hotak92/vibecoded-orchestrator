#!/usr/bin/env bash
# Opt-in container-based smoke test of install.py.
#
# Builds a clean ubuntu:22.04 container, copies the repo in, runs
# install.py with --no-containers --skip-models --no-joern --no-agents
# --no-skills (so we exercise the Python install path without pulling
# images / models / agents). Useful for catching package-manager-only
# bugs in install.py and requirements.txt resolution.
#
# Requires: docker OR podman in PATH.
# Network: yes (apt + pip).
# Time: ~3-5 min.
#
# Usage:  scripts/test-install.sh
#         RUNTIME=docker scripts/test-install.sh    # force docker
#         IMAGE=ubuntu:24.04 scripts/test-install.sh
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-ubuntu:22.04}"
RUNTIME="${RUNTIME:-}"

if [ -z "$RUNTIME" ]; then
    if command -v podman >/dev/null 2>&1; then
        RUNTIME=podman
    elif command -v docker >/dev/null 2>&1; then
        RUNTIME=docker
    else
        echo "ERROR: neither podman nor docker found in PATH" >&2
        exit 1
    fi
fi

echo "Using runtime: $RUNTIME"
echo "Using image:   $IMAGE"

# Generate a small bootstrap script the container will run.
read -r -d '' bootstrap <<'EOF' || true
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-pip ca-certificates curl >/dev/null
cd /work
echo
echo "=== python3 --version ==="
python3 --version
echo
echo "=== install.py --help ==="
python3 install.py --help | head -5
echo
echo "=== install.py (dry-run-ish) ==="
# We can't run the full installer (no podman/docker inside the container).
# Skip every step that needs network beyond pip + every step that touches
# a container daemon.
python3 install.py --no-containers --skip-models --no-joern --no-agents --no-skills
echo
echo "=== venv created? ==="
ls -la /work/.venv/bin/python || (echo "MISSING"; exit 1)
echo
echo "=== pip list (head) ==="
/work/.venv/bin/pip list 2>/dev/null | head -10
echo
echo "=== .env created? ==="
test -f /work/.env && head -10 /work/.env || (echo "MISSING"; exit 1)
echo
echo "All smoke-test steps passed."
EOF

# Mount the repo read-only? No — install.py writes .venv/.env into the cwd.
# Run on a copy by mounting a tmpfs overlay. Easiest: copy via a build-less run.
"$RUNTIME" run --rm -v "$PWD:/src:ro" -w /work "$IMAGE" bash -c "
    cp -a /src/. /work/
    chmod -R u+w /work
    $bootstrap
"

#!/usr/bin/env bash
# Candidate-only nginx/API and native Chromium contract fixtures.
set -euo pipefail

SMOKE_IMAGE="${1:?usage: edge-trust-smoke.sh IMAGE}"
SMOKE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
docker run --rm --network none --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=128m,uid=101,gid=102,mode=700 \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --pids-limit 128 --memory 512m \
    --mount "type=bind,src=${SMOKE_ROOT}/tests,dst=/fixture,readonly" \
    --entrypoint python "$SMOKE_IMAGE" /fixture/integration/edge_trust_smoke.py
node "$SMOKE_ROOT/tests/integration/edge_trust_browser.cjs" "$SMOKE_IMAGE"

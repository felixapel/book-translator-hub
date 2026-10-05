#!/usr/bin/env bash
# Exercise the managed hub lifecycle from caller-owned fresh data through a
# retained UID-101 reinstall against a real Docker daemon.
set -euo pipefail

SMOKE_IMAGE="${1:?usage: hub-btctl-lifecycle-smoke.sh IMAGE PREFIX}"
SMOKE_PREFIX="${2:?usage: hub-btctl-lifecycle-smoke.sh IMAGE PREFIX}"
[[ "$SMOKE_PREFIX" =~ ^[a-z0-9][a-z0-9.-]{0,48}$ ]] || exit 64
if [ "${BT_SMOKE_VALIDATE_PREFIX_ONLY:-0}" = 1 ]; then
    exit 0
fi

ROOT_DIR="$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bt-hub-lifecycle.XXXXXX")"
STATE_DIR="$WORK_DIR/state"
DATA_DIR="$WORK_DIR/data"
BACKUP_DIR="$WORK_DIR/backup"
ENV_FILE="$WORK_DIR/hub.env"
CWA_NETWORK="${SMOKE_PREFIX}-cwa-net"
KAVITA_NETWORK="${SMOKE_PREFIX}-kavita-net"
CWA_CONTAINER="${SMOKE_PREFIX}-cwa"
KAVITA_CONTAINER="${SMOKE_PREFIX}-kavita"
HUB_CONTAINER="${SMOKE_PREFIX}-managed"
CWA_IMAGE="${SMOKE_PREFIX}-cwa-fixture:4.0.6"
KAVITA_IMAGE="${SMOKE_PREFIX}-kavita-fixture:0.9.0.2"
VERSION="$(tr -d '\r\n' <"$ROOT_DIR/VERSION")"
REVISION="$(git -C "$ROOT_DIR" rev-parse HEAD)"
MANAGED_IMAGE="local/book-translator-hub:${VERSION}-${REVISION:0:12}"
managed_image_preexisting=0

# Hub image tags are commit-scoped rather than run-scoped. Serialize this
# smoke for one exact commit so cleanup cannot untag an image another run is
# preparing to use on the same shared Docker host.
exec 9>"${TMPDIR:-/tmp}/book-translator-hub-${REVISION}.lock"
flock 9
if docker image inspect "$MANAGED_IMAGE" >/dev/null 2>&1; then
    managed_image_preexisting=1
fi

cleanup() {
    local status=$?
    trap - EXIT HUP INT TERM
    set +e
    docker rm -f -v "$HUB_CONTAINER" "$CWA_CONTAINER" "$KAVITA_CONTAINER" \
        >/dev/null 2>&1 || true
    docker network rm "$CWA_NETWORK" "$KAVITA_NETWORK" >/dev/null 2>&1 || true
    docker image rm "$CWA_IMAGE" "$KAVITA_IMAGE" >/dev/null 2>&1 || true
    if [ "$managed_image_preexisting" -eq 0 ]; then
        docker image rm "$MANAGED_IMAGE" >/dev/null 2>&1 || true
    fi
    if [ -d "$WORK_DIR" ]; then
        docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
            --mount "type=bind,src=${WORK_DIR},dst=/cleanup" \
            "$SMOKE_IMAGE" -ec 'chown -R "$1:$2" /cleanup; chmod -R u+rwX /cleanup' \
            sh "$(id -u)" "$(id -g)" >/dev/null 2>&1 || true
        rm -rf -- "$WORK_DIR"
    fi
    exit "$status"
}
trap cleanup EXIT HUP INT TERM
mkdir -m 0700 "$STATE_DIR" "$DATA_DIR" "$BACKUP_DIR"

free_port() {
    python3 - <<'PY'
import socket
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
}

CWA_PORT="$(free_port)"
KAVITA_PORT="$(free_port)"
docker network create "$CWA_NETWORK" >/dev/null
docker network create "$KAVITA_NETWORK" >/dev/null
docker tag "$SMOKE_IMAGE" "$CWA_IMAGE"
docker tag "$SMOKE_IMAGE" "$KAVITA_IMAGE"
docker run -d --name "$CWA_CONTAINER" --network "$CWA_NETWORK" \
    --read-only --tmpfs /tmp \
    --label org.opencontainers.image.version=4.0.6 \
    --mount "type=bind,src=${ROOT_DIR}/tests/python/test_cwa_strong_fixture.py,dst=/fixture.py,readonly" \
    --entrypoint python "$CWA_IMAGE" /fixture.py >/dev/null
docker run -d --name "$KAVITA_CONTAINER" --network "$KAVITA_NETWORK" \
    --read-only --tmpfs /tmp \
    --label org.opencontainers.image.version=0.9.0.2 \
    --mount "type=bind,src=${ROOT_DIR}/tests/python/test_kavita_auth_fixture.py,dst=/fixture.py,readonly" \
    --entrypoint python "$KAVITA_IMAGE" /fixture.py >/dev/null

for _ in $(seq 1 30); do
    if docker exec "$CWA_CONTAINER" python -c \
        'import socket; socket.create_connection(("127.0.0.1",8083),1).close()' \
        >/dev/null 2>&1 \
        && docker exec "$KAVITA_CONTAINER" python -c \
        'import socket; socket.create_connection(("127.0.0.1",5000),1).close()' \
        >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
docker exec "$CWA_CONTAINER" python -c \
    'import socket; socket.create_connection(("127.0.0.1",8083),1).close()'
docker exec "$KAVITA_CONTAINER" python -c \
    'import socket; socket.create_connection(("127.0.0.1",5000),1).close()'

{
    printf 'BT_TOPOLOGY=hub\n'
    printf 'BT_INSTALL_PROFILE=compose-existing\n'
    printf 'BT_INSTALL_NAME=%s\n' "$HUB_CONTAINER"
    printf 'BT_STATE_DIR=%s\n' "$STATE_DIR"
    printf 'BT_DATA_DIR=%s\n' "$DATA_DIR"
    printf 'BT_BACKUP_DIR=%s\n' "$BACKUP_DIR"
    printf 'BT_ENABLE_CWA=true\n'
    printf 'BT_CWA_PUBLIC_ORIGIN=http://127.0.0.1:%s\n' "$CWA_PORT"
    printf 'BT_CWA_READER_UPSTREAM=http://%s:8083\n' "$CWA_CONTAINER"
    printf 'BT_CWA_READER_CONTAINER=%s\n' "$CWA_CONTAINER"
    printf 'BT_CWA_READER_NETWORK=%s\n' "$CWA_NETWORK"
    printf 'BT_CWA_READER_VERSION=4.0.6\n'
    printf 'BT_CWA_AUTH_PROFILE=reader-session\n'
    printf 'BT_CWA_READER_CONNECTOR_ID=01234567-89ab-4cde-8123-0123456789ab\n'
    printf 'BT_CWA_PUBLISHED_PORT=%s\n' "$CWA_PORT"
    printf 'BT_ENABLE_KAVITA=true\n'
    printf 'BT_KAVITA_PUBLIC_ORIGIN=http://127.0.0.1:%s\n' "$KAVITA_PORT"
    printf 'BT_KAVITA_READER_UPSTREAM=http://%s:5000\n' "$KAVITA_CONTAINER"
    printf 'BT_KAVITA_READER_CONTAINER=%s\n' "$KAVITA_CONTAINER"
    printf 'BT_KAVITA_READER_NETWORK=%s\n' "$KAVITA_NETWORK"
    printf 'BT_KAVITA_READER_VERSION=0.9.0.2\n'
    printf 'BT_KAVITA_AUTH_PROFILE=reader-session\n'
    printf 'BT_KAVITA_READER_CONNECTOR_ID=11234567-89ab-4cde-8123-0123456789ab\n'
    printf 'BT_KAVITA_PUBLISHED_PORT=%s\n' "$KAVITA_PORT"
    printf 'LLM_PROVIDER=local\n'
    printf 'LLM_MODEL=smoke-model\n'
    printf 'LLM_API_KEY=\n'
    printf 'BT_LOCAL_URL=http://host.docker.internal:2819/v1/chat/completions\n'
    printf 'BT_MAX_CONCURRENT=2\n'
    printf 'BT_MAX_UPSTREAM_INFLIGHT=2\n'
} >"$ENV_FILE"
chmod 0600 "$ENV_FILE"

assert_doctor() {
    "$ROOT_DIR/btctl" --repository "$ROOT_DIR" doctor --env "$ENV_FILE" --json \
        | python3 -c 'import json,sys; r=json.load(sys.stdin); assert r["ok"] and all(c["status"]=="ok" for c in r["checks"]),r'
}

# This is deliberately caller-owned mode 0700. UID 101 cannot traverse it
# until the lifecycle normalizes it, which reproduces the fresh-install edge.
test "$(stat -c '%u:%g:%a' "$DATA_DIR")" = "$(id -u):$(id -g):700"
"$ROOT_DIR/btctl" --repository "$ROOT_DIR" plan --env "$ENV_FILE" --json >/dev/null
"$ROOT_DIR/btctl" --repository "$ROOT_DIR" install --env "$ENV_FILE" --yes --json >/dev/null
assert_doctor
test "$(stat -c '%u:%a' "$DATA_DIR")" = "101:2750"
declare -A tenant_key_before session_key_before
for reader in cwa kavita; do
    test "$(docker exec "$HUB_CONTAINER" stat -c '%u:%a:%s' "/app/data/$reader/reader_session_key")" = "101:600:32"
    test "$(docker exec "$HUB_CONTAINER" stat -c '%u:%a:%s' "/app/data/$reader/tenant_key")" = "101:600:32"
    tenant_key_before[$reader]="$(docker exec "$HUB_CONTAINER" sha256sum "/app/data/$reader/tenant_key" | cut -d' ' -f1)"
    session_key_before[$reader]="$(docker exec "$HUB_CONTAINER" sha256sum "/app/data/$reader/reader_session_key" | cut -d' ' -f1)"
done

"$ROOT_DIR/btctl" --repository "$ROOT_DIR" uninstall --env "$ENV_FILE" --yes --json >/dev/null
for reader in cwa kavita; do
    docker run --rm --network none --user 101:102 --read-only \
        --cap-drop ALL --security-opt no-new-privileges:true \
        --entrypoint /bin/sh \
        --mount "type=bind,src=${DATA_DIR},dst=/data,readonly" \
        "$SMOKE_IMAGE" -ec \
        'test ! -e "/data/$1/reader_session_key"; test -f "/data/$1/translations.db"; test -f "/data/$1/tenant_key"' \
        sh "$reader"
done

# Reinstall from retained UID-101/mode-0700 reader trees, then verify exact
# idempotent teardown again. Both credential-inspection permission regimes are
# therefore exercised by the same supported btctl workflow.
"$ROOT_DIR/btctl" --repository "$ROOT_DIR" install --env "$ENV_FILE" --yes --json >/dev/null
assert_doctor
# Sessions are revoked by the fresh key; cache and glossary tenants are not.
for reader in cwa kavita; do
    test "$(docker exec "$HUB_CONTAINER" sha256sum "/app/data/$reader/tenant_key" | cut -d' ' -f1)" = "${tenant_key_before[$reader]}"
    test "$(docker exec "$HUB_CONTAINER" sha256sum "/app/data/$reader/reader_session_key" | cut -d' ' -f1)" != "${session_key_before[$reader]}"
done
"$ROOT_DIR/btctl" --repository "$ROOT_DIR" uninstall --env "$ENV_FILE" --yes --json >/dev/null
test ! -e "$STATE_DIR/install-attempt.json"
printf 'btctl hub fresh and retained lifecycle: OK\n'

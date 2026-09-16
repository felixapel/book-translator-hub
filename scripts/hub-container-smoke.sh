#!/usr/bin/env bash
# Exercise both reader listeners and fail-fast supervision in one production image.
set -euo pipefail

SMOKE_IMAGE="${1:?usage: hub-container-smoke.sh IMAGE PREFIX}"
SMOKE_PREFIX="${2:?usage: hub-container-smoke.sh IMAGE PREFIX}"
[[ "$SMOKE_PREFIX" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,48}$ ]] || exit 64

HUB_CONTAINER="${SMOKE_PREFIX}-hub"
CWA_CONTAINER="${SMOKE_PREFIX}-cwa"
KAVITA_CONTAINER="${SMOKE_PREFIX}-kavita"
SMOKE_NETWORK="${SMOKE_PREFIX}-net"
DATA_DIR=""

cleanup() {
    docker rm -f -v "$HUB_CONTAINER" "$CWA_CONTAINER" "$KAVITA_CONTAINER" \
        >/dev/null 2>&1 || true
    docker network rm "$SMOKE_NETWORK" >/dev/null 2>&1 || true
    if [ -n "$DATA_DIR" ]; then
        docker run --rm --user 0:0 --entrypoint /bin/sh \
            --mount "type=bind,src=${DATA_DIR},dst=/data" \
            "$SMOKE_IMAGE" -c 'rm -rf /data/* /data/.[!.]* /data/..?*' \
            >/dev/null 2>&1 || true
        rmdir "$DATA_DIR" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT
cleanup

DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bt-hub-smoke.XXXXXX")"
docker network create "$SMOKE_NETWORK" >/dev/null
docker run --rm --user 0:0 --entrypoint /bin/sh \
    --mount "type=bind,src=${DATA_DIR},dst=/data" \
    "$SMOKE_IMAGE" -c 'chown 101:102 /data && chmod 0700 /data'
docker run --rm --network none --user 101:102 --entrypoint python \
    --mount "type=bind,src=${DATA_DIR},dst=/data" \
    "$SMOKE_IMAGE" -c 'from pathlib import Path
root=Path("/data")
for name,byte in (("cwa",b"c"),("kavita",b"k")):
    directory=root/name;directory.mkdir(mode=0o700)
    key=directory/"reader_session_key";key.write_bytes(byte*32);key.chmod(0o600)'

sandbox=(
    --read-only
    --tmpfs /tmp:rw,noexec,nosuid,size=128m,uid=101,gid=102,mode=700
    --cap-drop ALL
    --security-opt no-new-privileges:true
)

docker run -d --name "$CWA_CONTAINER" --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    --mount "type=bind,src=$(pwd)/tests/python/test_cwa_strong_fixture.py,dst=/fixture.py,readonly" \
    --entrypoint python "$SMOKE_IMAGE" /fixture.py >/dev/null
docker run -d --name "$KAVITA_CONTAINER" --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    --mount "type=bind,src=$(pwd)/tests/python/test_kavita_auth_fixture.py,dst=/fixture.py,readonly" \
    -e KAVITA_FIXTURE_VERSION=0.9.1.4 \
    --entrypoint python "$SMOKE_IMAGE" /fixture.py >/dev/null

docker run -d --name "$HUB_CONTAINER" --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    --mount "type=bind,src=${DATA_DIR},dst=/app/data" \
    -e BT_ROLE=hub \
    -e BT_ENABLE_CWA=true \
    -e BT_CWA_PUBLIC_ORIGIN=https://books.example.test \
    -e "BT_CWA_READER_UPSTREAM=http://${CWA_CONTAINER}:8083" \
    -e BT_CWA_READER_VERSION=4.0.6 \
    -e BT_CWA_AUTH_PROFILE=reader-session \
    -e BT_CWA_READER_CONNECTOR_ID=01234567-89ab-4cde-8123-0123456789ab \
    -e BT_CWA_PUBLISHED_PORT=8385 \
    -e BT_ENABLE_KAVITA=true \
    -e BT_KAVITA_PUBLIC_ORIGIN=https://kavita.example.test \
    -e "BT_KAVITA_READER_UPSTREAM=http://${KAVITA_CONTAINER}:5000" \
    -e BT_KAVITA_READER_VERSION=0.9.1.4 \
    -e BT_KAVITA_AUTH_PROFILE=reader-session \
    -e BT_KAVITA_READER_CONNECTOR_ID=11234567-89ab-4cde-8123-0123456789ab \
    -e BT_KAVITA_PUBLISHED_PORT=8386 \
    -e LLM_PROVIDER=local \
    -e LLM_MODEL=smoke-model \
    -e BT_LOCAL_URL=http://127.0.0.1:1234/v1/chat/completions \
    -e BT_MAX_CONCURRENT=2 \
    -e BT_MAX_UPSTREAM_INFLIGHT=2 \
    -p 127.0.0.1::8080 \
    -p 127.0.0.1::8081 \
    "$SMOKE_IMAGE" >/dev/null

CWA_PORT="$(docker port "$HUB_CONTAINER" 8080/tcp | sed 's/.*://')"
KAVITA_PORT="$(docker port "$HUB_CONTAINER" 8081/tcp | sed 's/.*://')"
for _ in $(seq 1 45); do
    if curl -sf "http://127.0.0.1:${CWA_PORT}/bt-api/ping" >/dev/null 2>&1 \
        && curl -sf "http://127.0.0.1:${KAVITA_PORT}/bt-api/ping" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
curl -sf "http://127.0.0.1:${CWA_PORT}/bt-api/ping" | grep -q '"status":"ok"'
curl -sf "http://127.0.0.1:${KAVITA_PORT}/bt-api/ping" | grep -q '"status":"ok"'
curl -sf "http://127.0.0.1:${KAVITA_PORT}/library/1/series/2/book/3" \
    | grep -q '/bt-static/loader.js'

: >"${DATA_DIR}/kavita-session.headers"
curl -sf -D "${DATA_DIR}/kavita-session.headers" -o /dev/null -X POST \
    -H 'Authorization: Bearer container-smoke-kavita-access' \
    -H 'User-Agent: Hub-Smoke-Browser/1.0' \
    -H 'Origin: https://kavita.example.test' \
    "http://127.0.0.1:${KAVITA_PORT}/bt-api/session"
KAVITA_PLUGIN_COOKIE="$(awk 'BEGIN{IGNORECASE=1} /^set-cookie:/ {sub(/^[^:]*:[[:space:]]*/, ""); split($0, parts, ";"); if (parts[1] ~ /^__Host-bt-kavita-session=/) print parts[1]}' "${DATA_DIR}/kavita-session.headers" | tail -n 1 | tr -d '\r')"
[[ "$KAVITA_PLUGIN_COOKIE" =~ ^__Host-bt-kavita-session=[A-Za-z0-9_-]{32,128}$ ]]
curl -sf -H "Cookie: ${KAVITA_PLUGIN_COOKIE}" \
    -H 'User-Agent: Hub-Smoke-Browser/1.0' \
    "http://127.0.0.1:${KAVITA_PORT}/bt-api/metrics" \
    | grep -q '"total_requests"'

docker exec "$HUB_CONTAINER" test -f /app/data/cwa/translations.db
docker exec "$HUB_CONTAINER" test -f /app/data/kavita/translations.db
docker exec "$HUB_CONTAINER" test -f /app/data/cwa/reader_session_key
docker exec "$HUB_CONTAINER" test -f /app/data/kavita/reader_session_key
test "$(docker exec "$HUB_CONTAINER" stat -c %a /app/data/cwa/reader_session_key)" = 600
test "$(docker exec "$HUB_CONTAINER" stat -c %a /app/data/kavita/reader_session_key)" = 600
docker exec "$HUB_CONTAINER" python -c \
    'from pathlib import Path
assert Path("/app/data/cwa/reader_session_key").read_bytes()==b"c"*32
assert Path("/app/data/kavita/reader_session_key").read_bytes()==b"k"*32'
docker exec "$HUB_CONTAINER" grep -q '\$bt_cwa_session_cookie' /tmp/nginx/proxy-cwa.conf
docker exec "$HUB_CONTAINER" grep -q '\$bt_kavita_session_cookie' /tmp/nginx/proxy-kavita.conf

docker exec "$HUB_CONTAINER" python -c \
    'import os; p=[]
for name in os.listdir("/proc"):
    if name.isdecimal():
        try:
            cmd=open(f"/proc/{name}/cmdline","rb").read()
            status=open(f"/proc/{name}/status",encoding="utf-8").read()
        except OSError: continue
        parent=next((line.split()[1] for line in status.splitlines() if line.startswith("PPid:")),"")
        if b"127.0.0.1:8391" in cmd and parent=="1": p.append(int(name))
assert len(p)==1; os.kill(p[0], 15)'
for _ in $(seq 1 20); do
    [ "$(docker inspect "$HUB_CONTAINER" --format '{{.State.Status}}')" = exited ] && break
    sleep 1
done
test "$(docker inspect "$HUB_CONTAINER" --format '{{.State.Status}}')" = exited

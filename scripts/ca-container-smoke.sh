#!/usr/bin/env bash
# Certify the single-container Community Applications profile.
set -euo pipefail

SMOKE_IMAGE="${1:?usage: ca-container-smoke.sh IMAGE PREFIX}"
SMOKE_PREFIX="${2:?usage: ca-container-smoke.sh IMAGE PREFIX}"
if [[ ! "$SMOKE_PREFIX" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,48}$ ]]; then
    echo "invalid smoke prefix: $SMOKE_PREFIX" >&2
    exit 64
fi

APP_CONTAINER="${SMOKE_PREFIX}-app"
CWA_CONTAINER="${SMOKE_PREFIX}-cwa"
PROVIDER_CONTAINER="${SMOKE_PREFIX}-provider"
WRONG_DATA_CONTAINER="${SMOKE_PREFIX}-wrong-data"
SMOKE_NETWORK="${SMOKE_PREFIX}-net"
DATA_DIR=""
COOKIE_JAR=""
RESPONSE_FILE=""
PROVIDER_POLICY=""
PROVIDER_GENERATION=""
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

cleanup() {
    docker rm -f -v \
        "$APP_CONTAINER" "$CWA_CONTAINER" "$PROVIDER_CONTAINER" \
        "$WRONG_DATA_CONTAINER" \
        >/dev/null 2>&1 || true
    docker network rm "$SMOKE_NETWORK" >/dev/null 2>&1 || true
    if [ -n "$DATA_DIR" ] && [ -d "$DATA_DIR" ]; then
        docker run --rm --user 0:0 --network none \
            --read-only --cap-drop ALL \
            --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
            --security-opt no-new-privileges:true \
            --mount "type=bind,src=${DATA_DIR},dst=/data" \
            --entrypoint /bin/sh "$SMOKE_IMAGE" \
            -ec 'chown -R "$1:$2" /data; chmod -R u+rwX /data' \
            sh "$HOST_UID" "$HOST_GID" >/dev/null 2>&1 || true
        rm -rf -- "$DATA_DIR"
    fi
    [ -z "$COOKIE_JAR" ] || rm -f "$COOKIE_JAR"
    [ -z "$RESPONSE_FILE" ] || rm -f "$RESPONSE_FILE"
}
trap cleanup EXIT
cleanup

test "$(docker image inspect "$SMOKE_IMAGE" --format '{{.Config.User}}')" = "appuser"
docker network create "$SMOKE_NETWORK" >/dev/null
DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cwa-ca-data.XXXXXX")"

sandbox=(
    --read-only
    --tmpfs "/tmp:rw,noexec,nosuid,size=64m,uid=101,gid=102,mode=700"
    --cap-drop ALL
    --security-opt no-new-privileges:true
)
app_sandbox=(
    --user 101:102
    "${sandbox[@]}"
)

FIXTURE_SOURCE="$(pwd)/tests/python/test_cwa_strong_fixture.py"
test -r "$FIXTURE_SOURCE"
docker run -d --name "$CWA_CONTAINER" --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    --mount "type=bind,src=${FIXTURE_SOURCE},dst=/fixture/test_cwa_strong_fixture.py,readonly" \
    --entrypoint python "$SMOKE_IMAGE" \
    /fixture/test_cwa_strong_fixture.py >/dev/null

# A deterministic local OpenAI-compatible provider keeps this smoke offline
# from real LLMs while exercising the complete batch envelope.
docker run -d --name "$PROVIDER_CONTAINER" --network "$SMOKE_NETWORK" \
    --read-only --tmpfs /tmp --entrypoint python "$SMOKE_IMAGE" -c '
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            system = payload["messages"][0]["content"]
            user = payload["messages"][-1]["content"]
            if "cwa-translate-segments/v1" in system:
                envelope = json.loads(user)
                content = json.dumps({
                    "protocol": "cwa-translate-segments/v1",
                    "translations": [
                        {"id": segment["id"], "text": "translated:" + segment["text"]}
                        for segment in envelope["segments"]
                    ],
                })
            else:
                content = "translated:" + user
            body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):
        pass

HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
' >/dev/null

for endpoint in \
    "${CWA_CONTAINER}:8083/ajax/emailstat" \
    "${PROVIDER_CONTAINER}:8000"; do
    ready=false
    for _ in $(seq 1 30); do
        if docker run --rm --network "$SMOKE_NETWORK" --entrypoint python \
            "$SMOKE_IMAGE" -c \
            "import socket; host, port = '${endpoint%%/*}'.split(':'); socket.create_connection((host, int(port)), 1).close()" \
            >/dev/null 2>&1; then
            ready=true
            break
        fi
        sleep 1
    done
    if [ "$ready" != true ]; then
        echo "fixture did not become ready: $endpoint" >&2
        exit 1
    fi
done

app_environment=(
    -e BT_ROLE=all
    -e "CWA_UPSTREAM=http://${CWA_CONTAINER}:8083"
    -e BT_PUBLIC_ORIGIN=http://books.example.test:8385
    -e BT_BROWSER_AUTH_MODE=cwa_session
    -e BT_BROWSER_CREDENTIALS=same-origin
    -e BT_AUTH_MODE=cwa_session
    -e "BT_CWA_AUTH_URL=http://${CWA_CONTAINER}:8083/ajax/emailstat"
    -e LLM_PROVIDER=local
    -e LLM_MODEL=ca-smoke-model
    -e "BT_LOCAL_URL=http://${PROVIDER_CONTAINER}:8000/v1/chat/completions"
)

# The CA bind must fail closed until the documented pre-step creates a private
# directory owned by the runtime identity.
docker run --rm --user 0:0 --network none \
    --mount "type=bind,src=${DATA_DIR},dst=/data" \
    --entrypoint /bin/sh "$SMOKE_IMAGE" \
    -ec 'chown 0:0 /data; chmod 0755 /data'
docker run -d --name "$WRONG_DATA_CONTAINER" --network "$SMOKE_NETWORK" \
    "${app_sandbox[@]}" \
    --mount "type=bind,src=${DATA_DIR},dst=/app/data" \
    "${app_environment[@]}" "$SMOKE_IMAGE" >/dev/null
for _ in $(seq 1 20); do
    if [ "$(docker inspect "$WRONG_DATA_CONTAINER" --format '{{.State.Running}}')" = "false" ]; then
        break
    fi
    sleep 0.1
done
if [ "$(docker inspect "$WRONG_DATA_CONTAINER" --format '{{.State.Running}}')" = "true" ]; then
    echo "combined profile started with wrong ownership or mode" >&2
    exit 1
fi
wrong_data_output="$(docker logs "$WRONG_DATA_CONTAINER" 2>&1)"
grep -Eiq 'cache|data|writ' <<<"$wrong_data_output"
docker rm "$WRONG_DATA_CONTAINER" >/dev/null

docker run --rm --user 0:0 --network none \
    --mount "type=bind,src=${DATA_DIR},dst=/data" \
    --entrypoint /bin/sh "$SMOKE_IMAGE" \
    -ec 'chown 101:102 /data; chmod 0700 /data'
test "$(stat -c '%u:%g:%a' "$DATA_DIR")" = "101:102:700"

# Invalid CA configuration must fail before serving anything.
if invalid_output="$(docker run --rm --network "$SMOKE_NETWORK" \
    "${app_sandbox[@]}" \
    --mount "type=bind,src=${DATA_DIR},dst=/app/data" \
    -e BT_ROLE=all \
    -e "CWA_UPSTREAM=http://${CWA_CONTAINER}:8083" \
    -e BT_BROWSER_AUTH_MODE=cwa_session \
    -e BT_BROWSER_CREDENTIALS=same-origin \
    -e BT_AUTH_MODE=cwa_session \
    -e "BT_CWA_AUTH_URL=http://${CWA_CONTAINER}:8083/ajax/emailstat" \
    "$SMOKE_IMAGE" 2>&1)"; then
    echo "combined profile unexpectedly started without BT_PUBLIC_ORIGIN" >&2
    exit 1
fi
grep -q 'BT_PUBLIC_ORIGIN' <<<"$invalid_output"

start_app() {
    docker run -d --name "$APP_CONTAINER" --network "$SMOKE_NETWORK" \
        "${app_sandbox[@]}" \
        --mount "type=bind,src=${DATA_DIR},dst=/app/data" \
        "${app_environment[@]}" \
        -p 127.0.0.1::8080 \
        "$SMOKE_IMAGE" >/dev/null
}

start_app
APP_PORT="$(docker port "$APP_CONTAINER" 8080/tcp | sed 's/.*://')"
test -n "$APP_PORT"
test -z "$(docker port "$APP_CONTAINER" 8390/tcp 2>/dev/null || true)"
for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${APP_PORT}/bt-api/ping" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
curl -sf "http://127.0.0.1:${APP_PORT}/bt-api/ping" | grep -q '"status":"ok"'

test "$(docker exec "$APP_CONTAINER" id -u)" = "101"
test "$(docker exec "$APP_CONTAINER" id -g)" = "102"
test "$(docker inspect "$APP_CONTAINER" --format '{{.Config.User}}')" = "101:102"
test "$(docker inspect "$APP_CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Type}} {{.Source}}{{end}}{{end}}')" = "bind $DATA_DIR"
test "$(docker inspect "$APP_CONTAINER" --format '{{.HostConfig.ReadonlyRootfs}}')" = "true"
docker inspect "$APP_CONTAINER" --format '{{json .HostConfig.CapDrop}}' | grep -q 'ALL'
docker inspect "$APP_CONTAINER" --format '{{json .HostConfig.SecurityOpt}}' | \
    grep -q 'no-new-privileges:true'

COOKIE_JAR="$(mktemp "${TMPDIR:-/tmp}/cwa-ca-cookie.XXXXXX")"
RESPONSE_FILE="$(mktemp "${TMPDIR:-/tmp}/cwa-ca-response.XXXXXX")"
BROWSER_UA='CA-Smoke-Browser/1.0'
curl -sf -c "$COOKIE_JAR" -H "User-Agent: ${BROWSER_UA}" \
    "http://127.0.0.1:${APP_PORT}/fixture/login" | grep -q '"authenticated":true'

load_provider_policy() {
    PROVIDER_POLICY="$(curl -sf -b "$COOKIE_JAR" \
        -H "User-Agent: ${BROWSER_UA}" \
        "http://127.0.0.1:${APP_PORT}/bt-api/provider-policy")"
    PROVIDER_GENERATION="$(sed -n \
        's/.*"generation":"\([^"]*\)".*/\1/p' <<<"$PROVIDER_POLICY")"
    test -n "$PROVIDER_GENERATION"
}

load_provider_policy

request_translation() {
    local status
    local payload
    local origin="${1-http://books.example.test:8385}"
    local expected_status="${2:-200}"
    payload='{"paragraphs":["first smoke paragraph","second smoke paragraph"],'
    payload+='"source_lang":"English","target_lang":"Spanish",'
    payload+='"book_id":"ca-smoke-book","chapter_id":"chapter-1",'
    payload+="\"provider_policy\":${PROVIDER_POLICY}}"
    status="$(curl -sS -b "$COOKIE_JAR" -H "User-Agent: ${BROWSER_UA}" \
        -H "Origin: ${origin}" \
        -H 'Content-Type: application/json' \
        --data "$payload" \
        "http://127.0.0.1:${APP_PORT}/bt-api/translate/batch" \
        --output "$RESPONSE_FILE" --write-out '%{http_code}')"
    if [ "$status" != "$expected_status" ]; then
        echo "combined profile translation returned HTTP ${status}, expected ${expected_status}" >&2
        sed -n '1,10p' "$RESPONSE_FILE" >&2
        return 1
    fi
}

# Valid reader cookies do not authorize a missing or cross-site write origin.
request_translation '' 403
request_translation 'http://untrusted.example.test' 403
request_translation
grep -q 'translated:first smoke paragraph' "$RESPONSE_FILE"
grep -q 'translated:second smoke paragraph' "$RESPONSE_FILE"

# Recreate the CA container with its original bind, stop the provider, and
# prove the same request is served from the persistent cache.
docker rm -f "$APP_CONTAINER" >/dev/null
docker stop "$PROVIDER_CONTAINER" >/dev/null
start_app
APP_PORT="$(docker port "$APP_CONTAINER" 8080/tcp | sed 's/.*://')"
for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${APP_PORT}/bt-api/ping" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
load_provider_policy
request_translation
grep -Eq '"cached":\[true,true\]' "$RESPONSE_FILE"

echo "Community Applications combined profile, strong session, translation, and recreate cache: OK"

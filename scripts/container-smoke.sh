#!/usr/bin/env bash
# Exercise one locally built image as independent, sandboxed API/proxy roles.
set -euo pipefail

SMOKE_IMAGE="${1:?usage: container-smoke.sh IMAGE PREFIX}"
SMOKE_PREFIX="${2:?usage: container-smoke.sh IMAGE PREFIX}"
if [[ ! "$SMOKE_PREFIX" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,48}$ ]]; then
    echo "invalid smoke prefix: $SMOKE_PREFIX" >&2
    exit 64
fi

API_CONTAINER="${SMOKE_PREFIX}-api"
PROXY_CONTAINER="${SMOKE_PREFIX}-proxy"
CWA_CONTAINER="${SMOKE_PREFIX}-cwa-strong"
KAVITA_CONTAINER="${SMOKE_PREFIX}-kavita"
EDGE_CONTAINER="${SMOKE_PREFIX}-edge"
OUTPOST_CONTAINER="${SMOKE_PREFIX}-outpost"
SMOKE_NETWORK="${SMOKE_PREFIX}-net"
SMOKE_VOLUME="${SMOKE_PREFIX}-data"
SMOKE_TOKEN="container-smoke-only-secret"
EDGE_DIR=""
CWA_COOKIE_JAR=""
SESSION_HEADERS=""

cleanup() {
    docker rm -f -v \
        "$EDGE_CONTAINER" "$OUTPOST_CONTAINER" \
        "$PROXY_CONTAINER" "$API_CONTAINER" "$CWA_CONTAINER" \
        "$KAVITA_CONTAINER" \
        >/dev/null 2>&1 || true
    docker volume rm -f "$SMOKE_VOLUME" >/dev/null 2>&1 || true
    docker network rm "$SMOKE_NETWORK" >/dev/null 2>&1 || true
    if [ -n "$EDGE_DIR" ]; then
        rm -rf "$EDGE_DIR"
    fi
    if [ -n "$CWA_COOKIE_JAR" ]; then
        rm -f "$CWA_COOKIE_JAR"
    fi
    if [ -n "$SESSION_HEADERS" ]; then
        rm -f "$SESSION_HEADERS"
    fi
}
trap cleanup EXIT
cleanup

test "$(docker image inspect "$SMOKE_IMAGE" --format '{{.Config.User}}')" = "appuser"
docker network create "$SMOKE_NETWORK" >/dev/null
docker volume create "$SMOKE_VOLUME" >/dev/null

sandbox=(
    --read-only
    --tmpfs "/tmp:rw,noexec,nosuid,size=64m,uid=101,gid=102,mode=700"
    --cap-drop ALL
    --security-opt no-new-privileges:true
)

# Run a dependency-free, minimal CWA v4.0.6 strong-session model from the
# checkout inside the candidate image. It exercises ProxyFix(x_for=1), the
# address/User-Agent session fingerprint, and /ajax/emailstat's HTML-vs-JSON
# contract without pulling another mutable application image into this gate.
FIXTURE_SOURCE="$(pwd)/tests/python/test_cwa_strong_fixture.py"
test -r "$FIXTURE_SOURCE"
docker run -d --name "$CWA_CONTAINER" --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    --mount "type=bind,src=${FIXTURE_SOURCE},dst=/fixture/test_cwa_strong_fixture.py,readonly" \
    --entrypoint python \
    "$SMOKE_IMAGE" /fixture/test_cwa_strong_fixture.py >/dev/null

cwa_ready=false
for _ in $(seq 1 30); do
    if docker exec "$CWA_CONTAINER" python -c \
        'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8083/ajax/emailstat", timeout=1).read()' \
        >/dev/null 2>&1; then
        cwa_ready=true
        break
    fi
    sleep 1
done
if [ "$cwa_ready" != true ]; then
    echo "CWA strong-session fixture did not become ready" >&2
    exit 1
fi

if invalid_output="$(docker run --rm --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    -e BT_ROLE=proxy \
    -e "CWA_UPSTREAM=http://${API_CONTAINER}:8390" \
    -e BT_BROWSER_AUTH_MODE=cwa_session \
    -e BT_BROWSER_CREDENTIALS=same-origin \
    "$SMOKE_IMAGE" 2>&1)"; then
    echo "proxy unexpectedly started without BT_PUBLIC_ORIGIN" >&2
    exit 1
fi
grep -q 'BT_PUBLIC_ORIGIN' <<<"$invalid_output"
if grep -q 'Traceback' <<<"$invalid_output"; then
    echo "invalid proxy configuration exposed a traceback" >&2
    exit 1
fi

docker run -d --name "$API_CONTAINER" --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    --mount "type=volume,source=${SMOKE_VOLUME},target=/app/data" \
    -e BT_ROLE=api \
    -e BT_AUTH_MODE=token \
    -e "BT_API_TOKEN=${SMOKE_TOKEN}" \
    -p 127.0.0.1::8390 \
    "$SMOKE_IMAGE" >/dev/null

docker run -d --name "$PROXY_CONTAINER" --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    -e BT_ROLE=proxy \
    -e "CWA_UPSTREAM=http://${CWA_CONTAINER}:8083" \
    -e "BT_API_UPSTREAM=http://${API_CONTAINER}:8390" \
    -e BT_PUBLIC_ORIGIN=https://books.example.test:8443 \
    -e BT_BROWSER_AUTH_MODE=cwa_session \
    -e BT_BROWSER_CREDENTIALS=same-origin \
    -p 127.0.0.1::8080 \
    "$SMOKE_IMAGE" >/dev/null

API_PORT="$(docker port "$API_CONTAINER" 8390/tcp | sed 's/.*://')"
PROXY_PORT="$(docker port "$PROXY_CONTAINER" 8080/tcp | sed 's/.*://')"
test -n "$API_PORT" && test -n "$PROXY_PORT"

for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${PROXY_PORT}/bt-api/ping" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
curl -sf "http://127.0.0.1:${API_PORT}/ping" | grep -q '"status":"ok"'
curl -sf "http://127.0.0.1:${PROXY_PORT}/bt-api/ping" | grep -q '"status":"ok"'
curl -sf -H 'Host: attacker.example' -H 'X-Forwarded-Proto: javascript' \
    -H 'X-Forwarded-For: 203.0.113.99' \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/ping" | grep -q '"status":"ok"'

# The generated configuration, not client-controlled forwarding headers, owns
# the public authority and the immediate client hop.
test "$(docker exec "$PROXY_CONTAINER" grep -Fc \
    'proxy_set_header Host books.example.test:8443;' /tmp/nginx/proxy.conf)" = "3"
test "$(docker exec "$PROXY_CONTAINER" grep -Fc \
    'proxy_set_header X-Forwarded-Proto https;' /tmp/nginx/proxy.conf)" = "3"
test "$(docker exec "$PROXY_CONTAINER" grep -Fc \
    'proxy_set_header X-Forwarded-For $remote_addr;' /tmp/nginx/proxy.conf)" = "3"
test "$(docker exec "$PROXY_CONTAINER" grep -Fc \
    'proxy_set_header User-Agent $http_user_agent;' /tmp/nginx/proxy.conf)" = "3"
test "$(docker exec "$PROXY_CONTAINER" grep -Fc \
    'proxy_set_header Remote-User "";' /tmp/nginx/proxy.conf)" = "1"
docker exec "$PROXY_CONTAINER" grep -Fq \
    'client_max_body_size 2g;' /tmp/nginx/proxy.conf

# Protected surfaces reject anonymous callers through both published paths,
# while the configured compatibility token succeeds.
test "$(curl -sS -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:${API_PORT}/metrics")" = "401"
test "$(curl -sS -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/metrics")" = "401"
curl -sf -H "X-BT-Token: ${SMOKE_TOKEN}" \
    "http://127.0.0.1:${API_PORT}/metrics" | grep -q '"total_requests"'
curl -sf -H "X-BT-Token: ${SMOKE_TOKEN}" \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/metrics" | grep -q '"total_requests"'

# Recreate the API in the default managed CWA-session mode, log in through the
# exact same injection proxy, and prove that a strong session succeeds only
# when both the proxy-observed peer and browser User-Agent are replayed. A
# negative verdict for another User-Agent must not poison the valid context.
docker rm -f "$API_CONTAINER" >/dev/null
docker run -d --name "$API_CONTAINER" --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    --mount "type=volume,source=${SMOKE_VOLUME},target=/app/data" \
    -e BT_ROLE=api \
    -e BT_AUTH_MODE=cwa_session \
    -e "BT_CWA_AUTH_URL=http://${CWA_CONTAINER}:8083/ajax/emailstat" \
    -e BT_PUBLIC_ORIGIN=https://books.example.test:8443 \
    -e BT_ALLOWED_ORIGINS=https://books.example.test:8443 \
    -e "BT_TRUSTED_PROXY_HOST=${PROXY_CONTAINER}" \
    -p 127.0.0.1::8390 \
    "$SMOKE_IMAGE" >/dev/null
API_PORT="$(docker port "$API_CONTAINER" 8390/tcp | sed 's/.*://')"
test -n "$API_PORT"
for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${API_PORT}/ping" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

CWA_COOKIE_JAR="$(mktemp "${TMPDIR:-/tmp}/cwa-strong-cookie.XXXXXX")"
BROWSER_UA='Container-Smoke-Browser/1.0'
curl -sf -c "$CWA_COOKIE_JAR" -H "User-Agent: ${BROWSER_UA}" \
    "http://127.0.0.1:${PROXY_PORT}/fixture/login" \
    | grep -q '"authenticated":true'
test "$(curl -sS -o /dev/null -w '%{http_code}' \
    -b "$CWA_COOKIE_JAR" -H 'User-Agent: Wrong-Browser/1.0' \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/metrics")" = "401"
curl -sf -b "$CWA_COOKIE_JAR" -H "User-Agent: ${BROWSER_UA}" \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/metrics" \
    | grep -q '"total_requests"'
test "$(curl -sS -o /dev/null -w '%{http_code}' \
    -b "$CWA_COOKIE_JAR" -H "User-Agent: ${BROWSER_UA}" \
    "http://127.0.0.1:${API_PORT}/metrics")" = "401"

# Exercise the managed opaque-session boundary. Raw CWA credentials may reach
# only the exact exchange endpoint; ordinary API routes receive only the
# five-minute plugin cookie, bound to the proxy-observed address and User-Agent.
docker rm -f "$API_CONTAINER" >/dev/null
docker run -d --name "$API_CONTAINER" --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    --mount "type=volume,source=${SMOKE_VOLUME},target=/app/data" \
    -e BT_ROLE=api \
    -e BT_AUTH_MODE=reader_session \
    -e BT_READER_TYPE=cwa \
    -e "BT_READER_AUTH_URL=http://${CWA_CONTAINER}:8083/ajax/emailstat" \
    -e BT_READER_VERSION=4.0.6 \
    -e BT_READER_CONTRACT_VERSION=cwa-epub-v1 \
    -e BT_READER_CONNECTOR_ID=00000000-0000-4000-8000-000000000001 \
    -e BT_PUBLIC_ORIGIN=https://books.example.test:8443 \
    -e BT_ALLOWED_ORIGINS=https://books.example.test:8443 \
    -e BT_SESSION_KEY_PATH=/app/data/reader_session_key \
    -e "BT_TRUSTED_PROXY_HOST=${PROXY_CONTAINER}" \
    -p 127.0.0.1::8390 \
    "$SMOKE_IMAGE" >/dev/null
API_PORT="$(docker port "$API_CONTAINER" 8390/tcp | sed 's/.*://')"
for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${API_PORT}/ping" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

SESSION_HEADERS="$(mktemp "${TMPDIR:-/tmp}/reader-session-headers.XXXXXX")"
curl -sf -D "$SESSION_HEADERS" -o /dev/null -X POST \
    -b "$CWA_COOKIE_JAR" \
    -H "User-Agent: ${BROWSER_UA}" \
    -H 'Origin: https://books.example.test:8443' \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/session"
PLUGIN_COOKIE="$(awk 'BEGIN{IGNORECASE=1} /^set-cookie:/ {sub(/^[^:]*:[[:space:]]*/, ""); split($0, parts, ";"); if (parts[1] ~ /^__Host-bt-session=/) print parts[1]}' "$SESSION_HEADERS" | tail -n 1 | tr -d '\r')"
if [[ ! "$PLUGIN_COOKIE" =~ ^__Host-bt-session=[A-Za-z0-9_-]{32,128}$ ]]; then
    echo "opaque CWA session cookie was not issued" >&2
    exit 1
fi
test "$(curl -sS -o /dev/null -w '%{http_code}' \
    -b "$CWA_COOKIE_JAR" -H "User-Agent: ${BROWSER_UA}" \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/metrics")" = "401"
curl -sf -H "Cookie: ${PLUGIN_COOKIE}" -H "User-Agent: ${BROWSER_UA}" \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/metrics" \
    | grep -q '"total_requests"'
test "$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "Cookie: ${PLUGIN_COOKIE}" -H 'User-Agent: Wrong-Browser/1.0' \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/metrics")" = "401"
curl -sf -X DELETE \
    -H "Cookie: ${PLUGIN_COOKIE}; session=cwa-cookie-must-not-reach-api" \
    -H "User-Agent: ${BROWSER_UA}" \
    -H 'Origin: https://books.example.test:8443' \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/session" \
    | grep -q '"status":"revoked"'
test "$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "Cookie: ${PLUGIN_COOKIE}" -H "User-Agent: ${BROWSER_UA}" \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/metrics")" = "401"

# An exact Kavita fixture proves both stock HTML proxying and native bearer
# exchange. Its account DTO includes unrelated auth-key data; the broker uses
# only the bounded user id and selected certified version fields.
# Preserve the older certified pair here; hub-container-smoke covers 0.9.1.4.
KAVITA_SMOKE_VERSION="${KAVITA_SMOKE_VERSION:-0.9.0.2}"
case "$KAVITA_SMOKE_VERSION" in
    0.9.0.2|0.9.1.4) ;;
    *) echo "unsupported Kavita smoke version: $KAVITA_SMOKE_VERSION" >&2; exit 64 ;;
esac
KAVITA_SMOKE_CONTRACT="kavita-${KAVITA_SMOKE_VERSION}-epub-v1"
KAVITA_FIXTURE_SOURCE="$(pwd)/tests/python/test_kavita_auth_fixture.py"
test -r "$KAVITA_FIXTURE_SOURCE"
docker run -d --name "$KAVITA_CONTAINER" --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    --mount "type=bind,src=${KAVITA_FIXTURE_SOURCE},dst=/fixture/test_kavita_auth_fixture.py,readonly" \
    -e "KAVITA_FIXTURE_VERSION=${KAVITA_SMOKE_VERSION}" \
    --entrypoint python "$SMOKE_IMAGE" \
    /fixture/test_kavita_auth_fixture.py >/dev/null
for _ in $(seq 1 30); do
    if docker exec "$KAVITA_CONTAINER" python -c \
        'import socket; socket.create_connection(("127.0.0.1", 5000), 1).close()' \
        >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

docker rm -f "$API_CONTAINER" "$PROXY_CONTAINER" >/dev/null
docker run -d --name "$API_CONTAINER" --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    --mount "type=volume,source=${SMOKE_VOLUME},target=/app/data" \
    -e BT_ROLE=api \
    -e BT_AUTH_MODE=reader_session \
    -e BT_READER_TYPE=kavita \
    -e "BT_READER_AUTH_URL=http://${KAVITA_CONTAINER}:5000/api/Account" \
    -e "BT_READER_VERSION=${KAVITA_SMOKE_VERSION}" \
    -e "BT_READER_CONTRACT_VERSION=${KAVITA_SMOKE_CONTRACT}" \
    -e BT_READER_CONNECTOR_ID=00000000-0000-4000-8000-000000000002 \
    -e BT_PUBLIC_ORIGIN=https://books.example.test:8443 \
    -e BT_ALLOWED_ORIGINS=https://books.example.test:8443 \
    -e BT_SESSION_KEY_PATH=/app/data/reader_session_key \
    -e "BT_TRUSTED_PROXY_HOST=${PROXY_CONTAINER}" \
    -p 127.0.0.1::8390 \
    "$SMOKE_IMAGE" >/dev/null
docker run -d --name "$PROXY_CONTAINER" --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    -e BT_ROLE=proxy \
    -e BT_READER_TYPE=kavita \
    -e "BT_READER_UPSTREAM=http://${KAVITA_CONTAINER}:5000" \
    -e "BT_API_UPSTREAM=http://${API_CONTAINER}:8390" \
    -e BT_PUBLIC_ORIGIN=https://books.example.test:8443 \
    -e "BT_READER_VERSION=${KAVITA_SMOKE_VERSION}" \
    -e "BT_READER_CONTRACT_VERSION=${KAVITA_SMOKE_CONTRACT}" \
    -e BT_BROWSER_AUTH_MODE=reader_session \
    -e BT_BROWSER_CREDENTIALS=same-origin \
    -p 127.0.0.1::8080 \
    "$SMOKE_IMAGE" >/dev/null
API_PORT="$(docker port "$API_CONTAINER" 8390/tcp | sed 's/.*://')"
PROXY_PORT="$(docker port "$PROXY_CONTAINER" 8080/tcp | sed 's/.*://')"
for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${PROXY_PORT}/bt-api/ping" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
curl -sf "http://127.0.0.1:${PROXY_PORT}/library/7/series/42/book/99" \
    | grep -q '<div class="book-content">'
curl -sf "http://127.0.0.1:${PROXY_PORT}/library/7/series/42/book/99" \
    | grep -q '/bt-static/loader.js'

: >"$SESSION_HEADERS"
curl -sf -D "$SESSION_HEADERS" -o /dev/null -X POST \
    -H 'Authorization: Bearer container-smoke-kavita-access' \
    -H "User-Agent: ${BROWSER_UA}" \
    -H 'Origin: https://books.example.test:8443' \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/session"
PLUGIN_COOKIE="$(awk 'BEGIN{IGNORECASE=1} /^set-cookie:/ {sub(/^[^:]*:[[:space:]]*/, ""); split($0, parts, ";"); if (parts[1] ~ /^__Host-bt-session=/) print parts[1]}' "$SESSION_HEADERS" | tail -n 1 | tr -d '\r')"
if [[ ! "$PLUGIN_COOKIE" =~ ^__Host-bt-session=[A-Za-z0-9_-]{32,128}$ ]]; then
    echo "opaque Kavita session cookie was not issued" >&2
    exit 1
fi
test "$(curl -sS -o /dev/null -w '%{http_code}' \
    -H 'Authorization: Bearer container-smoke-kavita-access' \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/metrics")" = "401"
curl -sf -H "Cookie: ${PLUGIN_COOKIE}" -H "User-Agent: ${BROWSER_UA}" \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/metrics" \
    | grep -q '"total_requests"'
curl -sf -X DELETE \
    -H "Cookie: ${PLUGIN_COOKIE}; .AspNetCore.Cookies=oidc-cookie-must-not-reach-api" \
    -H "User-Agent: ${BROWSER_UA}" \
    -H 'Origin: https://books.example.test:8443' \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/session" \
    | grep -q '"status":"revoked"'
test "$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "Cookie: ${PLUGIN_COOKIE}" -H "User-Agent: ${BROWSER_UA}" \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/metrics")" = "401"

# Render the managed Authentik/Nginx edge fragment, validate it with the nginx
# binary shipped in the candidate, and exercise a successful auth subrequest.
# This catches phase-ordering bugs that textual assertions cannot detect.
EDGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cwa-translate-edge.XXXXXX")"
python3 - "$SMOKE_PREFIX" "$SMOKE_NETWORK" "$OUTPOST_CONTAINER" \
    "$EDGE_DIR/authentik.conf" "$EDGE_DIR/nginx.conf" <<'PY'
import sys
from pathlib import Path

from btctl_auth import render_authentik_edge
from btctl_core import DeploymentPlan, InstallConfig, ReleaseIdentity

prefix, network, outpost, artifact_path, nginx_path = sys.argv[1:]
identity = ReleaseIdentity.from_checkout(
    version=Path("VERSION").read_text(encoding="utf-8").strip(),
    sha="a" * 40,
    clean=True,
)
config = InstallConfig.from_mapping(
    {
        "BT_INSTALL_PROFILE": "compose-existing",
        "BT_INSTALL_NAME": prefix,
        "BT_INGRESS_MODE": "docker-edge",
        "BT_PROXY_PORT": "",
        "BT_EDGE_NETWORK": network,
        "BT_AUTH_PROFILE": "authentik-forwarded",
        "BT_PUBLIC_ORIGIN": "https://books.example.test",
        "CWA_UPSTREAM": "http://calibre-web-automated:8083",
        "BT_CWA_CONTAINER": "calibre-web-automated",
        "BT_CWA_NETWORK": f"{network}-cwa",
        "BT_CWA_VERSION": "4.0.6",
        "BT_STATE_DIR": f"/tmp/{prefix}-state",
        "BT_DATA_DIR": f"/tmp/{prefix}-data",
        "BT_BACKUP_DIR": f"/tmp/{prefix}-backup",
        "BT_IDENTITY_PROXY_IP": "127.0.0.1/32",
        "BT_AUTHENTIK_VERSION": "2026.5.4",
        "BT_AUTHENTIK_OUTPOST_URL": f"http://{outpost}:9000",
        "BT_REVERSE_PROXY": "nginx",
        "LLM_PROVIDER": "local",
        "LLM_MODEL": "smoke-model",
        "BT_LOCAL_URL": "http://host.docker.internal:2819/v1/chat/completions",
        "LLM_API_KEY": "",
    },
    identity,
)
artifact = render_authentik_edge(config, DeploymentPlan.from_config(config))
Path(artifact_path).write_text(artifact.content, encoding="utf-8")
Path(nginx_path).write_text(
    """worker_processes 1;
error_log /dev/stderr info;
pid /tmp/nginx.pid;
events { worker_connections 128; }
http {
    access_log off;
    client_body_temp_path /tmp/client_temp;
    proxy_temp_path /tmp/proxy_temp;
    fastcgi_temp_path /tmp/fastcgi_temp;
    uwsgi_temp_path /tmp/uwsgi_temp;
    scgi_temp_path /tmp/scgi_temp;
    server {
        listen 8080;
        server_name books.example.test;
        include /edge/authentik.conf;
        location @goauthentik_proxy_signin { return 401; }
    }
}
""",
    encoding="utf-8",
)
PY
chmod 0755 "$EDGE_DIR"
chmod 0644 "$EDGE_DIR/authentik.conf" "$EDGE_DIR/nginx.conf"

docker run -d --name "$OUTPOST_CONTAINER" --network "$SMOKE_NETWORK" \
    --read-only --tmpfs /tmp --entrypoint python "$SMOKE_IMAGE" -c '
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if any(self.headers.get(name) for name in (
            "X-authentik-uid", "X-BT-Subject", "X-BT-Roles"
        )):
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        cookie = self.headers.get("Cookie", "")
        if "authentik_session=valid" not in cookie and "authentik_session=missing" not in cookie:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        if "authentik_session=valid" in cookie:
            self.send_header("X-authentik-uid", "canonical-smoke-user")
        self.send_header("Content-Length", "0")
        self.end_headers()
    def log_message(self, *args):
        pass
HTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
' >/dev/null

docker run --rm --network "$SMOKE_NETWORK" --read-only --tmpfs /tmp \
    --mount "type=bind,src=${EDGE_DIR},dst=/edge,readonly" \
    --entrypoint nginx "$SMOKE_IMAGE" -t -c /edge/nginx.conf
docker run -d --name "$EDGE_CONTAINER" --network "$SMOKE_NETWORK" \
    --read-only --tmpfs /tmp -p 127.0.0.1::8080 \
    --mount "type=bind,src=${EDGE_DIR},dst=/edge,readonly" \
    --entrypoint nginx "$SMOKE_IMAGE" \
    -c /edge/nginx.conf -g 'daemon off;' >/dev/null
EDGE_PORT="$(docker port "$EDGE_CONTAINER" 8080/tcp | sed 's/.*://')"
EDGE_IP="$(docker inspect "$EDGE_CONTAINER" --format \
    "{{with index .NetworkSettings.Networks \"${SMOKE_NETWORK}\"}}{{.IPAddress}}{{end}}")"
test -n "$EDGE_IP"

# Recreate the API in the exact managed forwarded-identity mode. The edge is
# now the sole trusted peer; direct host requests and the injection proxy are
# untrusted even if they forge the subject header.
docker rm -f "$API_CONTAINER" >/dev/null
docker run -d --name "$API_CONTAINER" --network "$SMOKE_NETWORK" \
    "${sandbox[@]}" \
    --mount "type=volume,source=${SMOKE_VOLUME},target=/app/data" \
    -e BT_ROLE=api \
    -e BT_AUTH_MODE=forwarded \
    -e BT_PUBLIC_ORIGIN=https://books.example.test \
    -e BT_ALLOWED_ORIGINS=https://books.example.test \
    -e "BT_IDENTITY_TRUSTED_PROXIES=${EDGE_IP}/32" \
    -e "BT_TRUSTED_PROXIES=${EDGE_IP}/32" \
    -e BT_FORWARDED_SUBJECT_HEADER=X-authentik-uid \
    -e BT_FORWARDED_ROLES_HEADER= \
    -p 127.0.0.1::8390 \
    "$SMOKE_IMAGE" >/dev/null
API_PORT="$(docker port "$API_CONTAINER" 8390/tcp | sed 's/.*://')"
test -n "$API_PORT"

for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${API_PORT}/ping" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

test "$(curl -sS -o /dev/null -w '%{http_code}' \
    -H 'X-authentik-uid: forged-direct-user' \
    "http://127.0.0.1:${API_PORT}/metrics")" = "401"
test "$(curl -sS -o /dev/null -w '%{http_code}' \
    -H 'Host: books.example.test' \
    "http://127.0.0.1:${EDGE_PORT}/bt-api/metrics")" = "401"
test "$(curl -sS -o /dev/null -w '%{http_code}' \
    -H 'Host: books.example.test' \
    -H 'Cookie: authentik_session=missing' \
    "http://127.0.0.1:${EDGE_PORT}/bt-api/metrics")" = "401"
curl -sf \
    -H 'Host: books.example.test' \
    -H 'Cookie: authentik_session=valid; browser_only=must-not-reach-api' \
    -H 'X-authentik-uid: forged-browser-user' \
    -H 'X-BT-Subject: forged-alternate-user' \
    -H 'X-BT-Roles: admin' \
    "http://127.0.0.1:${EDGE_PORT}/bt-api/metrics" \
    | grep -q '"total_requests"'

for container in "$API_CONTAINER" "$PROXY_CONTAINER"; do
    test "$(docker exec "$container" id -u)" = "101"
    test "$(docker exec "$container" id -g)" = "102"
    test "$(docker inspect "$container" --format '{{.HostConfig.ReadonlyRootfs}}')" = "true"
    docker inspect "$container" --format '{{json .HostConfig.CapDrop}}' | grep -q 'ALL'
    docker inspect "$container" --format '{{json .HostConfig.SecurityOpt}}' | grep -q 'no-new-privileges:true'
    if docker exec "$container" sh -c ': > /app/rootfs-write-probe' 2>/dev/null; then
        echo "$container unexpectedly wrote to the image rootfs" >&2
        exit 1
    fi
    docker exec "$container" sh -c ': > /tmp/write-probe && rm /tmp/write-probe'
done
if docker exec "$API_CONTAINER" sh -c \
    'test -e /app/test_auth.py -o -e /app/benchmark.py -o -e /app/tests -o -e /app/tools'; then
    echo "published runtime unexpectedly contains tests or benchmarks" >&2
    exit 1
fi
docker exec "$API_CONTAINER" sh -c \
    ': > /app/data/write-probe && rm /app/data/write-probe'
if docker inspect "$PROXY_CONTAINER" \
    --format '{{range .Mounts}}{{println .Destination}}{{end}}' \
    | grep -Fxq /app/data; then
    echo "proxy role unexpectedly mounts API state" >&2
    exit 1
fi

# Independent lifecycle: stopping the API must not stop nginx. The proxy then
# fails closed with 502 (immediate refusal) or 504 (bounded connect timeout).
docker stop --time 5 "$API_CONTAINER" >/dev/null
test "$(docker inspect "$PROXY_CONTAINER" --format '{{.State.Running}}')" = "true"
failure_status="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:${PROXY_PORT}/bt-api/ping")"
case "$failure_status" in
    502|504) ;;
    *)
        echo "proxy returned $failure_status after API shutdown, expected 502/504" >&2
        exit 1
        ;;
esac
docker stop --time 5 "$PROXY_CONTAINER" >/dev/null

echo "container roles, sandbox, routing, and independent shutdown: OK"

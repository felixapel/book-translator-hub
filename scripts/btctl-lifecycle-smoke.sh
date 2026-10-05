#!/usr/bin/env bash
# Exercise the supported source-only lifecycle against a real Docker daemon.
set -euo pipefail

SMOKE_IMAGE="${1:?usage: btctl-lifecycle-smoke.sh IMAGE PREFIX}"
SMOKE_PREFIX="${2:?usage: btctl-lifecycle-smoke.sh IMAGE PREFIX}"
if [[ ! "$SMOKE_PREFIX" =~ ^[a-z0-9][a-z0-9.-]{0,32}$ ]]; then
    echo "invalid lifecycle smoke prefix: $SMOKE_PREFIX" >&2
    exit 64
fi

ROOT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cwa-translate-lifecycle.XXXXXX")"
CWA_NETWORK="${SMOKE_PREFIX}-cwa-net"
CWA_CONTAINER="${SMOKE_PREFIX}-cwa"
CWA_IMAGE="${SMOKE_PREFIX}-fixture-cwa:4.0.6"
CWA_FIXTURE="$ROOT_DIR/cwa-fixture.py"
KAVITA_NETWORK="${SMOKE_PREFIX}-kavita-net"
KAVITA_CONTAINER="${SMOKE_PREFIX}-kavita"
KAVITA_IMAGE="${SMOKE_PREFIX}-fixture-kavita:0.9.0.2"
KAVITA_FIXTURE="$(pwd)/tests/python/test_kavita_auth_fixture.py"
LEGACY_IMAGE="${SMOKE_PREFIX}-fixture-legacy:2.1.4"
LEGACY_CONTAINER="${SMOKE_PREFIX}-legacy"
FRESH_INSTALL="${SMOKE_PREFIX}-fresh"
KAVITA_INSTALL="${SMOKE_PREFIX}-kavita-translate"
MIGRATION_INSTALL="${SMOKE_PREFIX}-migration"

cleanup() {
    docker rm -f -v \
        "${FRESH_INSTALL}-proxy" "${FRESH_INSTALL}-api" \
        "${KAVITA_INSTALL}-proxy" "${KAVITA_INSTALL}-api" \
        "${MIGRATION_INSTALL}-proxy" "${MIGRATION_INSTALL}-api" \
        "$LEGACY_CONTAINER" "$CWA_CONTAINER" "$KAVITA_CONTAINER" \
        >/dev/null 2>&1 || true
    docker network rm \
        "${FRESH_INSTALL}-private" "${KAVITA_INSTALL}-private" \
        "${MIGRATION_INSTALL}-private" "$CWA_NETWORK" "$KAVITA_NETWORK" \
        >/dev/null 2>&1 || true
    docker image rm "$CWA_IMAGE" "$KAVITA_IMAGE" "$LEGACY_IMAGE" \
        >/dev/null 2>&1 || true
    if [ -d "$ROOT_DIR" ]; then
        docker run --rm --user 0:0 \
            --entrypoint /bin/sh \
            --mount "type=bind,src=${ROOT_DIR},dst=/cleanup" \
            "$SMOKE_IMAGE" -ec \
            'chmod -R u+rwX,g+rwX /cleanup' >/dev/null 2>&1 || true
    fi
    rm -rf -- "$ROOT_DIR"
}
trap cleanup EXIT
cleanup
mkdir -p "$ROOT_DIR"
chmod 0700 "$ROOT_DIR"

free_port() {
    python3 - <<'PY'
import socket
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
}

write_environment() {
    local path="$1"
    local install_name="$2"
    local proxy_port="$3"
    local state_dir="$4"
    local data_dir="$5"
    local backup_dir="$6"
    local legacy_data_dir="${7:-}"
    {
        printf 'BT_INSTALL_PROFILE=compose-existing\n'
        printf 'BT_INSTALL_NAME=%s\n' "$install_name"
        printf 'BT_INGRESS_MODE=published\n'
        printf 'BT_PROXY_PORT=%s\n' "$proxy_port"
        printf 'BT_EDGE_NETWORK=\n'
        printf 'BT_AUTH_PROFILE=cwa-session\n'
        printf 'BT_PUBLIC_ORIGIN=http://127.0.0.1:%s\n' "$proxy_port"
        printf 'CWA_UPSTREAM=http://%s:8083\n' "$CWA_CONTAINER"
        printf 'BT_CWA_CONTAINER=%s\n' "$CWA_CONTAINER"
        printf 'BT_CWA_NETWORK=%s\n' "$CWA_NETWORK"
        printf 'BT_CWA_VERSION=4.0.6\n'
        printf 'BT_STATE_DIR=%s\n' "$state_dir"
        printf 'BT_DATA_DIR=%s\n' "$data_dir"
        printf 'BT_BACKUP_DIR=%s\n' "$backup_dir"
        printf 'LLM_PROVIDER=local\n'
        printf 'LLM_MODEL=smoke-model\n'
        printf 'BT_LOCAL_URL=http://host.docker.internal:2819/v1/chat/completions\n'
        printf 'LLM_API_KEY=\n'
        if [ -n "$legacy_data_dir" ]; then
            printf 'BT_LEGACY_CONTAINER=%s\n' "$LEGACY_CONTAINER"
            printf 'BT_LEGACY_DATA_DIR=%s\n' "$legacy_data_dir"
        fi
    } >"$path"
    chmod 0600 "$path"
}

write_kavita_environment() {
    local path="$1"
    local proxy_port="$2"
    local state_dir="$3"
    local data_dir="$4"
    local backup_dir="$5"
    {
        printf 'BT_INSTALL_PROFILE=compose-existing\n'
        printf 'BT_INSTALL_NAME=%s\n' "$KAVITA_INSTALL"
        printf 'BT_INGRESS_MODE=published\n'
        printf 'BT_PROXY_PORT=%s\n' "$proxy_port"
        printf 'BT_EDGE_NETWORK=\n'
        printf 'BT_AUTH_PROFILE=reader-session\n'
        printf 'BT_PUBLIC_ORIGIN=http://127.0.0.1:%s\n' "$proxy_port"
        printf 'BT_READER_TYPE=kavita\n'
        printf 'BT_READER_UPSTREAM=http://%s:5000\n' "$KAVITA_CONTAINER"
        printf 'BT_READER_CONTAINER=%s\n' "$KAVITA_CONTAINER"
        printf 'BT_READER_NETWORK=%s\n' "$KAVITA_NETWORK"
        printf 'BT_READER_VERSION=0.9.0.2\n'
        printf 'BT_CWA_IDENTITY_HEADER=\n'
        printf 'BT_STATE_DIR=%s\n' "$state_dir"
        printf 'BT_DATA_DIR=%s\n' "$data_dir"
        printf 'BT_BACKUP_DIR=%s\n' "$backup_dir"
        printf 'LLM_PROVIDER=local\n'
        printf 'LLM_MODEL=smoke-model\n'
        printf 'BT_LOCAL_URL=http://host.docker.internal:2819/v1/chat/completions\n'
        printf 'LLM_API_KEY=\n'
    } >"$path"
    chmod 0600 "$path"
}

assert_doctor() {
    local environment="$1"
    ./btctl doctor --env "$environment" --json | python3 -c '
import json, sys
report = json.load(sys.stdin)
assert report["ok"] is True, report
assert all(item["status"] == "ok" for item in report["checks"]), report
'
}

assert_v2_database() {
    local container="$1"
    docker exec "$container" python -c '
import sqlite3
database = sqlite3.connect("/app/data/translations.db")
assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
tables = {row[0] for row in database.execute(
    "SELECT name FROM sqlite_master WHERE type=\"table\""
)}
assert {"translations", "translations_v2"} <= tables, tables
row = database.execute(
    "SELECT translated_text FROM translations WHERE cache_key=\"legacy-key\""
).fetchone()
assert row == ("legacy target",), row
database.close()
'
}

docker network create "$CWA_NETWORK" >/dev/null
docker tag "$SMOKE_IMAGE" "$CWA_IMAGE"
LEGACY_CONTEXT="$ROOT_DIR/v2.1.4-source"
mkdir -m 0700 "$LEGACY_CONTEXT"
test "$(git show v2.1.4:VERSION | tr -d '\r\n')" = "2.1.4"
git archive --format=tar v2.1.4 | tar -xf - -C "$LEGACY_CONTEXT"
docker build --pull=false --tag "$LEGACY_IMAGE" "$LEGACY_CONTEXT" >/dev/null
cat >"$CWA_FIXTURE" <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth_path = self.path == "/ajax/emailstat"
        authenticated = "session=valid" in self.headers.get("Cookie", "")
        if auth_path and not authenticated:
            body = b'{"error":"unauthorized"}'
            status = 401
        else:
            body = b"[]" if auth_path else b"ok"
            status = 200
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json" if self.path == "/ajax/emailstat" else "text/plain",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_args):
        pass
HTTPServer(("0.0.0.0", 8083), Handler).serve_forever()
PY
chmod 0644 "$CWA_FIXTURE"
docker run -d --name "$CWA_CONTAINER" --network "$CWA_NETWORK" \
    --read-only --tmpfs /tmp \
    --mount "type=bind,src=${CWA_FIXTURE},dst=/app/cwa-fixture.py,readonly" \
    --entrypoint python "$CWA_IMAGE" /app/cwa-fixture.py >/dev/null
for _ in $(seq 1 30); do
    if docker exec "$CWA_CONTAINER" python -c \
        'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8083/", timeout=1).close()' \
        >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
docker exec "$CWA_CONTAINER" python -c \
    'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8083/", timeout=1).close()'

test -r "$KAVITA_FIXTURE"
docker network create "$KAVITA_NETWORK" >/dev/null
docker tag "$SMOKE_IMAGE" "$KAVITA_IMAGE"
docker run -d --name "$KAVITA_CONTAINER" --network "$KAVITA_NETWORK" \
    --read-only --tmpfs /tmp \
    --mount "type=bind,src=${KAVITA_FIXTURE},dst=/app/test_kavita_auth_fixture.py,readonly" \
    --entrypoint python "$KAVITA_IMAGE" /app/test_kavita_auth_fixture.py >/dev/null
for _ in $(seq 1 30); do
    if docker exec "$KAVITA_CONTAINER" python -c \
        'import socket; socket.create_connection(("127.0.0.1", 5000), 1).close()' \
        >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
docker exec "$KAVITA_CONTAINER" python -c \
    'import socket; socket.create_connection(("127.0.0.1", 5000), 1).close()'

# Fresh lifecycle: install, state-loss adoption, conservative uninstall, and
# reinstall with preserved data and archived terminal evidence.
FRESH_STATE="$ROOT_DIR/fresh-state"
FRESH_DATA="$ROOT_DIR/fresh-data"
FRESH_BACKUP="$ROOT_DIR/fresh-backup"
FRESH_ENV="$ROOT_DIR/fresh.env"
FRESH_PORT="$(free_port)"
write_environment \
    "$FRESH_ENV" "$FRESH_INSTALL" "$FRESH_PORT" \
    "$FRESH_STATE" "$FRESH_DATA" "$FRESH_BACKUP"

./btctl plan --env "$FRESH_ENV" --json >/dev/null
./btctl install --env "$FRESH_ENV" --yes --json >/dev/null
assert_doctor "$FRESH_ENV"
test "$(docker inspect "${FRESH_INSTALL}-api" --format '{{.Config.User}}')" = "101:102"
test -z "$(docker port "${FRESH_INSTALL}-api")"
curl -fsS "http://127.0.0.1:${FRESH_PORT}/bt-api/ping" | grep -q '"status":"ok"'

# Install the pinned Kavita connector concurrently, exercise its native-token
# exchange, then remove only its owned resources. CWA must remain healthy and
# both readers/data directories must remain external throughout.
KAVITA_STATE="$ROOT_DIR/kavita-state"
KAVITA_DATA="$ROOT_DIR/kavita-data"
KAVITA_BACKUP="$ROOT_DIR/kavita-backup"
KAVITA_ENV="$ROOT_DIR/kavita.env"
KAVITA_PORT="$(free_port)"
KAVITA_HEADERS="$ROOT_DIR/kavita-session.headers"
write_kavita_environment \
    "$KAVITA_ENV" "$KAVITA_PORT" \
    "$KAVITA_STATE" "$KAVITA_DATA" "$KAVITA_BACKUP"

./btctl plan --env "$KAVITA_ENV" --json >/dev/null
./btctl install --env "$KAVITA_ENV" --yes --json >/dev/null
assert_doctor "$KAVITA_ENV"
assert_doctor "$FRESH_ENV"
test "$(docker inspect "${KAVITA_INSTALL}-api" --format '{{.Config.User}}')" = "101:102"
test -z "$(docker port "${KAVITA_INSTALL}-api")"
curl -fsS "http://127.0.0.1:${KAVITA_PORT}/library/7/series/42/book/99" \
    | grep -q '<div class="book-content">'
curl -fsS -D "$KAVITA_HEADERS" -o /dev/null -X POST \
    -H 'Authorization: Bearer container-smoke-kavita-access' \
    -H 'User-Agent: Lifecycle-Smoke-Browser/1.0' \
    -H "Origin: http://127.0.0.1:${KAVITA_PORT}" \
    "http://127.0.0.1:${KAVITA_PORT}/bt-api/session"
KAVITA_SESSION_COOKIE="$(awk 'BEGIN{IGNORECASE=1} /^set-cookie:/ {sub(/^[^:]*:[[:space:]]*/, ""); split($0, parts, ";"); if (parts[1] ~ /^bt-session=/) print parts[1]}' "$KAVITA_HEADERS" | tail -n 1 | tr -d '\r')"
if [[ ! "$KAVITA_SESSION_COOKIE" =~ ^bt-session=[A-Za-z0-9_-]{32,128}$ ]]; then
    echo "Kavita lifecycle smoke did not receive an opaque session" >&2
    exit 1
fi
curl -fsS \
    -H "Cookie: ${KAVITA_SESSION_COOKIE}" \
    -H 'User-Agent: Lifecycle-Smoke-Browser/1.0' \
    "http://127.0.0.1:${KAVITA_PORT}/bt-api/metrics" \
    | grep -q '"total_requests"'
test -e "$FRESH_DATA/reader_session_key"
test -e "$KAVITA_DATA/reader_session_key"
./btctl uninstall --env "$KAVITA_ENV" --yes --json | python3 -c \
    'import json,sys; assert json.load(sys.stdin)["status"] == "uninstalled"'
test ! -e "$KAVITA_DATA/reader_session_key"
test -e "$KAVITA_DATA/translations.db"
test "$(docker inspect "$KAVITA_CONTAINER" --format '{{.State.Running}}')" = "true"
test "$(docker inspect "$CWA_CONTAINER" --format '{{.State.Running}}')" = "true"
assert_doctor "$FRESH_ENV"

# Split installs regenerate their connector id, but tenants follow tenant_key.
fresh_tenant_key="$(sha256sum "$FRESH_DATA/tenant_key" | cut -d' ' -f1)"
rm -- "$FRESH_STATE/state.json"
./btctl adopt --env "$FRESH_ENV" --json | python3 -c \
    'import json,sys; assert json.load(sys.stdin)["status"] == "adopted"'
assert_doctor "$FRESH_ENV"
./btctl uninstall --env "$FRESH_ENV" --yes --json | python3 -c \
    'import json,sys; assert json.load(sys.stdin)["status"] == "uninstalled"'
test -d "$FRESH_DATA"
test "$(docker inspect "$CWA_CONTAINER" --format '{{.State.Running}}')" = "true"

./btctl install --env "$FRESH_ENV" --yes --json >/dev/null
assert_doctor "$FRESH_ENV"
test "$(find "$FRESH_STATE/history" -type f -name '*-uninstalled.json' | wc -l)" = "1"
test "$(sha256sum "$FRESH_DATA/tenant_key" | cut -d' ' -f1)" = "$fresh_tenant_key"
./btctl uninstall --env "$FRESH_ENV" --yes --json >/dev/null

# Real offline migration: an exact v2.1.4 container and v1 database become a
# split v2.2 runtime, roll back to the healthy preserved legacy container, and
# then re-upgrade from the journal without overwriting the v2 data target.
LEGACY_DATA="$ROOT_DIR/legacy-data"
mkdir -m 0700 "$LEGACY_DATA"
python3 - "$LEGACY_DATA/translations.db" <<'PY'
import datetime
import sqlite3
import sys

database = sqlite3.connect(sys.argv[1])
database.execute("""CREATE TABLE translations (
    cache_key TEXT PRIMARY KEY,
    source_text TEXT NOT NULL,
    source_lang TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 1
)""")
database.execute(
    "INSERT INTO translations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    (
        "legacy-key", "legacy source", "English", "Spanish",
        "legacy target", "legacy-model",
        datetime.datetime.now(datetime.timezone.utc).isoformat(), 1,
    ),
)
database.commit()
assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
database.close()
PY

docker run -d --name "$LEGACY_CONTAINER" \
    --mount "type=bind,src=${LEGACY_DATA},dst=/app/data" \
    --health-cmd 'python -c "raise SystemExit(0)"' \
    --health-interval 1s --health-timeout 2s --health-retries 10 \
    --entrypoint python "$LEGACY_IMAGE" \
    -c 'import time; time.sleep(3600)' >/dev/null

MIGRATION_STATE="$ROOT_DIR/migration-state"
MIGRATION_DATA="$ROOT_DIR/migration-data"
MIGRATION_BACKUP="$ROOT_DIR/migration-backup"
MIGRATION_ENV="$ROOT_DIR/migration.env"
MIGRATION_PORT="$(free_port)"
write_environment \
    "$MIGRATION_ENV" "$MIGRATION_INSTALL" "$MIGRATION_PORT" \
    "$MIGRATION_STATE" "$MIGRATION_DATA" "$MIGRATION_BACKUP" "$LEGACY_DATA"

./btctl upgrade --env "$MIGRATION_ENV" --yes --json >/dev/null
assert_doctor "$MIGRATION_ENV"
test "$(docker inspect "$LEGACY_CONTAINER" --format '{{.State.Status}}')" = "exited"
assert_v2_database "${MIGRATION_INSTALL}-api"

./btctl rollback --env "$MIGRATION_ENV" --yes --json | python3 -c \
    'import json,sys; assert json.load(sys.stdin)["status"] == "rolled_back"'
test "$(docker inspect "$LEGACY_CONTAINER" --format '{{.State.Status}}')" = "running"
test "$(docker inspect "$LEGACY_CONTAINER" --format '{{.State.Health.Status}}')" = "healthy"
test -d "$MIGRATION_DATA"

./btctl upgrade --env "$MIGRATION_ENV" --yes --json >/dev/null
assert_doctor "$MIGRATION_ENV"
test "$(docker inspect "$LEGACY_CONTAINER" --format '{{.State.Status}}')" = "exited"
assert_v2_database "${MIGRATION_INSTALL}-api"
python3 - "$MIGRATION_STATE/migration-v214.json" <<'PY'
import json
import sys
from pathlib import Path

journal = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert journal["status"] == "upgraded", journal
assert journal["attempt"] == 2, journal
PY

echo "btctl CWA/Kavita isolation, adopt, uninstall, reinstall, migration, rollback, and reupgrade: OK"

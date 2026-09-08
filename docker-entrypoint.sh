#!/bin/sh
# Non-root runtime dispatcher for API, proxy, or legacy combined mode.
set -eu
umask 027

# Normalize ports: support PORT, API_PORT, BT_API_PORT and BT_PROXY_PORT, PROXY_PORT
PORT="${PORT:-${API_PORT:-${BT_API_PORT:-8390}}}"
BT_PROXY_PORT="${BT_PROXY_PORT:-${PROXY_PORT:-8080}}"
API_PORT="$PORT"
PROXY_PORT="$BT_PROXY_PORT"
BT_ROLE="${BT_ROLE:-auto}"
BT_UI_VERSION="$(cat /app/VERSION 2>/dev/null || echo dev)"

# Reader upstream normalization: support CWA_URL, CALIBRE_WEB_URL, KAVITA_URL, etc.
CWA_URL="${CWA_URL:-${CALIBRE_WEB_URL:-${CALIBRE_URL:-${CWA_UPSTREAM:-${BT_CWA_READER_UPSTREAM:-}}}}}"
KAVITA_URL="${KAVITA_URL:-${KAVITA_UPSTREAM:-${BT_KAVITA_READER_UPSTREAM:-}}}"
BT_READER_UPSTREAM="${BT_READER_UPSTREAM:-${CWA_UPSTREAM:-${CWA_URL:-${KAVITA_URL:-}}}}"

# Auto-detect reader type if not explicitly configured
if [ -z "${BT_READER_TYPE:-}" ]; then
    if [ -n "$KAVITA_URL" ] && [ -z "$CWA_URL" ]; then
        BT_READER_TYPE="kavita"
    else
        BT_READER_TYPE="cwa"
    fi
fi

# Local LLM URL aliases & normalization
BT_LOCAL_URL="${BT_LOCAL_URL:-${LOCAL_LLM_URL:-${LOCAL_URL:-${VLLM_URL:-${OLLAMA_URL:-}}}}}"
if [ -n "$BT_LOCAL_URL" ]; then
    case "$BT_LOCAL_URL" in
        */v1/chat/completions) ;;
        */v1/chat/completions/) BT_LOCAL_URL="${BT_LOCAL_URL%/}" ;;
        */v1) BT_LOCAL_URL="${BT_LOCAL_URL}/chat/completions" ;;
        */v1/) BT_LOCAL_URL="${BT_LOCAL_URL}chat/completions" ;;
        *:[0-9]*|*:[0-9]*/) BT_LOCAL_URL="${BT_LOCAL_URL%/}/v1/chat/completions" ;;
    esac
    export BT_LOCAL_URL
fi

# Auto-derive auth endpoints if missing
if [ "${BT_AUTH_MODE:-token}" = "cwa_session" ] && [ -z "${BT_CWA_AUTH_URL:-}" ] && [ -n "$BT_READER_UPSTREAM" ]; then
    BT_CWA_AUTH_URL="${BT_READER_UPSTREAM}/ajax/emailstat"
    export BT_CWA_AUTH_URL
fi

if [ "${BT_AUTH_MODE:-token}" = "reader_session" ] && [ -z "${BT_READER_AUTH_URL:-}" ] && [ -n "$BT_READER_UPSTREAM" ]; then
    if [ "$BT_READER_TYPE" = "kavita" ]; then
        BT_READER_AUTH_URL="${BT_READER_UPSTREAM}/api/Account"
    else
        BT_READER_AUTH_URL="${BT_READER_UPSTREAM}/ajax/emailstat"
    fi
    export BT_READER_AUTH_URL
fi


export PORT API_PORT BT_API_PORT BT_PROXY_PORT PROXY_PORT BT_ROLE BT_UI_VERSION     BT_READER_UPSTREAM BT_READER_TYPE CWA_URL KAVITA_URL

if [ "$BT_ROLE" = "auto" ]; then
    if [ -n "${BT_READER_UPSTREAM:-${CWA_UPSTREAM:-}}" ]; then
        BT_ROLE="all"
    else
        BT_ROLE="api"
    fi
    export BT_ROLE
fi

case "$BT_ROLE" in
    api|proxy|all|hub) ;;
    *)
        echo "[entrypoint] ERROR: BT_ROLE must be api, proxy, all, hub, or auto" >&2
        exit 64
        ;;
esac

if [ "$BT_ROLE" = "hub" ]; then
    exec python /app/hub_runtime.py
fi

if [ "$BT_ROLE" = "all" ]; then
    export BT_TRUSTED_PROXIES="${BT_TRUSTED_PROXIES:-127.0.0.1/32}"
fi

check_data_dir() {
    if [ ! -d /app/data ]; then
        echo "[entrypoint] ERROR: /app/data is missing" >&2
        exit 78
    fi
    data_mode="$(stat -c %a /app/data 2>/dev/null || true)"
    case "$data_mode" in
        700|750|2700|2750) ;;
        *)
            echo "[entrypoint] ERROR: /app/data must have private mode 0700 or 2750" >&2
            exit 78
            ;;
    esac
    probe="/app/data/.write-probe.$$"
    if ! (umask 027 && : > "$probe") 2>/dev/null; then
        echo "[entrypoint] ERROR: /app/data must be writable by uid 101 gid 102" >&2
        exit 78
    fi
    rm -f "$probe"
}

validate_api_auth() {
    mode="${BT_AUTH_MODE:-token}"
    case "$mode" in
        token)
            if [ -z "${BT_API_TOKEN:-}" ]; then
                echo "[entrypoint] ERROR: BT_API_TOKEN is required when BT_AUTH_MODE=token" >&2
                exit 78
            fi
            ;;
        cwa_session)
            if [ -z "${BT_CWA_AUTH_URL:-}" ]; then
                echo "[entrypoint] ERROR: BT_CWA_AUTH_URL is required when BT_AUTH_MODE=cwa_session" >&2
                exit 78
            fi
            ;;
        reader_session)
            if [ -z "${BT_READER_TYPE:-}" ] \
                || [ -z "${BT_READER_AUTH_URL:-}" ] \
                || [ -z "${BT_READER_VERSION:-}" ] \
                || [ -z "${BT_READER_CONNECTOR_ID:-}" ] \
                || [ -z "${BT_PUBLIC_ORIGIN:-}" ] \
                || [ -z "${BT_SESSION_KEY_PATH:-}" ]; then
                echo "[entrypoint] ERROR: reader_session configuration is incomplete" >&2
                exit 78
            fi
            ;;
        forwarded)
            if [ -z "${BT_IDENTITY_TRUSTED_PROXIES:-}" ]; then
                echo "[entrypoint] ERROR: BT_IDENTITY_TRUSTED_PROXIES is required when BT_AUTH_MODE=forwarded" >&2
                exit 78
            fi
            ;;
        disabled)
            if [ "${BT_ALLOW_INSECURE_AUTH:-false}" != "true" ]; then
                echo "[entrypoint] ERROR: disabled auth requires BT_ALLOW_INSECURE_AUTH=true" >&2
                exit 78
            fi
            echo "[entrypoint] WARNING: authentication disabled; development only" >&2
            ;;
        *)
            echo "[entrypoint] ERROR: unsupported BT_AUTH_MODE: $mode" >&2
            exit 78
            ;;
    esac
}

initialize_cache() {
    if ! python -c 'from cache import init_db; init_db()' >/dev/null 2>&1; then
        echo "[entrypoint] ERROR: cache database initialization failed" >&2
        exit 78
    fi
}

configure_proxy() {
    BT_READER_UPSTREAM="${BT_READER_UPSTREAM:-${CWA_UPSTREAM:-}}"
    BT_READER_TYPE="${BT_READER_TYPE:-cwa}"
    if [ -z "$BT_READER_UPSTREAM" ]; then
        echo "[entrypoint] ERROR: BT_READER_UPSTREAM is required for the proxy role" >&2
        exit 64
    fi
    BT_API_UPSTREAM="${BT_API_UPSTREAM:-http://127.0.0.1:${PORT}}"
    BT_CWA_MAX_BODY_SIZE="${BT_CWA_MAX_BODY_SIZE:-2g}"
    # CWA's configurable reverse-proxy login header is an authentication
    # credential. The injection proxy is not an identity authority, so it
    # always removes that client-supplied header before forwarding to CWA.
    BT_CWA_IDENTITY_HEADER="${BT_CWA_IDENTITY_HEADER:-Remote-User}"
    export BT_API_UPSTREAM BT_CWA_MAX_BODY_SIZE BT_CWA_IDENTITY_HEADER \
        BT_READER_UPSTREAM BT_READER_TYPE

    mkdir -p \
        /tmp/nginx/client_temp \
        /tmp/nginx/proxy_temp \
        /tmp/nginx/fastcgi_temp \
        /tmp/nginx/uwsgi_temp \
        /tmp/nginx/scgi_temp
    python /app/proxy/render_config.py \
        /app/proxy/nginx.conf.template /tmp/nginx/proxy.conf \
        /tmp/nginx/browser-config.json
    nginx -t -c /app/proxy/nginx-main.conf -e /dev/stderr
}

start_api() {
    gunicorn --bind "0.0.0.0:${PORT}" --workers 1 --threads 8 \
        --worker-tmp-dir /dev/shm \
        --timeout 120 server:app
}

start_proxy() {
    nginx -c /app/proxy/nginx-main.conf -e /dev/stderr -g 'daemon off;'
}

case "$BT_ROLE" in
    api)
        check_data_dir
        validate_api_auth
        initialize_cache
        echo "[entrypoint] API role on :${PORT}"
        exec gunicorn --bind "0.0.0.0:${PORT}" --workers 1 --threads 8 \
            --worker-tmp-dir /dev/shm \
            --timeout 120 server:app
        ;;
    proxy)
        configure_proxy
        echo "[entrypoint] proxy role on :${BT_PROXY_PORT} -> ${BT_READER_UPSTREAM}"
        exec nginx -c /app/proxy/nginx-main.conf -e /dev/stderr -g 'daemon off;'
        ;;
esac

# Legacy one-container compatibility. It is non-root, but the recommended
# topology uses two role-specific containers so each has its own health and
# restart lifecycle.
check_data_dir
validate_api_auth
initialize_cache
configure_proxy
echo "[entrypoint] combined role: API :${PORT}, proxy :${BT_PROXY_PORT}"
start_api &
API_PID=$!
start_proxy &
NGINX_PID=$!

stop_children() {
    kill -TERM "$API_PID" "$NGINX_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
    wait "$NGINX_PID" 2>/dev/null || true
}

shutdown() {
    stop_children
    exit 0
}
trap shutdown TERM INT

while :; do
    if ! kill -0 "$API_PID" 2>/dev/null; then
        echo "[entrypoint] gunicorn exited; stopping combined container" >&2
        stop_children
        exit 1
    fi
    if ! kill -0 "$NGINX_PID" 2>/dev/null; then
        echo "[entrypoint] nginx exited; stopping combined container" >&2
        stop_children
        exit 1
    fi
    sleep 5 &
    wait $! || true
done

"""
book-translator — Flask microservice for ebook paragraph translation.
Runs on port 8390. Frontend (CWA overlay) calls this service.
"""
import logging
import json
import copy
import hashlib
import math
import os
import re
import time
import threading
import uuid
import hmac
import ipaddress
import socket
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit
from flask import Flask, request, jsonify, Response, stream_with_context
from werkzeug.exceptions import HTTPException

from tts import TTS_SERVICE, resolve_kokoro_voice
from auth import (
    AuthRejected,
    AuthUnavailable,
    CwaSessionBinding,
    RequestAuthenticator,
)
from translator import (
    translate_text, translate_text_stream, translate_batch_detailed as translate_batch,
    check_backend_health,
    BT_MAX_UPSTREAM_RESPONSE_BYTES,
    BT_UPSTREAM_QUEUE_TIMEOUT, SegmentProtocolError,
    ProviderUnavailableError, create_work_budget, model_for_provider,
    cache_lookup_backends, translation_groups, batch_cache_contract,
    single_cache_contract, singleflight_stats, BatchRecoveryTracker,
    RECOVERY_METRIC_NAMES,
    estimate_source_tokens, provider_call_stats, BT_BATCH_SIZE,
    BT_REQUEST_MAX_ATTEMPTS,
    _reset_provider_call_stats_for_tests,
    provider_policy,
    initialize_provider_configuration,
)
from epub_export import build_epub, epub_filename
from work_budget import WorkBudget, WorkBudgetExceeded
from cache import (
    CacheScope, get_cached, get_cached_many, put_cache, put_cache_many,
    record_cache_hit, get_cache_stats, cleanup_old_entries,
)
import feedback as feedback_store
import glossary as glossary_store


def _cache_scope(
    *,
    tenant: str,
    book_id: str,
    chapter_id: str,
    context_hash: str,
    provider: str,
    model: str,
    prompt_hash: str,
    protocol_version: str,
) -> CacheScope:
    return CacheScope(
        tenant=tenant,
        book_id=book_id,
        chapter_id=chapter_id,
        context_hash=context_hash,
        provider=provider,
        model=model,
        prompt_hash=prompt_hash,
        protocol_version=protocol_version,
    )


def _operation_namespace(tenant: str, book_id: str, chapter_id: str) -> str:
    """Opaque tenant/book/chapter boundary for in-process singleflight."""
    return hashlib.sha256(
        "\0".join((tenant, book_id, chapter_id)).encode("utf-8")
    ).hexdigest()


def _request_glossary(tenant: str, book_id: str) -> list[tuple[str, str]]:
    """Load stored glossary terms for prompt injection (non-fatal)."""
    try:
        entries = glossary_store.list_entries(tenant, book_id)
    except Exception as exc:
        log.warning(
            "Glossary lookup failed (non-fatal) error_type=%s",
            type(exc).__name__,
        )
        return []
    return [(entry["source"], entry["target"]) for entry in entries]


def _cache_lookup(
    text: str,
    source_lang: str,
    target_lang: str,
    *,
    tenant: str = "legacy-anonymous",
    book_id: str = "unscoped",
    chapter_id: str = "unscoped",
    allow_cloud_fallback: bool = False,
) -> str | None:
    """Probe exact single-translation and 1-item batch contracts in provider failover order."""
    # The glossary is server-owned state for this tenant/book, loaded here so
    # the probe always uses the exact prompt contract the miss will translate
    # under. A lookup failure degrades to the no-glossary contract (non-fatal).
    glossary = _request_glossary(tenant, book_id)
    contracts = [single_cache_contract(source_lang, target_lang, glossary)]
    try:
        contracts.append(
            batch_cache_contract([text], [0], source_lang, target_lang, glossary)
        )
    except Exception:
        pass
    for provider, model in cache_lookup_backends(
        allow_cloud_fallback=allow_cloud_fallback
    ):
        for contract in contracts:
            scope = _cache_scope(
                tenant=tenant,
                book_id=book_id,
                chapter_id=chapter_id,
                context_hash=contract.context_hash,
                provider=provider,
                model=model,
                prompt_hash=contract.prompt_hash,
                protocol_version=contract.protocol_version,
            )
            try:
                hit = get_cached(text, source_lang, target_lang, scope=scope)
            except Exception as exc:
                log.warning(
                    "Cache probe failed (non-fatal) error_type=%s",
                    type(exc).__name__,
                )
                continue
            if hit is not None:
                return hit
    return None

# Single version source: the VERSION file (also stamped into cache-bust query
# strings by the proxy). Falls back to "dev" for odd working directories.
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")) as _vf:
        __version__ = _vf.read().strip() or "dev"
except OSError:
    __version__ = "dev"

# Operator/shared-secret credential. Production authentication is selected by
# BT_AUTH_MODE; its safe default is token and therefore requires BT_API_TOKEN.
# This module-level value remains the destructive-operation credential too.
API_TOKEN = os.environ.get("BT_API_TOKEN", "")

# Request-size caps: one request must not be able to trigger unbounded LLM work
# (GPU starvation locally, an open-ended bill on cloud APIs). Oversized input is
# rejected with 413 rather than truncated — silent truncation would corrupt text.
BT_MAX_BATCH_PARAGRAPHS = int(os.environ.get("BT_MAX_BATCH_PARAGRAPHS", "50"))
if not 1 <= BT_BATCH_SIZE <= 50:
    raise ValueError("BT_BATCH_SIZE must be an integer from 1 to 50")
if not 1 <= BT_MAX_BATCH_PARAGRAPHS <= 1000:
    raise ValueError(
        "BT_MAX_BATCH_PARAGRAPHS must be an integer from 1 to 1000"
    )
if BT_BATCH_SIZE > BT_MAX_BATCH_PARAGRAPHS:
    raise ValueError(
        "BT_BATCH_SIZE must not exceed BT_MAX_BATCH_PARAGRAPHS"
    )
# A max-size request must be executable within the attempt budget even
# in the clean path with no retries.
_min_attempts = -(-BT_MAX_BATCH_PARAGRAPHS // BT_BATCH_SIZE)
if BT_REQUEST_MAX_ATTEMPTS < _min_attempts:
    raise ValueError(
        "BT_REQUEST_MAX_ATTEMPTS too small for BT_MAX_BATCH_PARAGRAPHS / "
        f"BT_BATCH_SIZE: need at least {_min_attempts} attempts"
    )
BT_MAX_PARAGRAPH_CHARS = int(os.environ.get("BT_MAX_PARAGRAPH_CHARS", "8000"))
BT_CACHE_SCOPE_MAX_CHARS = int(os.environ.get("BT_CACHE_SCOPE_MAX_CHARS", "512"))

# EPUB export caps: one export packages a chapter of already-translated
# text (no LLM work), so the paragraph count may exceed the per-request
# translation batch while per-paragraph and total-character backstops still
# bound ZIP build time and memory. Oversized input is rejected with 413.
BT_MAX_EXPORT_PARAGRAPHS = int(os.environ.get("BT_MAX_EXPORT_PARAGRAPHS", "500"))
BT_MAX_EXPORT_TITLE_CHARS = int(os.environ.get("BT_MAX_EXPORT_TITLE_CHARS", "200"))
BT_MAX_EXPORT_TOTAL_CHARS = int(
    os.environ.get("BT_MAX_EXPORT_TOTAL_CHARS", str(1024 * 1024))
)
if not 1 <= BT_MAX_EXPORT_PARAGRAPHS <= 5000:
    raise ValueError(
        "BT_MAX_EXPORT_PARAGRAPHS must be an integer from 1 to 5000"
    )
if not 1 <= BT_MAX_EXPORT_TITLE_CHARS <= 500:
    raise ValueError(
        "BT_MAX_EXPORT_TITLE_CHARS must be an integer from 1 to 500"
    )
if not 1024 <= BT_MAX_EXPORT_TOTAL_CHARS <= 10 * 1024 * 1024:
    raise ValueError(
        "BT_MAX_EXPORT_TOTAL_CHARS must be an integer from 1024 to 10485760"
    )

# Global request-size cap (defence in depth). The per-field caps above check
# the *parsed* content; MAX_CONTENT_LENGTH is a hard backstop at the WSGI
# layer that rejects a 10 MB JSON before we even start parsing. With our
# defaults (50 paragraphs × 8000 chars + overhead) the per-request ceiling is
# ~400 KB; the 2 MB default here gives ~5× headroom for a single oversized
# paragraph and rejects a 10 MB body long before the per-field check fires.
# Operators behind a slow link can lower it; operators on a research cluster
# translating longer paragraphs can raise it (or lower BT_MAX_PARAGRAPH_CHARS).
BT_MAX_CONTENT_LENGTH = int(os.environ.get("BT_MAX_CONTENT_LENGTH", str(2 * 1024 * 1024)))

# Rate-limit key: request.remote_addr by default. Behind a reverse proxy every
# client shares the proxy's address, so opt in to X-Forwarded-For ONLY when
# the proxy is trusted to set it (never trust it from direct clients).
#
# BT_TRUST_PROXY is a boolean switch: when true, X-Forwarded-For's first hop
# becomes the rate-limit key. The safer BT_TRUSTED_PROXIES is a comma-
# separated list of CIDRs/ips that remote_addr must match before X-Forwarded-
# -For is honored. Set BT_TRUSTED_PROXIES (e.g. "127.0.0.1/32,::1/128" for
# a local nginx, or "10.0.0.0/8" for a private-network reverse proxy) to
# prevent spoofing: a client on the LAN can otherwise send an arbitrary
# X-Forwarded-For header and bypass the rate limiter per request.
#
# Precedence:
#   - BT_TRUSTED_PROXIES is set  -> honor X-Forwarded-For IFF remote_addr is
#                                   in the list
#   - BT_TRUST_PROXY=true         -> honor X-Forwarded-For from any peer
#                                   (legacy / dev only; not safe in prod)
#   - otherwise                   -> use remote_addr
BT_TRUST_PROXY = os.environ.get("BT_TRUST_PROXY", "false").lower() in ("1", "true", "yes")
BT_TRUSTED_PROXIES = {
    p.strip() for p in os.environ.get("BT_TRUSTED_PROXIES", "").split(",") if p.strip()
}
_TRUSTED_PROXY_NETS = [ipaddress.ip_network(c, strict=False) for c in BT_TRUSTED_PROXIES]
BT_TRUSTED_PROXY_HOST = os.environ.get("BT_TRUSTED_PROXY_HOST", "").strip()
if BT_TRUSTED_PROXY_HOST and not re.fullmatch(
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?",
    BT_TRUSTED_PROXY_HOST,
):
    raise ValueError("BT_TRUSTED_PROXY_HOST must be one exact DNS hostname")
_trusted_proxy_host_lock = threading.Lock()
_trusted_proxy_host_cache: tuple[str, float, frozenset] = (
    "",
    0.0,
    frozenset(),
)

# ── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("book-translator.server")

# Fail during process startup when the selected auth authority is incomplete.
# BT_AUTH_MODE=disabled remains available only as an explicit development/test
# choice; it is never the default.
AUTHENTICATOR = RequestAuthenticator.from_environment()
initialize_provider_configuration()
# Non-secret process generation. A managed provider reconfigure replaces the
# API process, so an open reader can prove it learned policy from this exact
# provider generation without exposing provider/model/endpoint identity.
PROVIDER_POLICY_GENERATION = uuid.uuid4().hex

app = Flask(__name__)

# Reject oversize request bodies at the WSGI layer (defence in depth — the
# per-field caps in the route handlers are the second backstop). Returning
# 413 here means a 10 MB JSON gets rejected before Flask even parses it.
app.config["MAX_CONTENT_LENGTH"] = BT_MAX_CONTENT_LENGTH


def _request_cache_namespace(data: dict) -> tuple[str, str, str]:
    """Return server-owned tenant plus bounded client book/chapter metadata.

    ``tenant`` is never accepted from JSON.  The authentication middleware
    owns it; the legacy value is temporary compatibility until an explicit
    production auth mode is selected.  Book/chapter identifiers affect only a
    one-way cache hash and are never logged or stored verbatim.
    """
    tenant = getattr(request, "auth_subject", None) or "legacy-anonymous"
    values = []
    for field in ("book_id", "chapter_id"):
        value = data.get(field, "unscoped")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"'{field}' must be a non-empty string")
        value = value.strip()
        if len(value) > BT_CACHE_SCOPE_MAX_CHARS:
            raise ValueError(
                f"'{field}' exceeds the {BT_CACHE_SCOPE_MAX_CHARS}-character limit"
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError(f"'{field}' contains control characters")
        values.append(value)
    return tenant, values[0], values[1]


def _cloud_fallback_consent(data: dict) -> bool:
    """Validate the additive per-request privacy decision at the API edge."""
    if "allow_cloud_fallback" not in data:
        return False
    consent = data["allow_cloud_fallback"]
    if type(consent) is not bool:
        raise ValueError("'allow_cloud_fallback' must be a boolean")
    return consent


def _stale_provider_policy(data: dict) -> bool:
    """Reject official-reader requests bound to an obsolete locality policy."""
    if "provider_policy" not in data:
        # Token-mode API clients retain the pre-existing optional contract.
        # Managed browser modes fail closed so an already-open old reader
        # cannot bypass the generation boundary after an API replacement.
        return AUTHENTICATOR.mode in {
            "cwa_session", "reader_session", "forwarded"
        }
    policy = data["provider_policy"]
    if (
        not isinstance(policy, dict)
        or set(policy) != {"primary", "fallback", "generation"}
        or policy.get("primary") not in {"local", "remote"}
        or policy.get("fallback") not in {None, "local", "remote"}
        or not isinstance(policy.get("generation"), str)
        or len(policy.get("generation")) != 32
    ):
        raise ValueError("'provider_policy' must be an exact locality policy")
    current = {
        **provider_policy(),
        "generation": PROVIDER_POLICY_GENERATION,
    }
    return policy != current

# ── Language validation (H7) ────────────────────────────────────────────────
# The selectable set mirrors Gemma 4's pre-training coverage (top-10 most
# spoken + Gemma's benchmarked and wider language groups). Must stay in sync
# with TOP_LANGUAGES/MORE_LANGUAGES in static/translator.js (test-enforced).

VALID_LANGUAGES = {
    "Afrikaans", "Albanian", "Amharic", "Arabic", "Aymara", "Basque",
    "Bengali", "Bosnian", "Bulgarian", "Burmese", "Catalan", "Cebuano",
    "Chewa", "Chinese", "Chinese (Traditional)", "Croatian", "Czech",
    "Danish", "Dutch", "English", "Esperanto", "Estonian", "Finnish",
    "French", "Gaelic", "Galician", "Ganda", "German", "Greek", "Guarani",
    "Gujarati", "Hausa", "Hawaiian", "Hebrew", "Hindi", "Hungarian",
    "Icelandic", "Igbo", "Indonesian", "Italian", "Japanese", "Javanese",
    "Kannada", "Kazakh", "Khmer", "Korean", "Kyrgyz", "Lao", "Latin",
    "Latvian", "Lingala", "Lithuanian", "Macedonian", "Maithili",
    "Malagasy", "Malay", "Malayalam", "Maori", "Marathi", "Mongolian",
    "Nahuatl", "Navajo", "Nepali", "Norwegian", "Odia", "Oromo", "Pashto",
    "Persian", "Polish", "Portuguese", "Punjabi", "Quechua", "Romanian",
    "Russian", "Samoan", "Serbian", "Shona", "Sindhi", "Sinhala", "Slovak",
    "Slovenian", "Somali", "Spanish", "Sundanese", "Swahili", "Swedish",
    "Tagalog", "Tajik", "Tamil", "Telugu", "Thai", "Tibetan", "Turkish",
    "Turkmen", "Ukrainian", "Urdu", "Uzbek", "Vietnamese", "Welsh",
    "Xhosa", "Yoruba", "Zulu"
}


def _validate_languages(source_lang: str, target_lang: str):
    """Return error string if languages are invalid, else None."""
    if not isinstance(source_lang, str) or not isinstance(target_lang, str):
        return "'source_lang' and 'target_lang' must be strings"

    invalid = []
    if source_lang not in VALID_LANGUAGES:
        invalid.append(f"source_lang '{source_lang}'")
    if target_lang not in VALID_LANGUAGES:
        invalid.append(f"target_lang '{target_lang}'")
    if invalid:
        return f"Invalid language(s): {', '.join(invalid)}. Valid: {sorted(VALID_LANGUAGES)}"
    return None


def _has_invalid_unicode(value: str) -> bool:
    """JSON can decode lone UTF-16 surrogates that UTF-8 cannot represent."""
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return True
    return False


# ── CORS whitelist (H5) ─────────────────────────────────────────────────────
# Configure with BT_ALLOWED_ORIGINS (comma-separated exact origins, e.g.
# "https://books.example.com,http://mynas:8083"). BT_ALLOW_PRIVATE_LAN
# (default true) additionally allows localhost and RFC1918 addresses on any
# port — the common self-hosted case. Note: in proxy-injection mode the overlay
# is same-origin and CORS never comes into play.

def _validate_cors_origin(origin: str) -> str:
    """Require an exact serialized HTTP origin, never a path or wildcard."""
    if (
        not origin
        or origin != origin.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in origin)
    ):
        raise ValueError("BT_ALLOWED_ORIGINS contains an invalid origin")
    try:
        parsed = urlsplit(origin)
        parsed.port
    except ValueError as exc:
        raise ValueError("BT_ALLOWED_ORIGINS contains an invalid origin") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in parsed.hostname)
    ):
        raise ValueError("BT_ALLOWED_ORIGINS must contain exact http(s) origins")
    return origin


ALLOWED_ORIGINS = {
    _validate_cors_origin(o.strip())
    for o in os.environ.get(
        "BT_ALLOWED_ORIGINS", "http://localhost:8083,http://localhost:8383"
    ).split(",")
    if o.strip()
}

# Auto-register reader and public origins into CORS whitelist
def _extract_origin_candidate(url_candidate: str) -> str | None:
    if not url_candidate or not isinstance(url_candidate, str):
        return None
    candidate = url_candidate.strip()
    if not candidate:
        return None
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme in ("http", "https") and parsed.hostname:
            port_part = f":{parsed.port}" if parsed.port else ""
            host_part = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            return f"{parsed.scheme}://{host_part}{port_part}"
    except Exception:
        pass
    return None

for _var_name in (
    "CWA_URL",
    "CALIBRE_WEB_URL",
    "CALIBRE_URL",
    "CWA_UPSTREAM",
    "BT_CWA_READER_UPSTREAM",
    "KAVITA_URL",
    "KAVITA_UPSTREAM",
    "BT_KAVITA_READER_UPSTREAM",
    "BT_READER_UPSTREAM",
    "BT_PUBLIC_ORIGIN",
):
    _cand = os.environ.get(_var_name, "")
    _origin = _extract_origin_candidate(_cand)
    if _origin:
        try:
            ALLOWED_ORIGINS.add(_validate_cors_origin(_origin))
        except ValueError:
            pass
BT_ALLOW_PRIVATE_LAN = os.environ.get("BT_ALLOW_PRIVATE_LAN", "true").lower() in ("1", "true", "yes")
_PRIVATE_ORIGIN_RE = re.compile(
    r"^https?://("
    r"localhost|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|\[::1\]|"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r")(:\d+)?$"
)


def _is_origin_allowed(origin: str | None) -> str | None:
    """Return the origin if it's allowed, else None."""
    if not origin:
        return None
    if origin in ALLOWED_ORIGINS:
        return origin
    # Credentialed CWA-session requests may never combine cookies with a
    # subnet-wide origin policy. Cross-origin operators must enumerate the
    # exact reader origin; same-origin proxy mode needs no CORS at all.
    if AUTHENTICATOR.mode in {"cwa_session", "reader_session"}:
        return None
    if BT_ALLOW_PRIVATE_LAN and _PRIVATE_ORIGIN_RE.match(origin):
        return origin
    return None


# ── Rate limiter (H6) ───────────────────────────────────────────────────────

_rate_limit_lock = threading.Lock()
_rate_limit_store: dict[str, list[float]] = defaultdict(list)
_auth_rate_limit_store: dict[str, list[float]] = defaultdict(list)
_auth_inflight_store: dict[str, int] = {}
BT_RATE_LIMIT_PER_MINUTE = int(os.environ.get("BT_RATE_LIMIT_PER_MINUTE", "300"))
BT_RATE_LIMIT_RETRY_AFTER = int(os.environ.get("BT_RATE_LIMIT_RETRY_AFTER", "10"))
BT_AUTH_RATE_LIMIT_PER_MINUTE = int(
    os.environ.get("BT_AUTH_RATE_LIMIT_PER_MINUTE", "300")
)
BT_AUTH_MAX_INFLIGHT_PER_CLIENT = int(
    os.environ.get("BT_AUTH_MAX_INFLIGHT_PER_CLIENT", "2")
)
BT_RATE_LIMIT_MAX_CLIENTS = int(os.environ.get("BT_RATE_LIMIT_MAX_CLIENTS", "10000"))

if min(
    BT_RATE_LIMIT_PER_MINUTE,
    BT_RATE_LIMIT_RETRY_AFTER,
    BT_AUTH_RATE_LIMIT_PER_MINUTE,
    BT_AUTH_MAX_INFLIGHT_PER_CLIENT,
    BT_RATE_LIMIT_MAX_CLIENTS,
) <= 0:
    raise ValueError("rate-limit settings must be positive integers")

RATE_LIMIT_MAX = BT_RATE_LIMIT_PER_MINUTE
RATE_LIMIT_WINDOW = 60


def _cleanup_rate_limits():
    """Background thread to clean up inactive IPs from the rate limiter."""
    while True:
        time.sleep(3600)  # Every hour
        now = time.monotonic()
        cutoff = now - RATE_LIMIT_WINDOW
        with _rate_limit_lock:
            keys_to_delete = []
            for store in (_rate_limit_store, _auth_rate_limit_store):
                keys_to_delete = []
                for ip, timestamps in store.items():
                    active = [t for t in timestamps if t > cutoff]
                    if not active:
                        keys_to_delete.append(ip)
                    else:
                        store[ip] = active
                for ip in keys_to_delete:
                    del store[ip]

threading.Thread(target=_cleanup_rate_limits, daemon=True).start()


def _acquire_auth_inflight(client_key: str) -> bool:
    """Reserve a bounded auth-authority slot for one observed client."""
    with _rate_limit_lock:
        current = _auth_inflight_store.get(client_key, 0)
        if current >= BT_AUTH_MAX_INFLIGHT_PER_CLIENT:
            return False
        if current == 0 and len(_auth_inflight_store) >= BT_RATE_LIMIT_MAX_CLIENTS:
            return False
        _auth_inflight_store[client_key] = current + 1
        return True


def _release_auth_inflight(client_key: str) -> None:
    with _rate_limit_lock:
        current = _auth_inflight_store.get(client_key, 0)
        if current <= 1:
            _auth_inflight_store.pop(client_key, None)
        else:
            _auth_inflight_store[client_key] = current - 1

def _token_matches(provided: str, expected: str) -> bool:
    """Constant-time token comparison (avoids timing side-channels on the
    shared-secret checks; hmac.compare_digest is the standard tool)."""
    if not provided or not expected or len(provided) > 4096 or len(expected) > 4096:
        return False
    try:
        return hmac.compare_digest(provided, expected)
    except TypeError:
        # compare_digest rejects non-ASCII str values. Treat malformed header
        # input as an ordinary credential rejection instead of a framework 500.
        return False


def _resolved_trusted_proxy_addresses() -> frozenset:
    """Resolve one managed Docker alias with a short drift-tolerant cache."""
    global _trusted_proxy_host_cache
    host = BT_TRUSTED_PROXY_HOST
    if not host:
        return frozenset()
    now = time.monotonic()
    with _trusted_proxy_host_lock:
        cached_host, expires_at, addresses = _trusted_proxy_host_cache
        if cached_host == host and now < expires_at:
            return addresses
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        resolved = frozenset(
            ipaddress.ip_address(record[4][0].split("%", 1)[0]) for record in records
        )
    except (OSError, ValueError):
        resolved = frozenset()
    with _trusted_proxy_host_lock:
        _trusted_proxy_host_cache = (host, now + 5.0, resolved)
    return resolved


def _client_ip() -> str:
    """Rate-limit key. Uses X-Forwarded-For's LAST hop only when the peer
    (the IP the WSGI server actually saw, NOT the X-Forwarded-For value) is
    trusted. That means either BT_TRUSTED_PROXIES matches the peer, or
    BT_TRUST_PROXY=true is set (legacy / dev only — anyone who can reach
    the API can spoof X-Forwarded-For in this mode).

    Why the LAST hop: standard proxies (nginx `$proxy_add_x_forwarded_for`,
    SWAG, Traefik) APPEND the address they saw to any incoming header, so the
    only entry a client cannot forge is the final one — the address observed
    by our trusted proxy. Taking the FIRST hop (the previous behaviour) let
    any client bypass the rate limiter entirely by sending a made-up
    `X-Forwarded-For: <random>` header per request, precisely in the
    "trusted proxy" configurations meant to be production-safe.
    """
    peer = request.remote_addr or "unknown"
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        client_ip_from_xff = fwd.split(",")[-1].strip()
    else:
        client_ip_from_xff = ""

    # Exact CIDRs cover identity edges. Managed split deployments use one
    # Docker-only DNS alias so proxy address changes do not require a subnet
    # allowlist or an API restart.
    if (BT_TRUSTED_PROXIES or BT_TRUSTED_PROXY_HOST) and client_ip_from_xff:
        try:
            peer_ip = ipaddress.ip_address(peer)
        except ValueError:
            return peer  # malformed peer — don't trust the XFF
        if any(peer_ip in net for net in _TRUSTED_PROXY_NETS) or peer_ip in (
            _resolved_trusted_proxy_addresses()
        ):
            return client_ip_from_xff or peer
        # Peer not in allowlist: an attacker is forging XFF. Use peer.
        return peer

    # Legacy BT_TRUST_PROXY=true: trust XFF from any peer.
    if BT_TRUST_PROXY and client_ip_from_xff:
        return client_ip_from_xff

    return peer


def _clean_single_ip(value: object) -> str:
    """Validate one IP while preserving the exact text CWA fingerprints."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or value != value.strip()
        or "," in value
        or "%" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise AuthRejected("authentication rejected")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise AuthRejected("authentication rejected") from None
    return value


def _cwa_session_binding() -> CwaSessionBinding:
    """Reconstruct CWA's login-time peer context from an authenticated path.

    X-Forwarded-For is authentication material here, not merely routing
    metadata. Only one managed DNS peer or an exact /32 or /128 allowlist
    entry may supply it. The legacy BT_TRUST_PROXY rate-limit switch never
    grants that authority.
    """
    peer_text = _clean_single_ip(request.remote_addr or "")
    peer = ipaddress.ip_address(peer_text)
    exact_networks = tuple(
        network
        for network in _TRUSTED_PROXY_NETS
        if network.prefixlen == network.max_prefixlen
    )
    explicit_proxy_authority = bool(BT_TRUSTED_PROXY_HOST or exact_networks)
    peer_is_exact_proxy = any(peer in network for network in exact_networks)
    if BT_TRUSTED_PROXY_HOST:
        peer_is_exact_proxy = peer_is_exact_proxy or peer in (
            _resolved_trusted_proxy_addresses()
        )

    if "X-Forwarded-For" in request.headers:
        if not peer_is_exact_proxy:
            raise AuthRejected("authentication rejected")
        cwa_remote_addr = _clean_single_ip(
            request.headers.get("X-Forwarded-For", "")
        )
    else:
        # A managed proxy must always overwrite XFF. Falling back to its socket
        # address would destroy a strong session and could cache a false verdict.
        if explicit_proxy_authority:
            raise AuthRejected("authentication rejected")
        cwa_remote_addr = peer_text

    user_agent = (
        request.headers.get("User-Agent")
        if "User-Agent" in request.headers
        else None
    )
    return CwaSessionBinding(
        cwa_remote_addr=cwa_remote_addr,
        user_agent=user_agent,
    )


def _check_window_rate_limit(
    store: dict[str, list[float]], ip: str, limit: int
) -> bool:
    """Consume one bounded sliding-window admission slot."""
    now = time.monotonic()
    with _rate_limit_lock:
        cutoff = now - RATE_LIMIT_WINDOW
        if ip not in store and len(store) >= BT_RATE_LIMIT_MAX_CLIENTS:
            # Reclaim stale buckets only under pressure. If every bucket is
            # active, reject a new identity rather than growing memory or
            # evicting an attacker into a fresh allowance.
            expired = [
                key
                for key, values in store.items()
                if not any(timestamp > cutoff for timestamp in values)
            ]
            for key in expired:
                del store[key]
            if len(store) >= BT_RATE_LIMIT_MAX_CLIENTS:
                return False
        timestamps = store[ip]
        # Evict expired timestamps
        store[ip] = [t for t in timestamps if t > cutoff]
        if len(store[ip]) >= limit:
            return False
        store[ip].append(now)
        return True


def _check_rate_limit(ip: str) -> bool:
    """Return True if translation/API work should be admitted."""
    return _check_window_rate_limit(_rate_limit_store, ip, RATE_LIMIT_MAX)


def _check_auth_rate_limit(ip: str) -> bool:
    """Bound credential attempts separately from expensive API work."""
    return _check_window_rate_limit(
        _auth_rate_limit_store, ip, BT_AUTH_RATE_LIMIT_PER_MINUTE
    )


# ── Metrics (M5) ────────────────────────────────────────────────────────────

_metrics_lock = threading.Lock()
_HTTP_RESPONSE_CLASSES = ("2xx", "3xx", "4xx", "5xx")
_METRIC_OUTCOMES = (
    "auth_rejected",
    "auth_unavailable",
    "auth_rate_limited",
    "api_rate_limited",
    "work_budget_exhausted",
    "provider_unavailable",
    "invalid_provider_response",
    "translation_failed",
    "internal_error",
    "batch_partial_failure_requests",
)
_WORK_BUDGET_REASONS = (
    "attempts",
    "input_bytes",
    "output_tokens",
    "deadline",
    "queue",
    "cancelled",
    "unknown",
)
_SEGMENT_RECOVERY_METRICS = RECOVERY_METRIC_NAMES
_BATCH_GROUP_SIZE_BUCKETS = (
    "single", "2_4", "5_8", "9_10", "over_10"
)
_BATCH_GROUP_SOURCE_TOKEN_BUCKETS = (
    "up_to_128", "up_to_256", "up_to_450", "up_to_600", "over_600"
)
_TRANSLATION_LATENCY_MS_BUCKETS = (
    "up_to_250", "up_to_500", "up_to_1000", "up_to_2500", "up_to_5000",
    "over_5000",
)


def _empty_metrics() -> dict:
    """Create the complete fixed-cardinality in-process metric schema."""
    return {
        # Backward-compatible translation/cache aggregates.
        "total_requests": 0,
        "total_latency_ms": 0.0,
        "cache_hits": 0,
        "cache_misses": 0,
        "errors": 0,
        "translation_latency_ms_buckets": {
            name: 0 for name in _TRANSLATION_LATENCY_MS_BUCKETS
        },
        # No route, identity, book, provider URL, or error string is ever a
        # metric key. Every dimension below is owned by this module.
        "http_responses": {name: 0 for name in _HTTP_RESPONSE_CLASSES},
        "outcomes": {name: 0 for name in _METRIC_OUTCOMES},
        "work_budget_reasons": {name: 0 for name in _WORK_BUDGET_REASONS},
        "segment_recovery": {
            name: 0 for name in _SEGMENT_RECOVERY_METRICS
        },
        "batch_partial_failure_segments": 0,
        "batch_groups_total": 0,
        "batch_paragraphs_total": 0,
        "batch_group_size_buckets": {
            name: 0 for name in _BATCH_GROUP_SIZE_BUCKETS
        },
        "batch_group_source_token_buckets": {
            name: 0 for name in _BATCH_GROUP_SOURCE_TOKEN_BUCKETS
        },
    }


_metrics = _empty_metrics()


def _record_metric(latency_ms: float, hits: int = 0, misses: int = 0, error: bool = False):
    """Record backward-compatible translation/cache aggregates."""
    with _metrics_lock:
        _metrics["total_requests"] += 1
        _metrics["total_latency_ms"] += latency_ms
        _metrics["cache_hits"] += hits
        _metrics["cache_misses"] += misses
        if latency_ms <= 250:
            bucket = "up_to_250"
        elif latency_ms <= 500:
            bucket = "up_to_500"
        elif latency_ms <= 1000:
            bucket = "up_to_1000"
        elif latency_ms <= 2500:
            bucket = "up_to_2500"
        elif latency_ms <= 5000:
            bucket = "up_to_5000"
        else:
            bucket = "over_5000"
        _metrics["translation_latency_ms_buckets"][bucket] += 1
        if error:
            _metrics["errors"] += 1


def _record_http_response(status_code: int) -> None:
    """Count a response by its fixed HTTP class, including middleware exits."""
    response_class = f"{int(status_code) // 100}xx"
    if response_class not in _HTTP_RESPONSE_CLASSES:
        return
    with _metrics_lock:
        _metrics["http_responses"][response_class] += 1


def _record_outcome(name: str) -> None:
    """Count one server-owned semantic outcome; dynamic labels are forbidden."""
    if name not in _METRIC_OUTCOMES:
        raise ValueError("unknown metric outcome")
    with _metrics_lock:
        _metrics["outcomes"][name] += 1


def _record_work_budget_exhaustion(reason: str) -> None:
    """Count a bounded work rejection without exposing arbitrary reasons."""
    bounded_reason = reason if reason in _WORK_BUDGET_REASONS else "unknown"
    with _metrics_lock:
        _metrics["outcomes"]["work_budget_exhausted"] += 1
        _metrics["work_budget_reasons"][bounded_reason] += 1


def _record_batch_partial_failure(segment_count: int) -> None:
    """Count one partial batch plus its failed segments, without content labels."""
    if isinstance(segment_count, bool) or not isinstance(segment_count, int) or segment_count <= 0:
        raise ValueError("segment_count must be a positive integer")
    with _metrics_lock:
        _metrics["outcomes"]["batch_partial_failure_requests"] += 1
        _metrics["batch_partial_failure_segments"] += segment_count


def _record_segment_recovery(increments: dict[str, int]) -> None:
    """Add one fixed-cardinality tracker snapshot to process metrics."""
    if set(increments) != set(_SEGMENT_RECOVERY_METRICS):
        raise ValueError("invalid recovery metric dimensions")
    if any(
        isinstance(increments[name], bool)
        or not isinstance(increments[name], int)
        or increments[name] < 0
        for name in _SEGMENT_RECOVERY_METRICS
    ):
        raise ValueError("recovery metric values must be non-negative integers")
    if not any(increments[name] for name in _SEGMENT_RECOVERY_METRICS):
        return
    with _metrics_lock:
        for name in _SEGMENT_RECOVERY_METRICS:
            _metrics["segment_recovery"][name] += increments[name]


def _batch_group_size_bucket(size: int) -> str:
    if size == 1:
        return "single"
    if size <= 4:
        return "2_4"
    if size <= 8:
        return "5_8"
    if size <= 10:
        return "9_10"
    return "over_10"


def _batch_group_source_token_bucket(tokens: int) -> str:
    if tokens <= 128:
        return "up_to_128"
    if tokens <= 256:
        return "up_to_256"
    if tokens <= 450:
        return "up_to_450"
    if tokens <= 600:
        return "up_to_600"
    return "over_600"


def _record_batch_plan(paragraphs: list[str], groups: list[list[int]]) -> None:
    """Record deterministic batch shape without content-derived labels."""
    size_buckets = {name: 0 for name in _BATCH_GROUP_SIZE_BUCKETS}
    token_buckets = {
        name: 0 for name in _BATCH_GROUP_SOURCE_TOKEN_BUCKETS
    }
    paragraph_count = 0
    for group in groups:
        if not group:
            raise ValueError("batch groups must not be empty")
        paragraph_count += len(group)
        size_buckets[_batch_group_size_bucket(len(group))] += 1
        source_tokens = sum(
            estimate_source_tokens(paragraphs[index].strip())
            for index in group
        )
        token_buckets[
            _batch_group_source_token_bucket(source_tokens)
        ] += 1
    with _metrics_lock:
        _metrics["batch_groups_total"] += len(groups)
        _metrics["batch_paragraphs_total"] += paragraph_count
        for name, count in size_buckets.items():
            _metrics["batch_group_size_buckets"][name] += count
        for name, count in token_buckets.items():
            _metrics["batch_group_source_token_buckets"][name] += count


def _record_segment_recovery_safely(increments: dict[str, int]) -> None:
    """Best-effort metric sink that can never change an API outcome."""
    try:
        _record_segment_recovery(increments)
    except Exception as exc:
        log.error(
            "segment recovery metrics unavailable error_type=%s",
            type(exc).__name__,
        )


def _flush_segment_recovery(tracker: BatchRecoveryTracker) -> None:
    """Flush now and retain a safe sink for records from late workers."""
    try:
        tracker.attach_sink_and_flush(_record_segment_recovery_safely)
    except Exception as exc:
        log.error(
            "segment recovery metric flush failed error_type=%s",
            type(exc).__name__,
        )


def _reset_metrics_for_tests() -> None:
    """Restore the metric schema atomically for deterministic contract tests."""
    with _metrics_lock:
        _metrics.clear()
        _metrics.update(_empty_metrics())
    _reset_provider_call_stats_for_tests()


def _work_budget_response(exc: WorkBudgetExceeded):
    """Map internal admission limits to a stable, non-sensitive 503."""
    _record_work_budget_exhaustion(exc.reason)
    request_id = getattr(request, "request_id", None)
    log.warning("req=%s upstream work rejected reason=%s", request_id, exc.reason)
    response = jsonify({
        "error": "work_budget_exhausted",
        "reason": exc.reason,
        "request_id": request_id,
    })
    if exc.reason == "queue":
        response.headers["Retry-After"] = str(
            max(1, math.ceil(BT_UPSTREAM_QUEUE_TIMEOUT)))
    return response, 503


# ── Shared batch helper (M3) ───────────────────────────────────────────────

def _translate_paragraphs(
    paragraphs: list[str],
    source_lang: str,
    target_lang: str,
    budget: WorkBudget,
    *,
    tenant: str = "legacy-anonymous",
    book_id: str = "unscoped",
    chapter_id: str = "unscoped",
    allow_cloud_fallback: bool = False,
    glossary: list[tuple[str, str]] | None = None,
) -> dict:
    """
    Shared helper for batch translation logic used by /translate/batch.

    Returns dict with translations list, cached_count, fresh_count,
    total_elapsed_ms, and per-paragraph attribution:
      - backends[i]  = provider that served paragraph i
                       ("cache" if served from cache; the actual
                       provider name like "local"/"minimax" if fresh;
                       "" if the paragraph was empty)
      - cached[i]    = True if paragraph i was a cache hit

    The per-paragraph fields are optional in the sense that older API
    clients can ignore them; the existing aggregate counts are unchanged.
    """
    translations = [""] * len(paragraphs)
    backends = [""] * len(paragraphs)        # NEW: per-paragraph backend attribution
    cached = [False] * len(paragraphs)       # NEW: per-paragraph cache-hit flag
    error_codes: list[str | None] = [None] * len(paragraphs)
    retry_after_seconds: list[int | None] = [None] * len(paragraphs)
    cached_count = 0
    fresh_count = 0
    start = time.monotonic()

    # Cache and translate deterministic groups atomically.  Serving one cached
    # segment while translating its siblings would remove that text from the
    # provider prompt and silently change the context.  A group is therefore a
    # hit only when every non-empty segment exists under one exact backend and
    # prompt/context contract; otherwise the whole original group is refreshed.
    groups = translation_groups(paragraphs)
    _record_batch_plan(paragraphs, groups)
    missing_groups: list[list[int]] = []
    contracts = {
        tuple(group): batch_cache_contract(
            paragraphs, group, source_lang, target_lang, glossary
        )
        for group in groups
    }

    # Collapse repeated (text, backend, contract) probes across groups and
    # providers: one batch fetch per unseen triple, memoized for the request.
    probe_memo: dict[
        tuple[str, str, str, str, str, str],
        tuple[str | None, CacheScope],
    ] = {}

    def _probe_many(
        items: list[tuple[int, str, CacheScope, tuple[str, str, str, str, str, str]]],
    ) -> dict[int, tuple[str | None, CacheScope]]:
        probed: dict[int, tuple[str | None, CacheScope]] = {}
        missing: list[tuple[int, str, CacheScope, tuple[str, str, str, str, str, str]]] = []
        for index, text, scope, memo_key in items:
            if memo_key in probe_memo:
                probed[index] = probe_memo[memo_key]
            else:
                missing.append((index, text, scope, memo_key))
        if missing:
            hits = get_cached_many(
                [
                    (text, source_lang, target_lang, scope)
                    for _, text, scope, _ in missing
                ],
                record_hit=False,
            )
            for (index, _, scope, memo_key), hit in zip(missing, hits):
                probe_memo[memo_key] = (hit, scope)
                probed[index] = (hit, scope)
        return probed

    def _probe_items(
        group: list[int], contract, provider: str, model: str,
    ) -> list[tuple[int, str, CacheScope, tuple[str, str, str, str, str, str]]]:
        items = []
        for index in group:
            scope = _cache_scope(
                tenant=tenant,
                book_id=book_id,
                chapter_id=chapter_id,
                context_hash=contract.context_hash,
                provider=provider,
                model=model,
                prompt_hash=contract.prompt_hash,
                protocol_version=contract.protocol_version,
            )
            memo_key = (
                paragraphs[index],
                provider,
                model,
                contract.context_hash,
                contract.prompt_hash,
                contract.protocol_version,
            )
            items.append((index, paragraphs[index], scope, memo_key))
        return items

    def _accept(
        items: list[tuple[int, str, CacheScope, tuple[str, str, str, str, str, str]]],
    ) -> list[tuple[int, str, CacheScope]] | None:
        probed = _probe_many(items)
        candidate: list[tuple[int, str, CacheScope]] = []
        for index, _, scope, _ in items:
            hit, _ = probed[index]
            if hit is None:
                return None
            candidate.append((index, hit, scope))
        return candidate or None

    for group in groups:
        contract = contracts[tuple(group)]
        accepted: list[tuple[int, str, CacheScope]] | None = None
        for provider, model in cache_lookup_backends(
            allow_cloud_fallback=allow_cloud_fallback
        ):
            accepted = _accept(_probe_items(group, contract, provider, model))
            if accepted is not None:
                break

        if accepted is None:
            single_c = single_cache_contract(source_lang, target_lang, glossary)
            for provider, model in cache_lookup_backends(
                allow_cloud_fallback=allow_cloud_fallback
            ):
                accepted = _accept(
                    _probe_items(group, single_c, provider, model)
                )
                if accepted is not None:
                    break

        if accepted is None:
            missing_groups.append(group)
            continue

        for index, hit, scope in accepted:
            translations[index] = hit
            backends[index] = "cache"
            cached[index] = True
            cached_count += 1
            record_cache_hit(
                paragraphs[index],
                source_lang,
                target_lang,
                scope=scope,
            )

    if missing_groups:
        recovery_tracker = BatchRecoveryTracker()
        try:
            results = translate_batch(
                paragraphs,
                source_lang,
                target_lang,
                budget=budget,
                selected_groups=missing_groups,
                operation_namespace=_operation_namespace(
                    tenant, book_id, chapter_id
                ),
                allow_cloud_fallback=allow_cloud_fallback,
                recovery_tracker=recovery_tracker,
                glossary=glossary,
            )
        finally:
            _flush_segment_recovery(recovery_tracker)
        cache_writes: list[tuple[str, str, str, str, CacheScope]] = []
        for group in missing_groups:
            contract = contracts[tuple(group)]
            for index in group:
                item = results[index]
                translated = item.text
                backend = item.provider
                translations[index] = translated
                backends[index] = backend or "unknown"
                error_codes[index] = item.error_code
                retry_after_seconds[index] = item.retry_after_seconds
                if translated.startswith("[TRANSLATION ERROR:"):
                    continue
                fresh_count += 1
                if not item.server_cacheable:
                    continue
                scope = _cache_scope(
                    tenant=tenant,
                    book_id=book_id,
                    chapter_id=chapter_id,
                    context_hash=contract.context_hash,
                    provider=backend,
                    model=model_for_provider(backend),
                    prompt_hash=contract.prompt_hash,
                    protocol_version=contract.protocol_version,
                )
                cache_writes.append((
                    paragraphs[index], source_lang, target_lang,
                    translated, scope,
                ))
        if cache_writes:
            try:
                put_cache_many(cache_writes)
                _invalidate_stats_cache()
            except Exception as exc:
                log.error(
                    "Cache write failed (non-fatal) error_type=%s",
                    type(exc).__name__,
                )

    if cached_count:
        _invalidate_stats_cache()

    total_elapsed_ms = int((time.monotonic() - start) * 1000)

    return {
        "translations": translations,
        "backends": backends,
        "cached": cached,
        "error_codes": error_codes,
        "retry_after_seconds": retry_after_seconds,
        "cached_count": cached_count,
        "fresh_count": fresh_count,
        "total_elapsed_ms": total_elapsed_ms,
    }


# ── Request middleware (M5: request ID + timing, H6: rate limiting) ─────────

@app.before_request
def before_request_hook():
    """Attach request metadata, authenticate, then admit API work."""
    request.request_id = str(uuid.uuid4())
    request.start_time = time.monotonic()

    # Liveness/readiness and preflight stay independent of external auth so
    # orchestration can diagnose an auth-authority outage. Everything else,
    # including metrics and stats, receives a server-owned opaque subject.
    session_exchange = (
        AUTHENTICATOR.mode == "reader_session"
        and request.path == "/session"
        and request.method == "POST"
    )
    protected = (
        request.method != "OPTIONS"
        and request.path not in ("/health", "/ready", "/ping")
        and not session_exchange
    )
    if protected:
        auth_client_key = _client_ip()
        # Admit by the spoof-resistant observed client before calling an
        # external authority. This prevents sequential credential churn from
        # reaching CWA or Authentik after the client's attempt budget is spent.
        if not _check_auth_rate_limit(auth_client_key):
            _record_outcome("auth_rate_limited")
            response = jsonify({
                "error": "rate_limited",
                "retry_after": BT_RATE_LIMIT_RETRY_AFTER,
                "retry_safe": True,
                "scope": "auth_admission",
                "request_id": request.request_id,
            })
            response.headers["Retry-After"] = str(BT_RATE_LIMIT_RETRY_AFTER)
            return response, 429
        if not _acquire_auth_inflight(auth_client_key):
            _record_outcome("auth_rate_limited")
            response = jsonify({
                "error": "rate_limited",
                "retry_after": BT_RATE_LIMIT_RETRY_AFTER,
                "retry_safe": True,
                "scope": "auth_admission",
                "request_id": request.request_id,
            })
            response.headers["Retry-After"] = str(BT_RATE_LIMIT_RETRY_AFTER)
            return response, 429
        try:
            try:
                auth_kwargs = {}
                if AUTHENTICATOR.mode in {"cwa_session", "reader_session"}:
                    auth_kwargs["cwa_binding"] = _cwa_session_binding()
                identity = AUTHENTICATOR.authenticate(
                    request.headers,
                    request.remote_addr,
                    **auth_kwargs,
                )
            except AuthRejected:
                _record_outcome("auth_rejected")
                log.warning("req=%s authentication rejected", request.request_id)
                return jsonify({
                    "error": "unauthorized",
                    "request_id": request.request_id,
                }), 401
            except AuthUnavailable:
                _record_outcome("auth_unavailable")
                log.warning(
                    "req=%s authentication authority unavailable",
                    request.request_id,
                )
                return jsonify({
                    "error": "authentication_unavailable",
                    "request_id": request.request_id,
                }), 503
            if identity.subject == "legacy-anonymous":
                authenticated_key = f"authenticated:legacy-anonymous:{_client_ip()}"
            else:
                authenticated_key = f"authenticated:{identity.subject}"
            request.auth_subject = identity.subject
            request.auth_roles = identity.roles
            request.rate_limit_key = authenticated_key
        finally:
            _release_auth_inflight(auth_client_key)

    # Rate limiting (H6) — exempt observability endpoints so operators can
    # monitor health/stats even while the per-client budget is exhausted.
    # /stats is in this set (not the auth set above) deliberately: it must
    # stay reachable during a rate-limit storm so the operator can see how
    # badly things are going, but it still passes the selected auth authority.
    # Skip CORS preflights too: an OPTIONS would otherwise burn 2x budget per
    # real cross-origin request, and a 429 on a preflight surfaces as a cryptic
    # CORS error in the browser instead of a rate limit the frontend can honor.
    if request.method != "OPTIONS" and request.path not in ("/health", "/ready", "/metrics", "/ping", "/stats", "/tts/status"):
        rate_limit_key = getattr(request, "rate_limit_key", _client_ip())
        if not _check_rate_limit(rate_limit_key):
            _record_outcome("api_rate_limited")
            log.warning(
                "Rate limit exceeded for authenticated request (req %s)",
                request.request_id,
            )
            response = jsonify({
                "error": "rate_limited",
                "retry_after": BT_RATE_LIMIT_RETRY_AFTER,
                "retry_safe": True,
                "scope": "api_admission",
                "request_id": request.request_id,
            })
            response.headers["Retry-After"] = str(BT_RATE_LIMIT_RETRY_AFTER)
            return response, 429


@app.after_request
def after_request_hook(response):
    """Log timing, add CORS headers, add request ID header."""
    _record_http_response(response.status_code)
    # Timing (M5)
    elapsed_ms = int((time.monotonic() - getattr(request, "start_time", time.monotonic())) * 1000)
    req_id = getattr(request, "request_id", "unknown")
    log.info(
        "req=%s method=%s path=%s status=%d elapsed=%dms",
        req_id, request.method, request.path, response.status_code, elapsed_ms,
    )
    response.headers["X-Request-ID"] = req_id

    # CORS (H5: origin whitelist)
    origin = request.headers.get("Origin")
    allowed = _is_origin_allowed(origin)
    if allowed:
        response.headers["Access-Control-Allow-Origin"] = allowed
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-BT-Token"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        # Let cross-origin JS read the request ID and 429 Retry-After header.
        response.headers["Access-Control-Expose-Headers"] = "X-Request-ID, Retry-After"
        response.vary.add("Origin")
        if AUTHENTICATOR.mode in {"cwa_session", "reader_session"}:
            response.headers["Access-Control-Allow-Credentials"] = "true"

    return response


# ── Routes ──────────────────────────────────────────────────────────────────


@app.route("/session", methods=["POST", "DELETE"])
def reader_session():
    """Exchange reader credentials for, or revoke, one short-lived BT session."""
    broker = getattr(AUTHENTICATOR, "reader_session_broker", None)
    if AUTHENTICATOR.mode != "reader_session" or broker is None:
        return jsonify({
            "error": "not_found",
            "request_id": getattr(request, "request_id", None),
        }), 404

    try:
        binding = _cwa_session_binding()
    except AuthRejected:
        return jsonify({
            "error": "unauthorized",
            "request_id": request.request_id,
        }), 401
    if request.method == "DELETE":
        try:
            broker.revoke(request.headers, binding)
        except Exception as exc:
            from reader_session import BrokerRejected

            if isinstance(exc, BrokerRejected):
                return jsonify({
                    "error": "unauthorized",
                    "request_id": request.request_id,
                }), 401
            raise
        response = jsonify({"status": "revoked", "request_id": request.request_id})
        response.headers["Set-Cookie"] = broker.clear_cookie
        response.headers["Cache-Control"] = "no-store"
        return response

    # The credential travels in Authorization or Cookie. A request body is
    # unnecessary and would expand the sensitive parser surface.
    if request.content_length not in (None, 0) or request.get_data(cache=False):
        return jsonify({
            "error": "bad_request",
            "request_id": request.request_id,
        }), 400
    client_key = _client_ip()
    if not _check_auth_rate_limit(client_key) or not _acquire_auth_inflight(client_key):
        response = jsonify({
            "error": "rate_limited",
            "retry_after": BT_RATE_LIMIT_RETRY_AFTER,
            "retry_safe": True,
            "scope": "auth_admission",
            "request_id": request.request_id,
        })
        response.headers["Retry-After"] = str(BT_RATE_LIMIT_RETRY_AFTER)
        return response, 429
    try:
        try:
            issue = broker.exchange(request.headers, binding)
        except Exception as exc:
            from reader_session import BrokerRejected, BrokerUnavailable

            if isinstance(exc, BrokerRejected):
                return jsonify({
                    "error": "unauthorized",
                    "request_id": request.request_id,
                }), 401
            if isinstance(exc, BrokerUnavailable):
                return jsonify({
                    "error": "authentication_unavailable",
                    "request_id": request.request_id,
                }), 503
            raise
    finally:
        _release_auth_inflight(client_key)
    response = jsonify({
        "status": "ok",
        "expires_in": issue.expires_in,
        "reader_type": broker.reader_type,
        "reader_version": broker.reader_version,
        "request_id": request.request_id,
    })
    response.headers["Set-Cookie"] = issue.set_cookie
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@app.errorhandler(HTTPException)
def http_error(exc: HTTPException):
    """Keep framework-generated failures on the public JSON API contract."""
    error_codes = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        413: "request_too_large",
        415: "unsupported_media_type",
        429: "rate_limited",
    }
    status_code = int(exc.code or 500)
    return jsonify({
        "error": error_codes.get(status_code, "http_error"),
        "request_id": getattr(request, "request_id", None),
    }), status_code


@app.errorhandler(Exception)
def unhandled_error(exc: Exception):
    """Fail closed without returning or logging private exception strings."""
    _record_outcome("internal_error")
    request_id = getattr(request, "request_id", None)
    log.error(
        "req=%s unhandled request error type=%s",
        request_id, type(exc).__name__,
    )
    return jsonify({
        "error": "internal_error",
        "request_id": request_id,
    }), 500


@app.errorhandler(413)
def request_too_large(_e):
    """Return a clean JSON 413 when MAX_CONTENT_LENGTH trips.

    Without this handler Werkzeug returns an HTML body, which is fine for a
    browser but breaks any API client that JSON-decodes the response. We
    still want the per-field caps to fire first when the body is small but
    contains one oversized value — this is purely the backstop path.
    """
    req_id = getattr(request, "request_id", None)
    return jsonify({
        "error": f"Request body exceeds the {BT_MAX_CONTENT_LENGTH}-byte limit",
        "request_id": req_id,
    }), 413


@app.route("/ping")
def ping():
    """Liveness probe — instant, never touches the LLM. Used by the Docker
    HEALTHCHECK so a busy/slow vLLM can't mark the container unhealthy while it
    is in fact serving translations. /health is shallow; /health/deep probes
    providers only for an authenticated operator."""
    return jsonify({"status": "ok"})


@app.route("/health")
@app.route("/ready")
def health():
    """Shallow readiness: process/config loaded, with no provider network I/O."""
    return jsonify({
        "status": "ok",
        "service": "book-translator",
        "version": __version__,
        "request_id": getattr(request, "request_id", None),
    })


@app.route("/provider-policy")
def get_provider_policy():
    """Return backend locality plus an opaque generation for consent binding."""
    response = jsonify({
        **provider_policy(),
        "generation": PROVIDER_POLICY_GENERATION,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/health/deep")
def deep_health():
    """Operator-only provider probe using normal work and concurrency caps."""
    provided_token = request.headers.get("X-BT-Token", "")
    if API_TOKEN:
        authorized = _token_matches(provided_token, API_TOKEN)
    else:
        try:
            operator_token = _get_cleanup_token()
        except CleanupCredentialUnavailable:
            return jsonify({
                "error": "operator_credential_unavailable",
                "request_id": getattr(request, "request_id", None),
            }), 503
        authorized = _token_matches(provided_token, operator_token)
    if not authorized:
        return jsonify({
            "error": "Unauthorized",
            "request_id": getattr(request, "request_id", None),
        }), 401

    try:
        backend_health = check_backend_health(create_work_budget())
    except WorkBudgetExceeded as exc:
        return _work_budget_response(exc)
    overall = "ok" if backend_health and all(
        b.get("status") == "ok" for b in backend_health.values()
    ) else "degraded"
    return jsonify({
        "status": overall,
        "service": "book-translator",
        "version": __version__,
        "backends": backend_health,
        "request_id": getattr(request, "request_id", None),
    })


_stats_cache_lock = threading.Lock()
_stats_cache: dict[str, object] = {"data": None, "ts": 0.0}
_STATS_TTL_SECONDS = 5.0


def _invalidate_stats_cache() -> None:
    """Drop the /stats snapshot after this process mutates the cache."""
    with _stats_cache_lock:
        _stats_cache["data"] = None


@app.route("/stats")
def stats():
    """Return cache statistics (snapshot held for a few seconds)."""
    now = time.monotonic()
    with _stats_cache_lock:
        payload = _stats_cache["data"]
        if payload is not None and (now - _stats_cache["ts"]) < _STATS_TTL_SECONDS:
            return jsonify(payload)
    fresh = get_cache_stats()
    with _stats_cache_lock:
        _stats_cache["data"] = fresh
        _stats_cache["ts"] = time.monotonic()
    return jsonify(fresh)


@app.route("/metrics")
def metrics():
    """Return fixed-cardinality, content-free request metrics (M5)."""
    with _metrics_lock:
        snapshot = copy.deepcopy(_metrics)
    total = snapshot["total_requests"]
    avg_latency = round(snapshot["total_latency_ms"] / total, 1) if total > 0 else 0
    total_cache = snapshot["cache_hits"] + snapshot["cache_misses"]
    cache_hit_rate = round(snapshot["cache_hits"] / total_cache * 100, 1) if total_cache > 0 else 0
    return jsonify({
        "total_requests": total,
        "average_latency_ms": avg_latency,
        "cache_hit_rate_pct": cache_hit_rate,
        "cache_hits": snapshot["cache_hits"],
        "cache_misses": snapshot["cache_misses"],
        "errors": snapshot["errors"],
        "translation_latency_ms_buckets": snapshot[
            "translation_latency_ms_buckets"
        ],
        "http_responses_total": sum(snapshot["http_responses"].values()),
        "http_responses": snapshot["http_responses"],
        "outcomes": snapshot["outcomes"],
        "work_budget_reasons": snapshot["work_budget_reasons"],
        "segment_recovery": snapshot["segment_recovery"],
        "batch_partial_failure_segments": snapshot[
            "batch_partial_failure_segments"
        ],
        "batch_groups_total": snapshot["batch_groups_total"],
        "batch_paragraphs_total": snapshot["batch_paragraphs_total"],
        "batch_group_size_buckets": snapshot[
            "batch_group_size_buckets"
        ],
        "batch_group_source_token_buckets": snapshot[
            "batch_group_source_token_buckets"
        ],
        "provider_calls": provider_call_stats(),
        "singleflight": singleflight_stats(),
    })


@app.route("/translate", methods=["POST"])
def translate():
    """
    Translate a single paragraph.

    POST body: {
        "text": "Hello world",
        "source_lang": "English",
        "target_lang": "Spanish",
        "allow_cloud_fallback": false
    }

    Returns: {
        "translated": "Hola mundo",
        "cached": true/false,
        "elapsed_ms": 1234,
        "backend": "local"|"minimax",
        "request_id": "uuid"
    }
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    if "text" not in data or not isinstance(data["text"], str):
        return jsonify({"error": "Missing or invalid 'text' field"}), 400

    text = data["text"].strip()
    if _has_invalid_unicode(text):
        return jsonify({"error": "'text' contains invalid Unicode"}), 400
    if len(text) > BT_MAX_PARAGRAPH_CHARS:
        return jsonify({
            "error": f"'text' exceeds the {BT_MAX_PARAGRAPH_CHARS}-character limit"
        }), 413

    source_lang = data.get("source_lang", "English")
    target_lang = data.get("target_lang", "Spanish")

    # Validate languages (H7)
    lang_error = _validate_languages(source_lang, target_lang)
    if lang_error:
        return jsonify({"error": lang_error}), 400

    try:
        allow_cloud_fallback = _cloud_fallback_consent(data)
        stale_provider_policy = _stale_provider_policy(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if stale_provider_policy:
        return jsonify({
            "error": "provider_policy_changed",
            "request_id": getattr(request, "request_id", None),
        }), 409

    try:
        tenant, book_id, chapter_id = _request_cache_namespace(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    budget = create_work_budget()

    req_id = getattr(request, "request_id", None)

    if not text:
        _record_metric(0, hits=0, misses=1)
        return jsonify({"translated": "", "cached": False, "elapsed_ms": 0, "request_id": req_id})

    # Short-circuit source==target: do NOT spend an LLM call or pollute the
    # cache with self-pairs (e.g. en->en, es->es). Echoes the source text as
    # the translation; the frontend already renders this as a passthrough.
    if source_lang == target_lang:
        log.info("req=%s short-circuit source==target (%s)", req_id, source_lang)
        _record_metric(0, hits=0, misses=0)
        return jsonify({
            "translated": text,
            "cached": False,
            "skipped": "source==target",
            "elapsed_ms": 0,
            "request_id": req_id,
        })

    glossary = _request_glossary(tenant, book_id)

    # Check cache first
    cached = _cache_lookup(
        text,
        source_lang,
        target_lang,
        tenant=tenant,
        book_id=book_id,
        chapter_id=chapter_id,
        allow_cloud_fallback=allow_cloud_fallback,
    )
    if cached is not None:
        _record_metric(0, hits=1, misses=0)
        _invalidate_stats_cache()
        return jsonify({
            "translated": cached,
            "cached": True,
            "elapsed_ms": 0,
            "request_id": req_id,
        })

    # Translate via best available backend
    start = time.monotonic()
    try:
        translated, backend = translate_text(
            text,
            source_lang,
            target_lang,
            budget=budget,
            operation_namespace=_operation_namespace(
                tenant, book_id, chapter_id
            ),
            allow_cloud_fallback=allow_cloud_fallback,
            glossary=glossary,
        )
    except WorkBudgetExceeded as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _record_metric(elapsed_ms, hits=0, misses=1, error=True)
        return _work_budget_response(exc)
    except ProviderUnavailableError:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _record_metric(elapsed_ms, hits=0, misses=1, error=True)
        _record_outcome("provider_unavailable")
        log.warning("req=%s translation provider unavailable", req_id)
        return jsonify({
            "error": "provider_unavailable",
            "request_id": req_id,
        }), 502
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _record_metric(elapsed_ms, hits=0, misses=1, error=True)
        _record_outcome("translation_failed")
        log.error(
            "req=%s translation failed error_type=%s",
            req_id, type(e).__name__,
        )
        return jsonify({
            "error": "translation_failed",
            "request_id": req_id,
        }), 500

    elapsed_ms = int((time.monotonic() - start) * 1000)
    _record_metric(elapsed_ms, hits=0, misses=1)

    # Store in cache with correct model name
    try:
        contract = single_cache_contract(source_lang, target_lang, glossary)
        scope = _cache_scope(
            tenant=tenant,
            book_id=book_id,
            chapter_id=chapter_id,
            context_hash=contract.context_hash,
            provider=backend,
            model=model_for_provider(backend),
            prompt_hash=contract.prompt_hash,
            protocol_version=contract.protocol_version,
        )
        put_cache(
            text,
            source_lang,
            target_lang,
            translated,
            scope=scope,
        )
        try:
            batch_c = batch_cache_contract(
                [text], [0], source_lang, target_lang, glossary)
            batch_scope = _cache_scope(
                tenant=tenant,
                book_id=book_id,
                chapter_id=chapter_id,
                context_hash=batch_c.context_hash,
                provider=backend,
                model=model_for_provider(backend),
                prompt_hash=batch_c.prompt_hash,
                protocol_version=batch_c.protocol_version,
            )
            put_cache(
                text,
                source_lang,
                target_lang,
                translated,
                scope=batch_scope,
            )
        except Exception:
            pass
        _invalidate_stats_cache()
    except Exception as e:
        log.error("Cache write failed (non-fatal) error_type=%s", type(e).__name__)

    return jsonify({
        "translated": translated,
        "cached": False,
        "elapsed_ms": elapsed_ms,
        "backend": backend,
        "request_id": req_id,
    })



class _StreamResponseTooLarge(RuntimeError):
    """Streamed provider output crossed the configured in-memory boundary."""


@app.route("/translate/stream", methods=["POST"])
def translate_stream():
    """
    Translate a single paragraph with Server-Sent Events (SSE) token streaming.

    POST body: {
        "text": "Hello world",
        "source_lang": "English",
        "target_lang": "Spanish",
        "allow_cloud_fallback": false
    }

    Yields SSE events:
      data: {"delta": "Hola", "cached": false}
      ...
      event: done
      data: {"translated": "Hola mundo", "cached": false, "backend": "local", "elapsed_ms": 120}
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    if "text" not in data or not isinstance(data["text"], str):
        return jsonify({"error": "Missing or invalid 'text' field"}), 400

    text = data["text"].strip()
    if _has_invalid_unicode(text):
        return jsonify({"error": "'text' contains invalid Unicode"}), 400
    if len(text) > BT_MAX_PARAGRAPH_CHARS:
        return jsonify({
            "error": f"'text' exceeds the {BT_MAX_PARAGRAPH_CHARS}-character limit"
        }), 413

    source_lang = data.get("source_lang", "English")
    target_lang = data.get("target_lang", "Spanish")

    lang_error = _validate_languages(source_lang, target_lang)
    if lang_error:
        return jsonify({"error": lang_error}), 400

    try:
        allow_cloud_fallback = _cloud_fallback_consent(data)
        stale_provider_policy = _stale_provider_policy(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if stale_provider_policy:
        return jsonify({
            "error": "provider_policy_changed",
            "request_id": getattr(request, "request_id", None),
        }), 409

    try:
        tenant, book_id, chapter_id = _request_cache_namespace(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    req_id = getattr(request, "request_id", None)

    if not text:
        def empty_gen():
            yield f"data: {json.dumps({'delta': '', 'cached': False})}\n\n"
            yield f"event: done\ndata: {json.dumps({'translated': '', 'cached': False, 'elapsed_ms': 0, 'request_id': req_id})}\n\n"
        return Response(empty_gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    if source_lang == target_lang:
        def echo_gen():
            yield f"data: {json.dumps({'delta': text, 'cached': False})}\n\n"
            yield f"event: done\ndata: {json.dumps({'translated': text, 'cached': False, 'skipped': 'source==target', 'elapsed_ms': 0, 'request_id': req_id})}\n\n"
        return Response(echo_gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    glossary = _request_glossary(tenant, book_id)

    cached = _cache_lookup(
        text,
        source_lang,
        target_lang,
        tenant=tenant,
        book_id=book_id,
        chapter_id=chapter_id,
        allow_cloud_fallback=allow_cloud_fallback,
    )
    if cached is not None:
        _record_metric(0, hits=1, misses=0)
        _invalidate_stats_cache()
        def cached_gen():
            yield f"data: {json.dumps({'delta': cached, 'cached': True})}\n\n"
            yield f"event: done\ndata: {json.dumps({'translated': cached, 'cached': True, 'elapsed_ms': 0, 'request_id': req_id})}\n\n"
        return Response(cached_gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    budget = create_work_budget()
    start = time.monotonic()

    def stream_gen():
        full_text = []
        streamed_bytes = 0
        backend_used = "local"
        try:
            for delta, backend in translate_text_stream(
                text,
                source_lang,
                target_lang,
                budget=budget,
                allow_cloud_fallback=allow_cloud_fallback,
                glossary=glossary,
            ):
                streamed_bytes += len(delta.encode("utf-8", errors="strict"))
                if streamed_bytes > BT_MAX_UPSTREAM_RESPONSE_BYTES:
                    log.warning(
                        "req=%s stream response exceeded byte cap", req_id
                    )
                    raise _StreamResponseTooLarge()
                full_text.append(delta)
                backend_used = backend
                yield f"data: {json.dumps({'delta': delta, 'cached': False})}\n\n"

            complete_translation = "".join(full_text)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _record_metric(elapsed_ms, hits=0, misses=1)

            try:
                contract = single_cache_contract(
                    source_lang, target_lang, glossary)
                scope = _cache_scope(
                    tenant=tenant,
                    book_id=book_id,
                    chapter_id=chapter_id,
                    context_hash=contract.context_hash,
                    provider=backend_used,
                    model=model_for_provider(backend_used),
                    prompt_hash=contract.prompt_hash,
                    protocol_version=contract.protocol_version,
                )
                put_cache(
                    text,
                    source_lang,
                    target_lang,
                    complete_translation,
                    scope=scope,
                )
                try:
                    batch_c = batch_cache_contract(
                        [text], [0], source_lang, target_lang, glossary)
                    batch_scope = _cache_scope(
                        tenant=tenant,
                        book_id=book_id,
                        chapter_id=chapter_id,
                        context_hash=batch_c.context_hash,
                        provider=backend_used,
                        model=model_for_provider(backend_used),
                        prompt_hash=batch_c.prompt_hash,
                        protocol_version=batch_c.protocol_version,
                    )
                    put_cache(
                        text,
                        source_lang,
                        target_lang,
                        complete_translation,
                        scope=batch_scope,
                    )
                except Exception:
                    pass
                _invalidate_stats_cache()
            except Exception as e:
                log.error("Failed to cache streamed translation: %s", e)

            yield f"event: done\ndata: {json.dumps({'translated': complete_translation, 'cached': False, 'backend': backend_used, 'elapsed_ms': elapsed_ms, 'request_id': req_id})}\n\n"
        except WorkBudgetExceeded as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _record_metric(elapsed_ms, hits=0, misses=1, error=True)
            _record_work_budget_exhaustion(exc.reason)
            log.warning(
                "req=%s stream work rejected reason=%s", req_id, exc.reason
            )
            payload = {
                "error": "work_budget_exhausted",
                "error_code": "work_budget_exhausted",
                "reason": exc.reason,
                "request_id": req_id,
            }
            if exc.reason == "queue":
                payload["retry_after_seconds"] = max(
                    1, math.ceil(BT_UPSTREAM_QUEUE_TIMEOUT)
                )
            yield f"event: error\ndata: {json.dumps(payload)}\n\n"
        except ProviderUnavailableError as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _record_metric(elapsed_ms, hits=0, misses=1, error=True)
            _record_outcome("provider_unavailable")
            log.warning(
                "req=%s stream provider unavailable error_code=%s",
                req_id, exc.error_code,
            )
            payload = {
                "error": exc.error_code or "provider_unavailable",
                "error_code": exc.error_code or "provider_unavailable",
                "request_id": req_id,
            }
            if exc.retry_after_seconds is not None:
                payload["retry_after_seconds"] = exc.retry_after_seconds
            yield f"event: error\ndata: {json.dumps(payload)}\n\n"
        except _StreamResponseTooLarge:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _record_metric(elapsed_ms, hits=0, misses=1, error=True)
            _record_outcome("translation_failed")
            yield f"event: error\ndata: {json.dumps({'error': 'response_too_large', 'error_code': 'response_too_large', 'request_id': req_id})}\n\n"
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _record_metric(elapsed_ms, hits=0, misses=1, error=True)
            _record_outcome("translation_failed")
            log.error("Stream generation failed error_type=%s", type(exc).__name__)
            yield f"event: error\ndata: {json.dumps({'error': 'translation_failed', 'error_code': 'translation_failed', 'request_id': req_id})}\n\n"

    return Response(
        stream_with_context(stream_gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.route("/translate/batch", methods=["POST"])
def translate_batch_endpoint():
    """
    Translate multiple paragraphs. Used for pre-fetching next pages.

    POST body: {
        "paragraphs": ["Paragraph 1", "Paragraph 2", ...],
        "source_lang": "English",
        "target_lang": "Spanish",
        "allow_cloud_fallback": false
    }

    Returns: {
        "translations": ["Translated 1", "Translated 2", ...],
        "backends": ["local", "cache", "minimax", ...],
        "cached": [false, true, false, ...],
        "cached_count": N,
        "fresh_count": M,
        "total_elapsed_ms": 12345,
        "request_id": "uuid"
    }

    Per-paragraph attribution (backends[i] / cached[i]) lets the frontend
    show "translated by local LLM" or "served from cache" badges, and lets
    operators confirm a request is hitting the backend they expect (e.g.
    that a fallback provider was used when the local one was down).
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    if "paragraphs" not in data or not isinstance(data["paragraphs"], list):
        return jsonify({"error": "Missing or invalid 'paragraphs' field"}), 400

    paragraphs = data["paragraphs"]
    if len(paragraphs) > BT_MAX_BATCH_PARAGRAPHS:
        return jsonify({
            "error": f"Too many paragraphs ({len(paragraphs)}); max {BT_MAX_BATCH_PARAGRAPHS} per request"
        }), 413
    if not all(isinstance(p, str) for p in paragraphs):
        return jsonify({"error": "All 'paragraphs' entries must be strings"}), 400
    invalid_unicode = next(
        (i for i, paragraph in enumerate(paragraphs)
         if _has_invalid_unicode(paragraph)),
        None,
    )
    if invalid_unicode is not None:
        return jsonify({
            "error": f"Paragraph {invalid_unicode} contains invalid Unicode"
        }), 400
    oversized = next((i for i, p in enumerate(paragraphs) if len(p) > BT_MAX_PARAGRAPH_CHARS), None)
    if oversized is not None:
        return jsonify({
            "error": f"Paragraph {oversized} exceeds the {BT_MAX_PARAGRAPH_CHARS}-character limit"
        }), 413

    source_lang = data.get("source_lang", "English")
    target_lang = data.get("target_lang", "Spanish")

    # Validate languages (H7)
    lang_error = _validate_languages(source_lang, target_lang)
    if lang_error:
        return jsonify({"error": lang_error}), 400

    try:
        allow_cloud_fallback = _cloud_fallback_consent(data)
        stale_provider_policy = _stale_provider_policy(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if stale_provider_policy:
        return jsonify({
            "error": "provider_policy_changed",
            "request_id": getattr(request, "request_id", None),
        }), 409

    try:
        tenant, book_id, chapter_id = _request_cache_namespace(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    budget = create_work_budget()

    # Short-circuit source==target: mirror /translate's behaviour. Echo every
    # paragraph back unchanged; mark as skipped so the frontend can distinguish
    # "no translation needed" from "translated and cached".
    if source_lang == target_lang:
        req_id = getattr(request, "request_id", None)
        log.info("req=%s short-circuit batch source==target (%s, %d paragraphs)",
                 req_id, source_lang, len(paragraphs))
        _record_metric(0, hits=0, misses=0)
        return jsonify({
            "translations": paragraphs,
            "backends": ["skipped"] * len(paragraphs),
            "cached": [False] * len(paragraphs),
            "error_codes": [None] * len(paragraphs),
            "retry_after_seconds": [None] * len(paragraphs),
            "cached_count": 0,
            "fresh_count": 0,
            "skipped": "source==target",
            "total_elapsed_ms": 0,
            "request_id": req_id,
        })

    # Provider prompts and cache fingerprints use the same normalized text.
    # Empty slots stay empty so response indices remain stable.
    paragraphs = [paragraph.strip() for paragraph in paragraphs]

    try:
        result = _translate_paragraphs(
            paragraphs,
            source_lang,
            target_lang,
            budget,
            tenant=tenant,
            book_id=book_id,
            chapter_id=chapter_id,
            allow_cloud_fallback=allow_cloud_fallback,
            glossary=_request_glossary(tenant, book_id),
        )
    except WorkBudgetExceeded as exc:
        _record_metric(0, hits=0, misses=1, error=True)
        return _work_budget_response(exc)
    except SegmentProtocolError:
        _record_metric(0, hits=0, misses=1, error=True)
        _record_outcome("invalid_provider_response")
        log.warning("req=%s provider returned an invalid segment envelope",
                    getattr(request, "request_id", None))
        return jsonify({
            "error": "invalid_provider_response",
            "request_id": getattr(request, "request_id", None),
        }), 502
    except ProviderUnavailableError:
        _record_metric(0, hits=0, misses=1, error=True)
        _record_outcome("provider_unavailable")
        log.warning(
            "req=%s batch provider unavailable",
            getattr(request, "request_id", None),
        )
        return jsonify({
            "error": "provider_unavailable",
            "request_id": getattr(request, "request_id", None),
        }), 502
    except Exception as exc:
        _record_metric(0, hits=0, misses=1, error=True)
        _record_outcome("translation_failed")
        log.error(
            "req=%s batch translation failed error_type=%s",
            getattr(request, "request_id", None), type(exc).__name__,
        )
        return jsonify({
            "error": "translation_failed",
            "request_id": getattr(request, "request_id", None),
        }), 500
    result["request_id"] = getattr(request, "request_id", None)

    _record_metric(result["total_elapsed_ms"], hits=result["cached_count"], misses=result["fresh_count"])
    partial_failures = sum(
        isinstance(value, str) and value.startswith("[TRANSLATION ERROR:")
        for value in result["translations"]
    )
    if partial_failures:
        _record_batch_partial_failure(partial_failures)

    return jsonify(result)




@app.route("/export/epub", methods=["POST"])
def export_epub():
    """Rebuild a minimal EPUB from already-translated chapter text.

    POST body: {
        "paragraphs": ["Translated 1", "Translated 2", ...],
        "title": "Chapter 1",
        "source_lang": "English",
        "target_lang": "Spanish"
    }

    Returns the rebuilt EPUB as application/epub+zip. Authentication and
    per-client rate limiting apply via the shared before-request hook; no
    LLM work is spent here, so no provider policy or cloud-consent fields
    are accepted. Book/chapter identifiers are validated with the shared
    scope contract so client metadata stays bounded.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    if "paragraphs" not in data or not isinstance(data["paragraphs"], list):
        return jsonify({"error": "Missing or invalid 'paragraphs' field"}), 400

    paragraphs = data["paragraphs"]
    if not paragraphs:
        return jsonify({"error": "'paragraphs' must not be empty"}), 400
    if len(paragraphs) > BT_MAX_EXPORT_PARAGRAPHS:
        return jsonify({
            "error": f"Too many paragraphs ({len(paragraphs)}); max {BT_MAX_EXPORT_PARAGRAPHS} per export"
        }), 413
    if not all(isinstance(p, str) for p in paragraphs):
        return jsonify({"error": "All 'paragraphs' entries must be strings"}), 400
    invalid_unicode = next(
        (i for i, paragraph in enumerate(paragraphs)
         if _has_invalid_unicode(paragraph)),
        None,
    )
    if invalid_unicode is not None:
        return jsonify({
            "error": f"Paragraph {invalid_unicode} contains invalid Unicode"
        }), 400
    oversized = next((i for i, p in enumerate(paragraphs) if len(p) > BT_MAX_PARAGRAPH_CHARS), None)
    if oversized is not None:
        return jsonify({
            "error": f"Paragraph {oversized} exceeds the {BT_MAX_PARAGRAPH_CHARS}-character limit"
        }), 413
    total_chars = sum(len(p) for p in paragraphs)
    if total_chars > BT_MAX_EXPORT_TOTAL_CHARS:
        return jsonify({
            "error": f"Export exceeds the {BT_MAX_EXPORT_TOTAL_CHARS}-character limit"
        }), 413

    title = data.get("title", "Translated chapter")
    if not isinstance(title, str) or not title.strip():
        return jsonify({"error": "Missing or invalid 'title' field"}), 400
    title = title.strip()
    if len(title) > BT_MAX_EXPORT_TITLE_CHARS:
        return jsonify({
            "error": f"'title' exceeds the {BT_MAX_EXPORT_TITLE_CHARS}-character limit"
        }), 413
    if _has_invalid_unicode(title):
        return jsonify({"error": "'title' contains invalid Unicode"}), 400

    source_lang = data.get("source_lang", "English")
    target_lang = data.get("target_lang", "Spanish")
    lang_error = _validate_languages(source_lang, target_lang)
    if lang_error:
        return jsonify({"error": lang_error}), 400

    try:
        _request_cache_namespace(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        epub_bytes = build_epub(
            title,
            [paragraph.strip() for paragraph in paragraphs],
            target_lang=target_lang,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    response = Response(epub_bytes, mimetype="application/epub+zip")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{epub_filename(title)}"'
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/glossary", methods=["GET"])
def glossary_list():
    """List stored glossary terms for one book.

    Query params: book_id, chapter_id (both optional, same contract as the
    translate endpoints). Returns {"entries": [{"source": .., "target": ..}]}.
    """
    try:
        tenant, book_id, _chapter_id = _request_cache_namespace(
            request.args.to_dict()
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        entries = glossary_store.list_entries(tenant, book_id)
    except Exception as exc:
        log.error("Glossary list failed error_type=%s", type(exc).__name__)
        return jsonify({
            "error": "glossary_unavailable",
            "request_id": getattr(request, "request_id", None),
        }), 500
    return jsonify({
        "entries": entries,
        "request_id": getattr(request, "request_id", None),
    })


@app.route("/glossary", methods=["POST"])
def glossary_upsert():
    """Insert or replace one glossary term for one book.

    POST body: {"source": "..", "target": "..",
                "book_id": "..", "chapter_id": ".."}.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    try:
        tenant, book_id, _chapter_id = _request_cache_namespace(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not isinstance(data.get("source"), str) or not isinstance(
        data.get("target"), str
    ):
        return jsonify(
            {"error": "Missing or invalid 'source'/'target' field"}), 400
    try:
        entry = glossary_store.put_entry(
            tenant, book_id, data["source"], data["target"]
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.error("Glossary write failed error_type=%s", type(exc).__name__)
        return jsonify({
            "error": "glossary_unavailable",
            "request_id": getattr(request, "request_id", None),
        }), 500
    _invalidate_stats_cache()
    return jsonify({
        "entry": entry,
        "request_id": getattr(request, "request_id", None),
    })


@app.route("/glossary", methods=["DELETE"])
def glossary_delete():
    """Delete one glossary term for one book.

    DELETE body: {"source": "..", "book_id": "..", "chapter_id": ".."}.
    Returns 404 when the term does not exist.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    try:
        tenant, book_id, _chapter_id = _request_cache_namespace(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not isinstance(data.get("source"), str):
        return jsonify({"error": "Missing or invalid 'source' field"}), 400
    try:
        removed = glossary_store.delete_entry(tenant, book_id, data["source"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.error("Glossary delete failed error_type=%s", type(exc).__name__)
        return jsonify({
            "error": "glossary_unavailable",
            "request_id": getattr(request, "request_id", None),
        }), 500
    if not removed:
        return jsonify({
            "error": "glossary_term_not_found",
            "request_id": getattr(request, "request_id", None),
        }), 404
    _invalidate_stats_cache()
    return jsonify({
        "deleted": True,
        "request_id": getattr(request, "request_id", None),
    })


@app.route("/feedback", methods=["POST"])
def feedback_record():
    """Record one per-paragraph rating for one book.

    POST body: {"para_key": "<opaque client paragraph key>",
                "rating": +1/-1 (or "up"/"down"),
                "book_id": "..", "chapter_id": ".."}.
    The key is an opaque identifier (hash of source text plus scope);
    raw book text is never accepted or stored here.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    try:
        tenant, book_id, _chapter_id = _request_cache_namespace(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if "para_key" not in data or "rating" not in data:
        return jsonify(
            {"error": "Missing 'para_key'/'rating' field"}), 400
    try:
        record = feedback_store.record_feedback(
            tenant, book_id, data["para_key"], data["rating"]
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.error("Feedback write failed error_type=%s", type(exc).__name__)
        return jsonify({
            "error": "feedback_unavailable",
            "request_id": getattr(request, "request_id", None),
        }), 500
    return jsonify({
        "feedback": record,
        "request_id": getattr(request, "request_id", None),
    })


@app.route("/feedback/summary", methods=["GET"])
def feedback_summary():
    """Return rating totals for one book: up/down/total/score.

    Query params: book_id, chapter_id (both optional, same contract as the
    translate endpoints).
    """
    try:
        tenant, book_id, _chapter_id = _request_cache_namespace(
            request.args.to_dict()
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        summary = feedback_store.get_summary(tenant, book_id)
    except Exception as exc:
        log.error("Feedback summary failed error_type=%s", type(exc).__name__)
        return jsonify({
            "error": "feedback_unavailable",
            "request_id": getattr(request, "request_id", None),
        }), 500
    return jsonify({
        "summary": summary,
        "request_id": getattr(request, "request_id", None),
    })



# ── Text-to-Speech (Speaches Kokoro) ───────────────────────────────────────


@app.route("/tts/status", methods=["GET"])
def tts_status():
    """Return health and model info for the local Speaches TTS engine."""
    return jsonify(TTS_SERVICE.check_health())


@app.route("/tts/synthesize", methods=["POST"])
def tts_synthesize():
    """Synthesize text into high-fidelity neural MP3 audio via Speaches Kokoro.

    JSON payload:
      text (str, required): Text to synthesize (max 5000 chars)
      lang (str, optional): Target language for voice selection
      voice (str, optional): Explicit Kokoro voice override
      speed (float, optional): Speech rate multiplier (0.5 to 2.0)
    """
    if not TTS_SERVICE.is_enabled:
        return jsonify({"error": "tts_disabled", "message": "TTS backend is disabled"}), 400
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "invalid_payload", "message": "text is required"}), 400
    if len(text) > 5000:
        return jsonify({"error": "text_too_long", "message": "text exceeds 5000 characters"}), 400
    lang = data.get("lang")
    voice = data.get("voice")
    speed = float(data.get("speed", 1.0))
    try:
        audio_bytes, content_type = TTS_SERVICE.synthesize(
            text, lang=lang, voice=voice, speed=speed
        )
        return Response(
            audio_bytes,
            mimetype=content_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                "Content-Length": str(len(audio_bytes)),
            },
        )
    except Exception as exc:
        log.error("TTS synthesis error: %s", exc)
        return jsonify({"error": "tts_error", "message": str(exc)}), 502


class CleanupCredentialUnavailable(RuntimeError):
    """The destructive endpoint cannot establish a shared credential."""


@app.route("/cache/cleanup", methods=["POST"])
def cache_cleanup():
    """Evict old cache entries. Optional body: {"days": 30}.
    `days` must be an integer >= 1 — a negative value would match every row
    (created_at < future date) and silently wipe the whole cache.

    Auth: always required. Two sources of truth for the token, in order:
      1. BT_API_TOKEN env var (the recommended operator credential; in token
         auth mode it is also the request credential)
      2. Auto-generated deployment token persisted in /app/data/cleanup_token
         (only consulted when BT_API_TOKEN is empty). The token is generated
         on first use with secrets.token_urlsafe and written to a private file
         (0600 by default, or 0640 for the managed Compose operator group; the
         value itself is never logged — read it with
         `docker exec <container> cat /app/data/cleanup_token`). This is
         the fail-safe: an operator who forgets to set BT_API_TOKEN does
         NOT get an unauthenticated destructive endpoint on their LAN.

    Tests can monkeypatch `_get_cleanup_token` to return a known value."""
    try:
        token = _get_cleanup_token()
    except CleanupCredentialUnavailable:
        return jsonify({
            "error": "cleanup_credential_unavailable",
            "request_id": getattr(request, "request_id", None),
        }), 503
    request_token = request.headers.get("X-BT-Token", "")
    if not _token_matches(request_token, token):
        return jsonify({
            "error": "Unauthorized",
            "request_id": getattr(request, "request_id", None),
        }), 401

    raw_body = request.get_data(cache=True)
    if not raw_body:
        data = {}
    else:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Request body must be a JSON object"}), 400
    days = data.get("days", 30)
    if isinstance(days, bool) or not isinstance(days, int) or not (1 <= days <= 3650):
        return jsonify({"error": "'days' must be an integer between 1 and 3650"}), 400
    deleted = cleanup_old_entries(days=days)
    _invalidate_stats_cache()
    return jsonify({"deleted": deleted, "days": days})


# ── Cleanup-token auto-generation ──────────────────────────────────────────
# Persistent path inside the data dir, alongside the sqlite database. The
# file is intentionally named without a leading dot so `ls` shows it by
# default — operators need to be able to see it to understand the auth model.
import fcntl as _fcntl  # Linux/Alpine process lock for the persisted secret
import secrets as _secrets  # local: small surface
import stat as _stat
import tempfile as _tempfile
_CLEANUP_TOKEN_PATH = Path(os.environ.get("BT_CACHE_DIR", "/app/data")) / "cleanup_token"
_CLEANUP_FILE_MODE = (
    0o640
    if os.environ.get("BT_CACHE_OPERATOR_GROUP_ACCESS", "false").lower()
    in ("1", "true", "yes")
    else 0o600
)
_cleanup_token_cache: str | None = None
_cleanup_token_lock = threading.Lock()


def _read_cleanup_token_file() -> str:
    """Read the persisted token without following symlinks, repairing mode."""
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    try:
        fd = os.open(_CLEANUP_TOKEN_PATH, flags)
    except FileNotFoundError:
        return ""

    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("cleanup token path is not a regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as token_file:
            fd = -1  # fdopen owns and closes it from this point.
            os.fchmod(token_file.fileno(), _CLEANUP_FILE_MODE)
            # A generated token is ~43 bytes. Refuse an unexpectedly large
            # file rather than letting a corrupt volume consume unbounded RAM.
            value = token_file.read(4097)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(value) > 4096:
        raise OSError("cleanup token file exceeds 4096 bytes")
    return value.strip()


def _persist_cleanup_token() -> tuple[str, bool]:
    """Read or atomically create the shared token under an OS file lock."""
    _CLEANUP_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _CLEANUP_TOKEN_PATH.with_name(
        f"{_CLEANUP_TOKEN_PATH.name}.lock")
    lock_flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                  | getattr(os, "O_NONBLOCK", 0))
    lock_fd = os.open(lock_path, lock_flags, _CLEANUP_FILE_MODE)

    if not _stat.S_ISREG(os.fstat(lock_fd).st_mode):
        os.close(lock_fd)
        raise OSError("cleanup token lock path is not a regular file")

    with os.fdopen(lock_fd, "r+", encoding="utf-8") as lock_file:
        os.fchmod(lock_file.fileno(), _CLEANUP_FILE_MODE)
        _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)

        existing = _read_cleanup_token_file()
        if existing:
            return existing, False

        token = _secrets.token_urlsafe(32)
        temp_fd, temp_name = _tempfile.mkstemp(
            prefix=f".{_CLEANUP_TOKEN_PATH.name}.",
            dir=_CLEANUP_TOKEN_PATH.parent,
        )
        try:
            os.fchmod(temp_fd, _CLEANUP_FILE_MODE)
            with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
                temp_fd = -1  # fdopen owns and closes it from this point.
                temp_file.write(token)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_name, _CLEANUP_TOKEN_PATH)
            temp_name = ""

            # Persist the rename itself before releasing the inter-process
            # lock, so a host crash cannot expose a partially-created secret.
            directory_fd = os.open(
                _CLEANUP_TOKEN_PATH.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if temp_name:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
        return token, True


def _get_cleanup_token() -> str:
    """Return the active token for /cache/cleanup.

    Order:
      1. If BT_API_TOKEN is set, use it as the operator credential. Token auth
         mode also uses it for the general request boundary.
      2. Else, read or create /app/data/cleanup_token. Creation is serialized
         across threads and processes, then atomically persisted with the
         configured private file mode. Subsequent calls reuse the in-process
         cached value.
    """
    global _cleanup_token_cache
    if API_TOKEN:
        return API_TOKEN
    with _cleanup_token_lock:
        if _cleanup_token_cache is not None:
            return _cleanup_token_cache
        try:
            _cleanup_token_cache, generated = _persist_cleanup_token()
            if generated:
                log.warning(
                    "BT_API_TOKEN not set. Auto-generated a /cache/cleanup token and "
                    "persisted it at %s (mode %04o). Read it with: "
                    "docker exec <container> cat %s — the value is intentionally "
                    "NOT logged (logs are not secret storage). Set BT_API_TOKEN to "
                    "use a fixed value instead.",
                    _CLEANUP_TOKEN_PATH,
                    _CLEANUP_FILE_MODE,
                    _CLEANUP_TOKEN_PATH,
                )
        except Exception as exc:
            # A per-process fallback would create different credentials across
            # Gunicorn workers and make a destructive endpoint nondeterministic.
            # Fail closed until the shared data volume is writable again.
            log.error(
                "Cleanup credential persistence failed type=%s; endpoint disabled",
                type(exc).__name__,
            )
            raise CleanupCredentialUnavailable from exc
        return _cleanup_token_cache


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT") or os.environ.get("API_PORT") or os.environ.get("BT_API_PORT") or "8390")
    log.info("Starting book-translator on port %d...", port)
    app.run(host="0.0.0.0", port=port, debug=False)

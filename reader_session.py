"""Short-lived, reader-neutral browser session broker.

Raw reader credentials are accepted only during an explicit exchange. Ordinary
translation requests use an opaque, five-minute cookie whose server-side record
contains no upstream credential or personally identifying value.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

import requests
from urllib3.util import SKIP_HEADER

from btctl_core import parse_positive_int as _parse_positive_int


_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_CHUNKED_RE = re.compile(r"^chunks-([1-9][0-9]?)$")
_CONNECTOR_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_KAVITA_COOKIE = ".AspNetCore.Cookies"
_MAX_COOKIE_HEADER = 16_384
_MAX_AUTH_HEADER = 8_192
_MAX_CHUNKS = 16


class BrokerConfigError(ValueError):
    """The broker cannot establish a safe startup configuration."""


class BrokerRejected(RuntimeError):
    """The request did not prove a valid reader identity."""


class BrokerUnavailable(RuntimeError):
    """The reader authority could not be checked within its budget."""


@dataclass(frozen=True, slots=True)
class SessionIssue:
    token: str
    subject: str
    expires_in: int
    set_cookie: str


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    subject: str
    expires_at: float
    address_binding: bytes
    user_agent_binding: bytes


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    for candidate, value in headers.items():
        if str(candidate).casefold() == wanted:
            return str(value)
    return ""


def _clean_origin(value: str, *, allow_loopback_http: bool = True) -> tuple[str, bool]:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value.isascii()
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise BrokerConfigError("BT_PUBLIC_ORIGIN must be one clean origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise BrokerConfigError("BT_PUBLIC_ORIGIN must be one http(s) origin") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise BrokerConfigError("BT_PUBLIC_ORIGIN must be one http(s) origin")
    host = parsed.hostname.casefold()
    loopback = host == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (allow_loopback_http and loopback):
        raise BrokerConfigError("reader sessions require HTTPS outside loopback development")
    normalized_host = f"[{host}]" if ":" in host else host
    if port is not None:
        normalized_host += f":{port}"
    normalized = f"{parsed.scheme}://{normalized_host}"
    if value.rstrip("/").casefold() != normalized.casefold():
        raise BrokerConfigError("BT_PUBLIC_ORIGIN must contain one exact authority")
    return normalized, parsed.scheme == "https"


def _validate_auth_url(reader_type: str, value: str) -> str:
    expected_path = "/api/Account" if reader_type == "kavita" else "/ajax/emailstat"
    try:
        parsed = urlsplit(value)
        parsed.port
    except (TypeError, ValueError) as exc:
        raise BrokerConfigError("BT_READER_AUTH_URL is invalid") from exc
    if (
        not isinstance(value, str)
        or value != value.strip()
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise BrokerConfigError(
            f"BT_READER_AUTH_URL must target exact {expected_path}"
        )
    return value


def _positive_float(value: object, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BrokerConfigError(f"{name} must be finite and positive") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise BrokerConfigError(f"{name} must be finite and positive")
    return parsed


def _positive_int(value: object, name: str) -> int:
    """Shared positive-integer parse; canonical logic lives in btctl_core."""
    try:
        return _parse_positive_int(value)
    except ValueError as exc:
        raise BrokerConfigError(f"{name} must be a positive integer") from exc


def _binding_parts(binding: object) -> tuple[bytes, bytes]:
    address = getattr(binding, "cwa_remote_addr", None)
    user_agent = getattr(binding, "user_agent", None)
    if (
        not isinstance(address, str)
        or not address
        or len(address) > 64
        or address != address.strip()
        or "," in address
        or "%" in address
    ):
        raise BrokerRejected("authentication rejected")
    try:
        address_bytes = ipaddress.ip_address(address).compressed.encode("ascii")
    except ValueError:
        raise BrokerRejected("authentication rejected") from None
    if user_agent is None:
        return address_bytes, b"\x00"
    if not isinstance(user_agent, str) or any(
        ord(character) < 32 or ord(character) == 127 for character in user_agent
    ):
        raise BrokerRejected("authentication rejected")
    try:
        encoded = user_agent.encode("latin-1", errors="strict")
    except UnicodeEncodeError:
        raise BrokerRejected("authentication rejected") from None
    if len(encoded) > 4096:
        raise BrokerRejected("authentication rejected")
    return address_bytes, b"\x01" + encoded


def _parse_cookie_header(raw: str) -> OrderedDict[str, str]:
    if (
        not isinstance(raw, str)
        or len(raw) > _MAX_COOKIE_HEADER
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise BrokerRejected("authentication rejected")
    parsed: OrderedDict[str, str] = OrderedDict()
    if not raw:
        return parsed
    for part in raw.split(";"):
        item = part.strip()
        if not item or "=" not in item:
            raise BrokerRejected("authentication rejected")
        name, value = item.split("=", 1)
        if (
            not _COOKIE_NAME_RE.fullmatch(name)
            or name in parsed
            or not value
            or len(value) > 8192
            or any(ord(character) < 33 or ord(character) == 127 for character in value)
        ):
            raise BrokerRejected("authentication rejected")
        parsed[name] = value
    return parsed


def _read_bounded_json(response: object, *, limit: int, deadline: float, clock) -> object:
    content_type = str(response.headers.get("Content-Type", ""))
    if content_type.split(";", 1)[0].strip().casefold() != "application/json":
        raise BrokerRejected("authentication rejected")
    declared = str(response.headers.get("Content-Length", "")).strip()
    if declared:
        try:
            size = int(declared, 10)
        except ValueError:
            raise BrokerUnavailable("authentication authority unavailable") from None
        if size < 0 or size > limit:
            raise BrokerUnavailable("authentication authority unavailable")
    payload = bytearray()
    for chunk in response.iter_content(chunk_size=8192):
        if clock() >= deadline:
            raise BrokerUnavailable("authentication authority unavailable")
        if not isinstance(chunk, bytes) or len(payload) + len(chunk) > limit:
            raise BrokerUnavailable("authentication authority unavailable")
        payload.extend(chunk)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BrokerRejected("authentication rejected") from None


class ReaderSessionBroker:
    """Exchange upstream reader credentials for bounded opaque sessions."""

    def __init__(
        self,
        *,
        reader_type: str,
        auth_url: str,
        reader_version: str,
        connector_id: str,
        public_origin: str,
        secret_key: bytes,
        cookie_name: str | None = None,
        ttl_seconds: int = 300,
        max_entries: int = 10_000,
        timeout_seconds: float = 2.0,
        max_response_bytes: int = 262_144,
        http_get: Callable[..., object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if reader_type not in {"cwa", "kavita"}:
            raise BrokerConfigError("BT_READER_TYPE must be cwa or kavita")
        if reader_type == "kavita" and reader_version != "0.9.0.2":
            raise BrokerConfigError("Kavita reader version is not certified")
        if not isinstance(reader_version, str) or not reader_version:
            raise BrokerConfigError("BT_READER_VERSION is required")
        if not isinstance(connector_id, str) or not _CONNECTOR_RE.fullmatch(connector_id):
            raise BrokerConfigError("BT_READER_CONNECTOR_ID must be a canonical UUID")
        if not isinstance(secret_key, bytes) or len(secret_key) < 32:
            raise BrokerConfigError("reader session key must contain at least 256 bits")
        ttl = _positive_int(ttl_seconds, "BT_SESSION_TTL_SECONDS")
        if ttl > 300:
            raise BrokerConfigError("BT_SESSION_TTL_SECONDS must not exceed 300")
        self.reader_type = reader_type
        self.reader_version = reader_version
        self.auth_url = _validate_auth_url(reader_type, auth_url)
        self.connector_id = connector_id
        self.public_origin, self.secure_cookie = _clean_origin(public_origin)
        default_cookie_name = "__Host-bt-session" if self.secure_cookie else "bt-session"
        selected_cookie_name = cookie_name or default_cookie_name
        if not isinstance(selected_cookie_name, str) or not _COOKIE_NAME_RE.fullmatch(
            selected_cookie_name
        ):
            raise BrokerConfigError("BT_SESSION_COOKIE_NAME is invalid")
        if self.secure_cookie != selected_cookie_name.startswith("__Host-"):
            raise BrokerConfigError(
                "BT_SESSION_COOKIE_NAME must match the public origin security boundary"
            )
        self.cookie_name = selected_cookie_name
        self.ttl_seconds = ttl
        self.max_entries = _positive_int(max_entries, "BT_SESSION_MAX_ENTRIES")
        self.timeout_seconds = _positive_float(
            timeout_seconds, "BT_READER_AUTH_TIMEOUT_SECONDS"
        )
        self.max_response_bytes = _positive_int(
            max_response_bytes, "BT_READER_AUTH_MAX_RESPONSE_BYTES"
        )
        self._secret = secret_key
        self._http_get = http_get
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._sessions: OrderedDict[bytes, _SessionRecord] = OrderedDict()
        self._lock = threading.Lock()

    def _hmac(self, label: bytes, value: bytes) -> bytes:
        return hmac.digest(self._secret, label + b"\x00" + value, "sha256")

    def _token_digest(self, token: str) -> bytes:
        return self._hmac(b"token", token.encode("ascii"))

    def _subject(self, upstream_identity: str) -> str:
        material = "\0".join(
            (self.reader_type, self.connector_id, upstream_identity)
        ).encode("utf-8")
        return (
            f"reader-session:{self.reader_type}:"
            + self._hmac(b"subject", material).hex()
        )

    def _validate_origin(self, headers: Mapping[str, str]) -> None:
        origin = _header(headers, "Origin")
        if not origin or not hmac.compare_digest(origin, self.public_origin):
            raise BrokerRejected("authentication rejected")
        fetch_site = _header(headers, "Sec-Fetch-Site")
        if fetch_site and fetch_site != "same-origin":
            raise BrokerRejected("authentication rejected")

    def _credentials(
        self, headers: Mapping[str, str]
    ) -> tuple[dict[str, str], str | None]:
        authorization = _header(headers, "Authorization")
        cookies = _parse_cookie_header(_header(headers, "Cookie"))
        prior = cookies.pop(self.cookie_name, None)
        if self.reader_type == "cwa":
            if authorization or not cookies:
                raise BrokerRejected("authentication rejected")
            if any(name not in {"session", "remember_token"} for name in cookies):
                raise BrokerRejected("authentication rejected")
            ordered = [
                (name, cookies[name])
                for name in ("session", "remember_token")
                if name in cookies
            ]
            if not ordered:
                raise BrokerRejected("authentication rejected")
            return {"Cookie": "; ".join(f"{k}={v}" for k, v in ordered)}, prior

        bearer = ""
        if authorization:
            if (
                len(authorization) > _MAX_AUTH_HEADER
                or not authorization.startswith("Bearer ")
                or authorization.count(" ") != 1
            ):
                raise BrokerRejected("authentication rejected")
            bearer = authorization[7:]
            if not bearer or any(
                ord(character) < 33 or ord(character) > 126 for character in bearer
            ):
                raise BrokerRejected("authentication rejected")

        oidc_names = {
            name for name in cookies if name == _KAVITA_COOKIE or name.startswith(_KAVITA_COOKIE + "C")
        }
        has_oidc = _KAVITA_COOKIE in cookies
        if bearer and oidc_names:
            raise BrokerRejected("authentication rejected")
        if bearer:
            return {"Authorization": f"Bearer {bearer}"}, prior
        if not has_oidc:
            raise BrokerRejected("authentication rejected")

        base = cookies[_KAVITA_COOKIE]
        match = _CHUNKED_RE.fullmatch(base)
        if match:
            count = int(match.group(1), 10)
            if count > _MAX_CHUNKS:
                raise BrokerRejected("authentication rejected")
            expected = {
                _KAVITA_COOKIE,
                *(_KAVITA_COOKIE + f"C{index}" for index in range(1, count + 1)),
            }
            if oidc_names != expected:
                raise BrokerRejected("authentication rejected")
            ordered_names = [_KAVITA_COOKIE] + [
                _KAVITA_COOKIE + f"C{index}" for index in range(1, count + 1)
            ]
        else:
            if oidc_names != {_KAVITA_COOKIE}:
                raise BrokerRejected("authentication rejected")
            ordered_names = [_KAVITA_COOKIE]
        return {
            "Cookie": "; ".join(f"{name}={cookies[name]}" for name in ordered_names)
        }, prior

    def _probe(self, credentials: dict[str, str], binding: object) -> str:
        address, user_agent = _binding_parts(binding)
        session = None
        response = None
        deadline = self._clock() + self.timeout_seconds
        try:
            get = self._http_get
            if get is None:
                session = requests.Session()
                session.trust_env = False
                get = session.get
            headers = {"Accept": "application/json", **credentials}
            headers["X-Forwarded-For"] = address.decode("ascii")
            if user_agent == b"\x00":
                headers["User-Agent"] = SKIP_HEADER
            else:
                headers["User-Agent"] = user_agent[1:].decode("latin-1")
            response = get(
                self.auth_url,
                headers=headers,
                timeout=self.timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
            if self._clock() >= deadline:
                raise BrokerUnavailable("authentication authority unavailable")
            status = int(response.status_code)
            if status != 200:
                if status in {401, 403} or 300 <= status < 400:
                    raise BrokerRejected("authentication rejected")
                raise BrokerUnavailable("authentication authority unavailable")
            payload = _read_bounded_json(
                response,
                limit=self.max_response_bytes,
                deadline=deadline,
                clock=self._clock,
            )
            if self.reader_type == "cwa":
                if not isinstance(payload, list) or not all(
                    isinstance(item, dict) for item in payload
                ):
                    raise BrokerRejected("authentication rejected")
                credential_material = credentials["Cookie"]
                return hashlib.sha256(credential_material.encode("ascii")).hexdigest()
            if not isinstance(payload, dict):
                raise BrokerRejected("authentication rejected")
            user_id = payload.get("id")
            version = payload.get("kavitaVersion")
            if (
                isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or user_id <= 0
                or version != self.reader_version
            ):
                raise BrokerRejected("authentication rejected")
            return str(user_id)
        except (BrokerRejected, BrokerUnavailable):
            raise
        except Exception as exc:
            raise BrokerUnavailable("authentication authority unavailable") from exc
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def _remove_token(self, token: str | None) -> None:
        if not token or not _TOKEN_RE.fullmatch(token):
            return
        with self._lock:
            self._sessions.pop(self._token_digest(token), None)

    def exchange(
        self, headers: Mapping[str, str], binding: object
    ) -> SessionIssue:
        self._validate_origin(headers)
        credentials, prior = self._credentials(headers)
        upstream_identity = self._probe(credentials, binding)
        address, user_agent = _binding_parts(binding)
        subject = self._subject(upstream_identity)
        token = self._token_factory()
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise BrokerUnavailable("session credential generation failed")
        now = self._clock()
        record = _SessionRecord(
            subject=subject,
            expires_at=now + self.ttl_seconds,
            address_binding=self._hmac(b"address", address),
            user_agent_binding=self._hmac(b"user-agent", user_agent),
        )
        with self._lock:
            if prior and _TOKEN_RE.fullmatch(prior):
                self._sessions.pop(self._token_digest(prior), None)
            self._sessions[self._token_digest(token)] = record
            self._sessions.move_to_end(self._token_digest(token))
            expired = [
                digest
                for digest, candidate in self._sessions.items()
                if candidate.expires_at <= now
            ]
            for digest in expired:
                self._sessions.pop(digest, None)
            while len(self._sessions) > self.max_entries:
                self._sessions.popitem(last=False)
        flags = [
            f"{self.cookie_name}={token}",
            "Path=/",
            f"Max-Age={self.ttl_seconds}",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if self.secure_cookie:
            flags.append("Secure")
        return SessionIssue(token, subject, self.ttl_seconds, "; ".join(flags))

    def authenticate(self, headers: Mapping[str, str], binding: object) -> str:
        if _header(headers, "Authorization"):
            raise BrokerRejected("authentication rejected")
        cookies = _parse_cookie_header(_header(headers, "Cookie"))
        if set(cookies) != {self.cookie_name}:
            raise BrokerRejected("authentication rejected")
        token = cookies[self.cookie_name]
        if not _TOKEN_RE.fullmatch(token):
            raise BrokerRejected("authentication rejected")
        address, user_agent = _binding_parts(binding)
        digest = self._token_digest(token)
        now = self._clock()
        with self._lock:
            record = self._sessions.get(digest)
            if record is None or record.expires_at <= now:
                self._sessions.pop(digest, None)
                raise BrokerRejected("authentication rejected")
            if not hmac.compare_digest(
                record.address_binding, self._hmac(b"address", address)
            ) or not hmac.compare_digest(
                record.user_agent_binding, self._hmac(b"user-agent", user_agent)
            ):
                raise BrokerRejected("authentication rejected")
            self._sessions.move_to_end(digest)
            return record.subject

    def revoke(self, headers: Mapping[str, str], binding: object) -> None:
        cookies = _parse_cookie_header(_header(headers, "Cookie"))
        if not cookies:
            return
        if set(cookies) != {self.cookie_name}:
            raise BrokerRejected("authentication rejected")
        token = cookies[self.cookie_name]
        if _TOKEN_RE.fullmatch(token):
            # A mismatched binding must not become a token-enumeration oracle;
            # authenticate first, then remove the exact digest.
            self.authenticate(headers, binding)
            self._remove_token(token)

    @property
    def clear_cookie(self) -> str:
        flags = [
            f"{self.cookie_name}=",
            "Path=/",
            "Max-Age=0",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if self.secure_cookie:
            flags.append("Secure")
        return "; ".join(flags)


def load_or_create_session_key(path: Path) -> bytes:
    """Load or atomically create one private 256-bit connector secret."""
    target = Path(path)
    if target.is_symlink() or target.parent.is_symlink():
        raise BrokerConfigError("reader session key path must not be a symbolic link")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.parent.is_symlink() or not target.parent.is_dir():
            raise BrokerConfigError("reader session key parent must be a directory")
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except FileExistsError:
            descriptor = None
        if descriptor is not None:
            payload = secrets.token_bytes(32)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(target, 0o600, follow_symlinks=False)
                directory_fd = os.open(
                    target.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except BaseException:
                try:
                    target.unlink()
                except OSError:
                    pass
                raise
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size != 32
            ):
                raise BrokerConfigError("reader session key must be one private owned file")
            payload = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
        if len(payload) != 32:
            raise BrokerConfigError("reader session key has an invalid length")
        return payload
    except BrokerConfigError:
        raise
    except OSError as exc:
        raise BrokerConfigError("reader session key is unavailable") from exc


def broker_from_environment(
    env: Mapping[str, str] | None = None,
    *,
    http_get: Callable[..., object] | None = None,
) -> ReaderSessionBroker:
    source = env if env is not None else os.environ
    key_path = Path(source.get("BT_SESSION_KEY_PATH", "/app/data/reader_session_key"))
    return ReaderSessionBroker(
        reader_type=source.get("BT_READER_TYPE", ""),
        auth_url=source.get("BT_READER_AUTH_URL", ""),
        reader_version=source.get("BT_READER_VERSION", ""),
        connector_id=source.get("BT_READER_CONNECTOR_ID", ""),
        public_origin=source.get("BT_PUBLIC_ORIGIN", ""),
        secret_key=load_or_create_session_key(key_path),
        cookie_name=source.get("BT_SESSION_COOKIE_NAME") or None,
        ttl_seconds=source.get("BT_SESSION_TTL_SECONDS", "300"),
        max_entries=source.get("BT_SESSION_MAX_ENTRIES", "10000"),
        timeout_seconds=source.get("BT_READER_AUTH_TIMEOUT_SECONDS", "2"),
        max_response_bytes=source.get(
            "BT_READER_AUTH_MAX_RESPONSE_BYTES", "262144"
        ),
        http_get=http_get,
    )

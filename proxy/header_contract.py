"""Reader login assertions cannot alias managed HTTP context or credentials."""

from __future__ import annotations

import re


_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_MANAGED_HEADERS = frozenset({
    "authorization", "connection", "content-length", "cookie", "forwarded",
    "host", "transfer-encoding", "upgrade", "user-agent", "x-authentik-groups",
    "x-authentik-uid", "x-bt-roles", "x-bt-subject", "x-forwarded-for",
    "x-forwarded-host", "x-forwarded-port", "x-forwarded-proto", "x-real-ip",
})


def validate_reader_identity_header(value: object) -> str:
    if not isinstance(value, str) or not value.isascii() or not _HEADER_NAME.fullmatch(value):
        raise ValueError("BT_CWA_IDENTITY_HEADER must be one bounded HTTP header name")
    if value.casefold() in _MANAGED_HEADERS:
        raise ValueError("BT_CWA_IDENTITY_HEADER conflicts with a proxy-managed security header")
    return value

"""Bounded exact-peer trust shared by installers and the nginx renderer."""

from __future__ import annotations

import ipaddress


def parse_edge_trusted_proxies(value: object) -> tuple[str, ...]:
    """Return a stable set of exact IP peers without exposing rejected input."""
    error = "BT_EDGE_TRUSTED_PROXIES must be a bounded list of at most 32 exact IP peers"
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or value != value.strip()
        or not value.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(error)
    if not value:
        return ()
    peers: set[str] = set()
    for item in value.split(","):
        peer = item.strip()
        if not peer or "%" in peer:
            raise ValueError(error)
        address, separator, prefix = peer.partition("/")
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise ValueError(error) from None
        if separator and prefix != str(parsed.max_prefixlen):
            raise ValueError(error)
        peers.add(str(parsed))
        if len(peers) > 32:
            raise ValueError(error)
    return tuple(sorted(peers))

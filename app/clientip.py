"""Client-IP resolution, byte-parity with groundsquirrel's ``client_ip.py``.

Forwarding headers are honored only when the direct peer address falls inside a
configured trusted-proxy network; otherwise the peer address is returned
verbatim. Precedence mirrors groundsquirrel exactly: ``X-Forwarded-For`` (first
entry), then ``X-Real-IP``, then the RFC 7239 ``Forwarded`` ``for=`` value, then
the peer. Any divergence from that order would silently break otherwise-valid gs
session cookies, which bind to the resolved client IP.
"""

from __future__ import annotations

from collections.abc import Mapping
from ipaddress import ip_address
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ipaddress import IPv4Network, IPv6Network


def resolve_client_ip(
    headers: Mapping[str, str],
    peer_ip: str,
    trusted_proxies: tuple[IPv4Network | IPv6Network, ...],
) -> str:
    """Resolve the effective client IP for ``peer_ip`` given request headers.

    When ``peer_ip`` is not a trusted proxy (or is unparseable, or the trusted
    set is empty), forwarding headers are ignored and ``peer_ip`` is returned.
    """
    if not _peer_is_trusted(peer_ip, trusted_proxies):
        return peer_ip

    forwarded_for = _header(headers, "X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    real_ip = _header(headers, "X-Real-IP")
    if real_ip:
        return real_ip.strip()

    forwarded = _header(headers, "Forwarded")
    if forwarded:
        for_value = _forwarded_for(forwarded)
        if for_value:
            return for_value

    return peer_ip


def _peer_is_trusted(
    peer_ip: str, trusted_proxies: tuple[IPv4Network | IPv6Network, ...]
) -> bool:
    if not trusted_proxies:
        return False
    try:
        addr = ip_address(peer_ip)
    except ValueError:
        return False
    return any(addr in net for net in trusted_proxies)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup over any Mapping.

    aiohttp request headers are a case-insensitive ``CIMultiDict`` that matches
    on the first ``get``; a plain ``dict`` with differently-cased keys is scanned
    case-insensitively, so any Mapping behaves the same.
    """
    value = headers.get(name)
    if value is not None:
        return value
    target = name.lower()
    for key in headers:
        if key.lower() == target:
            return headers[key]
    return None


def _forwarded_for(header_value: str) -> str | None:
    """Return the first RFC 7239 ``for=`` value, stripped of quotes only.

    Matches gs: brackets and ports inside the value are preserved verbatim (e.g.
    ``for="[2001:db8::1]:8080"`` yields ``[2001:db8::1]:8080``).
    """
    first_element = header_value.split(",")[0]
    for directive in first_element.split(";"):
        key, sep, value = directive.strip().partition("=")
        if sep and key.strip().lower() == "for":
            return value.strip().strip('"')
    return None

"""Tests for app.clientip — gs-parity client-IP resolution behind trusted proxies."""

from ipaddress import ip_network

from app.clientip import resolve_client_ip

_TRUSTED = (ip_network("10.0.0.0/8"),)


def test_untrusted_peer_ignores_forwarding():
    headers = {"X-Forwarded-For": "203.0.113.7"}
    assert resolve_client_ip(headers, "203.0.113.1", _TRUSTED) == "203.0.113.1"


def test_trusted_peer_takes_first_xff_entry():
    headers = {"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}
    assert resolve_client_ip(headers, "10.0.0.5", _TRUSTED) == "203.0.113.7"


def test_xff_first_entry_stripped():
    headers = {"X-Forwarded-For": "  203.0.113.7  , 10.0.0.1"}
    assert resolve_client_ip(headers, "10.0.0.5", _TRUSTED) == "203.0.113.7"


def test_x_real_ip_fallback():
    headers = {"X-Real-IP": "203.0.113.8"}
    assert resolve_client_ip(headers, "10.0.0.5", _TRUSTED) == "203.0.113.8"


def test_xff_precedence_over_real_ip():
    headers = {"X-Forwarded-For": "203.0.113.7", "X-Real-IP": "203.0.113.8"}
    assert resolve_client_ip(headers, "10.0.0.5", _TRUSTED) == "203.0.113.7"


def test_forwarded_header_parse():
    headers = {"Forwarded": 'for="203.0.113.9"'}
    assert resolve_client_ip(headers, "10.0.0.5", _TRUSTED) == "203.0.113.9"


def test_forwarded_first_for_value():
    headers = {"Forwarded": "for=203.0.113.9;proto=https, for=198.51.100.1"}
    assert resolve_client_ip(headers, "10.0.0.5", _TRUSTED) == "203.0.113.9"


def test_forwarded_directive_after_others():
    headers = {"Forwarded": "proto=https;for=203.0.113.9"}
    assert resolve_client_ip(headers, "10.0.0.5", _TRUSTED) == "203.0.113.9"


def test_forwarded_preserves_brackets_and_port():
    # gs strips quotes only; brackets/port are preserved verbatim.
    headers = {"Forwarded": 'for="[2001:db8::1]:8080"'}
    assert resolve_client_ip(headers, "10.0.0.5", _TRUSTED) == "[2001:db8::1]:8080"


def test_forwarded_without_for_directive_returns_peer():
    headers = {"Forwarded": "proto=https;by=10.0.0.1"}
    assert resolve_client_ip(headers, "10.0.0.5", _TRUSTED) == "10.0.0.5"


def test_no_headers_returns_peer():
    assert resolve_client_ip({}, "10.0.0.5", _TRUSTED) == "10.0.0.5"


def test_empty_trusted_proxies_returns_peer():
    headers = {"X-Forwarded-For": "203.0.113.7"}
    assert resolve_client_ip(headers, "10.0.0.5", ()) == "10.0.0.5"


def test_unparseable_peer_returns_peer():
    headers = {"X-Forwarded-For": "203.0.113.7"}
    assert resolve_client_ip(headers, "not-an-ip", _TRUSTED) == "not-an-ip"


def test_case_insensitive_header_lookup():
    headers = {"x-forwarded-for": "203.0.113.7"}
    assert resolve_client_ip(headers, "10.0.0.5", _TRUSTED) == "203.0.113.7"


def test_ipv6_trusted_peer():
    trusted = (ip_network("2001:db8::/32"),)
    headers = {"X-Forwarded-For": "203.0.113.7"}
    assert resolve_client_ip(headers, "2001:db8::5", trusted) == "203.0.113.7"

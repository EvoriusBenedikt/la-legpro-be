"""T1: get_client_ip / get_user_agent / sanitize_username (no DB)."""
from types import SimpleNamespace

from services.request_meta import get_client_ip, get_user_agent, sanitize_username


def req(peer, headers=None):
    client = SimpleNamespace(host=peer) if peer is not None else None
    return SimpleNamespace(client=client, headers={k.lower(): v for k, v in (headers or {}).items()})


def test_spoofed_header_from_public_peer_is_ignored():
    # NB: documentation ranges (192.0.2/24, 198.51.100/24, 203.0.113/24, 2001:db8::/32)
    # count as is_private in Python, so real public addresses are used as peers.
    assert get_client_ip(req("8.8.8.8", {"X-Real-IP": "1.2.3.4"})) == "8.8.8.8"


def test_header_accepted_from_private_peer():
    assert get_client_ip(req("172.18.0.5", {"X-Real-IP": "198.51.100.7"})) == "198.51.100.7"
    assert get_client_ip(req("10.1.2.3", {"X-Real-IP": "198.51.100.7"})) == "198.51.100.7"
    assert get_client_ip(req("192.168.1.9", {"X-Real-IP": "198.51.100.7"})) == "198.51.100.7"


def test_header_accepted_from_loopback_peer():
    assert get_client_ip(req("127.0.0.1", {"X-Real-IP": "198.51.100.7"})) == "198.51.100.7"
    assert get_client_ip(req("::1", {"X-Real-IP": "198.51.100.7"})) == "198.51.100.7"


def test_invalid_header_falls_back_to_peer():
    assert get_client_ip(req("172.18.0.5", {"X-Real-IP": "not-an-ip"})) == "172.18.0.5"
    assert get_client_ip(req("172.18.0.5", {"X-Real-IP": "1.2.3.4, 5.6.7.8"})) == "172.18.0.5"
    assert get_client_ip(req("172.18.0.5", {"X-Real-IP": ""})) == "172.18.0.5"
    assert get_client_ip(req("172.18.0.5")) == "172.18.0.5"


def test_ipv6_and_mapped_addresses():
    assert get_client_ip(req("2606:4700::1111", {"X-Real-IP": "1.2.3.4"})) == "2606:4700::1111"
    # ::ffff:a.b.c.d is mapped to IPv4 before the private/public decision
    assert get_client_ip(req("::ffff:8.8.4.4", {"X-Real-IP": "1.2.3.4"})) == "8.8.4.4"
    assert get_client_ip(req("::ffff:172.18.0.5", {"X-Real-IP": "198.51.100.7"})) == "198.51.100.7"
    # header may carry an IPv6 client, with a zone id stripped
    assert get_client_ip(req("172.18.0.5", {"X-Real-IP": "2001:db8::7%eth0"})) == "2001:db8::7"


def test_no_client_or_garbage_peer_is_unknown():
    assert get_client_ip(req(None)) == "unknown"
    assert get_client_ip(req("")) == "unknown"
    assert get_client_ip(req("testclient")) == "unknown"
    assert get_client_ip(SimpleNamespace(client=None, headers=None)) == "unknown"


def test_user_agent_truncated_and_cleaned():
    ua = get_user_agent(req("10.0.0.1", {"User-Agent": "A" * 500}))
    assert ua == "A" * 200
    ua = get_user_agent(req("10.0.0.1", {"User-Agent": 'Mozilla\r\nInjected: "x"\x00'}))
    assert "\r" not in ua and "\n" not in ua and "\x00" not in ua
    assert get_user_agent(req("10.0.0.1")) == "unknown"


def test_sanitize_username():
    assert sanitize_username("bob\nACCOUNT_LOCKED ip=1.1.1.1") == "bobACCOUNT_LOCKED ip=1.1.1.1"
    assert "\n" not in sanitize_username("a\r\nb\x00c\x1bd")
    assert len(sanitize_username("x" * 500)) == 64
    assert sanitize_username(None) == ""

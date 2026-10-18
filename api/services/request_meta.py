"""Request metadata helpers: real client IP, user agent, username hygiene
(auth hardening, 2026-10-14).

Trust model: the frontend nginx container is the trust boundary. It
overwrites X-Real-IP / X-Forwarded-For with the real client address (after
``set_real_ip_from`` for private ranges only). So a header is only believed
when the TCP peer is itself private/loopback (i.e. our own nginx, or uvicorn
``--proxy-headers`` having already rewritten ``request.client``). A public
peer is a direct hit and its headers are ignored, which stops spoofing of the
per-IP lockout and the audit trail.
"""
import ipaddress
import re

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _parse_ip(value):
    """Parse an IP string (IPv6 zone stripped, ``::ffff:a.b.c.d`` mapped to
    IPv4). Returns an ipaddress object or None. Never raises."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if "%" in v:
        v = v.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(v)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def get_client_ip(request) -> str:
    """Best-effort real client IP; ``"unknown"`` when it cannot be determined."""
    try:
        peer_raw = request.client.host if request.client else ""
    except Exception:
        peer_raw = ""
    peer = _parse_ip(peer_raw)
    if peer is None:
        return "unknown"
    if not (peer.is_private or peer.is_loopback):
        return str(peer)
    try:
        header = request.headers.get("x-real-ip")
    except Exception:
        header = None
    forwarded = _parse_ip(header)
    return str(forwarded) if forwarded is not None else str(peer)


def get_user_agent(request) -> str:
    """User-Agent with control characters removed, truncated to 200 chars."""
    try:
        ua = request.headers.get("user-agent", "") or ""
    except Exception:
        return "unknown"
    ua = _CONTROL_CHARS.sub("", ua)[:200]
    return ua if ua else "unknown"


def sanitize_username(value) -> str:
    """Attempted-username text safe for audit rows: control chars (incl.
    newlines) removed, truncated to 64 chars."""
    if not isinstance(value, str):
        return ""
    return _CONTROL_CHARS.sub("", value)[:64]

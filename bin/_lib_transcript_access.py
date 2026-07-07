"""Pure loopback / Host gate predicates for the conversation-viewer transcript
endpoints (Plan 2, spec §5).

No I/O, no globals — directly unit-testable. The dashboard's
``_require_transcripts_allowed`` composes ``transcripts_allowed`` (is this bind
served at all?) with ``host_allowed_for_transcripts`` (anti-DNS-rebinding on the
request's Host header). Loopback-by-default kills rebinding (it needs a
hostname that resolves to 127.0.0.1); under the explicit ``expose`` opt-in the
LAN device reaches the dashboard at its IP literal (allowed), while an
attacker's rebinding *domain* (a hostname) is rejected.
"""
from __future__ import annotations
import ipaddress

# Public surface (Plan 2): shipped in the npm tarball + brew formula + public
# mirror — imported by the dashboard's transcript gate at runtime.

_LOOPBACK_NAMES = {"localhost", "::1"}


def authority_host(host_header) -> str:
    """Extract the lowercased host from an HTTP authority (``Host`` header or a
    bind string). Strips the port, the ``[...]`` IPv6 brackets, and any
    ``%zone`` id. IPv6-safe (does NOT ``split(':')``). Empty string on missing
    input."""
    if not host_header:
        return ""
    h = str(host_header).strip()
    if h.startswith("["):
        # [IPv6]:port  ->  IPv6
        end = h.find("]")
        if end != -1:
            h = h[1:end]
        else:
            h = h[1:]
    elif h.count(":") == 1:
        # host:port (a single colon can't be a bare IPv6 literal)
        h = h.split(":", 1)[0]
    # else: bare IPv6 literal (multiple colons) or bare host — leave as-is
    h = h.split("%", 1)[0]   # drop IPv6 zone id
    return h.lower()


def is_loopback(host: str) -> bool:
    """True for the loopback names plus any IPv4/IPv6 loopback literal
    (127.0.0.0/8, ::1)."""
    if not host:
        return False
    h = host.lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def debug_backend_allowed(peer_ip, host) -> bool:
    """Gate for the loopback-only ``/api/debug/backend`` diagnostic endpoint
    (issue #276, Session A).

    STRICTER than the transcript gate — it never opens to the LAN. Two checks:

      * PRIMARY: the TCP peer (``client_address[0]``) must be a loopback
        literal. This is the unspoofable signal — the dashboard can bind
        ``0.0.0.0`` (``dashboard.bind = lan``), and a ``Host``-header-only
        check would let a LAN client connect to the LAN socket while sending
        ``Host: 127.0.0.1``. The peer address cannot be spoofed that way.
      * DEFENSE-IN-DEPTH: the ``Host`` authority must ALSO be an IP-literal
        loopback (anti-DNS-rebinding) — a hostname ``Host`` (a rebinding
        vector) is rejected even from a loopback peer.

    ``dashboard.expose_transcripts`` is NEVER consulted — this surface is
    loopback-only, ALWAYS. Fail closed on a missing/empty peer or Host.
    """
    if not is_loopback(peer_ip):
        return False
    return is_loopback(authority_host(host))


def transcripts_allowed(bind_host, expose: bool) -> bool:
    """Are transcripts served AT ALL on this bind? Loopback bind always; a
    non-loopback (LAN) bind only under the explicit ``expose`` opt-in."""
    return is_loopback(authority_host(bind_host)) or bool(expose)


def host_allowed_for_transcripts(host_header, expose: bool) -> bool:
    """Anti-DNS-rebinding Host allowlist. Loopback Host always OK. Otherwise
    only an IP-literal Host under ``expose`` (the LAN IP the dashboard is
    reached at) — a rebinding *domain* (any hostname) is rejected. Fail closed
    on missing/empty Host."""
    h = authority_host(host_header)
    if not h:
        return False
    if is_loopback(h):
        return True
    if not expose:
        return False
    try:
        ipaddress.ip_address(h)   # IP literal can't be DNS-rebound
        return True
    except ValueError:
        return False              # hostname (rebinding vector) — reject

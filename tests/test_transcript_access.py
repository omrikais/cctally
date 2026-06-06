import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _lib_transcript_access as ta


def test_authority_host_strips_port_and_brackets():
    assert ta.authority_host("127.0.0.1:8789") == "127.0.0.1"
    assert ta.authority_host("localhost:8789") == "localhost"
    assert ta.authority_host("[::1]:8789") == "::1"
    assert ta.authority_host("[fe80::1%eth0]:8789") == "fe80::1"   # zone-id stripped
    assert ta.authority_host("192.168.0.9") == "192.168.0.9"
    assert ta.authority_host("Example.COM:80") == "example.com"    # lowercased
    assert ta.authority_host("") == ""
    assert ta.authority_host(None) == ""


def test_is_loopback():
    for h in ("127.0.0.1", "localhost", "::1", "127.5.5.5"):
        assert ta.is_loopback(h) is True
    for h in ("192.168.0.9", "10.0.0.1", "example.com", "0.0.0.0", ""):
        assert ta.is_loopback(h) is False


def test_transcripts_allowed_loopback_default_then_expose():
    # Loopback bind: always allowed regardless of expose.
    assert ta.transcripts_allowed("127.0.0.1", False) is True
    assert ta.transcripts_allowed("localhost", False) is True
    # LAN bind: only when expose opt-in is on.
    assert ta.transcripts_allowed("0.0.0.0", False) is False
    assert ta.transcripts_allowed("192.168.0.9", False) is False
    assert ta.transcripts_allowed("0.0.0.0", True) is True


def test_host_allowed_blocks_dns_rebinding():
    # Loopback Host: always OK (kills the rebinding precondition).
    assert ta.host_allowed_for_transcripts("localhost:8789", False) is True
    assert ta.host_allowed_for_transcripts("127.0.0.1:8789", False) is True
    assert ta.host_allowed_for_transcripts("[::1]:8789", False) is True
    # Not exposed → any non-loopback Host rejected.
    assert ta.host_allowed_for_transcripts("192.168.0.9:8789", False) is False
    assert ta.host_allowed_for_transcripts("evil.attacker.com", False) is False
    # Exposed → IP-literal Host OK (can't be DNS-rebound), hostname rejected.
    assert ta.host_allowed_for_transcripts("192.168.0.9:8789", True) is True
    assert ta.host_allowed_for_transcripts("[fe80::1]:8789", True) is True
    assert ta.host_allowed_for_transcripts("evil.attacker.com", True) is False  # rebinding domain
    assert ta.host_allowed_for_transcripts("machine.local:8789", True) is False  # mDNS hostname
    # Missing/empty Host → fail closed.
    assert ta.host_allowed_for_transcripts("", True) is False
    assert ta.host_allowed_for_transcripts(None, True) is False

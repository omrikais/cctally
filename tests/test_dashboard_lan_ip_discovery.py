"""Unit tests for _discover_lan_ip + _format_url."""
import socket
from conftest import load_script


def test_format_url_ipv4():
    ns = load_script()
    assert ns["_format_url"]("127.0.0.1", 8789) == "http://127.0.0.1:8789/"


def test_format_url_ipv4_zero_zero():
    ns = load_script()
    assert ns["_format_url"]("0.0.0.0", 8789) == "http://0.0.0.0:8789/"


def test_format_url_ipv6_brackets():
    ns = load_script()
    assert ns["_format_url"]("::1", 8789) == "http://[::1]:8789/"


def test_format_url_ipv6_already_bracketed_passthrough():
    ns = load_script()
    assert ns["_format_url"]("[::1]", 8789) == "http://[::1]:8789/"


def test_format_url_hostname():
    ns = load_script()
    assert ns["_format_url"]("localhost", 8789) == "http://localhost:8789/"


def test_discover_lan_ip_returns_string_when_socket_works(monkeypatch):
    ns = load_script()
    ip = ns["_discover_lan_ip"]()
    # Don't assert specific IP — just that we got a string or None.
    assert ip is None or isinstance(ip, str)


def test_discover_lan_ip_returns_none_on_oserror(monkeypatch):
    ns = load_script()

    class _Sock:
        def __init__(self, *a, **kw): pass
        def connect(self, *a, **kw): raise OSError("no route")
        def getsockname(self): return ("0.0.0.0", 0)
        def close(self): pass

    monkeypatch.setattr(socket, "socket", _Sock)
    ip = ns["_discover_lan_ip"]()
    assert ip is None


def test_discover_lan_ip_honors_test_env(monkeypatch):
    ns = load_script()
    monkeypatch.setenv("CCTALLY_TEST_LAN_IP", "10.0.0.42")
    ip = ns["_discover_lan_ip"]()
    assert ip == "10.0.0.42"


def test_discover_lan_ip_test_env_suppress(monkeypatch):
    ns = load_script()
    monkeypatch.setenv("CCTALLY_TEST_LAN_IP", "__SUPPRESS__")
    ip = ns["_discover_lan_ip"]()
    assert ip is None

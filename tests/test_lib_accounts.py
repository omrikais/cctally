"""Pure-kernel contracts for bin/_lib_accounts.py (multi-account epic #341, Task 1).

Covers account_key derivation, natural-id extraction, JWT id_token decode,
the stat-read-stat stable-read protocol (identified / stably_absent / torn),
and account-ref resolution (label -> email -> key-prefix, ambiguity, the
literal ``unattributed`` sentinel).
"""
from __future__ import annotations

import base64
import json
import pathlib
import sqlite3
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_accounts as acc  # noqa: E402


# --------------------------------------------------------------------------
# account_key derivation
# --------------------------------------------------------------------------

def test_account_key_is_stable_and_32_hex():
    k1 = acc.account_key("claude", "122616fd-0000-4000-8000-000000000000")
    k2 = acc.account_key("claude", "122616fd-0000-4000-8000-000000000000")
    assert k1 == k2
    assert len(k1) == 32
    assert all(c in "0123456789abcdef" for c in k1)


def test_account_key_differs_by_provider():
    nat = "122616fd-0000-4000-8000-000000000000"
    assert acc.account_key("claude", nat) != acc.account_key("codex", nat)


def test_account_key_differs_by_natural_id():
    assert acc.account_key("claude", "a") != acc.account_key("claude", "b")


def test_account_key_rejects_empty():
    with pytest.raises(ValueError):
        acc.account_key("claude", "")
    with pytest.raises(ValueError):
        acc.account_key("", "x")


def test_sentinels():
    assert acc.UNATTRIBUTED == "unattributed"
    assert acc.VENDOR_WIDE == "*"


# --------------------------------------------------------------------------
# natural-id extraction
# --------------------------------------------------------------------------

def test_claude_natural_id_from_oauth_account():
    assert acc.claude_natural_id({"accountUuid": "u-1", "emailAddress": "a@x"}) == "u-1"


def test_claude_natural_id_missing_returns_none():
    assert acc.claude_natural_id({}) is None
    assert acc.claude_natural_id({"accountUuid": ""}) is None
    assert acc.claude_natural_id(None) is None


def test_codex_natural_id_is_the_pair():
    payload = {
        "email": "acct@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-abc",
            "chatgpt_plan_type": "pro",
        },
    }
    assert acc.codex_natural_id(payload) == "acct-abc\x00acct@example.com"


def test_codex_natural_id_api_key_mode_returns_none():
    # OPENAI_API_KEY mode: no chatgpt identity in the token payload at all.
    assert acc.codex_natural_id({"email": "a@x"}) is None
    assert acc.codex_natural_id({"https://api.openai.com/auth": {}}) is None
    assert acc.codex_natural_id(None) is None


# --------------------------------------------------------------------------
# id_token decode (base64 body, no signature verification)
# --------------------------------------------------------------------------

def _make_jwt(payload: dict) -> str:
    def b64(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header = b64({"alg": "RS256", "typ": "JWT"})
    body = b64(payload)
    sig = base64.urlsafe_b64encode(b"not-a-real-signature").decode("ascii").rstrip("=")
    return f"{header}.{body}.{sig}"


def test_decode_id_token_payload_roundtrips():
    payload = {
        "email": "a@x.com",
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"},
    }
    token = _make_jwt(payload)
    assert acc.decode_id_token_payload(token) == payload


def test_decode_id_token_payload_bad_inputs_return_none():
    assert acc.decode_id_token_payload("") is None
    assert acc.decode_id_token_payload("only-one-part") is None
    assert acc.decode_id_token_payload("aa.!!!not-base64!!!.bb") is None
    assert acc.decode_id_token_payload(None) is None
    # A body that base64-decodes to non-JSON.
    bad_body = base64.urlsafe_b64encode(b"not json").decode("ascii").rstrip("=")
    assert acc.decode_id_token_payload(f"h.{bad_body}.s") is None


# --------------------------------------------------------------------------
# stable_read_identity: stat-read-stat, three-valued
# --------------------------------------------------------------------------

def test_stable_read_identified(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"id": "x"}))

    def reader(data: bytes):
        return json.loads(data)["id"]

    out = acc.stable_read_identity(str(p), reader)
    assert out.status == "identified"
    assert out.value == "x"


def test_stable_read_stably_absent_when_missing(tmp_path):
    out = acc.stable_read_identity(str(tmp_path / "nope.json"), lambda d: "x")
    assert out.status == "stably_absent"
    assert out.value is None


def test_stable_read_stably_absent_when_reader_returns_none(tmp_path):
    # File present + stats stable, but content carries no identity (api-key mode).
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"OPENAI_API_KEY": "sk-..."}))
    out = acc.stable_read_identity(str(p), lambda d: None)
    assert out.status == "stably_absent"


def test_stable_read_torn_when_reader_signals_unparseable(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text("{ half-written")

    def reader(data: bytes):
        try:
            return json.loads(data)["id"]
        except (ValueError, KeyError):
            raise acc.TornRead()

    out = acc.stable_read_identity(str(p), reader)
    assert out.status == "torn"


def test_stable_read_torn_when_stats_flip_between_reads(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"id": "x"}))

    # A stat sequence whose signature changes on every call -> the sig1/sig2
    # comparison never matches, so every attempt is "torn" and the retries
    # exhaust to a torn verdict.
    counter = {"n": 0}
    real_stat = acc.os.stat

    class _FakeStat:
        def __init__(self, n):
            self.st_ino = 1
            self.st_size = n
            self.st_mtime_ns = n

    def fake_stat(path, *a, **k):
        counter["n"] += 1
        return _FakeStat(counter["n"])

    out = acc.stable_read_identity(str(p), lambda d: "x", _stat=fake_stat)
    assert out.status == "torn"
    assert counter["n"] >= 2  # stat-read-stat happened at least once


# --------------------------------------------------------------------------
# resolve_account_ref
# --------------------------------------------------------------------------

def _accounts_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE accounts (
               account_key TEXT PRIMARY KEY,
               provider TEXT NOT NULL,
               natural_id TEXT,
               email TEXT,
               label TEXT,
               plan_type TEXT,
               label_source TEXT,
               first_seen_utc TEXT,
               last_seen_utc TEXT
           )"""
    )
    for r in rows:
        conn.execute(
            "INSERT INTO accounts (account_key, provider, email, label) VALUES (?,?,?,?)",
            (r["account_key"], r["provider"], r.get("email"), r.get("label")),
        )
    conn.commit()
    return conn


def test_resolve_ref_literal_unattributed():
    conn = _accounts_conn([])
    assert acc.resolve_account_ref(conn, "unattributed", None) == "unattributed"


def test_resolve_ref_label_case_insensitive():
    conn = _accounts_conn([
        {"account_key": "a" * 32, "provider": "claude", "label": "Work", "email": "w@x"},
    ])
    assert acc.resolve_account_ref(conn, "work", None) == "a" * 32
    assert acc.resolve_account_ref(conn, "WORK", None) == "a" * 32


def test_resolve_ref_by_email():
    conn = _accounts_conn([
        {"account_key": "a" * 32, "provider": "codex", "label": None, "email": "me@x.com"},
    ])
    assert acc.resolve_account_ref(conn, "me@x.com", None) == "a" * 32


def test_resolve_ref_by_key_prefix():
    key = "abcd" + "0" * 28
    conn = _accounts_conn([{"account_key": key, "provider": "claude"}])
    assert acc.resolve_account_ref(conn, "abcd", None) == key


def test_resolve_ref_ambiguous_label_raises_with_candidates():
    conn = _accounts_conn([
        {"account_key": "a" * 32, "provider": "claude", "label": "dup"},
        {"account_key": "b" * 32, "provider": "codex", "label": "dup"},
    ])
    with pytest.raises(acc.AccountRefError) as ei:
        acc.resolve_account_ref(conn, "dup", None)
    assert set(ei.value.candidates) == {"a" * 32, "b" * 32}


def test_resolve_ref_provider_scoped():
    conn = _accounts_conn([
        {"account_key": "a" * 32, "provider": "claude", "label": "dup"},
        {"account_key": "b" * 32, "provider": "codex", "label": "dup"},
    ])
    # Scoping to one provider disambiguates.
    assert acc.resolve_account_ref(conn, "dup", "claude") == "a" * 32


def test_resolve_ref_unknown_raises():
    conn = _accounts_conn([{"account_key": "a" * 32, "provider": "claude", "label": "x"}])
    with pytest.raises(acc.AccountRefError):
        acc.resolve_account_ref(conn, "nonesuch", None)

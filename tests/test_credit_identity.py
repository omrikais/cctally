"""Credit identity comes from the source journal record, never the boundaries.

An Anthropic weekly credit is identified by ``credit_key`` and ordered by
``credit_order`` (#703 + #707, spec section 4). Both are derived from the
journal record that CAUSED the credit — a content-stable journal identity — so
the key is reproducible from the journal rather than from projection state, and
two distinct source records admit two credits in the same week or the same hour
while a repeat of the identical source record stays an idempotent retry.

``credit_order`` is the canonical journal sequence position of that same source
record, NOT the fold position of the derived row. Fold orders express
dependency rather than occurrence, and after unification manual credits fold at
op order 5 while automatic ones fold at event order 30 — so a fold-derived
order can reverse the real chronology of two credits (spec section 5.3).
"""
from __future__ import annotations

import pytest

from _lib_credit_identity import (
    CreditSource,
    derive_credit_key,
    derive_credit_order,
)


@pytest.mark.parametrize("kind,ident,expected", [
    ("immediate", "o:abc123", "o:abc123"),
    ("debounced", "o:def456", "o:def456"),
    ("backfill", "b:weekly_usage_snapshots:42", "b:weekly_usage_snapshots:42"),
    ("manual", "o:0f0f0f0f", "o:0f0f0f0f"),
    ("legacy", "wr:9", "legacy:wr:9"),
])
def test_credit_key_comes_from_the_source_record(kind, ident, expected):
    assert derive_credit_key(
        CreditSource(kind=kind, identity=ident, order=1)) == expected


def test_credit_key_never_derives_from_boundaries():
    a = CreditSource(kind="immediate", identity="o:aaa", order=1)
    b = CreditSource(kind="immediate", identity="o:bbb", order=2)
    assert derive_credit_key(a) != derive_credit_key(b)


def test_the_same_source_record_derives_the_same_key():
    """An idempotent retry, not a second credit."""
    a = CreditSource(kind="manual", identity="o:0f0f", order=7)
    b = CreditSource(kind="manual", identity="o:0f0f", order=7)
    assert derive_credit_key(a) == derive_credit_key(b)


def test_credit_order_is_the_source_records_journal_position():
    assert derive_credit_order(
        CreditSource(kind="immediate", identity="o:aaa", order=41)) == 41
    assert derive_credit_order(
        CreditSource(kind="legacy", identity="wr:9", order=0)) == 0


@pytest.mark.parametrize("fn", [derive_credit_key, derive_credit_order])
def test_an_unknown_kind_raises_rather_than_defaulting(fn):
    """A silent default here reintroduces colliding identities."""
    with pytest.raises(ValueError):
        fn(CreditSource(kind="whatever", identity="o:aaa", order=1))


def test_an_empty_identity_raises():
    """An empty key is not an identity; it collides with every other empty."""
    with pytest.raises(ValueError):
        derive_credit_key(CreditSource(kind="manual", identity="", order=1))


def test_credit_source_is_frozen():
    src = CreditSource(kind="manual", identity="o:aaa", order=1)
    with pytest.raises(Exception):
        src.identity = "o:bbb"


def test_the_module_is_pure(tmp_path):
    """No database access and no `_cctally_*` import: the derivation must be
    reproducible from the journal alone."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "bin" / "_lib_credit_identity.py").read_text(encoding="utf-8")
    assert "sqlite3" not in src
    assert "import _cctally" not in src
    assert "import cctally" not in src

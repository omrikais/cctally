"""Evidence store identity and loss (contract C8; spec sections 6.3 step 4,
6.4 F20, 6.10 R7-F7; acceptance row E).

A retention record carries a store identity and seeded loss intervals that
later passes preserve until they age out. The seeding tool and the internal
evidence index left with the runner storage tooling (test regime reset,
sessions 4 and 5); the retention pass's carry of an existing record stays.
"""

from __future__ import annotations

import pytest


def _ev():
    import _lib_test_evidence as EV
    return EV


STORE_B = "STORE-B+IMAGE-B:2"
DAY = 86400


# --- the retention record ------------------------------------------------------------

def test_a_record_without_a_store_is_legacy():
    EV = _ev()
    assert EV.retention_store({"passes": 3}) == "legacy"
    assert EV.retention_store(None) == "legacy"
    assert EV.retention_store({"store": STORE_B}) == STORE_B


def test_seeded_loss_is_carried_until_it_ages_out_and_keeps_coverage_degraded():
    EV = _ev()
    now = 1791676800  # 2026-10-10T00:00:00Z
    old = {"start": "2026-09-01T00:00:00Z", "end": "2026-09-02T00:00:00Z", "reason": "a"}
    recent = {"start": "2026-10-05T00:00:00Z", "end": "2026-10-06T00:00:00Z", "reason": "b"}
    open_ended = {"start": "2026-08-01T00:00:00Z", "end": None, "reason": "c"}
    previous = {"store": STORE_B, "seededLoss": [old, recent, open_ended]}
    kept = EV.carried_seeded_loss(previous, now, max_age_days=12)
    assert kept == [recent, open_ended]
    assert EV.retention_coverage(gaps=[], orphans=0, seeded_loss=kept) == "degraded"
    assert EV.retention_coverage(gaps=[], orphans=0, seeded_loss=[]) == "complete"
    assert EV.retention_coverage(gaps=[{"from_epoch": 1}], orphans=0,
                                 seeded_loss=[]) == "degraded"
    assert EV.carried_seeded_loss({"seededLoss": [recent]}, now + 30 * DAY,
                                  max_age_days=12) == []
    assert EV.carried_seeded_loss(None, now) == []


def test_loss_instants_accept_fractional_seconds_and_utc_offsets():
    """`_iso_epoch` reads whole and fractional seconds with `Z`, `+00:00`,
    another offset, or none (UTC), so a carried interval never fails on the
    spelling a writer chose."""
    EV = _ev()
    base = 1791590400  # 2026-10-10T00:00:00Z
    for text, want in (("2026-10-10T00:00:00Z", base), ("2026-10-10T00:00:00+00:00", base),
                       ("2026-10-10T00:00:00", base), ("2026-10-10T00:00:00.250Z", base + 0.25),
                       ("2026-10-10T00:00:00.123456+00:00", base + 0.123456),
                       ("2026-10-10T02:00:00+02:00", base), ("2026-10-09T19:30:00-0430", base)):
        assert abs(EV._iso_epoch(text) - want) < 1e-6, text
    with pytest.raises(ValueError):
        EV._iso_epoch("yesterday")
    now = base
    recent = {"start": "2026-10-05T00:00:00.5Z", "end": "2026-10-06T00:00:00.123+00:00",
              "reason": "b"}
    assert EV.carried_seeded_loss({"seededLoss": [recent]}, now, max_age_days=12) == [recent]

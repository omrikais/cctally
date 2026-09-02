"""Account dimension on alerts (#341 Task 3, spec §6): the UNCONDITIONAL 8th
`alerts.log` tab field (`account_key`; `*` for vendor-wide rows) proven with
RAW-BYTE reads, and the R8 `[<label>]` notification-title prefix that appears
ONLY when the vendor has more than one real account.

The log is runtime state (exempt from R8), so the 8th field is unconditional;
the title prefix IS gated by R8. Isolation via load_isolated_cctally_module so
LOG_DIR + DB_PATH + CLAUDE_JSON_PATH point at the per-test tmp dir.
"""
from __future__ import annotations

import pytest

import _cctally_core
from conftest import load_isolated_cctally_module


@pytest.fixture
def cc(tmp_path, monkeypatch):
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _acc(provider, natural):
    import _lib_accounts
    return _lib_accounts.account_key(provider, natural)


def _seed_claude(observes):
    import _cctally_journal as jr
    import _lib_journal as lj
    for kw in observes:
        jr.append_record(lj.make_account_observe(**kw))
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))


def _dispatch(cc, payload, sink):
    import _cctally_alerts
    return _cctally_alerts._dispatch_alert_notification(
        payload,
        popen_factory=(lambda args, **k: sink.append(list(args))),
        mode="real", platform="linux",
        which_on_path=lambda n: n == "notify-send",
    )


def _log_bytes():
    return (_cctally_core.LOG_DIR / "alerts.log").read_bytes()


# --------------------------------------------------------------------------
# raw-byte 8th field
# --------------------------------------------------------------------------

def test_eighth_field_is_account_key_raw_bytes(cc):
    cc.open_db().close()  # materialize the stats schema (accounts table)
    payload = cc._build_alert_payload_weekly(
        threshold=60, crossed_at_utc="2026-07-01T00:00:00Z",
        week_start_date="2026-07-01", cumulative_cost_usd=1.0,
        dollars_per_percent=0.01, account_key="deadbeefdeadbeef",
    )
    sink = []
    assert _dispatch(cc, payload, sink) == "queued"
    raw = _log_bytes()
    assert raw.endswith(b"\n")
    fields = raw.decode("utf-8").rstrip("\n").split("\t")
    assert len(fields) == 8                       # 7 -> 8 field evolution
    assert fields[1] == "weekly"                  # axis
    assert fields[6] == "info"                    # severity (7th)
    assert fields[7] == "deadbeefdeadbeef"        # 8th = account_key


def test_eighth_field_star_for_vendor_wide_budget(cc):
    cc.open_db().close()
    payload = cc._build_alert_payload_budget(
        threshold=90, crossed_at_utc="2026-07-01T00:00:00Z",
        week_start_at="2026-07-01T00:00:00Z", budget_usd=100.0,
        spent_usd=90.0, consumption_pct=90.0,
    )
    sink = []
    _dispatch(cc, payload, sink)
    fields = _log_bytes().decode("utf-8").rstrip("\n").split("\t")
    assert len(fields) == 8
    assert fields[7] == "*"                        # vendor-wide sentinel


def test_eighth_field_present_for_quota(cc):
    cc.open_db().close()
    payload = cc._build_alert_payload_quota(
        source="codex", source_root_key="root-a", logical_limit_key="primary",
        observed_slot="primary", window_minutes=300,
        resets_at_utc="2026-07-15T15:00:00+00:00", threshold=95, kind="actual",
        crossed_at_utc="2026-07-01T00:00:00Z", qualifying_percent=95.0,
        projected_percent=None, account_key="codexkey1234",
    )
    sink = []
    _dispatch(cc, payload, sink)
    fields = _log_bytes().decode("utf-8").rstrip("\n").split("\t")
    assert len(fields) == 8
    assert fields[7] == "codexkey1234"


# --------------------------------------------------------------------------
# R8 [label] title prefix
# --------------------------------------------------------------------------

def test_label_prefix_when_multi_real_account(cc):
    ka = _acc("claude", "uuid-a")
    kb = _acc("claude", "uuid-b")
    _seed_claude([
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
             email="a@x.com", label="alice", label_source="auto"),
        dict(at="2026-07-02T00:00:00Z", account_key=kb, provider="claude",
             email="b@x.com", label="bob", label_source="auto"),
    ])
    payload = cc._build_alert_payload_weekly(
        threshold=60, crossed_at_utc="2026-07-01T00:00:00Z",
        week_start_date="2026-07-01", cumulative_cost_usd=1.0,
        dollars_per_percent=0.01, account_key=ka,
    )
    sink = []
    _dispatch(cc, payload, sink)
    joined = " ".join(sink[0])
    assert "[alice]" in joined            # title carries the label prefix


def test_no_prefix_when_single_real_account_R8(cc):
    ka = _acc("claude", "uuid-solo")
    _seed_claude([
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
             email="solo@x.com", label="solo", label_source="auto"),
        # a legacy unattributed bucket must NOT trigger decoration (R8)
        dict(at="2026-07-01T00:00:00Z", account_key="unattributed",
             provider="claude", label_source="auto"),
    ])
    payload = cc._build_alert_payload_weekly(
        threshold=60, crossed_at_utc="2026-07-01T00:00:00Z",
        week_start_date="2026-07-01", cumulative_cost_usd=1.0,
        dollars_per_percent=0.01, account_key=ka,
    )
    sink = []
    _dispatch(cc, payload, sink)
    joined = " ".join(sink[0])
    assert "[solo]" not in joined
    # byte-identical title to the pre-#341 render (no bracket prefix at all)
    assert "[" not in joined.split("cctally")[0]


# --------------------------------------------------------------------------
# #697 — the metering-rate-change family takes the same R8 [label] prefix
# --------------------------------------------------------------------------
# The family's payload is built through its own kernel rather than as a dict
# literal, so a change to `alert_payload`'s shape reaches these tests.
#
# These dispatch with an EXPLICIT UTC zone, unlike `_dispatch` above. The
# family's body renders `effective_from` through `format_display_dt`, which
# treats a `None` zone as HOST-LOCAL (`bin/_lib_display_tz.py:358`), so a
# frozen argument list built without the pin reads `2026-08-24` on any host
# west of UTC. The pin makes the freeze deterministic; it is not what is
# under test, and `tests/test_meter_rate_change_default.py` already covers
# the zone-sensitivity of this field.

BOUNDARY_697 = "2026-08-25T00:00:00+00:00"


def _rate_change_payload(account_key, *, provider="claude"):
    import _lib_meter_rate_change as mrc
    return mrc.alert_payload(mrc.RateChangeTransition(
        provider=provider,
        account_key=account_key,
        effective_from=BOUNDARY_697,
        previous_units_per_point=2_442_620.0,
        new_units_per_point=1_665_096.0,
        severity="alarm",
        detected_at="2026-08-29T00:00:00+00:00",
    ))


def _dispatch_utc(cc, payload, sink):
    from zoneinfo import ZoneInfo
    import _cctally_alerts
    return _cctally_alerts._dispatch_alert_notification(
        payload,
        popen_factory=(lambda args, **k: sink.append(list(args))),
        mode="real", platform="linux",
        which_on_path=lambda n: n == "notify-send",
        tz=ZoneInfo("UTC"),
    )


#: The frozen one-and-zero-account argument list. HAND-WRITTEN, never derived
#: from the code under test.
#:
#: `32` is the correct rounding: (1_665_096 - 2_442_620) / 2_442_620 * 100 is
#: -31.8316, and "%.0f" of its absolute value is 32.
#:
#: `normal` is `_SEVERITY_URGENCY`'s DEFAULT for the `alarm` tier rather than
#: an entry, because that table knows only the threshold axes' `critical`
#: name for the third tier (`bin/_lib_alert_dispatch.py:26`). That is #701.
#: When #701 lands this literal changes, and this comment is why.
_UNDECORATED_ARGV_697 = [
    "notify-send", "-u", "normal", "--",
    "cctally - Claude metering rate changed",
    "each meter point now covers 32% less usage\n"
    "Effective 2026-08-25. Run `cctally quota` for the fitted budget "
    "and its evidence.",
]


def test_697_rate_change_title_carries_the_label_when_decorated(cc):
    ka = _acc("claude", "uuid-697-a")
    kb = _acc("claude", "uuid-697-b")
    _seed_claude([
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
             email="a@x.com", label="alice", label_source="auto"),
        dict(at="2026-07-02T00:00:00Z", account_key=kb, provider="claude",
             email="b@x.com", label="bob", label_source="auto"),
    ])
    sink = []
    assert _dispatch_utc(cc, _rate_change_payload(ka), sink) == "queued"
    # The TITLE slot, not the joined argv: a match anywhere else would pass
    # for a reason this test does not claim.
    assert sink[0][4] == "[alice] cctally - Claude metering rate changed"


def test_697_the_prefix_vendor_comes_from_the_payload_provider(cc):
    # Codex is decorated and Claude is NOT. That asymmetry is what makes this
    # case non-vacuous: `_alert_label_prefix` uses the resolved vendor only
    # for the count gate, and `display_account_label` then resolves the label
    # from the payload key's OWN provider. With Claude also decorated, a
    # static `"meter_rate_change": "claude"` map entry would pass the gate and
    # still render the correct Codex label, so this test would hold against
    # the very implementation it exists to reject.
    ca = _acc("codex", "uuid-697-cx-a")
    cb = _acc("codex", "uuid-697-cx-b")
    _seed_claude([
        dict(at="2026-07-01T00:00:00Z", account_key=ca, provider="codex",
             email="cx-a@x.com", label="carol", label_source="auto"),
        dict(at="2026-07-02T00:00:00Z", account_key=cb, provider="codex",
             email="cx-b@x.com", label="dave", label_source="auto"),
    ])
    sink = []
    _dispatch_utc(cc, _rate_change_payload(ca, provider="codex"), sink)
    assert sink[0][4] == "[carol] cctally - Codex metering rate changed"


def test_697_the_unattributed_sentinel_renders_as_a_word(cc):
    ka = _acc("claude", "uuid-697-s-a")
    kb = _acc("claude", "uuid-697-s-b")
    _seed_claude([
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
             email="a@x.com", label="alice", label_source="auto"),
        dict(at="2026-07-02T00:00:00Z", account_key=kb, provider="claude",
             email="b@x.com", label="bob", label_source="auto"),
    ])
    sink = []
    _dispatch_utc(cc, _rate_change_payload("unattributed"), sink)
    assert sink[0][4] == "[Unattributed] cctally - Claude metering rate changed"


def test_697_the_payload_provider_is_normalized_before_the_lookup(cc):
    # `real_account_count` matches `accounts.provider` EXACTLY, so an
    # unnormalized " Claude " matches no row, counts zero, and silently
    # produces no prefix -- indistinguishable from correct undecorated
    # behaviour, which is the defect class this issue closes.
    ka = _acc("claude", "uuid-697-n-a")
    kb = _acc("claude", "uuid-697-n-b")
    _seed_claude([
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
             email="a@x.com", label="alice", label_source="auto"),
        dict(at="2026-07-02T00:00:00Z", account_key=kb, provider="claude",
             email="b@x.com", label="bob", label_source="auto"),
    ])
    sink = []
    _dispatch_utc(cc, _rate_change_payload(ka, provider=" Claude "), sink)
    # A PREFIX check, not an equality: the copy builder renders the provider
    # with `str(...).capitalize()`, and `" Claude ".capitalize()` is
    # `" claude "` -- capitalize uppercases the first character, which here is
    # a space, and lowercases the rest. The title after the prefix is
    # therefore not the ordinary one, and asserting it whole would pin an
    # artefact of the fixture rather than the behaviour under test.
    assert sink[0][4].startswith("[alice] ")


def test_697_one_real_account_is_byte_identical(cc):
    ka = _acc("claude", "uuid-697-solo")
    _seed_claude([
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
             email="solo@x.com", label="solo", label_source="auto"),
    ])
    sink = []
    _dispatch_utc(cc, _rate_change_payload(ka), sink)
    assert sink[0] == _UNDECORATED_ARGV_697


def test_697_zero_real_accounts_is_byte_identical(cc):
    # The registry is materialized but EMPTY. Without `open_db` the helper
    # returns early on the missing database and this would exercise that
    # branch instead of the count gate. An implementation gating on
    # `count == 1` rather than `<= 1` adds a prefix here and nowhere else.
    cc.open_db().close()
    sink = []
    _dispatch_utc(cc, _rate_change_payload("unattributed"), sink)
    assert sink[0] == _UNDECORATED_ARGV_697


def test_697_a_provider_whose_str_raises_yields_no_prefix(cc):
    # The never-raise contract, asserted on the HELPER rather than through
    # dispatch. `_alert_text_meter_rate_change` calls `str()` on the same
    # payload field and is not wrapped, so a hostile provider fails there
    # first and a dispatch-level assertion would be testing that
    # pre-existing gap instead of this change.
    #
    # What this pins is that the resolution sits INSIDE the guard. Revision 1
    # of the spec placed it above the `try`, where this raises.
    import _cctally_alerts
    ka = _acc("claude", "uuid-697-r-a")
    kb = _acc("claude", "uuid-697-r-b")
    _seed_claude([
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
             email="a@x.com", label="alice", label_source="auto"),
        dict(at="2026-07-02T00:00:00Z", account_key=kb, provider="claude",
             email="b@x.com", label="bob", label_source="auto"),
    ])

    class _Hostile:
        def __str__(self):
            raise RuntimeError("hostile provider")

    assert _cctally_alerts._alert_label_prefix(
        "meter_rate_change", ka, _Hostile()) == ""


def test_697_axis_vendor_holds_exactly_the_seven_threshold_axes(cc):
    # Structural, and the point is the ABSENCE. The mechanism decision is
    # that this family resolves its vendor from the payload, so a
    # `meter_rate_change` entry appearing here alongside a correct
    # implementation would go unnoticed by every behavioural test above.
    import _cctally_alerts
    assert set(_cctally_alerts._AXIS_VENDOR) == {
        "weekly", "five_hour", "budget", "projected",
        "project_budget", "codex_budget", "quota",
    }

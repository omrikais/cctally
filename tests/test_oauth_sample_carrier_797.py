"""#797/#823/#840 T5 — the provider-sample carrier (spec 4.3 D8, section 6
T5).

Every OAuth fetch this binary performs records ``q``, the wall-clock instant
immediately BEFORE ``_fetch_oauth_usage`` is called, and carries it with that
response and its captured credential binding to the publication, where it
becomes the additive raw api ``obs`` payload key ``oauth_request_started_at``.
No key is written for a cache fallback, a failed fetch, a response the hook
discards after reacquiring the selected-state lock, or any other observation.

The fetch stubs here advance the injected clock by 5 s, so the request-start
instant and the capture instant differ: an implementation that used the
capture instant (or any post-fetch clock) as ``q`` fails
`test_q_precedes_fetch_on_*`, and one that remembered an earlier attempt's
``q`` fails `test_no_key_for_failed_fetch`.

The previous-release check, the frozen account-bound revision-1 release
(80f5e1409) reading a store that holds keyed observations, runs the frozen
archive and is maintainer-only, so it lives with the T6 oracle in
`tests/test_mixed_schedule_revision2_797.py` (see .mirror-allowlist).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time

import pytest

from conftest import load_script, redirect_paths

NOW = 1791417600
WEEK_END = NOW + 3 * 86400
FIVE_END = NOW + 3600
KEY = "oauth_request_started_at"
#: Full clock precision: always six fractional digits, always `Z`.
CANONICAL = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")


def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _instant(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def _set_clock(monkeypatch, epoch: float) -> None:
    monkeypatch.setattr(time, "time", lambda: float(epoch))
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(int(epoch)))


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _set_clock(monkeypatch, NOW)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    cctally = sys.modules["cctally"]
    monkeypatch.setattr(cctally, "_nudge_dashboard_repaint", lambda: None)
    monkeypatch.setattr(cctally, "_bust_statusline_cache", lambda: "skipped")
    monkeypatch.setattr(cctally, "_resolve_oauth_token", lambda: "credential-A")
    cctally.open_db().close()
    _identity("A")
    return cctally


def _identity(name: str) -> str:
    import _cctally_core as core
    import _lib_accounts
    core.CLAUDE_JSON_PATH.write_text(json.dumps({"oauthAccount": {
        "accountUuid": name, "emailAddress": f"{name}@example.test",
        "plan": "max"}}))
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    return _lib_accounts.account_key("claude", name)


def _response(weekly=60, five=65):
    return {"seven_day": {"utilization": weekly, "resets_at": _iso(WEEK_END)},
            "five_hour": {"utilization": five, "resets_at": _iso(FIVE_END)}}


def _journal():
    import _cctally_core as core
    records = []
    for path in sorted(core.JOURNAL_DIR.glob("*.jsonl")):
        records.extend(json.loads(line) for line in path.read_text().splitlines()
                       if line.strip())
    return records


def _usage_obs():
    return [r for r in _journal()
            if r.get("t") == "obs" and r.get("provider") == "claude"
            and "weekly_percent" in (r.get("payload") or {})]


def _keyed():
    return [r for r in _usage_obs() if KEY in r["payload"]]


def _advancing_fetch(monkeypatch, app, *, seconds=5, response=None):
    """A fetch stub that records the clock it was called at and advances the
    injected clock by ``seconds`` before returning."""
    seen = []

    def fetch(**_kwargs):
        before = time.time()
        seen.append(before)
        _set_clock(monkeypatch, before + seconds)
        return response or _response()

    monkeypatch.setattr(app, "_fetch_oauth_usage", fetch)
    return seen


def _assert_q_is_the_pre_call_instant(record, called_at):
    q = record["payload"][KEY]
    assert isinstance(q, str) and CANONICAL.match(q), q
    assert _instant(q) == dt.datetime.fromtimestamp(called_at, dt.timezone.utc)
    captured = _instant(record["payload"]["captured_at"])
    assert _instant(q) < captured, (q, record["payload"]["captured_at"])
    assert (captured - _instant(q)).total_seconds() == 5


# --------------------------------------------------------------------------
# q is the instant immediately before the fetch
# --------------------------------------------------------------------------

def test_q_precedes_fetch_on_forced_refresh(app, monkeypatch):
    seen = _advancing_fetch(monkeypatch, app)
    assert app._refresh_usage_inproc().status == "ok"
    [record] = _usage_obs()
    assert record["payload"]["source"] == "api"
    _assert_q_is_the_pre_call_instant(record, seen[0])


def test_q_precedes_fetch_on_hook_tick(app, monkeypatch):
    seen = _advancing_fetch(monkeypatch, app)
    status, _payload = app._hook_tick_oauth_refresh(throttle_seconds=0)
    assert status.startswith("ok"), status
    [record] = _usage_obs()
    assert record["payload"]["source"] == "api"
    _assert_q_is_the_pre_call_instant(record, seen[0])


# --------------------------------------------------------------------------
# no key for anything but a published fetch
# --------------------------------------------------------------------------

def _direct_record(app, *, weekly, source):
    """An ordinary bound recording (status line or direct `record-usage`)."""
    import _cctally_statusline as sl
    args = argparse.Namespace(
        percent=weekly, resets_at=str(WEEK_END), five_hour_percent=None,
        five_hour_resets_at=None, source=source)
    assert app.record_usage_bound(
        args, binding=sl._capture_binding(), nudge_dashboard=False) == 0


def test_no_key_for_cache_fallback(app, monkeypatch):
    """A rate-limited forced refresh falls back to the cached snapshot and
    publishes nothing; the request-start instant it recorded never reaches a
    later recording of the same process."""
    import _cctally_refresh

    def limited(**_kwargs):
        _set_clock(monkeypatch, time.time() + 5)
        raise _cctally_refresh.RefreshUsageRateLimitError("429")

    monkeypatch.setattr(app, "_fetch_oauth_usage", limited)
    result = app._refresh_usage_inproc()
    assert result.status == "rate_limited" and result.fallback
    assert _usage_obs() == []
    for source in ("statusline", "api"):
        _direct_record(app, weekly=40, source=source)
    assert len(_usage_obs()) == 2
    assert _keyed() == []


@pytest.mark.parametrize("failure", ["network", "malformed", "unparsable"])
def test_no_key_for_failed_fetch(app, monkeypatch, failure):
    """A failed fetch writes nothing, and the next successful fetch carries
    its OWN request-start instant, not the failed attempt's."""
    import _cctally_refresh

    def failing(**_kwargs):
        _set_clock(monkeypatch, time.time() + 5)
        if failure == "network":
            raise _cctally_refresh.RefreshUsageNetworkError("down")
        if failure == "malformed":
            raise _cctally_refresh.RefreshUsageMalformedError("bad body")
        return {"seven_day": {"utilization": "n/a", "resets_at": "never"}}

    monkeypatch.setattr(app, "_fetch_oauth_usage", failing)
    assert app._refresh_usage_inproc().status != "ok"
    assert app._hook_tick_oauth_refresh(
        throttle_seconds=0)[0].startswith("err"), "the hook must fail too"
    assert _usage_obs() == []
    _set_clock(monkeypatch, NOW + 100)
    seen = _advancing_fetch(monkeypatch, app)
    assert app._refresh_usage_inproc().status == "ok"
    [record] = _keyed()
    _assert_q_is_the_pre_call_instant(record, seen[0])
    assert _instant(record["payload"][KEY]) == dt.datetime.fromtimestamp(
        NOW + 100, dt.timezone.utc)


def test_no_key_for_hook_discarded_response(app, monkeypatch):
    """The hook releases the selected-state lock for the network request and
    rechecks freshness after reacquiring it; a concurrent winner makes it
    discard the response, which therefore publishes nothing and leaves no
    request-start instant behind."""
    winner = {"fresh": False}

    def fetch(**_kwargs):
        _set_clock(monkeypatch, time.time() + 5)
        winner["fresh"] = True
        return _response()

    monkeypatch.setattr(app, "_fetch_oauth_usage", fetch)
    monkeypatch.setattr(
        app, "_newest_snapshot_age_seconds",
        lambda *a, **k: 0.0 if winner["fresh"] else None)
    status, payload = app._hook_tick_oauth_refresh(throttle_seconds=60)
    assert status.startswith("skipped(fresh"), status
    assert payload is None
    assert _usage_obs() == []
    _direct_record(app, weekly=40, source="api")
    assert len(_usage_obs()) == 1
    assert _keyed() == []

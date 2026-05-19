"""Unit + integration tests for GET /api/project/<key> (spec §5.3, §6.5).

Covers both `_project_detail_for_window` (pure builder) and the HTTP
route handler that wraps it.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys

import pytest

from conftest import load_script  # noqa: E402

_NS = load_script()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import _cctally_dashboard  # noqa: E402

_project_detail_for_window = _cctally_dashboard._project_detail_for_window
_build_projects_envelope = _cctally_dashboard._build_projects_envelope


FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "projects"
NOW_UTC = dt.datetime(2026, 5, 19, 12, 0, 0, tzinfo=dt.timezone.utc)


def _open(path: pathlib.Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


# --- Unit-level tests of `_project_detail_for_window` ---------------------


def test_detail_for_known_key():
    conn = _open(FIXTURE_DIR / "multi-week.db")
    detail = _project_detail_for_window(
        conn,
        project_key="cctally-dev",
        weeks_back=4,
        now_utc=NOW_UTC,
        current_week=None,
    )
    assert detail is not None
    assert detail["key"] == "cctally-dev"
    assert detail["window_weeks"] == 4
    assert isinstance(detail["models"], list)
    assert isinstance(detail["sessions"], list)
    assert len(detail["sessions"]) <= 5  # top-5 cap per spec §5.3


def test_detail_404_on_unknown_key():
    conn = _open(FIXTURE_DIR / "multi-week.db")
    detail = _project_detail_for_window(
        conn,
        project_key="does-not-exist",
        weeks_back=4,
        now_utc=NOW_UTC,
        current_week=None,
    )
    assert detail is None


def test_detail_models_sorted_desc_by_cost():
    conn = _open(FIXTURE_DIR / "multi-week.db")
    detail = _project_detail_for_window(
        conn,
        project_key="cctally-dev",
        weeks_back=12,
        now_utc=NOW_UTC,
        current_week=None,
    )
    assert detail is not None
    costs = [m["cost_usd"] for m in detail["models"]]
    assert costs == sorted(costs, reverse=True)


def test_detail_window_cost_matches_trend_per_project():
    """R-PROJ4 invariant: endpoint window_cost_usd ==
    sum(trend.projects[k].weekly_cost) over the same window."""
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=4,
    )
    # Pick the top-cost project for the assertion (highest signal).
    target = env["trend"]["projects"][0]
    detail = _project_detail_for_window(
        conn,
        project_key=target["key"],
        weeks_back=4,
        now_utc=NOW_UTC,
        current_week=None,
    )
    assert detail is not None
    target_sum = sum(target["weekly_cost"])
    assert abs(detail["window_cost_usd"] - target_sum) < 1e-9


def test_detail_disambiguated_key_round_trips():
    """`foo (repos)` in the edge-cases fixture must resolve back through
    the endpoint (spec §9.2 R-PROJ3 note — disambiguated keys are the
    canonical identity, NOT the bare `display_key`)."""
    conn = _open(FIXTURE_DIR / "edge-cases.db")
    detail = _project_detail_for_window(
        conn,
        project_key="foo (repos)",
        weeks_back=4,
        now_utc=NOW_UTC,
        current_week=None,
    )
    assert detail is not None
    assert detail["key"] == "foo (repos)"
    # The bucket_path is the canonical equality key — confirm it's the
    # absolute path, not the disambiguated label.
    assert detail["bucket_path"] == "/repos/foo"


def test_detail_window_attributed_pct_sum_consistent():
    """The endpoint's `window_attributed_pct` must equal the sum across
    weeks within the window of `(project_cost / week_total) * week_pct`.

    Stays None when no contributing week has a snapshot.
    """
    conn = _open(FIXTURE_DIR / "edge-cases.db")
    detail = _project_detail_for_window(
        conn,
        project_key="foo (repos)",
        weeks_back=4,
        now_utc=NOW_UTC,
        current_week=None,
    )
    assert detail is not None
    # edge-cases fixture has NO weekly_usage_snapshots row → None.
    assert detail["window_attributed_pct"] is None


# --- HTTP-level tests: invoke `_handle_get_project_detail` ----------------
# Light-weight test double for the BaseHTTPRequestHandler — exercises the
# route handler without spinning up a real server. The handler reads its
# own ``self.path`` to extract the query, so we mock that plus the
# ``send_*`` / ``wfile`` machinery enough to capture the (status,
# headers, body) tuple.


class _FakeHandler:
    """Minimal HTTPHandler stand-in for the project endpoint.

    Only fields/methods touched by `_handle_get_project_detail` are
    implemented. Each instance is single-use.
    """

    def __init__(self, path: str, snapshot, dashboard_mod):
        self.path = path
        self._snapshot = snapshot
        self._dashboard = dashboard_mod
        self.status: int | None = None
        self.headers: list[tuple[str, str]] = []
        self.body: bytes = b""
        # Allow `self.snapshot_ref.get()` to return the fixture snapshot.
        self.snapshot_ref = self  # `.get()` lives on us, see below.

    def get(self):  # snapshot_ref.get()
        return self._snapshot

    def send_response(self, code: int) -> None:
        self.status = code

    def send_header(self, name: str, value: str) -> None:
        self.headers.append((name, value))

    def end_headers(self) -> None:
        pass

    def send_error(self, code: int, message: str | None = None) -> None:
        self.status = code
        if message is not None:
            self.body = message.encode("utf-8")

    @property
    def wfile(self):
        return self  # write goes back to us.

    def write(self, data: bytes) -> None:  # wfile.write
        self.body += data

    def log_error(self, *a, **kw) -> None:  # silenced
        pass


def _build_fake_snapshot():
    """Conjure a minimal object that the handler dereferences via
    ``snap.generated_at``. Other attrs are accessed only on success
    paths we don't exercise here."""
    class _Snap:
        generated_at = NOW_UTC
        current_week = None
    return _Snap()


def _open_for_handler(fixture: str) -> sqlite3.Connection:
    return _open(FIXTURE_DIR / fixture)


def _build_handler(path: str):
    snap = _build_fake_snapshot()
    h = _FakeHandler(path, snap, _cctally_dashboard)
    # Stash the conn factory so the handler can open the fixture.
    h._open_conn = lambda: _open_for_handler("multi-week.db")
    return h


def _call_route(path: str, fixture: str = "multi-week.db"):
    """Invoke `_handle_get_project_detail` with a fake handler.

    The production handler uses `c.open_cache_db()` / similar to acquire
    a conn. The simplest test surface: monkeypatch
    `_cctally_dashboard._project_endpoint_open_conn` to return the
    fixture conn.
    """
    snap = _build_fake_snapshot()
    h = _FakeHandler(path, snap, _cctally_dashboard)
    conn = _open(FIXTURE_DIR / fixture)
    try:
        _cctally_dashboard._handle_get_project_detail_impl(h, conn=conn)
    finally:
        conn.close()
    return h


def test_http_endpoint_returns_200_and_json():
    h = _call_route("/api/project/cctally-dev?weeks=4")
    assert h.status == 200, f"status={h.status} body={h.body!r}"
    body = json.loads(h.body)
    assert body["key"] == "cctally-dev"
    assert body["window_weeks"] == 4
    assert "models" in body
    assert "sessions" in body


def test_http_endpoint_400_on_bad_weeks():
    h = _call_route("/api/project/cctally-dev?weeks=99")
    assert h.status == 400, f"status={h.status} body={h.body!r}"
    body = json.loads(h.body)
    assert "error" in body


def test_http_endpoint_400_on_missing_weeks():
    h = _call_route("/api/project/cctally-dev")
    assert h.status == 400


def test_http_endpoint_400_on_non_numeric_weeks():
    h = _call_route("/api/project/cctally-dev?weeks=foo")
    assert h.status == 400


def test_http_endpoint_404_on_unknown_key():
    h = _call_route("/api/project/no-such-project?weeks=4")
    assert h.status == 404, f"status={h.status} body={h.body!r}"


def test_http_endpoint_urldecodes_key():
    """`foo (repos)` arrives URL-encoded as `foo%20(repos)` — must
    round-trip back to the disambiguated label."""
    h = _call_route(
        "/api/project/foo%20%28repos%29?weeks=4",
        fixture="edge-cases.db",
    )
    assert h.status == 200, f"status={h.status} body={h.body!r}"
    body = json.loads(h.body)
    assert body["key"] == "foo (repos)"

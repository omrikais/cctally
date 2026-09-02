"""#620 S2 — `GET /api/diagnosis`, the dashboard half of the diagnosis.

The route publishes exactly what `diagnosis_to_wire` produces, over the same
`build_diagnosis` the CLI calls, so the two surfaces cannot drift. What is
tested here is what the ROUTE adds: registration, selector parsing, the status
mapping, the read-only promise and the header contract.

The status mapping is the part that is easy to get wrong, because a withheld
report is a legitimate answer for every cause except one. A report fully
withheld as `retained_range_mismatch`, `stale_evidence`, `pricing_unavailable`
or `insufficient_population` is 200; a report whose requested provider's store
could not be READ, with no other provider answering, is 503 with the report
still in the body — the same condition the CLI turns into exit 3 while still
printing the report.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import http.client
import json
import sys
import threading
from dataclasses import replace

import pytest

from conftest import load_script, redirect_paths
from test_620_s2_diagnosis_sources import (  # reuse the seeded corpora
    WINDOW_END, WINDOW_START, _seed_claude, _seed_claude_blocks,
)

from tests._support_http import PRESENCE_BACKSTOP_SECONDS, start, stop


UTC = dt.timezone.utc
_WINDOW = f"{WINDOW_START.date().isoformat()}..{WINDOW_END.date().isoformat()}"
_WINDOW_QUERY = f"{_WINDOW}&tz=Etc%2FUTC"


def _dash():
    return sys.modules["_cctally_dashboard"]


class _Response:
    def __init__(self, status: int, headers, body: str) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def json(self):
        return json.loads(self.body)


class _Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def get(self, path: str, *, headers: dict | None = None) -> _Response:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            body = response.read().decode()
            return _Response(response.status, dict(response.getheaders()), body)
        finally:
            conn.close()


def _boot(ns):
    """A real server on an ephemeral loopback port.

    Driven over HTTP rather than by calling the handler method directly, so
    `_dispatch`, `_require_api_auth` and the header write are all exercised —
    a route that is registered but shadowed, or one that sends its body before
    its status, is only visible from outside.
    """
    dash = _dash()
    dash.DashboardHTTPHandler.hub = ns["SSEHub"]()
    dash.DashboardHTTPHandler.snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    dash.DashboardHTTPHandler.hub.publish(
        dash.DashboardHTTPHandler.snapshot_ref.get()
    )
    dash.DashboardHTTPHandler.no_sync = True
    # The bind this fixture models. It decides `_transcripts_visible_to_request`
    # now that the route threads that predicate into plan stage 1, and these are
    # CLASS attributes, so a value a neighbouring module left behind would
    # otherwise decide it.
    dash.DashboardHTTPHandler.cctally_host = "127.0.0.1"
    dash.DashboardHTTPHandler.cctally_expose_transcripts = False
    server = dash._QuietThreadingHTTPServer(
        ("127.0.0.1", 0), dash.DashboardHTTPHandler
    )
    thread = start(server)
    return server, thread, _Client(server.server_address[1])


@pytest.fixture
def rich_server(tmp_path, monkeypatch):
    """A store with a dominant model, project, session and block."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    _seed_claude(ns, models=(("claude-opus-4-20250514", 50),
                             ("claude-haiku-4-20250514", 10)))
    _seed_claude_blocks(ns)
    server, thread, client = _boot(ns)
    try:
        yield client
    finally:
        stop(server, thread)


@pytest.fixture
def empty_server(tmp_path, monkeypatch):
    """Both stores present and empty — a legitimately withheld report."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    ns["open_cache_db"]().close()
    ns["open_db"]().close()
    server, thread, client = _boot(ns)
    try:
        yield client
    finally:
        stop(server, thread)


@pytest.fixture
def storeless_server(tmp_path, monkeypatch):
    """No store files at all — the one withheld cause that is also a failure."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    server, thread, client = _boot(ns)
    try:
        yield client
    finally:
        stop(server, thread)


# --- registration -------------------------------------------------------

def test_route_is_registered_as_exact():
    """An exact entry, so it cannot shadow the parameterized conversation
    routes and they cannot shadow it."""
    load_script()
    entry = next(e for e in _dash()._GET_ROUTES if e[1] == "/api/diagnosis")
    assert entry[0] == "exact"
    assert entry[2] == "_handle_get_diagnosis"
    assert entry[4] is False


def test_single_flight_key_separates_every_build_and_authorization_axis():
    """No selector or privacy variant may receive another request's report."""
    ns = load_script()
    sources = ns["_load_sibling"]("_cctally_diagnosis_sources")
    base = sources.DiagnosisScope(
        source="codex",
        account_key="account-a",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        effective_speed="standard",
        display_tz="Etc/UTC",
        label="current",
    )
    scopes = (
        base,
        replace(base, source="claude"),
        replace(base, account_key="account-b"),
        replace(
            base,
            window_start=WINDOW_START + dt.timedelta(hours=1),
            window_end=WINDOW_END + dt.timedelta(hours=1),
        ),
        replace(base, effective_speed="fast"),
        replace(base, display_tz="Asia/Jerusalem"),
        replace(base, label="custom"),
    )
    keys = [
        _dash()._diagnosis_flight_key(scope, True, False)
        for scope in scopes
    ]
    keys.append(_dash()._diagnosis_flight_key(base, False, False))
    keys.append(_dash()._diagnosis_flight_key(base, True, True))
    assert len(set(keys)) == len(keys)


def test_process_wide_admission_serializes_distinct_scopes():
    """Different selectors do not coalesce, but still cannot multiply heaps."""
    load_script()
    admission = _dash()._DiagnosisAdmission()
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def _first():
        first_entered.set()
        assert release_first.wait(PRESENCE_BACKSTOP_SECONDS)
        return "first"

    def _second():
        second_entered.set()
        return "second"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            admission.run, ("scope-a", True, False), _first,
        )
        assert first_entered.wait(PRESENCE_BACKSTOP_SECONDS)
        second = executor.submit(
            admission.run, ("scope-b", True, False), _second,
        )
        with admission._changed:
            assert admission._changed.wait_for(
                lambda: len(admission._flights) == 2,
                timeout=PRESENCE_BACKSTOP_SECONDS,
            ), "the second diagnosis never registered for admission"
            assert not second_entered.is_set(), (
                "distinct diagnosis scopes ran concurrently"
            )
        release_first.set()
        assert first.result() == "first"
        assert second.result() == "second"


def test_the_diagnosis_is_not_an_envelope_key(tmp_path, monkeypatch):
    """B8: the diagnosis is on-demand, never a per-tick envelope cost."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    envelope = ns["snapshot_to_envelope"](ns["_empty_dashboard_snapshot"](),
                                          now_utc=WINDOW_END)
    assert "diagnosis" not in envelope
    assert "explain" not in envelope


# --- the status mapping -------------------------------------------------

def test_a_ranked_report_is_200(rich_server):
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 200, response.body
    payload = response.json
    assert payload["schemaVersion"] == 1
    assert payload["overallVerdict"] == "contributor_detected"
    assert payload["results"][0]["contributors"]


def test_valid_report_is_200_even_when_every_class_is_withheld(empty_server):
    """A withheld answer is a correct answer about what the store holds."""
    response = empty_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 200, response.body
    assert response.json["overallVerdict"] == "withheld"


def test_malformed_window_is_400(rich_server):
    response = rich_server.get("/api/diagnosis?window=nonsense")
    assert response.status == 400, response.body
    assert response.json["code"] == "range_unresolved"


def test_an_unresolvable_account_is_400(rich_server):
    response = rich_server.get(
        f"/api/diagnosis?window={_WINDOW_QUERY}&account=no-such-account")
    assert response.status == 400, response.body
    assert response.json["code"] == "account_unresolved"


def test_account_with_source_all_is_400(rich_server):
    """Account keys are provider-scoped; one selector cannot address both.
    The CLI exits 2 on the same condition."""
    response = rich_server.get(
        f"/api/diagnosis?window={_WINDOW_QUERY}&source=all&account=x")
    assert response.status == 400, response.body


def test_an_unreadable_store_publishes_the_report_with_503(storeless_server):
    """The same condition the CLI turns into exit 3, and for the same reason
    the CLI still prints: a person needs to see the typed cause."""
    response = storeless_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 503, response.body
    payload = response.json
    assert payload["overallCode"] == "provider_unavailable"
    assert payload["results"][0]["denominator"]["usd"]["state"] == "withheld"


def test_a_store_failure_resolving_the_window_is_503(rich_server, monkeypatch):
    """The route's `store_unavailable` arm, reached.

    `build_diagnosis` converts every store failure it meets into a withheld
    provider result, so the only `store_unavailable` that escapes as an
    `EstablishmentFailure` comes from selector resolution — the week anchor,
    which reads stats.db through the ordinary opener. Before that read was
    classified, a malformed store here surfaced as an unexplained 500.
    """
    import sqlite3

    dk = load_script()["_load_sibling"]("_lib_diff_kernel")

    def _raise(*_a, **_kw):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(dk, "_diff_resolve_anchor", _raise)
    response = rich_server.get("/api/diagnosis?window=this-week")
    assert response.status == 503, response.body
    assert response.json["code"] == "store_unavailable"


def test_an_unresolvable_week_anchor_is_400_not_500(rich_server, monkeypatch):
    """A token the grammar cannot resolve without an anchor the machine does
    not hold is an unresolved RANGE, which is the 400 arm. It reached the
    outer handler as a bare `RuntimeError` and was served as 500."""
    dk = load_script()["_load_sibling"]("_lib_diff_kernel")
    monkeypatch.setattr(dk, "_diff_resolve_anchor", lambda *_a, **_kw: (None, None))
    response = rich_server.get("/api/diagnosis?window=this-week")
    assert response.status == 400, response.body
    assert response.json["code"] == "range_unresolved"


def test_generation_incoherent_is_503(rich_server, monkeypatch):
    sources = load_script()["_load_sibling"]("_cctally_diagnosis_sources")
    probes = iter(["a", "b", "b", "c"] * 40)
    monkeypatch.setattr(sources, "_probe_component",
                        lambda *_a, **_kw: next(probes))
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 503, response.body
    assert response.json["code"] == "generation_incoherent"


def test_an_unexpected_exception_is_500_and_never_a_healthy_200(
    rich_server, monkeypatch,
):
    sources = load_script()["_load_sibling"]("_cctally_diagnosis_sources")

    def _raises(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(sources, "build_diagnosis", _raises)
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 500, response.body
    assert "no_contributor_detected" not in response.body
    assert "contributor_detected" not in response.body


# --- gating and headers -------------------------------------------------

def test_no_csrf_required_for_this_read_only_route(rich_server):
    """`_check_origin_csrf` is opt-in for mutating routes. This one mutates
    nothing, so a request carrying no Origin is served."""
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 200, response.body


def test_response_sets_no_cache(rich_server):
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["Content-Type"].startswith("application/json")


def test_api_auth_applies_to_the_route(rich_server, monkeypatch):
    """`_require_api_auth` runs before dispatch, so the route inherits it."""
    handler = _dash().DashboardHTTPHandler
    monkeypatch.setattr(handler, "cctally_api_token", "sekret")
    assert rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}").status == 401
    ok = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}",
                         headers={"Authorization": "Bearer sekret"})
    assert ok.status == 200, ok.body


# --- read-only ----------------------------------------------------------

def test_the_route_never_opens_a_writable_store(rich_server, monkeypatch):
    """Read-only is structural: the route must not migrate, repair or write.

    The writable openers are the ones that perform schema work, migration,
    legacy import, contract repair and replay. Reaching either from this
    request is the failure, and it is observable without trusting a grep.
    """
    ns = load_script()
    opened: list[str] = []
    for name in ("open_cache_db", "open_db"):
        real = ns[name]

        def _record(*a, _name=name, _real=real, **k):
            opened.append(_name)
            return _real(*a, **k)

        monkeypatch.setitem(ns, name, _record)
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 200, response.body
    assert opened == []


# --- selectors ----------------------------------------------------------

def test_reveal_projects_widens_only_the_label(rich_server):
    anonymized = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}").json
    revealed = rich_server.get(
        f"/api/diagnosis?window={_WINDOW_QUERY}&reveal_projects=1").json

    def _projects(payload):
        return [row for result in payload["results"]
                for row in result["contributors"]
                if row["subjectKind"] == "project"]

    assert _projects(anonymized), "non-vacuity: a project row must be reported"
    for row in _projects(anonymized):
        assert row["subjectLabel"].startswith("project-")
    for row in _projects(revealed):
        assert not row["subjectLabel"].startswith("project-")
        # `--reveal-projects` widens the LABEL only; the published key stays
        # the response-local alias in every mode.
        assert row["subjectKey"].startswith("project-")
    for row in _projects(anonymized) + _projects(revealed):
        assert "/" not in row["subjectLabel"], "a filesystem path escaped"


def test_explicit_half_open_bounds_are_honoured(rich_server):
    """The alert-follow path carries instants, not dates: a five-hour block
    start is not a calendar day, so the route must accept the bounds the
    warning actually recorded."""
    start = WINDOW_START.isoformat().replace("+00:00", "Z")
    end = WINDOW_END.isoformat().replace("+00:00", "Z")
    response = rich_server.get(
        f"/api/diagnosis?start_at={start}&end_at={end}")
    assert response.status == 200, response.body
    assert response.json["window"]["startAt"] == start
    assert response.json["window"]["endAt"] == end


def test_half_open_bounds_must_be_ordered(rich_server):
    start = WINDOW_END.isoformat().replace("+00:00", "Z")
    end = WINDOW_START.isoformat().replace("+00:00", "Z")
    response = rich_server.get(
        f"/api/diagnosis?start_at={start}&end_at={end}")
    assert response.status == 400, response.body
    assert response.json["code"] == "range_unresolved"


def test_a_malformed_bound_is_400(rich_server):
    response = rich_server.get(
        "/api/diagnosis?start_at=not-a-time&end_at=also-not")
    assert response.status == 400, response.body
    assert response.json["code"] == "range_unresolved"


def test_source_all_publishes_one_result_per_provider(rich_server):
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}&source=all")
    assert response.status == 200, response.body
    assert [r["source"] for r in response.json["results"]] == ["claude",
                                                               "codex"]


def test_concurrent_identical_diagnoses_share_one_process_wide_build(
    empty_server, monkeypatch,
):
    """Concurrent tabs cannot multiply the largest diagnosis process tree.

    The real HTTP server supplies one request thread per client.  Holding the
    first build open makes a second entry observable without relying on route
    timing, while the ``source=all`` selector keeps the production worker
    shape in scope.  Identical callers must join that in-flight result: one
    admitted build means one isolated provider worker and one report heap.
    """
    sources = sys.modules["cctally"]._load_sibling(
        "_cctally_diagnosis_sources"
    )
    real_build = sources.build_diagnosis
    lock = threading.Lock()
    first_entered = threading.Event()
    another_entered = threading.Event()
    release = threading.Event()
    admission = _dash()._DiagnosisAdmission()
    builds = 0
    active = 0
    peak_active = 0

    def _blocked_build(*args, **kwargs):
        nonlocal builds, active, peak_active
        with lock:
            builds += 1
            active += 1
            peak_active = max(peak_active, active)
            if builds == 1:
                first_entered.set()
            else:
                another_entered.set()
        try:
            assert release.wait(PRESENCE_BACKSTOP_SECONDS), (
                "the concurrent-route test did not release"
            )
            return real_build(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(sources, "build_diagnosis", _blocked_build)
    monkeypatch.setattr(_dash(), "_DIAGNOSIS_ADMISSION", admission)
    path = f"/api/diagnosis?window={_WINDOW_QUERY}&source=all"
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(empty_server.get, path)
        assert first_entered.wait(PRESENCE_BACKSTOP_SECONDS), (
            "the first diagnosis never started"
        )
        followers = [executor.submit(empty_server.get, path) for _ in range(2)]
        with admission._changed:
            assert admission._changed.wait_for(
                lambda: any(
                    flight.callers == 3
                    for flight in admission._flights.values()
                ),
                timeout=PRESENCE_BACKSTOP_SECONDS,
            ), "the concurrent diagnosis callers did not all register"
            assert not another_entered.is_set(), (
                "more than one diagnosis build was admitted"
            )
        release.set()
        responses = [first.result(), *(future.result() for future in followers)]

    assert builds == 1, "identical concurrent requests were not single-flight"
    assert peak_active == 1
    assert [response.status for response in responses] == [200, 200, 200]

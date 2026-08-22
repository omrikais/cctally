"""#620 S3 — the dashboard route's transcript gate (spec §5.3, C6, C20, D-E).

The route evaluates `_transcripts_visible_to_request` ONCE, before the plan is
resolved, and passes the result into plan stage 1. A class denied there is
settled: the store it would have needed is never opened, probed or digested,
and the identifier the response publishes describes what actually ran.

Three things this file pins that nothing else can:

  * a denied request is 200, not 403. A whole-route 403 would discard the
    accounting evidence the request IS authorized to see.
  * a denied request opens no conversations connection at all. Before this
    session's fix the route called `build_diagnosis` without the gate value,
    so the `True` default applied and every request — denied or not — paid for
    the projection and published a `generationId` bound to transcript-derived
    facts.
  * Codex `subagent_fanout` still measures on a denied route, because it reads
    `codex_conversation_threads` and `codex_session_entries`, both of which
    live in `cache.db`. That is the Codex table of Section 3, and it is the
    one cell where the same class name renders two verdicts in one report.
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import http.client
import inspect
import json
import pathlib
import sys
import textwrap
import threading

import pytest

from conftest import load_script, redirect_paths
from test_620_s2_diagnosis_sources import (  # reuse the seeded corpora
    WINDOW_END, WINDOW_START, _seed_claude, _seed_claude_blocks,
    _seed_codex_windows,
)
from test_620_s3_evaluators import (  # the S3 corpora
    CODEX_MODEL, _seed_churn, _seed_codex_entry, _seed_codex_thread,
)

from tests._support_http import start, stop

UTC = dt.timezone.utc
_WINDOW = f"{WINDOW_START.date().isoformat()}..{WINDOW_END.date().isoformat()}"

# A loopback IP literal is visible; a HOSTNAME is the DNS-rebinding vector the
# gate rejects, so it is how a denied request is produced without binding this
# test to a LAN interface. Both go through `_transcripts_visible_to_request`
# unmodified — D-E requires exercising the configuration, never inventing a
# denial by patching the predicate.
_DENIED_HOST = "dashboard.example.test"


def _dash():
    return sys.modules["_cctally_dashboard"]


def _sources():
    """The adapter module, loaded the way the route loads it.

    `sys.modules` alone is not enough: the route lazy-loads this sibling on
    the first request, so a test that patches it before any request has run
    would raise `KeyError` rather than patch anything.
    """
    return load_script()["_load_sibling"]("_cctally_diagnosis_sources")


class _Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def json(self):
        return json.loads(self.body)


class _Client:
    def __init__(self, port):
        self.port = port

    def get(self, path, *, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {}
            if host is not None:
                headers["Host"] = host
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            body = response.read().decode()
            return _Response(response.status, dict(response.getheaders()), body)
        finally:
            conn.close()


def _boot(ns, *, bind="127.0.0.1", expose=False):
    dash = _dash()
    handler = dash.DashboardHTTPHandler
    handler.hub = ns["SSEHub"]()
    handler.snapshot_ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    handler.hub.publish(handler.snapshot_ref.get())
    handler.no_sync = True
    # Stated rather than inherited: these are CLASS attributes, so a value a
    # neighbouring module left behind would decide this test's gate.
    handler.cctally_host = bind
    handler.cctally_expose_transcripts = expose
    handler.cctally_api_token = None
    server = dash._QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = start(server)
    return server, thread, _Client(server.server_address[1])


@pytest.fixture
def claude_server(tmp_path, monkeypatch):
    """A Claude store with accounting evidence AND transcript evidence."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    # Distinct session names, because `_seed_churn` owns
    # `/tmp/projects/sess-a.jsonl` and the two seeders would otherwise collide
    # on `session_entries(source_path, line_offset)`.
    _seed_claude(ns, models=(("claude-opus-4-20250514", 50),
                             ("claude-haiku-4-20250514", 10)),
                 projects=("/repo/x", "/repo/y"),
                 sessions=("sess-x", "sess-y"))
    _seed_claude_blocks(ns)
    _seed_churn(ns, compaction_before_window=False)
    server, thread, client = _boot(ns)
    try:
        yield client
    finally:
        stop(server, thread)


@pytest.fixture
def codex_server(tmp_path, monkeypatch):
    """A Codex store whose fan-out lives entirely in `cache.db`."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    _seed_codex_fanout_population(ns)
    server, thread, client = _boot(ns)
    try:
        yield client
    finally:
        stop(server, thread)


def _seed_codex_fanout_population(ns):
    """One resolvable parent and two delegated children, at REPORT scale.

    The evaluator unit tests seed four entries, which is enough to decide the
    predicate and not enough to clear `min_priced_entries`. A route test that
    asserts a VERDICT needs a population the class can be measured over, or it
    would assert `withheld / insufficient_population` and prove nothing about
    the gate.
    """
    cache = ns["open_cache_db"]()
    try:
        _seed_codex_thread(cache, key="v1.root-a.parent", native="p1",
                           root_thread="user")
        for index in range(12):
            _seed_codex_entry(
                cache, key="v1.root-a.parent", offset=index,
                at=WINDOW_START + dt.timedelta(minutes=5 * index + 5))
        for slot, child in enumerate(("c1", "c2")):
            _seed_codex_thread(cache, key=f"v1.root-a.{child}", native=child,
                               root_thread="subagent", parent="p1")
            for index in range(12):
                _seed_codex_entry(
                    cache, key=f"v1.root-a.{child}", offset=index,
                    at=WINDOW_START + dt.timedelta(
                        minutes=80 + 60 * slot + 5 * index),
                    model=CODEX_MODEL, input_tokens=4_000)
        cache.commit()
    finally:
        cache.close()
    _seed_codex_windows(ns, windows=[("root-a", "codex_standard", 0, 300)])


def _by_kind(response, index=0):
    return {c["contributorClass"]: c
            for c in response.json["results"][index]["classes"]}


# --- the denial is partial, never a 403 ---------------------------------

def test_a_denied_request_returns_200_with_s2_classes_still_measured(
        claude_server):
    """A whole-route 403 would discard accounting evidence the request IS
    authorized to see, and would contradict S2's partially-withheld contract.
    """
    response = claude_server.get(f"/api/diagnosis?source=claude&window={_WINDOW}",
                                 host=_DENIED_HOST)
    assert response.status == 200, response.body
    by_kind = _by_kind(response)
    assert by_kind["model_mix"]["verdict"] != "withheld"
    for kind in ("cache_churn", "short_high_context", "subagent_fanout"):
        assert by_kind[kind]["code"] == "transcripts_not_visible", kind


def test_the_same_request_from_a_loopback_host_measures_them(claude_server):
    """The discriminating half. Without it the test above would pass over a
    fixture that had nothing to measure in the first place."""
    response = claude_server.get(f"/api/diagnosis?source=claude&window={_WINDOW}",
                                 host="127.0.0.1")
    assert response.status == 200, response.body
    by_kind = _by_kind(response)
    for kind in ("cache_churn", "short_high_context", "subagent_fanout"):
        assert by_kind[kind]["code"] != "transcripts_not_visible", kind


def test_a_denied_request_never_opens_the_conversations_store(
        claude_server, monkeypatch):
    """C20. Before the gate was threaded the route paid for the projection on
    every request and published a `generationId` bound to transcript-derived
    facts, whether or not the request was allowed to read them."""
    sources = _sources()
    opened = []
    real = sources.open_read_only

    def _spy(kind):
        opened.append(kind)
        return real(kind)

    monkeypatch.setattr(sources, "open_read_only", _spy)
    response = claude_server.get(f"/api/diagnosis?source=claude&window={_WINDOW}",
                                 host=_DENIED_HOST)
    assert response.status == 200, response.body
    assert "conversations" not in opened, opened
    # Non-vacuity: the accounting stores WERE opened, so the absence above is
    # a decision rather than a request that never reached the adapter.
    assert "cache" in opened, opened


def test_a_denied_plan_publishes_no_conversations_generation_component(
        claude_server):
    """The identifier describes what actually ran. A denied plan reads no
    conversation bytes, so it publishes no `conversations` component — and its
    `generationId` differs from the allowed plan's, because the plan is part
    of the identity."""
    denied = claude_server.get(f"/api/diagnosis?source=claude&window={_WINDOW}",
                               host=_DENIED_HOST).json
    allowed = claude_server.get(f"/api/diagnosis?source=claude&window={_WINDOW}",
                                host="127.0.0.1").json
    assert "conversations" not in denied["results"][0]["generation"]
    assert "conversations" in allowed["results"][0]["generation"]
    assert (denied["results"][0]["generation"]["generationId"]
            != allowed["results"][0]["generation"]["generationId"])


def test_codex_fanout_is_measured_on_a_denied_route(codex_server):
    """The Codex table of Section 3: fan-out reads only `cache.db`, so it
    needs no transcript authorization and none is claimed."""
    response = codex_server.get(f"/api/diagnosis?source=codex&window={_WINDOW}",
                                host=_DENIED_HOST)
    assert response.status == 200, response.body
    by_kind = _by_kind(response)
    assert by_kind["subagent_fanout"]["verdict"] == "contributor"
    assert by_kind["cache_churn"]["verdict"] == "not_applicable"
    assert by_kind["short_high_context"]["code"] == "transcripts_not_visible"


# --- the predicate itself is untouched ----------------------------------

def _function_source_sha(module_path, name):
    source = pathlib.Path(module_path).read_text()
    lines = source.splitlines(keepends=True)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = "".join(lines[node.lineno - 1:node.end_lineno])
            return hashlib.sha256(textwrap.dedent(body).encode()).hexdigest()
    raise AssertionError(f"{name} not found in {module_path}")


# The predicate as of #620 S3. D-E: `_transcripts_visible_to_request` is
# unchanged and the twelve routes that call it are untouched, so a change to
# it must be a deliberate act that also updates this literal.
_GATE_SHA = "af98055decf2a3bcbf9e576fb2ba002a4a9b87c2e72b160ae210025762621799"


def test_transcripts_visible_predicate_is_not_modified():
    module = sys.modules.get("_cctally_dashboard")
    if module is None:
        load_script()
        module = sys.modules["_cctally_dashboard"]
    assert _function_source_sha(
        inspect.getfile(module), "_transcripts_visible_to_request"
    ) == _GATE_SHA


# --- the parameter is required, so an unthreaded call fails loudly ------

def test_build_diagnosis_requires_the_gate_value_explicitly():
    """R9. The `True` default let a call site forget the gate and silently
    read transcripts; the route was exactly that call site. A missing keyword
    must now raise rather than default to visible."""
    sources = _sources()
    for name in ("build_diagnosis", "build_provider_diagnosis"):
        parameter = inspect.signature(
            getattr(sources, name)).parameters["transcripts_visible"]
        assert parameter.default is inspect.Parameter.empty, name
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name


def test_the_route_threads_the_gate_rather_than_defaulting():
    """The call site, read as source. The behavioural tests above would also
    pass if the route hard-coded `True` on a build that had no gate at all, so
    the argument it actually passes is stated here."""
    module = sys.modules.get("_cctally_dashboard")
    if module is None:
        load_script()
        module = sys.modules["_cctally_dashboard"]
    body = _handler_method_source(inspect.getfile(module),
                                  "_handle_get_diagnosis")
    assert "transcripts_visible=" in body
    assert "_transcripts_visible_to_request()" in body


def _handler_method_source(module_path, name):
    source = pathlib.Path(module_path).read_text()
    lines = source.splitlines(keepends=True)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return "".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name} not found in {module_path}")


# --- the retryable state ------------------------------------------------

def test_generation_incoherent_is_503_and_the_body_names_the_cause(
        claude_server, monkeypatch):
    """A component that moves twice while it is read is published as a
    retryable failure rather than as a digest over a state that never existed
    as a whole."""
    sources = _sources()
    counter = {"n": 0}

    def _moving(component, bundle):
        counter["n"] += 1
        return f"{component}:{counter['n']}"

    monkeypatch.setattr(sources, "_probe_component", _moving)
    response = claude_server.get(f"/api/diagnosis?source=claude&window={_WINDOW}",
                                 host="127.0.0.1")
    assert response.status == 503, response.body
    assert response.json["code"] == "generation_incoherent"

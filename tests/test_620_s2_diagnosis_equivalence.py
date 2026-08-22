"""#620 S2 — the CLI and `GET /api/diagnosis` are one answer, not two.

Both surfaces serialize through `diagnosis_to_wire`, which is what turns "they
agree" from a promise into a byte comparison. Equality is defined over
`canonical_projection` rather than over terminal text against HTTP bytes: the
projection names its exclusions and fixes key ordering, so a difference in the
projection is a difference in the FACTS.

The exclusions are verified rather than trusted. `CANONICAL_EXCLUDED_PATHS` is
a constant and the projection is a function over it; a path listed but never
present, or present but never dropped, would leave the constant describing
something the projection does not do — and the byte comparison below would then
rest on a claim nobody checked.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import threading

import pytest

from conftest import load_script, redirect_paths
from test_620_s2_diagnosis_cli import _run_cli
from test_620_s2_diagnosis_route import _boot
from test_620_s2_diagnosis_sources import (
    WINDOW_END, WINDOW_START, _seed_claude, _seed_claude_blocks,
    _seed_codex, _seed_codex_windows,
)

from tests._support_http import stop


UTC = dt.timezone.utc
_WINDOW = f"{WINDOW_START.date().isoformat()}..{WINDOW_END.date().isoformat()}"


def _diag():
    return load_script()["_cctally_diagnosis"]


@pytest.fixture
def both_surfaces(tmp_path, monkeypatch):
    """One seeded store, reachable by a subprocess CLI and an in-process route.

    `redirect_paths` pins the kernel's path constants for this process and sets
    HOME; `_run_cli` hands the subprocess the same HOME plus an explicit
    `CCTALLY_DATA_DIR`, so both surfaces open the same files.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    # The display zone is part of the scope the generation id is computed over
    # and is published as `window.tz`. Pinning it here is what makes the two
    # surfaces answer the same question rather than two similar ones.
    monkeypatch.setenv("TZ", "Etc/UTC")
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    _seed_claude(ns, models=(("claude-opus-4-20250514", 50),
                             ("claude-haiku-4-20250514", 10)))
    _seed_claude_blocks(ns)
    _seed_codex(ns, rows=[("gpt-5.3-codex", 60 + i, "root-a") for i in range(40)])
    _seed_codex_windows(ns, windows=[("root-a", "codex_standard", 0, 300),
                                     ("root-a", "codex_standard", 300, 600)])
    server, thread, client = _boot(ns)
    try:
        yield tmp_path, client
    finally:
        stop(server, thread)


def _cli_payload(home, extra=()):
    run = _run_cli(["explain", "--json", "--window", _WINDOW, *extra], home=home)
    assert run.exit_code == 0, (run.stdout, run.stderr)
    return run.json


def _route_payload(client, query=""):
    response = client.get(f"/api/diagnosis?window={_WINDOW}{query}")
    assert response.status == 200, response.body
    return response.json


# --- the acceptance criterion -------------------------------------------

def test_cli_and_route_canonical_projections_are_byte_identical(both_surfaces):
    home, client = both_surfaces
    cli = _cli_payload(home)
    route = _route_payload(client)

    assert cli["results"][0]["generation"]["generationId"] == \
        route["results"][0]["generation"]["generationId"]
    projection = _diag().canonical_projection
    assert projection(cli) == projection(route)


def test_the_comparison_can_fail(both_surfaces):
    """The equality above is only evidence if inequality is reachable.

    Both halves of the comparison are re-taken across surfaces, one over a
    different window, so this fails exactly where the acceptance test would if
    the two surfaces ever answered different questions.
    """
    home, client = both_surfaces
    cli = _cli_payload(home)
    other_window = client.get("/api/diagnosis?window=2026-08-03..2026-08-09")
    assert other_window.status == 200, other_window.body
    projection = _diag().canonical_projection
    assert projection(cli) != projection(other_window.json)


def test_the_two_agree_under_source_all(both_surfaces):
    """Each provider gets its own scope, so `--source all` is the case where a
    per-provider scope could diverge between the surfaces without the
    single-provider path noticing."""
    home, client = both_surfaces
    cli = _cli_payload(home, extra=("--source", "all"))
    route = _route_payload(client, query="&source=all")
    assert [r["source"] for r in cli["results"]] == ["claude", "codex"]
    projection = _diag().canonical_projection
    assert projection(cli) == projection(route)


def test_the_two_agree_with_projects_revealed(both_surfaces):
    """Anonymization lives in the wire adapter, not on either surface. A label
    assigned at render time would differ between them, and the comparison would
    be over two different privacy decisions rather than over one report."""
    home, client = both_surfaces
    cli = _cli_payload(home, extra=("--reveal-projects",))
    route = _route_payload(client, query="&reveal_projects=1")
    projection = _diag().canonical_projection
    assert projection(cli) == projection(route)


def test_the_projection_is_not_vacuous(both_surfaces):
    """A projection that dropped everything would make any two payloads equal.

    So: the projected bytes must still carry the report's substance, and two
    reports over DIFFERENT windows must project differently.
    """
    home, client = both_surfaces
    cli = _cli_payload(home)
    projected = json.loads(_diag().canonical_projection(cli))
    assert projected["overallVerdict"]
    assert projected["results"][0]["contributors"]
    assert projected["constants"]["contributorShareFloor"] == 0.2

    other = _run_cli(
        ["explain", "--json", "--window", "2026-08-03..2026-08-09"], home=home,
    )
    assert other.exit_code == 0, other.stderr
    assert _diag().canonical_projection(cli) != \
        _diag().canonical_projection(other.json)


# --- the excluded paths, verified against a real payload ----------------

def test_every_excluded_path_is_present_and_then_dropped(both_surfaces):
    """The constant and the projection agree, checked in both directions.

    A path named in the constant but absent from the payload documents an
    exclusion that never fires; one present in the projection after being named
    would mean the projection does not implement its own constant.
    """
    home, _client = both_surfaces
    payload = _cli_payload(home)
    projected = json.loads(_diag().canonical_projection(payload))

    for path in _diag().CANONICAL_EXCLUDED_PATHS:
        before = _resolve(payload, path)
        assert before, f"{path} is excluded but never present in a real payload"
        after = _resolve(projected, path)
        assert not after, f"{path} is named as excluded but survived the projection"


def test_measured_at_is_the_reason_the_projection_exists(both_surfaces):
    """The two surfaces legitimately measure at different instants, and that
    difference must not read as a disagreement about the facts."""
    home, client = both_surfaces
    cli = _cli_payload(home)
    route = _route_payload(client)
    assert "measuredAt" in cli and "measuredAt" in route
    assert "measuredAt" not in json.loads(_diag().canonical_projection(cli))


def test_the_projection_keeps_the_contract_facts(both_surfaces):
    """Registry labels are a fact of the contract, not presentation.

    An earlier form dropped every key NAMED `label` at every depth, which also
    removed `constants.registry[].label` — and the byte comparison then held
    over a payload missing part of what it is supposed to compare.
    """
    home, _client = both_surfaces
    projected = json.loads(_diag().canonical_projection(_cli_payload(home)))
    labels = [entry["label"] for entry in projected["constants"]["registry"]]
    assert labels and all(labels)


def _resolve(payload, path):
    """Every value a `results[].contributors[].subjectLabel`-style path names."""
    nodes = [payload]
    for segment in path.split("."):
        key = segment[:-2] if segment.endswith("[]") else segment
        nodes = [node[key] for node in nodes
                 if isinstance(node, dict) and key in node]
        if segment.endswith("[]"):
            nodes = [item for node in nodes if isinstance(node, list)
                     for item in node]
    return nodes

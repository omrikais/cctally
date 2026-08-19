"""Issue #606: Daybreak Blue resolves through the current Sol rate card."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
CCTALLY = BIN / "cctally"
MODEL = "gpt-daybreak-blue-latest"


def _load(module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name, BIN / f"{module_name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


pricing = _load("_lib_pricing")
pricing_check = _load("_lib_pricing_check")


def test_daybreak_blue_resolves_to_sol_without_legacy_fallback():
    resolved, is_fallback = pricing._resolve_codex_pricing(MODEL)

    assert pricing.CODEX_MODEL_ALIASES[MODEL] == "gpt-5.6-sol"
    assert resolved is pricing.CODEX_MODEL_PRICING["gpt-5.6-sol"]
    assert is_fallback is False
    assert pricing._is_codex_fallback(MODEL) is False
    assert pricing._codex_fast_multiplier(MODEL) == 2.0


@pytest.mark.parametrize(
    ("speed", "expected_cost"),
    [("standard", 0.62), ("fast", 1.24)],
)
def test_daybreak_blue_prices_standard_and_fast_without_warning(
    speed, expected_cost, capsys,
):
    pricing._unknown_codex_model_warnings.discard(MODEL)

    cost = pricing._calculate_codex_entry_cost(
        MODEL,
        100_000,
        40_000,
        10_000,
        2_000,
        speed=speed,
    )

    assert cost == pytest.approx(expected_cost)
    assert "unknown model" not in capsys.readouterr().err


def test_pricing_coverage_accepts_daybreak_but_keeps_unknown_actionable():
    gaps = pricing_check.classify_coverage(
        [
            ("codex", MODEL, 26, 123_456),
            ("codex", "gpt-hypothetical-99", 1, 1_000),
        ],
        lambda _model: None,
        pricing._is_codex_fallback,
    )

    assert [(gap.model, gap.kind) for gap in gaps] == [
        ("gpt-hypothetical-99", "fallback"),
    ]


def _write_rollout(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-08-16T20:00:00Z",
            "type": "session_meta",
            "payload": {"id": "daybreak-retained", "model": MODEL},
        },
        {
            "timestamp": "2026-08-16T20:01:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 100_000,
                        "cached_input_tokens": 40_000,
                        "output_tokens": 10_000,
                        "reasoning_output_tokens": 2_000,
                        "total_tokens": 110_000,
                    },
                    "total_token_usage": {"total_tokens": 110_000},
                },
            },
        },
    ]
    path.write_text("".join(
        json.dumps(row, separators=(",", ":")) + "\n" for row in records
    ))


def _run_daily(tmp_path: pathlib.Path, speed: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / f"home-{speed}"
    data = tmp_path / f"data-{speed}"
    codex_home = tmp_path / "codex"
    home.mkdir()
    _write_rollout(codex_home / "sessions" / "2026" / "08" / "16" / "rollout.jsonl")
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "CCTALLY_DATA_DIR": str(data),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
        "CCTALLY_AS_OF": "2026-08-17T00:00:00Z",
        "TZ": "Etc/UTC",
        "NO_COLOR": "1",
    })
    return subprocess.run(
        [sys.executable, str(CCTALLY), "codex-daily", "--json", "--speed", speed],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_retained_daybreak_rollout_prices_through_real_cli(tmp_path):
    standard = _run_daily(tmp_path, "standard")
    fast = _run_daily(tmp_path, "fast")

    assert standard.returncode == fast.returncode == 0
    assert "unknown model" not in standard.stderr + fast.stderr
    standard_doc = json.loads(standard.stdout)
    fast_doc = json.loads(fast.stdout)
    model = standard_doc["daily"][0]["models"][MODEL]
    assert model["isFallback"] is False
    assert standard_doc["totals"]["costUSD"] == pytest.approx(0.62)
    assert fast_doc["totals"]["costUSD"] == pytest.approx(1.24)

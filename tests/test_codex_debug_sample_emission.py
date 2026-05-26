"""Issue #92 — codex-* `--debug` / `--debug-samples` diagnostic-sample emission.

Codex parity for the #89 Claude-side "Pricing Mismatch Debug Report".
Codex JSONL carries NO recorded ``costUSD`` to diff against, so the report
is the codex variant chosen in issue #92's design Q&A (option 2): a totals
header plus a "Sample Top Entries" block of the N highest computed-cost
entries (``Recorded cost: (none)`` per sample).

Parallel to ``tests/test_debug_sample_emission.py``.

Issue: #92  (parent: #89)
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"


def _load_cctally_module():
    """Import the ``cctally`` script as a module (no .py extension)."""
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("cctally", str(CCTALLY))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _make_codex_entry(
    mod,
    *,
    model="gpt-5-codex",
    input_tokens=1000,
    cached_input_tokens=200,
    output_tokens=500,
    reasoning_output_tokens=100,
    timestamp=None,
    source_path="/tmp/rollout-synth.jsonl",
    session_id="sess-synth",
):
    return mod.CodexEntry(
        timestamp=timestamp or dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        session_id=session_id,
        model=model,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        total_tokens=input_tokens + output_tokens,
        source_path=source_path,
    )


# Issue #92 §compute — _compute_codex_cost_stats unit tests
class TestComputeCodexCostStats:
    def test_empty_entries(self):
        mod = _load_cctally_module()
        stats = mod._compute_codex_cost_stats([])
        assert stats.total_entries == 0
        assert stats.total_cost == 0.0
        assert stats.model_counts == {}
        assert stats.samples == []
        assert stats.fallback_models == set()

    def test_single_entry_totals_and_sample(self):
        mod = _load_cctally_module()
        e = _make_codex_entry(mod)
        expected = mod._calculate_codex_entry_cost(
            e.model, e.input_tokens, e.cached_input_tokens,
            e.output_tokens, e.reasoning_output_tokens,
        )
        stats = mod._compute_codex_cost_stats([e])
        assert stats.total_entries == 1
        assert abs(stats.total_cost - expected) < 1e-12
        assert stats.model_counts == {"gpt-5-codex": 1}
        assert len(stats.samples) == 1
        s = stats.samples[0]
        assert s.model == "gpt-5-codex"
        assert abs(s.calculated_cost - expected) < 1e-12
        assert s.file == "rollout-synth.jsonl"
        assert s.is_fallback is False
        # Tokens payload mirrors the CodexEntry fields (LiteLLM convention).
        assert s.usage == {
            "input_tokens": 1000,
            "cached_input_tokens": 200,
            "output_tokens": 500,
            "reasoning_output_tokens": 100,
            "total_tokens": 1500,
        }

    def test_total_cost_sums_across_entries(self):
        mod = _load_cctally_module()
        entries = [
            _make_codex_entry(mod, input_tokens=1000, output_tokens=500),
            _make_codex_entry(mod, input_tokens=2000, output_tokens=900),
            _make_codex_entry(mod, input_tokens=300, output_tokens=100),
        ]
        expected = sum(
            mod._calculate_codex_entry_cost(
                e.model, e.input_tokens, e.cached_input_tokens,
                e.output_tokens, e.reasoning_output_tokens,
            )
            for e in entries
        )
        stats = mod._compute_codex_cost_stats(entries)
        assert stats.total_entries == 3
        assert abs(stats.total_cost - expected) < 1e-12
        assert stats.model_counts == {"gpt-5-codex": 3}

    def test_per_model_counts(self):
        mod = _load_cctally_module()
        entries = [
            _make_codex_entry(mod, model="gpt-5-codex"),
            _make_codex_entry(mod, model="gpt-5-codex"),
            _make_codex_entry(mod, model="gpt-5"),
        ]
        stats = mod._compute_codex_cost_stats(entries)
        assert stats.model_counts == {"gpt-5-codex": 2, "gpt-5": 1}

    def test_samples_sorted_descending_by_cost(self):
        mod = _load_cctally_module()
        # Three entries with strictly increasing token counts → increasing cost.
        small = _make_codex_entry(
            mod, input_tokens=100, output_tokens=50, cached_input_tokens=0,
            reasoning_output_tokens=0, source_path="/p/small.jsonl",
        )
        mid = _make_codex_entry(
            mod, input_tokens=1000, output_tokens=500, cached_input_tokens=0,
            reasoning_output_tokens=0, source_path="/p/mid.jsonl",
        )
        big = _make_codex_entry(
            mod, input_tokens=5000, output_tokens=2500, cached_input_tokens=0,
            reasoning_output_tokens=0, source_path="/p/big.jsonl",
        )
        # Feed in NON-sorted order; stats must sort desc by computed cost.
        stats = mod._compute_codex_cost_stats([mid, small, big])
        files = [s.file for s in stats.samples]
        assert files == ["big.jsonl", "mid.jsonl", "small.jsonl"]
        costs = [s.calculated_cost for s in stats.samples]
        assert costs == sorted(costs, reverse=True)

    def test_unknown_model_marked_fallback(self):
        mod = _load_cctally_module()
        e = _make_codex_entry(mod, model="totally-unknown-xyz")
        stats = mod._compute_codex_cost_stats([e])
        assert stats.total_entries == 1
        assert stats.model_counts == {"totally-unknown-xyz": 1}
        assert "totally-unknown-xyz" in stats.fallback_models
        assert stats.samples[0].is_fallback is True

    def test_known_model_not_fallback(self):
        mod = _load_cctally_module()
        e = _make_codex_entry(mod, model="gpt-5-codex")
        stats = mod._compute_codex_cost_stats([e])
        assert stats.fallback_models == set()
        assert stats.samples[0].is_fallback is False

    def test_total_cost_honors_fast_speed(self):
        """Issue #86 Session D (F3): the --debug walker scales by the per-model
        fast multiplier just like the report aggregators. gpt-5.5 → ×2.5."""
        mod = _load_cctally_module()
        entries = [_make_codex_entry(mod, model="gpt-5.5",
                                     input_tokens=1_000, cached_input_tokens=0,
                                     output_tokens=500, reasoning_output_tokens=0)]
        std = mod._compute_codex_cost_stats(entries, speed="standard").total_cost
        fast = mod._compute_codex_cost_stats(entries, speed="fast").total_cost
        assert std > 0
        assert abs(fast - std * 2.5) < 1e-9

    def test_default_speed_is_standard(self):
        """No speed arg == speed='standard' (preserves every existing caller)."""
        mod = _load_cctally_module()
        entries = [_make_codex_entry(mod, model="gpt-5.5")]
        assert (mod._compute_codex_cost_stats(entries).total_cost
                == mod._compute_codex_cost_stats(entries, speed="standard").total_cost)

    def test_samples_scale_with_fast_speed(self):
        """The per-entry sample costs (not just the total) scale under fast."""
        mod = _load_cctally_module()
        e = _make_codex_entry(mod, model="gpt-5.5",
                              input_tokens=1_000, cached_input_tokens=0,
                              output_tokens=500, reasoning_output_tokens=0)
        std_sample = mod._compute_codex_cost_stats([e], speed="standard").samples[0]
        fast_sample = mod._compute_codex_cost_stats([e], speed="fast").samples[0]
        assert std_sample.calculated_cost > 0
        assert abs(fast_sample.calculated_cost
                   - std_sample.calculated_cost * 2.5) < 1e-9


# Issue #92 §render — _render_codex_cost_report shape tests
class TestRenderCodexCostReport:
    def _stats(self, mod, **kwargs):
        s = mod._CodexCostStats()
        for k, v in kwargs.items():
            setattr(s, k, v)
        return s

    def _sample(self, mod, **kwargs):
        base = dict(
            file="rollout-x.jsonl",
            timestamp="2026-05-01T10:00:00+00:00",
            model="gpt-5-codex",
            calculated_cost=0.5,
            usage={"input_tokens": 1000, "cached_input_tokens": 0,
                   "output_tokens": 500, "reasoning_output_tokens": 0,
                   "total_tokens": 1500},
            is_fallback=False,
        )
        base.update(kwargs)
        return mod._CodexCostSample(**base)

    def test_empty_returns_sentinel(self):
        mod = _load_cctally_module()
        s = self._stats(mod, total_entries=0)
        out = mod._render_codex_cost_report(s, 5)
        assert out == ["No Codex usage data found to analyze."]

    def test_header_and_totals(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, command_label="codex-daily", total_entries=1234,
            total_cost=12.345678, model_counts={"gpt-5-codex": 1234},
        )
        out = mod._render_codex_cost_report(s, 0)
        joined = "\n".join(out)
        assert "=== Codex Pricing Debug Report ===" in joined
        assert "Command: cctally codex-daily" in joined
        assert "Total entries processed: 1,234" in joined
        assert "Models seen: gpt-5-codex (1,234)" in joined
        assert "Total computed cost: $12.345678" in joined

    def test_command_label_omitted_when_unset(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=1, total_cost=0.5,
            model_counts={"gpt-5-codex": 1},
        )
        out = mod._render_codex_cost_report(s, 0)
        assert not any(line.startswith("Command:") for line in out)

    def test_models_seen_sorted_desc_by_count_then_name(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=12, total_cost=1.0,
            model_counts={"gpt-5-codex": 5, "gpt-5": 5, "o3": 2},
        )
        out = mod._render_codex_cost_report(s, 0)
        line = next(l for l in out if l.startswith("Models seen:"))
        # count desc; ties broken by model name asc → gpt-5 before gpt-5-codex.
        assert line == "Models seen: gpt-5 (5), gpt-5-codex (5), o3 (2)"

    def test_fallback_annotation_in_models_seen(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=4, total_cost=1.0,
            model_counts={"gpt-5-codex": 1, "weird-model": 3},
            fallback_models={"weird-model"},
        )
        out = mod._render_codex_cost_report(s, 0)
        line = next(l for l in out if l.startswith("Models seen:"))
        assert "weird-model (3, fallback→gpt-5)" in line
        assert "gpt-5-codex (1)" in line

    def test_sample_block_full_shape(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=1, total_cost=0.842310,
            model_counts={"gpt-5-codex": 1},
        )
        s.samples = [self._sample(
            mod, file="rollout-abc.jsonl",
            timestamp="2026-05-01T10:00:00+00:00", model="gpt-5-codex",
            calculated_cost=0.842310,
            usage={"input_tokens": 700, "cached_input_tokens": 100,
                   "output_tokens": 300, "reasoning_output_tokens": 0,
                   "total_tokens": 1000},
        )]
        out = mod._render_codex_cost_report(s, 5)
        joined = "\n".join(out)
        assert "=== Sample Top Entries (first 5) ===" in joined
        assert "File: rollout-abc.jsonl" in joined
        assert "Timestamp: 2026-05-01T10:00:00+00:00" in joined
        assert "Model: gpt-5-codex" in joined
        assert "Recorded cost: (none)" in joined
        assert "Calculated cost: $0.842310" in joined
        assert '"input_tokens": 700' in joined
        assert joined.rstrip().endswith("---")

    def test_sample_limit_zero_omits_block(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=1, total_cost=0.5,
            model_counts={"gpt-5-codex": 1},
        )
        s.samples = [self._sample(mod)]
        out = mod._render_codex_cost_report(s, 0)
        joined = "\n".join(out)
        assert "=== Sample Top Entries" not in joined
        # Totals header still renders.
        assert "=== Codex Pricing Debug Report ===" in joined

    def test_sample_limit_caps_count(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=5, total_cost=5.0,
            model_counts={"gpt-5-codex": 5},
        )
        s.samples = [
            self._sample(mod, file=f"f{i}.jsonl", calculated_cost=5.0 - i)
            for i in range(5)
        ]
        out = mod._render_codex_cost_report(s, 2)
        assert "=== Sample Top Entries (first 2) ===" in "\n".join(out)
        file_lines = [l for l in out if l.startswith("File: ")]
        assert len(file_lines) == 2

    def test_sample_header_uses_requested_limit_even_when_fewer(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=3, total_cost=3.0,
            model_counts={"gpt-5-codex": 3},
        )
        s.samples = [self._sample(mod, file=f"f{i}.jsonl") for i in range(3)]
        out = mod._render_codex_cost_report(s, 5)
        assert "=== Sample Top Entries (first 5) ===" in "\n".join(out)
        file_lines = [l for l in out if l.startswith("File: ")]
        assert len(file_lines) == 3

    def test_fallback_marker_in_sample_model_line(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=1, total_cost=0.5,
            model_counts={"weird-model": 1}, fallback_models={"weird-model"},
        )
        s.samples = [self._sample(mod, model="weird-model", is_fallback=True)]
        out = mod._render_codex_cost_report(s, 5)
        assert any(
            line == "Model: weird-model (fallback→gpt-5)" for line in out
        ), out

    def test_dollar_6dp_formatting(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=1, total_cost=0.123456789,
            model_counts={"gpt-5-codex": 1},
        )
        s.samples = [self._sample(mod, calculated_cost=0.234567891)]
        out = mod._render_codex_cost_report(s, 5)
        joined = "\n".join(out)
        assert "Total computed cost: $0.123457" in joined
        assert "Calculated cost: $0.234568" in joined


# Issue #92 §emit — _emit_codex_debug_samples_if_set guard tests
class TestEmitCodexDebugSamplesGuard:
    def test_one_time_per_process(self, monkeypatch):
        """Shares the _DEBUG_REPORT_EMITTED guard: two calls emit once."""
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", False)
        e = _make_codex_entry(mod)
        ns = argparse.Namespace(debug=True, debug_samples=5)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            mod._emit_codex_debug_samples_if_set(ns, [e], command_label="codex-daily")
            mod._emit_codex_debug_samples_if_set(ns, [e], command_label="codex-daily")
        assert buf.getvalue().count("=== Codex Pricing Debug Report ===") == 1

    def test_no_emission_without_flag(self, monkeypatch):
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", False)
        ns = argparse.Namespace(debug=False, debug_samples=5)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            mod._emit_codex_debug_samples_if_set(
                ns, [_make_codex_entry(mod)], command_label="codex-daily")
        assert buf.getvalue() == ""

    def test_debug_samples_zero_omits_sample_block(self, monkeypatch):
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", False)
        ns = argparse.Namespace(debug=True, debug_samples=0)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            mod._emit_codex_debug_samples_if_set(
                ns, [_make_codex_entry(mod)], command_label="codex-daily")
        text = buf.getvalue()
        assert "=== Codex Pricing Debug Report ===" in text
        assert "=== Sample Top Entries" not in text

    def test_shares_guard_with_claude_path(self, monkeypatch):
        """One process emits a single debug report regardless of family:
        a prior Claude emission suppresses the codex one."""
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", True)
        ns = argparse.Namespace(debug=True, debug_samples=5)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            mod._emit_codex_debug_samples_if_set(
                ns, [_make_codex_entry(mod)], command_label="codex-daily")
        assert buf.getvalue() == ""


# Issue #92 §e2e — staged ~/.codex/sessions JSONL, report on stderr
def _stage_codex_session(home: Path, *, model="gpt-5-codex"):
    """Write a minimal codex rollout JSONL with 3 strictly-advancing
    token_count events (so the dedup iterator yields all three). Returns
    nothing; entries are dated 2026-05-01 (inside the default 2020→now range).
    """
    sess_dir = home / ".codex" / "sessions" / "2026" / "05" / "01"
    sess_dir.mkdir(parents=True, exist_ok=True)
    import json as _json

    def _evt(ts, *, inp, cached, out, reason, cum):
        return _json.dumps({
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": inp,
                        "cached_input_tokens": cached,
                        "output_tokens": out,
                        "reasoning_output_tokens": reason,
                        "total_tokens": inp + out,
                    },
                    "total_token_usage": {"total_tokens": cum},
                },
            },
        })

    lines = [
        _json.dumps({"timestamp": "2026-05-01T10:00:00.000Z",
                     "type": "session_meta", "payload": {"id": "sess-e2e"}}),
        _json.dumps({"timestamp": "2026-05-01T10:00:01.000Z",
                     "type": "turn_context", "payload": {"model": model}}),
        # mid, big, small — fed unsorted; report sorts desc by cost.
        _evt("2026-05-01T10:01:00.000Z", inp=700, cached=100, out=300, reason=0, cum=1000),
        _evt("2026-05-01T10:02:00.000Z", inp=3000, cached=500, out=1500, reason=200, cum=5500),
        _evt("2026-05-01T10:03:00.000Z", inp=200, cached=0, out=100, reason=0, cum=5800),
    ]
    (sess_dir / "rollout-e2e.jsonl").write_text("\n".join(lines) + "\n")


CODEX_CMDS = ["codex-daily", "codex-monthly", "codex-weekly", "codex-session"]


@pytest.mark.parametrize("cmd", CODEX_CMDS)
def test_e2e_debug_report_on_stderr(cmd, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _stage_codex_session(home)
    env = {"HOME": str(home), "PATH": __import__("os").environ.get("PATH", ""),
           "TZ": "Etc/UTC", "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
    r = subprocess.run(
        [sys.executable, str(CCTALLY), cmd, "--debug"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert "=== Codex Pricing Debug Report ===" in r.stderr, r.stderr
    assert f"Command: cctally {cmd}" in r.stderr, r.stderr
    assert "Total entries processed: 3" in r.stderr, r.stderr
    assert "Models seen: gpt-5-codex (3)" in r.stderr, r.stderr
    assert "=== Sample Top Entries (first 5) ===" in r.stderr, r.stderr
    assert "Recorded cost: (none)" in r.stderr, r.stderr
    # 3 sample blocks (3 entries, limit 5).
    assert r.stderr.count("File: rollout-e2e.jsonl") == 3, r.stderr


def test_e2e_debug_samples_zero_suppresses_block(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _stage_codex_session(home)
    env = {"HOME": str(home), "PATH": __import__("os").environ.get("PATH", ""),
           "TZ": "Etc/UTC", "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
    r = subprocess.run(
        [sys.executable, str(CCTALLY), "codex-daily", "--debug", "--debug-samples", "0"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert "=== Codex Pricing Debug Report ===" in r.stderr, r.stderr
    assert "=== Sample Top Entries" not in r.stderr, r.stderr


def test_e2e_no_debug_flag_no_report(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _stage_codex_session(home)
    env = {"HOME": str(home), "PATH": __import__("os").environ.get("PATH", ""),
           "TZ": "Etc/UTC", "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
    r = subprocess.run(
        [sys.executable, str(CCTALLY), "codex-daily"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert "Codex Pricing Debug Report" not in r.stderr, r.stderr


def _debug_total_cost(stderr: str) -> float:
    """Parse the 'Total computed cost: $X.XXXXXX' line from a debug report."""
    import re
    m = re.search(r"Total computed cost: \$([\d.]+)", stderr)
    assert m, f"no 'Total computed cost' line in:\n{stderr}"
    return float(m.group(1))


def test_e2e_debug_total_cost_honors_fast_speed(tmp_path):
    """Issue #86 Session D (F3): `codex-daily --debug --speed fast` reports a
    larger 'Total computed cost' than `--speed standard`, end-to-end."""
    home = tmp_path / "home"
    home.mkdir()
    _stage_codex_session(home, model="gpt-5.5")  # gpt-5.5 → fast ×2.5
    env = {"HOME": str(home), "PATH": __import__("os").environ.get("PATH", ""),
           "TZ": "Etc/UTC", "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}

    def _run(speed):
        r = subprocess.run(
            [sys.executable, str(CCTALLY), "codex-daily", "--debug",
             "--speed", speed],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert r.returncode == 0, (r.returncode, r.stderr)
        return _debug_total_cost(r.stderr)

    std = _run("standard")
    fast = _run("fast")
    assert std > 0
    assert abs(fast - std * 2.5) < 1e-6

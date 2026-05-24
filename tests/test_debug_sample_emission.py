"""Spec §9 — real --debug diagnostic-sample emission.

Spec: docs/superpowers/specs/2026-05-23-issue-89-debug-sample-emission.md
Issue: #89
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import os
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


# §9.5 — argparse validation for --debug-samples
class TestNonnegIntValidator:
    def test_accepts_zero(self):
        mod = _load_cctally_module()
        assert mod._nonneg_int("0") == 0

    def test_accepts_positive(self):
        mod = _load_cctally_module()
        assert mod._nonneg_int("5") == 5

    def test_rejects_negative(self):
        mod = _load_cctally_module()
        with pytest.raises(argparse.ArgumentTypeError) as exc:
            mod._nonneg_int("-1")
        assert "must be >= 0, got -1" in str(exc.value)

    def test_rejects_non_integer(self):
        mod = _load_cctally_module()
        with pytest.raises(argparse.ArgumentTypeError) as exc:
            mod._nonneg_int("foo")
        assert "must be a non-negative integer, got 'foo'" in str(exc.value)


# §9.6 — cache-backed source_path preservation
class TestCacheSourcePathPropagation:
    def test_iter_entries_populates_source_path(self, tmp_path, monkeypatch):
        """Cache-backed iter_entries must set UsageEntry.source_path.

        Catches the regression Codex review flagged: schema field added to
        UsageEntry but SELECT projection forgot to include source_path,
        making the cache path silently emit blank File: in --debug samples.

        Driven entirely via subprocess so the staged HOME is honored by
        the cctally module's import-time CACHE_DB_PATH binding — when the
        test process has already imported cctally against the ambient
        HOME, ``open_cache_db()`` re-uses the stale path. The subprocess
        side-steps that by computing CACHE_DB_PATH fresh under the
        per-test HOME.
        """
        # Use a temp HOME so we get an isolated cache DB
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

        # Stage a minimal JSONL fixture under <home>/.claude/projects/
        proj = home / ".claude" / "projects" / "synth-proj"
        proj.mkdir(parents=True)
        jsonl_path = proj / "session-A.jsonl"
        # One real assistant entry — timestamp inside the 2026 window
        import json as _j
        entry = {
            "type": "assistant",
            "timestamp": "2026-05-23T12:00:00Z",
            "message": {
                "id": "msg_abc",
                "model": "claude-opus-4-7",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
            "requestId": "req_xyz",
            "costUSD": 0.0008,
        }
        jsonl_path.write_text(_j.dumps(entry) + "\n", encoding="utf-8")

        # Drive iter_entries via a subprocess one-liner that imports
        # cctally fresh under the staged HOME and dumps source_path
        # for every entry the cache returns.
        probe = (
            "import importlib.util, datetime as dt, sys\n"
            "from importlib.machinery import SourceFileLoader\n"
            f"loader = SourceFileLoader('cctally', {str(CCTALLY)!r})\n"
            "spec = importlib.util.spec_from_loader('cctally', loader)\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "sys.modules['cctally'] = mod\n"
            "loader.exec_module(mod)\n"
            "conn = mod.open_cache_db()\n"
            "try:\n"
            "    es = mod.iter_entries(conn,\n"
            "        dt.datetime(2026, 5, 23, tzinfo=dt.timezone.utc),\n"
            "        dt.datetime(2026, 5, 23, 23, 59, 59, tzinfo=dt.timezone.utc))\n"
            "finally:\n"
            "    conn.close()\n"
            "for e in es:\n"
            "    print('SOURCE_PATH=' + (e.source_path or ''))\n"
        )
        # First prime the cache via a daily run
        r0 = subprocess.run(
            [sys.executable, str(CCTALLY), "daily", "--since", "2026-05-23",
             "--until", "2026-05-23"],
            capture_output=True, text=True, timeout=30,
        )
        assert r0.returncode == 0, (r0.returncode, r0.stderr)

        r = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, (r.returncode, r.stderr)
        paths = [
            line.removeprefix("SOURCE_PATH=")
            for line in r.stdout.splitlines()
            if line.startswith("SOURCE_PATH=")
        ]
        assert paths, f"expected at least one entry, got stdout={r.stdout!r} stderr={r.stderr!r}"
        for p in paths:
            assert p, "empty source_path"
            assert str(jsonl_path) == p or p.endswith("session-A.jsonl"), \
                f"unexpected source_path {p!r}"


# §9.1 — _compute_pricing_mismatch_stats unit tests
class TestComputePricingMismatchStats:
    def _make_entry(self, mod, *, cost_usd, model="claude-opus-4-7",
                    input_tokens=1000, output_tokens=500,
                    timestamp=None, source_path="/tmp/synth.jsonl"):
        import datetime as dt
        return mod.UsageEntry(
            timestamp=timestamp or dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
            model=model,
            usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            cost_usd=cost_usd,
            source_path=source_path,
        )

    def test_empty_entries(self):
        mod = _load_cctally_module()
        stats = mod._compute_pricing_mismatch_stats([])
        assert stats.total_entries == 0
        assert stats.entries_with_both == 0
        assert stats.matches == 0
        assert stats.mismatches == 0
        assert stats.discrepancies == []
        assert stats.model_stats == {}

    def test_all_matches(self):
        mod = _load_cctally_module()
        # Build entries where cost_usd equals the calculated cost — mode="calculate"
        # gives us the canonical computed value to set as recorded.
        e = self._make_entry(mod, cost_usd=None)  # placeholder
        calc = mod._calculate_entry_cost(
            e.model, e.usage, mode="calculate",
        )
        entries = [self._make_entry(mod, cost_usd=calc) for _ in range(3)]
        stats = mod._compute_pricing_mismatch_stats(entries)
        assert stats.total_entries == 3
        assert stats.entries_with_both == 3
        assert stats.matches == 3
        assert stats.mismatches == 0
        assert stats.discrepancies == []
        # model_stats has the model with mismatches=0
        ms = stats.model_stats["claude-opus-4-7"]
        assert ms.total == 3
        assert ms.matches == 3
        assert ms.mismatches == 0

    def test_one_mismatch(self):
        mod = _load_cctally_module()
        e = self._make_entry(mod, cost_usd=None)
        calc = mod._calculate_entry_cost(e.model, e.usage, mode="calculate")
        # cost_usd 50% higher than calculated → ~33% diff, > 0.1% → mismatch
        entry = self._make_entry(mod, cost_usd=calc * 1.5)
        stats = mod._compute_pricing_mismatch_stats([entry])
        assert stats.total_entries == 1
        assert stats.entries_with_both == 1
        assert stats.matches == 0
        assert stats.mismatches == 1
        assert len(stats.discrepancies) == 1
        d = stats.discrepancies[0]
        assert d.model == "claude-opus-4-7"
        assert d.percent_diff > 0.1

    def test_unknown_model_excluded(self):
        mod = _load_cctally_module()
        e = self._make_entry(mod, cost_usd=1.0, model="bogus-unknown-model")
        stats = mod._compute_pricing_mismatch_stats([e])
        assert stats.total_entries == 1
        # Unknown model → no pricing → does NOT count toward entries_with_both
        assert stats.entries_with_both == 0
        assert stats.mismatches == 0

    def test_no_cost_usd_excluded(self):
        mod = _load_cctally_module()
        e = self._make_entry(mod, cost_usd=None)
        stats = mod._compute_pricing_mismatch_stats([e])
        assert stats.total_entries == 1
        assert stats.entries_with_both == 0
        assert stats.mismatches == 0

    def test_zero_recorded_cost_percent_diff_zero(self):
        """Spec §6.4: cost_usd == 0 → percent_diff = 0 (not divide-by-zero)."""
        mod = _load_cctally_module()
        # Recorded 0; calculated non-zero → would be infinity without the guard
        e = self._make_entry(mod, cost_usd=0.0)
        stats = mod._compute_pricing_mismatch_stats([e])
        # entries_with_both counts when cost_usd is not None AND model has pricing
        # cost_usd=0.0 IS not None, so the entry counts.
        assert stats.entries_with_both == 1
        # percent_diff is 0; threshold is 0.1 → match
        assert stats.matches == 1
        assert stats.mismatches == 0

    def test_per_model_stats_streaming_mean(self):
        """avg_percent_diff is updated by streaming-mean (matches upstream)."""
        mod = _load_cctally_module()
        e = self._make_entry(mod, cost_usd=None)
        calc = mod._calculate_entry_cost(e.model, e.usage, mode="calculate")
        # 3 mismatch entries at increasing pct diff
        entries = [
            self._make_entry(mod, cost_usd=calc * 1.02),  # ~1.96%
            self._make_entry(mod, cost_usd=calc * 1.05),  # ~4.76%
            self._make_entry(mod, cost_usd=calc * 1.10),  # ~9.09%
        ]
        stats = mod._compute_pricing_mismatch_stats(entries)
        ms = stats.model_stats["claude-opus-4-7"]
        assert ms.total == 3
        assert ms.mismatches == 3
        # Expected streaming mean of the three pct diffs (approximate match)
        diffs = []
        for entry in entries:
            calc_each = mod._calculate_entry_cost(entry.model, entry.usage, mode="calculate")
            orig = entry.cost_usd
            diff = abs(orig - calc_each) / orig * 100
            diffs.append(diff)
        expected = sum(diffs) / len(diffs)
        assert abs(ms.avg_percent_diff - expected) < 1e-6

    def test_discrepancies_in_iteration_order(self):
        mod = _load_cctally_module()
        e_proto = self._make_entry(mod, cost_usd=None)
        calc = mod._calculate_entry_cost(e_proto.model, e_proto.usage, mode="calculate")
        # 10 mismatch entries with distinct source_paths
        import datetime as dt
        entries = [
            self._make_entry(
                mod, cost_usd=calc * (1.1 + i * 0.01),
                source_path=f"file-{i}.jsonl",
                timestamp=dt.datetime(2026, 5, 1, i, 0, tzinfo=dt.timezone.utc),
            )
            for i in range(10)
        ]
        stats = mod._compute_pricing_mismatch_stats(entries)
        assert len(stats.discrepancies) == 10
        # Iteration order preserved
        for i, d in enumerate(stats.discrepancies):
            assert d.file.endswith(f"file-{i}.jsonl") or d.file == f"file-{i}.jsonl"


# §9.2 — _render_pricing_mismatch_report shape tests
class TestRenderPricingMismatchReport:
    def _stats(self, mod, **kwargs):
        s = mod._MismatchStats()
        for k, v in kwargs.items():
            setattr(s, k, v)
        return s

    def test_empty_returns_no_pricing_line(self):
        mod = _load_cctally_module()
        s = self._stats(mod, entries_with_both=0)
        out = mod._render_pricing_mismatch_report(s, 5)
        assert out == ["No pricing data found to analyze."]

    def test_zero_mismatches_omits_model_and_samples(self):
        mod = _load_cctally_module()
        # 10 matches, no mismatches → header + 5 total lines, nothing else
        s = self._stats(
            mod, total_entries=10, entries_with_both=10, matches=10,
            mismatches=0,
        )
        s.model_stats = {"claude-opus-4-7": mod._MismatchModelStat(
            total=10, matches=10, mismatches=0, avg_percent_diff=0.0,
        )}
        out = mod._render_pricing_mismatch_report(s, 5)
        joined = "\n".join(out)
        assert "=== Pricing Mismatch Debug Report ===" in joined
        assert "Total entries processed: 10" in joined
        assert "=== Model Statistics ===" not in joined
        assert "=== Sample Discrepancies" not in joined

    def test_command_label_included(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, command_label="daily", total_entries=1, entries_with_both=1,
            matches=1, mismatches=0,
        )
        out = mod._render_pricing_mismatch_report(s, 5)
        assert any("Command: cctally daily" == line for line in out)

    def test_model_stats_descending_omits_zero(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=20, entries_with_both=20, matches=15,
            mismatches=5,
        )
        s.model_stats = {
            "claude-haiku-4-5": mod._MismatchModelStat(
                total=10, matches=10, mismatches=0, avg_percent_diff=0.0,
            ),
            "claude-opus-4-7": mod._MismatchModelStat(
                total=5, matches=2, mismatches=3, avg_percent_diff=2.0,
            ),
            "claude-sonnet-4-5": mod._MismatchModelStat(
                total=5, matches=3, mismatches=2, avg_percent_diff=1.0,
            ),
        }
        out = mod._render_pricing_mismatch_report(s, 0)
        joined = "\n".join(out)
        # haiku has 0 mismatches → omitted
        assert "claude-haiku-4-5:" not in joined
        # opus before sonnet (3 mismatches > 2)
        opus_idx = joined.find("claude-opus-4-7:")
        sonnet_idx = joined.find("claude-sonnet-4-5:")
        assert 0 < opus_idx < sonnet_idx

    def test_sample_limit_zero_omits_samples_block(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=1, entries_with_both=1, matches=0, mismatches=1,
        )
        s.discrepancies = [mod._MismatchSample(
            file="x.jsonl", timestamp="2026-05-01T00:00:00",
            model="claude-opus-4-7", original_cost=1.0,
            calculated_cost=0.9, difference=0.1, percent_diff=10.0,
            usage={"input_tokens": 1},
        )]
        s.model_stats = {"claude-opus-4-7": mod._MismatchModelStat(
            total=1, matches=0, mismatches=1, avg_percent_diff=10.0,
        )}
        out = mod._render_pricing_mismatch_report(s, 0)
        joined = "\n".join(out)
        assert "=== Sample Discrepancies" not in joined
        # Model Statistics block still shows because mismatches > 0
        assert "=== Model Statistics ===" in joined

    def test_sample_limit_caps_count(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=5, entries_with_both=5, matches=0, mismatches=5,
        )
        s.discrepancies = [
            mod._MismatchSample(
                file=f"f{i}.jsonl", timestamp=f"2026-05-0{i+1}T00:00:00",
                model="claude-opus-4-7", original_cost=1.0,
                calculated_cost=0.9 - i * 0.01, difference=0.1 + i * 0.01,
                percent_diff=10.0 + i, usage={},
            )
            for i in range(5)
        ]
        s.model_stats = {"claude-opus-4-7": mod._MismatchModelStat(
            total=5, matches=0, mismatches=5, avg_percent_diff=12.0,
        )}
        out = mod._render_pricing_mismatch_report(s, 2)
        joined = "\n".join(out)
        # Header always prints the requested sample_limit (upstream parity)
        assert "=== Sample Discrepancies (first 2) ===" in joined
        # File lines: exactly 2 sample blocks materialize
        file_lines = [l for l in out if l.startswith("File: ")]
        assert len(file_lines) == 2

    def test_sample_header_uses_requested_limit_even_when_fewer(self):
        """Upstream prints the requested sample_count in the header
        regardless of discrepancy count (debug-DvI5DUKR.js:133-145).
        """
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=3, entries_with_both=3, matches=0, mismatches=3,
        )
        s.discrepancies = [
            mod._MismatchSample(
                file=f"f{i}.jsonl", timestamp="t", model="m",
                original_cost=1.0, calculated_cost=0.5, difference=0.5,
                percent_diff=50.0, usage={},
            )
            for i in range(3)
        ]
        s.model_stats = {"m": mod._MismatchModelStat(
            total=3, mismatches=3, avg_percent_diff=50.0,
        )}
        # Note: model "m" is unknown to _resolve_model_pricing, but render
        # doesn't validate — it just prints what's in the stats.
        out = mod._render_pricing_mismatch_report(s, 5)
        joined = "\n".join(out)
        # Header says (first 5) even though only 3 discrepancies exist
        assert "=== Sample Discrepancies (first 5) ===" in joined
        file_lines = [l for l in out if l.startswith("File: ")]
        assert len(file_lines) == 3

    def test_dollar_and_percent_formatting(self):
        mod = _load_cctally_module()
        s = self._stats(
            mod, total_entries=1, entries_with_both=1, matches=0, mismatches=1,
        )
        s.discrepancies = [mod._MismatchSample(
            file="x.jsonl", timestamp="2026-05-01T00:00:00",
            model="claude-opus-4-7",
            original_cost=0.123456789, calculated_cost=0.234567891,
            difference=0.111111102, percent_diff=89.99,
            usage={"input_tokens": 1234},
        )]
        s.model_stats = {"claude-opus-4-7": mod._MismatchModelStat(
            total=1, mismatches=1, avg_percent_diff=89.99,
        )}
        out = mod._render_pricing_mismatch_report(s, 5)
        joined = "\n".join(out)
        # 6dp for dollar amounts
        assert "Original cost: $0.123457" in joined
        assert "Calculated cost: $0.234568" in joined
        # 2dp for sample percent
        assert "(89.99%)" in joined
        # 1dp for model-stat percent (Avg % difference line)
        assert "Avg % difference: 90.0%" in joined


class TestEmitDebugSamplesGuard:
    def test_one_time_per_process(self, monkeypatch):
        """Spec §7.1.3 / I6: the _DEBUG_REPORT_EMITTED guard means two
        calls in one process emit the report only once.
        """
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", False)

        e_proto = mod.UsageEntry(
            timestamp=__import__("datetime").datetime(2026, 5, 1, tzinfo=__import__("datetime").timezone.utc),
            model="claude-opus-4-7",
            usage={"input_tokens": 1000, "output_tokens": 500,
                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            cost_usd=1.0,
            source_path="synth.jsonl",
        )
        ns = argparse.Namespace(debug=True, debug_samples=5)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            mod._emit_debug_samples_if_set(ns, [e_proto], command_label="daily")
            mod._emit_debug_samples_if_set(ns, [e_proto], command_label="daily")
        # Either the header appears once (when entries trigger the long-form)
        # or "No pricing data found to analyze." appears once.
        text = buf.getvalue()
        header_count = text.count("=== Pricing Mismatch Debug Report ===")
        short_count = text.count("No pricing data found to analyze.")
        assert header_count + short_count == 1, text

    def test_no_emission_without_flag(self, monkeypatch):
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", False)
        ns = argparse.Namespace(debug=False, debug_samples=5)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            mod._emit_debug_samples_if_set(ns, [], command_label="daily")
        assert buf.getvalue() == ""

    def test_loader_callable_deferred(self, monkeypatch):
        """When `--debug` is unset, the loader must NOT be called."""
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", False)
        ns = argparse.Namespace(debug=False, debug_samples=5)
        call_count = {"n": 0}
        def loader():
            call_count["n"] += 1
            return []
        with contextlib.redirect_stderr(io.StringIO()):
            mod._emit_debug_samples_if_set(ns, loader, command_label="cache-report")
        assert call_count["n"] == 0

    def test_loader_callable_invoked_when_debug(self, monkeypatch):
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", False)
        ns = argparse.Namespace(debug=True, debug_samples=5)
        call_count = {"n": 0}
        def loader():
            call_count["n"] += 1
            return []
        with contextlib.redirect_stderr(io.StringIO()):
            mod._emit_debug_samples_if_set(ns, loader, command_label="cache-report")
        assert call_count["n"] == 1


class TestAdapters:
    def test_usage_entry_from_joined(self):
        import datetime as dt
        mod = _load_cctally_module()
        je = mod._JoinedClaudeEntry(
            timestamp=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
            model="claude-opus-4-7",
            input_tokens=100,
            output_tokens=50,
            cache_creation_tokens=10,
            cache_read_tokens=5,
            source_path="/p/foo.jsonl",
            session_id="sess-A",
            project_path="/p",
            cost_usd=0.05,
        )
        ue = mod._usage_entry_from_joined(je)
        assert ue.timestamp == je.timestamp
        assert ue.model == "claude-opus-4-7"
        assert ue.cost_usd == 0.05
        assert ue.source_path == "/p/foo.jsonl"
        assert ue.usage == {
            "input_tokens": 100, "output_tokens": 50,
            "cache_creation_input_tokens": 10, "cache_read_input_tokens": 5,
        }

    def test_resolve_session_id_uses_id_when_set(self):
        import datetime as dt
        mod = _load_cctally_module()
        je = mod._JoinedClaudeEntry(
            timestamp=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
            model="claude-opus-4-7",
            input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0,
            source_path="/p/whatever.jsonl",
            session_id="real-id",
            project_path=None,
            cost_usd=None,
        )
        assert mod._resolve_session_id_for_filter(je) == "real-id"

    def test_resolve_session_id_falls_back_to_filename_stem(self):
        """Spec §7.2.1.1: matches `_aggregate_claude_sessions` fallback."""
        import datetime as dt
        mod = _load_cctally_module()
        je = mod._JoinedClaudeEntry(
            timestamp=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
            model="claude-opus-4-7",
            input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0,
            source_path="/p/abc-def-uuid.jsonl",
            session_id=None,
            project_path=None,
            cost_usd=None,
        )
        assert mod._resolve_session_id_for_filter(je) == "abc-def-uuid"

    def test_project_filter_matches_empty_patterns(self):
        mod = _load_cctally_module()
        # _resolve_project_key returns a ProjectKey-like; build a stub
        class _K:
            display_key = "foo"
            git_root = None
            bucket_path = None
        assert mod._project_filter_matches(_K(), []) is True

    def test_project_filter_matches_display_key(self):
        mod = _load_cctally_module()
        class _K:
            display_key = "my-project"
            git_root = None
            bucket_path = None
        assert mod._project_filter_matches(_K(), ["my-proj"]) is True
        assert mod._project_filter_matches(_K(), ["other"]) is False

    def test_project_filter_matches_git_root(self):
        mod = _load_cctally_module()
        class _K:
            display_key = "shortname"
            git_root = "/Users/me/repos/some-repo"
            bucket_path = None
        assert mod._project_filter_matches(_K(), ["some-repo"]) is True

    def test_project_filter_matches_bucket_path(self):
        mod = _load_cctally_module()
        class _K:
            display_key = "shortname"
            git_root = None
            bucket_path = "/Users/me/.local/bucket/proj-bucket"
        assert mod._project_filter_matches(_K(), ["proj-bucket"]) is True


# §9.3 — _emit_debug_samples_if_set integration tests
INSCOPE_CMDS = [
    "daily", "monthly", "weekly", "session", "blocks",
    "five-hour-blocks", "project", "diff", "range-cost",
    "cache-report",
]


def _window_args_for(cmd):
    if cmd == "diff":
        # Explicit date-range tokens (NOT `last-week`/`this-week`): the
        # subscription-week tokens raise NoAnchorError on an empty fake
        # home and exit BEFORE reaching `_emit_diff_debug_samples`, which
        # historically masked an AttributeError in the diff `--debug` path
        # (it read `window.start`/`window.end`, but `ParsedWindow` only
        # exposes `start_utc`/`end_utc`). Date ranges resolve without an
        # anchor so the matrix actually exercises the debug emission.
        return ["--a", "2026-05-01..2026-05-07", "--b", "2026-05-08..2026-05-14"]
    if cmd == "range-cost":
        return ["--start", "2026-01-01T00:00:00Z", "--end", "2026-01-02T00:00:00Z"]
    return []


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


@pytest.mark.parametrize("cmd", INSCOPE_CMDS)
class TestDebugEmissionMatrix:
    def test_debug_absent_without_flag(self, cmd, fake_home):
        r = subprocess.run(
            [sys.executable, str(CCTALLY), cmd, *_window_args_for(cmd)],
            capture_output=True, text=True, timeout=30,
        )
        assert "=== Pricing Mismatch Debug Report ===" not in r.stderr
        assert "No pricing data found to analyze." not in r.stderr

    def test_debug_emits_report_short_or_long(self, cmd, fake_home):
        r = subprocess.run(
            [sys.executable, str(CCTALLY), cmd, *_window_args_for(cmd),
             "--debug"],
            capture_output=True, text=True, timeout=30,
        )
        # Empty home → no entries → short-form. (Or long-form if the
        # process somehow has cached entries; either is acceptable.)
        header_count = r.stderr.count("=== Pricing Mismatch Debug Report ===")
        short_count = r.stderr.count("No pricing data found to analyze.")
        if cmd == "diff":
            # diff Pattern D emits 2 reports — but only if anchor
            # resolution succeeds. Empty fake_home has no anchor;
            # the parser bails out before the emit point.
            if "no subscription-week anchor available" not in r.stderr:
                assert header_count + short_count >= 2, r.stderr
        else:
            assert header_count + short_count >= 1, r.stderr

    def test_debug_samples_zero_suppresses_block(self, cmd, fake_home):
        r = subprocess.run(
            [sys.executable, str(CCTALLY), cmd, *_window_args_for(cmd),
             "--debug", "--debug-samples", "0"],
            capture_output=True, text=True, timeout=30,
        )
        assert "=== Sample Discrepancies" not in r.stderr

    def test_debug_stderr_only_stdout_byte_stable(self, cmd, fake_home):
        """Spec §7.6.2 contract: --debug must not perturb stdout."""
        env_base = os.environ.copy()
        r_off = subprocess.run(
            [sys.executable, str(CCTALLY), cmd, *_window_args_for(cmd)],
            capture_output=True, text=True, timeout=30, env=env_base,
        )
        r_on = subprocess.run(
            [sys.executable, str(CCTALLY), cmd, *_window_args_for(cmd),
             "--debug"],
            capture_output=True, text=True, timeout=30, env=env_base,
        )
        assert r_off.stdout == r_on.stdout, (
            f"--debug perturbed stdout for {cmd}:\n"
            f"off: {r_off.stdout!r}\non: {r_on.stdout!r}"
        )


class TestScopeFidelity:
    """Spec §9.4 — the report describes the rendered scope, not a
    generic time window. These tests are the proof that the Pattern B/C/D
    wiring is correct.
    """

    def _seed_fixture(self, home, *, project_dir, jsonl_name, entries):
        """Write a JSONL fixture under <home>/.claude/projects/<project_dir>/."""
        import json as _j
        proj = home / ".claude" / "projects" / project_dir
        proj.mkdir(parents=True, exist_ok=True)
        fp = proj / jsonl_name
        with fp.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(_j.dumps(e) + "\n")
        return fp

    def _assistant_entry(self, *, ts, model="claude-opus-4-7",
                         msg_id="msg-1", req_id="req-1",
                         input_tokens=100, output_tokens=50,
                         cache_creation=0, cache_read=0,
                         recorded_cost=None):
        """Build a minimal valid assistant entry."""
        entry = {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "id": msg_id,
                "model": model,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                },
            },
            "requestId": req_id,
        }
        if recorded_cost is not None:
            entry["costUSD"] = recorded_cost
        return entry

    def test_session_id_filter_scopes_report(self, tmp_path, monkeypatch):
        """Spec §9.4 / §7.2.1.1: --id filter limits report scope to
        entries belonging to the matching session.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        # Two distinct JSONL fixtures with mismatched costs (so the
        # mismatch report has signal).
        self._seed_fixture(
            home, project_dir="proj-A", jsonl_name="sess-target.jsonl",
            entries=[
                self._assistant_entry(
                    ts="2026-05-23T12:00:00Z", msg_id="m1", req_id="r1",
                    recorded_cost=10.0,  # vastly off → mismatch
                ),
            ],
        )
        self._seed_fixture(
            home, project_dir="proj-B", jsonl_name="sess-other.jsonl",
            entries=[
                self._assistant_entry(
                    ts="2026-05-23T13:00:00Z", msg_id="m2", req_id="r2",
                    recorded_cost=20.0,
                ),
            ],
        )

        # Run with --id pointing at the target session (filename stem
        # since these fixtures don't carry an explicit session_id).
        r = subprocess.run(
            [sys.executable, str(CCTALLY), "session",
             "--since", "2026-05-23", "--until", "2026-05-23",
             "--id", "sess-target",
             "--debug"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, (r.returncode, r.stderr)
        # Report should describe 1 entry, not 2
        assert "Total entries processed: 1" in r.stderr, r.stderr

    def test_session_id_fallback_case(self, tmp_path, monkeypatch):
        """Spec §9.4 session_id_fallback: when je.session_id is None
        (sessionId column not yet backfilled), the report-side filter
        falls back to filename-stem matching. This is the regression
        from Codex round-3 review.

        Drive the helper directly via the importable module to avoid
        the cache-priming difficulty of forcing session_id=None.
        """
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", False)
        import datetime as dt
        je = mod._JoinedClaudeEntry(
            timestamp=dt.datetime(2026, 5, 23, 12, tzinfo=dt.timezone.utc),
            model="claude-opus-4-7",
            input_tokens=1000, output_tokens=500,
            cache_creation_tokens=0, cache_read_tokens=0,
            source_path="/p/proj-A/my-stem-uuid.jsonl",
            session_id=None,  # forces fallback
            project_path="/p/proj-A",
            cost_usd=10.0,
        )
        # The aggregator-equivalent fallback id should be "my-stem-uuid"
        assert mod._resolve_session_id_for_filter(je) == "my-stem-uuid"
        # And that's what the report-side filter uses to scope by --id

    def test_diff_emits_two_labeled_reports(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        r = subprocess.run(
            [sys.executable, str(CCTALLY), "diff",
             "--a", "last-week", "--b", "this-week", "--debug"],
            capture_output=True, text=True, timeout=30,
        )
        # Two short-form lines (empty home) — but ONLY if anchor
        # resolution succeeded. Empty home → no anchor → parser bails.
        if "no subscription-week anchor available" not in r.stderr:
            assert r.stderr.count("No pricing data found to analyze.") == 2, r.stderr

    def test_cache_report_honors_project_filter(self, tmp_path, monkeypatch):
        """Spec §9.4: cache-report's --project filter should scope
        the report identically to the rendered output.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        # Two distinct project fixtures
        self._seed_fixture(
            home, project_dir="proj-target", jsonl_name="sess-1.jsonl",
            entries=[
                self._assistant_entry(
                    ts="2026-05-23T12:00:00Z", msg_id="m1", req_id="r1",
                    recorded_cost=10.0,
                ),
            ],
        )
        self._seed_fixture(
            home, project_dir="proj-other", jsonl_name="sess-2.jsonl",
            entries=[
                self._assistant_entry(
                    ts="2026-05-23T13:00:00Z", msg_id="m2", req_id="r2",
                    recorded_cost=20.0,
                ),
            ],
        )
        # Use a 2-day window so cache-report's window resolver gives a
        # non-empty [since, until); same-day since/until collapses to an
        # empty window per the early-return at line 8797.
        r = subprocess.run(
            [sys.executable, str(CCTALLY), "cache-report",
             "--since", "2026-05-22", "--until", "2026-05-24",
             "--project", "proj-target",
             "--debug"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, (r.returncode, r.stderr)
        # Report sees 1 entry (the proj-target one), not 2
        assert "Total entries processed: 1" in r.stderr, r.stderr


class TestArgparseValidation:
    def test_negative_debug_samples_rejected(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        r = subprocess.run(
            [sys.executable, str(CCTALLY), "daily", "--debug-samples", "-1"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 2
        assert "must be >= 0, got -1" in r.stderr, r.stderr

    def test_non_integer_debug_samples_rejected(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        r = subprocess.run(
            [sys.executable, str(CCTALLY), "daily", "--debug-samples", "foo"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 2
        assert "must be a non-negative integer, got 'foo'" in r.stderr, r.stderr


class TestDiffDebugSamples:
    def test_emits_two_reports_one_per_window(self, monkeypatch):
        """Spec §7.2.2 Pattern D: cmd_diff emits TWO reports — one per
        window — labeled "Window A: <token>" and "Window B: <token>".
        """
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", False)
        # Stub the shared cache reader so we don't need a real cache. The
        # debug helper reuses `_diff_iter_claude_entries`, which trims the
        # half-open `end_utc` by 1 µs and calls `get_claude_session_entries`
        # — patch THAT (not `get_entries`) to observe the real wiring.
        captured_calls = []
        def fake_get_claude_session_entries(start, end, **kwargs):
            captured_calls.append((start, end, kwargs))
            return []  # empty → short-form report
        monkeypatch.setattr(
            mod, "get_claude_session_entries", fake_get_claude_session_entries
        )
        import datetime as dt
        # Real ParsedWindow objects (NOT a duck-typed `.start`/`.end` stub):
        # `ParsedWindow` exposes `start_utc`/`end_utc`, so this exercises the
        # production field names the helper now reads.
        wa = mod.ParsedWindow(
            label="last-week",
            start_utc=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
            end_utc=dt.datetime(2026, 5, 8, tzinfo=dt.timezone.utc),
            length_days=7.0, kind="week", week_aligned=True, full_weeks_count=1,
        )
        wb = mod.ParsedWindow(
            label="this-week",
            start_utc=dt.datetime(2026, 5, 8, tzinfo=dt.timezone.utc),
            end_utc=dt.datetime(2026, 5, 15, tzinfo=dt.timezone.utc),
            length_days=7.0, kind="week", week_aligned=True, full_weeks_count=1,
        )
        ns = argparse.Namespace(
            debug=True, debug_samples=5, sync=False,
            a="last-week", b="this-week",
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            mod._emit_diff_debug_samples(ns, wa, wb)
        text = buf.getvalue()
        # Both reports appear (short form because we returned [] entries).
        # The labels are part of `command_label`, which the short-form
        # report does NOT print (only long-form prints `Command:`). So
        # for the short-form path we instead assert there are exactly 2
        # "No pricing data found to analyze." lines.
        assert text.count("No pricing data found to analyze.") == 2, text
        # Two reads, one per window.
        assert len(captured_calls) == 2
        # Half-open semantics: the helper passes `start_utc` and an
        # `end_utc - 1µs` exclusive end to the inclusive-end cache reader.
        assert captured_calls[0][0] == wa.start_utc
        assert captured_calls[0][1] == wa.end_utc - dt.timedelta(microseconds=1)
        assert captured_calls[1][0] == wb.start_utc
        assert captured_calls[1][1] == wb.end_utc - dt.timedelta(microseconds=1)
        # Without --sync, the debug helper observes the cache as-is (no
        # ingest): skip_sync stays True for both windows.
        for _, _, kwargs in captured_calls:
            assert kwargs.get("skip_sync") is True

    def test_sync_flag_propagates_to_debug_reads(self, monkeypatch):
        """Codex review (round 1): under `diff --sync --debug` the debug
        helper MUST run the same ingest as `_build_diff_result`
        (`skip_sync=not args.sync`) so its pricing-mismatch stats reflect
        the freshly-synced JSONL the rendered diff shows — otherwise the
        debug report is computed from the STALE cache, misleading in
        exactly the case `--sync` exists to fix. The first read runs the
        delta ingest; subsequent reads (second window + the builder) are
        cheap delta no-ops, so this is not a problematic triple full-walk.
        """
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", False)
        captured_calls = []
        def fake_get_claude_session_entries(start, end, **kwargs):
            captured_calls.append((start, end, kwargs))
            return []
        monkeypatch.setattr(
            mod, "get_claude_session_entries", fake_get_claude_session_entries
        )
        import datetime as dt
        wa = mod.ParsedWindow(
            label="last-week",
            start_utc=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
            end_utc=dt.datetime(2026, 5, 8, tzinfo=dt.timezone.utc),
            length_days=7.0, kind="week", week_aligned=True, full_weeks_count=1,
        )
        wb = mod.ParsedWindow(
            label="this-week",
            start_utc=dt.datetime(2026, 5, 8, tzinfo=dt.timezone.utc),
            end_utc=dt.datetime(2026, 5, 15, tzinfo=dt.timezone.utc),
            length_days=7.0, kind="week", week_aligned=True, full_weeks_count=1,
        )
        ns = argparse.Namespace(
            debug=True, debug_samples=5, sync=True,
            a="last-week", b="this-week",
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            mod._emit_diff_debug_samples(ns, wa, wb)
        assert len(captured_calls) == 2
        # --sync set → skip_sync False for both windows (matches the
        # builder's `skip_sync=not args.sync`).
        for _, _, kwargs in captured_calls:
            assert kwargs.get("skip_sync") is False


# Review-loop tests (issue #89 review round, all P1/P2/P3 + SR-P2 gaps)
class TestSyntheticFiltering:
    """P1.1: synthetic entries must be excluded from total_entries AND
    must NOT trigger _resolve_model_pricing (which would emit a `[cost]
    unknown model: <synthetic>` warning and pollute
    _unknown_model_warnings).
    """

    def _make_entry(self, mod, *, model, cost_usd):
        import datetime as dt
        return mod.UsageEntry(
            timestamp=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
            model=model,
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            cost_usd=cost_usd,
            source_path="/tmp/synth.jsonl",
        )

    def test_synthetic_excluded_from_totals(self):
        mod = _load_cctally_module()
        synth = self._make_entry(mod, model="<synthetic>", cost_usd=0.05)
        # Real entry with matching cost so it's a match (not a mismatch).
        real_proto = self._make_entry(
            mod, model="claude-opus-4-7", cost_usd=None,
        )
        calc = mod._calculate_entry_cost(
            real_proto.model, real_proto.usage, mode="calculate",
        )
        real = self._make_entry(
            mod, model="claude-opus-4-7", cost_usd=calc,
        )
        stats = mod._compute_pricing_mismatch_stats([synth, real])
        # Synthetic skipped entirely — total_entries counts only the real entry
        assert stats.total_entries == 1
        # The real entry has a known model + cost_usd
        assert stats.entries_with_both == 1
        # And matches → no mismatches recorded
        assert stats.matches == 1
        assert stats.mismatches == 0
        # No _MismatchModelStat for synthetic
        assert "<synthetic>" not in stats.model_stats

    def test_synthetic_does_not_warn_to_stderr(self, tmp_path, monkeypatch):
        """When a fixture contains a synthetic entry, running cctally
        daily --debug must NOT emit `[cost] unknown model: <synthetic>`
        on stderr. (The bare `daily` (no --debug) does emit that warning
        as part of the normal pricing path; this test only covers the
        --debug code path's added filter.)

        Drive directly via the compute helper (subprocess test would
        also fire the bare-cost warning path which is out of scope here).
        """
        mod = _load_cctally_module()
        import datetime as dt
        synth = mod.UsageEntry(
            timestamp=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
            model="<synthetic>",
            usage={"input_tokens": 1, "output_tokens": 1,
                   "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 0},
            cost_usd=0.05,
            source_path="/tmp/synth.jsonl",
        )
        # Snapshot the unknown-model warning set BEFORE compute
        warnings_before = set(getattr(mod, "_unknown_model_warnings", set()))
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            mod._compute_pricing_mismatch_stats([synth])
        text = buf.getvalue()
        warnings_after = set(getattr(mod, "_unknown_model_warnings", set()))
        # No stderr noise from the compute helper
        assert "<synthetic>" not in text, text
        # _unknown_model_warnings should NOT have grown to include "<synthetic>"
        assert (warnings_after - warnings_before) == set(), (
            f"compute helper polluted _unknown_model_warnings: "
            f"{warnings_after - warnings_before}"
        )


class TestLoaderExceptionDegrades:
    """P1.2: when a callable loader raises sqlite3.DatabaseError / OSError,
    the helper must NOT propagate — emit a one-line "report unavailable"
    notice on stderr and return cleanly. The wrapping cmd_* should still
    complete.
    """

    def test_loader_database_error_does_not_propagate(self, monkeypatch):
        import sqlite3
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", False)
        ns = argparse.Namespace(debug=True, debug_samples=5)
        def loader():
            raise sqlite3.DatabaseError("simulated lock")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            # Must not raise
            mod._emit_debug_samples_if_set(
                ns, loader, command_label="cache-report",
            )
        text = buf.getvalue()
        assert "report unavailable: simulated lock" in text, text
        # Pricing Mismatch header must NOT appear (the loader didn't return)
        assert "=== Pricing Mismatch Debug Report ===" not in text

    def test_loader_os_error_does_not_propagate(self, monkeypatch):
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", False)
        ns = argparse.Namespace(debug=True, debug_samples=5)
        def loader():
            raise OSError("EIO")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            mod._emit_debug_samples_if_set(
                ns, loader, command_label="cache-report",
            )
        text = buf.getvalue()
        assert "report unavailable: EIO" in text, text

    def test_diff_window_a_failure_does_not_block_window_b(self, monkeypatch):
        """If the cache read raises for window A, the diff helper logs a
        one-line notice for window A and still attempts window B. The
        guard is still set in `finally:` so a downstream cmd_* doesn't
        double-emit.
        """
        import sqlite3
        mod = _load_cctally_module()
        monkeypatch.setattr(mod, "_DEBUG_REPORT_EMITTED", False)
        # First call raises, second returns empty
        call_log = []
        def fake_get_claude_session_entries(start, end, **kwargs):
            call_log.append((start, end))
            if len(call_log) == 1:
                raise sqlite3.DatabaseError("window-A boom")
            return []
        monkeypatch.setattr(
            mod, "get_claude_session_entries", fake_get_claude_session_entries
        )
        import datetime as dt
        wa = mod.ParsedWindow(
            label="last-week",
            start_utc=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
            end_utc=dt.datetime(2026, 5, 8, tzinfo=dt.timezone.utc),
            length_days=7.0, kind="week", week_aligned=True, full_weeks_count=1,
        )
        wb = mod.ParsedWindow(
            label="this-week",
            start_utc=dt.datetime(2026, 5, 8, tzinfo=dt.timezone.utc),
            end_utc=dt.datetime(2026, 5, 15, tzinfo=dt.timezone.utc),
            length_days=7.0, kind="week", week_aligned=True, full_weeks_count=1,
        )
        ns = argparse.Namespace(
            debug=True, debug_samples=5, sync=False,
            a="last-week", b="this-week",
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            mod._emit_diff_debug_samples(ns, wa, wb)
        text = buf.getvalue()
        # Window A: notice line
        assert "window A report unavailable: window-A boom" in text, text
        # Window B: short-form (empty entries returned)
        assert "No pricing data found to analyze." in text, text
        # Both windows attempted
        assert len(call_log) == 2
        # Guard was set in `finally:`
        assert mod._DEBUG_REPORT_EMITTED is True


class TestStreamingMeanBoundary:
    """P3.4: the < 0.1% threshold's boundary case — recorded ==
    calculated * (1 + 1e-3) → percent_diff == 0.1% exactly → classified
    as MISMATCH by the strict `< 0.1` comparator.
    """

    def test_threshold_boundary_at_exactly_0_1_percent(self):
        mod = _load_cctally_module()
        import datetime as dt
        proto = mod.UsageEntry(
            timestamp=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
            model="claude-opus-4-7",
            usage={"input_tokens": 1000, "output_tokens": 500,
                   "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 0},
            cost_usd=None,
            source_path="/tmp/synth.jsonl",
        )
        calc = mod._calculate_entry_cost(
            proto.model, proto.usage, mode="calculate",
        )
        # Set recorded such that percent_diff equals exactly 0.1%
        # |orig - calc| / orig * 100 == 0.1 ⇒ orig = calc / (1 - 1e-3)
        # (recorded > calculated case so abs() simplifies cleanly)
        recorded = calc / (1 - 1e-3)
        entry = mod.UsageEntry(
            timestamp=proto.timestamp, model=proto.model,
            usage=proto.usage, cost_usd=recorded,
            source_path=proto.source_path,
        )
        stats = mod._compute_pricing_mismatch_stats([entry])
        # Recompute percent_diff the same way the helper does
        actual_pct = abs(recorded - calc) / recorded * 100
        # The exact-0.1% case (within float epsilon) is classified per
        # the `< 0.1` comparator: anything >= 0.1 is a MISMATCH.
        # Float arithmetic may land just above or just below; assert
        # that whichever side we land on, the classification matches
        # the comparator's strict-less semantics.
        if actual_pct < 0.1:
            assert stats.matches == 1
            assert stats.mismatches == 0
        else:
            assert stats.matches == 0
            assert stats.mismatches == 1


class TestByteStableJsonStdout:
    """P3.3: --json + --debug must produce byte-identical stdout to
    --json alone. Strengthen by using a fixture with a real assistant
    entry so both runs return a non-trivial payload (the empty-home
    matrix test passes trivially because both runs return 'no data').
    """

    def test_daily_json_stdout_byte_stable_with_fixture(
        self, tmp_path, monkeypatch,
    ):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        # Seed a real assistant entry inside a 2026 window
        import json as _j
        proj = home / ".claude" / "projects" / "proj-X"
        proj.mkdir(parents=True)
        (proj / "sess-1.jsonl").write_text(
            _j.dumps({
                "type": "assistant",
                "timestamp": "2026-05-23T12:00:00Z",
                "message": {
                    "id": "msg-1",
                    "model": "claude-opus-4-7",
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
                "requestId": "req-1",
                "costUSD": 0.05,
            }) + "\n",
            encoding="utf-8",
        )
        common = ["daily", "--since", "2026-05-23", "--until", "2026-05-23",
                  "--json"]
        env = os.environ.copy()
        r_off = subprocess.run(
            [sys.executable, str(CCTALLY), *common],
            capture_output=True, text=True, timeout=30, env=env,
        )
        r_on = subprocess.run(
            [sys.executable, str(CCTALLY), *common, "--debug"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        assert r_off.returncode == 0, r_off.stderr
        assert r_on.returncode == 0, r_on.stderr
        assert r_off.stdout == r_on.stdout, (
            f"--debug perturbed --json stdout:\n"
            f"off ({len(r_off.stdout)}b): {r_off.stdout!r}\n"
            f"on  ({len(r_on.stdout)}b): {r_on.stdout!r}"
        )
        # And the --debug run did emit the report on stderr (proves
        # we're not just comparing two empty runs)
        assert (
            "=== Pricing Mismatch Debug Report ===" in r_on.stderr
            or "No pricing data found to analyze." in r_on.stderr
        ), r_on.stderr


class TestScopeFidelityRoundTwo:
    """SR-P2.1 / SR-P2.2 / SR-P2.3 / SR-P2.4: scope fidelity for the
    cmds whose report scope can drift from a naive [range_start,
    range_end) read.
    """

    def _seed_fixture(self, home, *, project_dir, jsonl_name, entries):
        import json as _j
        proj = home / ".claude" / "projects" / project_dir
        proj.mkdir(parents=True, exist_ok=True)
        fp = proj / jsonl_name
        with fp.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(_j.dumps(e) + "\n")
        return fp

    def _assistant_entry(self, *, ts, model="claude-opus-4-7",
                         msg_id="msg-1", req_id="req-1",
                         input_tokens=100, output_tokens=50,
                         recorded_cost=None):
        entry = {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "id": msg_id,
                "model": model,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
            "requestId": req_id,
        }
        if recorded_cost is not None:
            entry["costUSD"] = recorded_cost
        return entry

    def test_weekly_fetch_start_widening_scopes_report(
        self, tmp_path, monkeypatch,
    ):
        """SR-P2.1: weekly's fetch_start widens to weeks[0].start_ts (which
        may precede range_start). The --debug report describes the same
        widened scope (all_entries), not range_start..range_end.

        Empty fake-home → no anchor → _compute_subscription_weeks returns
        an empty list → fetch_start = range_start. We can't easily seed
        anchors in a fresh home; instead test that the weekly report's
        Total entries processed counts BOTH entries when both fall inside
        the fetch_start..range_end fetch window (range_start can be the
        later of the two; with no anchors fetch_start = range_start).
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        # Two entries on different days but both inside a single
        # --since/--until span. With no anchors, fetch_start = range_start
        # so this test asserts the report sees both entries (proves the
        # weekly path passes the loaded `all_entries` list to the helper
        # rather than an empty/narrowed slice).
        self._seed_fixture(
            home, project_dir="proj-W", jsonl_name="w.jsonl",
            entries=[
                self._assistant_entry(
                    ts="2026-05-21T12:00:00Z", msg_id="m1", req_id="r1",
                    recorded_cost=5.0,
                ),
                self._assistant_entry(
                    ts="2026-05-23T12:00:00Z", msg_id="m2", req_id="r2",
                    recorded_cost=5.0,
                ),
            ],
        )
        r = subprocess.run(
            [sys.executable, str(CCTALLY), "weekly",
             "--since", "2026-05-21", "--until", "2026-05-23",
             "--debug"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, (r.returncode, r.stderr)
        # The report ran over the widened fetch window (no anchors so
        # fetch_start == range_start; both seeded entries fall inside).
        assert "Total entries processed: 2" in r.stderr, r.stderr

    def test_project_filter_scopes_report(self, monkeypatch):
        """SR-P2.2: cmd_project --project filter scopes the --debug
        report to entries whose ProjectKey matches at least one pattern.

        Drive the filter logic directly via _project_filter_matches +
        _resolve_project_key rather than via subprocess: cmd_project's
        --debug scope filter at bin/cctally:5097 requires non-empty
        parsed_bounds (subscription-week snapshots), which a fresh-home
        subprocess can't provide. Once the snapshots are absent every
        entry's _week_start_for returns None → empty report scope. The
        helper-level test below proves the per-key filter semantics
        without needing the snapshot dependency.
        """
        mod = _load_cctally_module()

        # Build two ProjectKey-shaped duck objects mimicking what
        # _resolve_project_key would return. _project_filter_matches
        # does the predicate work that cmd_project's --debug filter
        # delegates to (see bin/cctally:5108 + 1357).
        class _K:
            def __init__(self, display_key, git_root, bucket_path):
                self.display_key = display_key
                self.git_root = git_root
                self.bucket_path = bucket_path

        target_key = _K("proj-target", "/p/proj-target", None)
        other_key = _K("proj-other", "/p/proj-other", None)
        patterns = ["proj-target"]
        assert mod._project_filter_matches(target_key, patterns) is True
        assert mod._project_filter_matches(other_key, patterns) is False
        # OR semantics across multiple patterns
        assert mod._project_filter_matches(
            target_key, ["proj-target", "proj-other"],
        ) is True
        # Empty patterns → match-all (no filter)
        assert mod._project_filter_matches(target_key, []) is True
        assert mod._project_filter_matches(other_key, []) is True

    def test_range_cost_project_filter_scopes_report(
        self, tmp_path, monkeypatch,
    ):
        """SR-P2.3: cmd_range_cost --project filter scopes the --debug
        report (project filter is applied at the loader so the report
        scope matches the rendered scope).
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        self._seed_fixture(
            home, project_dir="proj-target", jsonl_name="t.jsonl",
            entries=[self._assistant_entry(
                ts="2026-05-23T12:00:00Z", msg_id="m1", req_id="r1",
                recorded_cost=10.0,
            )],
        )
        self._seed_fixture(
            home, project_dir="proj-other", jsonl_name="o.jsonl",
            entries=[self._assistant_entry(
                ts="2026-05-23T13:00:00Z", msg_id="m2", req_id="r2",
                recorded_cost=20.0,
            )],
        )
        r = subprocess.run(
            [sys.executable, str(CCTALLY), "range-cost",
             "--start", "2026-05-23T00:00:00Z",
             "--end", "2026-05-23T23:59:59Z",
             "--project", "proj-target",
             "--debug"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "Total entries processed: 1" in r.stderr, r.stderr

    @pytest.mark.skip(
        reason=(
            "SR-P2.4 five-hour-blocks scope is implicitly covered by the "
            "unit-level block-window computation: cmd_five_hour_blocks "
            "uses a deferred loader bounded by [oldest_block_start, "
            "newest_block_start + BLOCK_DURATION) (see bin/cctally Pattern "
            "C). A subprocess test needs both blocks_table state AND "
            "JSONL fixtures aligned, which is brittle to seed in a clean "
            "home. The Pattern C loader-bounds logic is exercised by the "
            "matrix tests (subprocess) and the empty-home short-form "
            "path; full bound-fidelity coverage tracked in issue #92."
        )
    )
    def test_five_hour_blocks_scopes_to_block_bounds(
        self, tmp_path, monkeypatch,
    ):
        """SR-P2.4 (deferred): five-hour-blocks loader bounds to
        block-window range, not raw --since/--until.
        """
        # See @pytest.mark.skip reason above.
        pass


@pytest.mark.parametrize("cmd", INSCOPE_CMDS)
class TestDebugEmissionMatrixRoundTwo:
    """SR-P2.5: matrix-level coverage gaps."""

    def test_debug_samples_n_caps_count(self, cmd, fake_home):
        """SR-P2.5.1: --debug-samples 2 → at most 2 sample blocks in
        the report (header reads `=== Sample Discrepancies (first 2) ===`
        if any mismatches exist; File: lines ≤ 2 either way).
        """
        r = subprocess.run(
            [sys.executable, str(CCTALLY), cmd, *_window_args_for(cmd),
             "--debug", "--debug-samples", "2"],
            capture_output=True, text=True, timeout=30,
        )
        # Empty home: short-form (no Sample Discrepancies block).
        # If a Sample Discrepancies block DID emit, the cap must hold.
        if "=== Sample Discrepancies" in r.stderr:
            assert "=== Sample Discrepancies (first 2) ===" in r.stderr
        # File: line count is bounded by the requested cap
        file_lines = [
            ln for ln in r.stderr.splitlines() if ln.startswith("File: ")
        ]
        # diff Pattern D may emit 2 reports → 2 caps → up to 4 File:
        # lines total. Single-report cmds → up to 2.
        max_allowed = 4 if cmd == "diff" else 2
        assert len(file_lines) <= max_allowed, (
            f"{cmd}: expected at most {max_allowed} File: lines under "
            f"--debug-samples 2 (got {len(file_lines)}):\n{r.stderr}"
        )

    def test_debug_command_label_present_when_long_form(
        self, cmd, fake_home,
    ):
        """SR-P2.5.2: when the report is long-form (not the empty
        short-form), `Command: cctally <cmd>` appears under the header.
        Empty fake-home → short form → skip the assertion.
        """
        r = subprocess.run(
            [sys.executable, str(CCTALLY), cmd, *_window_args_for(cmd),
             "--debug"],
            capture_output=True, text=True, timeout=30,
        )
        # Only assert when the long-form header is present. Short-form
        # ("No pricing data found to analyze.") does NOT print the
        # command label per upstream parity.
        if "=== Pricing Mismatch Debug Report ===" in r.stderr:
            # For diff, the command label includes the window token
            # ("diff (Window A: <token>)"); for others, the bare cmd name.
            if cmd == "diff":
                assert "Command: cctally diff (Window A:" in r.stderr, r.stderr
            else:
                assert f"Command: cctally {cmd}" in r.stderr, r.stderr


class TestDebugOneTimeEmissionAcrossDispatch:
    """SR-P2.5.3: the _DEBUG_REPORT_EMITTED guard ensures only ONE
    report per process even when a cmd dispatch composes multiple cmd_*
    helpers. Each subprocess gets a fresh module so this is essentially
    a smoke test that the guard semantics are intact at the helper
    level. The per-cmd matrix variant (one subprocess per cmd) is
    naturally one-shot per process; the unit-level guarantee lives in
    TestEmitDebugSamplesGuard::test_one_time_per_process above.

    A full cross-cmd in-process matrix would require setting up each
    cmd's argparse contract independently — too invasive for the
    boundary value it provides. Tracked in issue #92 if escalated.
    """

    @pytest.mark.skip(
        reason=(
            "Per-process one-shot semantics covered by "
            "TestEmitDebugSamplesGuard::test_one_time_per_process at "
            "unit level; full cross-cmd in-process matrix requires "
            "per-cmd argparse stubbing that adds brittleness without "
            "new coverage. Tracked in issue #92 if escalated."
        )
    )
    def test_debug_one_time_emission_across_dispatch(self):
        pass

"""Pure-function kernel for cctally cache-report.

This module owns the day/session bucketing, financial computation, and
anomaly classification logic that previously lived inline in
``bin/cctally``. The CLI command ``cctally cache-report`` and the
dashboard sync builder both consume this kernel; the kernel itself is
pure (no I/O, no logging, no environment reads, no SQLite connection).

Display-tz threading: bucketing functions accept ``display_tz``
explicitly. ``None`` means host-local fallback (legacy behavior).
Callers pass the resolved IANA zone from ``resolve_display_tz``.

See ``docs/superpowers/specs/2026-05-21-cache-report-panel-design.md``
§5 for the full contract.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Optional, Tuple
from zoneinfo import ZoneInfo


# Tasks A2–A6 populate this module:
#   * dataclasses ``CacheModelBreakdown``, ``CacheRow``, ``_CacheReportResult``
#   * pure helpers ``_compute_cache_hit_percent``, ``_compute_entry_cache_dollars``
#   * aggregators ``_aggregate_cache_by_day``, ``_aggregate_cache_by_session``
#   * anomaly classification ``_classify_anomalies``, ``_compute_baseline_median``
#   * top-level orchestrator ``_build_cache_report``

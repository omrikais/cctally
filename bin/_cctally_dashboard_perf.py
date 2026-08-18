"""`cctally dashboard-perf` — read a running dashboard's tick cost (#583 S1 §3.3).

Reads the loopback diagnostic `/api/debug/backend` and renders the three
things an operator on a slow install needs: the publish period stated
SEPARATELY for the Codex-active and Codex-idle regimes, the tick-cost
breakdown with the ingest and builder halves named, and the dispatch mix. It
also arms and disarms the deep phase trace on the running process, so one
command covers both halves of F38 and neither answer needs a restart.

The command reaches only an IP-literal loopback host, and it is DELIBERATELY
STRICTER than the server about that. `_lib_transcript_access.is_loopback`
treats `localhost` and `::1` as loopback names before it tries to parse an IP
literal, so the endpoint serves `Host: localhost:8789` with 200. This command
still refuses a name: a name is resolved by the operating system, the client
cannot verify that the resolver's answer is this machine, and an unambiguous
target is worth more than the convenience. A hostname that is not a loopback
name is refused by the server too.

It also sends an `Origin` header matching the `Host` it calls, because
`_check_origin_csrf` rejects a request that carries none. Retaining that check
is deliberate — the loopback and anti-rebinding gates do not stop a malicious
page aiming a form POST at `http://127.0.0.1:8789` — and a non-browser client
can set arbitrary headers regardless, so satisfying it costs nothing.

Exit codes follow `docs/cli-contract.md`: 0 for a decoded HTTP 200, 2 for
argument validation including a non-loopback target, and 3 for connection,
HTTP, authentication, timeout or malformed-response failures — "no dashboard
is running" among them.
"""
from __future__ import annotations

import ipaddress
import json
import sys
import urllib.error
import urllib.request

from _lib_dashboard_json import encode_dashboard_json, encode_dashboard_json_bytes

_TIMEOUT_SECONDS = 5.0
_DIAGNOSTIC_PATH = "/api/debug/backend"
_TRACE_PATH = "/api/debug/backend/trace"

#: The two regimes the period is reported for. `not_observed` is discarded:
#: a tick no build's Codex decision reached says nothing about either cost
#: regime, and folding it into one of them would misreport that regime.
_REPORTED_REGIMES = ("active", "idle")

_REGIME_LABELS = {"active": "Codex-active", "idle": "Codex-idle"}


def _cctally():
    return sys.modules["cctally"]


class DashboardPerfError(Exception):
    """A staged failure: exit 3. Carries the message the user sees."""


def resolve_loopback_target(host: str) -> str:
    """Return `host` when it is an IP-literal loopback address, else raise.

    A hostname is rejected even when it resolves to loopback, because the
    endpoint's own anti-rebinding gate requires an IP-literal `Host`.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(
            f"--host must be an IP literal such as 127.0.0.1 or ::1 (got "
            f"{host!r}); a hostname is refused by the endpoint's "
            f"anti-rebinding gate"
        ) from None
    if not address.is_loopback:
        raise ValueError(
            f"--host must be a loopback address (got {host!r}); "
            f"dashboard-perf never contacts a LAN address"
        )
    return host


def _authority(host: str, port: int) -> str:
    if ":" in host:                      # an IPv6 literal needs brackets
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _request(host, port, path, *, token, body=None):
    """One loopback HTTP round trip. Raises DashboardPerfError on any failure."""
    authority = _authority(host, port)
    url = f"http://{authority}{path}"
    data = None
    headers = {"Origin": f"http://{authority}", "Accept": "application/json"}
    if body is not None:
        data = encode_dashboard_json_bytes(body)
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = {401: "authentication required — pass --token",
                  403: "refused by the loopback gate",
                  404: "this dashboard predates dashboard-perf"}.get(
                      exc.code, "unexpected response")
        raise DashboardPerfError(
            f"{url} answered HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise DashboardPerfError(
            f"cannot reach {url}: {exc.reason}. Is a dashboard running on "
            f"port {port}?") from None
    except OSError as exc:
        raise DashboardPerfError(f"cannot reach {url}: {exc}") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise DashboardPerfError(
            f"{url} returned a malformed response") from None


# ── the per-regime derivation (spec §3.3) ───────────────────────────────────


def summarise_regime_periods(records) -> dict:
    """Partition the ring by `codex_regime` and describe each reported regime.

    Discards `not_observed` and every record whose `period_ns` is null — the
    first publish of a process has no predecessor, so it measures no period.

    Reports the MEDIAN rather than the mean, because one startup or recovery
    outlier otherwise dominates a 64-sample window; the observed range is
    reported beside it so the outlier is still visible.
    """
    # Imported HERE, not at module scope. `bin/cctally` re-exports this
    # module eagerly, so a top-level import is paid by `statusline` on every
    # Claude Code prompt and by `hook-tick` on every tool batch — measured at
    # about 2.6 ms on `cctally --version` — for one `median` call in a command
    # neither of them runs. The other importers in this tree
    # (`_cctally_forecast.py`, `_lib_cache_report.py`) do the same.
    import statistics

    buckets = {regime: [] for regime in _REPORTED_REGIMES}
    for record in records or ():
        regime = record.get("codex_regime")
        period = record.get("period_ns")
        if regime in buckets and period is not None:
            buckets[regime].append(int(period))
    summary = {}
    for regime, samples in buckets.items():
        if not samples:
            summary[regime] = {"samples": 0, "median_ns": None,
                               "min_ns": None, "max_ns": None}
            continue
        summary[regime] = {
            "samples": len(samples),
            "median_ns": int(statistics.median(samples)),
            "min_ns": min(samples),
            "max_ns": max(samples),
        }
    return summary


def summarise_all_periods(records) -> dict:
    """The publish period over EVERY tick, whatever its Codex regime.

    The two regime rows refine this; they do not gate it. A dispatch-`idle`
    tick returns before `_tui_build_source_bundle`, so no build reaches the
    Codex decision and the tick is stamped `not_observed`, which the regime
    partition discards — correctly, because a tick that observed no decision
    says nothing about either cost regime. On a mostly-idle install that left
    the flagship figure reading `no samples yet` on both rows while every
    record after the first carried a correct `period_ns`, so the operator
    learned nothing about an install whose period was perfectly well measured.
    """
    samples = [int(r["period_ns"]) for r in records or ()
               if r.get("period_ns") is not None]
    if not samples:
        return {"samples": 0, "median_ns": None, "min_ns": None,
                "max_ns": None}
    import statistics

    return {
        "samples": len(samples),
        "median_ns": int(statistics.median(samples)),
        "min_ns": min(samples),
        "max_ns": max(samples),
    }


def _seconds(ns) -> str:
    return "—" if ns is None else f"{ns / 1_000_000_000:.2f}s"


def _millis(ns) -> str:
    return "—" if ns is None else f"{ns / 1_000_000:.0f}ms"


def _mean_or_none(values):
    return int(sum(values) / len(values)) if values else None


def _median(values):
    import statistics

    return int(statistics.median(values))


def _render_conversation_sync(tick: dict) -> list:
    """The second work loop's cost, beside the first's (#583 S4 / F5).

    The share is `sum(cpu_ns) / sum(period_ns)` over the passes that carry a
    forward period — a same-process ratio, so it is a measurement rather than a
    machine-speed assertion. `period_ns` is `start[i+1] - start[i]`, so the
    NEWEST retained pass has none: its successor has not started yet. That pass
    contributes to neither sum, which is what keeps numerator and denominator
    spanning the same window. Over the passes that do pair, the periods
    telescope to `start[last] - start[first]` and each pass's CPU falls inside
    its own period, so the ratio is the loop's true duty over that span.

    Rows are read defensively. A running dashboard older or newer than this
    reader can publish a different field set, and a diagnostic must degrade
    rather than take itself down. A period of zero or less is dropped together
    with its CPU: it cannot be a real interval, and a negative one would
    subtract from the denominator.
    """
    rows = tick.get("conversation_sync") or []
    lines = ["", "Conversation sync loop"]
    if not rows:
        # The same literal the regime rows use. A loop with no samples must
        # never render as a zero, which cannot be told apart from a measured
        # idle loop. Under `--no-sync` the thread never starts, so this is that
        # mode's correct and permanent reading.
        lines.append(f"  {'passes':<14} no samples yet")
        return lines
    durations = [int(r.get("duration_ns") or 0) for r in rows]
    cpus = [int(r.get("cpu_ns") or 0) for r in rows]
    paired = []
    for record in rows:
        cpu = record.get("cpu_ns")
        period = record.get("period_ns")
        if cpu is None or period is None or int(period) <= 0:
            continue
        paired.append((int(cpu), int(period)))
    lines.append(
        f"  {'wall':<14} mean {_millis(_mean_or_none(durations))} "
        f"over {len(rows)} pass(es)")
    lines.append(f"  {'thread cpu':<14} mean {_millis(_mean_or_none(cpus))}")
    if paired:
        periods = [p for _, p in paired]
        lines.append(
            f"  {'period':<14} median {_seconds(_median(periods))} "
            f"(range {_seconds(min(periods))}–{_seconds(max(periods))})")
        share = sum(c for c, _ in paired) / sum(periods)
        lines.append(f"  {'cpu share':<14} {share * 100:.1f}% of one core")
    else:
        lines.append(f"  {'period':<14} no samples yet")
    statuses: dict = {}
    for record in rows:
        raw = record.get("status")
        # A row with no status is malformed, not an outcome named `None`.
        key = raw if isinstance(raw, str) and raw else "malformed"
        statuses[key] = statuses.get(key, 0) + 1
    detail = " · ".join(f"{k} {v}" for k, v in sorted(statuses.items()))
    lines.append(f"  {'status':<14} {detail}")
    return lines


def render_dashboard_perf(payload: dict) -> str:
    """The human report. Pure — takes the decoded diagnostic, returns text."""
    tick = payload.get("tick") or {}
    tracing = payload.get("tracing") or {}
    records = tick.get("records") or []
    lines = ["cctally dashboard-perf", ""]

    lines.append("Publish period")
    overall = summarise_all_periods(records)
    if overall["samples"] == 0:
        lines.append(f"  {'all ticks':<14} no samples yet (0 of "
                     f"{len(records)} retained ticks qualify)")
    else:
        lines.append(
            f"  {'all ticks':<14} median {_seconds(overall['median_ns'])} "
            f"over {overall['samples']} sample(s), "
            f"range {_seconds(overall['min_ns'])}–"
            f"{_seconds(overall['max_ns'])}")
    summary = summarise_regime_periods(records)
    for regime in _REPORTED_REGIMES:
        stats = summary[regime]
        label = _REGIME_LABELS[regime]
        if stats["samples"] == 0:
            # Stated, never inferred. A zero or a dash cannot distinguish
            # "measured and fast" from "not measured", and telling those two
            # apart is the whole point of this surface.
            lines.append(f"  {label:<14} no samples yet (0 of {len(records)} "
                         f"retained ticks qualify)")
            continue
        lines.append(
            f"  {label:<14} median {_seconds(stats['median_ns'])} "
            f"over {stats['samples']} sample(s), "
            f"range {_seconds(stats['min_ns'])}–{_seconds(stats['max_ns'])}")
    lines.append("")

    lines.append("Tick cost (exclusive halves; the remainder is orchestration)")
    if records:
        ingest = [r["ingest_ns"] for r in records if r.get("ingest_ran")]
        builder = [r.get("builder_ns", 0) for r in records]
        duration = [r.get("duration_ns", 0) for r in records]
        lines.append(
            f"  ingest         mean {_millis(_mean_or_none(ingest))} "
            f"over {len(ingest)} tick(s) that ingested")
        lines.append(
            f"  builder        mean {_millis(_mean_or_none(builder))} "
            f"over {len(builder)} tick(s)")
        lines.append(
            f"  whole tick     mean {_millis(_mean_or_none(duration))}")
        # #583 S5 §2.4: the cache.db read pin, measured at its own BEGIN and
        # ROLLBACK boundaries. Reported separately from `builder` because it
        # is a SUBSET of builder time rather than a third exclusive half, and
        # `.get(..., 0)` keeps an older record without the field readable.
        pin = [r.get("cache_pin_ns", 0) or 0 for r in records]
        lines.append(
            f"  cache pin      mean {_millis(_mean_or_none(pin))} "
            f"held inside builder")
        newest = records[-1]
        lines.append(
            f"  newest tick    seq {newest.get('seq')} "
            f"{newest.get('dispatch')}/{newest.get('codex_regime')}"
            f"{'/cold' if newest.get('cold') else '/warm'} "
            f"ingest {_millis(newest.get('ingest_ns'))} "
            f"builder {_millis(newest.get('builder_ns'))} "
            f"pin {_millis(newest.get('cache_pin_ns', 0) or 0)} "
            f"total {_millis(newest.get('duration_ns'))}")
    else:
        lines.append("  no ticks recorded yet")
    standalone = tick.get("standalone")
    if standalone:
        lines.append(
            f"  standalone     builder {_millis(standalone.get('builder_ns'))} "
            f"total {_millis(standalone.get('duration_ns'))} "
            f"(the last build made outside a refresh tick)")
    lines.extend(_render_conversation_sync(tick))
    lines.append("")

    counts = tick.get("dispatch_counts") or {}
    lines.append(
        f"Dispatch mix   full {counts.get('full', 0)} · "
        f"idle {counts.get('idle', 0)} · degraded {counts.get('degraded', 0)} "
        f"(of {tick.get('tick_seq', 0)} completed ticks)")

    failures = tick.get("cache_open_failures") or {}
    if any(failures.values()):
        detail = " · ".join(f"{k} {v}" for k, v in sorted(failures.items()))
        lines.append(f"Group A cache-open failures   {detail}")
        lines.append("  These are silent: each one falls back to the wide "
                     "from-scratch fetch with byte-identical output.")
    else:
        lines.append("Group A cache-open failures   none")

    applied = tracing.get("applied")
    requested = tracing.get("requested")
    applies_at = tracing.get("applies_at", "none")
    state = "on" if applied else "off"
    lines.append(f"Phase trace    applied {state} · requested "
                 f"{'on' if requested else 'off'} · applies_at {applies_at}")
    if payload.get("phases") is not None:
        # `generated_at` IS the stored tree's instant, from the same slot the
        # tree comes from — which is what makes a stale tree readable as stale
        # rather than as the last tick.
        lines.append(f"  a stored phase tree is available, generated at "
                     f"{payload.get('generated_at')}")
    else:
        lines.append("  no phase tree stored — arm one with "
                     "`cctally dashboard-perf --trace on`")
    return "\n".join(lines) + "\n"


# ── the command ─────────────────────────────────────────────────────────────


def cmd_dashboard_perf(args) -> int:
    c = _cctally()
    as_json = bool(getattr(args, "json", False))
    try:
        host = resolve_loopback_target(getattr(args, "host", None)
                                       or "127.0.0.1")
    except ValueError as exc:
        print(f"dashboard-perf: {exc}", file=sys.stderr)
        return 2
    port = c._resolve_dashboard_port(getattr(args, "port", None))
    if not isinstance(port, int) or not 1 <= port <= 65535:
        # Argument validation, so exit 2. Left unchecked, a nonsensical port
        # failed at connect time and reported itself as a transport failure,
        # which `docs/cli-contract.md` reserves for a real one.
        print(f"dashboard-perf: --port must be between 1 and 65535 (got "
              f"{getattr(args, 'port', None)!r})", file=sys.stderr)
        return 2

    token = getattr(args, "token", None)
    trace = getattr(args, "trace", None)
    try:
        trace_result = None
        if trace is not None:
            trace_result = _request(
                host, port, _TRACE_PATH, token=token,
                body={"enabled": trace == "on"},
            )
        payload = _request(host, port, _DIAGNOSTIC_PATH, token=token)
    except DashboardPerfError as exc:
        if as_json:
            print(encode_dashboard_json(c.stamp_schema_version(
                {"status": "error", "error": str(exc), "diagnostic": None})))
        else:
            print(f"dashboard-perf: {exc}", file=sys.stderr)
        return 3

    if as_json:
        # `diagnostic` passes the server payload through VERBATIM and stays
        # explicitly opaque, consistent with what `bin/_lib_perf.py` promises
        # about phase names. The stamped wrapper is the stable part.
        print(encode_dashboard_json(c.stamp_schema_version(
            {"status": "ok", "diagnostic": payload})))
        return 0

    if trace_result is not None:
        print(f"dashboard-perf: trace {trace} requested "
              f"(requested={trace_result.get('requested')}, "
              f"applied={trace_result.get('applied')}, "
              f"applies_at={trace_result.get('applies_at')})")
    sys.stdout.write(render_dashboard_perf(payload))
    return 0

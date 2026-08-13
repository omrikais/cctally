"""#511 / #496 S5b §2.4 — no production append lands in a non-last segment.

Both appenders resolve their month segment from the wall clock BEFORE taking
the leaf lock, so a process that resolved just before a UTC month boundary and
then stalled appends to a segment that is no longer canonically last. Two of
S5b's three findings rest on a covered non-last segment being unable to change,
so this is fixed here rather than hedged around: revisions 2 and 3 of the design
tried a size comparison and then a `stat` before the fast path, and the third
gate pass established that torn-tail repair defeats the first and a
time-of-check-to-time-of-use window defeats the second.

The fix is one target revalidation inside the leaf-lock critical section both
appenders already hold, plus a re-resolution of a DEFAULTED `now_utc` at the
same point. It refuses rather than redirects, because refusal preserves physical
order and lets the writer retry against a freshly resolved target, where
silently redirecting a planned correction group would move it out from under a
caller that had already reasoned about its placement.
"""
from __future__ import annotations

import datetime as dt
import importlib
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import _lib_journal as jl  # noqa: E402
from conftest import load_script, redirect_paths  # noqa: E402


JANUARY = dt.datetime(2026, 1, 31, 23, 59, 59, tzinfo=dt.timezone.utc)
FEBRUARY = dt.datetime(2026, 2, 1, 0, 0, 1, tzinfo=dt.timezone.utc)
MARCH = dt.datetime(2026, 3, 1, 0, 0, 1, tzinfo=dt.timezone.utc)

JAN_SEGMENT = "observations-2026-01.jsonl"
FEB_SEGMENT = "observations-2026-02.jsonl"
MAR_SEGMENT = "observations-2026-03.jsonl"
APR_SEGMENT = "observations-2026-04.jsonl"


@pytest.fixture
def jr(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return importlib.import_module("_cctally_journal")


def _obs(percent: float = 10.0) -> dict:
    return jl.make_obs(
        at="2026-01-31T23:59:59Z",
        src="record-usage",
        provider="claude",
        payload={"weekly_percent": percent, "source": "statusline"},
    )


def _quota_obs(line_offset: int = 0) -> dict:
    return jl.make_obs(
        at="2026-01-31T23:59:59Z",
        src="codex-quota",
        provider="codex",
        payload={
            "kind": "quota_window_snapshot",
            "source": "codex",
            "source_path": "/tmp/codex/sessions/a.jsonl",
            "line_offset": line_offset,
            "logical_limit_key": "weekly",
            "used_percent": 5.0,
        },
    )


def _correction_group() -> list[dict]:
    return jl.make_correction_batch(
        batch_id="batch:511",
        family="claude-usage",
        at="2026-01-31T23:59:59Z",
        actions=[
            {
                "action": "tombstone",
                "id": "sa:511",
                "rev": 1,
                "at": "2026-01-31T23:59:59Z",
                "payload": None,
            }
        ],
    )


def _pin_clock(jr, monkeypatch, *values: dt.datetime):
    """Drive the appender's clock. The LAST value repeats forever.

    A stalled writer and a writer whose clock has moved on differ only in what
    the clock returns AFTER the lock is taken, so a test that cannot control
    the two reads separately cannot tell the two cases apart.

    Patches `datetime.datetime` rather than any helper the fix introduces, so
    the same test drives the pre-change and post-change appenders identically
    and the RED run is a behavioural failure rather than an AttributeError.
    """
    remaining = list(values)

    class _Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            value = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return value.astimezone(tz) if tz is not None else value

    monkeypatch.setattr(jr.dt, "datetime", _Clock)


def test_a_stalled_writer_refuses_a_non_last_segment(jr, monkeypatch):
    """The #511 shape: the writer's resolution is behind the journal itself.

    February already exists, so January is no longer canonically last. A writer
    whose clock still reads January must be refused rather than appending into
    a segment every later identity check treats as immutable.
    """
    jr.append_record(_obs(1.0), now_utc=FEBRUARY)
    _pin_clock(jr, monkeypatch, JANUARY)
    with pytest.raises(jr.JournalAppendTargetStale):
        jr.append_record(_obs(2.0))


def test_the_group_appender_refuses_the_same_way(jr, monkeypatch):
    """`append_records` resolves its segment independently of `append_record`.

    Fixing only the single-record appender would leave the claimed immutability
    false for exactly the correction and audit group appends the durable
    selector depends on.
    """
    jr.append_record(_obs(1.0), now_utc=FEBRUARY)
    _pin_clock(jr, monkeypatch, JANUARY)
    with pytest.raises(jr.JournalAppendTargetStale):
        jr.append_records(_correction_group())


def test_a_defaulted_target_is_re_resolved_under_the_lock(jr, monkeypatch):
    """The stall itself is closed, not merely detected.

    The writer resolves January before the lock and the clock reads February by
    the time it holds the lock. A caller that supplied no `now_utc` asked for
    "now", so the append lands in February — the canonically-last segment —
    rather than being refused.
    """
    _pin_clock(jr, monkeypatch, JANUARY, FEBRUARY)
    segment, _offset = jr.append_record(_obs(3.0))
    assert segment == FEB_SEGMENT
    assert not (jr._cctally_core.JOURNAL_DIR / JAN_SEGMENT).exists()


def test_the_group_appender_re_resolves_a_defaulted_target_too(jr, monkeypatch):
    _pin_clock(jr, monkeypatch, JANUARY, FEBRUARY)
    segment, _offset = jr.append_records(_correction_group())
    assert segment == FEB_SEGMENT


def test_an_explicit_now_utc_is_honoured_without_validation(jr):
    """A caller that supplies `now_utc` has made a deliberate placement choice.

    No production caller supplies one; roughly a dozen tests do, precisely to
    pin segment placement. Validating those would break them for no safety
    gain, because a deliberate placement choice is not a stall.
    """
    jr.append_record(_obs(1.0), now_utc=FEBRUARY)
    segment, _offset = jr.append_record(_obs(2.0), now_utc=JANUARY)
    assert segment == JAN_SEGMENT
    group_segment, _group_offset = jr.append_records(
        _correction_group(), now_utc=JANUARY)
    assert group_segment == JAN_SEGMENT
    assert (jr._cctally_core.JOURNAL_DIR / FEB_SEGMENT).exists()


def test_an_absent_target_that_sorts_last_is_accepted(jr, monkeypatch):
    """A brand-new month has no file yet; that is the ordinary rollover."""
    jr.append_record(_obs(1.0), now_utc=FEBRUARY)
    _pin_clock(jr, monkeypatch, MARCH)
    segment, _offset = jr.append_record(_obs(2.0))
    assert segment == "observations-2026-03.jsonl"


def test_default_append_recovers_only_empty_future_segments(jr, monkeypatch):
    """A restored empty future segment must not wedge every normal writer."""
    journal_dir = jr._cctally_core.JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / MAR_SEGMENT).touch()
    (journal_dir / APR_SEGMENT).touch()

    _pin_clock(jr, monkeypatch, FEBRUARY)
    segment, offset = jr.append_record(_obs(2.0))

    assert segment == FEB_SEGMENT
    assert offset == len(jl.encode_line(_obs(2.0)))
    assert (journal_dir / FEB_SEGMENT).read_bytes() == jl.encode_line(_obs(2.0))
    assert not (journal_dir / MAR_SEGMENT).exists()
    assert not (journal_dir / APR_SEGMENT).exists()


def test_group_append_recovers_empty_future_segments_too(jr, monkeypatch):
    journal_dir = jr._cctally_core.JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / MAR_SEGMENT).touch()

    _pin_clock(jr, monkeypatch, FEBRUARY)
    segment, offset = jr.append_records(_correction_group())

    expected = b"".join(jl.encode_line(record) for record in _correction_group())
    assert segment == FEB_SEGMENT
    assert offset == len(expected)
    assert (journal_dir / FEB_SEGMENT).read_bytes() == expected
    assert not (journal_dir / MAR_SEGMENT).exists()


def test_data_bearing_future_segment_refuses_without_partial_cleanup(
    jr, monkeypatch
):
    """One durable future byte makes the whole recovery decision fail closed."""
    journal_dir = jr._cctally_core.JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / MAR_SEGMENT).touch()
    future_bytes = jl.encode_line(_obs(99.0))
    (journal_dir / APR_SEGMENT).write_bytes(future_bytes)

    _pin_clock(jr, monkeypatch, FEBRUARY)
    with pytest.raises(jr.JournalAppendTargetStale) as exc_info:
        jr.append_record(_obs(2.0))

    message = str(exc_info.value)
    assert APR_SEGMENT in message
    assert "automatic recovery is limited to empty regular files" in message
    assert "merge its records forward" in message
    assert (journal_dir / MAR_SEGMENT).exists()
    assert (journal_dir / APR_SEGMENT).read_bytes() == future_bytes
    assert not (journal_dir / FEB_SEGMENT).exists()


@pytest.mark.parametrize(
    "future_name",
    [
        "observations-٢٠٢٦-٠٣.jsonl",
        "observations-²⁰²⁶-⁰³.jsonl",
    ],
)
def test_non_ascii_future_segment_names_fail_closed(
    jr, monkeypatch, future_name
):
    journal_dir = jr._cctally_core.JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)
    future_path = journal_dir / future_name
    future_path.touch()

    _pin_clock(jr, monkeypatch, FEBRUARY)
    with pytest.raises(jr.JournalAppendTargetStale) as exc_info:
        jr.append_record(_obs(2.0))

    assert future_name in str(exc_info.value)
    assert "segment name is not a later UTC month" in str(exc_info.value)
    assert future_path.exists()
    assert not (journal_dir / FEB_SEGMENT).exists()


def test_non_regular_future_segment_fails_closed(jr, monkeypatch):
    journal_dir = jr._cctally_core.JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)
    future_path = journal_dir / MAR_SEGMENT
    future_path.mkdir()

    _pin_clock(jr, monkeypatch, FEBRUARY)
    with pytest.raises(jr.JournalAppendTargetStale) as exc_info:
        jr.append_record(_obs(2.0))

    assert "path is not a regular file" in str(exc_info.value)
    assert future_path.is_dir()
    assert not (journal_dir / FEB_SEGMENT).exists()


def test_future_segment_removal_is_fsynced_before_target_creation(
    jr, monkeypatch
):
    journal_dir = jr._cctally_core.JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)
    future_path = journal_dir / MAR_SEGMENT
    future_path.touch()

    def fail_fsync(path):
        assert path == journal_dir
        raise OSError("fsync refused")

    monkeypatch.setattr(jr, "_fsync_dir", fail_fsync)
    _pin_clock(jr, monkeypatch, FEBRUARY)
    with pytest.raises(OSError, match="fsync refused"):
        jr.append_record(_obs(2.0))

    assert not future_path.exists()
    assert not (journal_dir / FEB_SEGMENT).exists()


def test_future_segment_unlink_runs_under_the_leaf_lock(jr, monkeypatch):
    journal_dir = jr._cctally_core.JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)
    future_path = journal_dir / MAR_SEGMENT
    future_path.touch()
    real_unlink = pathlib.Path.unlink
    lock_probe_results = []

    def probe_lock_then_unlink(path, *args, **kwargs):
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl, os, sys\n"
                    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
                    "try:\n"
                    "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                    "except BlockingIOError:\n"
                    "    raise SystemExit(75)\n"
                    "raise SystemExit(0)\n"
                ),
                str(jr._cctally_core.JOURNAL_LOCK_PATH),
            ],
            check=False,
        )
        lock_probe_results.append(probe.returncode)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", probe_lock_then_unlink)
    _pin_clock(jr, monkeypatch, FEBRUARY)
    jr.append_record(_obs(2.0))

    assert lock_probe_results == [75]


def test_a_bootstrap_segment_never_blocks_an_observation_append(jr, monkeypatch):
    """Bootstraps sort BEFORE observations, so one is never canonically last."""
    journal_dir = jr._cctally_core.JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "bootstrap-20260101T000000_000000.jsonl").write_bytes(b"{}\n")
    _pin_clock(jr, monkeypatch, FEBRUARY)
    segment, _offset = jr.append_record(_obs(1.0))
    assert segment == FEB_SEGMENT


def test_a_dedupe_skip_returns_before_validation(jr, monkeypatch):
    """A deduped quota obs writes nothing, so it has no target to validate."""
    jr.append_record(_quota_obs(), now_utc=JANUARY, dedupe_codex_quota=True)
    jr.append_record(_obs(1.0), now_utc=FEBRUARY)
    _pin_clock(jr, monkeypatch, JANUARY)
    assert jr.append_record(_quota_obs(), dedupe_codex_quota=True) is None


def test_the_quota_caller_reraises_the_refusal_instead_of_swallowing(
    jr, tmp_path, monkeypatch
):
    """`_append_codex_quota_obs` is best-effort for genuine errors only.

    This one is retryable: swallowing it advances the file offset past bytes
    whose observation was never journaled, and the rollout JSONL those bytes
    came from evaporates. Re-raising leaves the offset where it was, so the
    next sync re-reads and re-appends.
    """
    cache = importlib.import_module("_cctally_cache")

    def refuse(*_args, **_kwargs):
        raise jr.JournalAppendTargetStale("stale target")

    monkeypatch.setattr(jr, "append_record", refuse)
    with pytest.raises(jr.JournalAppendTargetStale):
        cache._append_codex_quota_obs([_quota_row()])


def test_the_quota_caller_still_swallows_a_genuine_error(jr, monkeypatch, capsys):
    """Best-effort behaviour is withdrawn for ONE retryable condition only."""
    cache = importlib.import_module("_cctally_cache")

    def blow_up(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(jr, "append_record", blow_up)
    cache._append_codex_quota_obs([_quota_row()])
    assert "quota obs journal append failed" in capsys.readouterr().err


def _quota_row():
    """One `_append_codex_quota_obs` input row (18 positional fields)."""
    return (
        "codex", "root-a", "/tmp/codex/sessions/a.jsonl", 0,
        "2026-01-31T23:59:59Z", "2026-01-31T23:00:00Z", "weekly",
        "limit-1", "weekly", 10080, 5.0, "2026-02-07T00:00:00Z", "pro",
        None, None, "gpt-5", None, None,
    )

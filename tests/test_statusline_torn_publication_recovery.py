"""#825/#832 fix round D, finding 825-832-CD2-001: a torn publication write is
not a durable observation.

A process dies mid-append after the observation's bytes — publication id
included — reached disk but before its terminating newline. The ingester never
yields that incomplete final line, and the next append's torn-tail repair
truncates it. Recovery must therefore recognize only a COMPLETE,
newline-terminated, decodable observation carrying the plan's publication
identity and binding; anything less is recovered from the saved plan, so the
reading is recorded exactly once.
"""
from __future__ import annotations

import pytest

import _outcome_quarantine_trace as T
import test_outcome_quarantine_control_f15 as F15

WEEK = F15.WEEK
T0 = F15.T0
#: Past every spool candidate's 90-second active window.
CANDIDATE_EXPIRY = 200


@pytest.fixture
def app(monkeypatch, tmp_path):
    return T.make_app(monkeypatch, tmp_path)


class _ProcessDied(BaseException):
    """The appending process is killed: nothing after the write runs."""


def _complete_observations(publication_id):
    """Decoded, newline-terminated observations carrying ``publication_id``."""
    import _cctally_core as core
    import _cctally_journal as jr
    import _lib_journal as J
    out = []
    for segment in jr.list_segments():
        data = (core.JOURNAL_DIR / segment).read_bytes()
        complete = data[:data.rfind(b"\n") + 1]
        for line in complete.splitlines():
            rec = J.decode_line(line)
            payload = (rec or {}).get("payload") or {}
            if (rec and rec.get("t") == "obs"
                    and payload.get("publication_id") == publication_id):
                out.append(rec)
    return out


def test_a_torn_publication_is_recovered_from_its_saved_plan(app, monkeypatch):
    import _cctally_journal as jr
    import _cctally_statusline as sl
    import _lib_journal as J
    clock = F15.Clock(app, monkeypatch, T0)
    F15._seed(app, clock, "A", 10, week_reset=WEEK)
    real_write = jr._write_all
    torn = []

    def tear(fd, data):
        if b'"publication_id"' in data and not torn:
            torn.append(data)
            # Everything but the terminating newline reaches the disk.
            real_write(fd, data[:-1])
            raise _ProcessDied()
        return real_write(fd, data)

    monkeypatch.setattr(jr, "_write_all", tear)
    for _ in range(6):
        try:
            F15._drive(app, clock, "A", (("one", 20),), WEEK, 1)
        except _ProcessDied:
            break
    monkeypatch.setattr(jr, "_write_all", real_write)
    assert torn, "a publication reached the journal append"
    publication_id = J.decode_line(torn[0][:-1])["payload"]["publication_id"]
    saved = sl._read_transition(F15._key("A"), now_epoch=clock.value)
    assert saved.publication["publicationId"] == publication_id
    assert _complete_observations(publication_id) == []

    # Recover after the candidate expired, with no intervening append.
    clock.advance(CANDIDATE_EXPIRY)
    sl._statusline_reduce_and_publish(nudge_sink=lambda: None)
    # A later append runs the torn-tail repair.
    jr.append_record(J.make_op(at=T.iso(clock.value), src="test",
                               payload={"kind": "test_unrelated_append"}))

    assert len(_complete_observations(publication_id)) == 1, (
        "the reading the torn write lost was never recovered")
    assert F15._weekly(app, "A") == 20
    assert sl._read_transition(
        F15._key("A"), now_epoch=clock.value).publication is None

"""#279 S2 F1: Codex-side parse-health counter taxonomy."""
import importlib.util
import io
import json
import pathlib
import sys

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
spec = importlib.util.spec_from_file_location("_lib_jsonl", BIN / "_lib_jsonl.py")
_jsonl = importlib.util.module_from_spec(spec)
sys.modules["_lib_jsonl"] = _jsonl
spec.loader.exec_module(_jsonl)


def _line(obj):
    return json.dumps(obj) + "\n"


_UNSET = object()  # sentinel so info_override=None can be applied explicitly


def _token_count(ts, total, last_total, info_override=_UNSET):
    info = {"total_token_usage": {"total_tokens": total},
            "last_token_usage": {"input_tokens": 1, "output_tokens": 1,
                                 "total_tokens": last_total}}
    if info_override is not _UNSET:
        info = info_override
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "token_count", "info": info}}


def _drain(text):
    state = _jsonl._CodexIterState()
    fh = io.StringIO(text)
    rows = list(_jsonl._iter_codex_jsonl_entries_with_offsets(
        fh, "/tmp/rollout-2026-07-01T00-00-00-"
            "01234567-89ab-cdef-0123-456789abcdef.jsonl", state=state))
    return rows, state


def test_codex_counter_taxonomy():
    text = (
        _line({"type": "session_meta", "payload": {"id": "sess-1"}})
        + _line(_token_count("2026-07-01T10:00:00Z", 10, 10))       # healthy
        + "{ not json\n"                                             # malformed
        + _line(_token_count("2026-07-01T10:00:01Z", 10, 10))       # dedup skip — normal
        + _line(_token_count("2026-07-01T10:00:02Z", 20, 10,
                             info_override=None))                    # info None — normal
        + _line(_token_count("2026-07-01T10:00:03Z", 20, 10,
                             info_override="bogus"))                 # info-non-dict
        + _line(_token_count("2026-07-01T10:00:04Z", 30, 10,
                             info_override={"total_token_usage":
                                            {"total_tokens": 30}}))  # no-last-token-usage
        + _line(_token_count("not-a-date", 40, 10))                  # bad-timestamp
    )
    rows, state = _drain(text)
    assert len(rows) == 1
    assert state.lines_seen == 8
    assert state.lines_malformed == 1
    assert state.token_events_skipped == 3
    assert state.skip_reasons == {"info-non-dict": 1,
                                  "no-last-token-usage": 1,
                                  "bad-timestamp": 1}


def test_no_session_id_counted():
    # No session_meta AND no filename UUID -> the row is dropped and counted.
    state = _jsonl._CodexIterState()
    fh = io.StringIO(_line(_token_count("2026-07-01T10:00:00Z", 10, 10)))
    rows = list(_jsonl._iter_codex_jsonl_entries_with_offsets(
        fh, "/tmp/not-a-rollout-name.jsonl", state=state))
    assert rows == []
    assert state.token_events_skipped == 1
    assert state.skip_reasons == {"no-session-id": 1}

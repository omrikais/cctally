"""Task 1 — pure journal kernel (bin/_lib_journal.py).

I/O-free by construction: line encode/decode/validate, id derivation
(content, bootstrap, evt natural-key forms), segment naming + canonical
ordering, and the torn-tail scan. Every assertion here maps to the
Task 1 Interfaces block in
docs/superpowers/plans/2026-07-22-db-journal-redesign.md and the line
format in the design spec §4.2 / canonical order §4.1.
"""
import datetime as dt

import _lib_journal as J


# --------------------------------------------------------------------------
# encode / decode
# --------------------------------------------------------------------------

def test_encode_line_is_canonical_compact_json_with_newline():
    raw = J.encode_line({"t": "obs", "b": 2, "a": 1})
    # compact separators, sorted keys, trailing newline
    assert raw == b'{"a":1,"b":2,"t":"obs"}\n'
    assert raw.endswith(b"\n")
    assert b", " not in raw and b": " not in raw  # no spaces after separators


def test_encode_decode_round_trip_including_non_ascii():
    rec = {"t": "obs", "at": "2026-07-22T20:14:07Z", "src": "statusline",
           "provider": "claude", "payload": {"project": "café — déjà", "n": 5}}
    raw = J.encode_line(rec)
    # non-ASCII preserved literally (ensure_ascii=False), not \uXXXX-escaped
    assert "café".encode("utf-8") in raw
    back = J.decode_line(raw)
    assert back == rec


def test_decode_line_returns_none_on_truncated_json():
    assert J.decode_line(b'{"t":"obs"') is None


def test_decode_line_returns_none_on_non_object():
    assert J.decode_line(b'[1,2,3]') is None
    assert J.decode_line(b'42') is None
    assert J.decode_line(b'"just a string"') is None


def test_decode_line_returns_none_when_t_missing_or_not_string():
    assert J.decode_line(b'{"at":"x"}') is None
    assert J.decode_line(b'{"t":5}') is None
    assert J.decode_line(b'{"t":null}') is None


def test_decode_line_tolerates_unknown_keys_and_unknown_t():
    obj = J.decode_line(b'{"t":"future-kind","zz":1,"payload":{}}')
    assert obj == {"t": "future-kind", "zz": 1, "payload": {}}


# --------------------------------------------------------------------------
# identity: content_id / bootstrap_id / evt_id
# --------------------------------------------------------------------------

def test_content_id_prefix_and_shape():
    cid = J.content_id({"t": "obs", "at": "x", "src": "s", "provider": "claude",
                        "payload": {"a": 1}})
    assert cid.startswith("o:")
    assert len(cid) == 2 + 16  # "o:" + 16 hex chars
    int(cid[2:], 16)  # the digest tail is valid hex


def test_content_id_stable_under_key_insertion_order():
    a = {"t": "obs", "at": "x", "src": "s", "provider": "claude", "payload": {"a": 1, "b": 2}}
    b = {"payload": {"b": 2, "a": 1}, "provider": "claude", "src": "s", "at": "x", "t": "obs"}
    assert J.content_id(a) == J.content_id(b)


def test_content_id_differs_for_different_payloads():
    base = {"t": "obs", "at": "x", "src": "s", "provider": "claude", "payload": {"a": 1}}
    other = {"t": "obs", "at": "x", "src": "s", "provider": "claude", "payload": {"a": 2}}
    assert J.content_id(base) != J.content_id(other)


def test_bootstrap_id_form():
    assert J.bootstrap_id("week_reset_events", 3) == "b:week_reset_events:3"


def test_evt_id_exact_natural_key_string():
    got = J.evt_id("pm", "2026-07-14T09:00:00Z", "b:week_reset_events:3", 57)
    assert got == "pm:2026-07-14T09:00:00Z:b:week_reset_events:3:57"


def test_evt_id_stringifies_non_string_parts():
    assert J.evt_id("fhb", 1000, True) == "fhb:1000:True"


# --------------------------------------------------------------------------
# make_obs / make_op / make_evt
# --------------------------------------------------------------------------

def test_make_obs_is_fully_formed_with_content_id():
    rec = J.make_obs(at="2026-07-22T20:14:07Z", src="statusline",
                     provider="claude", payload={"pct": 57})
    assert rec["v"] == J.LINE_VERSION
    assert rec["t"] == "obs"
    assert rec["src"] == "statusline"
    assert rec["provider"] == "claude"
    assert rec["payload"] == {"pct": 57}
    # id is the content digest over (t, at, src, provider, payload) — no v/id
    expected = J.content_id({"t": "obs", "at": "2026-07-22T20:14:07Z",
                             "src": "statusline", "provider": "claude",
                             "payload": {"pct": 57}})
    assert rec["id"] == expected


def test_make_op_has_no_provider_and_content_id():
    rec = J.make_op(at="2026-07-22T20:14:07Z", src="record-credit",
                    payload={"kind": "weekly_credit_floor"})
    assert rec["t"] == "op"
    assert "provider" not in rec
    assert rec["id"].startswith("o:")
    expected = J.content_id({"t": "op", "at": "2026-07-22T20:14:07Z",
                             "src": "record-credit",
                             "payload": {"kind": "weekly_credit_floor"}})
    assert rec["id"] == expected


def test_make_evt_carries_passed_id_default_rev_and_kind_in_payload():
    eid = J.evt_id("pm", "2026-07-14T09:00:00Z", "b:week_reset_events:3", 57)
    rec = J.make_evt(kind="percent_milestone", id=eid,
                     at="2026-07-22T20:14:07Z", payload={"cumulative_cost_usd": 1.5})
    assert rec["v"] == J.LINE_VERSION
    assert rec["t"] == "evt"
    assert rec["id"] == eid
    assert rec["rev"] == 0
    assert rec["src"] == "ingest"
    # kind lands in payload (the §5.3 fold-dispatch key)
    assert rec["payload"]["kind"] == "percent_milestone"
    assert rec["payload"]["cumulative_cost_usd"] == 1.5


def test_make_evt_rev_override_and_payload_not_mutated():
    payload = {"cumulative_cost_usd": 1.5}
    rec = J.make_evt(kind="percent_milestone", id="pm:x", at="t", payload=payload, rev=2)
    assert rec["rev"] == 2
    # caller's dict is not mutated by the kind injection
    assert "kind" not in payload


def test_make_records_round_trip_through_codec():
    for rec in (
        J.make_obs(at="t", src="statusline", provider="claude", payload={"a": 1}),
        J.make_op(at="t", src="record-credit", payload={"kind": "weekly_credit_floor"}),
        J.make_evt(kind="weekly_cost_snapshot", id="wcs:1", at="t", payload={"cost": 2}),
    ):
        assert J.decode_line(J.encode_line(rec)) == rec


# --------------------------------------------------------------------------
# segment naming + canonical ordering
# --------------------------------------------------------------------------

def test_segment_name_from_utc_month():
    now = dt.datetime(2026, 7, 22, 20, 14, 7, tzinfo=dt.timezone.utc)
    assert J.segment_name(now) == "observations-2026-07.jsonl"


def test_segment_name_converts_to_utc_before_flooring_month():
    # 2026-08-01 01:00 +03:00 == 2026-07-31 22:00 UTC → July segment
    tz = dt.timezone(dt.timedelta(hours=3))
    now = dt.datetime(2026, 8, 1, 1, 0, 0, tzinfo=tz)
    assert J.segment_name(now) == "observations-2026-07.jsonl"


def test_segment_sort_key_orders_bootstrap_before_observations():
    names = [
        "observations-2026-07.jsonl",
        "bootstrap-1700000000.jsonl",
        "observations-2026-06.jsonl",
        "bootstrap-1600000000.jsonl",
    ]
    ordered = sorted(names, key=J.segment_sort_key)
    assert ordered == [
        "bootstrap-1600000000.jsonl",
        "bootstrap-1700000000.jsonl",
        "observations-2026-06.jsonl",
        "observations-2026-07.jsonl",
    ]


# --------------------------------------------------------------------------
# torn-tail scan
# --------------------------------------------------------------------------

def test_valid_tail_offset_on_torn_tail_returns_past_last_newline():
    chunk = b'{"t":"obs"}\n{"t":"obs"}\ngarbage-no-nl'
    last_nl = chunk.rfind(b"\n")
    assert J.valid_tail_offset(chunk, 0) == last_nl + 1


def test_valid_tail_offset_on_clean_tail_returns_chunk_end():
    chunk = b'{"t":"obs"}\n{"t":"op"}\n'
    assert J.valid_tail_offset(chunk, 0) == len(chunk)


def test_valid_tail_offset_is_absolute_with_chunk_start():
    # a 64 KiB window that begins at file offset 4096
    chunk = b'aaa\nbbb'  # torn tail "bbb"
    assert J.valid_tail_offset(chunk, 4096) == 4096 + 4


def test_valid_tail_offset_no_newline_returns_chunk_start():
    assert J.valid_tail_offset(b'partial-first-line', 0) == 0


# --------------------------------------------------------------------------
# iter_decoded
# --------------------------------------------------------------------------

def test_iter_decoded_pairs_offsets_with_decode_results():
    lines = [
        (0, J.encode_line({"t": "obs", "a": 1})),
        (24, b'not json at all'),
        (60, J.encode_line({"t": "op", "b": 2})),
    ]
    out = list(J.iter_decoded(lines))
    assert out[0][0] == 0 and out[0][1] == {"t": "obs", "a": 1}
    assert out[1] == (24, None)
    assert out[2][0] == 60 and out[2][1] == {"t": "op", "b": 2}

"""Unit tests for _lib_jsonl.parse_cost_entry — the pure per-line cost parser
extracted in #138 so the fused single-pass sync walker can share it with the
streaming _iter_jsonl_entries_with_offsets reader (one json.loads per line)."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _lib_jsonl as lj


def _assistant_obj(**over):
    obj = {
        "type": "assistant",
        "requestId": "r1",
        "timestamp": "2026-06-01T00:00:00Z",
        "costUSD": 0.5,
        "message": {
            "role": "assistant", "id": "m1", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }
    obj.update(over)
    return obj


def test_parse_cost_entry_billable_assistant_returns_tuple():
    res = lj.parse_cost_entry(_assistant_obj(), "/p/a.jsonl")
    assert res is not None
    entry, msg_id, req_id = res
    assert isinstance(entry, lj.UsageEntry)
    assert msg_id == "m1"
    assert req_id == "r1"
    assert entry.model == "claude-opus-4-7"
    assert entry.cost_usd == 0.5
    assert entry.source_path == "/p/a.jsonl"
    assert entry.usage["output_tokens"] == 5
    assert entry.timestamp.tzinfo is not None  # tz-aware (UTC)


def test_parse_cost_entry_non_assistant_returns_none():
    assert lj.parse_cost_entry({"type": "user", "uuid": "u1"}, "/p/a.jsonl") is None


def test_parse_cost_entry_synthetic_model_dropped():
    obj = _assistant_obj()
    obj["message"]["model"] = "<synthetic>"
    assert lj.parse_cost_entry(obj, "/p/a.jsonl") is None


def test_parse_cost_entry_missing_usage_returns_none():
    obj = _assistant_obj()
    del obj["message"]["usage"]
    assert lj.parse_cost_entry(obj, "/p/a.jsonl") is None


def test_parse_cost_entry_missing_timestamp_returns_none():
    obj = _assistant_obj()
    del obj["timestamp"]
    assert lj.parse_cost_entry(obj, "/p/a.jsonl") is None


def test_parse_cost_entry_null_keys_pass_through_with_none_ids():
    """msg_id/req_id may be None (rare legacy/synthetic emissions). The cost
    parser must still return the entry — the caller routes None-keyed rows to a
    plain INSERT (partial UNIQUE index)."""
    obj = _assistant_obj()
    del obj["message"]["id"]
    del obj["requestId"]
    res = lj.parse_cost_entry(obj, "/p/a.jsonl")
    assert res is not None
    _entry, msg_id, req_id = res
    assert msg_id is None and req_id is None

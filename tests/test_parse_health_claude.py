"""#279 S2 F1: Claude-side parse-health counter taxonomy + passivity."""
import importlib.util
import json
import pathlib
import sys

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, BIN / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_jsonl = _load("_lib_jsonl", "_lib_jsonl.py")

HEALTHY = {
    "type": "assistant", "timestamp": "2026-07-01T10:00:00Z",
    "requestId": "req_1",
    "message": {"id": "msg_1", "model": "claude-opus-4-8",
                "usage": {"input_tokens": 5, "output_tokens": 7}},
}


def test_skip_reason_taxonomy():
    assert _jsonl.assistant_skip_reason(HEALTHY) is None
    assert _jsonl.assistant_skip_reason({"type": "user"}) is None       # not-assistant
    synth = json.loads(json.dumps(HEALTHY))
    synth["message"]["model"] = "<synthetic>"
    assert _jsonl.assistant_skip_reason(synth) is None                  # deliberate
    no_usage = json.loads(json.dumps(HEALTHY))
    del no_usage["message"]["usage"]
    assert _jsonl.assistant_skip_reason(no_usage) == "no-usage"
    no_model = json.loads(json.dumps(HEALTHY))
    del no_model["message"]["model"]
    assert _jsonl.assistant_skip_reason(no_model) == "no-model"
    bad_ts = json.loads(json.dumps(HEALTHY))
    bad_ts["timestamp"] = "not-a-date"
    assert _jsonl.assistant_skip_reason(bad_ts) == "bad-timestamp"
    missing_ts = json.loads(json.dumps(HEALTHY))
    del missing_ts["timestamp"]
    assert _jsonl.assistant_skip_reason(missing_ts) == "bad-timestamp"


def test_classified_core_consistent_with_parse_cost_entry():
    """parse_cost_entry(obj) is None <=> classify returns a reason."""
    shapes = [HEALTHY, {"type": "user"}, {"type": "assistant"},
              {"type": "assistant", "timestamp": "2026-07-01T10:00:00Z",
               "message": {"model": "m", "usage": {}}}]
    for obj in shapes:
        parsed, reason = _jsonl._classify_cost_entry(obj, "/tmp/x.jsonl")
        assert (parsed is None) != (reason is None)
        assert (_jsonl.parse_cost_entry(obj, "/tmp/x.jsonl") is None) == \
            (parsed is None)


def test_iter_sync_entries_counts(tmp_path):
    from conftest import load_script
    load_script()
    cache = sys.modules["_cctally_cache"]
    lines = [
        json.dumps(HEALTHY),
        "{ this is not json",
        json.dumps({"type": "user", "timestamp": "2026-07-01T10:00:01Z"}),
        json.dumps({**HEALTHY, "message": {"id": "m2", "model": "claude-opus-4-8"}}),  # no-usage
        "",
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(lines) + "\n")
    stats = cache.IngestStats()
    with open(p, "r") as fh:
        consumed = list(cache._iter_sync_entries(fh, str(p), stats=stats))
    assert stats.lines_seen == 4            # blank line not counted
    assert stats.lines_malformed == 1
    assert stats.assistant_lines_skipped == 1
    assert stats.skip_reasons == {"no-usage": 1}
    assert len(consumed) >= 1               # the healthy line still yields

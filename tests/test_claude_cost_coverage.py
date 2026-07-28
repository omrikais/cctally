"""Task A's smallest synthetic Claude billed-token coverage reproducer.

Claude Code can bill a title-generation request while retaining only an
``ai-title`` marker.  The marker has no model, request identity, or token
fields, so the transcript parser can account for the assistant request but
cannot reconstruct the title request.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _lib_jsonl as jsonl


def test_title_marker_cannot_fill_canonical_billed_token_gap(tmp_path):
    transcript = tmp_path / "session.jsonl"
    rows = [
        {
            "type": "assistant",
            "timestamp": "2026-07-26T01:00:00Z",
            "requestId": "request-main",
            "message": {
                "id": "message-main",
                "model": "claude-opus-5",
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 3064,
                    "cache_read_input_tokens": 0,
                },
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-07-26T01:00:01Z",
            "requestId": "request-main",
            "message": {
                "id": "message-main",
                "model": "claude-opus-5",
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 13,
                    "cache_creation_input_tokens": 3064,
                    "cache_read_input_tokens": 0,
                    "iterations": [
                        {
                            "input_tokens": 2,
                            "output_tokens": 13,
                            "cache_creation_input_tokens": 3064,
                            "cache_read_input_tokens": 0,
                        }
                    ],
                },
            },
        },
        {
            "type": "ai-title",
            "aiTitle": "Synthetic title",
            "sessionId": "session-1",
        },
    ]
    transcript.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    selected: dict[str, jsonl.UsageEntry] = {}
    unkeyed = jsonl._parse_usage_entries(
        transcript,
        dt.datetime(2026, 7, 26, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc),
        dedupe_map=selected,
    )
    transcript_tokens = sum(
        jsonl._entry_token_total(entry)
        for entry in [*selected.values(), *unkeyed]
    )

    assert len(selected) == 1
    assert unkeyed == []
    assert jsonl.parse_cost_entry(rows[-1], str(transcript)) is None

    # Independent synthetic canonical: retained Opus request (3,079) plus an
    # unretained Haiku title request (1,025). The selected assistant row keeps
    # its top-level usage only; adding iterations would double it.
    canonical_billed_tokens = 4104
    assert transcript_tokens == 3079
    assert canonical_billed_tokens - transcript_tokens == 1025

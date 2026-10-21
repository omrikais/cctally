"""Shared #968 fixtures: deterministic Codex rollouts and their zstd twins.

Compression goes through the PRODUCTION resolver, so the suite exercises the
backend cctally itself selects. The authoritative toolchain pins
`backports.zstd`, so a missing backend is a broken toolchain, never a skip.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import _lib_codex_rollout_container as rc  # noqa: E402


def zstd_bytes(data: bytes) -> bytes:
    backend = rc.resolve_zstd_backend()
    assert isinstance(backend, rc.ZstdBackend), (
        "the authoritative toolchain must provide a zstd backend "
        "(tests/requirements-dev.txt pins backports.zstd)")
    return backend.module.compress(data)


def write_rollout(path: pathlib.Path, records) -> list[int]:
    """Write records one JSON object per line; return the offset AFTER each line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    offsets: list[int] = []
    with open(path, "wb") as fh:
        for record in records:
            fh.write(json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n")
            offsets.append(fh.tell())
    return offsets


def compress_in_place(plain_path: pathlib.Path) -> pathlib.Path:
    """Replace X.jsonl with X.jsonl.zst holding the identical decoded bytes."""
    data = plain_path.read_bytes()
    target = plain_path.with_name(plain_path.name + ".zst")
    target.write_bytes(zstd_bytes(data))
    os.unlink(plain_path)
    return target


def token_records(count: int, *, session_id: str, model: str = "gpt-5",
                  start_minute: int = 2, cwd: str | None = None) -> list[dict]:
    meta = {"id": session_id}
    if cwd is not None:
        meta["cwd"] = cwd
    out = [
        {"timestamp": "2026-07-20T10:00:00.000Z", "type": "session_meta",
         "payload": meta},
        {"timestamp": "2026-07-20T10:00:01.000Z", "type": "turn_context",
         "payload": {"model": model}},
    ]
    cumulative = 0
    for index in range(count):
        cumulative += 100
        minute = start_minute + index
        out.append({
            "timestamp": f"2026-07-20T{10 + minute // 60:02d}:{minute % 60:02d}:00.000Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "last_token_usage": {
                    "input_tokens": 60, "cached_input_tokens": 0,
                    "output_tokens": 40, "reasoning_output_tokens": 0,
                    "total_tokens": 100},
                "total_token_usage": {"total_tokens": cumulative}}},
        })
    return out


def snapshot_tables(conn: sqlite3.Connection, tables, *,
                    exclude_columns=()) -> dict[str, list[tuple]]:
    """Every row of every named table, in rowid order, minus excluded columns."""
    out: dict[str, list[tuple]] = {}
    excluded = set(exclude_columns)
    for table in tables:
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                if row[1] not in excluded and f"{table}.{row[1]}" not in excluded]
        if not cols:
            out[table] = []
            continue
        out[table] = [tuple(r) for r in conn.execute(
            f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid")]
    return out


def conversation_records(count: int, *, session_id: str, label: str = "a",
                         cwd: str = "/tmp/proj-968",
                         model: str = "gpt-5") -> list[dict]:
    """A rollout that normalizes into conversation messages: session_meta,
    then ``count`` turns of user message, assistant message and token count."""
    base = dt.datetime(2026, 7, 20, 10, 0, tzinfo=dt.timezone.utc)

    def ts(seconds: int) -> str:
        return (base + dt.timedelta(seconds=seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")

    out = [{"timestamp": ts(0), "type": "session_meta", "payload": {
        "id": session_id, "session_id": session_id, "cwd": cwd,
        "model": model, "source": "cli", "thread_source": "user"}}]
    cumulative = 0
    for index in range(count):
        start = 1 + index * 10
        cumulative += 100
        out.extend([
            {"timestamp": ts(start), "type": "turn_context", "payload": {
                "model": model, "turn_id": f"turn-{label}-{index:04d}"}},
            {"timestamp": ts(start + 1), "type": "response_item", "payload": {
                "type": "message", "role": "user", "phase": "input",
                "content": [{"type": "input_text",
                             "text": f"Prompt {label}{index}"}]}},
            {"timestamp": ts(start + 2), "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "phase": "output",
                "content": [{"type": "output_text",
                             "text": f"Answer {label}{index}"}]}},
            {"timestamp": ts(start + 3), "type": "event_msg", "payload": {
                "type": "token_count", "info": {
                    "last_token_usage": {
                        "input_tokens": 60, "cached_input_tokens": 0,
                        "output_tokens": 40, "reasoning_output_tokens": 0,
                        "total_tokens": 100},
                    "total_token_usage": {"total_tokens": cumulative}}}},
        ])
    return out

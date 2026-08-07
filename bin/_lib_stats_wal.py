"""Bounded structural inspection of a SQLite WAL and its wal-index.

The WAL carries database pages. This module reads only file/header identity,
WAL frame headers, and wal-index page numbers; it never reads or returns frame
payloads. Callers decide whether their surrounding locks make the snapshot
authoritative enough for an operational decision.
"""

from __future__ import annotations

import os
import pathlib
import struct
import sys


_WAL_MAGICS = {0x377F0682, 0x377F0683}
_FIRST_PGNO_COUNT = 4062
_LATER_PGNO_COUNT = 4096
_SHM_REGION_BYTES = 32768
_MISMATCH_SAMPLE_MAX = 8
_FRAME_ANALYSIS_MAX = 16384

_INCOHERENT_VERDICTS = {
    "wal_index_generation_mismatch",
    "wal_index_mapping_mismatch",
}


def _file_record(stat: os.stat_result) -> dict:
    return {
        "inode": int(stat.st_ino),
        "sizeBytes": int(stat.st_size),
        "mtimeNs": int(stat.st_mtime_ns),
    }


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
    )


def _read_at(handle, offset: int, size: int) -> bytes:
    """Read one bounded structural range (test seam for byte accounting)."""
    handle.seek(offset)
    return handle.read(size)


def _native_u16(raw: bytes, offset: int) -> int:
    prefix = "<" if sys.byteorder == "little" else ">"
    return int(struct.unpack_from(f"{prefix}H", raw, offset)[0])


def _native_u32(raw: bytes, offset: int) -> int:
    prefix = "<" if sys.byteorder == "little" else ">"
    return int(struct.unpack_from(f"{prefix}I", raw, offset)[0])


def _shm_page_offset(frame_number: int) -> int:
    index = frame_number - 1
    if index < _FIRST_PGNO_COUNT:
        return 136 + index * 4
    index -= _FIRST_PGNO_COUNT
    region = 1 + index // _LATER_PGNO_COUNT
    return region * _SHM_REGION_BYTES + (index % _LATER_PGNO_COUNT) * 4


def _empty_evidence(verdict: str, reason: str | None = None) -> dict:
    return {
        "schemaVersion": 1,
        "verdict": verdict,
        "captureStable": False,
        "reason": reason,
        "wal": None,
        "shm": None,
        "frameMapping": {
            "comparedCount": 0,
            "mismatchCount": 0,
            "mismatchSample": [],
            "truncated": False,
        },
    }


def _path_matches(path: pathlib.Path, opened: os.stat_result) -> bool:
    try:
        return _same_file(path.stat(), opened)
    except OSError:
        return False


def _inspect_zero_wal(wal_path: pathlib.Path, shm_path: pathlib.Path) -> dict:
    result = _empty_evidence("wal_empty")
    try:
        with wal_path.open("rb") as wal_handle:
            wal_before = os.fstat(wal_handle.fileno())
            result["wal"] = _file_record(wal_before)
            if wal_before.st_size != 0:
                result["verdict"] = "capture_raced"
                result["reason"] = "WAL refilled before zero-WAL capture"
                return result
            try:
                with shm_path.open("rb") as shm_handle:
                    shm_before = os.fstat(shm_handle.fileno())
                    result["shm"] = _file_record(shm_before)
                    wal_after = os.fstat(wal_handle.fileno())
                    shm_after = os.fstat(shm_handle.fileno())
                    result["captureStable"] = (
                        _same_file(wal_before, wal_after)
                        and _same_file(shm_before, shm_after)
                        and _path_matches(wal_path, wal_after)
                        and _path_matches(shm_path, shm_after)
                    )
            except FileNotFoundError:
                # Absence cannot be proven as one stable pair on the unlocked
                # forensic path. The locked cutover still treats a zero WAL as
                # safe, but the evidence does not overclaim capture stability.
                result["captureStable"] = False
            except OSError as exc:
                result["reason"] = f"shm open failed: {exc}"[:200]
    except OSError as exc:
        return _empty_evidence("capture_raced", str(exc)[:200])
    if not result["captureStable"] and result["shm"] is not None:
        result["verdict"] = "capture_raced"
        result["reason"] = "WAL or SHM identity changed during capture"
    return result


def inspect_wal_index_family(db_path) -> dict:
    """Return bounded WAL/SHM coherence evidence without opening SQLite."""
    db_path = pathlib.Path(db_path)
    wal_path = pathlib.Path(f"{db_path}-wal")
    shm_path = pathlib.Path(f"{db_path}-shm")
    try:
        wal_stat = wal_path.stat()
    except FileNotFoundError:
        return _empty_evidence("wal_absent")
    except OSError as exc:
        return _empty_evidence("unavailable", f"wal stat failed: {exc}"[:200])
    if wal_stat.st_size == 0:
        return _inspect_zero_wal(wal_path, shm_path)
    try:
        shm_path.stat()
    except FileNotFoundError:
        result = _empty_evidence("shm_absent")
        result["wal"] = _file_record(wal_stat)
        return result
    except OSError as exc:
        return _empty_evidence("unavailable", f"shm stat failed: {exc}"[:200])

    result = _empty_evidence("unavailable")
    try:
        with wal_path.open("rb") as wal_handle, shm_path.open("rb") as shm_handle:
            wal_before = os.fstat(wal_handle.fileno())
            shm_before = os.fstat(shm_handle.fileno())
            result["wal"] = _file_record(wal_before)
            result["shm"] = _file_record(shm_before)
            wal_header = _read_at(wal_handle, 0, 32)
            shm_header = _read_at(shm_handle, 0, 136)
            if len(wal_header) < 32 or len(shm_header) < 136:
                result["reason"] = "WAL or SHM header is truncated"
            else:
                _populate_evidence(
                    result, wal_handle, shm_handle, wal_header, shm_header,
                    wal_before, shm_before,
                )
            wal_header_after = _read_at(wal_handle, 0, 32)
            shm_header_after = _read_at(shm_handle, 0, 136)
            wal_after = os.fstat(wal_handle.fileno())
            shm_after = os.fstat(shm_handle.fileno())
            stable = (
                _same_file(wal_before, wal_after)
                and _same_file(shm_before, shm_after)
                and _path_matches(wal_path, wal_after)
                and _path_matches(shm_path, shm_after)
                and wal_header == wal_header_after
                and shm_header == shm_header_after
            )
    except OSError as exc:
        return _empty_evidence("capture_raced", str(exc)[:200])

    result["captureStable"] = stable
    if not stable:
        result["verdict"] = "capture_raced"
        result["reason"] = "WAL or SHM identity changed during capture"
    return result


def _populate_evidence(
    result: dict,
    wal_handle,
    shm_handle,
    wal_header: bytes,
    shm_header: bytes,
    wal_stat: os.stat_result,
    shm_stat: os.stat_result,
) -> None:
    magic = struct.unpack_from(">I", wal_header, 0)[0]
    page_size_raw = struct.unpack_from(">I", wal_header, 8)[0]
    page_size = 65536 if page_size_raw == 1 else int(page_size_raw)
    if magic not in _WAL_MAGICS or page_size < 512 or page_size > 65536:
        result["reason"] = "WAL header magic or page size is invalid"
        return

    frame_bytes = 24 + page_size
    physical_frames = max(0, (wal_stat.st_size - 32) // frame_bytes)
    trailing_bytes = max(0, (wal_stat.st_size - 32) % frame_bytes)
    wal_salt_bytes = wal_header[16:24]
    wal_salt = wal_salt_bytes.hex()
    shm_salt = shm_header[32:40].hex()
    shm_page_size_raw = _native_u16(shm_header, 14)
    shm_page_size = 65536 if shm_page_size_raw == 1 else shm_page_size_raw
    mx_frame = _native_u32(shm_header, 16)
    scan_limit = min(physical_frames, _FRAME_ANALYSIS_MAX)
    current_generation_frames = 0
    current_generation_commit_frames = 0
    first_stale_tail_frame = None
    frame_headers: dict[int, bytes] = {}
    for frame_number in range(1, scan_limit + 1):
        offset = 32 + (frame_number - 1) * frame_bytes
        frame_header = _read_at(wal_handle, offset, 24)
        if len(frame_header) < 24:
            result["reason"] = "WAL frame header is truncated"
            return
        frame_headers[frame_number] = frame_header
        if frame_header[8:16] != wal_salt_bytes:
            first_stale_tail_frame = frame_number
            break
        current_generation_frames = frame_number
        if struct.unpack_from(">I", frame_header, 4)[0] != 0:
            current_generation_commit_frames = frame_number

    analysis_truncated = (
        first_stale_tail_frame is None
        and physical_frames > _FRAME_ANALYSIS_MAX
    ) or mx_frame > _FRAME_ANALYSIS_MAX
    compared_limit = min(
        mx_frame, current_generation_commit_frames, _FRAME_ANALYSIS_MAX
    )
    mismatch_count = 0
    mismatch_sample = []
    compared_count = 0
    mapping_incomplete = False
    for frame_number in range(1, compared_limit + 1):
        frame_header = frame_headers.get(frame_number)
        if frame_header is None:
            offset = 32 + (frame_number - 1) * frame_bytes
            frame_header = _read_at(wal_handle, offset, 24)
        shm_offset = _shm_page_offset(frame_number)
        if shm_offset + 4 > shm_stat.st_size:
            mapping_incomplete = True
            break
        shm_page_raw = _read_at(shm_handle, shm_offset, 4)
        if len(shm_page_raw) < 4:
            mapping_incomplete = True
            break
        wal_page = int(struct.unpack_from(">I", frame_header, 0)[0])
        shm_page = _native_u32(shm_page_raw, 0)
        compared_count += 1
        if wal_page != shm_page:
            mismatch_count += 1
            if len(mismatch_sample) < _MISMATCH_SAMPLE_MAX:
                mismatch_sample.append({
                    "frame": frame_number,
                    "walPage": wal_page,
                    "shmPage": shm_page,
                })

    header_copies_match = shm_header[:48] == shm_header[48:96]
    result["wal"].update({
        "magicHex": f"{magic:08x}",
        "saltHex": wal_salt,
        "pageSize": page_size,
        "physicalFrameCount": physical_frames,
        "trailingBytes": trailing_bytes,
        "currentGenerationFrameCount": current_generation_frames,
        "currentGenerationCommitFrameCount": current_generation_commit_frames,
        "stalePhysicalTailFrameCount": max(
            0, physical_frames - current_generation_frames
        ),
    })
    result["shm"].update({
        "headerCopiesMatch": header_copies_match,
        "saltHex": shm_salt,
        "pageSize": shm_page_size,
        "mxFrame": mx_frame,
        "nPage": _native_u32(shm_header, 20),
        "nBackfill": _native_u32(shm_header, 96),
        "nBackfillAttempted": _native_u32(shm_header, 128),
    })
    result["frameMapping"] = {
        "comparedCount": compared_count,
        "mismatchCount": mismatch_count,
        "mismatchSample": mismatch_sample,
        "truncated": analysis_truncated or mapping_incomplete,
    }

    if analysis_truncated:
        verdict = "analysis_truncated"
    elif wal_salt != shm_salt:
        verdict = "wal_index_generation_mismatch"
    elif (
        not header_copies_match
        or mismatch_count
        or mapping_incomplete
        or mx_frame != current_generation_commit_frames
        or shm_page_size != page_size
        or trailing_bytes != 0
    ):
        verdict = "wal_index_mapping_mismatch"
    else:
        verdict = "coherent"
    result["verdict"] = verdict
    result["reason"] = None


def is_incoherent_wal_index(evidence: dict) -> bool:
    return evidence.get("verdict") in _INCOHERENT_VERDICTS

"""Session detail modal fixture with deterministic injected detail
(spec §4.6.4 + §5.5).

Reuses the WARN underlay snapshot, then constructs a deterministic
TuiSessionDetail (matching the topmost session in the underlay) and
injects it via runtime.session_detail_override so the modal renders
without round-tripping through the live JSONL cache.
"""
import datetime as dt
import importlib.machinery
import importlib.util
import pathlib
import sys

# Load the underlying warn snapshot (which itself loads the main script
# module and exposes it as `m`).
_PATH = pathlib.Path(__file__).resolve().parent / "snapshot_warn.py"
_LOADER = importlib.machinery.SourceFileLoader("_warn_underlay_se", str(_PATH))
_SPEC = importlib.util.spec_from_loader("_warn_underlay_se", _LOADER)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["_warn_underlay_se"] = _MOD
_LOADER.exec_module(_MOD)

# `_MOD.m` is the main cctally script module loaded
# by snapshot_warn.py; we reach into it for TuiSessionDetail so the
# dataclass identity matches what the renderer references.
_SCRIPT = _MOD.m

_DETAIL = _SCRIPT.TuiSessionDetail(
    session_id="7f3a2b89-4c1e-49a1-a000-000000000001",
    started_at=dt.datetime(2026, 4, 20, 14, 38, 2, tzinfo=dt.timezone.utc),
    last_activity_at=dt.datetime(2026, 4, 20, 15, 20, 41, tzinfo=dt.timezone.utc),
    duration_minutes=42.65,
    project_label="subscription-stats",
    project_path="/home/user/projects/subscription-stats",
    source_paths=["~/.claude/projects/abc/session.jsonl"],
    models=[("sonnet-4.5", "primary")],
    input_tokens=12847,
    cache_creation_tokens=8251,
    cache_read_tokens=142038,
    output_tokens=18492,
    cache_hit_pct=87.0,
    cost_per_model=[("sonnet-4.5", 1.84)],
    cost_total_usd=1.84,
)
SNAPSHOT = _MOD.SNAPSHOT
RUNTIME_OVERRIDES = {"modal_kind": "session", "session_detail_override": _DETAIL}

#!/usr/bin/env python3
"""Manage the single auto-tracked `pricing-drift` GitHub issue (spec §5.4).

Reads a `cctally pricing-check --json` payload, decides create / update /
close / noop via the pure `pricing_issue_action` kernel, and runs the
matching `gh` command. The decision logic lives in the unit-tested kernel
(`bin/_lib_pricing_check.pricing_issue_action`); this script is the thin
`gh` shell around it.

drift_present := any `value_drift` OR any `missing_from_us`. (The existence
leg never runs in the cron — no OAuth — and `ahead_of_litellm` is
informational only, never actionable; spec invariant #2. Offline coverage
gaps are a LOCAL signal, surfaced by `doctor`, not the cron's job.)

Usage:
  pricing_issue.py <payload.json>            # live: queries + mutates via gh
  pricing_issue.py --dry-run <payload.json>  # print the intended action only

Environment:
  GH_REPO   target repo for every `gh` op (set by the workflow to the
            private repo so issue ops can't hit the public mirror).
  GH_TOKEN  gh reads its auth from here (set by the workflow).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

# Import the pure decision kernel the same way bin/cctally re-exports it.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "bin"))
import _lib_pricing_check  # noqa: E402

ISSUE_LABEL = "pricing-drift"
ISSUE_TITLE = "Pricing drift: embedded tables diverge from LiteLLM"


def _drift_present(payload: dict) -> bool:
    """True iff the payload carries an actionable LiteLLM-drift finding.

    Deliberately ignores `existence.unpriced_vendor_models`: the cron has no
    OAuth, so the `/v1/models` existence leg always auto-degrades and reports
    nothing here. If CI is ever granted an OAuth bearer, revisit this — an
    existence-only finding would otherwise go un-tracked by the drift issue.
    """
    drift = payload.get("drift") or {}
    return bool(drift.get("value_drift")) or bool(drift.get("missing_from_us"))


def _run_gh(args: list[str], *, capture: bool = False) -> str:
    """Run `gh <args>`. Returns stdout when capture=True; raises on failure."""
    cmd = ["gh", *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"[pricing_issue] `{' '.join(cmd)}` failed (rc={proc.returncode}):\n"
            f"{proc.stderr}\n"
        )
        raise SystemExit(proc.returncode)
    return proc.stdout


def _find_open_issue() -> int | None:
    """Return the number of the open `pricing-drift` issue, or None."""
    out = _run_gh(
        ["issue", "list", "--label", ISSUE_LABEL, "--state", "open",
         "--json", "number", "--limit", "1"],
        capture=True,
    )
    rows = json.loads(out or "[]")
    return rows[0]["number"] if rows else None


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def _build_body(payload: dict) -> str:
    """Living-ledger issue body: current drift state + remediation checklist.

    The body is REWRITTEN on every update (it reflects the latest run, not a
    history), matching the migration-error-sentinel discipline.
    """
    drift = payload.get("drift") or {}
    value_drift = drift.get("value_drift") or []
    missing = drift.get("missing_from_us") or []
    ahead = drift.get("ahead_of_litellm") or []
    snapshot = payload.get("snapshotDate", "?")
    source = payload.get("litellmSource", "LiteLLM")
    status = payload.get("status", "?")
    degraded = payload.get("degraded_components") or []

    lines: list[str] = []
    lines.append(
        "cctally's embedded model pricing diverges from the LiteLLM "
        "snapshot. This issue is auto-managed by the weekly "
        "`pricing-freshness` workflow — it is rewritten on each run and "
        "**auto-closed** when the drift clears."
    )
    lines.append("")
    lines.append(f"- Embedded snapshot date: `{snapshot}`")
    lines.append(f"- Source: {source}")
    lines.append(f"- Last run: {_today()} (check status: `{status}`)")
    if degraded:
        lines.append(
            f"- Degraded legs this run: `{', '.join(degraded)}` "
            "(reported for completeness; the findings below still stand)"
        )
    lines.append("")

    if value_drift:
        lines.append("### Value drift (shared model, price field differs)")
        lines.append("")
        lines.append("| Model | Field | Ours | LiteLLM |")
        lines.append("| --- | --- | --- | --- |")
        for row in value_drift:
            lines.append(
                f"| `{row.get('model')}` | `{row.get('field')}` "
                f"| {row.get('ours')} | {row.get('theirs')} |"
            )
        lines.append("")

    if missing:
        lines.append("### Missing from us (LiteLLM prices a model we don't)")
        lines.append("")
        for model in missing:
            lines.append(f"- `{model}`")
        lines.append("")

    if ahead:
        lines.append(
            "### Ahead of LiteLLM (informational — NOT actionable)"
        )
        lines.append("")
        lines.append(
            "Models we price that the scoped LiteLLM snapshot lacks. We may "
            "legitimately lead the source; listed for context only:"
        )
        lines.append("")
        for model in ahead:
            lines.append(f"- `{model}`")
        lines.append("")

    lines.append("### Remediation checklist")
    lines.append("")
    lines.append(
        "- [ ] Verify each drift against the vendor pricing page "
        "(LiteLLM lags — confirm before editing)."
    )
    lines.append(
        "- [ ] Update `CLAUDE_MODEL_PRICING` / `CODEX_MODEL_PRICING` in "
        "`bin/_lib_pricing.py` for genuine value changes."
    )
    lines.append(
        "- [ ] Add the new model to the right table for each "
        "`missing_from_us` entry (or add a `PRICING_DRIFT_ALLOWLIST` "
        "entry with a `reason` if the omission is deliberate)."
    )
    lines.append(
        "- [ ] If a value drift is intentional, add a "
        "`{model, field, reason}` `PRICING_DRIFT_ALLOWLIST` entry (the "
        "non-vacuity guard forces removal once the divergence resolves)."
    )
    lines.append(
        "- [ ] Bump `PRICING_SNAPSHOT_DATE` in `bin/_lib_pricing.py` after "
        "syncing."
    )
    lines.append("")
    lines.append(
        "_Auto-generated by `.github/workflows/pricing-freshness.yml`._"
    )
    return "\n".join(lines)


def _run_comment(payload: dict) -> str:
    drift = payload.get("drift") or {}
    nv = len(drift.get("value_drift") or [])
    nm = len(drift.get("missing_from_us") or [])
    return (
        f"Re-checked {_today()}: still drifting "
        f"({nv} value-drift field(s), {nm} missing-from-us model(s))."
    )


def _act(action: str, payload: dict, issue_number: int | None, *, dry_run: bool) -> None:
    if dry_run:
        target = f" (issue #{issue_number})" if issue_number is not None else ""
        print(f"pricing_issue: action={action}{target}")
        return

    if action == "create":
        body = _build_body(payload)
        # Ensure the label exists first — `gh issue create --label X` hard-fails
        # if X is absent, and `pricing-drift` is a machine-owned label that
        # nothing else creates. `--force` upserts (no-op if it already exists),
        # so this is idempotent across every run.
        _run_gh([
            "label", "create", ISSUE_LABEL, "--force",
            "--description", "Embedded pricing diverged from LiteLLM",
            "--color", "D93F0B",
        ])
        _run_gh([
            "issue", "create",
            "--title", ISSUE_TITLE,
            "--label", ISSUE_LABEL,
            "--body", body,
        ])
        print("pricing_issue: created pricing-drift issue")
    elif action == "update":
        assert issue_number is not None
        body = _build_body(payload)
        _run_gh([
            "issue", "edit", str(issue_number),
            "--body", body,
        ])
        _run_gh([
            "issue", "comment", str(issue_number),
            "--body", _run_comment(payload),
        ])
        print(f"pricing_issue: updated pricing-drift issue #{issue_number}")
    elif action == "close":
        assert issue_number is not None
        sha = os.environ.get("GITHUB_SHA", "")
        sha_note = f" {sha[:12]}" if sha else ""
        _run_gh([
            "issue", "close", str(issue_number),
            "--reason", "completed",
            "--comment",
            f"Pricing drift resolved as of {_today()}{sha_note}. "
            "Embedded tables match the LiteLLM snapshot again — "
            "auto-closed by the pricing-freshness workflow.",
        ])
        print(f"pricing_issue: closed pricing-drift issue #{issue_number}")
    else:  # noop
        print("pricing_issue: no drift, no open issue — nothing to do")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage the auto-tracked pricing-drift GitHub issue.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the intended action without calling gh.",
    )
    parser.add_argument(
        "payload", help="Path to a `pricing-check --json` payload file.",
    )
    args = parser.parse_args(argv)

    try:
        payload = json.loads(pathlib.Path(args.payload).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"[pricing_issue] cannot read payload: {exc}\n")
        return 2

    drift_present = _drift_present(payload)
    # In --dry-run we don't query gh for the open-issue state (no network /
    # auth assumed); model "no open issue" so the action is purely a function
    # of the payload. Live runs query the real state.
    issue_number = None if args.dry_run else _find_open_issue()
    existing_open = issue_number is not None

    action = _lib_pricing_check.pricing_issue_action(drift_present, existing_open)
    _act(action, payload, issue_number, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Manage the single auto-tracked `pricing-drift` GitHub issue (spec §5.4).

Reads a `cctally pricing-check --json` payload, decides create / update /
close / noop via the pure `pricing_issue_action` kernel, and runs the
matching `gh` command. The decision logic lives in the unit-tested kernel
(`bin/_lib_pricing_check.pricing_issue_action`); this script is the thin
`gh` shell around it.

findings_present := any `value_drift` OR `missing_from_us` OR
`staleSuppressions` OR `expiredSuppressions` (#279 S7 W7). (The existence
leg never runs in the cron — no OAuth — and `ahead_of_litellm` is
informational only, never actionable; spec invariant #2. Offline coverage
gaps are a LOCAL signal, surfaced by `doctor`, not the cron's job.)

Usage:
  pricing_issue.py <payload.json>            # live: queries + mutates via gh
  pricing_issue.py --dry-run <payload.json>  # print the intended action only
  pricing_issue.py --no-create-record        # record only that this run created nothing

Environment:
  GH_REPO   target repo for every `gh` op (set by the workflow to the
            private repo so issue ops can't hit the public mirror).
  GH_TOKEN  gh reads its auth from here (set by the workflow).
  GITHUB_RUN_ID / GITHUB_RUN_ATTEMPT / RUNNER_TEMP / GITHUB_OUTPUT
            the Actions run identity and step plumbing the per-creation
            provenance record uses (below).

Provenance (every live run attempt). A `github.token` creation triggers no
other workflow, so each run attempt leaves its own evidence of what it did,
and an audit of issue openings requires one from EVERY attempt. The script
writes `pricing-provenance.json` (workflow file, run id, run attempt,
`createdAt` and `result`), which the workflow uploads as the artifact
`pricing-provenance-attempt-<run attempt>`, so a re-run's record never shares
a name with an earlier attempt's. `result` is `created`, `updated` or
`no-create`. For a creation the script reads
the new issue's number from `gh issue create`, its node id from `gh issue
view`, and `createdAt` is the issue's creation time; an update records the
issue's number and node id the same way, and `createdAt` is when the record
was written; a close or a no-op records `no-create`, and so does a run that
stops before it ever asked to create an issue (`--no-create-record` covers a
run whose check failed before this script ran). A run that stops after asking
to create an issue records nothing it cannot confirm, so its missing record
stays visible. The body carries a hidden `cctally-provenance` marker naming
the creating run; the marker is only a pointer, and an update copies the
ORIGINAL marker verbatim into the rewritten body, never a new one.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys

# Import the pure decision kernel the same way bin/cctally re-exports it.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "bin"))
import _lib_pricing_check  # noqa: E402

ISSUE_LABEL = "pricing-drift"
ISSUE_TITLE = "Pricing drift: embedded tables diverge from LiteLLM"
ISSUE_MARKER = "<!-- cctally-pricing-drift-ledger:v1 -->"
PROVENANCE_WORKFLOW = "pricing-freshness.yml"
PROVENANCE_FILE = "pricing-provenance.json"
PROVENANCE_MARKER = "<!-- cctally-provenance: pricing-freshness run={run} attempt={attempt} -->"
PROVENANCE_RE = re.compile(r"<!--\s*cctally-provenance:[^>]*-->")


def _findings_present(payload: dict) -> bool:
    """True iff the payload carries an actionable finding the cron tracks:
    value drift, missing-from-us, OR (as of #279 S7 W7) a stale/expired
    allowlist suppression.

    Deliberately ignores `existence.unpriced_vendor_models`: the cron has no
    OAuth, so the `/v1/models` existence leg always auto-degrades and reports
    nothing here. If CI is ever granted an OAuth bearer, revisit this — an
    existence-only finding would otherwise go un-tracked by the drift issue.
    """
    drift = payload.get("drift") or {}
    return (
        bool(drift.get("value_drift"))
        or bool(drift.get("missing_from_us"))
        or bool(payload.get("staleSuppressions"))
        or bool(payload.get("expiredSuppressions"))
    )


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
    """Return the explicitly owned open pricing ledger, or None.

    The label is discovery metadata, not ownership: humans may use it on a
    manually filed pricing issue. Only a body carrying our durable marker may
    be rewritten or closed by this workflow.
    """
    out = _run_gh(
        ["issue", "list", "--label", ISSUE_LABEL, "--state", "open",
         "--json", "number,body", "--limit", "1000"],
        capture=True,
    )
    rows = json.loads(out or "[]")
    owned = [
        row for row in rows
        if (row.get("body") or "").startswith(ISSUE_MARKER + "\n")
    ]
    if len(owned) > 1:
        sys.stderr.write(
            "[pricing_issue] multiple marked pricing ledgers are open; "
            "refusing to choose one\n"
        )
        raise SystemExit(2)
    return owned[0]["number"] if owned else None


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
    lines.append(ISSUE_MARKER)
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

    stale = payload.get("staleSuppressions") or []
    expired = payload.get("expiredSuppressions") or []
    if stale or expired:
        lines.append("### Stale / expired suppressions")
        lines.append("")
        if stale:
            lines.append(
                "These `PRICING_DRIFT_ALLOWLIST` entries no longer map to a "
                "real divergence (LiteLLM now agrees / the model is present) — "
                "**remove them** so stale ignores can't accumulate:"
            )
            lines.append("")
            for e in stale:
                fld = f".`{e.get('field')}`" if e.get("field") else ""
                lines.append(f"- `{e.get('model')}`{fld}")
            lines.append("")
        if expired:
            lines.append(
                "These entries are past their `expires` cutover — **remove the "
                "allowlist entry / re-sync the embedded snapshot** so the "
                "deliberate divergence doesn't ossify past its stated date:"
            )
            lines.append("")
            for e in expired:
                fld = f".`{e.get('field')}`" if e.get("field") else ""
                lines.append(
                    f"- `{e.get('model')}`{fld} (expired {e.get('expires')})"
                )
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
    ns = len(payload.get("staleSuppressions") or [])
    ne = len(payload.get("expiredSuppressions") or [])
    return (
        f"Re-checked {_today()}: still open "
        f"({nv} value-drift field(s), {nm} missing-from-us model(s), "
        f"{ns} stale suppression(s), {ne} expired suppression(s))."
    )


def _provenance_marker() -> str | None:
    """The body marker naming THIS run, or None outside an Actions run."""
    run = os.environ.get("GITHUB_RUN_ID", "").strip()
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "").strip() or "1"
    if not run.isdigit() or not attempt.isdigit():
        return None
    return PROVENANCE_MARKER.format(run=run, attempt=attempt)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_record(result: str, *, issue: dict | None = None, created_at: str | None = None) -> None:
    """Write this run's `pricing-provenance` record: what it did, and to which issue.

    `issue` is the issue's `gh issue view --json id,number,createdAt` record
    (for `created` and `updated`). A missing record is a coverage gap for
    whoever audits openings, so every reason one could not be written is
    reported on stderr.
    """
    temp = os.environ.get("RUNNER_TEMP", "").strip()
    marker = _provenance_marker()
    if not temp or marker is None:
        sys.stderr.write(
            f"[pricing_issue] {result}, but no provenance record was written "
            f"(RUNNER_TEMP {'set' if temp else 'unset'}, run identity {'set' if marker else 'unset'})\n"
        )
        return
    record = {
        "schemaVersion": 1,
        "result": result,
        "workflow": PROVENANCE_WORKFLOW,
        "runId": int(os.environ["GITHUB_RUN_ID"]),
        "runAttempt": int(os.environ.get("GITHUB_RUN_ATTEMPT") or 1),
        "createdAt": created_at or _now(),
    }
    if issue is not None:
        record.update(issueNumber=int(issue["number"]), nodeId=issue["id"])
    path = pathlib.Path(temp) / PROVENANCE_FILE
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output = os.environ.get("GITHUB_OUTPUT", "").strip()
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"provenance={path}\n")
    target = f" for issue #{issue['number']}" if issue is not None else ""
    print(f"pricing_issue: provenance recorded ({result}){target} ({path})")


def _record_provenance(create_output: str) -> None:
    """Record a creation: the number from the `gh issue create` result, the node id and
    the creation time from the created issue itself."""
    match = re.search(r"/issues/(\d+)\s*$", create_output.strip())
    if match is None:
        sys.stderr.write("[pricing_issue] created, but no provenance record was written (issue url missing)\n")
        return
    number = int(match.group(1))
    view = json.loads(_run_gh(
        ["issue", "view", str(number), "--json", "id,number,createdAt"], capture=True,
    ))
    _write_record("created", issue=dict(view, number=number), created_at=view.get("createdAt"))


def _original_provenance_block(current: dict) -> str | None:
    """The provenance marker the issue was CREATED with, copied verbatim on update."""
    found = PROVENANCE_RE.search(current.get("body") or "")
    return found.group(0) if found else None


# Set just before this run asks GitHub to create an issue: from then on a failed
# run cannot say it created nothing, so it records nothing it cannot confirm.
_CREATE_REQUESTED = False


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
        marker = _provenance_marker()
        if marker is not None:
            body = f"{body}\n\n{marker}"
        global _CREATE_REQUESTED
        _CREATE_REQUESTED = True
        created = _run_gh([
            "issue", "create",
            "--title", ISSUE_TITLE,
            "--label", ISSUE_LABEL,
            "--body", body,
        ], capture=True)
        print("pricing_issue: created pricing-drift issue")
        _record_provenance(created or "")
    elif action == "update":
        assert issue_number is not None
        body = _build_body(payload)
        # The body is rewritten on every update, but its provenance is the
        # CREATING run's: copy that block verbatim, never regenerate it here.
        current = json.loads(_run_gh(
            ["issue", "view", str(issue_number), "--json", "id,number,createdAt,body"], capture=True,
        ))
        original = _original_provenance_block(current)
        if original is not None:
            body = f"{body}\n\n{original}"
        _run_gh([
            "issue", "edit", str(issue_number),
            "--body", body,
        ])
        _run_gh([
            "issue", "comment", str(issue_number),
            "--body", _run_comment(payload),
        ])
        print(f"pricing_issue: updated pricing-drift issue #{issue_number}")
        _write_record("updated", issue=dict(current, number=issue_number))
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
        _write_record("no-create")
    else:  # noop
        print("pricing_issue: no drift, no open issue — nothing to do")
        _write_record("no-create")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage the auto-tracked pricing-drift GitHub issue.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the intended action without calling gh.",
    )
    parser.add_argument(
        "--no-create-record", action="store_true",
        help="Only record that this run created no issue (its check failed before this script ran).",
    )
    parser.add_argument(
        "payload", nargs="?", help="Path to a `pricing-check --json` payload file.",
    )
    args = parser.parse_args(argv)
    if args.no_create_record:
        _write_record("no-create")
        return 0
    if not args.payload:
        parser.error("a payload file is required")
    if args.dry_run:
        return _decide(args)
    try:
        return _decide(args)
    except BaseException:
        # Stopped before asking to create an issue: this run created none (C6).
        if not _CREATE_REQUESTED:
            _write_record("no-create")
        raise


def _decide(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(pathlib.Path(args.payload).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"[pricing_issue] cannot read payload: {exc}\n")
        raise SystemExit(2)

    findings_present = _findings_present(payload)
    # In --dry-run we don't query gh for the open-issue state (no network /
    # auth assumed); model "no open issue" so the action is purely a function
    # of the payload. Live runs query the real state.
    issue_number = None if args.dry_run else _find_open_issue()
    existing_open = issue_number is not None

    action = _lib_pricing_check.pricing_issue_action(findings_present, existing_open)
    _act(action, payload, issue_number, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

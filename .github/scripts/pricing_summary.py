#!/usr/bin/env python3
"""Summarize a `cctally pricing-check --json` payload for the weekly cron (spec §5.4).

The pricing-freshness workflow runs the check, then this script renders what it
found into the run's step summary and, when there is an actionable finding,
raises one workflow warning annotation. The script makes no network call and
writes no GitHub issue: issues are filed only through the repository's filing
helper, and the scheduled check is not one of its callers (test regime reset,
Task 3.6). The payload itself is uploaded as the run's artifact.

findings_present := any `value_drift` OR `missing_from_us` OR
`staleSuppressions` OR `expiredSuppressions` (#279 S7 W7). (The existence
leg never runs in the cron — no OAuth — and `ahead_of_litellm` is
informational only, never actionable; spec invariant #2. Offline coverage
gaps are a LOCAL signal, surfaced by `doctor`, not the cron's job.)

Usage:
  pricing_summary.py <payload.json>   # append the summary to $GITHUB_STEP_SUMMARY
                                      # (stdout when it is unset)

Exit codes: 0 summarized (with or without findings), 2 unreadable payload.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys


def _findings_present(payload: dict) -> bool:
    """True iff the payload carries an actionable finding the cron tracks:
    value drift, missing-from-us, OR (as of #279 S7 W7) a stale/expired
    allowlist suppression.

    Deliberately ignores `existence.unpriced_vendor_models`: the cron has no
    OAuth, so the `/v1/models` existence leg always auto-degrades and reports
    nothing here. If CI is ever granted an OAuth bearer, revisit this — an
    existence-only finding would otherwise go unreported by the summary.
    """
    drift = payload.get("drift") or {}
    return (
        bool(drift.get("value_drift"))
        or bool(drift.get("missing_from_us"))
        or bool(payload.get("staleSuppressions"))
        or bool(payload.get("expiredSuppressions"))
    )


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def _build_summary(payload: dict) -> str:
    """The run's diagnostic summary: current drift state + remediation checklist."""
    drift = payload.get("drift") or {}
    value_drift = drift.get("value_drift") or []
    missing = drift.get("missing_from_us") or []
    ahead = drift.get("ahead_of_litellm") or []
    snapshot = payload.get("snapshotDate", "?")
    source = payload.get("litellmSource", "LiteLLM")
    status = payload.get("status", "?")
    degraded = payload.get("degraded_components") or []
    findings = _findings_present(payload)

    lines: list[str] = []
    lines.append("## Pricing freshness")
    lines.append("")
    if findings:
        lines.append(
            "cctally's embedded model pricing diverges from the LiteLLM "
            "snapshot. The findings below are this run's; the next weekly "
            "run re-checks them."
        )
    else:
        lines.append("No actionable pricing drift: the embedded tables match the LiteLLM snapshot.")
    lines.append("")
    lines.append(f"- Embedded snapshot date: `{snapshot}`")
    lines.append(f"- Source: {source}")
    lines.append(f"- Run date: {_today()} (check status: `{status}`)")
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

    if findings:
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
    return "\n".join(lines)


def _counts(payload: dict) -> str:
    drift = payload.get("drift") or {}
    nv = len(drift.get("value_drift") or [])
    nm = len(drift.get("missing_from_us") or [])
    ns = len(payload.get("staleSuppressions") or [])
    ne = len(payload.get("expiredSuppressions") or [])
    return (
        f"{nv} value-drift field(s), {nm} missing-from-us model(s), "
        f"{ns} stale suppression(s), {ne} expired suppression(s)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize a pricing-check payload into the run's step summary.",
    )
    parser.add_argument("payload", help="Path to a `pricing-check --json` payload file.")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(pathlib.Path(args.payload).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"[pricing_summary] cannot read payload: {exc}\n")
        return 2
    if not isinstance(payload, dict):
        sys.stderr.write("[pricing_summary] cannot read payload: not a JSON object\n")
        return 2

    summary = _build_summary(payload) + "\n"
    target = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(summary)
    else:
        sys.stdout.write(summary)
    findings = _findings_present(payload)
    if findings:
        # One annotation on the run page, so a drift is visible without opening the summary.
        print(f"::warning title=Pricing drift::{_counts(payload)}")
    print(f"pricing_summary: findings={'yes' if findings else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

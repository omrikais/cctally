"""Pin the `Derive dist-tag` shell branch in `.github/workflows/release-npm.yml`.

The beta-channel model (spec `2026-07-21-beta-channel-design.md` Section 1)
requires the release workflow to publish stable-form cuts under the npm
`beta` dist-tag (never `latest` again at cut time), while `-id.N` prerelease
forms keep the `next` escape-hatch tag. Nothing at cut time may move
`latest` — promotion owns that.

This test extracts the workflow step's `run:` script with stdlib string ops
(no YAML dependency — the CLI is stdlib-only, and the test suite must not add
one), executes it under a real `bash` with `GITHUB_REF_NAME` set and
`GITHUB_OUTPUT` pointed at a temp file, and asserts the derived `dist=` value
for both a stable-form tag and a prerelease-form tag.
"""

from __future__ import annotations

import pathlib
import subprocess
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-npm.yml"


def _extract_derive_dist_tag_run() -> str:
    """Return the shell body of the `Derive dist-tag` step's `run: |` block.

    Pure string parsing — locates the `- name: Derive dist-tag` step, finds
    the `run: |` (block scalar) key inside it, then collects every following
    line that is more-indented than the `run:` key (blank lines included)
    until the block scalar ends. The captured lines are dedented by the
    block's common indent so the result is an executable shell fragment.
    """
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    # Find the step.
    step_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "- name: Derive dist-tag":
            step_idx = i
            break
    assert step_idx is not None, "Derive dist-tag step not found in workflow"

    # Find `run: |` within the step body (before the next `- name:` step).
    run_idx = None
    run_indent = None
    for i in range(step_idx + 1, len(lines)):
        stripped = lines[i].lstrip()
        if stripped.startswith("- name:"):
            break  # next step; the run key must precede it
        if stripped.startswith("run:") and stripped.rstrip().endswith("|"):
            run_idx = i
            run_indent = len(lines[i]) - len(stripped)
            break
    assert run_idx is not None, "run: | block not found in Derive dist-tag step"

    # Collect the block-scalar body: lines indented deeper than the run key.
    body: list[str] = []
    for i in range(run_idx + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            body.append("")
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= run_indent:
            break
        body.append(line)
    # Dedent by the minimum indent of the non-blank body lines.
    non_blank = [ln for ln in body if ln.strip()]
    assert non_blank, "Derive dist-tag run block is empty"
    common = min(len(ln) - len(ln.lstrip()) for ln in non_blank)
    return "\n".join(ln[common:] if ln.strip() else "" for ln in body) + "\n"


def _run_derive(ref_name: str) -> str:
    """Execute the derivation script with GITHUB_REF_NAME=<ref_name>.

    Returns the derived dist-tag (the value after `dist=` written to
    GITHUB_OUTPUT). Raises AssertionError if the script wrote no dist line.
    """
    script = _extract_derive_dist_tag_run()
    with tempfile.NamedTemporaryFile("w+", suffix=".env", delete=True) as out:
        subprocess.run(
            ["bash", "-c", script],
            env={
                "GITHUB_REF_NAME": ref_name,
                "GITHUB_OUTPUT": out.name,
                "PATH": "/usr/bin:/bin:/usr/local/bin",
            },
            check=True,
        )
        out.seek(0)
        content = out.read()
    dist = None
    for line in content.splitlines():
        if line.startswith("dist="):
            dist = line[len("dist="):].strip()
    assert dist is not None, f"no dist= line written for ref {ref_name!r}: {content!r}"
    return dist


def test_stable_form_tag_derives_beta():
    """A plain SemVer tag (no `-`) lands in the beta channel."""
    assert _run_derive("v1.76.0") == "beta"


def test_prerelease_form_tag_derives_next():
    """A `-id.N` prerelease tag keeps the `next` escape-hatch dist-tag."""
    assert _run_derive("v1.76.0-rc.1") == "next"


def test_zero_and_multi_digit_stable_derive_beta():
    """Guard against a brittle matcher: other stable shapes also derive beta."""
    assert _run_derive("v2.0.0") == "beta"
    assert _run_derive("v10.11.12") == "beta"

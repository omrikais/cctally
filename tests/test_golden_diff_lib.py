"""Regression tests for bin/_lib-golden-diff.sh (issue #106).

The update harness once reported ``FAIL check-available-brew: stderr
diverged`` on CI with NO diff to show. Root cause: the old comparison
detected divergence with ``diff -u <(printf …) golden >/dev/null 2>&1``,
which (a) ran over a ``<(process substitution)`` whose ``/dev/fd`` setup
intermittently failed under heavy parallel load, making ``diff`` exit 2
(trouble), and (b) redirected ``2>&1`` to ``/dev/null`` so that trouble
exit was swallowed and mis-reported as a content divergence — while the
separate re-diff for display found the content actually matched, hence
"FAIL with no diff."

``_lib-golden-diff.sh`` replaces that pattern: ONE ``diff`` over real
files drives BOTH the verdict and the displayed bytes, diff's own stderr
is captured into the shown output, and a trouble exit (>=2) is surfaced
distinctly. The invariant these tests lock in: **a non-zero verdict is
never silent** — every FAIL prints something — and **no process
substitution is used**.
"""
import pathlib
import subprocess

LIB = pathlib.Path(__file__).resolve().parents[1] / "bin" / "_lib-golden-diff.sh"


def _run(snippet: str, tmp_path: pathlib.Path) -> tuple[int, str]:
    """Source the lib in a fresh bash and run ``snippet``.

    ``name`` (used in FAIL messages) and ``GOLDEN_DIFF_TMPDIR`` (scratch
    for the string variants' temp files) are pre-set the way the harness
    sets them. Returns ``(returncode_of_last_helper, combined_output)``.
    The helper's rc is surfaced via a trailing ``echo "RC=$?"`` the
    caller appends.
    """
    script = (
        f'set -uo pipefail\n'
        f'source "{LIB}"\n'
        f'name=t\n'
        f'GOLDEN_DIFF_TMPDIR="{tmp_path}"\n'
        f'{snippet}\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True
    )
    return proc.returncode, proc.stdout


def test_lib_exists_and_has_no_process_substitution():
    """The whole point of the fix: no ``<(`` process substitution in the
    executable code, whose transient /dev/fd setup failure under load was
    the #106 flake. Comment lines (which document the old broken pattern)
    are exempt."""
    assert LIB.exists(), f"missing {LIB}"
    code_lines = [
        ln for ln in LIB.read_text().splitlines()
        if not ln.lstrip().startswith("#")
    ]
    offenders = [ln for ln in code_lines if "<(" in ln]
    assert not offenders, f"process substitution in executable code: {offenders}"


def test_files_equal_passes_silently(tmp_path):
    g = tmp_path / "g"
    a = tmp_path / "a"
    g.write_text("hello\n")
    a.write_text("hello\n")
    rc, out = _run(
        f'_golden_diff_files lbl "{g}" "{a}"; echo "RC=$?"', tmp_path
    )
    assert "RC=0" in out, out
    # Nothing but the RC marker should be printed on a match.
    assert out.strip() == "RC=0", out


def test_files_differ_fails_loudly(tmp_path):
    g = tmp_path / "g"
    a = tmp_path / "a"
    g.write_text("hello\n")
    a.write_text("goodbye\n")
    rc, out = _run(
        f'_golden_diff_files lbl "{g}" "{a}"; echo "RC=$?"', tmp_path
    )
    assert "RC=1" in out, out
    assert "FAIL t: lbl diverged" in out, out
    # The actual diverging bytes MUST be shown — never a silent FAIL.
    assert "-hello" in out and "+goodbye" in out, out


def test_diff_trouble_is_never_silent(tmp_path):
    """The #106 core: when ``diff`` exits >=2 (trouble), the helper must
    surface it loudly, NOT swallow it and mis-report a content diff with
    nothing to show. A missing golden makes ``diff`` exit 2
    deterministically — a faithful proxy for the load-induced /dev/fd
    failure that the old ``>/dev/null 2>&1`` detection swallowed."""
    a = tmp_path / "a"
    a.write_text("hello\n")
    missing = tmp_path / "does-not-exist"
    rc, out = _run(
        f'_golden_diff_files lbl "{missing}" "{a}"; echo "RC=$?"', tmp_path
    )
    assert "RC=1" in out, out
    assert "FAIL t: lbl" in out, out
    # Must NOT be a silent FAIL: diff's own error text is surfaced.
    body = out.replace("FAIL t: lbl diverged", "").replace("RC=1", "")
    assert body.strip(), f"trouble exit produced a silent FAIL: {out!r}"


def test_str_variant_equal_and_differ(tmp_path):
    g = tmp_path / "g"
    g.write_text("hello\n")  # goldens end in a trailing newline
    rc, out = _run(
        f'_golden_diff_str lbl "{g}" "hello"; echo "RC=$?"', tmp_path
    )
    assert out.strip() == "RC=0", out

    rc, out = _run(
        f'_golden_diff_str lbl "{g}" "nope"; echo "RC=$?"', tmp_path
    )
    assert "RC=1" in out, out
    assert "FAIL t: lbl diverged" in out and "+nope" in out, out


def test_str_variant_no_leftover_tempfiles(tmp_path):
    """The string variant materializes to a temp file then removes it;
    it must not leak temp files into GOLDEN_DIFF_TMPDIR."""
    g = tmp_path / "g"
    g.write_text("x\n")
    _run(f'_golden_diff_str lbl "{g}" "x"; echo "RC=$?"', tmp_path)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "g"]
    assert leftovers == [], f"leftover temp files: {leftovers}"


def test_two_str_variant_equal_and_differ(tmp_path):
    # Both sides are strings, compared without an added trailing newline
    # (mirrors the JSON post-state comparison).
    rc, out = _run(
        '_golden_diff_two_str lbl "{}" "{}"; echo "RC=$?"', tmp_path
    )
    assert out.strip() == "RC=0", out

    rc, out = _run(
        '_golden_diff_two_str lbl "alpha" "beta"; echo "RC=$?"', tmp_path
    )
    assert "RC=1" in out, out
    assert "FAIL t: lbl diverged" in out, out
    assert "-alpha" in out and "+beta" in out, out

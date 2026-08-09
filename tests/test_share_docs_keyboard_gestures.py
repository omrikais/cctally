"""#503 S4 / F30 — the share docs may not advertise a gesture that does nothing.

The dashboard binds the share shortcut to `Shift+S`, not to a bare `S`
(`dashboard/web/src/share/keyboardShare.ts`). An unshifted `s` opens Settings,
so a passage telling the reader to "press `S`" sends them to the wrong surface.
The same holds for `B` and the basket.

Seventeen passages across the share docs carried the bare form before this was
fixed. The fix was a class sweep, and this test is what keeps it one: it is a
TRIPWIRE, not a unit test — it re-runs the completeness scan on every suite run
so a reintroduced bare gesture fails here instead of shipping.

Deliberately hardcoded, not imported: the pattern and the file list are written
out below rather than read from the modules they guard. A tripwire that derives
its expectation from the code it checks moves with that code and can never fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The public documents that describe the share keyboard surface to a reader.
PUBLIC_SHARE_DOCS = (
    "docs/commands/share-v2.md",
    "docs/commands/share.md",
)


def _share_docs() -> list[str]:
    """The documents to scan on THIS tree.

    This module is public (it matches a `tests/**` allowlist glob), but
    `docs/share-gotchas.md` is mirror-private. A public test that reaches an
    unconditional path into a private file passes here and reds the public
    CI on a clone that does not carry it — Scope A2 of
    `tests/test_public_test_dep_closure.py`, which is what caught this.

    So the private document is appended only when it is present. The gate names
    the literal rather than a loop variable, because the closure guard resolves
    a gate's path operand and cannot tell that a `Path / name` built from a
    parameter is the same file.
    """
    docs = list(PUBLIC_SHARE_DOCS)
    if (REPO_ROOT / "docs/share-gotchas.md").exists():
        docs.append("docs/share-gotchas.md")
    return docs

# A bare `S`/`B` gesture: either backticked and not preceded by `Shift+`, or
# written as "press S". The `([^h]|$)` tail is what lets `press \`Shift+S\``
# through — without it this matches the prefix of every CORRECT passage and can
# never return empty, which is exactly how the first version of this scan was
# unenforceable.
BARE_GESTURE = re.compile(
    r"(^|[^+\w])`[SB]`"
    r"|press +`?[SB]`?([^h]|$)"
)

# A share gesture is keyboard-only; telling the reader to click a control first
# describes a flow that does not exist.
CLICK_TO_FOCUS = re.compile(
    r"click[^.\n]{0,40}\b(then|before)\b[^.\n]{0,40}press",
    re.IGNORECASE,
)


def _hits(pattern: re.Pattern[str], rel: str) -> list[str]:
    path = REPO_ROOT / rel
    text = path.read_text(encoding="utf-8")
    return [
        f"{rel}:{n}: {line.strip()}"
        for n, line in enumerate(text.splitlines(), start=1)
        if pattern.search(line)
    ]


@pytest.mark.parametrize("rel", _share_docs())
def test_share_docs_advertise_no_bare_s_or_b_gesture(rel: str) -> None:
    hits = _hits(BARE_GESTURE, rel)
    assert not hits, (
        "share docs advertise a bare `S`/`B` gesture; the binding is Shift+S "
        "(an unshifted `s` opens Settings):\n" + "\n".join(hits)
    )


@pytest.mark.parametrize("rel", _share_docs())
def test_share_docs_describe_no_click_to_focus_step(rel: str) -> None:
    hits = _hits(CLICK_TO_FOCUS, rel)
    assert not hits, (
        "share docs describe a click-then-press flow that does not exist:\n"
        + "\n".join(hits)
    )


def test_the_bare_gesture_pattern_actually_matches_a_bare_gesture() -> None:
    """Non-vacuity guard.

    Both scans above assert emptiness, so a pattern that matched nothing at all
    would report a permanent green. Pin the discriminating behavior directly:
    the bare forms must match and the correct forms must not.
    """
    for bad in ("press `S` to share", "press S to share", "hit `B` for the basket"):
        assert BARE_GESTURE.search(bad), f"pattern missed a bare gesture: {bad!r}"
    for good in ("press `Shift+S` to share", "press `Shift+B`", "`Shift+S` opens it"):
        assert not BARE_GESTURE.search(good), f"pattern flagged a correct form: {good!r}"

"""Public helper: read the latest stamped release header from CHANGELOG.md.

Read-only. Pure with respect to inputs (CHANGELOG.md contents). The
historical name ``_release_read_latest_release_version`` carried a
``_release_`` prefix because the helper originated with the release-
automation work, but the function is not release-machinery: doctor,
the share kernel, and ``cctally --version`` all read it. Lives in a
public sibling so the maintainer-only release tooling can move to a
private artifact without dragging the version reader with it.
"""

from __future__ import annotations

import sys


def _cctally():
    """Call-time accessor for the ``cctally`` module (project memory
    ``_cctally() accessor pattern``). Avoids module-top ``import cctally``
    so monkeypatch-sensitive globals (``CHANGELOG_PATH`` and
    ``RELEASE_HEADER_RE``) stay reachable for tests."""
    return sys.modules["cctally"]


def _read_latest_changelog_version() -> tuple[str, str] | None:
    """Read latest ``## [X.Y.Z] - YYYY-MM-DD`` header from
    ``CHANGELOG_PATH``. Returns ``(version, date)`` or ``None`` if the
    file is missing or has no stamped release header.

    Body is byte-equivalent to the original
    ``_release_read_latest_release_version`` definition in ``bin/cctally``
    (the rename is the only intentional change); the regex
    ``RELEASE_HEADER_RE`` is read from the ``cctally`` module so any
    in-process update to the pattern remains the single source of truth.
    """
    c = _cctally()
    try:
        text = c.CHANGELOG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    m = c.RELEASE_HEADER_RE.search(text)
    if not m:
        return None
    return (m.group(1), m.group(2))

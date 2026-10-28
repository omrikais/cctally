"""Per-entry expectation visibility for an exact comparison the public clone also runs.

The cache-writer inventory in `tests/test_cache_coverage_496_s5b.py` names
`bin/cctally-snapshot-measure`, a file the mirror does not publish. The
comparison is exact equality and the public clone collects it, so on that
profile it would expect one entry the tree cannot produce.

Visibility is DECLARED per entry rather than inferred from the tree. An
unannotated entry is public, which is the fail-closed direction: a writer added
later is expected on both profiles until somebody classifies it. Only which
declarations apply is profile-dependent. The comparison itself never is, and is
never relaxed to a subset test because `.mirror-allowlist` is absent.

Stdlib-only, and it reads nothing but `.mirror-allowlist` and the tree's own
allowlist classifier `.githooks/_match.py`, which it never reimplements.
"""
from __future__ import annotations

import collections
import importlib.util
from pathlib import Path

ALLOWLIST = ".mirror-allowlist"
PUBLIC = "public"
PRIVATE = "private"
PROFILES = (PUBLIC, PRIVATE)


class OwnerExpectationError(RuntimeError):
    """A declaration is malformed, or disagrees with the real mirror boundary."""


Expectation = collections.namedtuple("Expectation", ("value", "visibility", "owner"))
"""One expectation entry, its declared visibility, and the path that owns it.

``owner`` is a repository-relative path. ``validate_owner_paths`` hands it to the
tree's own allowlist classifier, so a declaration that has drifted away from
`.mirror-allowlist` fails rather than silently narrowing a comparison.
"""


def _repo_default() -> Path:
    return Path(__file__).resolve().parent.parent


def private_expectation(value, owner):
    """``value``, declared to exist only on the private profile."""
    if not isinstance(owner, str) or not owner.strip():
        raise OwnerExpectationError(
            "a private expectation must name the repository-relative path that "
            f"owns it; got {owner!r}")
    return Expectation(value, PRIVATE, owner)


def active_profile(repo=None):
    """Which profile this tree is, decided by `.mirror-allowlist`'s presence."""
    root = Path(repo) if repo is not None else _repo_default()
    return PRIVATE if (root / ALLOWLIST).exists() else PUBLIC


def applicable_expectations(entries, *, profile):
    """``entries`` without the declarations ``profile`` does not carry.

    Returns a plain mapping of key to raw value, so the caller compares the same
    shape it always did. An entry that is not an ``Expectation`` is public and is
    passed through unchanged.
    """
    if profile not in PROFILES:
        raise OwnerExpectationError(f"unknown profile {profile!r}; expected one of {PROFILES}")
    applicable = {}
    for key, entry in dict(entries).items():
        if isinstance(entry, Expectation):
            if entry.visibility == PRIVATE and profile != PRIVATE:
                continue
            applicable[key] = entry.value
        else:
            applicable[key] = entry
    return applicable


def _load_allowlist_matcher(path):
    """Load `.githooks/_match.py` WITHOUT registering it in ``sys.modules``."""
    spec = importlib.util.spec_from_file_location("_owner_expectations_allowlist_matcher", path)
    if spec is None or spec.loader is None:
        raise OwnerExpectationError(f"the allowlist classifier at {path} is not loadable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise OwnerExpectationError(f"the allowlist classifier at {path} did not load: {exc}") from exc
    if not callable(getattr(module, "classify", None)):
        raise OwnerExpectationError(f"the allowlist classifier at {path} exposes no classify()")
    return module


def validate_owner_paths(entries, repo=None):
    """Refuse a private declaration the real allowlist actually publishes.

    Private profile only. The public projection carries neither
    `.mirror-allowlist` nor `.githooks/_match.py` by design, so it consumes the
    declared visibility directly; what is skipped there is this validation, not
    the comparison the declaration feeds. On a tree that claims to be private, an
    absent classifier is a refusal rather than a silent success.
    """
    root = Path(repo) if repo is not None else _repo_default()
    if active_profile(root) != PRIVATE:
        return None
    declared = sorted({
        entry.owner for entry in dict(entries).values()
        if isinstance(entry, Expectation) and entry.visibility == PRIVATE
    })
    if not declared:
        return None
    matcher = root / ".githooks" / "_match.py"
    if not matcher.exists():
        raise OwnerExpectationError(
            f"this tree is the private profile ({ALLOWLIST} is present) but the "
            f"allowlist classifier {matcher} is absent, so a declared owner "
            "path cannot be checked against the real mirror boundary")
    classify = _load_allowlist_matcher(matcher).classify
    text = (root / ALLOWLIST).read_text(encoding="utf-8")
    published = set(classify(list(declared), allowlist_text=text)["public"])
    drifted = [path for path in declared if path in published]
    if drifted:
        raise OwnerExpectationError(
            "an expectation entry declares private visibility but "
            f"{ALLOWLIST} publishes the path that owns it: "
            f"{', '.join(drifted)}. Either drop the declaration or negate the "
            "path in the allowlist; the two must agree.")
    return None

# shellcheck shell=bash
# Shared wrapper: cache a builder's --out fixture tree (#281 S11 / R9).
# Usage: build_fixtures_cached <builder-abs-path> <owned-out-dir>
#
# The cache label is derived from the builder basename inside _fixture_cache.py
# (canonical, caller-independent), so every caller of one builder shares one
# entry/key. Markers (FIXTURE-CACHE HIT|MISS|POISONED|BYPASS <label>) go to
# stderr; the builder's own stdout is discarded here so callers keep their
# existing `>/dev/null` semantics and cctally-test-all's stdout pass/fail
# parsing stays intact.
#
# Safety contract: a cache problem NEVER changes a test outcome — every failure
# mode inside _fixture_cache.py falls back to running the builder. The wrapper's
# exit code is the builder's exit code (or 0 on a cache hit), so callers can keep
# their `|| { echo FAIL; exit 1; }` guard unchanged.
_FIXTURE_CACHE_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_fixture_cache.py"
build_fixtures_cached () {
    python3 "$_FIXTURE_CACHE_PY" run --builder "$1" --out "$2" >/dev/null
}

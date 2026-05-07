#!/bin/bash
set -uo pipefail
work="$(pwd)/.."
REPO_ROOT="$1"

# Suppress .pyc generation (parity with setup.sh).
export PYTHONDONTWRITEBYTECODE=1

# Determinism: pin author/committer + release date.
export GIT_AUTHOR_NAME="Test"
export GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="Test"
export GIT_COMMITTER_EMAIL="test@example.com"
export GIT_AUTHOR_DATE="2026-05-07T00:00:00+0000"
export GIT_COMMITTER_DATE="2026-05-07T00:00:00+0000"
export CCTALLY_RELEASE_DATE_UTC="2026-05-07"

# Fake `gh` recording: prepend the scaffold's fake-bin to PATH.
export PATH="$work/fake-bin:$PATH"
export GH_ARGV_LOG="$work/gh-argv.log"
export GH_NOTES_DEST="$work/_artifacts/gh-notes.txt"

# Per-scenario artifact dir; the harness reads files under it for the
# golden-* comparisons.
mkdir -p "$work/_artifacts"
python3 bin/cctally release patch > "$work/_artifacts/stdout.txt" 2> "$work/_artifacts/stderr.txt"
rc=$?
echo "$rc" > "$work/_artifacts/exit.txt"
cp CHANGELOG.md "$work/_artifacts/changelog.md" 2>/dev/null || true
cp package.json "$work/_artifacts/package.json" 2>/dev/null || true
git log -1 --format=%B 2>/dev/null | sed -E "s/[0-9a-f]{7,40}/<SHA7>/g" > "$work/_artifacts/commit-msg.txt" || true
tag_name=$(git tag --points-at HEAD 2>/dev/null | grep -E '^v[0-9]' | head -n1)
if [ -n "$tag_name" ]; then
  git tag -l --format="%(contents)" "$tag_name" > "$work/_artifacts/tag-annotation.txt"
fi
if [ -f "$work/gh-argv.log" ]; then
  cp "$work/gh-argv.log" "$work/_artifacts/gh-argv.log"
fi
if [ -f "$work/npm-invocations.log" ]; then
  cp "$work/npm-invocations.log" "$work/_artifacts/npm-invocations.log"
else
  : > "$work/_artifacts/npm-invocations.log"
fi

# Extract body from stamp commit message (text after the `--- public ---`
# subject line + blank line). awk machine: switch into `in_pub` after
# the marker, drop one subject line, drop one blank line, then echo all
# subsequent lines verbatim.
git log -1 --format=%B > "$work/_artifacts/full-commit-msg.txt" 2>/dev/null || true
# Extract body, then strip trailing newline (the wrapping `\n` in the
# commit message; canonical body has none per spec §6.4).
awk '
  /^--- public ---$/ { in_pub = 1; skip = 2; next }
  in_pub && skip > 0 { skip--; next }
  in_pub { print }
' "$work/_artifacts/full-commit-msg.txt" \
  | python3 -c "import sys; sys.stdout.write(sys.stdin.read().rstrip(chr(10)))" \
  > "$work/_artifacts/body-from-commit.txt"

# Tag annotation body — strip first two lines (`vX.Y.Z` + blank) so what
# remains is the body. PGP signature block (signed tags) is stripped via
# BEGIN/END markers. Trailing newline from `git tag --format=%(contents)`
# is stripped to match canonical body (no trailing newline).
tag_name=$(git tag --points-at HEAD 2>/dev/null | grep -E '^v[0-9]' | head -n1)
if [ -n "$tag_name" ]; then
  git tag -l --format="%(contents)" "$tag_name" \
    | awk '
        /^-----BEGIN PGP SIGNATURE-----$/ { in_sig = 1 }
        !in_sig { print }
        /^-----END PGP SIGNATURE-----$/ { in_sig = 0 }
      ' \
    | sed '1,2d' \
    | python3 -c "import sys; sys.stdout.write(sys.stdin.read().rstrip(chr(10)))" \
    > "$work/_artifacts/body-from-tag.txt"
fi

# gh `--notes-file` content was copied to $work/_artifacts/gh-notes.txt
# by the fake-gh script when the release create invocation ran.
cp "$work/_artifacts/gh-notes.txt" "$work/_artifacts/body-from-gh.txt" 2>/dev/null || true

# Byte-equality check: 1 if all three files exist AND match; 0 otherwise.
equal=0
if [ -f "$work/_artifacts/body-from-commit.txt" ] \
   && [ -f "$work/_artifacts/body-from-tag.txt" ] \
   && [ -f "$work/_artifacts/body-from-gh.txt" ]; then
  if diff -q "$work/_artifacts/body-from-commit.txt" "$work/_artifacts/body-from-tag.txt" >/dev/null \
     && diff -q "$work/_artifacts/body-from-commit.txt" "$work/_artifacts/body-from-gh.txt" >/dev/null; then
    equal=1
  fi
fi
echo "$equal" > "$work/_artifacts/body-equal.txt"
exit "$rc"

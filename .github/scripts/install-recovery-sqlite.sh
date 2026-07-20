#!/usr/bin/env bash
# Install the official SQLite shell used by public Linux CI. Ubuntu's distro
# shell exposes `.recover` but is built without SQLITE_ENABLE_DBPAGE_VTAB, so
# recovery fails at runtime with `no such table: sqlite_dbpage`.
set -euo pipefail

destination=${1:?usage: install-recovery-sqlite.sh DESTINATION}
archive="$destination/sqlite-tools.zip"
probe="$destination/recover-probe.sql"
url="https://sqlite.org/2026/sqlite-tools-linux-x64-3530300.zip"
expected_sha256="089a0d94dff010c4f193fc6691c10ed03249eb09fe214284d656f131c32a73f6"

mkdir -p "$destination"
curl -fsSL --retry 3 --retry-all-errors "$url" -o "$archive"
actual_sha256=$(sha256sum "$archive" | awk '{print $1}')
if [[ "$actual_sha256" != "$expected_sha256" ]]; then
  echo "sqlite tools checksum mismatch: expected $expected_sha256, got $actual_sha256" >&2
  exit 1
fi
unzip -q -o "$archive" sqlite3 -d "$destination"
chmod +x "$destination/sqlite3"

"$destination/sqlite3" :memory: '.recover' >"$probe"
grep -q '^BEGIN;' "$probe"
grep -q '^COMMIT;' "$probe"
"$destination/sqlite3" --version

if [[ -n "${GITHUB_PATH:-}" ]]; then
  echo "$destination" >>"$GITHUB_PATH"
else
  echo "$destination"
fi

#!/bin/bash
# Emit the normalized toolchain manifest baked into a Gate 0.25 lane image.
#
# The image caches provisioning that every lane used to redo, so the environment
# the gate exercises is no longer visible in the lane's own output. This
# manifest is what makes the baked environment observable in release evidence:
# the image records it, the image is labelled with its SHA-256, and every lane
# prints that hash. It is a record and never a gate, so a field that cannot be
# collected on some future base image degrades to an empty value rather than
# failing the build.
#
# Usage: record-manifest.sh <checkout> <apt-epoch> <image-inputs>
# The digest-pinned base reference arrives through CCTALLY_BASE_IMAGE, which the
# Dockerfile redeclares after FROM so the build ARG reaches this process.
set -euo pipefail

checkout="${1:?checkout path required}"
apt_epoch="${2:?apt freshness epoch required}"
image_inputs="${3:?image input digest required}"
base_image="${CCTALLY_BASE_IMAGE:-}"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

venv_python="${checkout}/.venv/bin/python"
web="${checkout}/dashboard/web"
sqlite_shell=/opt/cctally-sqlite/sqlite3

digest_of() {
  sha256sum "$1" | cut -d' ' -f1
}

# Normalized package inventory: package, version, architecture and install
# status, sorted under the C collation so the listing is comparable between two
# builds rather than dependent on the build host's locale.
# `|| true` on both, matching every other collection step: this manifest is a
# record and never a gate, so a field that cannot be collected degrades to an
# empty value rather than failing the image build under `set -euo pipefail`.
dpkg-query -W -f '${binary:Package}\t${Version}\t${Architecture}\t${Status}\n' \
  | LC_ALL=C sort > "$work/packages.tsv" || true

"$venv_python" -m pip freeze --all | LC_ALL=C sort > "$work/pip-freeze.txt" || true

{
  cat /etc/apt/sources.list 2>/dev/null || true
  cat /etc/apt/sources.list.d/*.sources 2>/dev/null || true
  cat /etc/apt/sources.list.d/*.list 2>/dev/null || true
} | grep -v '^[[:space:]]*#' | grep -v '^[[:space:]]*$' | LC_ALL=C sort \
  > "$work/apt-sources.txt" || true

grep -h -E '^(Origin|Label|Suite|Version|Codename|Date|Valid-Until):' \
  /var/lib/apt/lists/*InRelease /var/lib/apt/lists/*Release 2>/dev/null \
  | LC_ALL=C sort -u > "$work/apt-release.txt" || true

# The installed npm dependency graph is read from the installed tree rather than
# from the manifests, because `npm ci` removes package.json and the lockfile from
# the image and the installed tree is what the lane actually resolves against.
"$venv_python" - "$web/node_modules" <<'PY' | LC_ALL=C sort > "$work/npm-graph.tsv" || true
import json
import os
import sys

root = sys.argv[1]
if os.path.isdir(root):
    for dirpath, _dirnames, filenames in os.walk(root):
        if "package.json" not in filenames:
            continue
        path = os.path.join(dirpath, "package.json")
        try:
            with open(path, "rb") as handle:
                data = json.load(handle)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        print(
            "{}\t{}\t{}".format(
                os.path.relpath(path, root),
                data.get("name", ""),
                data.get("version", ""),
            )
        )
PY

"$sqlite_shell" :memory: 'PRAGMA compile_options;' | LC_ALL=C sort \
  > "$work/sqlite-compile-options.txt" || true

locale -a 2>/dev/null | LC_ALL=C sort | grep -i '^en_us' > "$work/locales.txt" || true

package_lock_sha=""
if [ -f /opt/cctally-build/package-lock.sha256 ]; then
  package_lock_sha=$(tr -d '\r\n' < /opt/cctally-build/package-lock.sha256)
fi

jq -n \
  --argjson schemaVersion 1 \
  --arg imageInputs "$image_inputs" \
  --arg aptEpoch "$apt_epoch" \
  --arg builtAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg baseImage "$base_image" \
  --arg pythonMinor "$("$venv_python" -c 'import sys; print("{}.{}".format(*sys.version_info))')" \
  --arg architecture "$(dpkg --print-architecture)" \
  --rawfile osRelease /etc/os-release \
  --rawfile aptSources "$work/apt-sources.txt" \
  --rawfile aptReleaseIdentity "$work/apt-release.txt" \
  --arg packagesDigest "$(digest_of "$work/packages.tsv")" \
  --rawfile packages "$work/packages.tsv" \
  --arg pythonVersion "$("$venv_python" -c 'import platform; print(platform.python_version())')" \
  --arg pythonBaseExecutable "$("$venv_python" -c 'import sys; print(getattr(sys, "_base_executable", sys.base_prefix))')" \
  --arg pipVersion "$("$venv_python" -m pip --version)" \
  --arg pipFreezeDigest "$(digest_of "$work/pip-freeze.txt")" \
  --rawfile pipFreeze "$work/pip-freeze.txt" \
  --arg nodeVersion "$(node --version)" \
  --arg npmVersion "$(npm --version)" \
  --arg packageLockSha256 "$package_lock_sha" \
  --arg dependencyGraphDigest "$(digest_of "$work/npm-graph.tsv")" \
  --arg sqliteExecutableSha256 "$(digest_of "$sqlite_shell")" \
  --arg sqliteVersion "$("$sqlite_shell" -version)" \
  --rawfile sqliteCompileOptions "$work/sqlite-compile-options.txt" \
  --rawfile locales "$work/locales.txt" \
  '{
     schemaVersion: $schemaVersion,
     imageInputs: $imageInputs,
     aptEpoch: $aptEpoch,
     builtAt: $builtAt,
     baseImage: $baseImage,
     pythonMinor: $pythonMinor,
     architecture: $architecture,
     osRelease: $osRelease,
     apt: {
       sources: $aptSources,
       releaseIdentity: $aptReleaseIdentity,
       packagesDigest: $packagesDigest,
       packages: $packages
     },
     python: {
       version: $pythonVersion,
       baseExecutable: $pythonBaseExecutable,
       pipVersion: $pipVersion,
       freezeDigest: $pipFreezeDigest,
       freeze: $pipFreeze
     },
     node: {
       nodeVersion: $nodeVersion,
       npmVersion: $npmVersion,
       packageLockSha256: $packageLockSha256,
       dependencyGraphDigest: $dependencyGraphDigest
     },
     sqlite: {
       executableSha256: $sqliteExecutableSha256,
       version: $sqliteVersion,
       compileOptions: $sqliteCompileOptions
     },
     locale: {
       generated: $locales
     }
   }'

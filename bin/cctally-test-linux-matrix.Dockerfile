# syntax=docker/dockerfile:1
# Provisioning image for the release-blocking Gate 0.25 Linux matrix (#621).
#
# This file IS the provisioning fidelity contract that
# tests/test_linux_matrix_gate.py asserts. Every pin here — the SQLite tarball
# and its checksum, the compile flags, the apt package list, the Node archive
# verification — moved out of the generated container script and must keep its
# exact form.
#
# Both dependency trees are built at their FINAL in-checkout paths. The lane
# then merges the source over them with `cp -a`, which never deletes
# destination entries absent from the source, so they survive as REAL
# DIRECTORIES that the repository's trailing-slash gitignore rules match. An
# earlier design symlinked them from /opt and was falsified: a trailing-slash
# ignore rule does not match a symlink, so `git status` reported both as
# untracked and the clean-checkout invariant broke.
ARG CCTALLY_BASE_IMAGE
FROM ${CCTALLY_BASE_IMAGE}

# Redeclared after FROM so the build stage — and record-manifest.sh through the
# process environment — can read the digest-pinned base reference.
ARG CCTALLY_BASE_IMAGE
ARG CCTALLY_APT_EPOCH
ARG CCTALLY_IMAGE_INPUTS
ARG CCTALLY_CHECKOUT=/workspace/cctally-dev

ENV DEBIAN_FRONTEND=noninteractive
ENV TMPDIR=/opt/cctally-setup-tmp
RUN mkdir -p "$TMPDIR" /opt/cctally /opt/cctally-build

# The epoch is referenced BEFORE apt-get update so a new week genuinely
# invalidates this layer. Without that reference Docker would reuse the cached
# layer forever and the image would freeze against a moving Debian repository.
RUN echo "cctally apt freshness epoch: ${CCTALLY_APT_EPOCH}" \
 && apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends \
      ca-certificates curl git jq bsdextrautils procps bc sqlite3 unzip xz-utils locales rsync gcc libc6-dev \
 && localedef -i en_US -f UTF-8 en_US.UTF-8

COPY context/.nvmrc /opt/cctally-build/.nvmrc
RUN node_version=$(tr -d '\r\n' < /opt/cctally-build/.nvmrc) \
 && node_archive="node-v${node_version}-linux-arm64.tar.xz" \
 && curl -fsSL --retry 3 --retry-all-errors "https://nodejs.org/dist/v${node_version}/${node_archive}" -o "/opt/${node_archive}" \
 && curl -fsSL --retry 3 --retry-all-errors "https://nodejs.org/dist/v${node_version}/SHASUMS256.txt" -o /opt/node-shasums.txt \
 && (cd /opt && grep "  ${node_archive}$" node-shasums.txt | sha256sum -c -) \
 && tar -xJf "/opt/${node_archive}" -C /usr/local --strip-components=1

RUN sqlite_archive=/opt/sqlite-autoconf-3530300.tar.gz \
 && curl -fsSL --retry 3 --retry-all-errors "https://sqlite.org/2026/sqlite-autoconf-3530300.tar.gz" -o "$sqlite_archive" \
 && echo "c917d7db16648ec95f714974ace5e5dcf46b7dc70e26600a0a102a3141125db0  $sqlite_archive" | sha256sum -c - \
 && mkdir -p /opt/cctally-sqlite /opt/cctally-sqlite-src \
 && tar -xzf "$sqlite_archive" -C /opt/cctally-sqlite-src --strip-components=1 \
 && gcc -O2 -DSQLITE_ENABLE_DBPAGE_VTAB -DSQLITE_ENABLE_DBNSTAT_VTAB -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_RTREE -DHAVE_READLINE=0 \
      /opt/cctally-sqlite-src/shell.c /opt/cctally-sqlite-src/sqlite3.c \
      -ldl -lpthread -lm -o /opt/cctally-sqlite/sqlite3 \
 && /opt/cctally-sqlite/sqlite3 :memory: '.recover' >/dev/null

RUN groupadd --gid 10001 cctally \
 && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash cctally

# The virtualenv is built at its FINAL path, so its console-script shebangs and
# pyvenv.cfg are correct without any relocation.
COPY context/requirements-dev.txt /opt/cctally-build/requirements-dev.txt
RUN mkdir -p "${CCTALLY_CHECKOUT}" \
 && python -m venv "${CCTALLY_CHECKOUT}/.venv" \
 && "${CCTALLY_CHECKOUT}/.venv/bin/python" -m pip install --no-deps -r /opt/cctally-build/requirements-dev.txt \
 && "${CCTALLY_CHECKOUT}/.venv/bin/python" -m pip check

# npm ci needs the manifests in place; they are REMOVED afterwards so the
# run-time `cp -a` never meets a destination-owned repository file outside the
# two protected trees. The lockfile digest is captured before the removal
# because the toolchain manifest records it.
COPY context/package.json context/package-lock.json ${CCTALLY_CHECKOUT}/dashboard/web/
RUN cd "${CCTALLY_CHECKOUT}/dashboard/web" \
 && npm ci \
 && test -x node_modules/.bin/vitest \
 && test -x node_modules/.bin/eslint \
 && test -x node_modules/.bin/tsc \
 && sha256sum package-lock.json | cut -d' ' -f1 > /opt/cctally-build/package-lock.sha256 \
 && rm -f package.json package-lock.json

COPY context/record-manifest.sh /opt/cctally-build/record-manifest.sh
RUN /opt/cctally-build/record-manifest.sh "${CCTALLY_CHECKOUT}" "${CCTALLY_APT_EPOCH}" "${CCTALLY_IMAGE_INPUTS}" \
      > /opt/cctally/toolchain-manifest.json \
 && sha256sum /opt/cctally/toolchain-manifest.json | cut -d' ' -f1 \
      > /opt/cctally/toolchain-manifest.sha256

# Acceptance mode's in-container sampler. It is baked rather than mounted at run
# time so acceptance runs and ordinary runs use the identical image, and it is
# copied here, after the manifest, so adding it rebuilds only the trailing
# layers. Ordinary lanes never start it.
COPY context/sampler.py /opt/cctally-build/sampler.py

RUN chown -R 10001:10001 "${CCTALLY_CHECKOUT}" /opt/cctally /opt/cctally-setup-tmp

# A LABEL cannot carry a value the same build computes, so the gate builds this
# file twice: once with an empty toolchain digest to produce the manifest, and
# once with the digest it read back. The ARG is declared here, immediately
# before the only instructions that reference it, so the second pass is a cache
# hit on every layer above and rebuilds nothing but the metadata.
ARG CCTALLY_TOOLCHAIN=""

LABEL cctally.image.inputs="${CCTALLY_IMAGE_INPUTS}"
LABEL cctally.image.apt-epoch="${CCTALLY_APT_EPOCH}"
LABEL cctally.image.toolchain="${CCTALLY_TOOLCHAIN}"

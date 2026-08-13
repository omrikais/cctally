#!/usr/bin/env bash
# The ONE FTS5 assertion (#529 S6, M2).
#
# FTS5 is the one hard capability no provisioning step installs, because it is
# not a package: it depends on how the SQLite the interpreter links was built.
# Debian builds libsqlite3 with --enable-fts5 and Homebrew's python@3.13 links a
# SQLite that carries it, but nothing in this repository makes either true, so
# it has to be ASKED rather than assumed.
#
# It is asked in two places that need opposite behaviour from the same question,
# which is why this file has two modes rather than two implementations:
#
#   fts5_probe <interpreter>    Status only. Owns NO output, including the
#                               interpreter's own error text. The aggregator's
#                               admission path calls this, and that caller — not
#                               this helper — decides whether a missing
#                               capability is an authoritative refusal or a
#                               non-authoritative `incomplete` run that
#                               continues. A helper that printed a refusal
#                               diagnostic would be stating something false on
#                               the second of those paths.
#
#   fts5_require <interpreter>  Prints the diagnostic and exits 3. Provisioning
#                               calls this — the remote wrapper after it builds
#                               the venv, and each CI job that declares
#                               CCTALLY_AUTHORITATIVE_RUN — so the refusal lands
#                               BEFORE the suite starts rather than after the
#                               round trip has been paid for.
#
# Both run the same statement against the same explicitly named interpreter.
# There is no scratch file and no temporary database: the probe is an in-memory
# connection, so there is nothing to clean up and no `mktemp` template to get
# wrong.
#
# Sourced by bin/_lib-test-contract.sh and executable for a workflow step, which
# cannot source a library in one step and call a function from another.
#
# PUBLIC: published to the mirror by the `bin/_lib-*` glob, deliberately. It
# carries no host-specific path and no maintainer-local assumption.

# The statement. FTS5 is a compile-time SQLite option, so the only honest test
# of it is to create a table that needs it.
_FTS5_STATEMENT="import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE _p USING fts5(x)')"
_FTS5_VERSION_STATEMENT="import sqlite3; print(sqlite3.sqlite_version)"

# Status only. Every stream is discarded, including the interpreter's traceback.
fts5_probe() {  # <interpreter>
    local interpreter=${1:-python3}
    "$interpreter" -c "$_FTS5_STATEMENT" >/dev/null 2>&1
}

# The interpreter as the operator would have to type it to reproduce this. A
# bare `python3` does not identify which python3 answered, and a PATH with a
# pyenv shim in front of brew is exactly how a runner ends up asking a different
# interpreter than the suite will use.
_fts5_interpreter_path() {  # <interpreter>
    local interpreter=$1 resolved
    resolved=$(command -v "$interpreter" 2>/dev/null) || resolved=""
    printf '%s' "${resolved:-$interpreter}"
}

# The SQLite version that does NOT carry FTS5. Reported because it is the fact
# that identifies the build, and because an interpreter too broken to answer it
# is a different problem than an interpreter whose SQLite lacks a module.
_fts5_sqlite_version() {  # <interpreter>
    local interpreter=$1 version
    version=$("$interpreter" -c "$_FTS5_VERSION_STATEMENT" 2>/dev/null) || version=""
    printf '%s' "${version:-unknown}"
}

fts5_require() {  # <interpreter>
    local interpreter=${1:-python3}
    fts5_probe "$interpreter" && return 0
    echo "FTS5 unavailable; refusing authoritative gate before suite" >&2
    echo "  interpreter:     $(_fts5_interpreter_path "$interpreter")" >&2
    echo "  sqlite_version:  $(_fts5_sqlite_version "$interpreter")" >&2
    echo "  FTS5 is a compile-time SQLite option, not a package: install or select an interpreter whose SQLite was built with --enable-fts5. The 55 pytest.skip sites that degrade without it stay; what an authoritative run refuses is to report skipped coverage as a pass." >&2
    exit 3
}

# Direct execution. `return` fails outside a function in a sourced file, so this
# guard has to distinguish the two cases rather than rely on one of them.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    _fts5_usage() {
        echo "usage: _lib-fts5-probe.sh {probe|require} [interpreter]" >&2
        exit 2
    }
    case "${1:-}" in
        probe)   shift; fts5_probe "${1:-python3}" ;;
        require) shift; fts5_require "${1:-python3}" ;;
        # An unknown mode and a missing mode are both usage errors. Neither may
        # be a silent success: a workflow step whose typo made the gate a no-op
        # would report a capability it never asked about.
        *)       _fts5_usage ;;
    esac
fi

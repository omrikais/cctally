#!/usr/bin/env python3
"""Decide whether an authoritative receipt discharges the estate for a commit.

#630 S4. `bin/cctally-test-remote` publishes a small JSON receipt for every
passing authoritative full-suite run, keyed on the git tree OID it tested. This
script is the CI half: it reconstructs the pushed commit's canonical manifest
from git objects, compares it against each fetched candidate receipt, and
prints `discharged=true` only when at least one candidate verifies completely.

PUBLIC by mirror classification, and therefore SELF-CONTAINED: it may not
import `bin/_lib_remote_manifest.py`, which is private. The duplication of the
manifest record format is deliberate and is guarded by
`tests/test_receipt_classifier.py`, which asserts byte parity against that
kernel over the real repository tree and over synthetic fixtures. Change one,
change both.

The reconstruction reads git OBJECTS and never a checkout's filesystem. Every
receipt is minted on macOS and this runs on hosted Linux; byte parity between
the two working trees is not guaranteed (`core.precomposeunicode`, EOL and
executable-bit materialization, symlink representation), so the objects are the
only common ground.

Exit status is 0 whether or not the estate is discharged — the verdict travels
on stdout. Exit 2 is reserved for invalid usage. Any other failure of this
script leaves the workflow's `if:` conjunct false, so the estate runs.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

# The manifest record format this file reproduces. Bumped in lockstep with
# `MANIFEST_SCHEMA` in the private kernel; the parity test is what enforces the
# lockstep.
MANIFEST_SCHEMA = 1

# The one supported receipt wire version. A future shape is REFUSED rather than
# partially interpreted: a field this version does not know about may be the
# one that makes an otherwise-valid receipt inapplicable.
RECEIPT_WIRE_VERSION = 1

# The one gate whose receipt can discharge a full-estate run. Equal by
# construction to `RECEIPT_GATE_ID_ACCEPTED` in `bin/cctally-test-remote`, and
# a test reads that literal out of the wrapper and compares it here — drift
# would refuse every receipt while looking exactly like "no receipt matched".
GATE_ID_FULL_SUITE = "cctally-test-remote/full-suite@1"

TYPE_FILE, TYPE_DIR, TYPE_LINK = "f", "d", "l"
MODE_EXEC, MODE_PLAIN, MODE_LINK = "0755", "0644", "0777"

# The four roots the transport owns rather than ships. `build_manifest` SKIPS
# them; `_stage_selection_driver` REFUSES a selected entry inside one, and the
# driver is the contract a receipt was minted under, so this refuses too.
PROTECTED_ROOTS = (b".git", b".venv", b"dashboard/web/node_modules",
                   b".cctally-remote-state")
# Excluded by BASENAME at every depth: either machine may create one at any
# moment, so including it makes the equality untestable rather than strict.
IGNORED_NAMES = frozenset({b".DS_Store"})

_SAFE = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    b"-._~/+,:@=[]{}()!$&'"
)

_HEX40 = re.compile(r"\A[0-9a-f]{40}\Z")

# A hosted checkout of this repository holds a few tens of megabytes of tracked
# content, and the reconstruction streams it rather than buffering it, so this
# is a guard against a pathological object rather than a working limit.
_READ_CHUNK = 1 << 20


class ReconstructionRefused(Exception):
    """The commit cannot be reconstructed into a comparable manifest.

    Raised for the two shapes `_stage_selection_driver` refuses outright: a
    gitlink, and an entry inside a root the runner owns. Neither can be
    compared against a receipt, so the estate runs.
    """


def _quote(raw: bytes) -> str:
    out = []
    for byte in raw:
        if byte in _SAFE:
            out.append(chr(byte))
        else:
            out.append("%%%02X" % byte)
    return "".join(out)


def _git(repo, *args, binary=False):
    proc = subprocess.run(["git", "-C", repo, *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            "git %s failed: %s"
            % (" ".join(args), proc.stderr.decode("utf-8", "replace").strip()))
    return proc.stdout if binary else proc.stdout.decode()


def _read_exact(stream, count):
    parts = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(min(remaining, _READ_CHUNK))
        if not chunk:
            raise RuntimeError("git cat-file --batch ended mid-object")
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def _blob_facts(repo, oids, link_oids):
    """`{oid: (sha256-hex, raw-bytes-or-None)}` for every requested blob.

    The raw bytes are retained only for symlink blobs, whose content IS the
    link target and is a path rather than a file. Everything else is reduced to
    its digest as it streams past, so a large tracked asset never lands in
    memory whole.

    The request list is written to a temporary file and handed to the child as
    its stdin, rather than being pushed through a pipe while the same process
    reads the response: a few thousand OIDs exceed the pipe buffer, and a
    single-threaded write-then-read deadlocks at exactly the tree size that
    matters.
    """
    facts = {}
    if not oids:
        return facts
    with tempfile.TemporaryFile() as requests:
        requests.write(b"".join(oid + b"\n" for oid in oids))
        requests.seek(0)
        proc = subprocess.Popen(
            ["git", "-C", repo, "cat-file", "--batch"],
            stdin=requests, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for oid in oids:
                header = proc.stdout.readline()
                if not header:
                    raise RuntimeError("git cat-file --batch produced no record "
                                       "for %s" % oid.decode("ascii", "replace"))
                fields = header.split()
                if len(fields) != 3:
                    raise RuntimeError(
                        "git cat-file --batch refused %s: %s"
                        % (oid.decode("ascii", "replace"),
                           header.decode("utf-8", "replace").strip()))
                size = int(fields[2])
                if oid in link_oids:
                    body = _read_exact(proc.stdout, size)
                    facts[oid] = (hashlib.sha256(body).hexdigest(), body)
                else:
                    digest = hashlib.sha256()
                    remaining = size
                    while remaining > 0:
                        chunk = proc.stdout.read(min(remaining, _READ_CHUNK))
                        if not chunk:
                            raise RuntimeError(
                                "git cat-file --batch ended mid-object")
                        digest.update(chunk)
                        remaining -= len(chunk)
                    facts[oid] = (digest.hexdigest(), None)
                _read_exact(proc.stdout, 1)  # the record's trailing newline
        finally:
            proc.stdout.close()
            stderr = proc.stderr.read()
            proc.stderr.close()
            rc = proc.wait()
        if rc != 0:
            raise RuntimeError("git cat-file --batch exited %d: %s"
                               % (rc, stderr.decode("utf-8", "replace").strip()))
    return facts


def reconstruct_manifest(sha: str, repo: str = ".") -> bytes:
    """The canonical manifest for a commit, built from git objects only.

    `-z` with raw-byte parsing is REQUIRED rather than convenient: plain
    `ls-tree` quotes an unusual name itself, while this format quotes raw bytes
    and orders records by raw path bytes, so a quoted form would both mis-encode
    the record and mis-order it. Blobs are looked up by OID, never by path.

    Every directory implied by the selection gets its own record, because the
    kernel walks a real tree and necessarily sees directories. An EMPTY
    directory needs no record, because git cannot represent one.
    """
    raw = _git(repo, "ls-tree", "-r", "-z", sha, binary=True)
    entries = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        fields = meta.split(b" ")
        mode, oid = fields[0], fields[2]
        if mode == b"160000":
            raise ReconstructionRefused(
                "%s is a gitlink (submodule entry); the transport ships one "
                "repository, not a superproject" % _quote(path))
        # Deliberate asymmetry with `_stage_selection_driver`, which tests the
        # ignored basename BEFORE the protected root: a tracked `.DS_Store`
        # inside a protected root makes the driver skip the entry and makes
        # this refuse the whole reconstruction. Unreachable on any tree this
        # repository produces, and fail-closed either way — a refusal ends as
        # `discharged=false`, which runs the estate.
        for guarded in PROTECTED_ROOTS:
            if path == guarded or path.startswith(guarded + b"/"):
                raise ReconstructionRefused(
                    "%s is inside %s, a root the runner owns"
                    % (_quote(path), _quote(guarded)))
        if path.rsplit(b"/", 1)[-1] in IGNORED_NAMES:
            continue
        entries.append((path, mode, oid))

    link_oids = {oid for _, mode, oid in entries if mode == b"120000"}
    facts = _blob_facts(repo, [oid for _, _, oid in entries], link_oids)

    records, dirs = [], set()
    for path, mode, oid in entries:
        parts = path.split(b"/")
        for index in range(1, len(parts)):
            dirs.add(b"/".join(parts[:index]))
        if mode == b"120000":
            target = facts[oid][1]
            line = "%s %s %s %s %s" % (_quote(path), TYPE_LINK, MODE_LINK, "",
                                       _quote(target))
        else:
            normalized = MODE_EXEC if mode == b"100755" else MODE_PLAIN
            line = "%s %s %s %s %s" % (_quote(path), TYPE_FILE, normalized,
                                       facts[oid][0], "")
        records.append((path, line))
    for directory in dirs:
        records.append((directory, "%s %s %s %s %s"
                        % (_quote(directory), TYPE_DIR, MODE_EXEC, "", "")))
    ordered = sorted(records, key=lambda item: item[0])
    if not ordered:
        return b""
    return ("\n".join(line for _, line in ordered) + "\n").encode("ascii")


def manifest_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def toolchain_digest(repo: str = "."):
    """`sha256:<hex>` over the declared closure, or None when it is absent.

    Byte-for-byte the value `_receipt_toolchain_digest` freezes into a receipt,
    so a toolchain change since the run refuses the receipt rather than
    discharging a gate the current closure never ran under.
    """
    path = os.path.join(repo, "tests", "requirements-dev.txt")
    try:
        with open(path, "rb") as handle:
            return "sha256:" + hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def verify_candidate(receipt, *, tree_oid, ref_namespace, reconstructed_digest,
                     toolchain_digest):
    """Return None when the candidate verifies, else the reason it does not.

    Every condition is deciding. A candidate that fails any one of them
    contributes NO evidence, and it does not veto a sibling that verifies:
    otherwise one contaminated receipt would permanently poison a tree.
    """
    if not isinstance(receipt, dict):
        return "it is not a JSON object"
    if receipt.get("schemaVersion") != RECEIPT_WIRE_VERSION:
        return ("its wire version %r is not the supported version %d"
                % (receipt.get("schemaVersion"), RECEIPT_WIRE_VERSION))
    recorded_tree = receipt.get("testedTreeOid") or ""
    if not isinstance(recorded_tree, str) or not _HEX40.match(recorded_tree):
        return ("it names %r, which is not a tree oid"
                % (receipt.get("testedTreeOid"),))
    if recorded_tree != ref_namespace:
        return ("it is filed under %s but names tree %s"
                % (ref_namespace, recorded_tree))
    if recorded_tree != tree_oid:
        return ("it names tree %s, and the pushed commit's tree is %s"
                % (recorded_tree, tree_oid))
    if receipt.get("authoritative") is not True:
        return "the run was not authoritative"
    if receipt.get("timezone") != "Etc/UTC":
        return "the run executed under timezone %r" % (receipt.get("timezone"),)
    if receipt.get("gateId") != GATE_ID_FULL_SUITE:
        return ("its recorded gate is %r, not the releasable full-suite gate %r"
                % (receipt.get("gateId") or "<none>", GATE_ID_FULL_SUITE))
    if receipt.get("declaredToolchainDigest") != toolchain_digest:
        return ("the declared toolchain changed (%s recorded, %s now)"
                % (receipt.get("declaredToolchainDigest") or "<none>",
                   toolchain_digest))
    if receipt.get("outcome") != "pass" or receipt.get("exitCode") != 0:
        return ("the run did not pass (outcome=%r exitCode=%r)"
                % (receipt.get("outcome"), receipt.get("exitCode")))
    if receipt.get("failureClass") != "none":
        return "the run recorded failureClass=%r" % (receipt.get("failureClass"),)
    coverage = receipt.get("coverage")
    if not isinstance(coverage, dict):
        return "the receipt states no coverage"
    if coverage.get("mode") != "full" or coverage.get("pytest") != "full":
        return ("the run did not cover the whole estate (mode=%r pytest=%r)"
                % (coverage.get("mode"), coverage.get("pytest")))
    if coverage.get("omittedHarnesses"):
        return "the run omitted %d harness(es)" % len(coverage["omittedHarnesses"])
    if receipt.get("testedTreeDigest") != reconstructed_digest:
        return ("the tree it tested does not reconstruct (digest %s, now %s)"
                % ((receipt.get("testedTreeDigest") or "<none>")[:12],
                   reconstructed_digest[:12]))
    return None


def _load_candidates(directory):
    """`[(name, parsed-or-None)]` for every file in the receipts directory."""
    candidates = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return candidates
    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                candidates.append((name, json.load(handle)))
        except (OSError, ValueError):
            candidates.append((name, None))
    return candidates


def _write_summary(path, accepted, tree_oid, digest, reasons):
    if not path:
        return
    lines = ["## Receipt gate", ""]
    if accepted:
        lines += [
            "The estate is **discharged** for this push.",
            "",
            "| Fact | Value |",
            "| --- | --- |",
            "| Accepted run | `%s` |" % accepted.get("runId", "<none>"),
            "| Tree OID | `%s` |" % tree_oid,
            "| Manifest digest | `%s` |" % digest,
            "| Tested head | `%s` |" % accepted.get("testedHead", "<none>"),
        ]
    else:
        lines += ["No authoritative receipt discharged this tree, so the "
                  "estate runs.", ""]
        if tree_oid:
            lines.append("Tree OID `%s`, reconstructed digest `%s`."
                         % (tree_oid, digest or "<unavailable>"))
            lines.append("")
        for name, reason in reasons:
            lines.append("- `%s`: %s" % (name, reason))
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        pass


def _decide(ns):
    reasons = []
    tree_oid = ""
    digest = ""
    try:
        tree_oid = _git(ns.repo, "rev-parse", "%s^{tree}" % ns.sha).strip()
    except RuntimeError as exc:
        reasons.append(("<repository>", str(exc)))
    if tree_oid and not _HEX40.match(tree_oid):
        reasons.append(("<repository>",
                        "git resolved %r, which is not a tree oid" % tree_oid))
        tree_oid = ""
    if tree_oid:
        try:
            digest = manifest_digest(reconstruct_manifest(ns.sha, repo=ns.repo))
        except (ReconstructionRefused, RuntimeError) as exc:
            reasons.append(("<repository>", str(exc)))
    declared = toolchain_digest(ns.repo)
    if declared is None:
        reasons.append(("<repository>",
                        "tests/requirements-dev.txt is absent, so no receipt "
                        "can be checked against the declared toolchain"))
    accepted = None
    if tree_oid and digest and declared is not None:
        for name, receipt in _load_candidates(ns.receipts_dir):
            if receipt is None:
                reasons.append((name, "it is not readable JSON"))
                continue
            reason = verify_candidate(
                receipt,
                tree_oid=tree_oid,
                ref_namespace=ns.ref_namespace,
                reconstructed_digest=digest,
                toolchain_digest=declared,
            )
            if reason is None:
                accepted = receipt
                break
            reasons.append((name, reason))
    return accepted, tree_oid, digest, reasons


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Decide whether a receipt discharges the estate.")
    parser.add_argument("--sha", required=True,
                        help="the full 40-hex commit the push is at")
    parser.add_argument("--repo", default=".",
                        help="the checkout to reconstruct from")
    parser.add_argument("--receipts-dir",
                        help="a directory of candidate receipt JSON files")
    parser.add_argument("--ref-namespace",
                        help="the tree oid the candidates were fetched under")
    parser.add_argument("--summary-file",
                        help="append a job step summary here")
    parser.add_argument("--self-test-manifest", action="store_true",
                        help="print only the reconstructed manifest digest")
    ns = parser.parse_args(argv)

    if not _HEX40.match(ns.sha or ""):
        parser.error("--sha must be a full 40-hex commit id")

    if ns.self_test_manifest:
        try:
            print(manifest_digest(reconstruct_manifest(ns.sha, repo=ns.repo)))
        except (ReconstructionRefused, RuntimeError) as exc:
            sys.stderr.write("receipt-gate: %s\n" % exc)
            return 1
        return 0

    if not ns.receipts_dir or not ns.ref_namespace:
        parser.error("--receipts-dir and --ref-namespace are both required")
    if not _HEX40.match(ns.ref_namespace):
        parser.error("--ref-namespace must be a full 40-hex tree oid")

    accepted, tree_oid, digest, reasons = _decide(ns)
    for name, reason in reasons:
        sys.stderr.write("receipt-gate: %s contributes no evidence: %s\n"
                         % (name, reason))
    if accepted is not None:
        sys.stderr.write(
            "receipt-gate: run %s discharges tree %s at digest %s\n"
            % (accepted.get("runId", "<none>"), tree_oid, digest))
    else:
        sys.stderr.write("receipt-gate: no candidate discharged this tree; "
                         "the estate runs\n")
    _write_summary(ns.summary_file, accepted, tree_oid, digest, reasons)
    print("discharged=%s" % ("true" if accepted is not None else "false"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""The A5s mutation runner (#940 spec revision 7j, section 6.7, R7j-V1).

Proves that every maintained non-vacuity patch in
`tests/fixtures/a5s-nonvacuity/` is detected by the retention instrument,
node by node, on whatever lane runs it. The lane supplies its own test
command after `--` (the remote wrapper on Darwin, `python -m pytest` on
Ubuntu); the runner appends pytest's per-node short summary (`-rA`, with
`-vv` so the summary carries each failure's whole message instead of one
cut to the terminal width) and the target node ids.

1. The tree must be clean, and the patch files on disk must be exactly the
   patches the directory's README table names.
2. Baseline: the unpatched target set runs once, and every target node must
   pass.
3. For each patch: apply it, run its target nodes, reverse it, and require a
   clean tree again.
4. A patch is detected only when pytest exits 1 and **every** target node
   the README names for it is reported with only failing outcomes, one of
   them its designated outcome (`FAILED`, or `ERROR` for a designated setup
   failure) carrying its designated failure text. A child-path case is
   named by the parent node that runs it.

A missing, passing, skipped or uncollected target node fails qualification,
and so does every other exit: a collection, usage or internal error, a
wrapper or transport failure, an interruption. None of them counts as
detection. Exit status: 0 qualified, 1 not qualified, 2 usage error.

Run from the repository root, for example:

    python3 tests/_a5s_mutation_runner.py -- python3 -m pytest -p no:cacheprovider
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from typing import NamedTuple

PATCH_DIR = "tests/fixtures/a5s-nonvacuity"
TABLE_HEADER = ("Patch", "Node", "Outcome", "Failure text")
OUTCOMES = ("FAILED", "ERROR")
SUMMARY_WORDS = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS")
SUMMARY_FLAGS = ("-rA", "-vv")
_SUMMARY_START = re.compile(r"^=+ short test summary info =+$")
_SUMMARY_END = re.compile(r"^=+ .* =+$")
_CELL = re.compile(r"^`([^`]+)`$")


class Target(NamedTuple):
    node: str
    outcome: str
    text: str


class QualificationError(Exception):
    """The inputs cannot be qualified at all (README, patch set, tree)."""


def parse_readme(text):
    """`{patch: [Target, ...]}` in README order, from the per-node table."""
    lines = text.splitlines()
    for start, line in enumerate(lines):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and tuple(cells) == TABLE_HEADER:
            break
    else:
        raise QualificationError("the README has no per-node table (%s)"
                                 % " | ".join(TABLE_HEADER))
    plan = {}
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        values = [(_CELL.match(c) or [None, None])[1] for c in cells]
        if len(cells) != 4 or None in values:
            raise QualificationError("malformed table row: %s" % line)
        patch, node, outcome, failure = values
        if not patch.endswith(".patch") or "::" not in node:
            raise QualificationError("malformed table row: %s" % line)
        if outcome not in OUTCOMES:
            raise QualificationError("outcome %r is not one of %s"
                                     % (outcome, "/".join(OUTCOMES)))
        targets = plan.setdefault(patch, [])
        if any(t.node == node for t in targets):
            raise QualificationError("%s names %s twice" % (patch, node))
        targets.append(Target(node, outcome, failure))
    if not plan:
        raise QualificationError("the README's per-node table is empty")
    return plan


def parse_summary(output):
    """`(entries, folded_skips)`: entries maps each reported node id to its
    `[(word, message), ...]` in pytest's short test summary; a skip folded
    without a node id is kept apart."""
    lines = output.splitlines()
    starts = [i for i, line in enumerate(lines) if _SUMMARY_START.match(line)]
    entries, folded = {}, []
    if not starts:
        return entries, folded
    current = None
    for line in lines[starts[-1] + 1:]:
        if _SUMMARY_END.match(line):
            break
        word, _, rest = line.partition(" ")
        if word in SUMMARY_WORDS and rest:
            if word == "SKIPPED" and rest.startswith("["):
                folded.append(line)
                current = None
                continue
            node, _, message = rest.partition(" - ")
            current = [word, message]
            entries.setdefault(node.strip(), []).append(current)
        elif current is not None:
            current[1] += "\n" + line
    return entries, folded


def _freeze(entries):
    return {node: [tuple(e) for e in found] for node, found in entries.items()}


def judge_baseline(returncode, output, nodes):
    entries, folded = parse_summary(output)
    entries = _freeze(entries)
    problems = []
    if returncode != 0:
        problems.append("the unpatched run exited %d, not 0" % returncode)
    if folded:
        problems.append("a target node was skipped: %s" % folded[0])
    for node in nodes:
        found = entries.get(node)
        if not found:
            problems.append("%s: not reported" % node)
        elif any(word != "PASSED" for word, _m in found):
            problems.append("%s: %s unpatched" % (
                node, "/".join(word for word, _m in found)))
    return problems


def judge_patch(returncode, output, targets):
    entries, folded = parse_summary(output)
    entries = _freeze(entries)
    problems = []
    if returncode != 1:
        problems.append("pytest exited %d, not 1: an infrastructure outcome "
                        "never counts as detection" % returncode)
    if folded:
        problems.append("a target node was skipped: %s" % folded[0])
    for target in targets:
        found = entries.get(target.node)
        if not found:
            problems.append("%s: not reported (missing or uncollected)"
                            % target.node)
            continue
        others = sorted({w for w, _m in found if w not in OUTCOMES})
        if others:
            problems.append("%s: reported %s" % (target.node,
                                                  "/".join(others)))
            continue
        if not any(word == target.outcome and target.text in message
                   for word, message in found):
            problems.append("%s: did not fail at its designated assertion "
                            "(%s carrying %r); got %s" % (
                                target.node, target.outcome, target.text,
                                "; ".join("%s %s" % (w, m.splitlines()[0]
                                                     if m else "")
                                          for w, m in found)))
    return problems


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                          text=True)


def _require_clean(root, when):
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        raise QualificationError("git status failed %s: %s"
                                 % (when, status.stderr.strip()))
    if status.stdout.strip():
        raise QualificationError("the tree is not clean %s:\n%s"
                                 % (when, status.stdout.rstrip()))


def _run(command, nodes, root, out):
    """Run the lane's command over `nodes`, streaming its output."""
    argv = list(command) + list(SUMMARY_FLAGS) + list(nodes)
    out.write("a5s-mutation-runner: $ %s\n" % " ".join(argv))
    out.flush()
    try:
        proc = subprocess.Popen(argv, cwd=str(root), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                errors="replace")
    except OSError as exc:
        out.write("a5s-mutation-runner: cannot start the command: %s\n" % exc)
        return 127, ""
    chunks = []
    with proc:
        for line in proc.stdout:
            chunks.append(line)
            out.write(line)
    out.flush()
    return proc.returncode, "".join(chunks)


def run_qualification(root, command, only=None, out=None):
    """Baseline, then every selected patch; 0 when every patch is
    detected, 1 otherwise."""
    out = out or sys.stdout
    root = pathlib.Path(root)
    patch_dir = root / PATCH_DIR
    try:
        plan = parse_readme((patch_dir / "README.md").read_text(
            encoding="utf-8"))
        on_disk = sorted(p.name for p in patch_dir.glob("*.patch"))
        if on_disk != sorted(plan):
            raise QualificationError(
                "the patch files %s are not the README's patches %s"
                % (on_disk, sorted(plan)))
        selected = [name for name in plan
                    if not only or any(name.startswith(o) for o in only)]
        if not selected:
            raise QualificationError("no patch matches %s" % (only,))
        _require_clean(root, "before the baseline")
    except (OSError, QualificationError) as exc:
        out.write("a5s-mutation-runner: NOT QUALIFIED: %s\n" % exc)
        return 1

    nodes = []
    for name in selected:
        for target in plan[name]:
            if target.node not in nodes:
                nodes.append(target.node)
    rc, output = _run(command, nodes, root, out)
    problems = judge_baseline(rc, output, nodes)
    if problems:
        out.write("a5s-mutation-runner: baseline FAILED; no patch was run\n")
        for problem in problems:
            out.write("  - %s\n" % problem)
        out.write("a5s-mutation-runner: NOT QUALIFIED\n")
        return 1
    out.write("a5s-mutation-runner: baseline passed (%d target nodes)\n"
              % len(nodes))

    results = {}
    for name in selected:
        patch = str(patch_dir / name)
        check = _git(root, "apply", "--check", patch)
        if check.returncode != 0:
            results[name] = ["the patch does not apply: %s"
                             % check.stderr.strip()]
            continue
        applied = _git(root, "apply", patch)
        if applied.returncode != 0:
            results[name] = ["git apply failed: %s" % applied.stderr.strip()]
            continue
        try:
            rc, output = _run(command, [t.node for t in plan[name]], root,
                              out)
        finally:
            reverse = _git(root, "apply", "-R", patch)
        if reverse.returncode != 0:
            out.write("a5s-mutation-runner: NOT QUALIFIED: %s did not "
                      "reverse: %s\n" % (name, reverse.stderr.strip()))
            return 1
        try:
            _require_clean(root, "after reversing %s" % name)
        except QualificationError as exc:
            out.write("a5s-mutation-runner: NOT QUALIFIED: %s\n" % exc)
            return 1
        results[name] = judge_patch(rc, output, plan[name])

    out.write("a5s-mutation-runner: per-patch results\n")
    for name in selected:
        problems = results[name]
        out.write("  %s: %s\n" % (name, "DETECTED" if not problems
                                   else "NOT DETECTED"))
        for target in plan[name]:
            out.write("      %s %s %r\n" % (target.outcome, target.node,
                                            target.text))
        for problem in problems:
            out.write("    - %s\n" % problem)
    qualified = all(not results[name] for name in selected)
    out.write("a5s-mutation-runner: %s\n" % ("QUALIFIED" if qualified
                                              else "NOT QUALIFIED"))
    return 0 if qualified else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        sys.stderr.write("usage: _a5s_mutation_runner.py [--root DIR] "
                         "[--patch PREFIX ...] -- TEST-COMMAND ...\n")
        return 2
    split = argv.index("--")
    parser = argparse.ArgumentParser(prog="_a5s_mutation_runner.py")
    parser.add_argument("--root", default=str(
        pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument("--patch", action="append", default=[],
                        help="run only patches whose name starts with this")
    try:
        args = parser.parse_args(argv[:split])
    except SystemExit:
        return 2
    command = argv[split + 1:]
    if not command:
        sys.stderr.write("a5s-mutation-runner: no test command after --\n")
        return 2
    return run_qualification(args.root, command, args.patch or None)


if __name__ == "__main__":
    sys.exit(main())

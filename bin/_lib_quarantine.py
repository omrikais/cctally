#!/usr/bin/env python3
"""The one parser of tests/quarantine.json, the reviewed flaky-test list (spec 4.3).

Stdlib only. An entry's `id` takes one of two forms:

* a pytest node id, `tests/<file>.py::<node>` (a prefix: pytest's --deselect
  semantics cover every parametrization of a listed function);
* a harness case, `harness:<harness-name>::<case>`, where `<harness-name>` is
  the `NAME` of `bin/cctally-NAME-test` and `<case>` is one scenario of that
  harness's own case inventory (`bin/cctally-NAME-test --list-cases`).

`bin/cctally-test-all`, `bin/_lib_merge_gate.py` and the quick gate read the
list through this module; the scheduled CI quarantine leg calls its CLI. An
absent list quarantines nothing (the public mirror may carry none); a list
that exists but cannot be read raises QuarantineError, never "nothing listed".

CLI (exit 0, or 2 on an unreadable list or bad usage):
    python3 bin/_lib_quarantine.py pytest-ids
    python3 bin/_lib_quarantine.py harness-cases <harness>
"""

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys

LIST_PATH = Path('tests') / 'quarantine.json'
FIELDS = ('id', 'evidence', 'invariant', 'issue', 'replacement', 'reviewBy')
PYTEST_ID = re.compile(r'tests/[^:\s]+\.py::\S+')
HARNESS_ID = re.compile(r'harness:([a-z0-9][a-z0-9-]*)::([A-Za-z0-9][A-Za-z0-9_.-]*)')


class QuarantineError(ValueError):
    """tests/quarantine.json exists but is not a list this parser can apply."""


@dataclass(frozen=True)
class Entry:
    id: str
    kind: str                 # 'pytest' or 'harness'
    harness: str | None       # the harness name for a harness case
    case: str | None          # the case name for a harness case
    fields: dict


def parse_id(entry_id: str) -> tuple[str, str | None, str | None]:
    """(kind, harness, case) of one id; QuarantineError for any other form."""
    if not isinstance(entry_id, str):
        raise QuarantineError(f'an entry id must be a string, not {entry_id!r}')
    if PYTEST_ID.fullmatch(entry_id):
        return 'pytest', None, None
    match = HARNESS_ID.fullmatch(entry_id)
    if match:
        return 'harness', match.group(1), match.group(2)
    raise QuarantineError(
        f'{entry_id!r} is neither a pytest node id (tests/<file>.py::<node>) '
        'nor a harness case (harness:<harness>::<case>)')


def parse(doc) -> list[Entry]:
    if not isinstance(doc, list):
        raise QuarantineError('expected a JSON list of entries')
    entries = []
    for item in doc:
        if not isinstance(item, dict):
            raise QuarantineError(f'expected each entry to be an object, not {item!r}')
        kind, harness, case = parse_id(item.get('id'))
        entries.append(Entry(item['id'], kind, harness, case, dict(item)))
    ids = [e.id for e in entries]
    if len(set(ids)) != len(ids):
        raise QuarantineError('duplicate entry ids: ' + ', '.join(
            sorted({i for i in ids if ids.count(i) > 1})))
    return entries


def load_path(path: Path) -> list[Entry]:
    path = Path(path)
    if not path.is_file():
        return []
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise QuarantineError(f'{path}: {exc}') from None
    return parse(doc)


def load(repo: Path) -> list[Entry]:
    return load_path(Path(repo) / LIST_PATH)


def pytest_ids(entries) -> list[str]:
    return [e.id for e in entries if e.kind == 'pytest']


def harness_cases(entries, harness: str) -> list[str]:
    return [e.case for e in entries if e.kind == 'harness' and e.harness == harness]


def harness_inventory(repo: Path, harness: str, *, timeout: float = 60) -> list[str]:
    """The case names `bin/cctally-<harness>-test --list-cases` prints.

    QuarantineError when the harness does not exist or has no case inventory:
    a harness case that cannot be validated against the harness is not
    quarantinable."""
    script = Path(repo) / 'bin' / f'cctally-{harness}-test'
    if not script.is_file():
        raise QuarantineError(f'harness:{harness}: bin/cctally-{harness}-test does not exist')
    proc = subprocess.run([str(script), '--list-cases'], cwd=repo, capture_output=True,
                          text=True, timeout=timeout)
    if proc.returncode != 0:
        raise QuarantineError(f'harness:{harness}: --list-cases exited {proc.returncode}: '
                              + (proc.stderr or proc.stdout).strip()[-400:])
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def validate_harness_cases(repo: Path, entries) -> None:
    """Each harness case names an existing harness and a case in its inventory."""
    inventories: dict[str, list[str]] = {}
    for entry in entries:
        if entry.kind != 'harness':
            continue
        if entry.harness not in inventories:
            inventories[entry.harness] = harness_inventory(repo, entry.harness)
        if entry.case not in inventories[entry.harness]:
            raise QuarantineError(f'{entry.id}: no case {entry.case!r} in the '
                                  f'{entry.harness} harness inventory')


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo = Path(__file__).resolve().parents[1]
    if len(argv) >= 2 and argv[0] == '--repo':
        repo, argv = Path(argv[1]), argv[2:]
    try:
        if argv == ['pytest-ids']:
            out = pytest_ids(load(repo))
        elif len(argv) == 2 and argv[0] == 'harness-cases':
            out = harness_cases(load(repo), argv[1])
        else:
            print('usage: _lib_quarantine.py [--repo DIR] pytest-ids | harness-cases <harness>',
                  file=sys.stderr)
            return 2
    except QuarantineError as exc:
        print(f'quarantine: {exc}', file=sys.stderr)
        return 2
    for item in out:
        print(item)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

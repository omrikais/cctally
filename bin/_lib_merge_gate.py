"""Conservative impact selection for merge certification (stdlib only).

This is a reviewed component policy, not proof of every possible dependency.
Unknown paths and shared/infrastructure changes require the full estate. The
full background/release runs remain the cross-component backstop.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import signal
import subprocess
import sys

GATE_ID = 'cctally-test-remote/merge-focused@1'
MAX_SECONDS = 300
# All of these are leaf presentation components. Their entire frontend estate
# runs, including their consumers, instead of relying on a TS import heuristic.
PRESENTATION_FILES = frozenset(
    'dashboard/web/src/components/' + name + '.tsx'
    for name in ('ZoneTag', 'ModelLegend', 'SortableHeader', 'HelpOverlay',
                 'AlertsEmptyGauge', 'SessionsControls', 'BasketChip', 'Toast')
)
SMOKE_FILES = ('tests/test_cli_smoke.py', 'tests/test_lib_json_envelope.py')
PRESENTATION_PYTEST = ('tests/test_dashboard_handler_static.py',)


def _git(repo, *args):
    proc = subprocess.run(['git', '--no-optional-locks', '-C', str(repo), *args],
                          capture_output=True)
    if proc.returncode:
        raise ValueError('git revision/diff failed: ' + proc.stderr.decode(errors='replace').strip())
    return proc.stdout


def _changed_names(data):
    """--name-only -z lists both endpoints when rename detection is disabled."""
    return [p.decode('utf-8', errors='strict') for p in data.split(b'\0') if p]


def basis(repo, base_rev, head='HEAD', *, working=True):
    if not base_rev or base_rev.startswith('-'):
        raise ValueError('base revision must be nonempty and must not be an option')
    base = _git(repo, 'merge-base', base_rev, head).decode().strip()
    head_oid = _git(repo, 'rev-parse', '--verify', head + '^{commit}').decode().strip()
    committed = set(_changed_names(_git(repo, 'diff', '--no-renames', '--name-only', '-z', base, head_oid)))
    dirty = set()
    if working:
        dirty.update(_changed_names(_git(repo, 'diff', '--no-renames', '--name-only', '-z', head_oid)))
        dirty.update(_changed_names(_git(repo, 'ls-files', '--others', '--exclude-standard', '-z')))
        if _git(repo, 'ls-files', '-u'):
            raise ValueError('the index has unmerged entries')
    return {'baseOid': base, 'headOid': head_oid,
            'committedPaths': len(committed), 'worktreePaths': len(dirty),
            'paths': sorted(committed | dirty)}


def policy_digest(repo):
    return 'sha256:' + hashlib.sha256(
        (Path(repo) / 'bin/_lib_merge_gate.py').read_bytes()).hexdigest()


def _document(path):
    return path in ('README.md', 'CHANGELOG.md') or (
        path.startswith('docs/commands/') and path.endswith('.md'))


def _frontend_test(path):
    return path.startswith('dashboard/web/src/') and path.endswith(('.test.ts', '.test.tsx'))


def select(repo, paths):
    repo = Path(repo)
    manifest = json.loads((repo / 'tests/authoritative-test-manifest.json').read_text())
    private = (repo / '.mirror-allowlist').exists()
    names = [r['name'] for r in manifest['harnesses']
             if private or r.get('visibility', 'public') == 'public']
    if not names or len(names) != len(set(names)):
        raise ValueError('invalid harness manifest')
    paths = sorted(set(paths))
    reasons, components = [], set()
    for path in paths:
        p = PurePosixPath(path)
        if p.is_absolute() or '..' in p.parts or any(c.isspace() for c in path):
            reasons.append('unclassified path: ' + path)
        elif _document(path):
            components.add('documentation')
        elif path in PRESENTATION_FILES or _frontend_test(path):
            components.add('presentation')
        elif path.startswith('dashboard/static/'):
            components.add('presentation')
        elif path in PRESENTATION_PYTEST:
            components.add('presentation')
        else:
            reasons.append('shared, high-risk, or unclassified path: ' + path)
    harnesses = {'doc-lint'}
    files = set(SMOKE_FILES)
    if 'presentation' in components:
        harnesses.add('frontend')
        files.update(PRESENTATION_PYTEST)
    if not harnesses <= set(names):
        reasons.append('required merge harness absent from manifest')
    if any(not (repo / p).is_file() for p in files):
        reasons.append('required merge pytest file absent')
    full = bool(reasons)
    return {
        'mode': 'full' if full else 'merge-focused',
        'pytest': 'full' if full else 'selected',
        'selectedHarnesses': [n for n in names if full or n in harnesses],
        'omittedHarnesses': [n for n in names if not full and n not in harnesses],
        'selectedPytestFiles': [] if full else sorted(files),
        'components': sorted(components), 'reasons': reasons,
        'policyDigest': policy_digest(repo), 'maxWallSeconds': MAX_SECONDS,
    }


def plan(repo, base_rev, head='HEAD', *, working=True):
    b = basis(repo, base_rev, head, working=working)
    result = select(repo, b['paths'])
    result['mergeBasis'] = b
    return result


def filter_legs(plan_record, directory):
    """Narrow both expected-node legs to selected files; retain serial ownership."""
    directory = Path(directory)
    selected = set(plan_record['selectedPytestFiles'])
    found = set()
    for name in ('pytest', 'benchmark'):
        path = directory / (name + '.txt')
        nodes = [n for n in path.read_text().splitlines() if n.split('::')[0] in selected]
        found.update(n.split('::')[0] for n in nodes)
        path.write_text(''.join(n + '\n' for n in nodes))
    if found != selected:
        raise ValueError('selected files absent from recorded pytest estate: ' + ', '.join(sorted(selected - found)))


def background_health(rows):
    """Newest completed full verification wins; running work hides nothing."""
    for row in rows:
        if row.get('head_branch') != 'main' or row.get('status') != 'completed':
            continue
        if row.get('event') not in ('schedule', 'workflow_dispatch') and row.get('fullSuite') is not True:
            continue
        passed = row.get('conclusion') == 'success'
        return {'state': 'passed' if passed else 'failed', 'allowFocused': passed,
                'runId': row.get('id'), 'headSha': row.get('head_sha')}
    return {'state': 'unavailable', 'allowFocused': False, 'runId': None, 'headSha': None}


def read_background_health(repo):
    """Read actual full test-macos job results, never a receipt-discharge skip.

    Existing full CI jobs provide a bootstrap before the new schedule lands.
    A successful workflow with a skipped full job is not full verification.
    """
    def gh(*args):
        process = subprocess.run(['gh', *args], cwd=repo, capture_output=True,
                                 text=True, timeout=20)
        if process.returncode:
            raise ValueError('GitHub full-verification evidence is unavailable')
        return json.loads(process.stdout)
    try:
        name = gh('repo', 'view', '--json', 'nameWithOwner')['nameWithOwner']
        rows = gh('api', f'repos/{name}/actions/workflows/ci.yml/runs?branch=main&per_page=20')['workflow_runs']
        for row in rows:
            if row.get('status') != 'completed':
                # GitHub reuses the run ID on retry: the earlier failed
                # attempt disappears from this listing while the retry runs.
                # Do not let that mutation reveal an older successful run.
                if row.get('run_attempt', 1) != 1:
                    return background_health([])
                continue
            jobs = gh('api', f'repos/{name}/actions/runs/{row["id"]}/jobs?per_page=100')['jobs']
            full = next((j for j in jobs if j.get('name') == 'test-macos'
                         and j.get('status') == 'completed'
                         and j.get('conclusion') != 'skipped'), None)
            if full is not None:
                record = dict(row, fullSuite=True, conclusion=full.get('conclusion'))
                return background_health([record])
            if row.get('event') in ('schedule', 'workflow_dispatch'):
                # A scheduled/manual attempt must actually run the full job.
                # Only receipt-discharged ordinary pushes may be skipped while
                # looking for the last full verification.
                return background_health([])
            # ci.yml can skip its declared full job on a successful private
            # main push only through a successful release/receipt gate. The
            # skipped job plus successful workflow is evidence of discharge;
            # a merely successful classifier job alone is not (it may emit
            # discharged=false). Missing jobs, cancellation, and failures
            # must not silently inherit an older pass.
            discharged = (
                row.get('event') == 'push' and row.get('conclusion') == 'success'
                and any(j.get('name') == 'test-macos'
                        and j.get('status') == 'completed'
                        and j.get('conclusion') == 'skipped' for j in jobs)
                and any(j.get('name') in ('receipt-gate', 'release-stamp-gate')
                        and j.get('status') == 'completed'
                        and j.get('conclusion') == 'success' for j in jobs)
            )
            if not discharged:
                return background_health([])
        return background_health([])
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return background_health([])


def supervise(command, seconds):
    """Bound the complete gate, including foreground admission subprocesses.

    A fresh POSIX session contains the aggregator's separate pool/pytest
    process groups. Terminating that session also reaches children when bash
    is waiting on an admission command and cannot yet run its signal trap.
    The caller writes the ordinary contract's timeout verdict after teardown.
    """
    process = subprocess.Popen(command, start_new_session=True)

    def members():
        listing = subprocess.check_output(['ps', '-A', '-o', 'pid='], text=True)
        result = []
        for token in listing.split():
            pid = int(token)
            try:
                if os.getsid(pid) == process.pid:
                    result.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        return result

    def terminate():
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in members():
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            if sig == signal.SIGTERM:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        process.wait()

    def interrupted(signum, _frame):
        terminate()
        raise SystemExit(128 + signum)

    old = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        try:
            return process.wait(timeout=max(0, seconds))
        except subprocess.TimeoutExpired:
            terminate()
            return 124
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--base')
    parser.add_argument('--health', action='store_true')
    parser.add_argument('--required-mode', action='store_true')
    parser.add_argument('--shell', action='store_true')
    parser.add_argument('--filter-legs')
    parser.add_argument('--deadline', nargs=argparse.REMAINDER,
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.deadline is not None:
        if len(args.deadline) < 2:
            parser.error('deadline requires seconds and a command')
        return supervise(args.deadline[1:], int(args.deadline[0]))
    if args.health:
        health = read_background_health(args.repo)
        print(json.dumps(health, sort_keys=True))
        return 0 if health['allowFocused'] else 3
    if not args.base:
        parser.error('--base is required outside --health')
    try:
        result = plan(args.repo, args.base)
        if args.required_mode:
            print(result['mode'])
        elif args.filter_legs:
            filter_legs(result, args.filter_legs)
        elif args.shell:
            print('mode\t' + result['mode'])
            print('harnesses\t' + ' '.join(result['selectedHarnesses']))
            print('omitted\t' + ' '.join(result['omittedHarnesses']))
            print('pytest\t' + result['pytest'])
            print('files\t' + ' '.join(result['selectedPytestFiles']))
            print('seconds\t' + str(result['maxWallSeconds']))
            print('record\t' + json.dumps(result, sort_keys=True, separators=(',', ':')))
        else:
            print(json.dumps(result, sort_keys=True, indent=2))
    except (ValueError, OSError, KeyError, UnicodeError) as exc:
        print('merge-gate: ' + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

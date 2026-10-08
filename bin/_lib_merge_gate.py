"""Conservative impact selection for merge certification (stdlib only).

This is a reviewed component policy, not proof of every possible dependency.
Unknown paths and shared/infrastructure changes require the full estate. The
full background/release runs remain the cross-component backstop.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import time

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
# The reviewed memory publication set: both topic stores and the topic metadata.
# A private test pins it to the publication engine's own definition. Its
# configuration is NOT memory, so a change to it is ordinary task work.
MEMORY_PUBLICATION_SET = ('.claude-memory/**', '.agentmem/codex-memory/**', '.agentmem/topics.json')
MEMORY_CONFIGURATION = '.agentmem/config.json'
# Every test module that reads the memory corpus, beside the smoke files and
# the Doc lint harness: together they certify a memory-only candidate.
MEMORY_PYTEST = ('tests/test_cctally_memory.py', 'tests/test_agent_workflows.py',
                 'tests/test_session_workflow_texts.py')
# The capture publishes regular files only (a link is refused), so a memory
# path that is anything else on either side is not a memory publication.
REGULAR_MODES = ('100644', '100755')
# The instruction-text class (#894): agent-instruction prose whose every reader
# is one of INSTRUCTIONS_PYTEST. Markdown at any depth under these trees,
# nested CLAUDE.md/AGENTS.md included.
INSTRUCTION_TREES = ('.agent-workflows/skills/', '.claude/skills/', '.agents/skills/',
                     '.agent-workflows/contracts/', 'docs/superpowers/')
INSTRUCTION_NAMES = ('CLAUDE.md', 'AGENTS.md')
# Every nested instruction file outside the trees. A new one is a structural
# change to the instruction chain: its readers depend on where it lands, so it
# selects the full estate until a reviewed policy change lists it here.
NESTED_INSTRUCTION_FILES = ('bin/AGENTS.md', 'dashboard/AGENTS.md')
# The generator inputs of the root AGENTS.md (its configuration is not one).
INSTRUCTION_INPUTS = ('.agentmem/codex.md', '.agentmem/manifest.json')
# Every test module that reads, validates or scans an instruction path,
# beside the smoke files and the Doc lint harness.
INSTRUCTIONS_PYTEST = ('tests/test_agent_workflows.py', 'tests/test_session_workflow_texts.py',
                       'tests/test_cctally_memory.py', 'tests/test_session_issues.py',
                       'tests/test_session_provenance.py', 'tests/test_linux_matrix_gate.py',
                       'tests/test_release_internals.py', 'tests/test_quota_budget_error_harness.py',
                       'tests/test_session_policy.py', 'tests/test_lib_share.py',
                       'tests/test_dashboard_source_kernel.py', 'tests/test_share_privacy_detector.py',
                       'tests/test_fixture_cache.py')
# An instruction path is eligible only as a non-executable regular file.
INSTRUCTION_MODES = ('100644',)
# The two session execution plans are documentation: their only readers are
# people and the Doc lint harness, so a plan refresh takes the documentation
# focus. Exactly these two paths; any other docs/ Markdown is unclassified.
EXECUTION_PLAN_PATHS = ('docs/cctally-claude-execution-plan.md', 'docs/cctally-execution-plan.md')


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


def memory_publication_path(path):
    """Whether `path` is in the memory publication set (the engine's glob semantics)."""
    path = path.strip('/')
    for pattern in MEMORY_PUBLICATION_SET:
        if pattern.endswith('/**'):
            prefix = pattern[:-3]
            if path == prefix or path.startswith(prefix + '/'):
                return True
        elif path == pattern:
            return True
    return False


def _tree_modes(repo, rev, paths):
    """{path: git mode} of the entries `paths` name in `rev` (absent paths are omitted)."""
    out = _git(repo, '--literal-pathspecs', 'ls-tree', '-z', '--full-tree', rev, '--', *paths)
    modes = {}
    for record in out.split(b'\0'):
        if record:
            meta, name = record.split(b'\t', 1)
            modes[name.decode('utf-8', errors='strict')] = meta.split()[0].decode()
    return modes


def irregular_memory_paths(repo, base, head_oid, paths, *, working=True):
    """The memory paths that are not a regular file (or absent) at the base, at the head,
    or (with `working`) in the working tree: the file types the publication set does not define."""
    irregular = set()
    for rev in (base, head_oid):
        irregular.update(name for name, mode in _tree_modes(repo, rev, paths).items()
                         if mode not in REGULAR_MODES)
    if working:
        for path in paths:
            target = Path(repo) / path
            if target.is_symlink() or (target.exists() and not target.is_file()):
                irregular.add(path)
    return sorted(irregular & set(paths))


def _blob_prefixes(repo, rev, paths):
    """{path: its first two bytes} for the blobs `paths` name in `rev`, in one git call."""
    if not paths:
        return {}
    request = b''.join(f'{rev}:{path}\n'.encode('utf-8') for path in paths)
    proc = subprocess.run(['git', '--no-optional-locks', '-C', str(repo), 'cat-file', '--batch'],
                          input=request, capture_output=True)
    if proc.returncode:
        raise ValueError('git cat-file failed: ' + proc.stderr.decode(errors='replace').strip())
    out, offset, prefixes = proc.stdout, 0, {}
    for path in paths:
        end = out.index(b'\n', offset)
        header = out[offset:end].split()
        if len(header) != 3 or header[1] != b'blob':
            raise ValueError('git cat-file returned no blob for ' + path)
        size = int(header[2])
        prefixes[path] = out[end + 1:end + 1 + min(size, 2)]
        offset = end + 1 + size + 1
    return prefixes


def ineligible_instruction_paths(repo, base, head_oid, paths, *, working=True):
    """The instruction paths that are not a non-executable regular file without a
    `#!` first line at the base, at the head, or (with `working`) on disk. Absence
    on any side is eligible. With `working=False` only the two commits are read."""
    paths = sorted(p for p in paths if _sane(p) and _instruction_class(p) == 'instruction')
    if not paths:
        return []
    ineligible = set()
    for rev in (base, head_oid):
        modes = _tree_modes(repo, rev, paths)
        ineligible.update(name for name, mode in modes.items() if mode not in INSTRUCTION_MODES)
        regular = sorted(name for name, mode in modes.items() if mode in INSTRUCTION_MODES)
        ineligible.update(name for name, prefix in _blob_prefixes(repo, rev, regular).items()
                          if prefix == b'#!')
    if working:
        for path in paths:
            target = Path(repo) / path
            if target.is_symlink():
                ineligible.add(path)
            elif target.exists():
                if not target.is_file() or target.stat().st_mode & 0o111:
                    ineligible.add(path)
                else:
                    with target.open('rb') as handle:
                        if handle.read(2) == b'#!':
                            ineligible.add(path)
    return sorted(ineligible & set(paths))


def _memory_only(paths, irregular):
    """A memory-only candidate: every path in the publication set, each a regular file."""
    return (irregular is not None and bool(paths) and not irregular
            and all(memory_publication_path(p) and _sane(p) for p in paths))


def _sane(path):
    p = PurePosixPath(path)
    return not (p.is_absolute() or '..' in p.parts or any(c.isspace() for c in path))


def policy_digest(repo):
    return 'sha256:' + hashlib.sha256(
        (Path(repo) / 'bin/_lib_merge_gate.py').read_bytes()).hexdigest()


def _document(path):
    return path in ('README.md', 'CHANGELOG.md') or path in EXECUTION_PLAN_PATHS or (
        path.startswith('docs/commands/') and path.endswith('.md'))


def _frontend_test(path):
    return path.startswith('dashboard/web/src/') and path.endswith(('.test.ts', '.test.tsx'))


def _instruction_class(path):
    """'instruction' for an instruction path, a full-selection reason for an
    instruction-file name at an unreviewed location, or None for any other path."""
    if path.endswith('.md') and path.startswith(INSTRUCTION_TREES):
        return 'instruction'
    if PurePosixPath(path).name in INSTRUCTION_NAMES:
        if path in INSTRUCTION_NAMES or path in NESTED_INSTRUCTION_FILES:
            return 'instruction'
        return 'unreviewed nested instruction file: ' + path
    if path in INSTRUCTION_INPUTS:
        return 'instruction'
    return None


def select(repo, paths, *, irregular=None, instruction_evidence=None):
    """The reviewed selection of `paths`.

    `irregular` is the list of memory paths that are not regular files
    (`irregular_memory_paths`); only a caller that computed it can have a
    candidate selected as memory-only. Any other candidate is selected over its
    whole diff, where a memory path is unclassified.

    `instruction_evidence` is the list of instruction paths that are not
    non-executable regular files (`ineligible_instruction_paths`); only a caller
    that computed it can have an instruction path selected focused.
    """
    repo = Path(repo)
    manifest = json.loads((repo / 'tests/authoritative-test-manifest.json').read_text())
    private = (repo / '.mirror-allowlist').exists()
    names = [r['name'] for r in manifest['harnesses']
             if private or r.get('visibility', 'public') == 'public']
    if not names or len(names) != len(set(names)):
        raise ValueError('invalid harness manifest')
    paths = sorted(set(paths))
    reasons, components = [], set()
    memory = _memory_only(paths, irregular)
    if memory:
        components.add('memory')
    for path in ([] if memory else paths):
        p = PurePosixPath(path)
        if path in (irregular or ()):
            reasons.append('memory path that is not a regular file: ' + path)
        elif p.is_absolute() or '..' in p.parts or any(c.isspace() for c in path):
            reasons.append('unclassified path: ' + path)
        elif '__pycache__' in p.parts:
            # Terminal: the skill comparison never reads cache content, so no
            # class may admit it (the memory-only shortcut is decided above).
            reasons.append('runtime cache path: ' + path)
        elif (instruction := _instruction_class(path)) is not None:
            if instruction != 'instruction':
                reasons.append(instruction)
            elif instruction_evidence is None:
                reasons.append('instruction path without file evidence: ' + path)
            elif path in instruction_evidence:
                reasons.append('instruction path that is not a non-executable regular file: ' + path)
            else:
                components.add('instructions')
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
    if 'instructions' in components and 'presentation' in components:
        reasons.append('instruction text and presentation are not certified together')
    harnesses = {'doc-lint'}
    files = set(SMOKE_FILES)
    if 'presentation' in components:
        harnesses.add('frontend')
        files.update(PRESENTATION_PYTEST)
    if 'memory' in components:
        files.update(MEMORY_PYTEST)
    if 'instructions' in components:
        files.update(INSTRUCTIONS_PYTEST)
    if not harnesses <= set(names):
        reasons.append('required merge harness absent from manifest')
    if any(not (repo / p).is_file() for p in files):
        reasons.append('required merge pytest file absent')
    full = bool(reasons)
    # Whether the wrapper must read background full-verification health before
    # trusting this focus. Only a memory-only selection derived with its
    # irregular-file evidence is exempt: memory publication is certified by the
    # memory focus whatever the background state. Containing memory is not
    # enough, and a full selection has nothing to read health for.
    if full:
        background_health = 'not-applicable'
    elif sorted(components) == ['memory'] and memory:
        background_health = 'exempt'
    else:
        background_health = 'required'
    return {
        'mode': 'full' if full else 'merge-focused',
        'pytest': 'full' if full else 'selected',
        'selectedHarnesses': [n for n in names if full or n in harnesses],
        'omittedHarnesses': [n for n in names if not full and n not in harnesses],
        'selectedPytestFiles': [] if full else sorted(files),
        'components': sorted(components), 'reasons': reasons,
        'policyDigest': policy_digest(repo), 'backgroundHealth': background_health,
        'maxWallSeconds': MAX_SECONDS,
    }


def plan(repo, base_rev, head='HEAD', *, working=True):
    b = basis(repo, base_rev, head, working=working)
    irregular = None
    if b['paths'] and all(memory_publication_path(p) for p in b['paths']):
        irregular = irregular_memory_paths(repo, b['baseOid'], b['headOid'], b['paths'], working=working)
    evidence = None
    if any(_sane(p) and _instruction_class(p) == 'instruction' for p in b['paths']):
        evidence = ineligible_instruction_paths(repo, b['baseOid'], b['headOid'], b['paths'],
                                                working=working)
    result = select(repo, b['paths'], irregular=irregular, instruction_evidence=evidence)
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


# Health reads one unfiltered ci.yml page and selects main on the client:
# GitHub's `branch=` index can lag the unfiltered listing by weeks (#892).
_HEALTH_LISTED_RUNS = 100  # one page at GitHub's maximum size; never a second
_HEALTH_MAIN_DEPTH = 20    # the retired branch-filtered read's depth: no older evidence


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


# D1(b): a scheduled test-macos job whose suite failed only on the runtime
# budget. ci.yml runs the marker step when the suite's outcome record says so;
# the reader also requires the suite step to be the job's only failed step.
_SUITE_STEP = 'Run cctally test suite'
_BUDGET_MARKER_STEP = 'Runtime budget is the only failure'
# D1(a): how many first-parent main commits are searched for a receipt.
_HEALTH_RECEIPT_COMMITS = 20
# D1(a) receipt discovery's overall time budget (#899 S2): once spent, the CI-derived
# result stands; it never waits out twenty commits of slow reads.
_HEALTH_RECEIPT_BUDGET_S = 60
_RECEIPT_RUN_ID = re.compile(r'^(\d{8}T\d{6}Z)-')


def _attempt_start(value):
    """The UTC instant of a GitHub ISO-8601 timestamp; ValueError when absent or malformed."""
    if not isinstance(value, str) or not value:
        raise ValueError('a run has no attempt start time')
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('a run attempt start time carries no timezone')
    return moment.astimezone(timezone.utc)


def _receipt_start(run_id):
    """The UTC start a receipt's run ID embeds, or None."""
    match = _RECEIPT_RUN_ID.match(run_id) if isinstance(run_id, str) else None
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _budget_only(row, job):
    """D1(b): a scheduled run's failed test-macos whose only failure is the runtime budget."""
    if row.get('event') != 'schedule' or job.get('status') != 'completed' or job.get('conclusion') != 'failure':
        return False
    steps = [step for step in (job.get('steps') or []) if isinstance(step, dict)]
    markers = [step for step in steps if step.get('name') == _BUDGET_MARKER_STEP]
    failed = {step.get('name') for step in steps if step.get('conclusion') == 'failure'}
    return (len(markers) == 1 and markers[0].get('conclusion') == 'success'
            and failed == {_SUITE_STEP})


def _run_item(row, jobs_of):
    """The full-verification item one main run contributes: 'pass', 'fail', 'unknown'
    (decisive, never a pass), or None when the run is skipped."""
    if row.get('status') != 'completed':
        # GitHub reuses the run ID on retry: the earlier failed attempt
        # disappears from this listing while the retry runs. Do not let that
        # mutation reveal an older successful run. A first attempt that is
        # still running hides nothing.
        return 'unknown' if row.get('run_attempt', 1) != 1 else None
    jobs = jobs_of(row)
    full = next((j for j in jobs if j.get('name') == 'test-macos'
                 and j.get('status') == 'completed'
                 and j.get('conclusion') != 'skipped'), None)
    if full is not None:
        if full.get('conclusion') == 'success' or _budget_only(row, full):
            return 'pass'
        return 'fail'
    if row.get('event') in ('schedule', 'workflow_dispatch'):
        # A scheduled/manual attempt must actually run the full job. Only
        # receipt-discharged ordinary pushes may be skipped while looking for
        # the last full verification.
        return 'unknown'
    # ci.yml can skip its declared full job on a successful private main push
    # only through a successful release/receipt gate. The skipped job plus
    # successful workflow is evidence of discharge; a merely successful
    # classifier job alone is not (it may emit discharged=false). Missing
    # jobs, cancellation, and failures must not silently inherit an older pass.
    discharged = (
        row.get('event') == 'push' and row.get('conclusion') == 'success'
        and any(j.get('name') == 'test-macos'
                and j.get('status') == 'completed'
                and j.get('conclusion') == 'skipped' for j in jobs)
        and any(j.get('name') in ('receipt-gate', 'release-stamp-gate')
                and j.get('status') == 'completed'
                and j.get('conclusion') == 'success' for j in jobs)
    )
    return None if discharged else 'unknown'


def _ci_item(rows, jobs_of):
    """(kind, start, row) of the newest decisive run, or None.

    The window is sorted by attempt start, newest first, BEFORE the walk, so a
    retry of an older-created run is read in its true place. Equal starts are
    decided together and a non-pass among them wins.
    """
    dated = [(_attempt_start(row.get('run_started_at')), index, row) for index, row in enumerate(rows)]
    dated.sort(key=lambda item: (-item[0].timestamp(), item[1]))
    index = 0
    while index < len(dated):
        start = dated[index][0]
        group = []
        while index < len(dated) and dated[index][0] == start:
            group.append(dated[index][2])
            index += 1
        items = [(kind, row) for row in group if (kind := _run_item(row, jobs_of)) is not None]
        if items:
            kind, row = next((item for item in items if item[0] != 'pass'), items[0])
            return kind, start, row
    return None


def _receipt_pass(gh, name, repo, newer_than, digest_of=None):
    """D1(a): (runId, commit sha) of the first valid passing full-suite receipt for
    the exact tree of a recent first-parent main commit, newer than `newer_than`
    (any age when None); None when there is none. Raises on any read error.

    `digest_of(sha)`, when given, reconstructs a commit's tree-manifest digest in
    its caller's stead (bounded by the caller's budget); by default it is built
    in-process."""
    # The validator of this reader's own tree, the one receipt-gate runs.
    spec = importlib.util.spec_from_file_location(
        '_merge_gate_receipts',
        Path(__file__).resolve().parents[1] / '.github' / 'scripts' / 'classify_receipt.py')
    receipts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(receipts)
    toolchain = receipts.toolchain_digest(str(repo))
    if toolchain is None:
        return None
    listing = gh('api', f'repos/{name}/commits?sha=main&per_page=100')
    by_sha = {commit['sha']: commit for commit in listing}
    commit = listing[0] if listing else None
    for _ in range(_HEALTH_RECEIPT_COMMITS):
        if commit is None:
            break
        sha, tree = commit['sha'], commit['commit']['tree']['sha']
        digest = None
        refs = gh('api', f'repos/{name}/git/matching-refs/cctally/receipts/{tree}/')
        for ref in sorted(refs, key=lambda r: r['ref'], reverse=True):
            run_id = ref['ref'].rsplit('/', 1)[-1]
            when = _receipt_start(run_id)
            if when is None or (newer_than is not None and when <= newer_than):
                continue
            blob = gh('api', f'repos/{name}/git/blobs/{ref["object"]["sha"]}')
            receipt = json.loads(base64.b64decode(blob['content']))
            if not isinstance(receipt, dict) or receipt.get('runId') != run_id:
                continue  # dated by the run it records, and filed under that run
            if digest is None:
                digest = (digest_of(sha) if digest_of is not None
                          else receipts.manifest_digest(receipts.reconstruct_manifest(sha, str(repo))))
            if receipts.verify_candidate(receipt, tree_oid=tree, ref_namespace=tree,
                                         reconstructed_digest=digest,
                                         toolchain_digest=toolchain) is None:
                return run_id, sha
        parents = commit.get('parents') or []
        commit = by_sha.get(parents[0]['sha']) if parents else None
    return None


def read_background_health(repo):
    """The state of the newest completed full-verification item.

    Items are actual full test-macos job results, never a receipt-discharge
    skip (a successful workflow with a skipped full job is not full
    verification), with two additions (operator decision D1 of #899):

    (b) a scheduled run whose suite failed only on the runtime budget, proven by
        its marker step and its final step conclusions, is a pass;
    (a) a valid passing full-suite receipt for the exact tree of a recent
        first-parent main commit, newer than the newest CI item, is a pass.

    Runs are ordered by attempt start. A missing start, an unreadable listing
    and a malformed row are missing evidence; a failed receipt read leaves the
    CI-derived result unchanged and never manufactures a pass, and so does a
    receipt discovery that outlives its overall budget (_HEALTH_RECEIPT_BUDGET_S).
    That budget bounds the whole discovery, the reconstruction of a receipt's
    tree manifest included: it runs as a subprocess stopped when the budget is
    spent, after which nothing further is discovered (#899 S2).
    """
    def gh(*args):
        process = subprocess.run(['gh', *args], cwd=repo, capture_output=True,
                                 text=True, timeout=20)
        if process.returncode:
            raise ValueError('GitHub full-verification evidence is unavailable')
        return json.loads(process.stdout)

    def jobs_of(row):
        return gh('api', f'repos/{name}/actions/runs/{row["id"]}/jobs?per_page=100')['jobs']
    try:
        name = gh('repo', 'view', '--json', 'nameWithOwner')['nameWithOwner']
        listed = gh('api', f'repos/{name}/actions/workflows/ci.yml/runs'
                           f'?per_page={_HEALTH_LISTED_RUNS}')['workflow_runs']
        # Other branches are never main's evidence, so they are dropped before
        # any rule below can decide on them or count toward the depth.
        rows = [row for row in listed if row.get('head_branch') == 'main']
        item = _ci_item(rows[:_HEALTH_MAIN_DEPTH], jobs_of)
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            subprocess.TimeoutExpired):
        # A malformed listing (a null or non-object row) is missing evidence.
        return background_health([])
    if item is not None and item[0] == 'pass':
        return background_health([dict(item[2], fullSuite=True, conclusion='success')])
    if item is not None and item[0] == 'fail':
        result = background_health([dict(item[2], fullSuite=True, conclusion='failure')])
    else:
        result = background_health([])
    budget_end = time.monotonic() + _HEALTH_RECEIPT_BUDGET_S

    def budgeted_gh(*args):
        # Every discovery read gets only what is left of the overall budget, and a read
        # that ends past it voids the discovery: the CI-derived result stands.
        left = budget_end - time.monotonic()
        if left <= 0:
            raise ValueError('the receipt discovery budget is spent')
        process = subprocess.run(['gh', *args], cwd=repo, capture_output=True, text=True,
                                 timeout=min(20.0, left))
        if process.returncode:
            raise ValueError('GitHub full-verification evidence is unavailable')
        if time.monotonic() > budget_end:
            raise ValueError('the receipt discovery budget is spent')
        return json.loads(process.stdout)

    def budgeted_digest(sha):
        # The tree-manifest reconstruction runs as the classifier's own `--self-test-manifest`
        # in a separate session, stopped (with every process it started) when the budget is spent.
        left = budget_end - time.monotonic()
        if left <= 0:
            raise ValueError('the receipt discovery budget is spent')
        script = Path(__file__).resolve().parents[1] / '.github' / 'scripts' / 'classify_receipt.py'
        process = subprocess.Popen([sys.executable, str(script), '--sha', sha, '--repo', str(repo),
                                    '--self-test-manifest'], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            out, _ = process.communicate(timeout=left)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise ValueError('the receipt discovery budget is spent') from None
        if process.returncode:
            raise ValueError('the tree manifest could not be reconstructed')
        if time.monotonic() > budget_end:
            raise ValueError('the receipt discovery budget is spent')
        return out.strip()
    try:
        found = _receipt_pass(budgeted_gh, name, repo, item[1] if item is not None else None,
                              digest_of=budgeted_digest)
        if found is not None and time.monotonic() > budget_end:
            found = None  # validated after the budget: the CI-derived result stands
    except Exception:  # any read error, or a spent budget, leaves the CI-derived result unchanged
        found = None
    if found is None:
        return result
    run_id, sha = found
    return {'state': 'passed', 'allowFocused': True, 'runId': run_id, 'headSha': sha}


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
            print(f"{result['mode']} {result['backgroundHealth']}")
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

"""#968 U4: file batches publish without inline historical deletion."""
import sys
from unittest.mock import Mock

from test_codex_prune_drain import estate, _no_detached_drains, _cache_mod
from _codex_zstd_fixtures import token_records, write_rollout


def test_budgeted_sync_file_batch_never_enters_deletion_executor(estate, monkeypatch):
    ns, paths, spawned, clock = estate
    cache = _cache_mod()
    keeper = paths[-1]
    write_rollout(keeper, token_records(9, session_id='growing-keeper'))
    executors = {}
    for name in ('_prune_inactive_codex_source_roots',
                 '_delete_codex_file_derived_rows', 'run_codex_prune_drain',
                 'run_codex_conversation_prune_drain'):
        spy = Mock(wraps=getattr(cache, name))
        monkeypatch.setattr(cache, name, spy)
        executors[name] = spy
    batch = Mock(wraps=cache._write_codex_file_batch)
    monkeypatch.setattr(cache, '_write_codex_file_batch', batch)
    conn = ns['open_cache_db']()
    try:
        before = list(conn.execute('SELECT * FROM codex_session_entries WHERE source_path!=? ORDER BY source_path,line_offset', (str(keeper),)))
        stats = ns['sync_codex_cache'](conn, budget_seconds=30, maintenance='caller')
        assert batch.call_count > 0, 'reproduction must reach real file-batch finalization'
        assert {n: x.call_count for n,x in executors.items()} == {n: 0 for n in executors}, 'inline deletion executor reached by budgeted file batch'
        assert list(conn.execute('SELECT * FROM codex_session_entries WHERE source_path!=? ORDER BY source_path,line_offset', (str(keeper),))) == before
        assert stats.prune_debt_pending > 0
        assert conn.execute('SELECT COUNT(*) FROM codex_prune_debt').fetchone()[0] == 3
    finally:
        conn.close()
    # The normal detached executor still owns the complete deletion transaction.
    for name, spy in executors.items():
        monkeypatch.setattr(cache, name, spy._mock_wraps)
    result = cache.run_codex_prune_drain(wait_for_requests=False)
    assert result.remaining == 0 and result.identities_deleted == 3
    conn = ns['open_cache_db']()
    try:
        assert conn.execute('SELECT COUNT(*) FROM codex_session_entries WHERE source_path!=?', (str(keeper),)).fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM codex_session_entries WHERE source_path=?', (str(keeper),)).fetchone()[0] > 0
    finally:
        conn.close()

# The observational helper bytes are embedded so the normal estate is hermetic.
COLLECTOR_HELPERS_SOURCE = '"""Owned U4 collector: namespace, doctor outcomes and complete immutable reads."""\nimport json\nimport sqlite3\nfrom pathlib import Path\n\ndef ro(path, *, allowed_roots):\n    path = Path(path)\n    assert any(path.is_relative_to(root) for root in allowed_roots), str(path)\n    proof = {s: (Path(str(path)+s).stat().st_size if Path(str(path)+s).exists() else None)\n             for s in (\'-wal\', \'-journal\')}\n    assert all(v in (None, 0) for v in proof.values()), (\'immutable incomplete\', proof)\n    conn = sqlite3.connect(path.as_uri()+\'?mode=ro&immutable=1\', uri=True)\n    conn.execute(\'PRAGMA query_only=ON\')\n    conn.execute(\'PRAGMA temp_store=MEMORY\')\n    conn.row_factory = sqlite3.Row\n    return conn\n\ndef validated_doctor(text):\n    obj = json.loads(text)\n    stack, found = [obj], []\n    while stack:\n        node = stack.pop()\n        if isinstance(node, dict):\n            if node.get(\'id\') == \'data.codex_compressed_rollouts\': found.append(node)\n            stack.extend(node.values())\n        elif isinstance(node, list): stack.extend(node)\n    if len(found) != 1: raise ValueError(\'doctor named check must occur exactly once\')\n    doc = found[0]; d = doc.get(\'details\')\n    counts = (\'compressedOnly\',\'neverIngested\',\'retainedComplete\',\'retainedIncomplete\',\n              \'unverified\',\'verificationDebt\',\'readDebt\',\'pruneDebt\')\n    if (not isinstance(d, dict) or any(type(d.get(k)) is not int or d[k] < 0 for k in counts)\n        or type(d.get(\'decoderAvailable\')) is not bool\n        or doc.get(\'title\') != \'Compressed Codex rollouts\'):\n        raise ValueError(\'doctor named check fields missing or invalid\')\n    u = d[\'unverified\']; pending = d[\'verificationDebt\']+d[\'readDebt\']+d[\'pruneDebt\']\n    expected = (f"{u} compressed Codex rollouts kept their cached history but couldn\'t be verified against it."\n                if u else \'All compressed Codex rollouts are readable and verified.\')\n    if not (d[\'decoderAvailable\'] and pending == 0 and doc.get(\'severity\') == (\'warn\' if u else \'ok\')\n            and doc.get(\'summary\') == expected): raise ValueError(\'doctor-D2-D3-D6\')\n    return doc\n\ndef validate_family(family, top_levels, doctor_text=None):\n    if family.get(\'valid\') is not True: raise ValueError(\'invalid namespace family\')\n    named = None\n    for row in top_levels:\n        code = row.get(\'code\')\n        if row.get(\'exited\') is not True or row.get(\'signaled\') is not False:\n            raise ValueError(\'unexpected process termination\')\n        if row.get(\'step\') == \'doctor\':\n            if code not in (0,2) or type(code) is not int: raise ValueError(\'unexpected doctor exit\')\n            named = validated_doctor(doctor_text)\n        elif type(code) is not int or code != 0:\n            raise ValueError(\'unexpected non-doctor exit\')\n    return {\'namespaceValid\': True, \'compressedDoctor\': named,\n            \'doctorDocument\': json.loads(doctor_text) if doctor_text is not None else None,\n            \'topLevels\': top_levels}\n'
PHASE_PROBE_SOURCE = '"""Bounded observational elapsed spans. Each thread owns its nesting stack.\n\nInclusive includes same-thread children; exclusive subtracts their union (nested\nchildren already include their descendants). Different threads overlap and must\nnever be summed as wall time. No profiler or execution/deadline modification.\n"""\nimport functools\nimport threading\nimport time\n\nclass PhaseTimer:\n    def __init__(self, clock=time.monotonic, limit=512):\n        self.clock, self.limit = clock, limit\n        self.local = threading.local()\n        self.lock = threading.Lock()\n        self.rows = []\n        self.dropped = 0\n        self.serial = 0\n\n    def wrap(self, name, fn):\n        @functools.wraps(fn)\n        def measured(*args, **kwargs):\n            stack = getattr(self.local, \'stack\', None)\n            if stack is None: self.local.stack = stack = []\n            with self.lock:\n                self.serial += 1\n                ident = self.serial\n            row = {\'id\': ident, \'name\': name, \'thread\': threading.get_ident(),\n                   \'parent\': stack[-1][\'id\'] if stack else None,\n                   \'start\': self.clock(), \'children\': 0.0, \'exception\': False}\n            stack.append(row)\n            try:\n                return fn(*args, **kwargs)\n            except BaseException:\n                row[\'exception\'] = True\n                raise\n            finally:\n                end = self.clock()\n                assert stack.pop() is row, \'phase nesting corrupted\'\n                row[\'end\'] = end\n                row[\'inclusive\'] = end-row[\'start\']\n                row[\'exclusive\'] = row[\'inclusive\']-row.pop(\'children\')\n                assert row[\'exclusive\'] >= -1e-9 and end >= row[\'start\'], \'invalid phase clock\'\n                if stack: stack[-1][\'children\'] += row[\'inclusive\']\n                with self.lock:\n                    if len(self.rows) < self.limit: self.rows.append(row)\n                    else: self.dropped += 1\n        return measured\n\n    def snapshot(self):\n        with self.lock:\n            return {\'spans\': sorted(self.rows, key=lambda r:r[\'id\']),\n                    \'droppedSpans\':self.dropped, \'limit\':self.limit,\n                    \'semantics\':\'inclusive/exclusive within each thread; cross-thread spans overlap\'}\n\ndef validate_spans(snapshot):\n    rows = snapshot[\'spans\']; byid = {r[\'id\']:r for r in rows}\n    if len(byid) != len(rows) or snapshot[\'droppedSpans\']: raise ValueError(\'incomplete spans\')\n    for r in rows:\n        if not (r[\'end\'] >= r[\'start\'] and r[\'exclusive\'] >= -1e-9\n                and abs(r[\'inclusive\']-(r[\'end\']-r[\'start\'])) < 1e-8):\n            raise ValueError(\'bad duration\')\n        p = byid.get(r[\'parent\']) if r[\'parent\'] is not None else None\n        if r[\'parent\'] is not None and (p is None or p[\'thread\'] != r[\'thread\']\n                or p[\'start\'] > r[\'start\'] or p[\'end\'] < r[\'end\']):\n            raise ValueError(\'bad parent/thread nesting\')\n        children = sorted((x for x in rows if x[\'parent\']==r[\'id\']), key=lambda x:x[\'start\'])\n        if any(a[\'end\'] > b[\'start\'] for a,b in zip(children, children[1:])):\n            raise ValueError(\'overlapping same-thread children\')\n        if abs(r[\'exclusive\']-(r[\'inclusive\']-sum(x[\'inclusive\'] for x in children))) > 1e-8:\n            raise ValueError(\'bad exclusive accounting\')\n    return True\n'

import copy
import json
import threading
import types
import pytest
from pathlib import Path

ROOT_POLICY = (Path('/owned-output'), Path('/retained-pristine'), Path('/new-copy'))
FORBIDDEN_ROOTS = ('/forbidden/live', '/forbidden/original-baseline', '/forbidden/source', '/forbidden/baseline-copy', '/forbidden/control')


def helper(source, name):
    module = types.ModuleType(name)
    exec(compile(source, name+'.py', 'exec'), module.__dict__)
    return module


@pytest.fixture
def collector():
    module = helper(COLLECTOR_HELPERS_SOURCE, 'owned_u4_collector')
    class OwnedPath(type(Path())):
        pass
    module.Path = OwnedPath
    module.OWNED_ROOT, module.PRISTINE, module.STORE = ROOT_POLICY
    return module


def doctor(unverified=1):
    return {'checks':[{'id':'unrelated.global', 'severity':'fail', 'summary':'retained FAIL'},
        {'id':'data.codex_compressed_rollouts','title':'Compressed Codex rollouts',
         'severity':'warn' if unverified else 'ok',
         'summary':(f"{unverified} compressed Codex rollouts kept their cached history but couldn't be verified against it."
                    if unverified else 'All compressed Codex rollouts are readable and verified.'),
         'details':dict(compressedOnly=3, neverIngested=0,retainedComplete=2,
                        retainedIncomplete=0,unverified=unverified,verificationDebt=0,
                        readDebt=0,pruneDebt=0,decoderAvailable=True)}]}


def test_supported_doctor_exit_two_preserves_full_document(collector):
    doc = doctor()
    rows = [dict(step='hook',exited=True,signaled=False,code=0),
            dict(step='doctor',exited=True,signaled=False,code=2)]
    out = collector.validate_family({'valid':True}, rows, json.dumps(doc))
    assert out['doctorDocument'] == doc
    assert out['doctorDocument']['checks'][0]['severity'] == 'fail'
    assert out['compressedDoctor'] == doc['checks'][1]
    assert collector.validated_doctor(json.dumps(doctor(0)))['severity'] == 'ok'


@pytest.mark.parametrize('kind', ['namespace','non-doctor','doctor-exit','malformed','missing','duplicate','failed','pending','missing-field','bool-count','decoder','wording','signaled'])
def test_collector_rejects_invalid_or_unexpected_outcome(collector,kind):
    doc = doctor(); family={'valid':True}
    rows=[dict(step='doctor',exited=True,signaled=False,code=2)]
    if kind=='namespace': family['valid']=False
    elif kind=='non-doctor': rows[0]['step']='cache-sync'
    elif kind=='doctor-exit': rows[0]['code']=3
    elif kind=='missing': doc['checks'].pop()
    elif kind=='duplicate': doc['checks'].append(copy.deepcopy(doc['checks'][1]))
    elif kind=='failed': doc['checks'][1]['severity']='fail'
    elif kind=='pending': doc['checks'][1]['details']['readDebt']=1
    elif kind=='missing-field': del doc['checks'][1]['details']['pruneDebt']
    elif kind=='bool-count': doc['checks'][1]['details']['readDebt']=False
    elif kind=='decoder': doc['checks'][1]['details']['decoderAvailable']=False
    elif kind=='wording': doc['checks'][1]['summary']='almost right'
    elif kind=='signaled': rows[0]['signaled']=True
    text='{' if kind=='malformed' else json.dumps(doc)
    with pytest.raises((ValueError,TypeError)):
        collector.validate_family(family, rows, text)


@pytest.mark.parametrize('nonempty', ['-wal','-journal'])
@pytest.mark.parametrize('other', [None,0])
def test_immutable_guard_isolates_each_sidecar(collector,monkeypatch,nonempty,other):
    p=collector.STORE/'cache.db'
    sizes={nonempty:1, ('-journal' if nonempty=='-wal' else '-wal'):other}
    def suffix(p): return '-journal' if str(p).endswith('-journal') else '-wal'
    monkeypatch.setattr(collector.Path,'exists',lambda p:sizes[suffix(p)] is not None)
    monkeypatch.setattr(collector.Path,'stat',lambda p:types.SimpleNamespace(st_size=sizes[suffix(p)]))
    connect=Mock(); monkeypatch.setattr(collector.sqlite3,'connect',connect)
    with pytest.raises(AssertionError): collector.ro(p, allowed_roots=ROOT_POLICY)
    connect.assert_not_called()


@pytest.mark.parametrize('wal,journal', [(None,None),(0,None),(None,0),(0,0)])
def test_immutable_guard_allows_only_complete_owned_store(collector,monkeypatch,wal,journal):
    sizes={'-wal':wal,'-journal':journal}
    def suffix(p): return '-journal' if str(p).endswith('-journal') else '-wal'
    monkeypatch.setattr(collector.Path,'exists',lambda p:sizes[suffix(p)] is not None)
    monkeypatch.setattr(collector.Path,'stat',lambda p:types.SimpleNamespace(st_size=sizes[suffix(p)]))
    connect=Mock(); monkeypatch.setattr(collector.sqlite3,'connect',connect)
    for root in (collector.OWNED_ROOT,collector.PRISTINE,collector.STORE):
        p=root/'cache.db'; c=collector.ro(p, allowed_roots=ROOT_POLICY)
        assert connect.call_args.args==(p.as_uri()+'?mode=ro&immutable=1',)
        assert connect.call_args.kwargs=={'uri':True}
        assert c.execute.call_args_list[-2].args==('PRAGMA query_only=ON',)
    for root in FORBIDDEN_ROOTS:
        connect.reset_mock()
        with pytest.raises(AssertionError): collector.ro(Path(root)/'cache.db', allowed_roots=ROOT_POLICY)
        connect.assert_not_called()


def test_phase_nested_exception_and_thread_accounting():
    ph=helper(PHASE_PROBE_SOURCE,'owned_u4_phase')
    clock=iter([0.,1.,3.,5.]).__next__; timer=ph.PhaseTimer(clock)
    def fail(): raise RuntimeError('observed')
    child=timer.wrap('child',fail)
    def parent():
        with pytest.raises(RuntimeError): child()
    timer.wrap('parent',parent)()
    snap=timer.snapshot(); assert ph.validate_spans(snap)
    assert [(r['name'],r['inclusive'],r['exclusive'],r['exception']) for r in snap['spans']]==[('parent',5.,3.,False),('child',2.,2.,True)]
    gate=threading.Barrier(2); threaded=ph.PhaseTimer()
    fn=threaded.wrap('concurrent',lambda:gate.wait(timeout=10))
    t=threading.Thread(target=fn); t.start(); fn(); t.join(timeout=10)
    assert not t.is_alive(); snap=threaded.snapshot()
    assert len({r['thread'] for r in snap['spans']})==2
    assert all(r['parent'] is None for r in snap['spans'])
    assert ph.validate_spans(snap)


@pytest.mark.parametrize('kind',['parent','thread','exclusive','duration','duplicate','overflow'])
def test_phase_validator_rejects_broken_accounting(kind):
    ph=helper(PHASE_PROBE_SOURCE,'owned_u4_phase')
    a=dict(id=1,name='outer',thread=1,parent=None,start=0.,end=5.,inclusive=5.,exclusive=3.,exception=False)
    b=dict(id=2,name='inner',thread=1,parent=1,start=1.,end=3.,inclusive=2.,exclusive=2.,exception=True)
    snap=dict(spans=[a,b],droppedSpans=0)
    assert ph.validate_spans(snap)
    if kind=='parent': b['start']=-1
    elif kind=='thread': b['thread']=2
    elif kind=='exclusive': a['exclusive']=5
    elif kind=='duration': b['inclusive']=4
    elif kind=='duplicate': b['id']=1
    elif kind=='overflow': snap['droppedSpans']=1
    with pytest.raises(ValueError): ph.validate_spans(snap)

HOOK_PROBE_SOURCE = '"""One real due hook, bounded independent phase timers; diagnostic only."""\nimport atexit, importlib.machinery, importlib.util, json, os, sys, time\nfrom phase_probe import PhaseTimer, validate_spans\nTREE, RECEIPT = sys.argv[1:3]\nBIN=os.path.join(TREE,\'bin\'); SCRIPT=os.path.join(BIN,\'cctally\')\nREC={\'pid\':os.getpid(),\'syncCalls\':[],\'deletionCalls\':{},\'exit\':None,\'diagnosticOnly\':True}\ntimer=PhaseTimer()\ndef write():\n    snap=timer.snapshot()\n    try: REC[\'phaseValidation\']=validate_spans(snap)\n    except Exception as exc: REC[\'phaseValidation\']=str(exc)\n    REC[\'phases\']=snap\n    with open(RECEIPT,\'w\') as f: json.dump(REC,f,indent=2)\natexit.register(write)\nsys.argv=[SCRIPT,\'hook-tick\',\'--foreground\',\'--source\',\'codex\']\nsys.path.insert(0,BIN)\nloader=importlib.machinery.SourceFileLoader(\'cctally\',SCRIPT)\nspec=importlib.util.spec_from_loader(\'cctally\',loader); mod=importlib.util.module_from_spec(spec)\nsys.modules[\'cctally\']=mod;loader.exec_module(mod)\ncache=mod._load_sibling(\'_cctally_cache\')\nreal_sync=mod.sync_codex_cache\ndef timed_sync(*args,**kwargs):\n    started=time.monotonic()\n    try:return real_sync(*args,**kwargs)\n    finally:REC[\'syncCalls\'].append({\'seconds\':time.monotonic()-started,\'budgetSeconds\':kwargs.get(\'budget_seconds\')})\nmod.sync_codex_cache=timer.wrap(\'hook.sync_codex_cache\',timed_sync)\nfor name in (\'run_codex_prune_drain\',\'run_codex_conversation_prune_drain\',\n             \'_prune_inactive_codex_source_roots\',\'_delete_codex_file_derived_rows\'):\n    real=getattr(cache,name)\n    REC[\'deletionCalls\'][name]=0\n    def counter(*args,_name=name,_real=real,**kwargs):\n        REC[\'deletionCalls\'][_name]+=1\n        return _real(*args,**kwargs)\n    setattr(cache,name,timer.wrap(name,counter))\nfor name in (\'_sync_codex_cache_locked\',\'_discover_codex_files_with_roots\',\n             \'_plan_ordinary_codex_prune\',\'_write_codex_file_batch\',\n             \'_record_codex_container_status\',\'_codex_barrier_after_walk\',\n             \'_codex_backlog_after\',\'_mark_codex_containers_pending\',\n             \'_codex_barrier_apply_holds\',\'_order_codex_walk\',\n             \'apply_codex_window_spend_adoption\',\'reconcile_codex_window_attribution_spend\',\n             \'_prune_codex_accounting_change_log\',\'_maybe_truncate_wal\',\n             \'_request_codex_drain_worker\'):\n    setattr(cache,name,timer.wrap(\'cache.\'+name,getattr(cache,name)))\nfor module,name in ((cache._cctally_db_sib,\'backfill_codex_quota_observed_model\'),\n                    (mod._load_sibling(\'_cctally_journal\'),\'rehydrate_codex_journal_families\')):\n    setattr(module,name,timer.wrap(module.__name__+\'.\'+name,getattr(module,name)))\ntry:code=timer.wrap(\'hook.main\',mod.main)()\nexcept SystemExit as exc:code=exc.code\nREC[\'exit\']=code if isinstance(code,int) else (0 if code is None else 1)\nraise SystemExit(REC[\'exit\'])\n'


def test_hook_probe_runs_real_callback_without_changing_arguments(tmp_path):
    import subprocess
    script=tmp_path/'hook_probe.py'; script.write_text(HOOK_PROBE_SOURCE)
    (tmp_path/'phase_probe.py').write_text(PHASE_PROBE_SOURCE)
    tree=tmp_path/'tree'; (tree/'bin').mkdir(parents=True)
    (tree/'bin/cctally').write_text("""
import types, sys
cache=types.SimpleNamespace()
cache._cctally_db_sib=types.SimpleNamespace(__name__='fake_db',backfill_codex_quota_observed_model=lambda *a,**k:None)
journal=types.SimpleNamespace(__name__='fake_journal',rehydrate_codex_journal_families=lambda *a,**k:None)
for name in ('run_codex_prune_drain','run_codex_conversation_prune_drain','_prune_inactive_codex_source_roots','_delete_codex_file_derived_rows','_sync_codex_cache_locked','_discover_codex_files_with_roots','_plan_ordinary_codex_prune','_write_codex_file_batch','_record_codex_container_status','_codex_barrier_after_walk','_codex_backlog_after','_mark_codex_containers_pending','_codex_barrier_apply_holds','_order_codex_walk','apply_codex_window_spend_adoption','reconcile_codex_window_attribution_spend','_prune_codex_accounting_change_log','_maybe_truncate_wal','_request_codex_drain_worker'):
    setattr(cache,name,lambda *a,**k:None)
def _load_sibling(name): return cache if name=='_cctally_cache' else journal
def sync_codex_cache(*args,**kwargs):
    assert args==('connection',) and kwargs==dict(budget_seconds=5.0,lock_timeout=0,quota_reconcile='defer')
    cache._write_codex_file_batch()
    return 17
def main():
    assert sys.argv[1:]==['hook-tick','--foreground','--source','codex']
    assert sync_codex_cache('connection',budget_seconds=5.0,lock_timeout=0,quota_reconcile='defer')==17
    return 0
""")
    receipt=tmp_path/'receipt.json'
    q=subprocess.run([sys.executable,str(script),str(tree),str(receipt)],capture_output=True)
    assert q.returncode==0 and not q.stdout and not q.stderr, q.stderr
    out=json.loads(receipt.read_text())
    assert out['exit']==0 and out['phaseValidation'] is True
    assert out['syncCalls'][0]['budgetSeconds']==5.0
    assert all(v==0 for v in out['deletionCalls'].values())
    assert {r['name'] for r in out['phases']['spans']}=={'hook.main','hook.sync_codex_cache','cache._write_codex_file_batch'}

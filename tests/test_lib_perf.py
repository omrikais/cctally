import importlib.util
import pathlib


def _load_perf():
    p = pathlib.Path(__file__).resolve().parents[1] / "bin" / "_lib_perf.py"
    spec = importlib.util.spec_from_file_location("_lib_perf_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_disabled_returns_null_singleton_and_builds_no_tree():
    perf = _load_perf()
    perf.set_enabled(False)
    perf.reset_thread()
    a = perf.phase("x")
    b = perf.phase("y")
    assert a is b is perf._NULL_PHASE          # no allocation per call
    with perf.phase("root"):
        with perf.phase("child") as c:
            c.set_count(3)
    assert perf.current_root() is None          # nothing collected when off


def test_enabled_builds_nested_tree_with_counts_and_meta():
    perf = _load_perf()
    perf.set_enabled(True)
    perf.reset_thread()
    with perf.phase("root"):
        with perf.phase("child") as c:
            c.set_count(3)
            c.set_meta(files=10)
    root = perf.current_root()
    d = root.to_dict()
    assert d["name"] == "root"
    assert d["children"][0]["name"] == "child"
    assert d["children"][0]["count"] == 3
    assert d["children"][0]["meta"] == {"files": 10}
    assert isinstance(d["elapsed_ms"], float)


def test_thread_locality_no_cross_linking():
    import threading
    perf = _load_perf()
    perf.set_enabled(True)
    seen = {}

    def worker(tag):
        perf.reset_thread()
        with perf.phase(f"root-{tag}"):
            with perf.phase(f"child-{tag}"):
                pass
        seen[tag] = perf.current_root().to_dict()

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert seen["a"]["children"][0]["name"] == "child-a"
    assert seen["b"]["children"][0]["name"] == "child-b"


def test_flush_stderr_writes_tree_and_noop_on_none(capsys):
    perf = _load_perf()
    perf.set_enabled(True)
    perf.reset_thread()
    with perf.phase("root"):
        with perf.phase("child") as c:
            c.set_count(2)
    perf.flush_stderr(perf.current_root())
    err = capsys.readouterr().err
    assert "root" in err and "child" in err and "count=2" in err
    perf.flush_stderr(None)                     # no crash, no output
    assert capsys.readouterr().err == ""


def test_stash_and_read_last():
    perf = _load_perf()
    perf.set_enabled(True)
    perf.reset_thread()
    with perf.phase("root"):
        pass
    perf.stash_last(perf.current_root(), generation=7, generated_at="2026-07-08T00:00:00Z")
    last = perf.last_backend_perf()
    assert last["generation"] == 7
    assert last["phases"]["name"] == "root"


def test_leaked_inner_phase_does_not_corrupt_tree():
    # Regression for the SDD review P1: the sync_cache/snapshot seams open
    # phases via the raw CM protocol (_p.__enter__()/__exit__()) WITHOUT a
    # try/finally, so an exception escaping a bracketed region skips the inner
    # __exit__ and leaks its frame. The outer __exit__ must then NOT append a
    # phase to its own children (self-reference -> to_dict() RecursionError)
    # nor strand the stack so the real root is lost.
    perf = _load_perf()
    perf.set_enabled(True)
    perf.reset_thread()
    outer = perf.phase("outer")
    outer.__enter__()
    inner = perf.phase("inner")
    inner.__enter__()
    # inner LEAKS: its __exit__ is never called (simulated escaped exception).
    outer.__exit__(None, None, None)
    root = perf.current_root()
    assert root is not None            # old code left the stack non-empty -> None
    assert root.name == "outer"
    d = root.to_dict()                 # old code: self-referential -> RecursionError
    assert "outer" not in [c["name"] for c in d.get("children", [])]

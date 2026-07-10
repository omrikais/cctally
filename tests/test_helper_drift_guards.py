"""Helper drift guards (#279 S6 W4).

The pure ``_lib_*`` layers cannot back-import ``cctally`` for ubiquitous
helpers, so each layer keeps its OWN copy of ``_eprint`` / ``_load_lib`` /
``_ensure_sibling_loaded`` (a documented, deliberate split §5.3 / bootstrap
pattern — the duplication IS the bootstrap; full deduplication would need a
loader redesign). These guards pin every copy body-identical so a future edit
can't silently let one copy drift. AST-based, docstring-stripped body identity
across every definition under ``bin/`` (including the extensionless
``bin/cctally``).
"""
import ast
import pathlib

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"


def _py_sources():
    for p in sorted(BIN.glob("_*.py")):
        yield p
    yield BIN / "cctally"


def _defs_named(name):
    found = {}
    for path in _py_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                body = list(node.body)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    body = body[1:]  # strip docstring
                found[str(path.name)] = ast.dump(
                    ast.Module(body=body, type_ignores=[]))
    return found


def _assert_identical(name, min_count):
    found = _defs_named(name)
    assert len(found) >= min_count, \
        f"{name}: expected >= {min_count} defs, found {sorted(found)}"
    bodies = set(found.values())
    assert len(bodies) == 1, f"{name} drifted across: {sorted(found)}"


def test_eprint_bodies_identical():
    # 6 copies in the private tree; the 6th (bin/_cctally_preview.py) is a
    # maintainer-only sibling that classifies `unmatched` and is absent from
    # the public mirror. The floor is therefore the 5 public copies (the
    # set-identity check below still guards preview's copy wherever it exists);
    # a min_count of 6 would false-fail the public repo's pytest run.
    _assert_identical("_eprint", 5)


def test_eprint_matches_canonical_eprint():
    under = _defs_named("_eprint")
    canon = _defs_named("eprint")
    assert "_cctally_core.py" in canon
    assert set(under.values()) == {canon["_cctally_core.py"]}


def test_load_lib_bodies_identical():
    _assert_identical("_load_lib", 11)


def test_ensure_sibling_loaded_bodies_identical():
    _assert_identical("_ensure_sibling_loaded", 5)

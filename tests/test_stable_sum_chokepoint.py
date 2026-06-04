"""Repo-wide guard: output-bound float sums stay on the ``stable_sum`` chokepoint.

The float-stability fix (spec ``2026-06-03-python-floor-money-stability``) routed
every output-bound float ``sum()`` — USD totals, percent/ratio aggregates,
averages, ranking keys — through ``_lib_fmt.stable_sum`` (``math.fsum``), which is
exactly-rounded and byte-identical across CPython 3.11/3.12/3.13 (the built-in
``sum()`` switched to Neumaier compensated summation for floats in 3.12). The
spec calls ``stable_sum`` "the greppable chokepoint that makes 'no output-bound
float ``sum()`` remains' an enforceable invariant" — but until this guard the
invariant was tribal knowledge enforced by review, with the cross-version CI
matrix deferred to #132 (so no standing check catches a regression).

``tests/test_dashboard_ranking_stability.py`` source-pins exactly ONE site (the
dashboard project-ranking key). This generalizes that mechanism to the whole
money/percent surface: it walks the AST of every Python file under ``bin/`` and
fails if a *bare* ``sum(...)`` (the builtin, ``Name`` node ``sum`` — NOT
``stable_sum``, NOT ``math.fsum``) takes an argument that references an
unambiguous float-money/percent token. A future PR that adds a new such sum, or
reverts a ``stable_sum`` back to ``sum``, fails here on the dev box and in the
3.13-only CI — closing the gap the deferred matrix would otherwise have left.

Scope note: the token set is deliberately the unambiguous money/percent FIELD
names (attribute access / dict keys), not local variable names, to stay
false-positive-free. Integer/token sums (``sum(b.total_tokens ...)``,
``sum(col_widths)``, ``int(sum(ws))``) and ``+=`` accumulators are out of scope
by design and are not matched.
"""
import ast
import pathlib

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"

# Unambiguous float money/percent tokens that appear in the converted sites.
# Each denotes a float value; a bare builtin sum() over any of them reaches
# byte-compared output and MUST go through stable_sum for cross-version parity.
FLOAT_SUM_TOKENS = (
    "cost_usd",
    "net_usd",
    "saved_usd",
    "wasted_usd",
    "dollars_per_percent",
    "y_value",
    "weekly_cost",
    "total_cost_usd",
)


def _python_sources():
    """Every Python file under bin/ — the extracted kernels/command glue plus
    the single-file ``bin/cctally`` entry point (no ``.py`` suffix)."""
    files = sorted(BIN.glob("*.py"))
    main = BIN / "cctally"
    if main.exists():
        files.append(main)
    return files


def _arg_source(src: str, call: ast.Call) -> str:
    """Concatenated source text of a call's positional + keyword args."""
    segs = []
    for node in (*call.args, *(kw.value for kw in call.keywords)):
        seg = ast.get_source_segment(src, node)
        if seg:
            segs.append(seg)
    return " ".join(segs)


def _bare_sum_calls(src: str):
    """Yield (lineno, arg_source) for every builtin ``sum(...)`` call.

    Only ``Call`` nodes whose ``func`` is the bare ``Name`` ``sum`` match —
    ``stable_sum`` (a different ``Name``) and ``math.fsum`` (an ``Attribute``)
    are excluded structurally, so comments/strings/other identifiers can never
    create a false positive.
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "sum"
        ):
            yield node.lineno, _arg_source(src, node)


def _stable_sum_calls(src: str):
    """Yield (lineno, arg_source) for every ``stable_sum(...)`` call."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "stable_sum"
        ):
            yield node.lineno, _arg_source(src, node)


def test_no_bare_sum_over_float_money_tokens():
    """No builtin ``sum()`` may sum a float money/percent token anywhere in
    ``bin/`` — those sums must use ``stable_sum`` so output is byte-identical
    across CPython 3.11/3.12/3.13."""
    offenders = []
    for path in _python_sources():
        src = path.read_text(encoding="utf-8")
        for lineno, arg_src in _bare_sum_calls(src):
            hit = next((t for t in FLOAT_SUM_TOKENS if t in arg_src), None)
            if hit is not None:
                offenders.append(f"{path.name}:{lineno}  sum(... {hit} ...)")
    assert not offenders, (
        "Bare builtin sum() over a float money/percent token found — use "
        "_lib_fmt.stable_sum (math.fsum) instead so the rendered/JSON value is "
        "byte-identical across CPython 3.11/3.12/3.13. If this is genuinely an "
        "integer/non-output sum, remove the token from the expression or update "
        "FLOAT_SUM_TOKENS with a justification.\n  " + "\n  ".join(offenders)
    )


def test_guard_is_non_vacuous():
    """Sanity: the converted code actually contains stable_sum() calls over the
    guarded tokens, so the guard above is exercising a real surface (not passing
    because nothing matches the token set)."""
    matched = 0
    for path in _python_sources():
        src = path.read_text(encoding="utf-8")
        for _lineno, arg_src in _stable_sum_calls(src):
            if any(t in arg_src for t in FLOAT_SUM_TOKENS):
                matched += 1
    # The conversion touched well over a dozen such sites across render/
    # dashboard/share/cache-report; require a healthy floor so a future mass
    # revert (which would also strip these) can't make the guard vacuous.
    assert matched >= 10, (
        f"expected >=10 stable_sum() calls over float money/percent tokens, "
        f"found {matched} — the guard's token set may be stale"
    )

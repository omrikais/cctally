"""#195 gate P1-5: a read site that forgets the split does not raise, warn, or
move a golden — it silently under-prices. These are the three layers that make
that impossible."""
import ast, pathlib, pytest
import importlib.util

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
_SPEC = importlib.util.spec_from_file_location("_lib_pricing", BIN / "_lib_pricing.py")
pricing = importlib.util.module_from_spec(_SPEC); _SPEC.loader.exec_module(pricing)


def _scanned_sources():
    """Every Python source in bin/ — INCLUDING the extension-less entry point.

    `bin/cctally` is a Python module in everything but its filename, and it
    still holds cost-feeding code (`_usage_entry_from_joined`). A `*.py` glob
    silently skips it, which is exactly how a hand-rolled usage dict shipped
    past both scanners in the #195 gate."""
    return sorted(BIN.glob("*.py")) + [BIN / "cctally"]

# Usage dicts that legitimately never feed the cost engine.
# NON-VACUITY: every entry must still match a real site (see the guard tests).
ALLOWLIST = {
    # The Codex source adapter: `cache_creation_input_tokens` is structurally 0
    # (CODEX_MODEL_PRICING has no cache_creation leg) and the wrapper supplies
    # cache_saved_usd / cache_wasted_usd / cache_net_usd explicitly, so
    # _compute_entry_cache_dollars is never reached for these entries.
    ("_cctally_dashboard_sources.py", "Codex adapter — no cache_creation leg"),
    ("_lib_pricing.py", "claude_usage_dict itself — the canonical builder"),
}


WIRE_SHAPE_ALLOWLIST = {
    "_fixture_builders.py":
        "shared synthetic Claude JSONL emitter consumed by ingest tests",
    "build-bench-fixtures.py":
        "synthetic Claude JSONL benchmark input",
    "build-codex-parity-fixtures.py":
        "synthetic Claude reference transcript beside Codex parity data",
    "build-e2e-fixtures.py":
        "synthetic Claude conversation JSONL for end-to-end fixtures",
    "build-migrations-fixtures.py":
        "synthetic Claude JSONL used to seed migration fixtures",
    "build-statusline-fixtures.py":
        "synthetic Claude JSONL used by statusline fixtures",
}


def _is_wire_shape_emitter(name: str) -> bool:
    """Return whether one proven file emits Anthropic wire-format `usage`.

    These objects are input to `_classify_cost_entry`, not cost-feeding dicts.
    Routing them through `claude_usage_dict` would change the wire shape.
    """
    return name in WIRE_SHAPE_ALLOWLIST


# Display-only SELECT projections: they read `cache_create_tokens` to RENDER
# token counts, never to compute cost. Keyed on the exact projection text (not
# the filename) so a cost-bearing SELECT in the SAME file stays covered —
# `_lib_conversation_query.py` has both.
SELECT_ALLOWLIST = {
    ('"SELECT msg_id, req_id, input_tokens, output_tokens, "\n'
     '               "cache_create_tokens, cache_read_tokens, speed "'):
        "_turn_usage_map feeds display-only cache savings/rebuild estimates; "
        "effective speed is authoritative, but a reconstructed lost-prefix "
        "subset has no authoritative mapping to the row's 5m/1h write split",
    ('"SELECT cache_create_tokens, cache_read_tokens '):
        "_fixture_builders self-check of the param -> column mapping",
    ('(SELECT COALESCE(MAX(mutation_seq), 0) + 1 '):
        "not a projection at all — the mutation_seq subquery of an INSERT whose "
        "column list happens to name cache_create_tokens within the scan window",
    ('"WHERE cache_create_tokens > 0 AND cache_create_1h_tokens IS NULL"'):
        "_validate_cache_rows completeness probe — counts unknown TTL splits; "
        "it never computes cost",
}


def test_builder_requires_the_split_keyword():
    """Layer 1: an omission is a TypeError at call time, not silent mispricing."""
    with pytest.raises(TypeError):
        pricing.claude_usage_dict(
            speed=None, input_tokens=1, cache_creation_tokens=10)


def test_builder_requires_the_speed_keyword():
    """Layer 1: every cost-feeding usage dict declares effective speed."""
    with pytest.raises(TypeError):
        pricing.claude_usage_dict(
            cache_1h_tokens=None, input_tokens=1, cache_creation_tokens=10)


def _conversation_query():
    """`_lib_conversation_query` imports several bin/ siblings by bare name."""
    import sys
    if str(BIN) not in sys.path:
        sys.path.insert(0, str(BIN))
    import _lib_conversation_query
    return _lib_conversation_query


def test_entry_cost_requires_the_split_keyword():
    """Layer 1, second wrapper. `_entry_cost` is a thin shim over the builder,
    so a defaulted split there re-opens exactly the hazard the required keyword
    closes: a future caller that forgets it degrades SILENTLY. It has one
    caller today — the cost of keeping it required is one keyword."""
    q = _conversation_query()
    with pytest.raises(TypeError):
        q._entry_cost(
            "claude-opus-5", 1, 1, 10, 1, None, speed=None)


def test_entry_cost_requires_the_speed_keyword():
    """The conversation row shim must not silently default the speed tier."""
    q = _conversation_query()
    with pytest.raises(TypeError):
        q._entry_cost(
            "claude-opus-5", 1, 1, 10, 1, None, cc_1h=None)


def test_builder_omits_the_key_when_split_is_none():
    d = pricing.claude_usage_dict(
        speed=None, cache_creation_tokens=10, cache_1h_tokens=None)
    assert "cache_creation_1h_input_tokens" not in d


def test_builder_sets_the_key_when_split_is_known():
    d = pricing.claude_usage_dict(
        speed=None, cache_creation_tokens=10, cache_1h_tokens=4)
    assert d["cache_creation_1h_input_tokens"] == 4


def test_builder_sets_speed_only_when_retained():
    absent = pricing.claude_usage_dict(speed=None, cache_1h_tokens=None)
    fast = pricing.claude_usage_dict(speed="fast", cache_1h_tokens=None)
    assert "speed" not in absent
    assert fast["speed"] == "fast"


def _usage_builder_calls_missing_speed():
    hits = []
    for path in _scanned_sources():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else ""
            )
            if not name.endswith("claude_usage_dict"):
                continue
            if not any(kw.arg == "speed" for kw in node.keywords):
                hits.append((path.name, node.lineno))
    return hits


def test_all_cost_usage_builders_declare_effective_speed():
    assert _usage_builder_calls_missing_speed() == []


def _literal_usage_dicts():
    """Layer 2: find hand-rolled usage dicts that bypass the builder."""
    hits = []
    for path in _scanned_sources():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if {"cache_creation_input_tokens", "cache_read_input_tokens"} <= keys:
                hits.append((path.name, node.lineno))
    return hits


def _unexpected_literal_usage_dicts(hits=None):
    """Return cost-like dict sites that have no exact justified exemption."""
    if hits is None:
        hits = _literal_usage_dicts()
    return [
        (name, line)
        for name, line in hits
        if not any(name == allowed for allowed, _ in ALLOWLIST)
        and not _is_wire_shape_emitter(name)
    ]


def test_no_hand_rolled_usage_dicts_outside_the_builder():
    unexpected = _unexpected_literal_usage_dicts()
    assert unexpected == [], (
        "these sites build a usage dict by hand and will silently under-price; "
        f"route them through claude_usage_dict: {unexpected}")


def test_allowlist_is_non_vacuous():
    """An allowlist entry that no longer matches a real site must fail, so the
    allowlist can never rot into a blanket suppression."""
    live = {n for n, _ in _literal_usage_dicts()}
    stale = {a for a, _ in ALLOWLIST if a not in live}
    assert stale == set(), f"stale allowlist entries: {stale}"


def _stale_wire_shape_allowlist_entries(live=None):
    if live is None:
        live = {name for name, _ in _literal_usage_dicts()}
    return set(WIRE_SHAPE_ALLOWLIST) - set(live)


def test_wire_shape_carve_out_is_non_vacuous():
    """Every explicit wire-shape exemption must still match a real site."""
    stale = _stale_wire_shape_allowlist_entries()
    assert stale == set(), f"stale wire-shape allowlist entries: {stale}"
    assert all(reason.strip() for reason in WIRE_SHAPE_ALLOWLIST.values())


def test_wire_shape_carve_out_rejects_an_unlisted_builder():
    """A new builder's cost-like dict reaches the scanner's offender list."""
    hit = ("build-unlisted-wire-shape.py", 41)
    assert _unexpected_literal_usage_dicts([hit]) == [hit]


def test_wire_shape_carve_out_reports_each_stale_entry():
    """Removing one live emitter makes that exact allowlist entry stale."""
    live = set(WIRE_SHAPE_ALLOWLIST)
    removed = "build-bench-fixtures.py"
    live.remove(removed)
    assert _stale_wire_shape_allowlist_entries(live) == {removed}


def _select_offenders():
    offenders = []
    for path in _scanned_sources():
        src = path.read_text(encoding="utf-8", errors="replace")
        for chunk in src.split("FROM session_entries")[:-1]:
            tail = chunk[-800:]
            if "cache_create_tokens" not in tail or "cache_create_1h_tokens" in tail:
                continue
            if any(marker in tail for marker in SELECT_ALLOWLIST):
                continue
            offenders.append(path.name)
    return offenders


def _speed_select_offenders():
    offenders = []
    for path in _scanned_sources():
        src = path.read_text(encoding="utf-8", errors="replace")
        for chunk in src.split("FROM session_entries")[:-1]:
            tail = chunk[-800:]
            if "cache_create_tokens" not in tail or "speed" in tail:
                continue
            if any(marker in tail for marker in SELECT_ALLOWLIST):
                continue
            offenders.append(path.name)
    return offenders


def test_session_entries_cost_selects_carry_the_split_column():
    """Layer 2b: routing through the builder is useless if the SELECT that
    feeds it never fetched the column."""
    offenders = _select_offenders()
    assert offenders == [], (
        "these SELECTs read cache_create_tokens for cost without the 1h column: "
        f"{sorted(set(offenders))}")


def test_session_entries_cost_selects_carry_the_speed_column():
    """A cost-bearing projection must retain the authoritative effective tier."""
    offenders = _speed_select_offenders()
    assert offenders == [], (
        "these SELECTs read cache_create_tokens for cost without speed: "
        f"{sorted(set(offenders))}")


def test_select_allowlist_is_non_vacuous():
    """Every allowlisted projection must still exist verbatim in bin/."""
    blob = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in _scanned_sources())
    stale = [m for m in SELECT_ALLOWLIST if m not in blob]
    assert stale == [], f"stale SELECT allowlist entries: {stale}"

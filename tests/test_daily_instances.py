"""Unit tests for daily --instances / -p / --project-aliases (issue #86 Session E).

In-memory only: the pure aggregator + JSON/render helpers + parser are exercised
with hand-built UsageEntry / ProjectKey objects. SQLite-backed golden coverage
lives in bin/cctally-daily-instances-test (Task 2).
"""
import datetime as dt
import json
import sys

import conftest

# load_script() compiles + execs bin/cctally into a real ModuleType registered
# as sys.modules["cctally"], pulling every sibling (_lib_aggregators,
# _lib_render, _cctally_cache, _lib_jsonl) into sys.modules as a side effect.
# It also handles the no-.py-extension loading the stdlib importlib can't.
conftest.load_script()
cctally = sys.modules["cctally"]
agg = sys.modules["_lib_aggregators"]
render = sys.modules["_lib_render"]
cache = sys.modules["_cctally_cache"]
UsageEntry = sys.modules["_lib_jsonl"].UsageEntry
ProjectKey = cache.ProjectKey


def test_parse_project_aliases_valid_malformed_empty():
    f = cctally._parse_project_aliases
    assert f("a=Apple,b=Banana") == {"a": "Apple", "b": "Banana"}
    # malformed (no '='), empty segments, and blank key/value are dropped.
    assert f("a=Apple,,garbage,=X,y=,c = Carrot ") == {"a": "Apple", "c": "Carrot"}
    assert f(None) == {}
    assert f("") == {}


def test_alias_for_matches_display_then_paths():
    f = cctally._alias_for
    key = ProjectKey(bucket_path="/r/work/app", display_key="app",
                     git_root="/r/work/app")
    assert f(key, {"app": "WorkApp"}) == "WorkApp"
    assert f(key, {"/r/work/app": "ByGitRoot"}) == "ByGitRoot"
    assert f(key, {}) is None
    assert f(key, None) is None


def _ue(ts_iso, model, inp, out, cost=None):
    return UsageEntry(
        timestamp=dt.datetime.fromisoformat(ts_iso),
        model=model,
        usage={"input_tokens": inp, "output_tokens": out,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        cost_usd=cost,
        source_path="/x.jsonl",
    )


def _key(bucket_path, display_key, git_root=None):
    return ProjectKey(bucket_path=bucket_path, display_key=display_key,
                      git_root=git_root or bucket_path)


def _je(ts_iso, model, inp, out, *, usage_extra=None):
    return cache._JoinedClaudeEntry(
        timestamp=dt.datetime.fromisoformat(ts_iso),
        model=model,
        input_tokens=inp, output_tokens=out,
        cache_creation_tokens=0, cache_read_tokens=0,
        source_path="/x.jsonl", session_id="s1", project_path="/r/app",
        cost_usd=None, usage_extra=usage_extra,
    )


def test_usage_entry_from_joined_carries_speed_extra():
    # The joined-entry adapter must restore non-token `usage` extras (notably
    # `speed`) so the project-axis daily path renders `<model>-fast` like the
    # normal path. Token keys stay intact; absent extras → no speed key.
    f = cctally._usage_entry_from_joined
    fast = f(_je("2026-05-20T08:00:00+00:00", "claude-haiku-4-5", 100, 20,
                 usage_extra={"speed": "fast"}))
    assert fast.usage["speed"] == "fast"
    assert fast.usage["input_tokens"] == 100 and fast.usage["output_tokens"] == 20
    plain = f(_je("2026-05-20T08:00:00+00:00", "claude-haiku-4-5", 100, 20))
    assert "speed" not in plain.usage


def test_aggregate_daily_by_project_renders_fast_model_from_speed_extra():
    # End-to-end of the P2 fix: a fast-tier joined entry, once adapted, must
    # surface as `<model>-fast` in the group's models / model_breakdowns —
    # identical to the non-instances path (`_aggregate_buckets` reads
    # `usage["speed"]`). A plain entry stays the bare model name.
    k = _key("/r/lib", "lib")
    fast_ue = cctally._usage_entry_from_joined(
        _je("2026-05-20T08:00:00+00:00", "claude-haiku-4-5", 200, 40,
            usage_extra={"speed": "fast"})
    )
    groups = agg._aggregate_daily_by_project([(k, fast_ue)], tz=None, mode="auto")
    bucket = groups[0][1][0]
    assert bucket.models == ["claude-haiku-4-5-fast"]
    assert [m["modelName"] for m in bucket.model_breakdowns] == ["claude-haiku-4-5-fast"]


def test_aggregate_daily_by_project_groups_orders_and_keeps_same_basename_distinct():
    # Two distinct git-roots that share the basename "app".
    work = _key("/r/work/app", "app", "/r/work/app")
    personal = _key("/r/personal/app", "app", "/r/personal/app")
    cheap = _key("/r/lib", "lib", "/r/lib")
    keyed = [
        (work, _ue("2026-05-20T10:00:00+00:00", "claude-sonnet-4-5", 1000, 1000)),
        (work, _ue("2026-05-21T10:00:00+00:00", "claude-sonnet-4-5", 2000, 2000)),
        (personal, _ue("2026-05-20T10:00:00+00:00", "claude-opus-4-1", 5_000_000, 5_000_000)),
        (cheap, _ue("2026-05-20T10:00:00+00:00", "claude-haiku-4-5", 10, 10)),
    ]
    groups = agg._aggregate_daily_by_project(keyed, tz=None, mode="auto")
    # Distinct ProjectKey objects => 3 groups (same-basename NOT merged).
    assert len(groups) == 3
    keys = [k for k, _ in groups]
    assert work in keys and personal in keys and cheap in keys
    # Ordered by total cost desc: personal (opus, huge) > work > lib (haiku, tiny).
    assert keys[0] is personal
    assert keys[-1] is cheap
    # work group has both dates, date-asc.
    work_buckets = dict(groups)[work]
    assert [b.bucket for b in work_buckets] == ["2026-05-20", "2026-05-21"]


def test_aggregate_daily_by_project_mode_display_zeroes_absent_cost():
    k = _key("/r/app", "app")
    keyed = [(k, _ue("2026-05-20T10:00:00+00:00", "claude-sonnet-4-5", 1000, 1000, cost=None))]
    groups = agg._aggregate_daily_by_project(keyed, tz=None, mode="display")
    buckets = groups[0][1]
    assert buckets[0].cost_usd == 0.0  # display + no recorded costUSD => $0


def _bucket(date, cost, models=("claude-sonnet-4-5",)):
    BU = agg.BucketUsage
    return BU(bucket=date, input_tokens=10, output_tokens=20,
              cache_creation_tokens=0, cache_read_tokens=0, total_tokens=30,
              cost_usd=cost, models=list(models),
              model_breakdowns=[{"modelName": models[0], "inputTokens": 10,
                                 "outputTokens": 20, "cacheCreationTokens": 0,
                                 "cacheReadTokens": 0, "cost": cost}])


def test_daily_row_dict_parity_with_bucket_to_json():
    b = _bucket("2026-05-20", 1.5)
    # The extracted single-row builder must match the row _bucket_to_json emits.
    row = render._daily_row_dict(b, date_key="date")
    via_bucket = json.loads(render._bucket_to_json([b], list_key="daily", date_key="date"))
    assert via_bucket["daily"][0] == row


def test_bucket_by_project_to_json_shape_and_order():
    groups = [("app (work)", [_bucket("2026-05-20", 9.0)]),
              ("lib", [_bucket("2026-05-20", 0.5)])]
    out = json.loads(render._bucket_by_project_to_json(groups, date_key="date"))
    assert list(out.keys()) == ["schemaVersion", "projects", "totals"]
    assert out["schemaVersion"] == 1
    # Keys preserve caller (cost-desc) order.
    assert list(out["projects"].keys()) == ["app (work)", "lib"]
    assert out["projects"]["app (work)"][0]["date"] == "2026-05-20"
    assert out["totals"]["totalCost"] == 9.5
    assert out["totals"]["totalTokens"] == 60


def test_json_labels_dedupe_residual_collision_no_data_loss():
    # Two distinct git-roots that collide AFTER disambiguation: same basename
    # ("app") AND same immediate-parent basename ("x") → both disambiguate to
    # "app (x)". `_project_disambiguate_labels` alone can't tell them apart;
    # cmd_daily's label-resolution must counter-suffix the second so the JSON
    # `projects` dict keeps BOTH groups (no silent overwrite / data loss).
    a = _key("/a/x/app", "app", "/a/x/app")
    b = _key("/b/x/app", "app", "/b/x/app")
    groups = [(a, [_bucket("2026-05-20", 9.0)]),
              (b, [_bucket("2026-05-20", 1.0)])]

    # Sanity: the bare disambiguation helper DOES collide on these two.
    aug = render._project_disambiguate_labels(
        [{"key": k, "cost_usd": sum(x.cost_usd for x in bl)} for k, bl in groups]
    )
    assert aug.get(0) == "app (x)" and aug.get(1) == "app (x)"

    # Replicate cmd_daily's json_label resolution loop (the fix under test).
    seen: dict = {}
    json_groups = []
    for i, (k, bl) in enumerate(groups):
        base = aug.get(i, k.display_key)
        n = seen.get(base, 0) + 1
        seen[base] = n
        json_label = base if n == 1 else f"{base} (#{n})"
        json_groups.append((json_label, bl))

    out = json.loads(render._bucket_by_project_to_json(json_groups, date_key="date"))
    keys = list(out["projects"].keys())
    # BOTH groups survive with distinct keys — second counter-suffixed.
    assert keys == ["app (x)", "app (x) (#2)"]
    assert len(out["projects"]) == 2
    # Each group's distinct cost is preserved (no overwrite).
    assert out["projects"]["app (x)"][0]["totalCost"] == 9.0
    assert out["projects"]["app (x) (#2)"][0]["totalCost"] == 1.0
    # Totals sum both groups.
    assert out["totals"]["totalCost"] == 10.0


def test_table_labels_dedupe_alias_collision_keeps_sections_distinct():
    # A basename alias (`app=Alias`) matches BOTH same-basename git-roots via
    # `_alias_for`'s display_key lookup, so the raw aliased table label collides.
    # cmd_daily's table-label `(#N)` counter must keep the two distinct-total
    # sections tellable apart — replicate that loop (the P3 fix under test).
    a = _key("/a/work/app", "app", "/a/work/app")
    b = _key("/b/personal/app", "app", "/b/personal/app")
    aliases = {"app": "Alias"}
    # json_labels are already disambiguated/unique upstream (see the json-dedup
    # test); only the alias-collapsed table labels need the extra counter.
    json_labels = ["app (work)", "app (personal)"]

    seen_table: dict = {}
    table_labels = []
    for k, jl in zip((a, b), json_labels):
        base = cctally._alias_for(k, aliases) or jl
        nt = seen_table.get(base, 0) + 1
        seen_table[base] = nt
        table_labels.append(base if nt == 1 else f"{base} (#{nt})")

    # Both map to "Alias" pre-counter; the counter makes them distinct.
    assert table_labels == ["Alias", "Alias (#2)"]
    # No alias → table label falls back to the (already-distinct) json_label.
    assert (cctally._alias_for(a, {}) or json_labels[0]) == "app (work)"


def test_render_bucket_table_section_layout_has_project_headers_and_one_total():
    groups = [("app (work)", [_bucket("2026-05-20", 9.0)]),
              ("app (personal)", [_bucket("2026-05-20", 1.0)])]
    out = render._render_bucket_table(
        [], first_col_name="Date", title_suffix="Daily",
        compact_split_fn=render._daily_compact_split,
        breakdown=False, compact=False, project_groups=groups,
    )
    assert "Project: app (work)" in out
    assert "Project: app (personal)" in out
    # Exactly one global Total footer row (the "Total Tokens" header column
    # also contains the substring "Total", so match the footer row's first
    # cell specifically: a "│ Total " left-aligned cell in the Date column).
    import re
    plain = re.sub(r"\033\[[0-9;]*m", "", out)
    footer_rows = [
        ln for ln in plain.splitlines()
        if re.match(r"^[│|]\s*Total\s", ln) and "Total Tokens" not in ln
    ]
    assert len(footer_rows) == 1
    # Footer sums both projects (9.0 + 1.0 = $10.00), proving one global total.
    assert "$10.00" in footer_rows[0]


def _daily_args(argv):
    parser = cctally.build_parser()
    return parser.parse_args(argv)


def test_daily_parser_has_instances_project_aliases_flat_and_nested():
    flat = _daily_args(["daily", "-i", "-p", "foo", "-p", "bar",
                        "--project-aliases", "foo=Foo"])
    assert flat.instances is True
    assert flat.project == ["foo", "bar"]          # repeatable append
    assert flat.project_aliases == "foo=Foo"
    nested = _daily_args(["claude", "daily", "-i", "-p", "foo"])
    assert nested.instances is True
    assert nested.project == ["foo"]


def test_daily_parser_defaults():
    a = _daily_args(["daily"])
    assert a.instances is False
    assert a.project is None
    assert a.project_aliases is None

"""usage: scratch_proof.py MID_DATA AFTER_DATA SCRATCH_ROOT OUT.json

#901 §6.3 scratch-root proof (esc-0db0e25cdfa4): adding ROOT/scratch to
CLAUDE_CONFIG_DIR / CODEX_HOME must not purge, replay or duplicate any row the
real roots already own. MID = a CoW copy of the clone after a warm run on the
real roots alone; AFTER = the clone after a warm run on real + scratch roots
with scratch appends. Below each file's MID frontier, every per-file aggregate
(count, offsets, tokens, row ids, accounts) must be identical in AFTER: equal
ids rule out delete-and-reinsert replays that keep counts equal.
Opens only the two scratch copies. Exit 0 PASS, 1 FAIL.
Run by run-scratch-proof.sh."""
import json, os, sqlite3, sys

mid_dir, after_dir, scratch, out = sys.argv[1:5]
res = {"checks": {}, "fail": []}


def conn(d, name):
    # Read-write on purpose: both are scratch copies, and a candidate dashboard can
    # leave a WAL behind (no checkpoint on close) that a read-only open may refuse.
    c = sqlite3.connect(f"{d}/{name}")
    c.execute("PRAGMA temp_store=MEMORY")
    c.execute("ATTACH ? AS mid", (f"{mid_dir}/{name}",))
    return c


def per_file(c, schema, files, entries, offcol, extra, key="path"):
    q = (f"SELECT f.{key}, count(e.rowid), total(e.{offcol}), {extra}, total(e.rowid), "
         f"group_concat(DISTINCT e.account_key) FROM mid.{files} f "
         f"LEFT JOIN {schema}.{entries} e ON e.source_path=f.{key} AND e.{offcol} < f.last_byte_offset "
         f"GROUP BY f.{key}")
    return {r[0]: list(r[1:]) for r in c.execute(q)}


def compare(label, c, files, entries, offcol, extra):
    a = per_file(c, "mid", files, entries, offcol, extra)
    b = per_file(c, "main", files, entries, offcol, extra)
    diff = [p for p in a if a[p][:4] != b[p][:4] or (a[p][4] or "") != (b[p][4] or "")]
    res["checks"][label] = {"files": len(a), "rows_below_frontier": sum(v[0] for v in a.values()),
                            "differing": len(diff), "sample": [(p, a[p], b[p]) for p in diff[:5]]}
    if diff:
        res["fail"].append(f"{label}: {len(diff)} file(s) changed below the MID frontier")


def file_sets(label, c, files, root_col=None):
    mid = {r[0]: r[1:] for r in c.execute(f"SELECT path{', ' + root_col if root_col else ''} FROM mid.{files}")}
    aft = {r[0]: r[1:] for r in c.execute(f"SELECT path{', ' + root_col if root_col else ''} FROM main.{files}")}
    gone = [p for p in mid if p not in aft]
    new = [p for p in aft if p not in mid]
    new_scratch = [p for p in new if p.startswith(scratch)]
    new_real = [p for p in new if not p.startswith(scratch)]
    rekeyed = [p for p in mid if p in aft and mid[p] != aft[p]] if root_col else []
    res["checks"][label + ".files"] = {"mid": len(mid), "after": len(aft), "gone": gone[:10], "gone_n": len(gone),
                                       "gone_still_on_disk": [p for p in gone if os.path.exists(p)][:10],
                                       "new_scratch": new_scratch, "new_real_n": len(new_real),
                                       "new_real_missing_on_disk": [p for p in new_real if not os.path.exists(p)][:10],
                                       "rekeyed": rekeyed[:10], "rekeyed_n": len(rekeyed)}
    if any(os.path.exists(p) for p in gone):
        res["fail"].append(f"{label}: tracked file(s) still on disk were dropped")
    if rekeyed:
        res["fail"].append(f"{label}: {len(rekeyed)} file(s) changed root key")
    if not new_scratch:
        res["fail"].append(f"{label}: the scratch root's seed was not ingested")


def dups(label, c, q):
    n = c.execute(q).fetchone()[0]
    res["checks"][label] = n
    if n:
        res["fail"].append(f"{label}: {n} duplicate key(s)")


c = conn(after_dir, "cache.db")
file_sets("cache.claude", c, "session_files")
file_sets("cache.codex", c, "codex_session_files", "source_root_key")
compare("cache.session_entries", c, "session_files", "session_entries", "line_offset",
        "total(e.input_tokens+e.output_tokens+e.cache_create_tokens+e.cache_read_tokens)")
compare("cache.codex_session_entries", c, "codex_session_files", "codex_session_entries", "line_offset",
        "total(e.total_tokens)")
compare("cache.quota_window_snapshots", c, "codex_session_files", "quota_window_snapshots", "line_offset",
        "total(e.used_percent)")
dups("cache.session_entries.dup", c,
     "SELECT count(*) FROM (SELECT 1 FROM session_entries GROUP BY source_path, line_offset HAVING count(*)>1)")
dups("cache.codex_session_entries.dup", c,
     "SELECT count(*) FROM (SELECT 1 FROM codex_session_entries GROUP BY source_root_key, source_path, line_offset HAVING count(*)>1)")
roots_mid = c.execute("SELECT * FROM mid.codex_source_roots ORDER BY 1").fetchall()
roots_aft = c.execute("SELECT * FROM main.codex_source_roots ORDER BY 1").fetchall()
res["checks"]["cache.codex_source_roots"] = {"mid": [r[:2] for r in roots_mid], "after": [r[:2] for r in roots_aft]}
if not set(r[0] for r in roots_mid) <= set(r[0] for r in roots_aft):
    res["fail"].append("cache.codex_source_roots: a real root key disappeared")
by_root = lambda s: dict(c.execute(f"SELECT source_root_key, count(*) FROM {s}.codex_session_entries GROUP BY 1"))
res["checks"]["cache.codex_rows_by_root"] = {"mid": by_root("mid"), "after": by_root("main")}
res["checks"]["cache.change_logs"] = {
    t: [c.execute(f"SELECT count(*) FROM {s}.{t}").fetchone()[0] for s in ("mid", "main")]
    for t in ("quota_window_change_log", "codex_accounting_change_log")}
c.close()

c = conn(after_dir, "conversations.db")
file_sets("conv.claude", c, "conversation_source_files")
file_sets("conv.codex", c, "codex_conversation_source_files", "source_root_key")
compare("conv.conversation_messages", c, "conversation_source_files", "conversation_messages", "byte_offset",
        "total(length(e.text))")
compare("conv.codex_conversation_messages", c, "codex_conversation_source_files", "codex_conversation_messages",
        "line_offset", "total(e.content_len)")
dups("conv.conversation_messages.dup", c,
     "SELECT count(*) FROM (SELECT 1 FROM conversation_messages GROUP BY session_id, uuid HAVING count(*)>1)")
c.close()

res["verdict"] = "PASS" if not res["fail"] else "FAIL"
json.dump(res, open(out, "w"), indent=1, default=str)
print(json.dumps({"verdict": res["verdict"], "fail": res["fail"]}))
for k, v in res["checks"].items():
    print(k, json.dumps(v, default=str)[:400])
sys.exit(0 if not res["fail"] else 1)

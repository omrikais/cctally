#!/usr/bin/env python3
"""Content digest of a measured clone, to prove a write reduction changed no result (#939).

  accounting_digest.py digest --data DIR --out OUT.json [--db NAME ...] [--fts-integrity]
  accounting_digest.py compare A.json B.json [--pre PRE.json]

`digest` reads each named store of DIR read-only (default: conversations.db
and cache.db) and records, per table, the row count and an order-independent
multiset hash of its rows: a sum, modulo 2**128, of a BLAKE2b digest of each
row's repr. Order independence matters because a remedy may legitimately
change the order rows are written in, never their content. Excluded, exactly
as the frozen self-test's control comparison excludes them
(`frozen_selftest.WALL_CLOCK`, `NONCE`, `WALL_CLOCK_KEY_MARKERS`): columns that
hold the ingest's own wall clock, per-run nonces and wall-clock meta keys.
An identity the ingest mints at random on every run (`CANONICAL_NONCE_COLUMNS`,
a source incarnation) is not dropped but replaced by the path of the cursor row
that holds it as its current incarnation, so a stamp still has to name the same
incarnation as its cursor; an id no cursor holds (a superseded incarnation the
copy already carried) is kept as stored.
Also excluded are the FTS5 shadow tables whose bytes are index layout rather
than content (`*_data`, `*_idx`, `*_config`): segment layout depends on how
the writes were grouped into transactions. The FTS index is judged instead by
its answers: for every FTS5 table, the sorted rowids that match each term of a
fixed term list (`FTS_TERMS`, prose and code vocabulary), hashed, with their
count; and, with `--fts-integrity` (on a disposable clone only, because the
command needs a writable connection), the result of FTS5's own
`integrity-check` for every FTS5 table.

Per-transaction counters (`RUN_COUNTER_COLUMNS`, `RUN_COUNTER_KEYS`) are
recorded under `counters` and never hashed. `compare --pre` judges them against
a digest of the store the two clones came from: the rows (and meta keys) whose
counter advanced must be the same in both clones, and every keyed row whose
content changed from PRE must have advanced its counter. A JSON-object meta
value is hashed without its wall-clock fields. A key/value table, or a table with a primary
key, of at most `KEYED_ROWS_MAX` rows also records one hash per row key, and
`compare` names the differing keys.

`compare` prints every table whose count or hash differs and exits 1 when any
does, 0 when the two digests agree on every table both name, and 2 when one
names a table the other lacks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from frozen_selftest import NONCE, WALL_CLOCK, WALL_CLOCK_KEY_MARKERS  # noqa: E402

FTS_LAYOUT_SUFFIXES = ("_data", "_idx", "_config")
FTS_TERMS = tuple(dict.fromkeys("""
error test the python sqlite commit function file codex dashboard migration
cost session claude issue and for that with this from have not are was but
can will use should would could which when where what why how all any each
other some such only same than then there these they their into about after
before because between during through under again further once here both few
more most own very just also return import class def self true false none null
print json yaml config path data value key index table select insert update
delete create where join group order limit async await const let var type
interface string number list dict array object error exception raise try except
finally while break continue yield lambda git branch merge push pull diff log
status rebase stash tag worktree npm node react css html http https api server
client request response token user assistant model tool result message review
""".split()))
MOD = 1 << 128
#: Columns holding an identity the ingest mints at random on every run, in any
#: table (`_lib_conversation.new_source_incarnation_id`). Canonicalized to the
#: path of the cursor row whose current incarnation it is.
CANONICAL_NONCE_COLUMNS = {"source_incarnation_id"}
#: (cursor table, path column) pairs that name each current incarnation.
INCARNATION_OWNERS = (("conversation_source_files", "path"),)
#: Counters that advance once per committed transaction that changed derived
#: state (#901 §5.3b), so a remedy that groups the same writes into fewer
#: transactions moves them by a different amount. #901 G10 (a) (901-SR-023)
#: requires only that they advance, never numeric equality with a control.
#: They are recorded under `counters` and never hashed.
RUN_COUNTER_COLUMNS = {"render_revision"}
RUN_COUNTER_KEYS = {"codex_find_projection_generation", "conversation_render_revision"}
#: Inside a JSON-object meta value, fields with these suffixes hold the
#: ingest's own wall clock (`observedAt`, `readsPendingSince`, ...).
JSON_WALL_CLOCK_SUFFIXES = ("At", "Since", "_at", "_utc")
#: A table this small with a primary key (or a key/value table) also records
#: one hash per row key, so `compare` names the rows that differ rather than
#: only the table.
KEYED_ROWS_MAX = 2000


def _strip_json_wall_clock(value):
    if isinstance(value, dict):
        return {k: _strip_json_wall_clock(v) for k, v in sorted(value.items())
                if not k.endswith(JSON_WALL_CLOCK_SUFFIXES)}
    if isinstance(value, list):
        return [_strip_json_wall_clock(v) for v in value]
    return value


def _normalized_meta_value(value):
    """A JSON-object meta value without its wall-clock fields; else as stored."""
    if isinstance(value, (str, bytes)) and value[:1] in ("{", b"{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        if isinstance(parsed, dict):
            return json.dumps(_strip_json_wall_clock(parsed), sort_keys=True)
    return value


def _row_hash(row) -> int:
    return int.from_bytes(hashlib.blake2b(repr(row).encode("utf-8", "surrogatepass"),
                                          digest_size=16).digest(), "big")


def _incarnation_paths(conn) -> dict:
    owners = {}
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table, column in INCARNATION_OWNERS:
        if table not in tables:
            continue
        for inc, path in conn.execute(
                f'SELECT source_incarnation_id, "{column}" FROM "{table}" '
                "WHERE source_incarnation_id IS NOT NULL"):
            owners[inc] = f"incarnation-of:{path}"
    return owners


def _fts_integrity(path: pathlib.Path, names) -> dict:
    out = {}
    conn = sqlite3.connect(str(path))
    try:
        for name in names:
            try:
                conn.execute(f'INSERT INTO "{name}"("{name}") VALUES(\'integrity-check\')')
                out[name] = "ok"
            except sqlite3.Error as exc:
                out[name] = f"error: {exc}"
        conn.rollback()
    finally:
        conn.close()
    return out


def digest_store(path: pathlib.Path, *, fts_integrity: bool = False) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out = {}
    try:
        incarnations = _incarnation_paths(conn)
        schema = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchall()
        fts = [n for n, sql in schema
               if sql and sql.upper().startswith("CREATE VIRTUAL") and "FTS5" in sql.upper()]
        layout = {f + s for f in fts for s in FTS_LAYOUT_SUFFIXES}
        for name, sql in sorted(schema):
            if sql and sql.upper().startswith("CREATE VIRTUAL"):
                continue
            if name in layout:
                continue
            info = list(conn.execute(f'PRAGMA table_info("{name}")'))
            cols = [r[1] for r in info]
            keep = [c for c in cols if c not in WALL_CLOCK
                    and c not in RUN_COUNTER_COLUMNS and (name, c) not in NONCE]
            nonce_idx = [i for i, c in enumerate(keep) if c in CANONICAL_NONCE_COLUMNS]
            counter_cols = [c for c in cols if c in RUN_COUNTER_COLUMNS]
            ki = keep.index("key") if "key" in keep and "value" in keep else None
            vi = keep.index("value") if ki is not None else None
            pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5] and r[1] in keep]
            key_idx = [ki] if ki is not None else [keep.index(c) for c in pk]
            total = count = 0
            # A table carrying a per-transaction counter is keyed whatever its
            # size, so `compare --pre` can judge every row's advancement.
            track_all = bool(counter_cols)
            keyed = {} if key_idx else None
            counters = {}
            select = keep + counter_cols
            cur = conn.execute(
                f'SELECT {", ".join(chr(34) + c + chr(34) for c in select)} FROM "{name}"')
            for full in cur:
                row, extra = tuple(full[:len(keep)]), full[len(keep):]
                if nonce_idx:
                    row = tuple(incarnations.get(v, v) if i in nonce_idx else v
                                for i, v in enumerate(row))
                if ki is not None:
                    key = str(row[ki])
                    if any(m in key for m in WALL_CLOCK_KEY_MARKERS):
                        continue
                    if key in RUN_COUNTER_KEYS:
                        counters[key] = row[vi]
                        continue
                    row = tuple(_normalized_meta_value(v) if i == vi else v
                                for i, v in enumerate(row))
                h = _row_hash(row)
                total = (total + h) % MOD
                count += 1
                if keyed is not None and (track_all or len(keyed) <= KEYED_ROWS_MAX):
                    rkey = "|".join(str(row[i]) for i in key_idx)
                    keyed[rkey] = f"{h:032x}"
                    if extra:
                        counters[rkey] = list(extra)
            out[name] = {"rows": count, "hash": f"{total:032x}", "columns": keep}
            if keyed is not None and (track_all or len(keyed) <= KEYED_ROWS_MAX):
                out[name]["keys"] = keyed
            if counters and (ki is not None or track_all or len(counters) <= KEYED_ROWS_MAX):
                out[name]["counters"] = counters
        for name in sorted(fts):
            answers = {}
            for term in FTS_TERMS:
                try:
                    ids = [r[0] for r in conn.execute(
                        f'SELECT rowid FROM "{name}" WHERE "{name}" MATCH ? ORDER BY rowid',
                        (term,))]
                except sqlite3.Error as exc:
                    answers[term] = {"error": str(exc)}
                    continue
                h = hashlib.blake2b(repr(ids).encode(), digest_size=16).hexdigest()
                answers[term] = {"matches": len(ids), "hash": h}
            out[f"{name} (fts answers)"] = answers
    finally:
        conn.close()
    if fts_integrity and fts:
        out["(fts integrity-check)"] = _fts_integrity(path, sorted(fts))
    return out


def cmd_digest(args) -> int:
    data = pathlib.Path(args.data)
    report = {"data": str(data), "stores": {}}
    for name in args.db or ("conversations.db", "cache.db"):
        path = data / name
        if path.exists():
            report["stores"][name] = digest_store(path, fts_integrity=args.fts_integrity)
    pathlib.Path(args.out).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    tables = sum(len(v) for v in report["stores"].values())
    print(json.dumps({"stores": sorted(report["stores"]), "tables": tables}))
    return 0


INTEGRITY = "(fts integrity-check)"


def _integrity_problems(side: str, stores: dict, required: bool) -> int:
    """A non-`ok` integrity result is refused, never compared (two equal
    failures must not compare equal); a missing one is refused when required."""
    problems = 0
    for store in sorted(stores):
        res = stores[store].get(INTEGRITY)
        if res is None:
            if required:
                problems += 1
                print(f"{side} {store}: FTS integrity-check missing")
            continue
        for name, value in sorted(res.items()):
            if value != "ok":
                problems += 1
                print(f"{side} {store}:{name}: FTS integrity-check {value!r}")
    return problems


def cmd_compare(args) -> int:
    a = json.loads(pathlib.Path(args.a).read_text())["stores"]
    b = json.loads(pathlib.Path(args.b).read_text())["stores"]
    refused = (_integrity_problems("A", a, args.require_integrity)
               + _integrity_problems("B", b, args.require_integrity))
    differ = missing = 0
    for store in sorted(set(a) | set(b)):
        ta, tb = a.get(store, {}), b.get(store, {})
        for table in sorted((set(ta) | set(tb)) - {INTEGRITY}):
            if table not in ta or table not in tb:
                missing += 1
                print(f"{store}:{table}: only in {'A' if table in ta else 'B'}")
                continue
            strip = lambda t: {k: v for k, v in t.items() if k != "counters"}
            if strip(ta[table]) != strip(tb[table]):
                differ += 1
                ka, kb = ta[table].get("keys"), tb[table].get("keys")
                if isinstance(ka, dict) and isinstance(kb, dict):
                    keys = sorted(k for k in set(ka) | set(kb) if ka.get(k) != kb.get(k))
                    print(f"{store}:{table}: differs in keys {keys}")
                    continue
                print(f"{store}:{table}: differs\n  A {json.dumps(ta[table])[:300]}\n"
                      f"  B {json.dumps(tb[table])[:300]}")
    bad_counters = 0
    if args.pre:
        pre = json.loads(pathlib.Path(args.pre).read_text())["stores"]
        bad_counters = _compare_counters(pre, a, b)
    print(f"compare: {differ} differing, {missing} one-sided, "
          f"{bad_counters} counter problems, {refused} integrity refusals")
    if refused:
        return 3
    return 2 if missing else (1 if differ or bad_counters else 0)


def _as_int(value):
    """A counter compares as an integer whatever its storage class; a value
    that does not parse is an error, never a lexical comparison."""
    if isinstance(value, list):
        return [_as_int(v) for v in value]
    if isinstance(value, bool) or value is None:
        raise ValueError(f"not an integer counter: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ValueError(f"not an integer counter: {value!r}")


def _counter_moves(pre_c: dict, post_c: dict, post_keys) -> tuple:
    """(advanced, regressed, vanished, unparsable) key sets of one table."""
    advanced, regressed, vanished, bad = set(), set(), set(), set()
    for key, value in post_c.items():
        try:
            after = _as_int(value)
            before = None if key not in pre_c else _as_int(pre_c[key])
        except ValueError:
            bad.add(key)
            continue
        if before is None or after > before:
            advanced.add(key)
        elif after < before:
            regressed.add(key)
    for key in pre_c:
        # A row that still exists must still carry its counter.
        if key not in post_c and (post_keys is None or key in post_keys
                                   or key in RUN_COUNTER_KEYS):
            vanished.add(key)
    return advanced, regressed, vanished, bad


def _compare_counters(pre, a, b) -> int:
    """Counters against the source store, as integers: none regresses,
    vanishes or fails to parse; both clones advance the same set (a
    store-global counter advances in both or in neither); and every keyed row
    whose content changed from PRE advanced its counter. A global counter is
    not tied to "any content changed": a Claude-only change correctly leaves
    the Codex find-projection generation alone (#939 R3-06)."""
    problems = 0
    for store in sorted(set(a) & set(b)):
        pre_store = pre.get(store) or {}
        for table in sorted((set(a[store]) & set(b[store])) - {INTEGRITY}):
            ta, tb = a[store][table], b[store][table]
            if not isinstance(ta, dict) or not isinstance(tb, dict):
                continue
            ca, cb = ta.get("counters"), tb.get("counters")
            p = pre_store.get(table) or {}
            cp = p.get("counters") or {}
            if not isinstance(ca, dict) or not isinstance(cb, dict):
                if cp:
                    problems += 1
                    print(f"{store}:{table}: counters vanished (present in PRE)")
                continue
            moves = {}
            for side, t, c in (("A", ta, ca), ("B", tb, cb)):
                keys = t.get("keys")
                adv, reg, gone, bad = _counter_moves(cp, c, None if keys is None else set(keys))
                moves[side] = adv
                for label, ks in (("regressed", reg), ("vanished", gone), ("unparsable", bad)):
                    if ks:
                        problems += 1
                        print(f"{store}:{table}: {side} counters {label}: {sorted(ks)[:8]}")
                kp = p.get("keys") or {}
                if isinstance(keys, dict):
                    changed = {k for k, h in keys.items() if kp.get(k) != h}
                    stale = sorted(k for k in changed if k in c and k not in adv)
                    if stale:
                        problems += 1
                        print(f"{store}:{table}: {side} changed rows without advancing "
                              f"their counter: {stale[:8]}")
            if moves["A"] != moves["B"]:
                problems += 1
                print(f"{store}:{table}: counters advanced differently "
                      f"(only A {sorted(moves['A'] - moves['B'])[:8]}, "
                      f"only B {sorted(moves['B'] - moves['A'])[:8]})")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("digest")
    d.add_argument("--data", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--db", action="append")
    d.add_argument("--fts-integrity", action="store_true",
                   help="also run FTS5 integrity-check (writable connection; clones only)")
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--pre", help="digest of the store both clones came from")
    c.add_argument("--require-integrity", action="store_true",
                   help="refuse a digest without an FTS integrity-check result")
    args = ap.parse_args(argv)
    return cmd_digest(args) if args.cmd == "digest" else cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())

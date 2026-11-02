#!/usr/bin/env python3
"""Page-to-object map of one SQLite database, from `dbstat` (#939).

  pagemap.py --db PATH --out PREFIX [--immutable]

Writes PREFIX.json ({"db", "pageSize", "pageCount", "freelist", "names": [...],
"types": {name: table|index|...}, "tables": {name: tbl_name},
"pages": {name: n}}) and PREFIX.u16, an
array of unsigned 16-bit object ids indexed by page number (id 0 = a page no
b-tree owns: the freelist, a pointer-map page, or a page past the end). Every
page `dbstat` reports - b-tree interior, leaf and overflow pages - is charged
to the table or index that owns it, so the WAL frame log and the main-database
page log (`wtrace.c`) can be attributed per object (`catchup_attrib.py`).

Open read-only. `--immutable` is for a closed copy with an empty WAL that no
process writes (a store copy bound to a freeze: a plain read-only open would
create its sidecars); a measured clone is opened normally so the map includes
any frames still in its WAL.
"""

from __future__ import annotations

import argparse
import array
import json
import pathlib
import sqlite3
import sys


def build(db: str, immutable: bool) -> "tuple[dict, array.array]":
    uri = f"file:{db}?{'immutable=1' if immutable else 'mode=ro'}"
    conn = sqlite3.connect(uri, uri=True)
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        types, tables = {}, {}
        for kind, name, tbl in conn.execute(
                "SELECT type, name, tbl_name FROM sqlite_schema"):
            types[name], tables[name] = kind, tbl
        names = ["<unowned>"]
        ids: "dict[str, int]" = {}
        pages = array.array("H", bytes(2 * (page_count + 1)))
        counts: "dict[str, int]" = {}
        for name, pageno in conn.execute("SELECT name, pageno FROM dbstat"):
            oid = ids.get(name)
            if oid is None:
                oid = ids[name] = len(names)
                names.append(name)
                if oid > 0xFFFF:
                    raise SystemExit("more than 65535 objects")
            if 0 < pageno <= page_count:
                pages[pageno] = oid
            counts[name] = counts.get(name, 0) + 1
    finally:
        conn.close()
    meta = {"db": db, "pageSize": page_size, "pageCount": page_count,
            "freelist": freelist, "names": names, "types": types,
            "tables": tables, "pages": counts}
    return meta, pages


def load(prefix) -> "tuple[dict, array.array]":
    meta = json.loads(pathlib.Path(f"{prefix}.json").read_text())
    pages = array.array("H")
    pages.frombytes(pathlib.Path(f"{prefix}.u16").read_bytes())
    return meta, pages


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--immutable", action="store_true")
    args = ap.parse_args(argv)
    meta, pages = build(args.db, args.immutable)
    pathlib.Path(f"{args.out}.u16").write_bytes(pages.tobytes())
    pathlib.Path(f"{args.out}.json").write_text(json.dumps(meta, indent=1) + "\n")
    print(json.dumps({"pageCount": meta["pageCount"], "objects": len(meta["names"]) - 1,
                      "freelist": meta["freelist"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

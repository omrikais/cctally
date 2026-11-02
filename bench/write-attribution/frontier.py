#!/usr/bin/env python3
"""#939 D6: the startup frontier, its judgements, admission and verdicts.

  frontier.py capture --out FRONTIER.json [--tree TREE]
  frontier.py judge   --frontier FRONTIER.json --data DATA [--reference REF]
                      [--out RECEIPT.json] [--tree TREE]
  frontier.py admit-step --frontier F --data DATA --perf PERF.json
                      --state STATE.json --out ADMISSION.json [--tree TREE]
  frontier.py pass-after --perf PERF.json --receipt RECEIPT.json
  frontier.py wait-bound --perf PERF.json

`capture` walks the input roots the tested tree reads (`frozen_roots.
input_roots`: each Claude data directory's `projects/`, each Codex home's
`sessions/` tree and its flat `archived_sessions/`, `.jsonl` and `.jsonl.zst`)
and records one entry per LOGICAL rollout: Codex names are canonicalized with
the product's own functions (`_lib_codex_rollout_container`
`dated_logical_for_archive`, `logical_path`, `select_container`; twins and
duplicate roots collapse to one identity, first configured root first), and
every entry carries its selected container (kind, path, device, inode, size,
mtime), its generation token and its complete-record end: the offset after the
last newline, in decoded bytes for a compressed container, always computed
from the container's bytes. No store is read, so no cursor can certify itself.

`judge` reads conversations.db (`mode=ro`) with cache.db attached (`mode=ro`,
never `immutable=1`) inside one read transaction, and classifies each entry:
  consumed    both cursors (the conversation cursor and the accounting cursor)
              name the captured container and reach its complete-record end;
              or the container was replaced after capture, both cursors name
              the container the product's selection now picks, whose own
              complete-record end (from its bytes) is at least the captured
              one, and reach it. An entry with nothing to consume (end 0) is
              consumed.
  deferred    a Codex entry not consumed with positive evidence of a product
              deferral at the CAPTURED generation: a `codex_barrier_targets`
              row for its file identity naming that generation with a
              state/reason pair the barrier defines, or a pending-container
              mark on the captured container.
  unconsumed  anything else, every Claude entry not consumed included.
Each deferred entry carries its cursors, its evidence, the conversation keys
its membership names at the captured generation with their population
requirements, and the raw certificate and publication facts of those keys and
of the reference's keys. Its OBLIGATION STATE (`obligation_state`) is clipped
to the captured frontier: each cursor clipped to the captured complete-record
end within the captured container, the target's state and reason while it
names the captured generation, the pending mark, and per key named by the
reference (J1) its certificate revision and `published_revision` clipped to
the key's `required_revision` at J1. Input past the captured end is not part
of it.

`admit-step` is one poll of the L-post admission protocol (spec D6): J1 once
`coldStartup.releasedBy` is non-null and an `ok` pass has completed; then J2
after an `ok` pass that started after J1 ended; J2 must find no unconsumed
entry and every obligation state equal to J1's. Exit 0 admitted (writes
ADMISSION.json), 1 keep polling, 2 on an unusable input. `pass-after` exits 0
once a pass that started after RECEIPT ended has completed; `wait-bound`
prints the terminal wait bound (two pass intervals plus 120 s).
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import pathlib
import sqlite3
import sys
import time
from types import SimpleNamespace

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_TREE = HERE.parents[1]
FRONTIER_VERSION = 1
CHUNK = 1 << 20
#: Spec D6: admission polling and its deadline, and the terminal wait slack.
POLL_S = 15
ADMISSION_DEADLINE_S = 1800
TERMINAL_SLACK_S = 120
#: Without any published pass interval the terminal wait falls back to this.
FALLBACK_INTERVAL_S = 300

#: (provider) -> ((schema, cursor table, label), ...): the two cursors an
#: entry must have advanced to be consumed.
CURSORS = {
    "claude": (("main", "conversation_source_files", "conversation"),
               ("cache_db", "session_files", "accounting")),
    "codex": (("main", "codex_conversation_source_files", "conversation"),
              ("cache_db", "codex_session_files", "accounting")),
}
CURSOR_FIELDS = ("last_byte_offset", "size_bytes", "device_id", "inode",
                 "container_kind", "container_path", "container_size_bytes",
                 "container_state", "container_state_reason",
                 "pending_container_fingerprint")


# ── the tested tree's leaf libraries ──────────────────────────────────────

def load_libs(tree=None) -> SimpleNamespace:
    """The tree's container, identity and barrier leaves (stdlib-only). A
    module already imported (catchup.py loaded the tree) is reused."""
    bin_dir = str(pathlib.Path(tree or DEFAULT_TREE) / "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    rc = importlib.import_module("_lib_codex_rollout_container")
    ident = importlib.import_module("_lib_source_identity")
    barrier = importlib.import_module("_lib_codex_publication_barrier")
    return SimpleNamespace(rc=rc, ident=ident, barrier=barrier,
                           pairs=defined_pairs(barrier))


def defined_pairs(barrier) -> frozenset:
    """Every (state, reason) `classify_target` can produce."""
    pairs = {(barrier.READY, None)}
    pairs |= {(barrier.REQUIRED, r) for r in (
        barrier.PENDING_HOLD, "membership-unproven", "accounting-incomplete")}
    pairs |= {(barrier.SUSPENDED, h) for h in barrier.SUSPENSION_HOLDS}
    return frozenset(pairs)


def _frozen_roots():
    sys.path.insert(0, str(HERE))
    import frozen_roots
    return frozen_roots


def env_roots(env=None) -> list:
    env = os.environ if env is None else env
    return _frozen_roots().input_roots(
        env.get("HOME") or os.path.expanduser("~"), env.get("CODEX_HOME"),
        env.get("CLAUDE_CONFIG_DIR"))


# ── capture ───────────────────────────────────────────────────────────────

def complete_end(stream, limit: "int | None" = None) -> int:
    """The offset after the last newline within the first `limit` bytes."""
    total = last = 0
    while limit is None or total < limit:
        want = CHUNK if limit is None else min(CHUNK, limit - total)
        chunk = stream.read(want)
        if not chunk:
            break
        idx = chunk.rfind(b"\n")
        if idx >= 0:
            last = total + idx + 1
        total += len(chunk)
    return last


def _container_json(kind, path, st) -> dict:
    return {"kind": kind, "path": str(path), "device": int(st.st_dev),
            "inode": int(st.st_ino), "size": int(st.st_size),
            "mtimeNs": int(st.st_mtime_ns)}


def plain_facts(path) -> "dict | None":
    """A plain file's container and complete-record end, read up to the size
    it had when it was statted (growth during the read is not captured)."""
    try:
        st = os.stat(path)
        with open(path, "rb") as fh:
            end = complete_end(fh, int(st.st_size))
    except OSError:
        return None
    return {"container": _container_json("plain", path, st),
            "generation": f"plain:{int(st.st_ino)}", "completeEnd": end}


def codex_facts(selected, libs) -> dict:
    """The selected container's facts through the product's own reader: a
    plain container is read to its stat size; a compressed one is decoded to
    frame completion (`validate_full_stream`), whose decoded length also
    binds its generation token."""
    rc, fp = libs.rc, selected.fingerprint
    container = {"kind": fp.kind, "path": str(fp.path), "device": int(fp.device),
                 "inode": int(fp.inode), "size": int(fp.size),
                 "mtimeNs": int(fp.mtime_ns)}
    if fp.kind == rc.PLAIN:
        try:
            with open(fp.path, "rb") as fh:
                end = complete_end(fh, int(fp.size))
        except OSError as exc:
            return {"container": container, "generation": None,
                    "completeEnd": None, "error": f"read-error: {exc}"}
        return {"container": container,
                "generation": rc.generation_token(fp, None), "completeEnd": end}
    v = rc.validate_full_stream(selected)
    if not v.ok:
        return {"container": container, "generation": None,
                "completeEnd": None, "error": v.error}
    return {"container": container,
            "generation": rc.generation_token(fp, v.decoded_length),
            "completeEnd": int(v.complete_line_end)}


def _canonical(path) -> pathlib.Path:
    try:
        return pathlib.Path(path).resolve()
    except OSError:
        return pathlib.Path(path).absolute()


def codex_providers(homes, libs) -> list:
    """`_codex_provider_roots()`: per configured home in order, its canonical
    provider root, its locations and its source-root key; canonical
    duplicates collapse, and an archive another root walks is released."""
    rc = libs.rc
    out, seen = [], set()
    for configured in homes:
        provider = _canonical(configured)
        if provider in seen:
            continue
        locations = rc.home_locations(configured)
        if locations is None:
            continue
        seen.add(provider)
        out.append([provider, locations,
                    libs.ident.source_root_key(str(provider))])
    released = rc.release_walked_archives([p[1] for p in out])
    for provider, location in zip(out, released):
        provider[1] = location
    return out


def select_codex(logical, location, libs):
    """`_select_codex_container(logical, [root])`: the dated containers
    first, then the root's archive candidates for a dated rollout name."""
    rc = libs.rc
    selected = rc.select_container(logical)
    if selected is not None:
        return selected
    if rc.archive_name_date(pathlib.Path(str(logical)).name) is None:
        return None
    archive = rc.archive_dir_for(logical, (location,))
    return None if archive is None else rc.select_container(
        logical, archive_dir=archive)


def identity_path(selected, location, libs) -> pathlib.Path:
    """`_codex_identity_path`: an archived container's identity is its dated
    logical path under the canonical walk root, otherwise the logical path of
    the canonical container."""
    rc = libs.rc
    container = _canonical(selected.path)
    if location.archive_root is not None:
        canonical = (rc.CodexLocations(_canonical(location.walk_root),
                                       _canonical(location.archive_root)),)
        dated = rc.dated_logical_for_archive(container, canonical)
        if dated is not None:
            return dated
    return pathlib.Path(rc.logical_path(container))


def codex_logicals(location, libs, skipped: dict) -> set:
    """`_codex_root_logicals`: the walk root's recursive glob plus the flat
    archive mapped to dated logical paths (an unmappable name is skipped)."""
    rc = libs.rc
    logicals = {pathlib.Path(rc.logical_path(p))
                for pattern in ("**/*.jsonl", "**/*.jsonl.zst")
                for p in pathlib.Path(location.walk_root).glob(pattern)}
    if location.archive_root is not None:
        for pattern in ("*.jsonl", "*.jsonl.zst"):
            for p in pathlib.Path(location.archive_root).glob(pattern):
                logical = rc.dated_logical_for_archive(p, (location,))
                if logical is None:
                    skipped["archive-unmapped"] = skipped.get(
                        "archive-unmapped", 0) + 1
                    continue
                logicals.add(pathlib.Path(logical))
    return logicals


def capture(roots, libs) -> dict:
    """Spec D6 frontier capture over `frozen_roots.Root`s. Pure of stores."""
    entries, skipped = [], {}
    for root in roots:
        if root.kind != "claude-projects" or not root.walk:
            continue
        for path in sorted(pathlib.Path(root.walk).glob("**/*.jsonl"),
                           key=str):
            if not path.is_file():
                continue
            facts = plain_facts(path)
            if facts is None:
                skipped["claude-unreadable"] = skipped.get(
                    "claude-unreadable", 0) + 1
                continue
            entries.append({"provider": "claude", "logical": str(path),
                            **facts})
    homes = [r.logical for r in roots if r.kind == "codex-home"]
    seen = set()
    for provider, location, root_key in codex_providers(homes, libs):
        for logical in sorted(codex_logicals(location, libs, skipped), key=str):
            selected = select_codex(logical, location, libs)
            if selected is None:
                continue
            ipath = identity_path(selected, location, libs)
            if ipath in seen:
                continue
            seen.add(ipath)
            archive = (None if location.archive_root is None
                       else str(location.archive_root))
            entries.append({
                "provider": "codex", "logical": str(logical),
                "identityPath": str(ipath), "sourceRootKey": root_key,
                "fileIdentity": libs.ident.codex_file_key(root_key, str(ipath)),
                "walkRoot": str(location.walk_root), "archiveRoot": archive,
                "location": selected.location, **codex_facts(selected, libs)})
    return {"version": FRONTIER_VERSION,
            "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "frontier.py", "entries": entries, "skipped": skipped,
            "counts": {p: sum(1 for e in entries if e["provider"] == p)
                       for p in ("claude", "codex")}}


def frontier_problems(frontier) -> "list[str]":
    if not isinstance(frontier, dict) or frontier.get("version") != \
            FRONTIER_VERSION or not isinstance(frontier.get("entries"), list):
        return ["no frontier capture (frontier.py capture) of this version"]
    return []


# ── judgement ─────────────────────────────────────────────────────────────

def open_judgement(data_dir) -> sqlite3.Connection:
    """conversations.db as main and cache.db attached, both `mode=ro`."""
    data = pathlib.Path(data_dir)
    conn = sqlite3.connect((data / "conversations.db").resolve().as_uri()
                           + "?mode=ro", uri=True, isolation_level=None)
    conn.execute("ATTACH DATABASE ? AS cache_db",
                 ((data / "cache.db").resolve().as_uri() + "?mode=ro",))
    return conn


def _columns(conn, schema, table) -> set:
    try:
        return {r[1] for r in conn.execute(
            f'PRAGMA {schema}.table_info("{table}")')}
    except sqlite3.Error:
        return set()


class _Reader:
    """Column-aware lookups inside the judgement's one read transaction."""

    def __init__(self, conn):
        self.conn = conn
        self.cols = {}

    def _have(self, schema, table):
        key = (schema, table)
        if key not in self.cols:
            self.cols[key] = _columns(self.conn, schema, table)
        return self.cols[key]

    def row(self, schema, table, where, value, fields) -> "dict | None":
        have = self._have(schema, table)
        if where not in have:
            return None
        cols = [f for f in fields if f in have]
        if not cols:
            return None
        hit = self.conn.execute(
            f'SELECT {", ".join(cols)} FROM {schema}."{table}" '
            f'WHERE {where} = ?', (value,)).fetchone()
        return None if hit is None else dict(zip(cols, hit))

    def scalar(self, sql, args=()):
        try:
            hit = self.conn.execute(sql, args).fetchone()
        except sqlite3.Error:
            return None
        return None if hit is None else hit[0]

    def column(self, sql, args=()) -> list:
        try:
            return [r[0] for r in self.conn.execute(sql, args)]
        except sqlite3.Error:
            return []


def names_container(row, entry, container) -> "str | None":
    """How a cursor row names `container`: "identity" (its stored inode
    matches), "path" (it carries no inode: NULL is no evidence either way),
    or None (it names another container)."""
    if row is None:
        return None
    if entry["provider"] == "codex":
        kind = row.get("container_kind") or "plain"
        cpath = row.get("container_path") or entry["logical"]
        if kind != container["kind"] or cpath != container["path"]:
            return None
        size = row.get("container_size_bytes")
        if container["kind"] != "plain" and size is not None \
                and int(size) != container["size"]:
            return None
    # The product's rule (#814, `_lib_ingest_frontier.source_identity_replaced`):
    # the inode decides and the device only corroborates, because st_dev is
    # assigned at mount time and a remount renumbers every stored device_id
    # with no file changed. A missing or unreadable inode is no evidence.
    try:
        inode = None if row.get("inode") is None else int(row["inode"])
    except (TypeError, ValueError):
        inode = None
    if inode is None:
        return "path"
    if inode != container["inode"]:
        return None
    return "identity"


def _offset(row) -> int:
    return int((row or {}).get("last_byte_offset") or 0)


def current_container(entry, libs) -> "dict | None":
    """What the product's selection picks for the entry's logical path now,
    with its facts read from its bytes."""
    if entry["provider"] == "claude":
        return plain_facts(entry["logical"])
    rc = libs.rc
    archive = entry.get("archiveRoot")
    location = rc.CodexLocations(pathlib.Path(entry["walkRoot"]),
                                 None if archive is None
                                 else pathlib.Path(archive))
    selected = select_codex(entry["logical"], location, libs)
    return None if selected is None else codex_facts(selected, libs)


def _same_generation(a: dict, b: dict) -> bool:
    return (a.get("generation") is not None
            and a.get("generation") == b.get("generation")
            and a["container"]["kind"] == b["container"]["kind"]
            and a["container"]["path"] == b["container"]["path"])


def consumption(entry, cursors, libs) -> "tuple[bool, dict]":
    """(consumed, detail). Captured container first; a replacement only when
    the product now selects another generation, whose bytes reach at least
    the captured end, and every cursor names it and reaches its end."""
    end = entry.get("completeEnd")
    if end == 0:
        return True, {"rule": "empty"}
    if end is None:
        return False, {"rule": "unreadable", "error": entry.get("error")}
    naming = {label: names_container(row, entry, entry["container"])
              for label, row in cursors.items()}
    if all(naming.values()) and all(_offset(r) >= end for r in cursors.values()):
        return True, {"rule": "captured", "naming": naming}
    now = current_container(entry, libs)
    if now is None or now.get("completeEnd") is None or _same_generation(
            now, entry):
        return False, {"rule": "captured", "naming": naming}
    replaced = {label: names_container(row, entry, now["container"])
                for label, row in cursors.items()}
    if now["completeEnd"] >= end and all(replaced.values()) and all(
            _offset(r) >= now["completeEnd"] for r in cursors.values()):
        return True, {"rule": "replaced", "naming": replaced,
                      "replacement": now}
    return False, {"rule": "replaced", "naming": replaced, "replacement": now}


def fingerprint_json(container) -> str:
    return json.dumps([container["kind"], container["path"], container["size"],
                       container["mtimeNs"], container["device"],
                       container["inode"]], separators=(",", ":"))


#: Container states the product records on a container it has not accepted
#: (#968): `pending` awaits verification; `unverified` (for example #984's
#: `legacy-inequivalent`) is one the product will not advance on its own.
DEFERRING_CONTAINER_STATES = ("pending", "unverified")


def pending_mark(entry, cursors) -> "dict | None":
    """A pending or unverified container mark on the CAPTURED container, in
    either store: the product's own record that it has not accepted it."""
    captured = fingerprint_json(entry["container"])
    for label, row in cursors.items():
        state = (row or {}).get("container_state")
        if state not in DEFERRING_CONTAINER_STATES:
            continue
        fp = row.get("pending_container_fingerprint")
        if fp == captured or (fp in ("", None) and names_container(
                row, entry, entry["container"])):
            return {"store": label, "state": state,
                    "reason": row.get("container_state_reason"),
                    "fingerprint": fp}
    return None


def _key_facts(reader, keys) -> dict:
    out = {}
    for key in sorted(keys):
        out[key] = {
            "required": reader.scalar(
                "SELECT required_revision FROM cache_db.codex_barrier_population "
                "WHERE conversation_key = ?", (key,)),
            "certificate": reader.scalar(
                "SELECT MAX(revision) FROM cache_db.codex_barrier_certificates "
                "WHERE conversation_key = ?", (key,)),
            "published": reader.scalar(
                "SELECT published_revision FROM main.codex_conversation_publications"
                " WHERE conversation_key = ?", (key,)),
        }
    return out


def obligation_state(entry: dict, reference: "dict | None" = None) -> dict:
    """Spec D6: a deferred entry's progress toward the captured frontier,
    judged against the keys and requirements its reference (J1) named. Pure."""
    ref = reference or entry
    end = entry["completeEnd"]
    cursors = {}
    for label, cur in sorted((entry.get("cursor") or {}).items()):
        cursors[label] = (None if not cur.get("naming")
                          else min(int(cur.get("offset") or 0), end))
    target = entry.get("target")
    state = {"class": "deferred", "cursors": cursors,
             "target": (None if not target or not target.get("captured")
                        else [target.get("state"), target.get("reason")]),
             "pending": bool(entry.get("pending")), "keys": {}}
    facts = entry.get("facts") or {}
    for key in sorted(ref.get("keys") or []):
        required = (ref.get("required") or {}).get(key)
        fact = facts.get(key)
        if fact is None:
            state["keys"][key] = "unobserved"
        elif required is None:
            state["keys"][key] = {"populated": fact.get("required") is not None}
        else:
            state["keys"][key] = [min(int(fact.get("certificate") or 0), required),
                                  min(int(fact.get("published") or 0), required)]
    return state


def judge(frontier: dict, conn: sqlite3.Connection, libs, *,
          reference: "dict | None" = None) -> dict:
    """One judgement inside one read transaction (both stores touched at its
    start, so each snapshot is taken then). `reference` is J1: its keys are
    read again and every obligation state is clipped to its requirements."""
    problems = frontier_problems(frontier)
    if problems:
        raise ValueError(problems[0])
    started = time.time()
    ref_entries = {e["logical"]: e for e in (reference or {}).get("deferred") or []}
    reader = _Reader(conn)
    conn.execute("BEGIN")
    try:
        conn.execute("SELECT count(*) FROM main.sqlite_schema").fetchone()
        conn.execute("SELECT count(*) FROM cache_db.sqlite_schema").fetchone()
        deferred, unconsumed = [], []
        counts = {"consumed": 0, "deferred": 0, "unconsumed": 0,
                  "pathOnlyCursors": 0,
                  "byProvider": {p: {"consumed": 0, "deferred": 0,
                                     "unconsumed": 0} for p in CURSORS}}
        for entry in frontier["entries"]:
            provider = entry["provider"]
            cursors = {label: reader.row(schema, table, "path", entry["logical"],
                                         CURSOR_FIELDS)
                       for schema, table, label in CURSORS[provider]}
            consumed, detail = consumption(entry, cursors, libs)
            if consumed:
                klass = "consumed"
                if "path" in (detail.get("naming") or {}).values():
                    counts["pathOnlyCursors"] += 1
            else:
                record = _deferral(entry, cursors, reader, libs,
                                   ref_entries.get(entry["logical"]))
                record["consumption"] = detail
                if record.get("evidence"):
                    klass = "deferred"
                    record["obligation"] = obligation_state(
                        record, ref_entries.get(entry["logical"]))
                    deferred.append(record)
                else:
                    klass = "unconsumed"
                    unconsumed.append(record)
            counts[klass] += 1
            counts["byProvider"][provider][klass] += 1
    finally:
        conn.execute("COMMIT")
    return {"version": FRONTIER_VERSION, "complete": True,
            "startedAt": started, "endedAt": time.time(),
            "reference": None if reference is None else {
                "startedAt": reference.get("startedAt"),
                "endedAt": reference.get("endedAt")},
            "counts": counts, "deferred": deferred, "unconsumed": unconsumed}


def _deferral(entry, cursors, reader, libs, ref_entry) -> dict:
    record = {"provider": entry["provider"], "logical": entry["logical"],
              "fileIdentity": entry.get("fileIdentity"),
              "generation": entry.get("generation"),
              "completeEnd": entry.get("completeEnd"), "cursor": {}}
    for label, row in cursors.items():
        record["cursor"][label] = {
            "present": row is not None, "offset": _offset(row),
            "naming": names_container(row, entry, entry["container"]),
            "row": row}
    if entry["provider"] != "codex":
        record["evidence"] = None
        record["reason"] = "not consumed"
        return record
    evidence = {}
    target = reader.row(
        "cache_db", "codex_barrier_targets", "file_identity",
        entry.get("fileIdentity"),
        ("generation_token", "state", "reason", "scan_target",
         "complete_line_end", "accepted_generation_token",
         "accepted_complete_line_end"))
    if target is not None:
        captured = (entry.get("generation") is not None
                    and target.get("generation_token") == entry["generation"])
        pair = (target.get("state"), target.get("reason"))
        record["target"] = {**target, "captured": captured,
                            "defined": pair in libs.pairs}
        if captured and pair in libs.pairs:
            evidence["target"] = [pair[0], pair[1]]
    mark = pending_mark(entry, cursors)
    record["pending"] = mark
    if mark is not None:
        evidence["pending"] = mark
    keys = set()
    if entry.get("generation") is not None:
        keys = set(reader.column(
            "SELECT conversation_key FROM cache_db.codex_barrier_membership "
            "WHERE file_identity = ? AND generation_token = ?",
            (entry.get("fileIdentity"), entry["generation"])))
    record["keys"] = sorted(keys)
    record["facts"] = _key_facts(reader, keys | set(
        (ref_entry or {}).get("keys") or []))
    record["required"] = {k: record["facts"][k]["required"] for k in keys}
    record["evidence"] = evidence or None
    if not evidence:
        target_info = record.get("target")
        record["reason"] = (
            "no barrier target or container mark" if target_info is None else
            "stale barrier target (another generation)"
            if not target_info["captured"] else
            "undefined barrier state/reason pair")
    return record


# ── comparison, admission and verdicts (pure) ─────────────────────────────

def receipt_problems(receipt, label: str) -> "list[str]":
    if not isinstance(receipt, dict) or receipt.get("complete") is not True \
            or not isinstance(receipt.get("deferred"), list) \
            or not isinstance(receipt.get("unconsumed"), list) \
            or not isinstance(receipt.get("counts"), dict):
        return [f"{label}: missing or partial judgement receipt"]
    return []


def _state_in(receipt: dict, logical: str, reference: dict) -> dict:
    for entry in receipt.get("deferred") or []:
        if entry["logical"] == logical:
            ref = next((e for e in reference.get("deferred") or []
                        if e["logical"] == logical), entry)
            return obligation_state(entry, ref)
    if any(e["logical"] == logical for e in receipt.get("unconsumed") or []):
        return {"class": "unconsumed"}
    return {"class": "consumed"}


def obligation_changes(reference: dict, a: dict, b: dict) -> "list[str]":
    """Every deferred entry (of either receipt) whose obligation state differs
    between `a` and `b`, both clipped to `reference`'s keys."""
    logicals = sorted({e["logical"] for r in (a, b)
                       for e in r.get("deferred") or []})
    out = []
    for logical in logicals:
        sa, sb = (_state_in(a, logical, reference),
                  _state_in(b, logical, reference))
        if sa != sb:
            out.append(f"{logical}: obligation state changed "
                       f"({json.dumps(sa, sort_keys=True)[:200]} -> "
                       f"{json.dumps(sb, sort_keys=True)[:200]})")
    return out


def unconsumed_problems(receipt: dict, label: str) -> "list[str]":
    n = len(receipt.get("unconsumed") or [])
    if not n:
        return []
    first = receipt["unconsumed"][0]
    return [f"{label}: {n} unconsumed frontier entr{'y' if n == 1 else 'ies'} "
            f"(e.g. {first['logical']}: {first.get('reason')})"]


def pair_problems(j1: dict, j2: dict) -> "list[str]":
    """J2 against J1 (both receipts complete, no unconsumed entry, every
    obligation state unchanged)."""
    problems = receipt_problems(j1, "J1") + receipt_problems(j2, "J2")
    if problems:
        return problems
    return (unconsumed_problems(j1, "J1") + unconsumed_problems(j2, "J2")
            + obligation_changes(j1, j1, j2))


def window_problems(reference: dict, base: dict, jw, jt) -> dict:
    """Spec D6's verdict rule. `refused`: J_W missing, partial or with an
    unconsumed entry, or an obligation state that changed from `base` (J2) by
    J_W (catch-up work inside the window). `invalid`: J_T missing or partial,
    or any change first seen between J_W and J_T (no record proves it was
    made after the window)."""
    refused = (receipt_problems(reference, "J1") + receipt_problems(base, "J2")
               + receipt_problems(jw, "J_W"))
    if not refused:
        refused = (unconsumed_problems(jw, "J_W")
                   + [f"by J_W: {p}" for p in obligation_changes(
                       reference, base, jw)])
    invalid = receipt_problems(jt, "J_T")
    if not invalid and not receipt_problems(jw, "J_W"):
        invalid = [f"first seen at J_T: {p}" for p in obligation_changes(
            reference, jw, jt)]
    return {"refused": refused, "invalid": invalid}


def observe(payload) -> dict:
    """The admission facts of one `dashboard-perf --json` sample: the
    product's `releasedBy` and each conversation pass with its start and end
    placed on wall time by the response's own clock anchor."""
    diag = (payload or {}).get("diagnostic") if "diagnostic" in (payload or {}) \
        else payload
    diag = diag or {}
    tick = diag.get("tick") or {}
    cold = ((diag.get("writeIo") or {}).get("coldStartup") or {})
    clock = tick.get("conversation_sync_clock") or {}
    passes = []
    if "monotonic_ns" in clock and "unix_ns" in clock:
        offset = (int(clock["unix_ns"]) - int(clock["monotonic_ns"])) / 1e9
        for r in tick.get("conversation_sync") or []:
            passes.append({"seq": r.get("seq"), "status": r.get("status"),
                           "started": int(r["started_ns"]) / 1e9 + offset,
                           "ended": int(r["ended_ns"]) / 1e9 + offset,
                           "periodS": (None if r.get("period_ns") is None
                                       else int(r["period_ns"]) / 1e9)})
    return {"releasedBy": cold.get("releasedBy"), "held": cold.get("held"),
            "clock": bool(clock), "passes": passes}


def pass_after(observation: dict, t: float) -> "dict | None":
    """The first completed `ok` pass that started after `t`."""
    after = [p for p in observation["passes"]
             if p.get("status") == "ok" and p["started"] > t]
    return min(after, key=lambda p: p["started"]) if after else None


def terminal_wait_bound(observation: dict) -> float:
    """Two pass intervals plus 120 s (the longest published interval)."""
    periods = [p["periodS"] for p in observation["passes"] if p.get("periodS")]
    interval = max(periods) if periods else FALLBACK_INTERVAL_S
    return 2 * interval + TERMINAL_SLACK_S


def admission(state: dict, observation: dict, judge_fn) -> "tuple[dict, dict | None]":
    """One poll of the L-post admission protocol. `judge_fn(reference)`
    returns a receipt. Returns (state, admission record or None)."""
    state = dict(state or {})
    state.setdefault("refusals", [])
    if observation.get("releasedBy") is None:
        return state, None
    ok = [p for p in observation["passes"] if p.get("status") == "ok"]
    j1 = state.get("j1")
    if j1 is None:
        if not ok:
            return state, None
        receipt = judge_fn(None)
        problems = unconsumed_problems(receipt, "J1")
        if problems:
            state["refusals"].append(problems)
            return state, None
        state["j1"] = receipt
        state["s1"] = max(ok, key=lambda p: p["ended"])
        return state, None
    s2 = pass_after(observation, float(j1["endedAt"]))
    if s2 is None:
        return state, None
    j2 = judge_fn(j1)
    problems = pair_problems(j1, j2)
    if not problems:
        return state, {"valid": True, "kind": "drained",
                       "releasedBy": observation["releasedBy"],
                       "passes": [state["s1"], s2], "receipts": [j1, j2]}
    state["refusals"].append(problems)
    if unconsumed_problems(j2, "J2"):
        state["j1"] = state["s1"] = None
    else:
        state["j1"], state["s1"] = j2, s2
    return state, None


def admission_record_problems(record) -> "list[str]":
    """An L-post admission.json judged on its own content."""
    if not isinstance(record, dict) or not record.get("valid"):
        return ["no valid admission record"]
    problems = []
    if record.get("kind") != "drained":
        problems.append(f"admission kind {record.get('kind')!r} is not drained")
    if record.get("releasedBy") is None:
        problems.append("the product never released its admission hold "
                        "(coldStartup.releasedBy is null)")
    receipts = record.get("receipts") or []
    passes = record.get("passes") or []
    if len(receipts) != 2 or len(passes) != 2:
        return problems + ["admission does not name two passes and two receipts"]
    j1, j2 = receipts
    problems += pair_problems(j1, j2)
    if not receipt_problems(j1, "J1") and not (
            float(passes[1].get("started", 0)) > float(j1["endedAt"])):
        problems.append("the J2 pass started before J1 ended")
    return problems


def load_reference(path) -> dict:
    """A receipt from a receipt file, an admission record (J1) or a catch-up
    receipt (its stable pair's first receipt)."""
    data = json.loads(pathlib.Path(path).read_text())
    if "deferred" in data:
        return data
    if "receipts" in data:
        return data["receipts"][0]
    stability = data.get("stability") or {}
    if stability.get("receipts"):
        return stability["receipts"][0]
    raise ValueError(f"{path}: no judgement receipt")


def base_receipt(path) -> dict:
    """The second receipt of an admission record or a catch-up stable pair."""
    data = json.loads(pathlib.Path(path).read_text())
    if "receipts" in data:
        return data["receipts"][1]
    return (data.get("stability") or {})["receipts"][1]


# ── CLI ───────────────────────────────────────────────────────────────────

def _write(path, payload) -> None:
    pathlib.Path(path).write_text(json.dumps(payload, indent=1, sort_keys=True,
                                             default=str) + "\n")


def judge_dir(frontier, data, libs, reference=None) -> dict:
    conn = open_judgement(data)
    try:
        return judge(frontier, conn, libs, reference=reference)
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--out", required=True)
    c.add_argument("--tree")
    j = sub.add_parser("judge")
    j.add_argument("--frontier", required=True)
    j.add_argument("--data", required=True)
    j.add_argument("--reference")
    j.add_argument("--out")
    j.add_argument("--tree")
    a = sub.add_parser("admit-step")
    for name in ("--frontier", "--data", "--perf", "--state", "--out"):
        a.add_argument(name, required=True)
    a.add_argument("--tree")
    p = sub.add_parser("pass-after")
    p.add_argument("--perf", required=True)
    p.add_argument("--receipt", required=True)
    w = sub.add_parser("wait-bound")
    w.add_argument("--perf", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "pass-after":
        obs = observe(json.loads(pathlib.Path(args.perf).read_text()))
        receipt = json.loads(pathlib.Path(args.receipt).read_text())
        found = pass_after(obs, float(receipt["endedAt"]))
        print(json.dumps(found))
        return 0 if found else 1
    if args.cmd == "wait-bound":
        obs = observe(json.loads(pathlib.Path(args.perf).read_text()))
        print(int(terminal_wait_bound(obs)))
        return 0
    libs = load_libs(args.tree)
    if args.cmd == "capture":
        frontier = capture(env_roots(), libs)
        _write(args.out, frontier)
        print(json.dumps(frontier["counts"]))
        return 0
    frontier = json.loads(pathlib.Path(args.frontier).read_text())
    if args.cmd == "judge":
        reference = None if not args.reference else load_reference(args.reference)
        receipt = judge_dir(frontier, args.data, libs, reference)
        if args.out:
            _write(args.out, receipt)
        print(json.dumps(receipt["counts"]))
        return 0
    state_path = pathlib.Path(args.state)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    obs = observe(json.loads(pathlib.Path(args.perf).read_text()))
    state, record = admission(
        state, obs, lambda ref: judge_dir(frontier, args.data, libs, ref))
    _write(state_path, state)
    if record is None:
        return 1
    _write(args.out, record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

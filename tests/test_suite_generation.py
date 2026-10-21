"""The suite generation's claim, marker, terminal states, removal and sweep (#963 S1, spec 5.4-5.5).

Process facts are injected (FakeFacts), so PID reuse, a surviving group, an
unknown answer and a current reservation are each produced exactly; the
filesystem is real. The end-to-end behaviour through bin/cctally-test-all is in
tests/test_suite_generation_lifecycle.py.
"""
from __future__ import annotations

import ast
import fcntl
import io
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import _lib_test_generation as G
import _lib_test_temp as T

REPO = Path(__file__).resolve().parents[1]
OWNER, GROUP = 4242, 4243
DAY = G.RETENTION_SECONDS
RES = {"CCTALLY_RUNNER_RESERVATION_ROOT": "/r", "_CCTALLY_RUNNER_RESERVATION_TOKEN": "tok"}


class FakeFacts(G.Facts):
    """Answers keyed by pid, pgid and (root, token); anything unnamed is absent.
    The descendant scan sees ``tokens`` (None: the scanner is blind) and counts
    how often it ran."""

    def __init__(self, starts=None, groups=None, reservations=None, tokens=frozenset()):
        self.starts = dict(starts or {})
        self.groups = dict(groups or {})
        self.reservations = dict(reservations or {})
        self.tokens = tokens
        self.scans = 0
        self.starts.setdefault(os.getpid(), "the-sweeper")

    def descendant_tokens(self):
        self.scans += 1
        return None if self.tokens is None else set(self.tokens)

    def process_start(self, pid):
        return self.starts.get(pid, "")

    def group_alive(self, pgid):
        return self.groups.get(pgid, False)

    def reservation_current(self, root, token):
        return self.reservations.get((root, token), False)


def _claim(tmp_path, now=1000.0, environ=None, start="start-a"):
    return Path(G.claim(str(tmp_path), OWNER, facts=FakeFacts({OWNER: start}),
                        now=now, environ=environ or {}))


def _sweep(tmp_path, facts, now, *, reclaim=True, keep=None, uid=None):
    out = io.StringIO()
    G.sweep(str(tmp_path), keep=keep, reclaim=reclaim, facts=facts, now=now,
            out=out, uid=uid)
    return out.getvalue()


def _marker(gen):
    return json.loads((Path(gen) / G.MARKER).read_text(encoding="utf-8"))


def _tok(gen):
    return Path(gen).name[len("ctg-"):]


def _kept(gen, reason):
    return "cctally-test-all: kept suite generation %s: %s" % (_tok(gen), reason)


def test_a_claim_binds_its_marker_to_the_directory(tmp_path):
    gen = _claim(tmp_path, environ=RES)
    assert G.NAME_RE.match(gen.name) and gen.parent == tmp_path
    st = gen.lstat()
    assert stat.S_IMODE(st.st_mode) == 0o700
    assert (gen / "t").is_dir(), "the private PYTEST_DEBUG_TEMPROOT was not created"
    doc = _marker(gen)
    assert doc["token"] == _tok(gen) and doc["uid"] == os.geteuid()
    assert (doc["dev"], doc["ino"]) == (st.st_dev, st.st_ino)
    assert (doc["ownerPid"], doc["ownerStart"], doc["state"]) == (OWNER, "start-a", "claimed")
    assert doc["reservation"] == {"root": "/r", "token": "tok"}
    assert doc["groups"] == [] and doc["groupStarts"] == {}
    assert doc["terminalAt"] is None and doc["ownershipEndedAt"] is None
    assert _marker(_claim(tmp_path))["reservation"] is None
    # A dedicated token, never the directory name a command line could carry.
    assert re.match(r"^cctg\.[0-9a-f]{32}$", doc["descendantToken"]), doc["descendantToken"]
    assert _tok(gen) not in doc["descendantToken"]
    assert _marker(_claim(tmp_path))["descendantToken"] != doc["descendantToken"]


def test_a_live_owner_keeps_its_generation(tmp_path):
    gen = _claim(tmp_path)
    out = _sweep(tmp_path, FakeFacts({OWNER: "start-a"}), now=1000.0 + 5 * DAY)
    assert _kept(gen, "its owner is still running") in out, out
    assert gen.is_dir() and _marker(gen)["ownershipEndedAt"] is None


def test_pid_reuse_is_recorded_then_reclaimed_after_the_retention(tmp_path):
    """Counterexample (spec 5.5): a sweep that trusts a bare PID."""
    gen = _claim(tmp_path)
    reused = FakeFacts({OWNER: "start-b"})
    assert _sweep(tmp_path, reused, now=5000.0, reclaim=False) == "", (
        "the startup inspection printed; only the exit sweep reports")
    assert _marker(gen)["ownershipEndedAt"] == 5000.0, (
        "the sweep did not record when it first saw the owner's PID reused")
    out = _sweep(tmp_path, reused, now=5000.0 + DAY - 1)
    assert _kept(gen, "ownership ended at 1970-01-01T01:23:20Z; "
                      "retained until 1970-01-02T01:23:20Z") in out, out
    out = _sweep(tmp_path, reused, now=5000.0 + DAY + 1)
    assert ("cctally-test-all: reclaimed suite generation %s "
            "(ended 1970-01-01T01:23:20Z)" % _tok(gen)) in out, out
    assert not gen.exists()


def test_a_surviving_group_keeps_the_generation(tmp_path):
    gen = _claim(tmp_path)
    G.add_group(str(gen), GROUP, facts=FakeFacts({GROUP: "g-start"}))
    assert _marker(gen)["groups"] == [GROUP]
    assert _marker(gen)["groupStarts"] == {str(GROUP): "g-start"}
    # The owner and the group's leader are gone; a member of the group lives.
    survivors = FakeFacts({OWNER: "", GROUP: ""}, groups={GROUP: True})
    out = _sweep(tmp_path, survivors, now=1000.0 + 5 * DAY)
    assert _kept(gen, "a recorded process group survives") in out, out
    assert gen.is_dir() and _marker(gen)["ownershipEndedAt"] is None


def test_a_reused_group_number_does_not_keep_the_generation(tmp_path):
    gen = _claim(tmp_path)
    G.add_group(str(gen), GROUP, facts=FakeFacts({GROUP: "g-start"}))
    reused = FakeFacts({OWNER: "", GROUP: "someone-else"}, groups={GROUP: True})
    out = _sweep(tmp_path, reused, now=5000.0)
    assert _kept(gen, "ownership ended at 1970-01-01T01:23:20Z; "
                      "retained until 1970-01-02T01:23:20Z") in out, out


@pytest.mark.parametrize("facts,reason", [
    (dict(starts={OWNER: None}), "its owner's state is unknown"),
    (dict(starts={OWNER: "", GROUP: ""}, groups={GROUP: None}),
     "a recorded process group's state is unknown"),
    (dict(starts={OWNER: "", GROUP: None}),
     "a recorded process group's state is unknown"),
    (dict(starts={OWNER: ""}, reservations={("/r", "tok"): None}),
     "its runner reservation's state is unknown"),
    (dict(starts={OWNER: ""}, reservations={("/r", "tok"): True}),
     "its runner reservation is still current"),
], ids=["owner-unknown", "group-unknown", "leader-unknown", "reservation-unknown",
        "reservation-current"])
def test_unknown_or_current_evidence_preserves_the_generation(tmp_path, facts, reason):
    gen = _claim(tmp_path, environ=RES)
    G.add_group(str(gen), GROUP, facts=FakeFacts({GROUP: "g-start"}))
    out = _sweep(tmp_path, FakeFacts(**facts), now=1000.0 + 10 * DAY)
    assert _kept(gen, reason) in out, out
    assert gen.is_dir() and _marker(gen)["ownershipEndedAt"] is None


def test_foreign_legacy_and_unmatched_roots_are_never_touched(tmp_path):
    for name in ("pytest-0", "garbage-1", "pytest-of-someone", "ctg-ZZZZZZZZ", "ctg-123"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "payload").write_text("keep", encoding="utf-8")
    (tmp_path / "ctg-0000000a").mkdir()                       # no marker
    (tmp_path / "ctg-0000000b").mkdir()
    (tmp_path / "ctg-0000000b" / G.MARKER).write_text("{", encoding="utf-8")
    (tmp_path / "ctg-0000000c").mkdir()                       # a marker that is a link
    (tmp_path / "decoy.json").write_text("{}", encoding="utf-8")
    (tmp_path / "ctg-0000000c" / G.MARKER).symlink_to(tmp_path / "decoy.json")
    moved = _claim(tmp_path)
    os.rename(moved, tmp_path / "ctg-0000000d")              # marker names another directory
    control = _claim(tmp_path)
    G.finish(str(control), "failed", now=1000.0)             # its 24 hours have run out
    before = sorted(p.name for p in tmp_path.iterdir())
    out = _sweep(tmp_path, FakeFacts({OWNER: ""}), now=1000.0 + 2 * DAY)
    assert "reclaimed suite generation %s" % _tok(control) in out, out
    assert sorted(p.name for p in tmp_path.iterdir()) == [n for n in before if n != control.name]
    for name, reason in (("0000000a", "no ownership marker"),
                         ("0000000b", "unreadable ownership marker"),
                         ("0000000c", "unreadable ownership marker"),
                         ("0000000d", "the ownership marker does not match the directory")):
        assert "kept suite generation %s: %s" % (name, reason) in out, out
    for name in ("ZZZZZZZZ", "ctg-123", "pytest-0", "garbage-1", "pytest-of-someone"):
        assert name not in out, out
    for name in ("pytest-0", "garbage-1", "pytest-of-someone", "ctg-ZZZZZZZZ", "ctg-123"):
        assert (tmp_path / name / "payload").read_text(encoding="utf-8") == "keep"


def test_another_users_generation_is_never_touched(tmp_path):
    gen = _claim(tmp_path)
    out = _sweep(tmp_path, FakeFacts({OWNER: ""}), now=1000.0 + 2 * DAY,
                 uid=os.geteuid() + 1)
    assert _kept(gen, "another user's generation") in out, out
    assert gen.is_dir()


@pytest.mark.parametrize("state", ["failed", "interrupted"])
def test_a_terminal_state_releases_metadata_and_starts_the_clock(tmp_path, state):
    gen = _claim(tmp_path)
    sealed = gen / "m" / "sealed"
    sealed.mkdir(parents=True)
    (sealed / "f").write_text("x", encoding="utf-8")
    sealed.chmod(0o500)
    res = G.finish(str(gen), state, now=2000.0)
    assert not res.refusals, res.findings
    assert T.inspect(str(gen)).findings == [], "the terminal state left obstructive metadata"
    doc = _marker(gen)
    assert (doc["state"], doc["terminalAt"]) == (state, 2000.0)
    dead = FakeFacts({OWNER: ""})
    out = _sweep(tmp_path, dead, now=2000.0 + DAY - 1)
    assert _kept(gen, "%s at 1970-01-01T00:33:20Z; retained until "
                      "1970-01-02T00:33:20Z" % state) in out, out
    out = _sweep(tmp_path, dead, now=2000.0 + DAY + 1)
    assert ("cctally-test-all: reclaimed suite generation %s "
            "(ended 1970-01-01T00:33:20Z)" % _tok(gen)) in out, out
    assert not gen.exists()


def test_removal_never_follows_links_or_hard_links(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("outside", encoding="utf-8")
    keep.chmod(0o444)
    (outside / "dir").mkdir()
    (outside / "dir" / "inner").write_text("inner", encoding="utf-8")
    owned = _claim(tmp_path)
    (owned / "m").mkdir()
    (owned / "m" / "to-dir").symlink_to(outside / "dir")
    os.link(keep, owned / "m" / "alias.txt")
    assert G.remove_generation(str(owned)) is True
    assert not owned.exists()
    assert keep.read_text(encoding="utf-8") == "outside"
    assert stat.S_IMODE(keep.stat().st_mode) == 0o444 and keep.stat().st_nlink == 1
    assert (outside / "dir" / "inner").read_text(encoding="utf-8") == "inner"


class _RefuseUnlink(T.RealOps):
    def __init__(self, name):
        self.name = name

    def unlink(self, path):
        if os.path.basename(path) == self.name:
            raise PermissionError(1, "Operation not permitted", path)
        os.unlink(path)


def test_a_failed_removal_keeps_the_generation_as_failed(tmp_path):
    gen = _claim(tmp_path)
    (gen / "m").mkdir()
    (gen / "m" / "stuck").write_text("x", encoding="utf-8")
    out = io.StringIO()
    assert G.remove_generation(str(gen), ops=_RefuseUnlink("stuck"), now=3000.0, out=out) is False
    assert gen.is_dir() and (gen / "m" / "stuck").exists()
    doc = _marker(gen)
    assert (doc["state"], doc["terminalAt"]) == ("failed", 3000.0), doc
    assert "repair refused" in out.getvalue(), out.getvalue()


class _Mounted(T.RealOps):
    """A filesystem mounted at ``mount``: it and everything beneath it report
    another device, the fact a test cannot produce without a real mount."""

    def __init__(self, mount):
        self.mount = str(mount)

    def lstat(self, path):
        st = T.RealOps.lstat(self, path)
        p = os.fspath(path)
        if p != self.mount and not p.startswith(self.mount + os.sep):
            return st
        fields = dict((k, getattr(st, k)) for k in dir(st) if k.startswith("st_"))
        fields["st_dev"] = st.st_dev + 1
        return types.SimpleNamespace(**fields)


def test_removal_never_enters_a_filesystem_mounted_in_the_generation(tmp_path):
    """F-6: each child's kernel removal took the child's own device as its
    reference, so a filesystem mounted directly at <generation>/<child> was
    walked and emptied. The generation's device is the reference."""
    gen = _claim(tmp_path)
    mnt = gen / "mnt"
    (mnt / "d").mkdir(parents=True)
    (mnt / "d" / "inner").write_text("mounted", encoding="utf-8")
    (gen / "m").mkdir()
    (gen / "m" / "plain").write_text("x", encoding="utf-8")
    out = io.StringIO()
    assert G.remove_generation(str(gen), ops=_Mounted(mnt), now=4000.0, out=out) is False
    assert (mnt / "d" / "inner").read_text(encoding="utf-8") == "mounted", (
        "the removal entered the mounted filesystem")
    assert "device crossing: %s" % mnt in out.getvalue(), out.getvalue()
    assert not (gen / "m").exists(), "the generation's own entries were not removed"
    doc = _marker(gen)
    assert (doc["state"], doc["terminalAt"]) == ("failed", 4000.0), doc


def test_a_dead_reclaimer_does_not_restart_a_terminal_generations_clock(tmp_path):
    """F-9: a reclaimer that died left the marker ``reclaiming``. A generation
    that had reached a terminal state keeps that clock; stamping a fresh
    ownership end made an already eligible generation wait another 24 hours."""
    gen = _claim(tmp_path)
    G.finish(str(gen), "failed", now=2000.0)
    doc = _marker(gen)
    doc["state"] = "reclaiming"
    doc["reclaimer"] = {"pid": 777, "start": "r-start"}
    (gen / G.MARKER).write_text(json.dumps(doc), encoding="utf-8")
    dead = FakeFacts({OWNER: "", 777: ""})
    out = _sweep(tmp_path, dead, now=2000.0 + DAY - 1)
    assert _kept(gen, "reclamation interrupted, terminal at 1970-01-01T00:33:20Z; "
                      "retained until 1970-01-02T00:33:20Z") in out, out
    assert _marker(gen)["ownershipEndedAt"] is None
    out = _sweep(tmp_path, dead, now=2000.0 + DAY + 60)
    assert ("cctally-test-all: reclaimed suite generation %s "
            "(ended 1970-01-01T00:33:20Z)" % _tok(gen)) in out, out
    assert not gen.exists()


@pytest.mark.parametrize("damage", ["unreadable", "mismatched"])
def test_an_unprovable_generation_is_never_deleted(tmp_path, damage):
    """R32: the deliberate fail-safe. A generation whose ownership marker is
    unreadable or names another directory cannot be proved to be ours, so
    neither the success path nor any later sweep deletes it, however old."""
    gen = _claim(tmp_path)
    (gen / "m").mkdir()
    (gen / "m" / "payload").write_text("keep", encoding="utf-8")
    if damage == "unreadable":
        (gen / G.MARKER).write_text("{", encoding="utf-8")
        reason = "unreadable ownership marker"
    else:
        doc = _marker(gen)
        doc["ino"] += 1
        (gen / G.MARKER).write_text(json.dumps(doc), encoding="utf-8")
        reason = "the ownership marker does not match the directory"
    out = io.StringIO()
    assert G.remove_generation(str(gen), out=out) is False
    assert reason in out.getvalue(), out.getvalue()
    for days in (2, 30, 365):
        swept = _sweep(tmp_path, FakeFacts({OWNER: ""}), now=1000.0 + days * DAY)
        assert _kept(gen, reason) in swept, swept
    assert (gen / "m" / "payload").read_text(encoding="utf-8") == "keep"


def test_reap_signals_only_recorded_groups_that_are_still_ours(tmp_path):
    gen = _claim(tmp_path)
    for pgid, start in ((4243, "s3"), (4244, "s4"), (4245, "s5"), (4246, "s6")):
        G.add_group(str(gen), pgid, facts=FakeFacts({pgid: start}))
    facts = FakeFacts({OWNER: "start-a", 4243: "s3", 4244: "reused", 4245: "", 4246: None},
                      groups={4243: True, 4244: True, 4245: True, 4246: True})
    sent = []
    left = G.reap_groups(str(gen), facts=facts, kill=lambda g, s: sent.append((g, s)),
                         grace=0, settle=0)
    assert sent == [(4243, signal.SIGTERM), (4245, signal.SIGTERM),
                    (4243, signal.SIGKILL), (4245, signal.SIGKILL)], sent
    # 4244 is someone else's now; 4246's state is unknown, so it is never
    # signalled but is reported, like the two that outlive SIGKILL here.
    assert [pg for pg, _why in left] == [4243, 4245, 4246], left


class _Dying(FakeFacts):
    """A recorded group whose leader lives until SIGKILL reaches it."""

    def kill(self, pgid, sig):
        self.sent.append((pgid, sig))
        if sig == signal.SIGKILL and pgid in self.mortal:
            self.starts[pgid] = ""
            self.groups[pgid] = False


@pytest.mark.parametrize("shape,starts,groups,expected", [
    # (a) survives SIGKILL: reported, so the generation must not be removed.
    ("survivor", {GROUP: "g-start"}, {GROUP: True},
     [(GROUP, "it survives SIGKILL")]),
    # (b) unknown: never signalled (its number may name someone else), reported.
    ("unknown", {GROUP: None}, {GROUP: True},
     [(GROUP, "its state is unknown")]),
    # (c) dies after SIGKILL: established gone, not reported.
    ("dies", {GROUP: "g-start"}, {GROUP: True}, []),
], ids=["survivor", "unknown", "dies"])
def test_reap_reports_every_group_it_cannot_establish_as_terminated(
        tmp_path, shape, starts, groups, expected):
    """963-CD-G2 (spec 5.4): stop and reap children, THEN remove. The reaper
    answers which recorded groups are not established gone; only an empty
    answer lets the success path remove the generation."""
    gen = _claim(tmp_path)
    G.add_group(str(gen), GROUP, facts=FakeFacts({GROUP: "g-start"}))
    facts = _Dying({**starts, OWNER: "start-a"}, groups=groups)
    facts.sent, facts.mortal = [], {GROUP} if shape == "dies" else set()
    left = G.reap_groups(str(gen), facts=facts, kill=facts.kill, grace=0, settle=0.3)
    assert left == expected, left
    if shape == "unknown":
        assert facts.sent == [], "signalled a group whose state is unknown"
    else:
        assert facts.sent == [(GROUP, signal.SIGTERM), (GROUP, signal.SIGKILL)], facts.sent


def test_reap_reports_a_group_it_cannot_signal(tmp_path):
    gen = _claim(tmp_path)
    G.add_group(str(gen), GROUP, facts=FakeFacts({GROUP: "g-start"}))
    facts = FakeFacts({OWNER: "start-a", GROUP: "g-start"}, groups={GROUP: True})

    def refuse(pgid, sig):
        raise PermissionError(1, "Operation not permitted")

    left = G.reap_groups(str(gen), facts=facts, kill=refuse, grace=0, settle=0)
    assert [pg for pg, _why in left] == [GROUP], left
    assert "could not be signalled" in left[0][1], left


@pytest.mark.parametrize("alive", [True, False], ids=["survivor", "gone"])
def test_the_reap_cli_fails_while_a_group_is_not_established_gone(
        tmp_path, monkeypatch, capsys, alive):
    gen = _claim(tmp_path)
    G.add_group(str(gen), GROUP, facts=FakeFacts({GROUP: "g-start"}))
    facts = FakeFacts({OWNER: "start-a", GROUP: "g-start" if alive else ""},
                      groups={GROUP: alive})
    sent = []
    monkeypatch.setattr(G, "Facts", lambda: facts)
    monkeypatch.setattr(G.os, "killpg", lambda pg, sig: sent.append((pg, sig)))
    monkeypatch.setattr(G, "REAP_GRACE_SECONDS", 0)
    monkeypatch.setattr(G, "REAP_SETTLE_SECONDS", 0)
    rc = G.main(["reap", "--dir", str(gen)])
    err = capsys.readouterr().err
    if alive:
        assert rc == 1, err
        assert ("cctally-test-all: suite generation %s: process group %d was not "
                "established as terminated: it survives SIGKILL" % (_tok(gen), GROUP)) in err, err
    else:
        assert rc == 0 and err == "" and sent == [], (rc, err, sent)


@pytest.mark.parametrize("operation", ["claim", "sweep"])
def test_claims_and_sweeps_are_serialized(tmp_path, operation):
    run = {"claim": lambda: _claim(tmp_path),
           "sweep": lambda: _sweep(tmp_path, FakeFacts(), now=1000.0)}[operation]
    done = []
    fd = os.open(str(tmp_path), os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    worker = threading.Thread(target=lambda: done.append(run()), daemon=True)
    try:
        worker.start()
        # timing-budget: the short wait IS the claim - the worker must still be blocked on the lock after it; a longer wait only slows the test
        worker.join(1.0)
        assert worker.is_alive() and not done, (
            "a %s proceeded while another holder had the namespace lock" % operation)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    worker.join(10)
    assert done and not worker.is_alive()


def test_the_cli_claims_sweeps_removes_and_rejects_usage(tmp_path):
    lib = REPO / "bin" / "_lib_test_generation.py"

    def run(*args):
        return subprocess.run([sys.executable, "-B", str(lib), *args],
                              capture_output=True, text=True, timeout=60)

    claimed = run("claim", "--tmpdir", str(tmp_path), "--owner-pid", str(os.getpid()))
    assert claimed.returncode == 0, claimed.stderr
    # The directory, then the descendant token bin/cctally-test-all exports.
    path, descendant = claimed.stdout.splitlines()
    gen = Path(path)
    assert gen.parent == tmp_path and G.NAME_RE.match(gen.name)
    assert _marker(gen)["ownerPid"] == os.getpid()
    assert descendant == _marker(gen)["descendantToken"], claimed.stdout
    assert run("add-group", "--dir", str(gen), "--pgid", str(os.getpgrp())).returncode == 0
    assert _marker(gen)["groups"] == [os.getpgrp()]
    swept = run("sweep", "--tmpdir", str(tmp_path), "--reclaim")
    assert swept.returncode == 0, swept.stderr
    assert _kept(gen, "its owner is still running") in swept.stderr, swept.stderr
    assert run("remove", "--dir", str(gen)).returncode == 0 and not gen.exists()
    assert run("bogus").returncode == 2
    assert run("finish", "--dir", str(gen), "--state", "done").returncode == 2


# --- descendants outside the recorded groups (963-CD-G3, esc-db01a377fca6) --
_ANCESTOR = "cctg." + "a" * 32
_STRANGER = "cctg." + "b" * 32


def _retained_past_its_clock(tmp_path, environ=None):
    """A failed generation whose 24 hours have run out by ``1000 + 2 * DAY``."""
    gen = _claim(tmp_path, environ=environ)
    G.finish(str(gen), "failed", now=1000.0)
    return gen


@pytest.mark.parametrize("blind,reason", [
    (True, "descendant environments cannot be inspected"),
    (False, "a descendant carrying its token survives"),
], ids=["scanner-blind", "token-visible"])
def test_a_blind_scan_or_a_visible_token_keeps_the_generation(tmp_path, blind, reason):
    gen = _retained_past_its_clock(tmp_path)
    seen = None if blind else {_ANCESTOR, _marker(gen)["descendantToken"]}
    out = _sweep(tmp_path, FakeFacts({OWNER: ""}, tokens=seen), now=1000.0 + 2 * DAY)
    assert _kept(gen, reason) in out, out
    assert gen.is_dir()


def test_a_scan_without_the_token_falls_through_to_the_other_legs(tmp_path):
    reserved = _retained_past_its_clock(tmp_path, environ=RES)
    ended = _retained_past_its_clock(tmp_path)
    facts = FakeFacts({OWNER: ""}, reservations={("/r", "tok"): True},
                      tokens={_ANCESTOR, _STRANGER})
    out = _sweep(tmp_path, facts, now=1000.0 + 2 * DAY)
    assert _kept(reserved, "its runner reservation is still current") in out, out
    assert "reclaimed suite generation %s" % _tok(ended) in out, out
    assert reserved.is_dir() and not ended.exists()
    assert facts.scans == 1, "the descendant scan ran %d times in one sweep" % facts.scans


def test_a_marker_without_a_descendant_token_skips_the_scan(tmp_path):
    gen = _retained_past_its_clock(tmp_path)
    doc = _marker(gen)
    del doc["descendantToken"]
    (gen / G.MARKER).write_text(json.dumps(doc), encoding="utf-8")
    facts = FakeFacts({OWNER: ""}, tokens=None)
    out = _sweep(tmp_path, facts, now=1000.0 + 2 * DAY)
    assert "reclaimed suite generation %s" % _tok(gen) in out, out
    assert facts.scans == 0


def test_a_ps_listing_counts_only_when_it_shows_the_scanners_own_probe():
    probe = "f" * 32
    listing = ("    1 /sbin/launchd\n"
               "   42 /opt/venv/bin/python3 x.py PATH=/bin CCTALLY_SUITE_GENERATIONS=%s %s\n"
               "   43 ps -E -ww -A CCTALLY_SUITE_ENV_PROBE=%s\n" % (_ANCESTOR, _STRANGER, probe))
    assert G._tokens_in_listing(listing, probe) == {_ANCESTOR, _STRANGER}
    # The scanner could not read even its own environment: blind, not empty.
    assert G._tokens_in_listing(listing.replace(probe, "0" * 32), probe) is None
    # A path naming a generation directory never carries a token.
    assert G._tokens_in_listing("1 python /tmp/ctg-12345678/m CCTALLY_SUITE_ENV_PROBE=%s"
                                % probe, probe) == set()


def test_the_proc_walk_reads_this_users_environments_and_needs_its_probe(tmp_path):
    probe = "e" * 32
    proc = tmp_path / "proc"
    for pid, env in (("10", b"PATH=/bin\0CCTALLY_SUITE_GENERATIONS=" + _ANCESTOR.encode() + b"\0"),
                     ("11", None),                       # it vanished mid-walk
                     ("12", b"CCTALLY_SUITE_ENV_PROBE=" + probe.encode() + b"\0"),
                     ("self", b"X=" + _STRANGER.encode())):  # not a process number
        (proc / pid).mkdir(parents=True)
        if env is not None:
            (proc / pid / "environ").write_bytes(env)
    uid = os.geteuid()
    assert G._proc_tokens(str(proc), uid, 12, probe) == {_ANCESTOR}
    assert G._proc_tokens(str(proc), uid + 1, 12, probe) == set(), "another user's process counted"
    assert G._proc_tokens(str(proc), uid, 12, "d" * 32) is None
    assert G._proc_tokens(str(proc), uid, 11, probe) is None
    assert G._proc_tokens(str(tmp_path / "absent"), uid, 12, probe) is None


class _Table(FakeFacts):
    """A descendant table ``{pid: (ppid, tokens)}``; None: the scanner is blind."""

    def __init__(self, table, **kwargs):
        super().__init__(**kwargs)
        self.table = table

    def descendant_table(self):
        self.scans += 1
        return None if self.table is None else dict(self.table)


def test_settling_counts_only_holders_outside_the_caller_and_its_ancestors(tmp_path):
    # Test regime reset fix round (B2): the exiting run removes its generation
    # only when no other process carries its token. The caller and its
    # ancestors carry it themselves and never count.
    gen = _claim(tmp_path)
    token = _marker(gen)["descendantToken"]
    me = os.getpid()
    table = {me: (900, {token}), 900: (901, {token}), 901: (1, set()), 1: (0, set()),
             77: (1, {_ANCESTOR, token}), 78: (1, {_ANCESTOR})}
    assert G.surviving_descendants(str(gen), facts=_Table(table)) == [77]
    del table[77]
    assert G.surviving_descendants(str(gen), facts=_Table(table)) == []
    assert G.surviving_descendants(str(gen), facts=_Table(None)) is None
    doc = _marker(gen)
    del doc["descendantToken"]
    (gen / G.MARKER).write_text(json.dumps(doc), encoding="utf-8")
    facts = _Table(None)
    assert G.surviving_descendants(str(gen), facts=facts) == [] and facts.scans == 0


def test_a_pid_listing_attributes_each_token_to_its_process():
    probe = "f" * 32
    listing = ("    1     0 /sbin/launchd\n"
               "   42     1 /opt/venv/bin/python3 x.py CCTALLY_SUITE_GENERATIONS=%s\n"
               "a continued line %s\n"
               "   43    42 ps -E -ww -A CCTALLY_SUITE_ENV_PROBE=%s\n" % (_ANCESTOR, _STRANGER, probe))
    assert G._table_in_listing(listing, probe) == {
        1: (0, set()), 42: (1, {_ANCESTOR, _STRANGER}), 43: (42, set())}
    assert G._table_in_listing(listing.replace(probe, "0" * 32), probe) is None


def test_the_proc_table_reads_parents_and_this_users_environments(tmp_path):
    probe = "e" * 32
    proc = tmp_path / "proc"
    for pid, stat, env in (("10", "10 (py (x)) S 7 10", b"CCTALLY_SUITE_GENERATIONS=" + _ANCESTOR.encode()),
                           ("12", "12 (cat) S 10 12", b"CCTALLY_SUITE_ENV_PROBE=" + probe.encode()),
                           ("13", None, b"X=" + _STRANGER.encode())):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "environ").write_bytes(env)
        if stat is not None:
            (proc / pid / "stat").write_text(stat)
    uid = os.geteuid()
    assert G._proc_table(str(proc), uid, 12, probe) == {
        10: (7, {_ANCESTOR}), 12: (10, set()), 13: (0, {_STRANGER})}
    assert G._proc_table(str(proc), uid + 1, 12, probe) == {10: (7, set()), 12: (10, set()), 13: (0, set())}
    assert G._proc_table(str(proc), uid, 12, "d" * 32) is None


class _RealScan(FakeFacts):
    """Injected owner, groups and reservation; the REAL descendant scan."""

    def descendant_tokens(self):
        self.scans += 1
        return G.Facts.descendant_tokens(self)


# A child in a session of its own that waits for a release file, and a parent
# that starts it and exits at once: nothing records the child's group, and no
# reservation sample can see it through its ancestry.
_DETACHED_CHILD = (
    "import os, sys, time\n"
    "release, pidfile = sys.argv[1], sys.argv[2]\n"
    "with open(pidfile + '.tmp', 'w') as fh:\n"
    "    fh.write(str(os.getpid()))\n"
    "os.rename(pidfile + '.tmp', pidfile)\n"
    "deadline = time.monotonic() + 100\n"
    "while not os.path.exists(release) and time.monotonic() < deadline:\n"
    "    time.sleep(0.05)\n"
)
_SHORT_LIVED_PARENT = (
    "import subprocess, sys\n"
    "subprocess.Popen([sys.executable, '-c'] + sys.argv[1:], start_new_session=True,\n"
    "                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
    "                 stderr=subprocess.DEVNULL)\n"
)


def _gone(pid, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_a_detached_descendant_keeps_its_generation_until_it_exits(tmp_path):
    """963-CD-G3: a ``start_new_session=True`` child whose parent exits before
    anything observes it escapes the recorded groups and the reservation's
    ancestry sampling. The token it inherited keeps the generation past its
    retention, and the first sweep after it exits reclaims it."""
    gen = _retained_past_its_clock(tmp_path)
    token = _marker(gen)["descendantToken"]
    assert token not in os.environ.get("CCTALLY_SUITE_GENERATIONS", ""), (
        "the test process itself carries the token it assesses")
    release, pidfile = tmp_path / "release", tmp_path / "child.pid"
    env = {"PATH": os.environ["PATH"], "CCTALLY_SUITE_GENERATIONS": "%s %s" % (_ANCESTOR, token)}
    child = None
    try:
        parent = subprocess.run([sys.executable, "-c", _SHORT_LIVED_PARENT, _DETACHED_CHILD,
                                 str(release), str(pidfile)],
                                env=env, capture_output=True, text=True, timeout=30)
        assert parent.returncode == 0, parent.stderr
        deadline = time.monotonic() + 30
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child = int(pidfile.read_text(encoding="utf-8"))
        assert os.getpgid(child) == child, "the child is not in a session of its own"
        out = _sweep(tmp_path, _RealScan({OWNER: ""}), now=1000.0 + 2 * DAY)
        assert _kept(gen, "a descendant carrying its token survives") in out, out
        assert gen.is_dir()
    finally:
        release.write_text("", encoding="utf-8")
        if child is not None and not _gone(child):
            os.kill(child, signal.SIGKILL)
    assert _gone(child), "the released child did not exit"
    out = _sweep(tmp_path, _RealScan({OWNER: ""}), now=1000.0 + 2 * DAY)
    assert "reclaimed suite generation %s" % _tok(gen) in out, out
    assert not gen.exists()


# --- J4's enumeration, pinned (spec 5.4 "The plan's enumeration") ---------
_NESTED_EXEMPT = {
    # path: reason. Operator tools the suite never launches (note R10).
    "tests/_a5s_mutation_runner.py": "operator tool, not launched by the suite",
}

#: subprocess's process-starting calls; the argv is the first positional
#: argument or ``args=``. ``os.exec*``, ``os.spawn*`` and ``os.posix_spawn*``
#: carry it among their positional arguments.
_SUBPROCESS_LAUNCHES = frozenset({"run", "Popen", "call", "check_call", "check_output"})
_OS_LAUNCH_PREFIXES = ("exec", "spawn", "posix_spawn")
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _own_nodes(scope):
    """Every node of ``scope``'s own code: nested functions and classes are
    scopes of their own and are not entered (a lambda is)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _launch_inputs(call):
    """The argument expressions a call starts a process from; empty for any
    other call. A data literal (a recorded command, a digest input) reaches
    none of these and is never a launch."""
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        if f.value.id == "subprocess" and f.attr in _SUBPROCESS_LAUNCHES:
            first = call.args[:1] or [k.value for k in call.keywords if k.arg == "args"]
            return list(first)
        if f.value.id == "os" and f.attr.startswith(_OS_LAUNCH_PREFIXES):
            return list(call.args)
    return []


def _is_pytest_argv(node):
    """A list or tuple literal holding the words ``-m pytest`` that is neither
    --collect-only nor --version (neither creates a basetemp)."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return False
    words = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return (("-m", "pytest") in list(zip(words, words[1:]))
            and not {"--collect-only", "--version"} & set(words))


def _flowing(nodes, roots):
    """The expressions whose values reach ``roots`` (launch inputs, or a
    helper's argv parameters) through local assignments, to a fixpoint."""
    sources = list(roots)
    names = {n.id for e in sources for n in ast.walk(e) if isinstance(n, ast.Name)}
    assigns = []
    for node in nodes:
        if isinstance(node, ast.Assign) and node.value is not None:
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            assigns.append((targets, node.value))
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)) and node.value is not None:
            if isinstance(node.target, ast.Name):
                assigns.append(([node.target.id], node.value))
    grew = True
    while grew:
        grew = False
        for targets, value in assigns:
            if value not in sources and set(targets) & names:
                sources.append(value)
                names |= {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
                grew = True
    return sources, names


def _launchers(scopes):
    """Module-local launcher helpers: {function name: (parameter positions,
    parameter names, scope)} for every function that starts a process from one
    of its own parameters."""
    found = {}
    for name, scope, nodes in scopes:
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        roots = [e for n in nodes if isinstance(n, ast.Call) for e in _launch_inputs(n)]
        if not roots:
            continue
        _, names = _flowing(nodes, roots)
        params = [a.arg for a in scope.args.posonlyargs + scope.args.args]
        hit = {i for i, p in enumerate(params) if p in names}
        kw = {p for p in params + [a.arg for a in scope.args.kwonlyargs] if p in names}
        if hit or kw:
            found[name] = (hit, kw, scope)
    return found


def _nested_pytest_functions(path):
    """(scope name, source to search for a basetemp) for each scope that starts
    a nested pytest session: a `-m pytest` argv literal that reaches a process
    launch directly, through local names, or through a module-local launcher
    helper (whose source is searched too). The module body is the scope
    ``<module>``. A file that spells no ``-m`` word cannot build such an argv
    and is not parsed."""
    source = path.read_text(encoding="utf-8")
    if '"-m"' not in source and "'-m'" not in source:
        return []
    tree = ast.parse(source)
    lines = source.splitlines(True)

    def segment(scope):
        if scope is tree:   # the module's own statements, not its functions
            return "".join(segment(s) for s in tree.body if not isinstance(s, _SCOPES))
        return "".join(lines[scope.lineno - 1:scope.end_lineno])

    scopes = [("<module>", tree, list(_own_nodes(tree)))]
    scopes += [(fn.name, fn, list(_own_nodes(fn))) for fn in ast.walk(tree)
               if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))]
    helpers = _launchers(scopes)
    found = []
    for name, scope, nodes in scopes:
        roots = [e for n in nodes if isinstance(n, ast.Call) for e in _launch_inputs(n)]
        searched = [scope]
        for n in nodes:
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in helpers:
                positions, keywords, helper = helpers[n.func.id]
                passed = [a for i, a in enumerate(n.args) if i in positions]
                passed += [k.value for k in n.keywords if k.arg in keywords]
                if passed:
                    roots.extend(passed)
                    searched.append(helper)
        if not roots:
            continue
        sources, _ = _flowing(nodes, roots)
        if any(_is_pytest_argv(n) for e in sources for n in ast.walk(e)):
            found.append((name, "\n".join(segment(s) for s in searched)))
    return found


_LOAD_INVARIANCE = REPO / "bin" / "cctally-test-load-invariance"


def test_every_nested_pytest_launch_names_its_basetemp():
    """A nested session without an explicit basetemp allocates in the numbered
    namespace beneath PYTEST_DEBUG_TEMPROOT or TMPDIR, outside the launching
    test's allocation (J4). Every repository launcher passes one."""
    paths = sorted(p for p in (REPO / "tests").rglob("*.py")
                   if "fixtures" not in p.relative_to(REPO).parts)
    # Mirror-private: scanned where it exists (the public tree has no copy).
    if _LOAD_INVARIANCE.exists():
        paths.append(_LOAD_INVARIANCE)
    missing = []
    for path in paths:
        rel = path.relative_to(REPO).as_posix()
        if rel in _NESTED_EXEMPT or not path.exists():
            continue
        for name, segment in _nested_pytest_functions(path):
            if "--basetemp" not in segment and "nested_basetemp_args(" not in segment:
                missing.append("%s::%s" % (rel, name))
    assert missing == [], "nested pytest launches without an explicit basetemp: %s" % missing


_SCAN_CASES = '''
import os, subprocess, sys

def direct():
    subprocess.run([sys.executable, "-m", "pytest", "x"])

def by_name():
    argv = [sys.executable, "-m", "pytest", "-q"]
    argv += ["x"]
    cmd = argv
    subprocess.Popen(cmd)

def by_keyword():
    subprocess.check_output(args=(sys.executable, "-m", "pytest"))

def by_exec():
    os.execv(sys.executable, [sys.executable, "-m", "pytest"])

def _launch_with_basetemp(argv, env=None):
    return subprocess.run(argv + ["--basetemp", "bt"], env=env)

def _launch(argv):
    return subprocess.run(argv)

def through_a_helper_with_basetemp():
    _launch_with_basetemp([sys.executable, "-m", "pytest", "y"])

def through_a_helper():
    _launch(argv=[sys.executable, "-m", "pytest", "y"])

def data_only():
    record = {"command": {"argv": ["python3", "-m", "pytest", "n"]}}
    digest(["python3", "-m", "pytest", "-k", "s"])
    subprocess.run(["git", "status"], env={"X": str(record)})

def collect_only():
    subprocess.run([sys.executable, "-m", "pytest", "--collect-only"])

def outer():
    def inner():
        subprocess.run([sys.executable, "-m", "pytest", "z"])
    return inner

subprocess.call([sys.executable, "-m", "pytest", "module-level"])
'''


def test_the_nested_launch_scan_matches_launches_not_data(tmp_path):
    """The scan reports a `-m pytest` argv that reaches a process launch
    (directly, through local names, by keyword, through os.exec* or through a
    module-local launcher helper, at module level or in a nested function) and
    never a data literal (a recorded command, a digest input) or a
    --collect-only launch. A scan defect is fixed here with a pinning case,
    never by exempting a file or adding --basetemp to a data literal."""
    path = tmp_path / "test_scan_cases.py"
    path.write_text(_SCAN_CASES, encoding="utf-8")
    found = {name: ("--basetemp" in seg or "nested_basetemp_args(" in seg)
             for name, seg in _nested_pytest_functions(path)}
    assert found == {"<module>": False, "direct": False, "by_name": False, "by_keyword": False,
                     "by_exec": False, "through_a_helper_with_basetemp": True,
                     "through_a_helper": False, "inner": False}, found

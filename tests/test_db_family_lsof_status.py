"""A failed lsof cannot establish that a SQLite family is drained."""
import subprocess

import pytest


@pytest.mark.parametrize("status,output,expected", [
    (-9, "", None), (-15, "p123\n", None),
    (2, "", None), (127, "p123\n", None),
    (0, "p123\np456\np123\ninvalid\npbad\n", {123, 456}),
    (1, "", set()),
])
def test_lsof_status_decides_whether_family_knowledge_is_available(tmp_path, monkeypatch, status, output, expected):
    import _cctally_db as db
    import _lib_wal_checkpoint

    path = tmp_path / "cache.db"
    path.touch()
    monkeypatch.setattr(_lib_wal_checkpoint, "release", lambda _path: None)
    monkeypatch.setattr(db.shutil, "which", lambda _name: "/usr/sbin/lsof")
    def run(argv, **kwargs):
        assert argv == ["/usr/sbin/lsof", "-F", "p", "--", str(path)]
        assert kwargs == dict(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, check=False)
        return subprocess.CompletedProcess(argv, status, stdout=output)
    monkeypatch.setattr(db.subprocess, "run", run)
    assert db._db_family_open_pids(path) == expected, status

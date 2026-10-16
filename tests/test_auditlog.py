import errno
import json
import os
import time

import pytest

from claude_hr import auditlog, paths


def test_record_appends_one_json_line_per_event(sandbox):
    assert auditlog.record(
        "gate", repo="r-1", formatted=2, checks=["ruff", "pytest"], ok=True, seconds=1.25
    )
    assert auditlog.record("gate", repo="r-1", ok=False)
    files = list(auditlog.audit_dir().glob("*.jsonl"))
    assert len(files) == 1 and files[0].name == time.strftime("%Y-%m") + ".jsonl"
    rows = [json.loads(ln) for ln in files[0].read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in rows] == ["gate", "gate"]
    assert (
        rows[0]["checks"] == ["ruff", "pytest"]
        and rows[0]["seconds"] == 1.25
        and rows[1]["ok"] is False
    )
    assert rows[0]["ts"]


def test_record_keeps_metadata_only_and_never_raises(sandbox):
    auditlog.record("gate", detail="x" * 5000, nested={"a": {"b": {"c": "deep"}}}, obj=object())
    row = auditlog.tail(1)[0]
    assert len(row["detail"]) == auditlog.MAX_STR  # no free text
    assert row["nested"]["a"]["b"].startswith("{")  # deeper structures are flattened to short text
    assert row["obj"].startswith("<object")
    big = {f"k{i}": "y" * 100 for i in range(20)}
    auditlog.record("gate", **{f"f{i}": big for i in range(5)})
    assert (
        auditlog.tail(1)[0].get("truncated") is True
    )  # one record never exceeds a single safe write


def test_record_failure_is_silent(sandbox, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(
            auditlog.os, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied"))
        )
        assert auditlog.record("gate", ok=True) is False


def test_audit_files_are_private_on_posix(sandbox):
    auditlog.record("gate", ok=True)
    if os.name != "nt":
        assert oct(auditlog.audit_dir().stat().st_mode & 0o777) == "0o700"
        f = next(auditlog.audit_dir().glob("*.jsonl"))
        assert oct(f.stat().st_mode & 0o777) == "0o600"


def test_tail_returns_the_newest_events_in_order_and_skips_garbage(sandbox):
    for i in range(5):
        auditlog.record("gate", n=i)
    f = next(auditlog.audit_dir().glob("*.jsonl"))
    f.write_text(f.read_text(encoding="utf-8") + "not json\n", encoding="utf-8")
    assert [r["n"] for r in auditlog.tail(3)] == [2, 3, 4]
    assert auditlog.tail(0) == [] or len(auditlog.tail(0)) <= 1


def test_prune_removes_old_month_files_only(sandbox):
    auditlog.record("gate", n=1)
    old = auditlog.audit_dir() / "2020-01.jsonl"
    old.write_text('{"event":"x"}\n', encoding="utf-8")
    t = time.time() - 400 * 86400
    os.utime(old, (t, t))
    assert auditlog.prune(90) == 1
    assert not old.exists() and len(list(auditlog.audit_dir().glob("*.jsonl"))) == 1


def test_repo_id_is_stable_and_hides_the_remote(sandbox, tmp_path):
    root = tmp_path / "my project"
    root.mkdir()
    a = auditlog.repo_id(root, "https://github.com/acme/secret-repo.git")
    assert a == auditlog.repo_id(root, "https://github.com/acme/secret-repo.git")
    assert a != auditlog.repo_id(root, "https://github.com/acme/other.git")
    assert "acme" not in a and a.startswith("my project-")
    assert auditlog.repo_id(root) == auditlog.repo_id(root)  # falls back to the resolved path


def test_audit_dir_lives_under_the_claude_hr_home(sandbox):
    assert auditlog.audit_dir() == paths.hr_home() / "audit"


# ---- concurrent appends --------------------------------------------------------------------------------------------
_APPENDER = (
    "import sys, time\n"
    "from claude_hr import auditlog\n"
    "start, n, tag = float(sys.argv[1]), int(sys.argv[2]), sys.argv[3]\n"
    "while time.time() < start:\n"
    "    pass\n"
    "bad = 0\n"
    "for i in range(n):\n"
    "    if not auditlog.record('gate', tag=tag, i=i, pad='x' * 200):\n"
    "        bad += 1\n"
    "sys.exit(1 if bad else 0)\n"
)


def test_concurrent_appends_from_several_processes_lose_nothing(sandbox):
    """On Windows O_APPEND is emulated (seek, then write), so unserialised appenders overwrote each other: 2-4% of the
    records of two busy processes were silently lost and a few lines were cut in half."""
    import subprocess
    import sys
    from pathlib import Path

    n_procs, per_proc = 4, 150
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    start = time.time() + 3.0  # every child spins until this instant, so they all append at once
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _APPENDER, str(start), str(per_proc), f"p{k}"], env=env
        )
        for k in range(n_procs)
    ]
    assert [p.wait(timeout=180) for p in procs] == [0] * n_procs
    lines = next(auditlog.audit_dir().glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    rows = [json.loads(ln) for ln in lines]  # a torn line would raise here
    assert len(rows) == n_procs * per_proc
    for k in range(n_procs):
        assert sorted(r["i"] for r in rows if r["tag"] == f"p{k}") == list(range(per_proc))


def test_the_append_lock_is_released_after_each_record(sandbox):
    assert auditlog.record("gate", n=1)
    f = next(auditlog.audit_dir().glob("*.jsonl"))
    with auditlog._append_lock(f) as held:
        assert held  # nothing keeps the lock after record() returned
    assert auditlog.record("gate", n=2)


def test_a_lock_that_cannot_be_taken_never_costs_the_record(sandbox, monkeypatch):
    """Fail open: another process holding the lock for longer than LOCK_WAIT_S must not drop or delay the record forever."""
    auditlog.record("gate", n=1)
    f = next(auditlog.audit_dir().glob("*.jsonl"))
    monkeypatch.setattr(auditlog, "LOCK_WAIT_S", 0.05)
    with auditlog._append_lock(f) as held:
        assert held
        t0 = time.monotonic()
        assert (
            auditlog.record("gate", n=2) is True
        )  # the lock is busy: it waits briefly, then appends anyway
        assert time.monotonic() - t0 < 5
    assert [r["n"] for r in auditlog.tail(5)] == [1, 2]


def _locking_fails_with(monkeypatch, code: int) -> list[int]:
    """Make the OS locking call (msvcrt.locking / fcntl.flock) raise OSError(code); returns a list that gets one item per call."""
    calls: list[int] = []

    def boom(*a, **k):
        calls.append(1)
        raise OSError(code, os.strerror(code))

    if os.name == "nt":
        import msvcrt

        monkeypatch.setattr(msvcrt, "locking", boom)
    else:
        import fcntl

        monkeypatch.setattr(fcntl, "flock", boom)
    return calls


@pytest.mark.parametrize("name", ["ENOLCK", "EINVAL", "ENOSYS"])
def test_a_lock_the_system_cannot_take_costs_no_wait_and_is_remembered(sandbox, monkeypatch, name):
    """NFS without lockd (ENOLCK) or a filesystem without locks never grants a lock: waiting LOCK_WAIT_S for it on every
    gate record was a pure 1 s delay. Give up at once, append unlocked, and do not even try again in this process."""
    monkeypatch.setattr(
        auditlog, "_lock_unusable", False
    )  # restored by monkeypatch: the flag must not leak
    monkeypatch.setattr(auditlog, "LOCK_WAIT_S", 30.0)  # a wait would show
    calls = _locking_fails_with(monkeypatch, getattr(errno, name))
    t0 = time.monotonic()
    assert auditlog.record("gate", n=1) is True
    assert time.monotonic() - t0 < 5 and len(calls) == 1
    assert auditlog._lock_unusable is True
    assert auditlog.record("gate", n=2) is True
    assert len(calls) == 1  # the second record did not ask the OS again
    assert [r["n"] for r in auditlog.tail(5)] == [1, 2]


@pytest.mark.parametrize("name", ["EACCES", "EAGAIN"])
def test_a_lock_that_is_merely_busy_is_retried_for_a_short_bounded_time(sandbox, monkeypatch, name):
    monkeypatch.setattr(auditlog, "_lock_unusable", False)
    assert (
        auditlog.LOCK_WAIT_S <= 0.25
    )  # an append never waits long for another one (it is one small write)
    calls = _locking_fails_with(monkeypatch, getattr(errno, name))
    t0 = time.monotonic()
    assert (
        auditlog.record("gate", n=1) is True
    )  # fail open: appended unlocked after the bounded wait
    took = time.monotonic() - t0
    assert len(calls) > 1 and took < 2.0
    assert auditlog._lock_unusable is False  # contention says nothing about the filesystem
    assert auditlog.tail(1)[0]["n"] == 1


def test_try_lock_tells_busy_from_unusable(sandbox, monkeypatch):
    f = auditlog.audit_dir()
    f.mkdir(parents=True, exist_ok=True)
    fd = os.open(f / "probe.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        _locking_fails_with(monkeypatch, errno.EACCES)
        assert auditlog._try_lock(fd) is False
        _locking_fails_with(monkeypatch, errno.ENOLCK)
        assert auditlog._try_lock(fd) is None
    finally:
        os.close(fd)


def test_an_unusable_lock_file_never_costs_the_record(sandbox, monkeypatch):
    monkeypatch.setattr(
        auditlog, "_try_lock", lambda fd: (_ for _ in ()).throw(OSError("no locks here"))
    )
    # the failure is inside the helper's own guard path: the record still goes through
    assert auditlog.record("gate", n=1) is True
    assert auditlog.tail(1)[0]["n"] == 1


def test_prune_removes_the_lock_of_a_pruned_month_but_not_a_live_one(sandbox):
    auditlog.record("gate", n=1)
    live = next(auditlog.audit_dir().glob("*.lock"))
    old_lock = auditlog.audit_dir() / "2020-01.lock"
    old_lock.write_text("", encoding="utf-8")
    t = time.time() - 400 * 86400
    os.utime(old_lock, (t, t))
    old_log = auditlog.audit_dir() / "2020-02.jsonl"
    old_log.write_text('{"event":"x"}\n', encoding="utf-8")
    os.utime(old_log, (t, t))
    orphaned = auditlog.audit_dir() / "2020-02.lock"
    orphaned.write_text("", encoding="utf-8")
    os.utime(orphaned, (t, t))
    assert auditlog.prune(90) == 1  # only month files are counted
    assert not old_lock.exists() and not old_log.exists()
    assert live.exists()
    assert (
        not orphaned.exists()
    )  # its month file was pruned in the same run: the lock is orphaned and old

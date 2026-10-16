import json
import os
import time

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

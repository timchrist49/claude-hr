import json
import os
import subprocess
import sys
import time

import pytest

from claude_hr import config, maintenance, paths, usage, worktree


def write_transcript(root, project, entries):
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    f = d / "s1.jsonl"
    f.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return f


def msg(mid, model, ts, **usage_fields):
    return {"timestamp": ts, "uuid": mid, "message": {"id": mid, "model": model, "usage": usage_fields}}


def test_usage_ledger_dedups_streamed_messages_and_buckets(sandbox):
    root = paths.claude_home() / "projects"
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    write_transcript(root, "p-one", [
        msg("m1", "claude-sonnet-5-5", now, input_tokens=100, output_tokens=10, cache_read_input_tokens=900),
        msg("m1", "claude-sonnet-5-5", now, input_tokens=100, output_tokens=10, cache_read_input_tokens=900),  # same message again
        msg("m2", "claude-haiku-5-5", now, input_tokens=50, output_tokens=5, cache_creation_input_tokens=50),
        {"timestamp": now, "type": "user", "message": {"role": "user", "content": "hi"}},
    ])
    led = usage.collect(7)
    assert led.total.messages == 2
    assert led.total.input == 150
    assert led.total.cache_read == 900
    assert set(led.by_model) == {"claude-sonnet-5-5", "claude-haiku-5-5"}
    assert abs(led.by_model["claude-sonnet-5-5"].cache_hit - 0.9) < 1e-9
    assert "TOTAL" in usage.format_ledger(led, "model")


def test_usage_ignores_old_transcripts(sandbox):
    root = paths.claude_home() / "projects"
    f = write_transcript(root, "p", [msg("m1", "x", "2020-01-01T00:00:00.000Z", input_tokens=5, output_tokens=1)])
    old = time.time() - 40 * 86400
    os.utime(f, (old, old))
    assert usage.collect(7).total.messages == 0


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "init")
    return r


def test_worktree_lifecycle(repo):
    rc, path = worktree.new("fix login", cwd=repo)
    assert rc == 0
    assert (repo / ".claude" / "worktrees" / "fix-login").is_dir()
    assert git(repo, "status", "--porcelain").stdout.strip() == ""  # excluded from git status
    assert any("fix-login" in w.get("worktree", "") for w in worktree.list_all(cwd=repo))
    assert worktree.new("fix login", cwd=repo)[0] == 1  # no clobbering
    (repo / ".claude" / "worktrees" / "fix-login" / "dirty.txt").write_text("x")
    git(repo / ".claude" / "worktrees" / "fix-login", "add", "dirty.txt")
    rc, msg_ = worktree.remove("fix login", cwd=repo)
    assert rc == 1 and "uncommitted" in msg_  # refuses to destroy work
    assert worktree.remove("fix login", force=True, cwd=repo)[0] == 0


def test_worktree_names_cannot_escape_the_worktree_dir(repo):
    rc, path = worktree.new("../../etc", cwd=repo)  # leading traversal is neutralised, not followed
    assert rc == 0
    assert (repo / ".claude" / "worktrees").resolve() in __import__("pathlib").Path(path).resolve().parents
    with pytest.raises(ValueError):
        worktree.new("a/../b", cwd=repo)  # embedded traversal is refused


def test_cleanup_plan_spares_active_and_recent(sandbox, monkeypatch):
    jobs = paths.claude_home() / "jobs"
    for name, age in (("old11111", 40), ("new22222", 1), ("act33333", 40)):
        d = jobs / name / "tmp"
        d.mkdir(parents=True)
        (d / "blob.bin").write_bytes(b"x" * 1000)
        t = time.time() - age * 86400
        for target in (d / "blob.bin", d, jobs / name):  # activity is judged by the newest file, so age all of them
            os.utime(target, (t, t))
    monkeypatch.setattr(maintenance, "_active_job_ids", lambda: {"act33333"})
    plan = maintenance.plan(config.load())
    victims = {i.path.parent.name for i in plan.items if i.kind == "job-scratch"}
    assert victims == {"old11111"}
    assert any("act33333" in s for s in plan.skipped)
    n, _ = maintenance.apply(plan)
    assert n == 1
    assert (jobs / "act33333" / "tmp").exists() and not (jobs / "old11111" / "tmp").exists()


def test_daemon_log_is_rotated_not_deleted(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_MAINTENANCE__DAEMON_LOG_MAX_MB", "1")
    log = paths.claude_home() / "daemon.log"
    log.write_bytes(b"x" * (2 * 1024 * 1024))
    maintenance.apply(maintenance.plan(config.load()))
    assert not log.exists()
    assert (paths.claude_home() / "daemon.log.old").exists()


def test_filelock_excludes_and_recovers_from_dead_owner(tmp_path):
    from claude_hr.filelock import LockTimeout, locked

    lock = tmp_path / "x.lock"
    with locked(lock):
        with pytest.raises(LockTimeout):
            with locked(lock, timeout=0.3):
                pass
    lock.write_text("999999\n")  # owner PID that does not exist
    with locked(lock, timeout=1):
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_windows_job_kills_descendants_on_close():
    from claude_hr.resources import WindowsJob

    job = WindowsJob(0, kill_on_close=True)
    parent = subprocess.Popen([sys.executable, "-c", (
        "import subprocess,sys,time;"
        "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(c.pid,flush=True);time.sleep(60)")], stdout=subprocess.PIPE, text=True)
    assert job.assign(parent.pid)
    grandchild = int(parent.stdout.readline())
    from claude_hr import sysinfo

    assert sysinfo.pid_alive(grandchild)
    job.close()
    time.sleep(1.0)
    assert not sysinfo.pid_alive(grandchild)
    assert parent.poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_windows_job_memory_cap_is_enforced():
    from claude_hr.resources import WindowsJob

    job = WindowsJob(0.15, kill_on_close=True)  # 150 MB for the whole job
    p = subprocess.Popen([sys.executable, "-c", "x=bytearray(600*1024*1024);print('survived')"], stdout=subprocess.PIPE, text=True)
    assert job.assign(p.pid)
    out, _ = p.communicate(timeout=60)
    job.close()
    assert "survived" not in out
    assert p.returncode != 0


def test_cli_cache_dirs_follow_the_sandbox(sandbox, tmp_path):
    """Regression: a test once planned deletions under the REAL %LOCALAPPDATA% because the fixture didn't isolate it."""
    real = os.environ.get("CLAUDE_HR_TEST_REAL_LOCALAPPDATA")
    for d in maintenance._cli_cache_dirs():
        assert str(tmp_path) in str(d)
        assert real is None or real not in str(d)


def test_ctl_runs_without_a_console(sandbox):
    """Regression: scheduled jobs run under pythonw.exe where sys.stdout/stderr are None; import must not touch them."""
    code = (
        "import sys; sys.stdout = None; sys.stderr = None\n"
        "from claude_hr import ctl\n"
        "raise SystemExit(ctl.main(['job', 'monitor']))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"),
               CLAUDE_HR_HEADROOM__ENABLED="false")
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    log = paths.logs_dir() / "job-monitor.log"
    assert log.exists() and "rc=0" in log.read_text(encoding="utf-8")


def test_cleanup_fails_closed_when_active_sessions_unknown(sandbox, monkeypatch):
    d = paths.claude_home() / "jobs" / "old11111" / "tmp"
    d.mkdir(parents=True)
    (d / "blob.bin").write_bytes(b"x" * 100)
    t = time.time() - 90 * 86400
    for target in (d / "blob.bin", d, d.parent):
        os.utime(target, (t, t))
    monkeypatch.setattr(maintenance, "_active_job_ids", lambda: None)  # `claude agents --json` failed
    plan = maintenance.plan(config.load())
    assert not [i for i in plan.items if i.kind == "job-scratch"]
    assert any("cannot tell" in s for s in plan.skipped)


def test_recent_file_inside_old_job_dir_keeps_it(sandbox, monkeypatch):
    d = paths.claude_home() / "jobs" / "busy9999" / "tmp"
    d.mkdir(parents=True)
    (d / "fresh.log").write_text("still writing")  # written just now
    t = time.time() - 90 * 86400
    os.utime(d.parent, (t, t))  # the job dir's own mtime is ancient
    monkeypatch.setattr(maintenance, "_active_job_ids", lambda: set())
    assert not [i for i in maintenance.plan(config.load()).items if i.kind == "job-scratch"]


def test_rmtree_force_removes_read_only_files(tmp_path):
    import stat

    from claude_hr import ctl

    d = tmp_path / "repo" / ".git" / "objects"
    d.mkdir(parents=True)
    f = d / "pack"
    f.write_text("x")
    f.chmod(stat.S_IREAD)  # git writes objects read-only; plain rmtree fails on Windows
    ctl._rmtree_force(tmp_path / "repo")
    assert not (tmp_path / "repo").exists()


def test_doctor_survives_a_corrupt_settings_file(sandbox):
    from pathlib import Path

    from claude_hr import doctor

    paths.claude_settings().write_text("{ nope", encoding="utf-8")
    checks = doctor.run(config.load(), Path.cwd())
    assert any(c.name == "settings.json" and c.status == "fail" for c in checks)


def test_concurrent_state_writes_never_raise(sandbox, tmp_path):
    """Regression: parallel launches replacing the same bookkeeping file raised PermissionError ~15% of the time on Windows."""
    target = tmp_path / "ring.json"
    code = (
        "import sys, json\n"
        "from pathlib import Path\n"
        "from claude_hr.jsonio import atomic_write_text\n"
        "p = Path(sys.argv[1]); errors = 0\n"
        "for i in range(120):\n"
        "    try:\n"
        "        atomic_write_text(p, json.dumps({'i': i}))\n"
        "    except Exception:\n"
        "        errors += 1\n"
        "sys.exit(1 if errors else 0)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
    procs = [subprocess.Popen([sys.executable, "-c", code, str(target)], env=env) for _ in range(4)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    assert json.loads(target.read_text(encoding="utf-8"))["i"] >= 0


@pytest.mark.skipif(os.name != "nt", reason="the Windows shim is an .exe copy")
def test_windows_shim_passes_arguments_through_exactly(sandbox, tmp_path, monkeypatch):
    """Regression: a claude.cmd shim let cmd.exe re-parse & | > % ^ and truncate multi-line prompts."""
    from pathlib import Path

    from claude_hr import ctl, procutil

    launcher_exe = Path(sys.executable).with_name("claude-hr.exe")
    if not launcher_exe.exists():
        pytest.skip("claude-hr.exe not installed in this environment")
    monkeypatch.setattr(procutil, "which_all", lambda name, path=None: [launcher_exe])
    ns = type("A", (), {"action": "install"})()
    assert ctl.cmd_shim(ns, config.load()) == 0
    shim = paths.bin_dir() / "claude.exe"
    assert shim.exists()
    tricky = ["x&y", "a|b", "a>b.txt", "50%PATH%", "line1\nline2", "^caret", 'say "hi"', "trail\\"]
    code = "import sys, json; print(json.dumps(sys.argv[1:]))"
    env = dict(os.environ, CLAUDE_HR_CLAUDE_BIN=sys.executable, CLAUDE_HR_DIRECT="1", CLAUDE_HR_HEADROOM__ENABLED="false")
    env.pop("CLAUDE_HR_SESSION", None)
    r = subprocess.run([str(shim), "-c", code, *tricky], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == tricky
    assert not (tmp_path / "b.txt").exists()


def test_doctor_says_so_when_linux_memory_caps_are_inactive(sandbox, monkeypatch):
    """The caps silently do nothing without a systemd user bus (minimal VMs, stock WSL); doctor must not hide that."""
    from pathlib import Path

    from claude_hr import doctor, resources

    monkeypatch.setattr(doctor.paths, "IS_LINUX", True)
    monkeypatch.setattr(resources, "caps_available", lambda: False)
    caps = [c for c in doctor.run(config.load(), Path.cwd()) if c.name == "resource caps"]
    assert caps and caps[0].status == "warn" and "dbus-user-session" in caps[0].fix
    monkeypatch.setattr(resources, "caps_available", lambda: True)
    caps = [c for c in doctor.run(config.load(), Path.cwd()) if c.name == "resource caps"]
    assert caps and caps[0].status == "ok"


def test_setup_profile_is_saved_so_doctor_and_later_setups_keep_it(sandbox):
    """Regression: `install --profile minimal` applied to one run only; doctor then reported the standard plugins as missing."""
    import tomllib

    from claude_hr import ctl

    assert config.load().get("plugins.profile") == "standard"
    assert ctl._run(["setup", "--only", "claude-md", "--profile", "minimal", "--dry-run"]) == 0
    assert not paths.user_config_file().exists()  # a dry run changes nothing
    assert ctl._run(["setup", "--only", "claude-md", "--profile", "minimal"]) == 0
    assert tomllib.loads(paths.user_config_file().read_text(encoding="utf-8"))["plugins"]["profile"] == "minimal"
    assert config.load().get("plugins.profile") == "minimal"
    assert ctl._run(["config", "set", "plugins.profile", "full"]) == 0  # `config set` still works through the same helper
    assert config.load().get("plugins.profile") == "full"


# ---- config persistence (found by the 0.1.3 review) --------------------------------------------------------------------
def test_setup_profile_keeps_comments_and_non_bmp_text_in_an_existing_config(sandbox):
    """`setup --profile` used to rewrite config.toml with ensure_ascii escapes (surrogate pairs are invalid TOML), which broke
    every later claude-hr-ctl command, and it dropped the user's comments even when the profile did not change."""
    import tomllib

    from claude_hr import ctl

    cfg_file = paths.user_config_file()
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    original = '# my notes\n[monitor]\nalert_command = "notify-send \U0001F6A8"\n[plugins]\nprofile = "minimal"\n'
    cfg_file.write_text(original, encoding="utf-8")
    assert ctl._run(["setup", "--only", "claude-md", "--profile", "minimal"]) == 0
    assert cfg_file.read_text(encoding="utf-8") == original  # unchanged value -> file untouched, comment kept
    assert ctl._run(["setup", "--only", "claude-md", "--profile", "full"]) == 0
    data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))  # still valid TOML
    assert data["monitor"]["alert_command"] == "notify-send \U0001F6A8" and data["plugins"]["profile"] == "full"
    assert config.load().get("monitor.alert_command") == "notify-send \U0001F6A8"


def test_toml_writer_round_trips_awkward_values():
    import datetime
    import tomllib

    from claude_hr import ctl

    data = {"a": {"weird key": 'q"uote\back\x7f', "when": datetime.datetime(2026, 1, 2, 3, 4, 5), "day": datetime.date(2026, 1, 2),
                  "t": {"x y": [1, 2.5, "z"]}}, "plain-key_1": True}
    assert tomllib.loads(ctl._dump_toml(data)) == data


def test_a_config_file_with_a_bom_loads_and_can_be_edited(sandbox):
    from claude_hr import ctl

    f = paths.user_config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\xef\xbb\xbf[resources]\nmax_sessions = 3\n")  # what Windows PowerShell 5.1 `Set-Content -Encoding UTF8` writes
    assert config.load().get("resources.max_sessions") == 3
    assert ctl._run(["config", "set", "plugins.profile", "full"]) == 0
    assert not f.read_bytes().startswith(b"\xef\xbb\xbf") and config.load().get("resources.max_sessions") == 3


def test_persist_failures_are_reported_not_raised(sandbox, monkeypatch):
    from claude_hr import ctl

    f = paths.user_config_file()
    f.parent.mkdir(parents=True, exist_ok=True)

    def deny(self, *a, **k):
        raise PermissionError("read-only")

    with monkeypatch.context() as scoped:  # NOT monkeypatch.undo(): that would also undo the sandbox environment
        scoped.setattr(type(f), "write_text", deny)
        ok, msg = ctl._persist_config("plugins.profile", "minimal")
    assert ok is False and "read-only" in msg
    assert str(paths.hr_home()).startswith(str(sandbox))  # still inside the sandbox
    f.write_text('plugins = "not a table"\n', encoding="utf-8")
    ok, msg = ctl._persist_config("plugins.profile", "minimal")
    assert ok is False and "not a table" in msg


def test_config_set_without_a_value_is_rejected(sandbox):
    from claude_hr import ctl

    assert ctl._run(["config", "set", "plugins.profile"]) == 2
    assert not paths.user_config_file().exists()


def test_install_time_lite_footprint_is_saved(sandbox, monkeypatch):
    """`CLAUDE_HR_HEADROOM__FOOTPRINT=lite` before the installer only lives for that process; later reinstalls would go full."""
    import tomllib

    from claude_hr import ctl

    monkeypatch.setenv("CLAUDE_HR_HEADROOM__FOOTPRINT", "lite")
    assert ctl._run(["setup", "--only", "claude-md"]) == 0
    assert tomllib.loads(paths.user_config_file().read_text(encoding="utf-8"))["headroom"]["footprint"] == "lite"
    monkeypatch.delenv("CLAUDE_HR_HEADROOM__FOOTPRINT")
    assert config.load().get("headroom.footprint") == "lite"


def test_scheduler_failures_are_reported_as_errors(sandbox, capsys):
    from claude_hr import ctl

    line, bad = ctl._schedule_line("systemd timer update: FAILED boom")
    assert bad and line.startswith("[ERR] schedule:")
    line, bad = ctl._schedule_line("cron: could not read the current crontab (x); not touching it")
    assert bad
    line, bad = ctl._schedule_line("Task Scheduler: nothing to remove")
    assert not bad and line.startswith("[ok ] schedule:")


def test_wt_rm_delete_branch_says_what_it_did(repo):
    """`--delete-branch` used `git branch -d`, ignored the result and printed only 'removed': an unmerged branch silently stayed."""
    # a branch with unmerged work is kept, and the message says so
    rc, path = worktree.new("feat", cwd=repo)
    assert rc == 0
    wt = repo / ".claude" / "worktrees" / "feat"
    (wt / "new.txt").write_text("x")
    git(wt, "add", "new.txt")
    git(wt, "commit", "-q", "-m", "unmerged work")
    rc, msg_ = worktree.remove("feat", delete_branch=True, cwd=repo)
    assert rc == 0 and "kept branch hr/feat" in msg_ and "git branch -D hr/feat" in msg_
    assert "hr/feat" in git(repo, "branch", "--list", "hr/*").stdout
    # a branch with nothing to lose is deleted, and the message says so
    rc, path = worktree.new("clean", cwd=repo)
    rc, msg_ = worktree.remove("clean", delete_branch=True, cwd=repo)
    assert rc == 0 and "deleted branch hr/clean" in msg_
    assert "hr/clean" not in git(repo, "branch", "--list", "hr/*").stdout
    # without the flag nothing is said about branches
    worktree.new("plain", cwd=repo)
    rc, msg_ = worktree.remove("plain", cwd=repo)
    assert rc == 0 and msg_.startswith("removed ") and "deleted branch" not in msg_ and "kept branch" not in msg_
    assert "hr/plain" in git(repo, "branch", "--list", "hr/*").stdout  # the default keeps the branch


def test_doctor_flags_a_self_update_that_did_not_finish(sandbox, monkeypatch):
    from pathlib import Path

    from claude_hr import doctor

    d = paths.hr_home() / "repo" / "src" / "claude_hr"
    d.mkdir(parents=True)
    (d / "__init__.py").write_text('__version__ = "99.0.0"\n', encoding="utf-8")
    hit = [c for c in doctor.run(config.load(), Path.cwd()) if c.name == "claude-hr version"]
    assert hit and hit[0].status == "warn" and "99.0.0" in hit[0].detail and "update --only self" in hit[0].fix
    (d / "__init__.py").write_text('__version__ = "0.0.1"\n', encoding="utf-8")
    assert not [c for c in doctor.run(config.load(), Path.cwd()) if c.name == "claude-hr version"]

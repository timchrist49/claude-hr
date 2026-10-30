"""Launcher programs that Windows refuses to start: the stand-ins (.cmd + shell script) that `claude-hr-ctl launchers` writes.

The logic is tested everywhere with a probe that says what a test wants; the Windows tests at the end use files that Windows
really refuses (an execute-deny rule on a copy of curl.exe, the same "Access is denied" an endpoint product gives) and run the
stand-ins for real.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from claude_hr import config, ctl, doctor, launchers, paths
from claude_hr.headroom import Headroom, Health
from claude_hr.launchers import LAUNCHERS, MARKER, Probe, ensure, remove, render_cmd, render_sh

WINDOWS = os.name == "nt"
REFUSED = Probe(False, True, 5, "Access is denied")
STARTS = Probe(True)
PY = r"C:\Users\x\AppData\Roaming\uv\tools\claude-hr\Scripts\python.exe"


def make_bin(tmp_path: Path, names=tuple(LAUNCHERS)) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    for name in names:
        (folder / f"{name}.exe").write_bytes(b"MZ the launcher of " + name.encode())
    return folder


def fake_probe(verdicts: dict, log: list | None = None):
    """verdicts: command name -> a Probe, or a list of them (one per look, the last one repeats)."""

    def probe(path: Path, args: list[str]) -> Probe:
        name = path.name.split(".exe")[0]
        if log is not None:
            log.append((name, path.name, list(args)))
        v = verdicts.get(name, STARTS)
        if isinstance(v, list):
            return v.pop(0) if len(v) > 1 else v[0]
        return v

    return probe


def look(folder: Path, verdicts: dict, **kw):
    kw.setdefault("waits", ())
    kw.setdefault("python", PY)
    return ensure(bin_dir=folder, probe_fn=fake_probe(verdicts), **kw)


# ---- what ensure does ------------------------------------------------------------------------------------------------------
def test_launchers_that_start_are_left_alone(tmp_path):
    folder = make_bin(tmp_path)
    out = look(folder, {})
    assert out.ok and not out.changed and not out.refused
    assert sorted(p.name for p in folder.iterdir()) == sorted(f"{n}.exe" for n in LAUNCHERS)
    assert "start normally" in " ".join(out.lines)
    assert launchers.refused_names() == []


def test_a_refused_launcher_is_moved_aside_and_gets_stand_ins(tmp_path):
    folder = make_bin(tmp_path)
    out = look(folder, {"claude-hr-ctl": REFUSED})
    assert out.ok and out.changed
    assert list(out.refused) == ["claude-hr-ctl"]
    assert not (folder / "claude-hr-ctl.exe").exists()
    assert (
        folder / "claude-hr-ctl.exe.refused"
    ).read_bytes() == b"MZ the launcher of claude-hr-ctl"
    cmd = (folder / "claude-hr-ctl.cmd").read_text(encoding="ascii")
    sh = (folder / "claude-hr-ctl").read_text(encoding="ascii")
    assert MARKER in cmd and MARKER in sh
    assert f'"{PY}" -I -m claude_hr.ctl %*' in cmd
    assert "-I -m claude_hr.ctl" in sh
    # the others are untouched: no stand-in, the launcher is where it was
    for other in ("claude-hr", "claude-hr-hook"):
        assert (folder / f"{other}.exe").is_file()
        assert not (folder / f"{other}.cmd").exists() and not (folder / other).exists()
    assert launchers.refused_names() == ["claude-hr-ctl"]
    assert launchers.is_shimmed("claude-hr-ctl") and not launchers.is_shimmed("claude-hr")
    assert "error 5: Access is denied" in " ".join(out.lines)
    assert "claude-hr-ctl launchers ensure" in " ".join(
        out.lines
    )  # what to do once the folders are allowed


def test_each_launcher_gets_the_module_it_runs(tmp_path):
    folder = make_bin(tmp_path)
    look(folder, {n: REFUSED for n in LAUNCHERS})
    for name, (module, _args) in LAUNCHERS.items():
        assert f"-I -m {module} %*" in (folder / f"{name}.cmd").read_text(encoding="ascii")
        assert f"-I -m {module} " in (folder / name).read_text(encoding="ascii")
    assert sorted(launchers.refused_names()) == sorted(LAUNCHERS)


def test_the_probe_starts_each_launcher_with_an_argument_that_does_nothing(tmp_path):
    folder = make_bin(tmp_path)
    log: list = []
    ensure(bin_dir=folder, python=PY, probe_fn=fake_probe({}, log), waits=())
    assert sorted(log) == [
        ("claude-hr", "claude-hr.exe", ["ctl", "--version"]),
        ("claude-hr-ctl", "claude-hr-ctl.exe", ["--version"]),
        ("claude-hr-hook", "claude-hr-hook.exe", []),
    ]


def test_a_refusal_is_believed_only_after_waiting_and_looking_again(tmp_path):
    """A scanner can hold a file that was written a moment ago: three more looks, spread over twelve seconds."""
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    slept: list[float] = []
    out = ensure(
        bin_dir=folder,
        python=PY,
        probe_fn=fake_probe({"claude-hr-ctl": [REFUSED, REFUSED, STARTS]}),
        sleep=slept.append,
    )
    assert slept == [
        2.0,
        4.0,
    ]  # the second look was refused too, the third started: no more waiting
    assert (folder / "claude-hr-ctl.exe").is_file() and not out.refused and not out.changed
    slept.clear()
    out = ensure(
        bin_dir=folder,
        python=PY,
        probe_fn=fake_probe({"claude-hr-ctl": REFUSED}),
        sleep=slept.append,
    )
    assert slept == [2.0, 4.0, 6.0]
    assert out.changed and (folder / "claude-hr-ctl.exe.refused").is_file()


def test_the_waiting_is_shared_by_all_refused_launchers(tmp_path):
    folder = make_bin(tmp_path)
    slept: list[float] = []
    ensure(
        bin_dir=folder,
        python=PY,
        probe_fn=fake_probe({n: REFUSED for n in LAUNCHERS}),
        sleep=slept.append,
    )
    assert slept == [2.0, 4.0, 6.0]  # not three times as long


def test_a_launcher_that_starts_again_gets_its_place_back_and_the_stand_ins_go(tmp_path):
    folder = make_bin(tmp_path)
    look(folder, {"claude-hr-ctl": REFUSED})
    assert launchers.is_shimmed("claude-hr-ctl")
    out = look(folder, {"claude-hr-ctl": STARTS})
    assert out.ok and out.changed
    assert (folder / "claude-hr-ctl.exe").read_bytes() == b"MZ the launcher of claude-hr-ctl"
    for left in ("claude-hr-ctl.exe.refused", "claude-hr-ctl.cmd", "claude-hr-ctl"):
        assert not (folder / left).exists(), left
    assert launchers.refused_names() == [] and not launchers.is_shimmed("claude-hr-ctl")


def test_looking_again_at_a_launcher_that_is_still_refused_changes_nothing(tmp_path):
    folder = make_bin(tmp_path)
    look(folder, {"claude-hr-ctl": REFUSED})
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    out = look(folder, {"claude-hr-ctl": REFUSED})
    assert out.ok
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert launchers.is_shimmed("claude-hr-ctl")


def test_a_new_launcher_after_a_reinstall_is_looked_at_and_replaces_the_old_aside(tmp_path):
    """`uv tool install --reinstall` writes claude-hr-ctl.exe again; it shadows the stand-in until the next look."""
    folder = make_bin(tmp_path)
    look(folder, {"claude-hr-ctl": REFUSED})
    (folder / "claude-hr-ctl.exe").write_bytes(b"MZ the NEW launcher")
    out = look(folder, {"claude-hr-ctl": REFUSED})
    assert out.ok and out.changed
    assert not (folder / "claude-hr-ctl.exe").exists()
    assert (folder / "claude-hr-ctl.exe.refused").read_bytes() == b"MZ the NEW launcher"
    assert (folder / "claude-hr-ctl.cmd").is_file()


def test_other_trouble_is_not_a_refusal_and_nothing_is_moved(tmp_path):
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    out = look(
        folder, {"claude-hr-ctl": Probe(False, False, 193, "%1 is not a valid Win32 application")}
    )
    assert not out.ok and not out.refused and not out.changed
    assert (folder / "claude-hr-ctl.exe").is_file() and len(list(folder.iterdir())) == 1
    assert "left as it is" in " ".join(out.lines)
    assert launchers.refused_names() == []


def test_other_trouble_with_a_launcher_that_was_looked_at_again_puts_it_back_aside(tmp_path):
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    look(folder, {"claude-hr-ctl": REFUSED})
    look(folder, {"claude-hr-ctl": Probe(False, False, 32, "being used by another process")})
    assert not (folder / "claude-hr-ctl.exe").exists()  # the stand-in is not hidden by it
    assert (folder / "claude-hr-ctl.exe.refused").is_file() and (
        folder / "claude-hr-ctl.cmd"
    ).is_file()


def test_a_file_that_is_not_ours_is_never_overwritten_and_the_launcher_stays(tmp_path):
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    (folder / "claude-hr-ctl.cmd").write_bytes(b"@echo mine\r\n")
    out = look(folder, {"claude-hr-ctl": REFUSED})
    assert not out.ok
    assert (folder / "claude-hr-ctl.cmd").read_bytes() == b"@echo mine\r\n"
    assert (folder / "claude-hr-ctl.exe").is_file() and not (folder / "claude-hr-ctl").exists()
    assert "was not made by claude-hr" in " ".join(out.lines)
    assert not launchers.is_shimmed("claude-hr-ctl")


def test_a_launcher_that_cannot_be_moved_is_reported_and_is_not_called_shimmed(
    tmp_path, monkeypatch
):
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    real = os.replace

    def deny(src, dst, *a, **k):
        if str(src).endswith("claude-hr-ctl.exe"):
            raise PermissionError(5, "Access is denied")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", deny)
    out = look(folder, {"claude-hr-ctl": REFUSED})
    assert not out.ok and "could not be moved aside" in " ".join(out.lines)
    assert launchers.refused_names() == ["claude-hr-ctl"] and not launchers.is_shimmed(
        "claude-hr-ctl"
    )


def test_a_python_path_that_cmd_cannot_read_gets_no_cmd_stand_in(tmp_path):
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    out = look(folder, {"claude-hr-ctl": REFUSED}, python=r"C:\Users\José\Scripts\python.exe")
    assert not out.ok and "cmd.exe cannot read" in " ".join(out.lines)
    assert (folder / "claude-hr-ctl.exe").is_file() and not (folder / "claude-hr-ctl.cmd").exists()


def test_a_dry_run_looks_and_says_but_changes_nothing(tmp_path):
    folder = make_bin(tmp_path)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    out = look(folder, {"claude-hr-ctl": REFUSED}, dry=True)
    assert "would move it aside" in " ".join(out.lines) and list(out.refused) == ["claude-hr-ctl"]
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert not launchers.state_file().exists()


def test_a_dry_run_also_looks_at_a_launcher_that_was_moved_aside(tmp_path):
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    look(folder, {"claude-hr-ctl": REFUSED})
    seen: list = []
    out = ensure(
        bin_dir=folder,
        python=PY,
        probe_fn=fake_probe({"claude-hr-ctl": STARTS}, seen),
        waits=(),
        dry=True,
    )
    assert seen and seen[0][1] == "claude-hr-ctl.exe.refused"
    assert not (folder / "claude-hr-ctl.exe").exists() and out.ok  # nothing was put back


def test_a_folder_without_launchers_has_nothing_to_check(tmp_path):
    out = look(tmp_path / "empty", {})
    assert out.ok and "nothing to check" in " ".join(out.lines)


def test_ensure_never_raises(tmp_path):
    folder = make_bin(tmp_path, ("claude-hr-ctl",))

    def boom(path, args):
        raise RuntimeError("the probe broke")

    out = ensure(bin_dir=folder, python=PY, probe_fn=boom, waits=())
    assert not out.ok and "could not check the launchers: the probe broke" in " ".join(out.lines)


def test_two_looks_at_once_do_not_both_move_files(tmp_path, monkeypatch):
    from claude_hr.filelock import LockTimeout

    folder = make_bin(tmp_path, ("claude-hr-ctl",))

    def busy(*a, **k):
        raise LockTimeout("held")

    monkeypatch.setattr(launchers, "locked", busy)
    out = look(folder, {"claude-hr-ctl": REFUSED})
    assert "another claude-hr process is checking" in " ".join(out.lines)
    assert (folder / "claude-hr-ctl.exe").is_file()


def test_remove_gives_the_launchers_their_names_back_and_leaves_foreign_files(tmp_path):
    folder = make_bin(tmp_path)
    look(folder, {n: REFUSED for n in LAUNCHERS})
    (folder / "claude-hr-hook").write_text(
        "#!/bin/sh\necho mine\n"
    )  # somebody replaced our script with their own
    lines = remove(bin_dir=folder)
    assert (folder / "claude-hr.exe").is_file() and (folder / "claude-hr-ctl.exe").is_file()
    assert not (folder / "claude-hr.cmd").exists() and not (folder / "claude-hr-ctl").exists()
    assert (
        folder / "claude-hr-hook"
    ).read_text() == "#!/bin/sh\necho mine\n"  # no marker: not ours, so it stays
    assert any("claude-hr-ctl.exe is back" in ln for ln in lines)
    assert launchers.refused_names() == []


# ---- what is written -------------------------------------------------------------------------------------------------------
def test_the_cmd_stand_in_has_crlf_endings_and_the_last_line_that_keeps_ctrl_c_working(tmp_path):
    folder = make_bin(tmp_path, ("claude-hr",))
    look(folder, {"claude-hr": REFUSED})
    raw = (folder / "claude-hr.cmd").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert (
        raw.decode("ascii").splitlines()[-1].startswith("endlocal & goto #_undefined_# 2>NUL || ")
    )


def test_the_shell_stand_in_has_lf_endings(tmp_path):
    folder = make_bin(tmp_path, ("claude-hr",))
    look(folder, {"claude-hr": REFUSED})
    raw = (folder / "claude-hr").read_bytes()
    assert raw.startswith(b"#!/bin/sh\n") and b"\r" not in raw


def test_the_cmd_text_keeps_cmd_metacharacters_out_of_its_remarks():
    """cmd.exe reads %, ^ and & even in a remark (a trailing ^ continues a remark onto the next line)."""
    text = render_cmd(PY, "claude_hr.ctl")
    for line in text.splitlines():
        if line.startswith("rem "):
            assert not set("%^&|<>") & set(line), line
    assert text.splitlines()[0] == "@echo off" and "setlocal DisableDelayedExpansion" in text


def test_a_percent_sign_in_the_python_path_is_doubled_for_cmd():
    assert '"C:\\100%%\\python.exe" -I -m claude_hr.ctl %*' in render_cmd(
        "C:\\100%\\python.exe", "claude_hr.ctl"
    )


def test_the_shell_text_round_trips_through_a_shell_even_with_quotes_in_the_path():
    python = "C:\\Users\\it's a name\\python.exe"
    line = render_sh(python, "claude_hr.hooks").splitlines()[-1]
    assert shlex.split(line) == [
        "exec",
        "C:/Users/it's a name/python.exe",
        "-I",
        "-m",
        "claude_hr.hooks",
        "$@",
    ]


def test_isolated_mode_keeps_the_current_directory_off_the_import_path():
    """`python -m` puts the current directory first; a launcher runs in whatever repository the person is in."""
    assert " -I -m " in render_cmd(PY, "m") and " -I -m " in render_sh(PY, "m")


# ---- where it plugs in -----------------------------------------------------------------------------------------------------
def test_the_hook_module_runs_with_dash_m_which_is_how_its_stand_in_starts_it():
    src = str(Path(__file__).resolve().parents[1] / "src")
    r = subprocess.run(
        [sys.executable, "-m", "claude_hr.hooks"],
        env=dict(os.environ, PYTHONPATH=src),
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 2 and "usage: claude-hr-hook" in r.stderr


def test_setup_has_a_launchers_step_that_runs_first_on_windows_only(sandbox, monkeypatch, capsys):
    assert ctl.ALL_STEPS[0] == "launchers"
    seen: list = []

    def fake_ensure(**kw):
        seen.append(kw)
        return launchers.Outcome(
            ok=True, changed=False, lines=["claude-hr, claude-hr-ctl start normally"]
        )

    monkeypatch.setattr(launchers, "ensure", fake_ensure)
    monkeypatch.setattr(paths, "ensure_dirs", lambda: None)
    monkeypatch.setattr(paths, "IS_WINDOWS", False)
    assert ctl.main(["setup", "--only", "launchers", "--dry-run"]) == 0
    assert seen == [] and "launchers" not in capsys.readouterr().out
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    assert ctl.main(["setup", "--only", "launchers", "--dry-run"]) == 0
    assert seen == [{"dry": True}]
    assert "[ok ] launchers" in capsys.readouterr().out


def test_a_launchers_step_that_could_not_fix_a_refused_launcher_counts_as_failed(
    sandbox, monkeypatch, capsys
):
    monkeypatch.setattr(paths, "ensure_dirs", lambda: None)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(
        launchers,
        "ensure",
        lambda **kw: launchers.Outcome(ok=False, lines=["claude-hr-ctl: no stand-in is in place"]),
    )
    assert ctl.main(["setup", "--only", "launchers", "--dry-run"]) == 1
    assert "[ERR] launchers" in capsys.readouterr().out


def test_the_fallback_can_be_switched_off(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_HR_LAUNCHERS__FALLBACK", "false")
    monkeypatch.setattr(paths, "ensure_dirs", lambda: None)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(
        launchers, "ensure", lambda **kw: pytest.fail("the fallback is switched off")
    )
    assert ctl.main(["setup", "--only", "launchers", "--dry-run"]) == 0


def test_headroom_is_not_downloaded_where_launchers_are_refused(sandbox, monkeypatch, capsys):
    """headroom.exe comes from the same tool and is refused the same way; the download is large."""
    monkeypatch.setattr(paths, "ensure_dirs", lambda: None)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(launchers, "refused_names", lambda: ["claude-hr-ctl"])
    monkeypatch.setattr(Headroom, "version", lambda self: None)
    monkeypatch.setattr(Headroom, "binary", lambda self: None)
    monkeypatch.setattr(
        Headroom, "install", lambda self, *a, **k: pytest.fail("Headroom must not be installed")
    )
    assert ctl.main(["setup", "--only", "headroom"]) == 0
    assert "skipped install: Windows refuses launcher programs" in capsys.readouterr().out


def test_headroom_is_installed_as_before_where_nothing_is_refused(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(paths, "ensure_dirs", lambda: None)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(launchers, "refused_names", lambda: [])
    calls: list = []
    monkeypatch.setattr(Headroom, "version", lambda self: None)
    monkeypatch.setattr(Headroom, "binary", lambda self: None)
    monkeypatch.setattr(
        Headroom,
        "install",
        lambda self, *a, **k: calls.append(1) or subprocess.CompletedProcess([], 1, "", "no"),
    )
    ctl.main(["setup", "--only", "headroom"])
    assert calls == [1]


def test_a_headroom_that_windows_refuses_is_reported_and_does_not_raise(sandbox, monkeypatch):
    hr = Headroom(config.load())
    monkeypatch.setattr(Headroom, "binary", lambda self: Path("headroom.exe"))
    monkeypatch.setattr(Headroom, "health", lambda self, timeout=0.6: Health(False))

    def refuse(self):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Headroom, "start", refuse)
    monkeypatch.setattr("claude_hr.procutil.port_listening", lambda host, port: False)
    h = hr.ensure(wait_s=0)
    assert not h.ok and h.status == "refused" and "Access is denied" in h.detail


def test_the_status_line_names_the_hook_when_its_launcher_is_replaced(
    sandbox, tmp_path, monkeypatch
):
    from claude_hr import procutil
    from claude_hr.claude_setup import ClaudeSetup

    exe = tmp_path / "claude-hr-hook.exe"
    exe.write_bytes(b"MZ")
    setup = ClaudeSetup(config.load(), Headroom(config.load()), None)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(procutil, "which_all", lambda name, path=None: [exe])
    monkeypatch.setattr(launchers, "is_shimmed", lambda name: name == "claude-hr-hook")
    assert (
        setup.statusline_command() == "claude-hr-hook statusline"
    )  # by name: the stand-ins are found that way
    monkeypatch.setattr(launchers, "is_shimmed", lambda name: False)
    assert setup.statusline_command().endswith(
        "claude-hr-hook.exe statusline"
    )  # as before: the path of the launcher


def test_plain_claude_gets_no_cmd_stand_in(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(launchers, "is_shimmed", lambda name: name == "claude-hr")
    assert ctl.main(["shim", "install"]) == 1
    out = capsys.readouterr().out
    assert "Windows refuses claude-hr.exe" in out and "cmd.exe" in out


def test_the_updater_looks_at_the_launchers_after_a_reinstall_but_not_in_a_preview(
    sandbox, monkeypatch
):
    from claude_hr import updater

    calls: list = []
    monkeypatch.setattr(launchers, "ensure", lambda **kw: calls.append(kw) or launchers.Outcome())
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    updater.Updater(config.load())._look_at_launchers()
    assert calls == [{}]
    updater.Updater(config.load(), dry=True)._look_at_launchers()
    assert calls == [{}]
    monkeypatch.setenv("CLAUDE_HR_LAUNCHERS__FALLBACK", "false")
    updater.Updater(config.load())._look_at_launchers()
    assert calls == [{}]


def test_the_updater_survives_a_look_that_fails(sandbox, monkeypatch):
    from claude_hr import updater

    def boom(**kw):
        raise RuntimeError("no")

    monkeypatch.setattr(launchers, "ensure", boom)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    updater.Updater(config.load())._look_at_launchers()  # must not raise


def test_doctor_is_quiet_when_nothing_is_refused_and_says_what_to_do_when_something_is(tmp_path):
    folder = make_bin(tmp_path)
    assert doctor.launcher_checks() == []
    look(folder, {"claude-hr-ctl": REFUSED})
    [check] = doctor.launcher_checks()
    assert check.name == "launchers" and check.status == "warn"
    assert "claude-hr-ctl" in check.detail and "stand-ins" in check.detail
    assert str(folder) in check.fix and "launchers ensure" in check.fix
    # an update writes a new claude-hr-ctl.exe, which wins the lookup over the stand-in: doctor says so
    (folder / "claude-hr-ctl.exe").write_bytes(b"MZ new")
    [check] = doctor.launcher_checks()
    assert "missing or hidden" in check.detail and check.fix == "claude-hr-ctl launchers ensure"


def test_the_launchers_command_does_nothing_off_windows(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(paths, "IS_WINDOWS", False)
    assert ctl.main(["launchers"]) == 0
    assert "only refused on Windows" in capsys.readouterr().out


def test_the_launchers_command_looks_by_default_and_fixes_only_when_told(
    sandbox, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    seen: list = []
    monkeypatch.setattr(
        launchers, "ensure", lambda **kw: seen.append(kw) or launchers.Outcome(lines=["x"])
    )
    assert ctl.main(["launchers"]) == 0
    assert ctl.main(["launchers", "status"]) == 0
    assert ctl.main(["launchers", "ensure"]) == 0
    assert seen == [{"dry": True}, {"dry": True}, {"dry": False}]
    monkeypatch.setattr(
        launchers, "remove", lambda **kw: ["claude-hr-ctl: claude-hr-ctl.exe is back"]
    )
    assert ctl.main(["launchers", "remove"]) == 0
    assert "stand-ins removed" in capsys.readouterr().out


def test_uninstall_purge_takes_the_stand_ins_away_but_a_plain_uninstall_keeps_them():
    """The stand-ins belong to the tool, which a plain uninstall (managed config only) leaves installed."""
    import inspect

    src = inspect.getsource(ctl.cmd_uninstall)
    assert src.count("launchers.remove()") == 1
    assert src.index("if a.purge:") < src.index("launchers.remove()")


def test_state_file_is_valid_json_the_doctor_and_setup_can_read(tmp_path):
    folder = make_bin(tmp_path)
    look(folder, {"claude-hr-ctl": REFUSED})
    data = json.loads(launchers.state_file().read_text(encoding="utf-8"))
    assert data["names"]["claude-hr-ctl"] == {
        "refused": True,
        "shim": True,
        "code": 5,
        "message": "Access is denied",
    }
    assert data["names"]["claude-hr"] == {"refused": False, "shim": False}
    assert data["bin"] == str(folder)


def test_a_damaged_state_file_reads_as_nothing_refused():
    launchers.state_file().parent.mkdir(parents=True, exist_ok=True)
    launchers.state_file().write_text("[1, 2, 3]", encoding="utf-8")
    assert launchers.refused_names() == [] and not launchers.is_shimmed("claude-hr-ctl")
    launchers.state_file().write_text('{"names": {"claude-hr-ctl": "x"}}', encoding="utf-8")
    assert launchers.refused_names() == []


def test_default_bin_dir_follows_uv(monkeypatch, tmp_path):
    for var in ("UV_TOOL_BIN_DIR", "XDG_BIN_HOME"):
        monkeypatch.delenv(var, raising=False)
    assert launchers.default_bin_dir() == paths.home() / ".local" / "bin"
    monkeypatch.setenv("XDG_BIN_HOME", str(tmp_path / "xdg"))
    assert launchers.default_bin_dir() == tmp_path / "xdg"
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(tmp_path / "uv"))
    assert launchers.default_bin_dir() == tmp_path / "uv"


# ---- Windows: files that Windows really refuses ------------------------------------------------------------------------------
pytestmark_windows = pytest.mark.skipif(
    not WINDOWS, reason="needs Windows' own refusal to start a file"
)


def _real_program(folder: Path, name: str) -> Path:
    target = folder / name
    shutil.copy2(
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe", target
    )
    return target


@pytest.fixture
def deny():
    """deny(path): an execute-deny rule on the file, which gives the "Access is denied" of an endpoint product."""
    denied: list[Path] = []
    user = os.environ.get("USERNAME", "")

    def apply(path: Path) -> None:
        subprocess.run(
            ["icacls", str(path), "/deny", f"{user}:(X)"], check=True, capture_output=True
        )
        denied.append(path)

    yield apply
    for path in denied:
        subprocess.run(["icacls", str(path), "/remove:d", user], capture_output=True)


@pytestmark_windows
def test_windows_really_refuses_a_file_and_probe_says_so(tmp_path, deny):
    refused = _real_program(tmp_path, "refused.exe")
    deny(refused)
    allowed = _real_program(tmp_path, "allowed.exe")
    got = launchers.probe(refused, ["--version"])
    assert not got.ok and got.refused and got.code == 5 and "denied" in got.message.lower()
    assert launchers.probe(allowed, ["--version"]).ok  # it starts and fails: still a yes
    gone = launchers.probe(tmp_path / "missing.exe", [])
    assert not gone.ok and not gone.refused and gone.code == 2  # not found is not a refusal


@pytestmark_windows
def test_windows_really_refuses_one_launcher_and_the_whole_cycle_works(tmp_path, deny):
    folder = tmp_path / "bin"
    folder.mkdir()
    for name in LAUNCHERS:
        _real_program(folder, f"{name}.exe")
    deny(folder / "claude-hr-ctl.exe")
    out = ensure(bin_dir=folder, python=sys.executable, waits=())
    assert out.ok and out.changed and list(out.refused) == ["claude-hr-ctl"]
    assert (folder / "claude-hr-ctl.exe.refused").is_file() and (
        folder / "claude-hr-ctl.cmd"
    ).is_file()
    assert (folder / "claude-hr.exe").is_file() and (folder / "claude-hr-hook.exe").is_file()
    # the same look again: nothing to do
    assert not ensure(bin_dir=folder, python=sys.executable, waits=()).changed
    # the block is lifted: the launcher gets its name back, the stand-ins go
    subprocess.run(
        ["icacls", str(folder / "claude-hr-ctl.exe.refused"), "/remove:d", os.environ["USERNAME"]],
        check=True,
        capture_output=True,
    )
    out = ensure(bin_dir=folder, python=sys.executable, waits=())
    assert out.ok and out.changed
    assert (folder / "claude-hr-ctl.exe").is_file() and not (folder / "claude-hr-ctl.cmd").exists()


@pytestmark_windows
def test_the_cmd_stand_in_runs_python_with_the_arguments_exit_code_and_input_it_was_given(tmp_path):
    stand_in = tmp_path / "zipfile-tool.cmd"
    stand_in.write_bytes(
        render_cmd(sys.executable, "zipfile").replace("\n", "\r\n").encode("ascii")
    )
    r = subprocess.run(
        [str(stand_in), "-l", "no such.zip"], capture_output=True, text=True, timeout=60
    )
    assert (
        r.returncode == 1 and "no such.zip" in r.stderr
    )  # the argument with a space arrived whole, the exit code came back
    jt = tmp_path / "json-tool.cmd"
    jt.write_bytes(render_cmd(sys.executable, "json.tool").replace("\n", "\r\n").encode("ascii"))
    r = subprocess.run([str(jt)], input='{"a":1}', capture_output=True, text=True, timeout=60)
    assert (
        r.returncode == 0 and '"a": 1' in r.stdout
    )  # stdin goes through (hooks are fed JSON on it)


@pytestmark_windows
def test_the_cmd_stand_in_works_from_powershell_and_returns_the_exit_code(tmp_path):
    stand_in = tmp_path / "zipfile-tool.cmd"
    stand_in.write_bytes(
        render_cmd(sys.executable, "zipfile").replace("\n", "\r\n").encode("ascii")
    )
    ps = f"& '{stand_in}' -l 'no such.zip' 2>&1 | Out-String | Write-Output; exit $LASTEXITCODE"
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 1 and "no such.zip" in r.stdout


@pytestmark_windows
def _git_bash() -> Path | None:
    """Git for Windows' bash (not WSL's bash.exe, which does not see Windows paths): next to git, a few folders up."""
    git = shutil.which("git")
    candidates = [p / "bin" / "bash.exe" for p in Path(git).parents] if git else []
    candidates.append(
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
    )
    return next((c for c in candidates if c.is_file()), None)


@pytestmark_windows
def test_the_shell_stand_in_runs_in_git_bash_with_its_arguments_exactly(tmp_path):
    bash = _git_bash()
    if bash is None:
        pytest.skip("Git Bash not found")
    stand_in = tmp_path / "zipfile-tool"
    stand_in.write_bytes(render_sh(sys.executable, "zipfile").encode("ascii"))
    r = subprocess.run(
        [str(bash), str(stand_in), "-l", "a&b %PATH% ^c.zip"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (
        r.returncode == 1 and "a&b %PATH% ^c.zip" in r.stderr
    )  # no cmd.exe on the way: nothing in the argument was read


@pytestmark_windows
def test_a_refused_launcher_is_found_by_name_through_the_stand_in(tmp_path, deny, monkeypatch):
    """The point of it all: with claude-hr-ctl.exe refused, the NAME claude-hr-ctl still starts something."""
    folder = tmp_path / "bin"
    folder.mkdir()
    refused = _real_program(folder, "claude-hr-ctl.exe")
    deny(refused)
    ensure(bin_dir=folder, python=sys.executable, waits=())
    # point the stand-in at a module that is importable under -I, so the test does not need claude-hr installed
    (folder / "claude-hr-ctl.cmd").write_bytes(
        render_cmd(sys.executable, "zipfile").replace("\n", "\r\n").encode("ascii")
    )
    monkeypatch.setenv("PATH", f"{folder};{os.environ['PATH']}")
    ps = "Get-Command claude-hr-ctl | Select-Object -ExpandProperty Source"
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=120
    )
    assert r.stdout.strip().lower().endswith("claude-hr-ctl.cmd")
    r = subprocess.run(
        ["cmd", "/c", "claude-hr-ctl", "-l", "x.zip"], capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 1 and "x.zip" in r.stderr

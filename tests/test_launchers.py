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


def test_the_launcher_moved_aside_is_deleted_when_a_new_one_starts_in_its_place(tmp_path):
    """`uv tool install --reinstall` after the cause was fixed: the new launcher starts, the old one is nobody's."""
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    look(folder, {"claude-hr-ctl": REFUSED})
    (folder / "claude-hr-ctl.exe").write_bytes(b"MZ the NEW launcher")
    out = look(folder, {"claude-hr-ctl": STARTS})
    assert out.ok and out.changed
    assert (folder / "claude-hr-ctl.exe").read_bytes() == b"MZ the NEW launcher"
    for left in ("claude-hr-ctl.exe.refused", "claude-hr-ctl.cmd", "claude-hr-ctl"):
        assert not (folder / left).exists(), left
    assert "the old claude-hr-ctl.exe.refused is deleted" in " ".join(out.lines)


def test_a_file_that_no_look_moved_aside_is_never_deleted(tmp_path):
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    (folder / "claude-hr-ctl.exe.refused").write_bytes(b"somebody's own file")
    look(folder, {"claude-hr-ctl": STARTS})
    assert (folder / "claude-hr-ctl.exe.refused").read_bytes() == b"somebody's own file"


def test_a_dry_run_deletes_no_old_launcher(tmp_path):
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    look(folder, {"claude-hr-ctl": REFUSED})
    (folder / "claude-hr-ctl.exe").write_bytes(b"MZ the NEW launcher")
    look(folder, {"claude-hr-ctl": STARTS}, dry=True)
    assert (folder / "claude-hr-ctl.exe.refused").is_file()


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


def test_a_stand_in_that_windows_refuses_too_is_reported_and_the_step_fails(tmp_path):
    """A Deny rule that new files inherit from the folder, or an AppLocker script rule, refuses the .cmd as well."""
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    out = look(folder, {"claude-hr-ctl": REFUSED, "claude-hr-ctl.cmd": REFUSED})
    assert not out.ok
    text = " ".join(out.lines)
    assert "refuses the stand-in claude-hr-ctl.cmd too (error 5: Access is denied)" in text
    assert (
        "PowerShell and cmd.exe cannot start claude-hr-ctl" in text
        and "still starts it in Git Bash" in text
    )
    assert launchers.state()["names"]["claude-hr-ctl"]["standin_refused"] is True
    assert (folder / "claude-hr-ctl").is_file(), "the shell script is there for Git Bash"
    [check] = doctor.launcher_checks()
    assert "also the .cmd stand-in" in check.detail and "Git Bash" in check.detail


def test_a_stand_in_that_starts_is_not_mentioned_and_the_script_only_setting_probes_no_cmd(
    tmp_path,
):
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    log: list = []
    ensure(
        bin_dir=folder, python=PY, probe_fn=fake_probe({"claude-hr-ctl": REFUSED}, log), waits=()
    )
    assert [entry[1] for entry in log].count("claude-hr-ctl.cmd") == 1, (
        "the stand-in is started once to see that it can be"
    )
    assert launchers.state()["names"]["claude-hr-ctl"]["standin_refused"] is False
    (tmp_path / "two").mkdir()
    folder2 = make_bin(tmp_path / "two", ("claude-hr-ctl",))
    log2: list = []
    ensure(
        bin_dir=folder2,
        python=PY,
        probe_fn=fake_probe({"claude-hr-ctl": REFUSED}, log2),
        waits=(),
        cmd=False,
    )
    assert "claude-hr-ctl.cmd" not in [entry[1] for entry in log2]


def test_a_python_path_that_cmd_cannot_read_gets_no_cmd_stand_in(tmp_path):
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    out = look(folder, {"claude-hr-ctl": REFUSED}, python=r"C:\Users\José\Scripts\python.exe")
    assert not out.ok and "cmd.exe cannot read" in " ".join(out.lines)
    assert (folder / "claude-hr-ctl.exe").is_file() and not (folder / "claude-hr-ctl.cmd").exists()


def test_a_python_path_with_an_exclamation_mark_gets_no_cmd_stand_in(tmp_path):
    """Where delayed expansion is on (a registry value, a calling batch file), cmd.exe would read !...! in the batch file."""
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    out = look(folder, {"claude-hr-ctl": REFUSED}, python=r"C:\Users\Hi!\Scripts\python.exe")
    assert not out.ok and "exclamation mark" in " ".join(out.lines)
    assert (folder / "claude-hr-ctl.exe").is_file() and not (folder / "claude-hr-ctl.cmd").exists()


def test_a_launcher_that_was_waited_for_before_is_refused_once_not_four_times(tmp_path):
    """Each refused start is an event for whoever watches the PC, and the name is a failing program while it is looked at."""
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    look(folder, {"claude-hr-ctl": REFUSED})
    slept: list[float] = []
    log: list = []
    out = ensure(
        bin_dir=folder,
        python=PY,
        probe_fn=fake_probe({"claude-hr-ctl": REFUSED}, log),
        sleep=slept.append,
    )
    assert slept == [] and [entry[1] for entry in log].count("claude-hr-ctl.exe") == 1
    assert out.ok and launchers.is_shimmed("claude-hr-ctl")
    # a NEW launcher (a reinstall) has not been waited for: it still gets the full look
    (folder / "claude-hr-ctl.exe").write_bytes(b"MZ the NEW launcher")
    ensure(
        bin_dir=folder,
        python=PY,
        probe_fn=fake_probe({"claude-hr-ctl": REFUSED}),
        sleep=slept.append,
    )
    assert slept == [2.0, 4.0, 6.0]


def test_a_python_that_is_not_a_uv_tool_environment_gets_no_stand_ins_written_for_it(
    tmp_path, monkeypatch
):
    """A stand-in runs whatever its Python finds: a checkout under development, or a venv of a branch under review, must not
    become claude-hr for every command and hook."""
    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    monkeypatch.setattr(launchers, "running_from_tool_environment", lambda: False)
    out = ensure(bin_dir=folder, probe_fn=fake_probe({"claude-hr-ctl": REFUSED}), waits=())
    assert "is not the Python of a uv tool environment" in " ".join(out.lines)
    assert [p.name for p in folder.iterdir()] == ["claude-hr-ctl.exe"]
    assert not launchers.state_file().exists()
    monkeypatch.setattr(launchers, "running_from_tool_environment", lambda: True)
    monkeypatch.setattr(launchers, "console_python", lambda: PY)
    ensure(bin_dir=folder, probe_fn=fake_probe({"claude-hr-ctl": REFUSED}), waits=())
    assert (folder / "claude-hr-ctl.cmd").is_file()


def test_a_tool_environment_is_told_by_its_uv_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert not launchers.running_from_tool_environment()
    (tmp_path / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    assert launchers.running_from_tool_environment()


def test_the_sandbox_does_not_see_where_uv_puts_launchers(sandbox):
    """The shell of a developer may export UV_TOOL_BIN_DIR or XDG_BIN_HOME: a test must not follow it to the real launchers
    (tests/conftest.py clears them; stand-ins are besides only written by the Python of a uv tool environment)."""
    assert not {"UV_TOOL_BIN_DIR", "XDG_BIN_HOME", "UV_TOOL_DIR"} & set(os.environ)
    assert launchers.default_bin_dir() == sandbox / ".local" / "bin"


def test_only_the_first_lines_of_a_file_say_whether_it_is_ours(tmp_path):
    ours, late = tmp_path / "a.cmd", tmp_path / "b.cmd"
    ours.write_text(f"@echo off\nrem {MARKER}\n", encoding="ascii")
    late.write_text("x" * 700 + MARKER, encoding="ascii")
    assert launchers._is_ours(ours)
    assert not launchers._is_ours(late) and not launchers._is_ours(tmp_path / "missing.cmd")


def test_the_status_line_runs_python_by_the_path_the_last_look_recorded(
    sandbox, tmp_path, monkeypatch
):
    python = tmp_path / "tool" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"MZ")
    monkeypatch.setattr(
        launchers, "console_python", lambda: r"C:\dev\checkout\.venv\Scripts\python.exe"
    )
    assert launchers.stand_in_python() == r"C:\dev\checkout\.venv\Scripts\python.exe", (
        "nothing recorded yet"
    )
    launchers.write_json(launchers.state_file(), {"python": str(python), "names": {}})
    assert launchers.stand_in_python() == str(python)
    python.unlink()  # a recorded Python that is gone is no Python to name
    assert launchers.stand_in_python() == r"C:\dev\checkout\.venv\Scripts\python.exe"


def test_remove_says_so_when_another_process_is_looking(tmp_path, monkeypatch):
    from claude_hr.filelock import LockTimeout

    folder = make_bin(tmp_path, ("claude-hr-ctl",))
    look(folder, {"claude-hr-ctl": REFUSED})

    def busy(*a, **k):
        raise LockTimeout("held")

    monkeypatch.setattr(launchers, "locked", busy)
    assert "try again" in " ".join(remove(bin_dir=folder))
    assert (folder / "claude-hr-ctl.cmd").is_file(), "nothing was touched"


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


# ---- the .cmd stand-ins are a choice ---------------------------------------------------------------------------------------
def test_the_cmd_stand_ins_can_be_left_out_and_only_the_shell_scripts_are_written(tmp_path):
    folder = make_bin(tmp_path)
    out = look(folder, {n: REFUSED for n in LAUNCHERS}, cmd=False)
    assert out.ok and out.changed
    for name in LAUNCHERS:
        assert (folder / name).is_file() and (folder / f"{name}.exe.refused").is_file()
        assert not (folder / f"{name}.cmd").exists() and not (folder / f"{name}.exe").exists()
        assert launchers.is_shimmed(name)
        assert launchers.state()["names"][name]["cmd"] is False
    text = " ".join(out.lines)
    assert "a shell script: Git Bash only" in text
    assert "hand their arguments to cmd.exe" not in text, "no .cmd, nothing to warn about"


def test_switching_the_cmd_stand_ins_off_removes_the_ones_written_before(tmp_path):
    folder = make_bin(tmp_path, ("claude-hr",))
    look(folder, {"claude-hr": REFUSED})
    assert (folder / "claude-hr.cmd").is_file()
    look(folder, {"claude-hr": REFUSED}, cmd=False)
    assert not (folder / "claude-hr.cmd").exists() and (folder / "claude-hr").is_file()
    look(folder, {"claude-hr": REFUSED}, cmd=True)
    assert (folder / "claude-hr.cmd").is_file()


def test_the_cmd_stand_ins_come_with_a_warning_about_cmd_exe_that_says_what_to_do(tmp_path):
    folder = make_bin(tmp_path, ("claude-hr",))
    out = look(folder, {"claude-hr": REFUSED})
    text = " ".join(out.lines)
    assert launchers.CMD_WARNING in text
    assert "acts on & | < > and % ^ ! in them and drops everything after a line break" in text
    assert "never pass them text from an untrusted source" in text
    assert f"{PY} -I -m claude_hr.launcher" in text, "a script is told how to call Python directly"
    assert "launchers.cmd false" in text


def test_the_cmd_stand_ins_are_on_by_default_and_the_default_is_documented_in_the_config():
    cfg = config.load()
    assert cfg.flag("launchers.fallback") is True and cfg.flag("launchers.cmd") is True
    text = (paths.package_data_dir() / "defaults.toml").read_text(encoding="utf-8")
    assert "acts on & | < > and % ^ ! in them" in text and "cmd = true" in text


def test_setup_passes_the_cmd_setting_on(sandbox, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_HR_LAUNCHERS__CMD", "false")
    seen: list = []
    monkeypatch.setattr(launchers, "ensure", lambda **kw: seen.append(kw) or launchers.Outcome())
    monkeypatch.setattr(paths, "ensure_dirs", lambda: None)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    assert ctl.main(["setup", "--only", "launchers", "--dry-run"]) == 0
    assert seen == [{"dry": True, "cmd": False}]


def test_doctor_tells_which_kind_of_stand_ins_there_are(tmp_path):
    folder = make_bin(tmp_path)
    look(folder, {"claude-hr-ctl": REFUSED})
    [check] = doctor.launcher_checks()
    assert (
        "hand their arguments to cmd.exe" in check.detail
        and "launchers.cmd = false" in check.detail
    )
    look(folder, {"claude-hr-ctl": REFUSED}, cmd=False)
    [check] = doctor.launcher_checks()
    assert (
        "only as shell scripts for Git Bash" in check.detail
        and "no claude-hr command" in check.detail
    )


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
    assert seen == [{"dry": True, "cmd": True}]
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
    monkeypatch.setattr(launchers, "stand_in_python", lambda: r"C:\tool\Scripts\python.exe")
    # Python by absolute path, isolated: a bare name would be looked up in the folder of the session first by cmd.exe, and a
    # repository could put a claude-hr-hook.bat there
    assert (
        setup.statusline_command()
        == '"C:/tool/Scripts/python.exe" -I -m claude_hr.hooks statusline'
    )
    monkeypatch.setattr(launchers, "is_shimmed", lambda name: False)
    assert setup.statusline_command().endswith(
        "claude-hr-hook.exe statusline"
    )  # as before: the path of the launcher


@pytest.fixture
def stand_ins_in_use(monkeypatch):
    """A Windows PC where every launcher of claude-hr runs as a .cmd stand-in."""
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(launchers, "is_shimmed", lambda name: True)
    monkeypatch.setattr(launchers, "stand_in_python", lambda: r"C:\tool\Scripts\python.exe")
    monkeypatch.setattr(launchers, "console_python", lambda: r"C:\tool\Scripts\python.exe")


@pytest.mark.parametrize(
    "command",
    [
        "claude-hr-ctl wt new 'x&calc'",
        'claude-hr-ctl wt new "x|y"',
        "claude-hr-ctl wt new 'a<b'",
        "claude-hr-ctl wt new fix-%GITHUB_TOKEN%",
        "claude-hr-ctl wt new a^b",
        "claude-hr-ctl wt new 'a!b'",
        "claude-hr-ctl wt new \u2018x&calc\u2019",  # typographic quotes: still one argument to PowerShell
        "claude-hr-ctl run 'it''s & that'",
        "& 'C:\\Users\\x\\.local\\bin\\claude-hr-ctl.cmd' 'x&calc'",
        "cd repo; claude-hr 'a>b'",
        'CLAUDE-HR-HOOK "a<b"',
        # a backtick-escaped character still reaches cmd.exe as that character; `n `r `u{26} make others
        "claude-hr-ctl wt new x`&calc",
        'claude-hr-ctl wt new "x`&calc"',
        "claude-hr-ctl wt new a`|b",
        "claude-hr-ctl wt new `%literal",
        'claude-hr-ctl wt new "a`nb"',
        'claude-hr-ctl wt new "a`u{26}b"',
        # the stop-parsing token hands the rest of the line over as it is
        "claude-hr-ctl --% wt new x&calc",
        "claude-hr-ctl wt new --% x&calc",
        # the call sits inside a string that something else runs
        'cmd /c "claude-hr-ctl wt new x&calc"',
        'powershell -Command "claude-hr-ctl x | y"',
        "iex 'claude-hr-ctl wt new x&calc'",
        # whatever the text before the call does to the quotes, the rule looks at what comes after the call
        "# it's a comment\nclaude-hr-ctl wt new 'x&calc'",
        "<# it's #> claude-hr-ctl wt new 'x&calc'",
        "@'\nit's\n'@; claude-hr-ctl wt new 'x&calc'",
        'echo "$("a")"; claude-hr-ctl wt new \'x&calc\'',
        # text that PowerShell builds at run time cannot be checked
        "claude-hr-ctl wt new $name",
        'claude-hr-ctl wt new "$name"',
        'claude-hr-ctl wt new "$($x)"',
        "claude-hr-ctl wt new (Get-Name)",
        "claude-hr-ctl wt new @args",
        "claude-hr-ctl wt new { a&b }",  # a script block is passed as its text
        "claude-hr-ctl wt new feature `\n  x",
        # strict on purpose: a pipe, a redirection or a statement after the call is refused too
        "claude-hr-ctl status | Select-String 'a'",
        "claude-hr-ctl doctor 2>&1",
        "claude-hr-ctl doctor > out.txt",
        "claude-hr-ctl wt new feature/x; echo 'a&b'",
        "claude-hr-ctl doctor # it's fine & x",
        # a line break: inside quotes cmd.exe drops the rest of the arguments (a --dry-run among them); not read apart
        "claude-hr-ctl run 'first line\nsecond line' --dry-run",
        "claude-hr-ctl run 'a\r\nb'",
        "claude-hr-ctl doctor\nWrite-Output done",
    ],
)
def test_the_guard_stops_a_powershell_call_of_a_stand_in_with_what_cmd_exe_acts_on_after_it(
    command, stand_ins_in_use, capsys
):
    from claude_hr import hooks

    assert hooks._judge(command, "PowerShell") == 2
    err = capsys.readouterr().err
    assert "Blocked by the claude-hr guard" in err and ".cmd stand-in" in err
    assert r"C:\tool\Scripts\python.exe' -I -m claude_hr." in err, (
        "the way round it is in the message"
    )


@pytest.mark.parametrize(
    "command",
    [
        "claude-hr-ctl doctor",
        "claude-hr-ctl wt new feature/x",
        "claude-hr-ctl wt new 'it''s fine'",
        "claude-hr-ctl wt new 'a b' \"c d\"",
        "claude-hr-ctl config set a.b C:\\work\\x,y=z",
        "cd repo\nclaude-hr-ctl doctor",  # line breaks before the call are not its arguments
        "claude-hr-ctl doctor\n",  # nor one at the very end
        "claude-hr-ctl doctor \r\n  ",
        "claude-hr-ctl doctor; Write-Output done",
        # text before the call cannot be an argument of it
        "'a&b' | Out-Null; claude-hr-ctl doctor",
        "Write-Output 'don''t'; claude-hr-ctl doctor",
        "<# a & b #> claude-hr-ctl doctor",
        "$x = (Get-Date); claude-hr-ctl doctor",
        # not a call: a file, a folder, another word
        "git log --oneline && echo done",
        "Get-Content claude-hr.log | Select-String 'a&b'",
        "cd ~/.claude-hr/repo; git status | Select-String x",
        "Get-Content C:\\Users\\x\\.claude-hr\\logs\\crash.log | Select-Object -Last 5",
        "claude-hr-ctl.exe 'a&b'",  # the launcher program itself, which Windows refuses: it never reaches cmd.exe
        "echo 'a&b'",
    ],
)
def test_the_guard_leaves_other_powershell_calls_alone(command, stand_ins_in_use):
    from claude_hr import hooks

    assert hooks._judge(command, "PowerShell") == 0


def test_the_guard_reads_the_arguments_start_process_takes_before_the_program(
    stand_ins_in_use,
):
    from claude_hr import hooks

    command = "Start-Process -ArgumentList 'x&calc' -FilePath claude-hr-ctl"
    assert hooks._judge(command, "PowerShell") == 2
    assert hooks._judge("saps -Args 'a|b' claude-hr-ctl", "PowerShell") == 2
    assert hooks._judge("Start-Process claude-hr-ctl -ArgumentList doctor", "PowerShell") == 0


def test_a_broken_launcher_record_does_not_switch_off_the_git_check(
    stand_ins_in_use, monkeypatch, capsys
):
    from claude_hr import hooks

    def broken(name):
        raise ValueError("launchers.json is not JSON")

    monkeypatch.setattr(launchers, "is_shimmed", broken)
    assert hooks._judge("claude-hr-ctl doctor; git push --force origin main", "PowerShell") == 0
    assert "permissionDecision" in capsys.readouterr().out, "the git check still asks"


def test_the_guard_costs_one_linear_look_however_the_command_is_padded(stand_ins_in_use):
    """A hook that is killed at its time limit lets the command through: padding must not buy that. (This times the stand-in
    guard itself; the older patterns that _judge runs first have their own, documented limits on multi-megabyte commands.)"""
    import time

    from claude_hr import hooks

    started = time.monotonic()
    assert not hooks.stand_in_risk("claude-hr-ctl doctor " + "x" * 2_000_000)
    assert hooks.stand_in_risk("claude-hr-ctl doctor; " + "echo 'x' " * 200_000 + "&")
    assert hooks.stand_in_risk(("claude-hr-ctl doctor; " * 100_000) + "|")
    assert not hooks.stand_in_risk("echo " + "'a&b' " * 300_000), "no launcher called"
    assert time.monotonic() - started < 10


def test_the_guard_looks_only_where_a_stand_in_is_in_use(monkeypatch):
    from claude_hr import hooks

    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(launchers, "is_shimmed", lambda name: name == "claude-hr-hook")
    assert hooks._judge("claude-hr-ctl wt new 'x&calc'", "PowerShell") == 0, (
        "its launcher starts: no cmd.exe on the way"
    )
    assert hooks._judge("claude-hr-hook 'x&calc'", "PowerShell") == 2
    assert hooks._judge("claude-hr-hook 'x&calc'", "Bash") == 0, (
        "Git Bash runs the shell script, not the .cmd"
    )
    monkeypatch.setattr(paths, "IS_WINDOWS", False)
    assert hooks._judge("claude-hr-hook 'x&calc'", "PowerShell") == 0


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
    assert calls == [{"cmd": True}]
    updater.Updater(config.load(), dry=True)._look_at_launchers()
    assert calls == [{"cmd": True}]
    monkeypatch.setenv("CLAUDE_HR_LAUNCHERS__FALLBACK", "false")
    updater.Updater(config.load())._look_at_launchers()
    assert calls == [{"cmd": True}]


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
    assert "claude-hr-ctl" in check.detail and "shell scripts" in check.detail
    assert str(folder) in check.fix and "launchers ensure" in check.fix
    # an update writes a new claude-hr-ctl.exe, which wins the lookup over the stand-in: doctor says so
    (folder / "claude-hr-ctl.exe").write_bytes(b"MZ new")
    [check] = doctor.launcher_checks()
    assert "missing or hidden" in check.detail and check.fix == "claude-hr-ctl launchers ensure"


def test_a_headroom_left_out_on_purpose_is_a_warning_with_the_reason_not_a_failure(
    sandbox, fake_claude, monkeypatch
):
    """Where launchers are refused setup does not install Headroom; doctor (and so the health verdict of an update) must not
    call that a failure of the machine, and must say why."""
    monkeypatch.setattr(Headroom, "binary", lambda self: None)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(launchers, "refused_names", lambda: [])
    got = {c.name: c for c in doctor.run(config.load(), Path.cwd())}
    assert got["headroom"].status == "fail" and got["headroom"].detail == "not installed"
    monkeypatch.setattr(launchers, "refused_names", lambda: ["claude-hr-ctl"])
    got = {c.name: c for c in doctor.run(config.load(), Path.cwd())}
    assert got["headroom"].status == "warn"
    assert "Windows refuses launcher programs" in got["headroom"].detail
    assert "claude-hr-ctl launchers ensure" in got["headroom"].fix


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
    assert seen == [
        {"dry": True, "cmd": True},
        {"dry": True, "cmd": True},
        {"dry": False, "cmd": True},
    ]
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
        "cmd": True,
        "standin_refused": False,
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
    # a relative value depends on where we happen to be: not a folder to trust
    monkeypatch.setenv("UV_TOOL_BIN_DIR", "relative/bin")
    assert launchers.default_bin_dir() == tmp_path / "xdg"
    monkeypatch.delenv("XDG_BIN_HOME")
    assert launchers.default_bin_dir() == paths.home() / ".local" / "bin"


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

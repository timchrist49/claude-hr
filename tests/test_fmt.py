"""claude_hr.fmt: repo-aware formatting of changed files.

The formatting tests drive a fake formatter (a tiny Python script run through sys.executable) wired in through the
Formatter dataclass, against real temporary git repos. Detection tests create config files and fake executables, and
emulate both OS naming schemes (`ruff.exe` vs `ruff`) on any host. A few tests run a real tool when it is on PATH.
"""

import errno
import os
import subprocess
import sys
import threading
import time
import types
from dataclasses import dataclass

import pytest

from claude_hr import fmt, paths, procutil

BOM = b"\xef\xbb\xbf"

# The fake formatter: strips trailing whitespace and ends the file with one newline. Modes change what else it does.
FAKE_SCRIPT = r"""
import os
import sys
import time

mode, path = sys.argv[1], sys.argv[2]
data = sys.stdin.buffer.read().decode("utf-8")
if mode == "fail":
    sys.stderr.write("fake: cannot parse " + os.path.basename(path) + "\n")
    sys.exit(3)
if mode == "sleep":
    time.sleep(60)
if mode == "empty":
    sys.exit(0)
if mode == "mutate":
    with open(path, "ab") as fh:
        fh.write(b"# concurrent edit\n")
eol = "\r\n" if "\r\n" in data else "\n"
if mode == "lf":
    eol = "\n"
elif mode == "crlf":
    eol = "\r\n"
lines = data.replace("\r\n", "\n").split("\n")
fixed = "\n".join(line.rstrip(" \t") for line in lines).rstrip("\n")
out = (fixed + "\n").replace("\n", eol) if fixed else ""
sys.stdout.buffer.write(out.encode("utf-8"))
"""


@dataclass(frozen=True)
class Fake(fmt.Formatter):
    mode: str = "strip"
    script: str = ""

    def stdin_argv(self, path):
        argv = [self.exe]
        if self.script:
            argv.append(self.script)
        return [*argv, self.mode, str(path)]


@pytest.fixture(scope="session")
def fake_script(tmp_path_factory):
    p = tmp_path_factory.mktemp("fmt-tools") / "fake_fmt.py"
    p.write_text(FAKE_SCRIPT, encoding="utf-8", newline="\n")
    return str(p)


@pytest.fixture
def fake(monkeypatch, fake_script, sandbox):
    """fake("lf") makes every file use the fake formatter in that mode."""

    def use(mode="strip", name="ruff"):
        f = Fake(name, sys.executable, mode, fake_script)
        monkeypatch.setattr(fmt, "detect", lambda path, root: f)
        return f

    use()
    return use


def git(repo, *args, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    r = subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, timeout=60
    )
    if check:
        assert r.returncode == 0, r.stderr
    return r


@pytest.fixture
def repo(tmp_path):
    p = tmp_path / "work" / "my repo"  # a space on purpose: every code path must cope
    p.mkdir(parents=True)
    git(p, "init", "-q")
    for k, v in (
        ("user.email", "t@example.com"),
        ("user.name", "t"),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
    ):
        git(p, "config", k, v)
    return p


def commit(repo, message="c"):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def put(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


# ---- formatting behaviour --------------------------------------------------------------------------------------


def test_untracked_file_is_formatted_and_paths_with_spaces_work(repo, fake):
    f = put(repo / "sub dir" / "new file.py", "x = 1   \n\n\n")
    report = fmt.format_changed(repo)
    assert report.formatted == ["sub dir/new file.py"]
    assert f.read_bytes() == b"x = 1\n"
    assert report.formatters == ["ruff"]
    assert report.errors == [] and report.skipped == []
    assert report.elapsed_s >= 0


def test_non_ascii_file_names_work(repo, fake):
    f = put(repo / "déjà vu" / "naïve ünï.py", "x = 1  \n")
    report = fmt.format_changed(repo)
    assert report.formatted == ["déjà vu/naïve ünï.py"]
    assert f.read_bytes() == b"x = 1\n"


def test_clean_gate_skips_tracked_file_with_preexisting_drift(repo, fake):
    f = put(repo / "a.py", "x = 1   \n")  # already drifted at HEAD
    commit(repo)
    dirty = b"x = 1   \ny = 2   \n"
    f.write_bytes(dirty)
    report = fmt.format_changed(repo)
    assert report.formatted == []
    assert report.skipped == [("a.py", "pre-existing formatting drift")]
    assert f.read_bytes() == dirty
    # a deliberate run without the gate formats it
    report = fmt.format_changed(repo, clean_gate=False)
    assert report.formatted == ["a.py"]
    assert f.read_bytes() == b"x = 1\ny = 2\n"


def test_clean_gate_formats_tracked_file_whose_head_is_clean(repo, fake):
    f = put(repo / "a.py", "x = 1\n")
    commit(repo)
    f.write_bytes(b"x = 1\ny = 2   \n")
    report = fmt.format_changed(repo)
    assert report.formatted == ["a.py"]
    assert f.read_bytes() == b"x = 1\ny = 2\n"


def test_new_file_staged_but_never_committed_is_always_formatted(repo, fake):
    put(repo / "seed.py", "x = 1\n")
    commit(repo)  # HEAD exists, but not for the new file
    f = put(repo / "added.py", "y = 2   \n")
    git(repo, "add", "added.py")
    assert fmt.format_changed(repo).formatted == ["added.py"]
    assert f.read_bytes() == b"y = 2\n"


def test_untracked_file_in_repo_without_any_commit(repo, fake):
    f = put(repo / "a.py", "x = 1  \n")
    assert fmt.format_changed(repo).formatted == ["a.py"]
    assert f.read_bytes() == b"x = 1\n"


def test_crlf_file_stays_crlf_when_the_formatter_emits_lf(repo, fake):
    fake("lf")
    f = put(repo / "win.py", "a = 1  \r\nb = 2\r\n")
    report = fmt.format_changed(repo)
    assert report.formatted == ["win.py"]
    assert f.read_bytes() == b"a = 1\r\nb = 2\r\n"


def test_crlf_file_that_is_already_clean_is_not_flipped_or_rewritten(repo, fake):
    fake("lf")  # this formatter would flip every line, which must not count as a change
    f = put(repo / "win.py", "a = 1\r\nb = 2\r\n")
    old = 1_000_000_000_000_000_000
    os.utime(f, ns=(old, old))
    report = fmt.format_changed(repo)
    assert report.formatted == []
    assert f.read_bytes() == b"a = 1\r\nb = 2\r\n"
    assert f.stat().st_mtime_ns == old


def test_lf_file_stays_lf_when_the_formatter_emits_crlf(repo, fake):
    fake("crlf")
    f = put(repo / "unix.py", "a = 1  \nb = 2\n")
    assert fmt.format_changed(repo).formatted == ["unix.py"]
    assert f.read_bytes() == b"a = 1\nb = 2\n"


def test_gate_judges_a_crlf_head_exactly_like_the_write_path_does(repo, fake):
    """HEAD is CRLF and clean; the formatter emits LF. The write path keeps CRLF (_match_eol), so the gate must compare
    HEAD the same way: this file is clean at HEAD and gets formatted, with its CRLF kept."""
    fake("lf")
    f = put(repo / "win.py", "a = 1\r\n")
    commit(repo)
    f.write_bytes(b"a = 1\r\nb = 2  \r\n")
    report = fmt.format_changed(repo)
    assert report.skipped == [] and report.formatted == ["win.py"]
    assert f.read_bytes() == b"a = 1\r\nb = 2\r\n"


def test_gate_still_sees_real_drift_in_a_crlf_head(repo, fake):
    fake("lf")
    f = put(repo / "win.py", "a = 1  \r\n")  # trailing whitespace at HEAD: genuinely drifted
    commit(repo)
    f.write_bytes(b"a = 1  \r\nb = 2  \r\n")
    report = fmt.format_changed(repo)
    assert report.skipped == [("win.py", fmt.DRIFT_REASON)] and report.formatted == []
    assert f.read_bytes() == b"a = 1  \r\nb = 2  \r\n"


def test_gate_still_sees_a_mixed_eol_head_as_drift(repo, fake):
    fake("lf")
    f = put(repo / "mixed.py", "a = 1\r\nb = 2\n")  # no single convention to preserve
    commit(repo)
    f.write_bytes(b"a = 1\r\nb = 2\nc = 3  \n")
    report = fmt.format_changed(repo)
    assert report.skipped == [("mixed.py", fmt.DRIFT_REASON)]


def test_unchanged_output_is_not_rewritten(repo, fake):
    f = put(repo / "clean.py", "x = 1\n")
    old = 1_000_000_000_000_000_000
    os.utime(f, ns=(old, old))
    report = fmt.format_changed(repo)
    assert report.formatted == []
    assert f.stat().st_mtime_ns == old


def test_bom_is_preserved(repo, fake):
    f = put(repo / "bom.py", BOM + b"x = 1  \n")
    assert fmt.format_changed(repo).formatted == ["bom.py"]
    assert f.read_bytes() == BOM + b"x = 1\n"


def test_non_utf8_file_is_skipped(repo, fake):
    f = put(repo / "latin.py", b"x = '\xe9'  \n")
    report = fmt.format_changed(repo)
    assert report.skipped == [("latin.py", "not valid UTF-8")]
    assert f.read_bytes() == b"x = '\xe9'  \n"


def test_formatter_timeout_records_error_and_leaves_the_file(repo, fake, monkeypatch):
    fake("sleep")
    monkeypatch.setattr(fmt, "_PER_FILE_MAX_S", 0.5)
    f = put(repo / "slow.py", "x = 1  \n")
    report = fmt.format_changed(repo)
    assert report.formatted == []
    assert len(report.errors) == 1 and report.errors[0][0] == "slow.py"
    assert report.errors[0][1].startswith("timeout after")
    assert f.read_bytes() == b"x = 1  \n"
    assert report.elapsed_s < 20


def test_formatter_failure_is_recorded_and_the_file_untouched(repo, fake):
    fake("fail")
    f = put(repo / "bad.py", "x = (  \n")
    report = fmt.format_changed(repo)
    assert report.formatted == []
    assert report.errors == [("bad.py", "fake: cannot parse bad.py")]
    assert f.read_bytes() == b"x = (  \n"


def test_empty_output_for_real_input_is_a_failure_not_a_wipe(repo, fake):
    fake("empty")
    f = put(repo / "a.py", "x = 1  \n")
    report = fmt.format_changed(repo)
    assert report.errors == [("a.py", "formatter produced empty output")]
    assert f.read_bytes() == b"x = 1  \n"


def test_file_edited_while_formatting_is_not_overwritten(repo, fake):
    fake(
        "mutate"
    )  # the fake appends to the file while it runs, like an editor saving at the same moment
    f = put(repo / "a.py", "x = 1  \n")
    report = fmt.format_changed(repo)
    assert report.skipped == [("a.py", "changed while formatting")]
    assert f.read_bytes() == b"x = 1  \n# concurrent edit\n"


# ---- writing: atomic, never truncating -------------------------------------------------------------------------


def _temp_leftovers(directory):
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(".claude-hr-fmt-"))


@pytest.mark.parametrize("failing", ["replace", "fsync", "write"])
def test_a_failed_write_leaves_the_original_intact_and_no_temp_file(
    repo, fake, monkeypatch, failing
):
    """The old code opened the file with 'wb' (truncating it at once) and wrote into it: a write that failed halfway
    (full disk, quota, network share) left a truncated file that git cannot restore when it is untracked."""
    original = b"x = 1  \n" * 50
    f = put(repo / "a.py", original)
    disk_full = OSError(errno.ENOSPC, "disk full")

    def boom(*args, **kwargs):
        raise disk_full

    real_fdopen = os.fdopen

    class FailingFile:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()
            return False

        def write(self, data):
            raise disk_full

    with monkeypatch.context() as m:
        if failing == "replace":
            m.setattr(os, "replace", boom)
        elif failing == "fsync":
            m.setattr(os, "fsync", boom)
        else:
            m.setattr(os, "fdopen", lambda fd, mode: FailingFile(real_fdopen(fd, mode)))
        report = fmt.format_changed(repo)
    assert report.errors == [("a.py", "cannot write: disk full")]
    assert report.formatted == []
    assert f.read_bytes() == original
    assert _temp_leftovers(repo) == []
    assert fmt.format_changed(repo).formatted == [
        "a.py"
    ]  # and it works again once the disk is back


@pytest.mark.parametrize(
    ("on_windows", "fail_times", "attempts", "formatted"),
    [
        (True, 2, 3, True),  # a lock that goes away: retried, written
        (True, 99, 4, False),  # a lock that stays: given up after _REPLACE_TRIES, reported
        (False, 1, 1, False),  # elsewhere a permission error is final at once
    ],
)
def test_a_locked_file_is_retried_briefly_on_windows_only(
    repo, monkeypatch, on_windows, fail_times, attempts, formatted
):
    original = b"x = 1  \n"
    f = put(repo / "a.py", original)
    real_replace, tries = os.replace, []

    def locked(src, dst):
        tries.append(1)
        if len(tries) <= fail_times:
            raise PermissionError(errno.EACCES, "locked by another process")
        return real_replace(src, dst)

    with monkeypatch.context() as m:
        # _replace_atomically spawns nothing, so IS_WINDOWS can be flipped around the call on any host
        m.setattr(paths, "IS_WINDOWS", on_windows)
        m.setattr(os, "replace", locked)
        m.setattr(time, "sleep", lambda s: None)
        if formatted:
            assert fmt._replace_atomically(f, b"x = 1\n", os.lstat(f)) is None
        else:
            with pytest.raises(PermissionError, match="locked by another process"):
                fmt._replace_atomically(f, b"x = 1\n", os.lstat(f))
    assert len(tries) == attempts
    assert f.read_bytes() == (b"x = 1\n" if formatted else original)
    assert _temp_leftovers(repo) == []


def test_owner_is_kept_or_the_rewrite_is_refused(repo, monkeypatch):
    f = put(repo / "a.py", "x = 1  \n")
    owned_by_other = types.SimpleNamespace(st_uid=0, st_gid=0)
    chowned = []

    def chown_fails(path, uid, gid):
        chowned.append((uid, gid))
        raise PermissionError(errno.EPERM, "not permitted")

    with monkeypatch.context() as m:
        m.setattr(os, "geteuid", lambda: 1000, raising=False)
        m.setattr(os, "getegid", lambda: 1000, raising=False)
        m.setattr(os, "chown", chown_fails, raising=False)
        assert fmt._keeps_owner("t", owned_by_other) is False and chowned == [(0, 0)]
        mine_other_group = types.SimpleNamespace(st_uid=1000, st_gid=7)
        assert fmt._keeps_owner("t", mine_other_group) is True  # the group is best effort
        same = types.SimpleNamespace(st_uid=1000, st_gid=1000)
        chowned.clear()
        assert fmt._keeps_owner("t", same) is True and chowned == []
        m.setattr(os, "chown", lambda path, uid, gid: chowned.append((uid, gid)), raising=False)
        assert fmt._keeps_owner("t", owned_by_other) is True  # root: allowed
    with monkeypatch.context() as m:
        m.setattr(fmt, "_keeps_owner", lambda tmp, before: False)
        with pytest.raises(PermissionError, match="owner"):
            fmt._replace_atomically(f, b"x = 1\n", os.lstat(f))
    assert f.read_bytes() == b"x = 1  \n" and _temp_leftovers(repo) == []


def test_a_successful_write_leaves_no_temp_file_behind(repo, fake):
    f = put(repo / "a.py", "x = 1  \n")
    assert fmt.format_changed(repo).formatted == ["a.py"]
    assert f.read_bytes() == b"x = 1\n"
    assert _temp_leftovers(repo) == []


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX permission bits")
def test_the_rewritten_file_keeps_its_permission_bits(repo, fake):
    f = put(repo / "run.py", "x = 1  \n")
    f.chmod(0o750)
    assert fmt.format_changed(repo).formatted == ["run.py"]
    assert f.read_bytes() == b"x = 1\n"
    assert f.stat().st_mode & 0o7777 == 0o750


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can write read-only files"
)
def test_a_read_only_file_is_not_replaced_behind_its_back(repo, fake):
    """os.replace needs only a writable directory, so without a check an atomic rewrite would overwrite a file the
    user made read-only (the old in-place write failed on it)."""
    f = put(repo / "locked.py", "x = 1  \n")
    f.chmod(0o444)
    try:
        report = fmt.format_changed(repo)
        assert report.errors == [("locked.py", "cannot write: file is read-only")]
        assert f.read_bytes() == b"x = 1  \n"
        assert _temp_leftovers(repo) == []
    finally:
        f.chmod(0o644)


def _one(f, repo, name, **kw):
    """Drive _format_one for a single file, as _process does."""
    report = fmt.FormatReport()
    cand = fmt._Cand(repo / name, name, name, tracked=False)
    fmt._format_one(
        f,
        cand,
        repo,
        report,
        deadline=fmt._now() + 60,
        clean_gate=False,
        check=kw.get("check", False),
    )
    return report


def test_a_symlink_is_never_written_through(repo, fake):
    target = put(repo / "real.py", "x = 1  \n")
    try:
        os.symlink(target, repo / "link.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    report = _one(
        fake(), repo, "link.py"
    )  # as if the file had been swapped for a link after the scan
    assert report.skipped == [("link.py", "not a regular file")] and report.formatted == []
    assert target.read_bytes() == b"x = 1  \n"
    assert (repo / "link.py").is_symlink()


def test_a_hard_linked_file_is_skipped_not_unlinked_from_its_twin(repo, fake):
    first = put(repo / "a.py", "x = 1  \n")
    try:
        os.link(first, repo / "twin.py")
    except (OSError, NotImplementedError):
        pytest.skip("hard links are not available here")
    report = _one(fake(), repo, "a.py")
    assert report.skipped == [("a.py", "hard-linked file")] and report.formatted == []
    assert os.path.samefile(first, repo / "twin.py")
    assert first.read_bytes() == b"x = 1  \n"
    assert _one(fake(), repo, "a.py", check=True).skipped == [("a.py", "hard-linked file")]


def test_the_scope_is_every_uncommitted_file_not_only_the_ones_an_agent_edited(repo, fake):
    """Deliberate: the formatter at the end of a turn does what a pre-commit hook would. Nothing in the gate knows
    who changed a file, so a file the user edited by hand is formatted too (the clean gate keeps it to their lines)."""
    mine = put(repo / "mine.py", "x = 1\n")
    commit(repo)
    mine.write_bytes(b"x = 1\ny = 2   \n")  # the user's own work in progress, not the agent's
    assert fmt.format_changed(repo).formatted == ["mine.py"]
    assert mine.read_bytes() == b"x = 1\ny = 2\n"


# ---- the clean gate and renamed files ---------------------------------------------------------------------------


def test_gate_judges_a_renamed_file_against_its_old_path(repo, fake):
    """`git mv` + an edit shows up as `RM new <- old`. HEAD has no `new`, which used to read as "a new file" (always
    formatted), so the whole drifted file was reformatted."""
    old = put(repo / "a.py", "x = 1   \n")  # drifted at HEAD
    commit(repo)
    git(repo, "mv", "a.py", "b.py")
    moved = repo / "b.py"
    moved.write_bytes(b"x = 1   \ny = 2   \n")
    assert old.exists() is False
    report = fmt.format_changed(repo)
    assert report.skipped == [("b.py", fmt.DRIFT_REASON)] and report.formatted == []
    assert moved.read_bytes() == b"x = 1   \ny = 2   \n"
    assert fmt.format_changed(repo, clean_gate=False).formatted == [
        "b.py"
    ]  # the deliberate run still formats it


def test_a_pure_rename_of_a_drifted_file_is_not_reformatted(repo, fake):
    put(repo / "a.py", "x = 1   \n")
    commit(repo)
    git(repo, "mv", "a.py", "b.py")
    report = fmt.format_changed(repo)
    assert report.skipped == [("b.py", fmt.DRIFT_REASON)]
    assert (repo / "b.py").read_bytes() == b"x = 1   \n"


def test_a_renamed_file_that_was_clean_at_its_old_path_is_formatted(repo, fake):
    put(repo / "a.py", "x = 1\n")
    commit(repo)
    git(repo, "mv", "a.py", "sub_b.py")
    moved = repo / "sub_b.py"
    moved.write_bytes(b"x = 1\ny = 2   \n")
    assert fmt.format_changed(repo).formatted == ["sub_b.py"]
    assert moved.read_bytes() == b"x = 1\ny = 2\n"


def test_renames_are_detected_even_when_the_user_turned_rename_detection_off(repo, fake):
    git(repo, "config", "diff.renames", "false")
    put(repo / "a.py", "x = 1   \n")
    commit(repo)
    git(repo, "mv", "a.py", "b.py")
    (repo / "b.py").write_bytes(b"x = 1   \ny = 2   \n")
    assert fmt.format_changed(repo).skipped == [("b.py", fmt.DRIFT_REASON)]


def test_a_rename_whose_original_is_not_in_head_is_unknowable_and_skipped(repo, fake):
    put(repo / "seed.py", "x = 1\n")
    commit(repo)
    f = fake()
    put(repo / "b.py", "x = 1\ny = 2   \n")
    gone = fmt._Cand(repo / "b.py", "b.py", "b.py", tracked=True, orig_top_rel="never_committed.py")
    why = fmt._gate(f, repo, gone, fmt._now() + 60)
    assert why and "renamed from" in why
    seed = fmt._Cand(repo / "b.py", "b.py", "b.py", tracked=True, orig_top_rel="seed.py")
    assert fmt._gate(f, repo, seed, fmt._now() + 60) is None  # seed.py is clean at HEAD


def test_skips_binary_large_lock_minified_generated_and_vendored_files(repo, fake):
    put(repo / "ok.py", "x = 1  \n")
    put(repo / "binary.py", b"\x00\x01\x02 junk\n")
    put(repo / "big.py", "x = 1\n" * 1000)
    put(repo / "package-lock.json", "{ }\n")
    put(repo / "app.min.js", "var a = 1  ;\n")
    put(repo / "gen.py", "# Code generated by tool. DO NOT EDIT.\nx = 1  \n")
    put(repo / "node_modules" / "pkg" / "index.js", "var a = 1  ;\n")
    put(repo / "vendor" / "lib.py", "x = 1  \n")
    put(repo / "notes.txt", "not a source file  \n")
    report = fmt.format_changed(repo, max_kb=1)
    assert report.formatted == ["ok.py"]
    assert dict(report.skipped) == {
        "binary.py": "binary file",
        "big.py": "larger than 1 KB",
        "package-lock.json": "lockfile",
        "app.min.js": "minified file",
        "gen.py": "generated file",
        "node_modules/pkg/index.js": "generated or vendored directory",
        "vendor/lib.py": "generated or vendored directory",
    }
    assert [p.name for p in fmt.changed_files(repo, max_files=40, max_kb=1)] == ["ok.py"]


def test_max_files_caps_the_work(repo, fake):
    files = [put(repo / f"f{i}.py", "x = 1  \n") for i in range(1, 6)]
    report = fmt.format_changed(repo, max_files=2)
    assert report.formatted == ["f1.py", "f2.py"]
    assert report.skipped == [("3 more file(s)", "max_files cap (2)")]
    assert files[2].read_bytes() == b"x = 1  \n"
    assert len(fmt.changed_files(repo, max_files=2, max_kb=512)) == 2


def test_gitignored_files_are_never_touched(repo, fake):
    put(repo / ".gitignore", "ignored.py\nout/\n")
    ign = put(repo / "ignored.py", "x = 1  \n")
    ign2 = put(repo / "out" / "x.py", "x = 1  \n")
    put(repo / "kept.py", "x = 1  \n")
    report = fmt.format_changed(repo)
    assert report.formatted == ["kept.py"]
    assert report.skipped == []
    assert ign.read_bytes() == b"x = 1  \n" and ign2.read_bytes() == b"x = 1  \n"


def test_skip_globs(repo, fake):
    put(repo / "legacy" / "old.py", "x = 1  \n")
    put(repo / "pkg" / "gen_models.py", "x = 1  \n")
    put(repo / "gen_top.py", "x = 1  \n")
    put(repo / "keep.py", "x = 1  \n")
    report = fmt.format_changed(repo, skip_globs=["legacy/*", "**/gen_*.py"])
    assert report.formatted == ["keep.py"]
    assert dict(report.skipped) == {
        "legacy/old.py": "matches skip_globs",
        "pkg/gen_models.py": "matches skip_globs",
        "gen_top.py": "matches skip_globs",
    }


def test_time_budget_skips_the_rest(repo, fake, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(fmt, "_now", lambda: clock[0])
    real = fmt._format

    def slow(*args, **kwargs):
        clock[0] += 100.0  # as if the formatter took 100 s
        return real(*args, **kwargs)

    monkeypatch.setattr(fmt, "_format", slow)
    for n in "abc":
        put(repo / f"{n}.py", "x = 1  \n")
    report = fmt.format_changed(repo, timeout_s=30)
    assert report.formatted == ["a.py"]
    assert report.skipped == [("b.py", "time budget"), ("c.py", "time budget")]
    assert report.elapsed_s == 100.0


def test_zero_budget_formats_nothing(repo, fake):
    f = put(repo / "a.py", "x = 1  \n")
    report = fmt.format_changed(repo, timeout_s=0)
    assert report.formatted == [] and report.skipped == [("a.py", "time budget")]
    assert f.read_bytes() == b"x = 1  \n"


def test_not_a_git_repository(tmp_path, fake, monkeypatch):
    plain = tmp_path / "plain"
    put(plain / "a.py", "x = 1  \n")
    monkeypatch.setenv(
        "GIT_CEILING_DIRECTORIES", str(tmp_path)
    )  # never discover a repo above the temp dir
    report = fmt.format_changed(plain)
    assert report.formatted == [] and report.skipped == [(".", "not a git repository")]
    assert fmt.changed_files(plain, max_files=10, max_kb=512) == []


def test_git_missing_fails_open(repo, fake, monkeypatch, tmp_path):
    put(repo / "a.py", "x = 1  \n")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    report = fmt.format_changed(repo)
    assert report.formatted == [] and report.errors == []


def test_root_may_be_a_subdirectory_of_the_repo(repo, fake):
    inside = put(repo / "a" / "x.py", "x = 1  \n")
    outside = put(repo / "b" / "y.py", "x = 1  \n")
    report = fmt.format_changed(repo / "a")
    assert report.formatted == ["x.py"]
    assert inside.read_bytes() == b"x = 1\n" and outside.read_bytes() == b"x = 1  \n"
    assert [p.name for p in fmt.changed_files(repo / "a", max_files=9, max_kb=512)] == ["x.py"]


def test_deleted_tracked_file_is_ignored(repo, fake):
    f = put(repo / "gone.py", "x = 1\n")
    commit(repo)
    f.unlink()
    report = fmt.format_changed(repo)
    assert report.formatted == [] and report.skipped == [] and report.errors == []


def test_merge_conflicts_are_never_touched(repo, fake):
    f = put(repo / "a.py", "x = 1\n")
    commit(repo)
    git(repo, "switch", "-q", "-c", "side")
    f.write_bytes(b"x = 2\n")
    commit(repo, "side")
    git(repo, "switch", "-q", "-")
    f.write_bytes(b"x = 3  \n")
    commit(repo, "main")
    assert git(repo, "merge", "side", check=False).returncode != 0
    conflicted = f.read_bytes()
    report = fmt.format_changed(repo)
    assert report.skipped == [("a.py", "unmerged (merge conflict)")]
    assert f.read_bytes() == conflicted


def test_symlinks_are_skipped(repo, fake):
    real = put(repo / "real.py", "x = 1  \n")
    try:
        os.symlink(real, repo / "link.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    report = fmt.format_changed(repo)
    assert ("link.py", "symlink") in report.skipped
    assert report.formatted == ["real.py"]


def test_allow_filters_by_formatter_name(repo, fake):
    f = put(repo / "a.py", "x = 1  \n")
    report = fmt.format_changed(repo, allow=["black"])
    assert report.formatted == [] and report.skipped == [
        ("a.py", "ruff is not in the allowed formatters")
    ]
    assert f.read_bytes() == b"x = 1  \n"
    assert fmt.format_changed(repo, allow=["Ruff"]).formatted == ["a.py"]
    put(repo / "b.py", "x = 1  \n")
    assert fmt.format_changed(repo, allow=[]).formatted == [
        "b.py"
    ]  # empty = any detected formatter


def test_a_plain_string_for_allow_or_skip_globs_is_one_entry_not_characters(repo, fake):
    put(repo / "a.py", "x = 1  \n")
    put(repo / "skip_me.py", "x = 1  \n")
    report = fmt.format_changed(repo, allow="ruff", skip_globs="skip_*.py")
    assert report.formatted == ["a.py"] and report.skipped == [("skip_me.py", "matches skip_globs")]


def test_check_mode_reports_without_writing(repo, fake):
    f = put(repo / "a.py", "x = 1  \n")
    report = fmt.format_changed(repo, check=True)
    assert report.formatted == ["a.py"]
    assert f.read_bytes() == b"x = 1  \n"


def test_repo_without_a_configured_formatter_is_left_alone(repo):
    f = put(repo / "a.py", "x = 1  \n")  # no pyproject/ruff.toml: nothing is imposed
    report = fmt.format_changed(repo)
    assert report.formatted == [] and report.formatters == []
    assert report.skipped == [("a.py", "no formatter configured")]
    assert f.read_bytes() == b"x = 1  \n"


def test_a_formatter_binary_committed_to_the_repo_is_refused(repo):
    win = paths.IS_WINDOWS
    put(repo / "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    shipped = repo / ".venv" / ("Scripts" if win else "bin") / ("ruff.exe" if win else "ruff")
    put(shipped, b"")
    shipped.chmod(0o755)
    git(repo, "add", "-f", "-A")
    git(repo, "commit", "-q", "-m", "ship a binary")
    f = put(repo / "a.py", "x = 1  \n")
    report = fmt.format_changed(
        repo, trusted=True
    )  # even a trusted repo may not ship its own formatter binary
    assert report.formatted == [] and report.errors == []
    assert report.skipped[0][0] == "a.py" and "tracked in git" in report.skipped[0][1]
    assert f.read_bytes() == b"x = 1  \n"
    assert fmt.describe(repo, trusted=True)[0]["usable"] is False


def test_an_untrusted_repo_may_not_run_a_formatter_that_lives_inside_it(repo):
    """An agent with write access only can create .venv/bin/ruff or node_modules/.bin/prettier: the gate must not run it."""
    win = paths.IS_WINDOWS
    put(repo / "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    local = repo / ".venv" / ("Scripts" if win else "bin") / ("ruff.exe" if win else "ruff")
    put(local, b"")
    local.chmod(0o755)
    f = put(repo / "a.py", "x = 1  \n")
    report = fmt.format_changed(repo)  # trusted defaults to False
    assert report.formatted == [] and report.errors == []
    assert "lives inside the repository" in report.skipped[0][1]
    assert "verify trust" in report.skipped[0][1]
    assert f.read_bytes() == b"x = 1  \n"
    got = fmt.describe(repo)[0]
    assert got["usable"] is False and "lives inside the repository" in got["reason"]
    assert "usable" not in fmt.describe(repo, trusted=True)[0]


PRETTIER_CONFIGS = [
    # executable
    ("prettier.config.js", "module.exports = {};\n"),
    (".prettierrc.cjs", "module.exports = {};\n"),
    (".prettierrc.mjs", "export default {};\n"),
    ("prettier.config.ts", "export default {};\n"),
    # data that can still load code
    (".prettierrc", '{"plugins": ["./local-plugin.js"]}\n'),
    (".prettierrc.json", '{"parser": "./my-parser.js"}\n'),
    (".prettierrc.yaml", "plugins:\n  - ./p.js\n"),
    (".prettierrc.toml", 'plugins = ["./p.js"]\n'),
    ("package.json", '{"prettier": "@company/prettier-config"}\n'),
    (".prettierrc", "prettier-config-standard\n"),
    (".prettierrc", '{"\\u0070lugins": ["./p.js"]}\n'),
    # and plain data: prettier's configuration is never vetted by reading it, so even this needs a trusted repo
    (".prettierrc", '{"semi": false}\n'),
    (".prettierrc.yaml", "semi: false\ntabWidth: 4\n"),
    ("package.json", '{"name": "x", "prettier": {"semi": false}}\n'),
]


@pytest.mark.parametrize("name, content", PRETTIER_CONFIGS)
def test_prettier_runs_only_for_a_trusted_repository(repo, fake, name, content):
    """Whatever the configuration looks like, prettier may load code from it, and every attempt to vet a config by
    reading it differed from prettier's own parsers somewhere (found by review, three times)."""
    fake("strip", "prettier")  # a prettier that is NOT inside the repo (a global install)
    put(repo / name, content)
    f = put(repo / "a.ts", "const a = 1;  \n")
    report = fmt.format_changed(repo)
    assert report.formatted == [] and "prettier configuration can run code" in report.skipped[0][1]
    assert "verify trust" in report.skipped[0][1]
    assert f.read_bytes() == b"const a = 1;  \n"
    assert fmt.format_changed(repo, trusted=True).formatted == ["a.ts"]  # the user vouched for it


def test_prettier_is_described_as_not_used_until_the_repo_is_trusted(proj, win):
    node_tool(proj, "prettier", win)
    (proj / ".prettierrc").write_text('{"semi": false}\n', encoding="utf-8")
    # a prettier installed outside the repository (PATH) is still refused: its configuration is the repo's
    only = fmt._repo_code_refusal(
        fmt.Formatter("prettier", sys.executable), proj / "a.ts", proj, False
    )
    assert only and "prettier configuration can run code" in only
    assert (
        fmt._repo_code_refusal(fmt.Formatter("prettier", sys.executable), proj / "a.ts", proj, True)
        is None
    )


@pytest.mark.parametrize("name", ["ruff", "black", "biome", "gofmt", "rustfmt"])
def test_other_formatters_configuration_is_data_and_needs_no_trust(repo, fake, name):
    fake("strip", name)
    put(repo / "a.ts", "const a = 1;  \n")
    assert fmt.format_changed(repo).formatted == ["a.ts"]


def test_entry_points_never_raise(repo, monkeypatch):
    put(repo / "a.py", "x = 1  \n")

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    with monkeypatch.context() as m:
        m.setattr(fmt, "_status_candidates", boom)
        report = fmt.format_changed(repo)
        assert report.errors == [(".", "internal error: RuntimeError: boom")]
        assert fmt.changed_files(repo, max_files=5, max_kb=5) == []
    with monkeypatch.context() as m:
        m.setattr(fmt, "_path_candidates", boom)
        report = fmt.format_paths(repo, [repo / "a.py"])
        assert report.errors == [(".", "internal error: RuntimeError: boom")]
    with monkeypatch.context() as m:
        m.setattr(fmt, "_detect", boom)
        assert fmt.describe(repo) == []
        assert fmt.detect(repo / "a.py", repo) is None
    missing = fmt.Formatter("ruff", str(repo / "missing-exe"))
    assert fmt.format_text(missing, repo / "a.py", "x = 1\n", cwd=repo, timeout=5) is None


# ---- explicit paths (claude-hr-ctl fmt <paths>) ----------------------------------------------------------------


def test_format_paths_formats_files_and_expands_directories(repo, fake):
    put(repo / ".gitignore", "pkg/skipme.py\n")
    drifted = put(repo / "pkg" / "a.py", "x = 1  \n")
    commit(repo)  # tracked and drifted at HEAD: a deliberate run still formats it (the gate is off)
    put(repo / "pkg" / "b.py", "y = 2  \n")
    put(repo / "pkg" / "skipme.py", "z = 3  \n")
    other = put(repo / "other.py", "w = 4  \n")
    report = fmt.format_paths(repo, [repo / "pkg"])
    assert report.formatted == ["pkg/a.py", "pkg/b.py"]
    assert drifted.read_bytes() == b"x = 1\n"
    assert other.read_bytes() == b"w = 4  \n"
    assert (repo / "pkg" / "skipme.py").read_bytes() == b"z = 3  \n"
    report = fmt.format_paths(repo, ["other.py"])  # relative targets resolve against root
    assert report.formatted == ["other.py"]


def test_format_paths_outside_root_and_check_mode(repo, fake, tmp_path):
    outside = put(tmp_path / "elsewhere" / "z.py", "x = 1  \n")
    inside = put(repo / "a.py", "x = 1  \n")
    report = fmt.format_paths(repo, [outside, inside], check=True)
    assert report.formatted == ["a.py"]
    assert (str(outside), "outside the repository root") in report.skipped
    assert inside.read_bytes() == b"x = 1  \n"


def test_format_paths_says_when_a_named_file_cannot_be_formatted(repo, fake):
    put(repo / "notes.txt", "hello  \n")
    put(repo / "docs" / "more.txt", "hello  \n")
    report = fmt.format_paths(repo, [repo / "notes.txt", repo / "docs", repo / "missing.py"])
    assert report.formatted == []
    assert ("notes.txt", "no formatter handles this file type") in report.skipped
    assert ("missing.py", "not a file") in report.skipped
    assert not any(
        rel.startswith("docs") for rel, _ in report.skipped
    )  # directory contents stay quiet


# ---- helpers ---------------------------------------------------------------------------------------------------


def test_parse_status_handles_renames_untracked_and_spaces():
    raw = b" M a.py\0?? b c.py\0R  new.py\0old.py\0UU conflict.py\0A  z.py\0"
    assert fmt._parse_status(raw) == [
        (" M", "a.py"),
        ("??", "b c.py"),
        ("R ", "new.py"),
        ("UU", "conflict.py"),
        ("A ", "z.py"),
    ]
    assert fmt._parse_status(b"") == []


def test_parse_status_entries_keep_the_original_path_of_renames_and_copies():
    raw = b" M a.py\0R  new.py\0old dir/old.py\0C  copy.py\0src.py\0?? b c.py\0RM x.py\0y.py\0"
    assert fmt._parse_status_entries(raw) == [
        (" M", "a.py", ""),
        ("R ", "new.py", "old dir/old.py"),
        ("C ", "copy.py", "src.py"),
        ("??", "b c.py", ""),
        ("RM", "x.py", "y.py"),
    ]
    assert fmt._parse_status_entries(b"R  truncated.py\0") == [("R ", "truncated.py", "")]


def test_match_eol():
    assert fmt._match_eol("a\r\nb\r\n", "a\nb\n") == "a\r\nb\r\n"
    assert fmt._match_eol("a\nb\n", "a\r\nb\r\n") == "a\nb\n"
    assert (
        fmt._match_eol("a\r\nb\n", "a\nb\n") == "a\nb\n"
    )  # mixed original: leave the output alone
    assert fmt._match_eol("a", "a\n") == "a\n"  # no line break to learn from
    assert fmt._eol_style("x\r\ny\r\n") == "crlf" and fmt._eol_style("") is None


def test_glob_matching():
    assert fmt._glob_match("a/b/c.py", ["a/*"])
    assert fmt._glob_match("gen.py", ["**/gen.py"])
    assert fmt._glob_match("x/y/gen.py", ["**/gen.py"])
    assert not fmt._glob_match("a/b.py", ["b/*"])
    assert fmt._glob_match("a/b.py", ["a\\*.py"])  # Windows-style globs are accepted


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[tool.ruff]\nline-length = 100\n", True),
        ("[ tool.ruff ]  # comment\n", True),
        ("[tool.ruff]\r\nx = 1\r\n", True),
        ("[tool.ruff.format]\nquote-style = 'single'\n", True),
        (
            "[tool.ruff.lint]\nselect = ['E']\n",
            False,
        ),  # linter-only config does not choose ruff as the formatter
        ("[tool.ruffle]\n", False),
        ("# [tool.ruff]\n", False),
        ("[project]\nname = 'x'\n", False),
    ],
)
def test_toml_table_detection(text, expected):
    assert fmt._toml_has_table(text, "tool.ruff", "tool.ruff.format") is expected


def test_cmd_shim_unsafe_characters():
    assert fmt._cmd_shim_unsafe(["C:/x/prettier.cmd", "--stdin-filepath", "C:/r&d/a.ts"])
    assert fmt._cmd_shim_unsafe(["C:/x/prettier.CMD", "--stdin-filepath", "C:/100%/a.ts"])
    assert fmt._cmd_shim_unsafe(["x.bat", "a|b.ts"])
    assert not fmt._cmd_shim_unsafe(
        ["C:/x/prettier.cmd", "--stdin-filepath", "C:/my repo/(app)/a.ts"]
    )
    assert not fmt._cmd_shim_unsafe(
        ["C:/x/ruff.exe", "C:/r&d/a.py"]
    )  # real executables are not parsed by cmd.exe


def test_child_environment_has_no_credentials_and_no_git_redirects(monkeypatch):
    for k, v in {
        "GH_TOKEN": "t",
        "ANTHROPIC_API_KEY": "k",
        "AWS_SECRET_ACCESS_KEY": "s",
        "MY_PASSWORD": "p",
        "GIT_DIR": "/elsewhere",
        "GIT_INDEX_FILE": "/elsewhere/index",
        "KEEP_ME": "yes",
    }.items():
        monkeypatch.setenv(k, v)
    env = fmt._clean_env()
    assert env["KEEP_ME"] == "yes" and "PATH" in {k.upper() for k in env}
    for gone in (
        "GH_TOKEN",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "MY_PASSWORD",
        "GIT_DIR",
        "GIT_INDEX_FILE",
    ):
        assert gone not in env
    assert env["GIT_OPTIONAL_LOCKS"] == "0"


CREDENTIAL_LIKE = [
    # what verify.scrubbed_env drops, and what fmt used to let through (a formatter may run repo-controlled code)
    "AWS_ACCESS_KEY_ID",
    "STRIPE_KEY",
    "ENCRYPTION_KEY",
    "SIGNING_KEY",
    "DD_APP_KEY",
    # the other names the review found reaching the formatter
    "GH_PAT",
    "CI_JOB_JWT",
    "SENTRY_DSN",
    "SLACK_WEBHOOK_URL",
    "DATABASE_URL",
    # the families the review asked for
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "AWS_REGION",
    "AZURE_CLIENT_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GCP_PROJECT",
    "GITHUB_ACTOR",
    "GH_HOST",
    "NPM_CONFIG_REGISTRY",
    "SSH_AUTH_SOCK",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "DB_PWD",
    "MY_COOKIE",
    "ORM_CONNECTION_STRING",
    "TLS_PRIVATE_FILE",
    "SSL_CERT_FILE",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
    "GIT_CONFIG_PARAMETERS",
]


@pytest.mark.parametrize("name", CREDENTIAL_LIKE)
def test_child_environment_drops_every_credential_like_name(monkeypatch, name):
    monkeypatch.setenv(name, "secret-value")
    assert name.upper() not in {k.upper() for k in fmt._clean_env()}


def test_child_environment_drops_urls_with_embedded_passwords_whatever_they_are_called(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:hunter2@proxy.example:3128")
    monkeypatch.setenv("REDIS_URL", "redis://:hunter2@cache.example:6379/0")
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://app:hunter2@db.example/x")
    monkeypatch.setenv("PIP_INDEX_URL", "https://__token__:hunter2@pypi.example/simple")
    monkeypatch.setenv("NO_PROXY_URL", "http://proxy.example:3128")  # no credentials: stays
    monkeypatch.setenv(
        "REPO_HOMEPAGE", "https://example.com/a@b/c"
    )  # an @ without user:password: stays
    env = fmt._clean_env()
    for gone in ("HTTPS_PROXY", "REDIS_URL", "MONGODB_URI", "PIP_INDEX_URL"):
        assert gone not in {k.upper() for k in env}
    assert env["NO_PROXY_URL"] == "http://proxy.example:3128"
    assert env["REPO_HOMEPAGE"] == "https://example.com/a@b/c"


def test_child_environment_keeps_what_git_and_toolchains_need(monkeypatch):
    kept = {
        "GIT_AUTHOR_NAME": "A",
        "GIT_AUTHOR_EMAIL": "a@example.com",
        "GIT_COMMITTER_NAME": "C",
        "GIT_COMMITTER_EMAIL": "c@example.com",
        "CARGO_HOME": "/cargo",
        "RUSTUP_HOME": "/rustup",
        "HOME": "/home/u",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "VIRTUAL_ENV": "/venv",
        "GOPATH": "/go",
    }
    for k, v in kept.items():
        monkeypatch.setenv(k, v)
    env = {k.upper(): v for k, v in fmt._clean_env().items()}
    for k, v in kept.items():
        assert env[k] == v, k
    assert "PATH" in env


def test_git_still_works_when_injected_git_config_is_in_the_environment(repo, monkeypatch):
    """GIT_CONFIG_COUNT without its GIT_CONFIG_KEY_n makes git die, so the family is dropped together."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fmtprobe")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "x")
    r = fmt._git(repo, "rev-parse", "--show-toplevel", timeout=30)
    assert r.rc == 0, r.err


def test_windows_children_do_not_search_the_repository_directory_first(monkeypatch):
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    with monkeypatch.context() as m:
        m.setattr(paths, "IS_WINDOWS", True)
        assert fmt._clean_env()["NoDefaultCurrentDirectoryInExePath"] == "1"
    with monkeypatch.context() as m:
        m.setattr(paths, "IS_WINDOWS", False)
        assert "NoDefaultCurrentDirectoryInExePath" not in fmt._clean_env()


def test_taskkill_is_called_by_absolute_path_never_by_bare_name(tmp_path, monkeypatch):
    system32 = tmp_path / "Windows" / "System32"
    system32.mkdir(parents=True)
    exe = system32 / "taskkill.exe"
    exe.write_bytes(b"")
    calls = []
    killed = []
    proc = types.SimpleNamespace(pid=4242, kill=lambda: killed.append(True))
    with monkeypatch.context() as m:
        m.setattr(paths, "IS_WINDOWS", True)
        m.setattr(procutil, "run", lambda argv, **kw: calls.append(argv))
        m.setenv("SystemRoot", str(tmp_path / "Windows"))
        fmt._kill_tree(proc)
        assert calls == [[str(exe), "/PID", "4242", "/T", "/F"]] and killed == [True]
        # no taskkill.exe in System32: the bare name is the fallback, and procutil.run resolves it without the cwd
        exe.unlink()
        calls.clear()
        fmt._kill_tree(proc)
        assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]
        # a relative SystemRoot is never trusted
        m.setenv("SystemRoot", "relative-dir")
        assert not fmt._taskkill_exe().startswith("relative-dir")


def test_git_is_resolved_through_procutil_and_never_from_the_current_directory(
    tmp_path, monkeypatch
):
    with monkeypatch.context() as m:
        m.setattr(fmt, "_GIT_EXE", {})
        seen = []
        m.setattr(procutil, "resolve_exe", lambda name, env=None: seen.append(name) or "/abs/git")
        assert fmt._git_exe() == "/abs/git" and seen == ["git"]
    planted = tmp_path / "clone"
    planted.mkdir()
    (planted / ("git.exe" if paths.IS_WINDOWS else "git")).write_bytes(b"")
    (planted / ("git.exe" if paths.IS_WINDOWS else "git")).chmod(0o755)
    with monkeypatch.context() as m:
        m.setattr(fmt, "_GIT_EXE", {})
        m.setenv(
            "PATH", os.pathsep.join([".", ""])
        )  # relative and empty entries: the current directory
        m.chdir(planted)
        assert fmt._git_exe() is None
        assert fmt._git(planted, "--version", timeout=10).rc == 127  # reported, never run


@pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_rustfmt_banner_is_not_written_into_the_file(tmp_path, eol):
    banner = tmp_path / "rustfmt.py"
    payload = f"stdin:{eol}{eol}fn main() {{}}{eol}".encode()
    banner.write_text(
        f"import sys\nsys.stdin.read()\nsys.stdout.buffer.write({payload!r})\n", encoding="utf-8"
    )

    @dataclass(frozen=True)
    class Banner(fmt.Formatter):
        def stdin_argv(self, path):
            return [self.exe, str(banner)]

    f = Banner("rustfmt", sys.executable)
    got = fmt.format_text(f, tmp_path / "a.rs", "fn  main(){}\n", cwd=tmp_path, timeout=30)
    assert got == "fn main() {}" + eol
    other = Banner("ruff", sys.executable)  # only rustfmt output is cleaned
    assert fmt.format_text(other, tmp_path / "a.py", "x\n", cwd=tmp_path, timeout=30).startswith(
        "stdin:"
    )


def test_timeout_kills_the_whole_process_tree(repo, fake_script, monkeypatch, tmp_path):
    """A wrapper (npm's .cmd shim, a shell script) runs the real formatter as a child. Killing only the wrapper
    would leave the child holding the pipes, and the call would block until it exits."""
    tools = tmp_path / "wrapper tools"
    tools.mkdir()
    if sys.platform == "win32":
        wrapper = tools / "wrap.cmd"
        wrapper.write_bytes(f'@echo off\r\n"{sys.executable}" "{fake_script}" %*\r\n'.encode())
    else:
        wrapper = tools / "wrap.sh"
        wrapper.write_text(
            f'#!/bin/sh\n"{sys.executable}" "{fake_script}" "$@"\n', encoding="utf-8"
        )
        wrapper.chmod(0o755)
    f = Fake("prettier", str(wrapper), "sleep", "")
    monkeypatch.setattr(fmt, "detect", lambda path, root: f)
    monkeypatch.setattr(fmt, "_PER_FILE_MAX_S", 1.0)
    put(repo / "a.py", "x = 1  \n")
    started = time.monotonic()
    report = fmt.format_changed(repo, trusted=True)
    took = time.monotonic() - started
    assert len(report.errors) == 1 and report.errors[0][1].startswith("timeout after")
    assert (
        took < 5.0
    )  # the fake sleeps 60 s, and a surviving child would add the 5 s drain wait on top


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe shims exist only on Windows")
def test_cmd_shim_formatter_runs_with_spaces_and_refuses_unsafe_names(
    repo, fake_script, monkeypatch, tmp_path
):
    tools = tmp_path / "my tools"
    tools.mkdir()
    shim = tools / "fake fmt.cmd"
    shim.write_bytes(f'@echo off\r\n"{sys.executable}" "{fake_script}" %*\r\n'.encode())
    f = Fake("prettier", str(shim), "strip", "")
    monkeypatch.setattr(fmt, "detect", lambda path, root: f)
    ok = put(repo / "dir with space" / "a.py", "x = 1  \n")
    unsafe = put(repo / "r&d.py", "x = 1  \n")
    report = fmt.format_changed(repo, trusted=True)
    assert report.formatted == ["dir with space/a.py"]
    assert ok.read_bytes() == b"x = 1\n"
    assert (
        len(report.errors) == 1
        and report.errors[0][0] == "r&d.py"
        and "unsafe" in report.errors[0][1]
    )
    assert unsafe.read_bytes() == b"x = 1  \n"


# ---- detection -------------------------------------------------------------------------------------------------


@pytest.fixture(params=[False, True], ids=["posix-names", "windows-names"])
def win(request, monkeypatch):
    """Emulate either OS's executable naming; detection only touches the filesystem, so this works on any host."""
    monkeypatch.setattr(paths, "IS_WINDOWS", request.param)
    return request.param


def mk(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    path.chmod(0o755)
    return path


def venv_tool(root, name, win):
    return mk(root / ".venv" / ("Scripts" if win else "bin") / (name + ".exe" if win else name))


def node_tool(root, name, win):
    return mk(root / "node_modules" / ".bin" / (name + ".cmd" if win else name))


def path_tool(bindir, name, win):
    return mk(bindir / (name + ".exe" if win else name))


@pytest.fixture
def proj(tmp_path, monkeypatch):
    root = tmp_path / "proj dir"
    root.mkdir()
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))  # a clean PATH: only what a test puts there can be found
    return root


def same(exe, expected):
    return os.path.samefile(exe, expected)


def test_ruff_is_chosen_from_pyproject_table_and_found_in_the_project_venv(proj, win):
    (proj / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    tool = venv_tool(proj, "ruff", win)
    f = fmt.detect(proj / "a.py", proj)
    assert f is not None and f.name == "ruff" and same(f.exe, tool)
    assert fmt.detect(proj / "stubs" / "a.pyi", proj).name == "ruff"


@pytest.mark.parametrize("config", ["ruff.toml", ".ruff.toml"])
def test_ruff_is_chosen_from_ruff_toml(proj, win, config):
    (proj / config).write_text("line-length = 100\n", encoding="utf-8")
    venv_tool(proj, "ruff", win)
    assert fmt.detect(proj / "a.py", proj).name == "ruff"


def test_ruff_format_table_alone_selects_ruff(proj, win):
    (proj / "pyproject.toml").write_text(
        "[tool.ruff.format]\nquote-style = 'single'\n", encoding="utf-8"
    )
    venv_tool(proj, "ruff", win)
    assert fmt.detect(proj / "a.py", proj).name == "ruff"


def test_ruff_lint_only_config_does_not_pick_ruff_over_black(proj, win):
    (proj / "pyproject.toml").write_text(
        "[tool.ruff.lint]\nselect = ['E']\n\n[tool.black]\n", encoding="utf-8"
    )
    venv_tool(proj, "ruff", win)
    venv_tool(proj, "black", win)
    assert fmt.detect(proj / "a.py", proj).name == "black"


def test_black_when_only_black_is_configured(proj, win):
    (proj / "pyproject.toml").write_text("[tool.black]\nline-length = 100\n", encoding="utf-8")
    venv_tool(proj, "ruff", win)  # installed but not configured: never imposed
    tool = venv_tool(proj, "black", win)
    f = fmt.detect(proj / "a.py", proj)
    assert f.name == "black" and same(f.exe, tool)


def test_ruff_wins_when_both_are_configured(proj, win):
    (proj / "pyproject.toml").write_text("[tool.black]\n\n[tool.ruff]\n", encoding="utf-8")
    venv_tool(proj, "black", win)
    venv_tool(proj, "ruff", win)
    assert fmt.detect(proj / "a.py", proj).name == "ruff"


def test_configured_but_not_installed_means_no_formatter(proj, win):
    (proj / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    assert fmt.detect(proj / "a.py", proj) is None


def test_nothing_configured_means_nothing_is_imposed(proj, win, tmp_path, monkeypatch):
    venv_tool(proj, "ruff", win)
    bindir = tmp_path / "bin"
    path_tool(bindir, "ruff", win)
    monkeypatch.setenv("PATH", str(bindir))
    (proj / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    assert fmt.detect(proj / "a.py", proj) is None
    assert fmt.detect(proj / "a.txt", proj) is None


def test_project_venv_beats_path_and_path_is_the_fallback(proj, win, tmp_path, monkeypatch):
    (proj / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    on_path = path_tool(bindir, "ruff", win)
    monkeypatch.setenv("PATH", str(bindir))
    assert same(fmt.detect(proj / "a.py", proj).exe, on_path)
    in_venv = venv_tool(proj, "ruff", win)
    assert same(fmt.detect(proj / "a.py", proj).exe, in_venv)


def test_relative_path_entries_are_ignored(proj, win, monkeypatch):
    (proj / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    path_tool(
        proj / "relbin", "ruff", win
    )  # a planted binary reachable only through a relative PATH entry
    monkeypatch.setenv("PATH", "relbin")
    monkeypatch.chdir(proj)
    assert fmt.detect(proj / "a.py", proj) is None


def test_config_is_found_walking_up_and_the_nearest_one_wins(proj, win):
    (proj / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    venv_tool(proj, "ruff", win)
    venv_tool(proj, "black", win)
    deep = proj / "pkg" / "deep"
    assert fmt.detect(deep / "x.py", proj).name == "ruff"
    (proj / "pkg").mkdir(exist_ok=True)
    (proj / "pkg" / "pyproject.toml").write_text("[tool.black]\n", encoding="utf-8")
    assert fmt.detect(deep / "x.py", proj).name == "black"
    assert fmt.detect(proj / "top.py", proj).name == "ruff"


def test_path_outside_root_has_no_formatter(proj, win, tmp_path):
    (proj / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    venv_tool(proj, "ruff", win)
    assert fmt.detect(tmp_path / "elsewhere" / "a.py", proj) is None


# ---- detection reads files the repository controls: bounded, regular files only ----------------------------------


def test_an_oversized_config_file_is_never_read_and_means_not_configured(proj, win, monkeypatch):
    monkeypatch.setattr(fmt, "_MAX_CONFIG_BYTES", 300)
    venv_tool(proj, "ruff", win)
    config = proj / "pyproject.toml"
    config.write_text("[tool.ruff]\n" + "# padding\n" * 40, encoding="utf-8")  # about 400 bytes
    assert config.stat().st_size > 300
    assert fmt.detect(proj / "a.py", proj) is None
    config.write_text("[tool.ruff]\n" + "# padding\n" * 10, encoding="utf-8")
    assert fmt.detect(proj / "a.py", proj).name == "ruff"


def test_read_regular_never_takes_more_than_limit_plus_one_bytes(tmp_path, monkeypatch):
    big = tmp_path / "grows.toml"
    big.write_bytes(b"x" * 1000)
    requested = []
    real_read, real_stat = os.read, os.stat

    def spy(fd, n):
        requested.append(n)
        return real_read(fd, n)

    def stale_stat(p, *a, **kw):  # the file "was small" when it was checked, and then it grew
        st = real_stat(p, *a, **kw)
        return types.SimpleNamespace(st_mode=st.st_mode, st_size=0)

    with monkeypatch.context() as m:
        m.setattr(os, "read", spy)
        m.setattr(os, "stat", stale_stat)
        assert fmt._read_regular(big, 100) is None
    assert sum(requested) <= 101
    assert fmt._read_regular(big, 1000) == b"x" * 1000
    assert fmt._read_regular(big, 999) is None
    assert fmt._read_regular(tmp_path / "missing.toml", 10) is None


def test_a_directory_named_like_a_config_file_is_not_a_config(proj, win):
    venv_tool(proj, "ruff", win)
    (proj / "pyproject.toml").mkdir()
    assert fmt._read_text(proj / "pyproject.toml") is None
    assert fmt.detect(proj / "a.py", proj) is None


def _within(seconds, fn, *args):
    """Run fn in a daemon thread and fail (instead of hanging the suite) if it blocks."""
    box = []
    t = threading.Thread(target=lambda: box.append(fn(*args)), daemon=True)
    t.start()
    t.join(seconds)
    assert box, f"{fn.__name__} blocked for more than {seconds} s"
    return box[0]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
@pytest.mark.parametrize("name", ["pyproject.toml", "package.json", "Cargo.toml"])
def test_a_fifo_in_place_of_a_config_file_does_not_block_detection(proj, win, name):
    """open() on a FIFO with nobody writing blocks forever; the Stop hook would hang until Claude Code kills it."""
    os.mkfifo(proj / name)
    assert _within(10, fmt._read_text, proj / name) is None
    venv_tool(proj, "ruff", win)
    assert _within(10, fmt.detect, proj / "a.py", proj) is None
    assert _within(10, fmt.detect, proj / "a.ts", proj) is None
    assert _within(10, fmt._rust_edition, proj / "src" / "lib.rs") == "2021"


@pytest.mark.skipif(not os.path.exists("/dev/null"), reason="needs /dev/null")
def test_a_symlink_to_a_device_is_not_read(proj):
    """/dev/zero (endless) and /dev/ptmx (opening it allocates a terminal) are the dangerous ones. /dev/null is the safe
    stand-in for the test: it reads as empty text if the file kind is not checked, and as None when it is."""
    try:
        os.symlink("/dev/null", proj / "pyproject.toml")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    assert (
        fmt._read_text(proj / "pyproject.toml") is None
    )  # os.stat follows the link: a character device


def test_hostile_nesting_in_a_config_file_does_not_abort_detection(proj, win):
    node_tool(proj, "prettier", win)
    (proj / "package.json").write_text(
        '{"name": "x", "prettier": {"semi": false}}\n', encoding="utf-8"
    )
    sub = proj / "sub"
    sub.mkdir()
    # package.json mentions "prettier" and is deeply nested: json.loads raises RecursionError, not ValueError
    (sub / "package.json").write_text('{"prettier": ' + "[" * 100_000, encoding="utf-8")
    assert (
        fmt.detect(sub / "a.ts", proj).name == "prettier"
    )  # the parent's configuration is still found


def test_rust_edition_survives_a_hostile_manifest_and_a_directory(tmp_path):
    (tmp_path / "Cargo.toml").write_text("a = " + "[" * 100_000, encoding="utf-8")
    assert fmt._rust_edition(tmp_path / "src" / "lib.rs") == "2021"
    nested = tmp_path / "nested"
    (nested / "Cargo.toml").mkdir(parents=True)  # a directory is not a manifest
    assert fmt._rust_edition(nested / "lib.rs") == "2021"


@pytest.mark.parametrize(
    "config",
    [
        ".prettierrc",
        ".prettierrc.json",
        ".prettierrc.yaml",
        "prettier.config.js",
        "prettier.config.mjs",
    ],
)
def test_prettier_needs_its_config_and_the_project_install(proj, win, config):
    (proj / config).write_text("{}\n", encoding="utf-8")
    assert fmt.detect(proj / "a.ts", proj) is None  # config but no node_modules/.bin/prettier
    tool = node_tool(proj, "prettier", win)
    for name in (
        "a.ts",
        "b.tsx",
        "c.js",
        "d.jsx",
        "e.css",
        "f.scss",
        "g.json",
        "h.md",
        "i.yaml",
        "j.mdx",
    ):
        f = fmt.detect(proj / "src" / name, proj)
        assert f is not None and f.name == "prettier" and same(f.exe, tool), name


def test_prettier_key_in_package_json_counts_as_config(proj, win):
    node_tool(proj, "prettier", win)
    (proj / "package.json").write_text(
        '{"name": "x", "devDependencies": {"prettier": "3"}}\n', encoding="utf-8"
    )
    assert fmt.detect(proj / "a.ts", proj) is None  # a dependency is not a config
    (proj / "package.json").write_text(
        '{"name": "x", "prettier": {"semi": false}}\n', encoding="utf-8"
    )
    assert fmt.detect(proj / "a.ts", proj).name == "prettier"


def test_prettier_is_never_taken_from_path_or_npx(proj, win, tmp_path, monkeypatch):
    (proj / ".prettierrc").write_text("{}\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    path_tool(bindir, "prettier", win)
    monkeypatch.setenv("PATH", str(bindir))
    assert fmt.detect(proj / "a.ts", proj) is None


def test_markdown_and_yaml_only_through_a_repo_configured_prettier(proj, win):
    node_tool(proj, "biome", win)
    (proj / "biome.json").write_text("{}\n", encoding="utf-8")
    assert fmt.detect(proj / "README.md", proj) is None
    assert fmt.detect(proj / "ci.yaml", proj) is None
    assert fmt.detect(proj / "a.scss", proj) is None
    (proj / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert fmt.detect(proj / "x.toml", proj) is None


def test_biome_needs_its_config_and_install(proj, win):
    (proj / "biome.jsonc").write_text("{}\n", encoding="utf-8")
    assert fmt.detect(proj / "a.ts", proj) is None
    tool = node_tool(proj, "biome", win)
    for name in ("a.ts", "b.jsx", "c.json", "d.css"):
        f = fmt.detect(proj / name, proj)
        assert f is not None and f.name == "biome" and same(f.exe, tool), name


def test_prettier_wins_over_biome_when_both_are_set_up(proj, win):
    for cfg in (".prettierrc", "biome.json"):
        (proj / cfg).write_text("{}\n", encoding="utf-8")
    node_tool(proj, "prettier", win)
    node_tool(proj, "biome", win)
    assert fmt.detect(proj / "a.ts", proj).name == "prettier"


def test_biome_is_used_when_prettier_is_configured_but_not_installed(proj, win):
    for cfg in (".prettierrc", "biome.json"):
        (proj / cfg).write_text("{}\n", encoding="utf-8")
    node_tool(proj, "biome", win)
    assert fmt.detect(proj / "a.ts", proj).name == "biome"


def test_gofmt_needs_go_mod_and_gofmt_on_path(proj, win, tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    tool = path_tool(bindir, "gofmt", win)
    monkeypatch.setenv("PATH", str(bindir))
    assert fmt.detect(proj / "main.go", proj) is None  # no go.mod
    (proj / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    f = fmt.detect(proj / "cmd" / "main.go", proj)
    assert f.name == "gofmt" and same(f.exe, tool)
    monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))
    assert fmt.detect(proj / "main.go", proj) is None  # go.mod but no gofmt


def test_rustfmt_needs_cargo_toml_and_rustfmt_on_path(proj, win, tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    tool = path_tool(bindir, "rustfmt", win)
    monkeypatch.setenv("PATH", str(bindir))
    assert fmt.detect(proj / "src" / "lib.rs", proj) is None
    (proj / "Cargo.toml").write_text('[package]\nname = "x"\nedition = "2018"\n', encoding="utf-8")
    f = fmt.detect(proj / "src" / "lib.rs", proj)
    assert f.name == "rustfmt" and same(f.exe, tool)
    assert f.stdin_argv(proj / "src" / "lib.rs")[1:] == ["--emit", "stdout", "--edition", "2018"]


@pytest.mark.parametrize(
    ("cargo", "edition"),
    [
        ('[package]\nname = "x"\nedition = "2024"\n', "2024"),
        ('[package]\nname = "x"\n', "2021"),
        ('[package]\nedition = "1999"\n', "2021"),
        ("this is [not toml", "2021"),
        ("[package]\nedition = 2021\n", "2021"),
    ],
)
def test_rust_edition_comes_from_cargo_toml(tmp_path, cargo, edition):
    (tmp_path / "Cargo.toml").write_text(cargo, encoding="utf-8")
    assert fmt._rust_edition(tmp_path / "src" / "lib.rs") == edition


def test_rust_edition_follows_the_workspace(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["m"]\n[workspace.package]\nedition = "2024"\n', encoding="utf-8"
    )
    member = tmp_path / "m"
    member.mkdir()
    (member / "Cargo.toml").write_text(
        '[package]\nname = "m"\nedition.workspace = true\n', encoding="utf-8"
    )
    assert fmt._rust_edition(member / "src" / "lib.rs") == "2024"
    assert fmt._rust_edition(tmp_path / "other.rs") == "2021"


def test_stdin_argv_for_every_tool(tmp_path):
    p = tmp_path / "dir with space" / "f.ext"
    s = str(p)
    cases = {
        "ruff": ["EXE", "format", "--stdin-filename", s, "-"],
        "black": ["EXE", "-q", "--stdin-filename", s, "-"],
        "prettier": ["EXE", "--stdin-filepath", s],
        "biome": ["EXE", "format", "--stdin-file-path=" + s],
        "gofmt": ["EXE"],
        "rustfmt": ["EXE", "--emit", "stdout", "--edition", "2021"],
    }
    for name, argv in cases.items():
        assert fmt.Formatter(name, "EXE").stdin_argv(p) == argv, name
    with pytest.raises(ValueError):
        fmt.Formatter("nope", "EXE").stdin_argv(p)


def test_describe_lists_each_detected_formatter_with_its_extensions(proj, win):
    assert fmt.describe(proj) == []
    (proj / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    (proj / ".prettierrc").write_text("{}\n", encoding="utf-8")
    ruff = venv_tool(proj, "ruff", win)
    prettier = node_tool(proj, "prettier", win)
    got = {d["name"]: d for d in fmt.describe(proj, trusted=True)}
    assert set(got) == {"ruff", "prettier"}
    assert (
        got["ruff"]["extensions"] == [".py", ".pyi"]
        and got["ruff"]["configured_by"] == "pyproject.toml"
    )
    assert same(got["ruff"]["exe"], ruff) and "usable" not in got["ruff"]
    assert got["prettier"]["configured_by"] == ".prettierrc" and same(
        got["prettier"]["exe"], prettier
    )
    assert {".ts", ".md", ".yaml", ".css"} <= set(got["prettier"]["extensions"])


# ---- real tools (run only where installed; nothing is downloaded) -----------------------------------------------


REAL = [
    (
        "gofmt",
        "gofmt",
        "f.go",
        "package main\nfunc  main(){}\n",
        "package main\n\nfunc main() {}\n",
    ),
    ("rustfmt", "rustfmt", "f.rs", "fn  main(){}\n", "fn main() {}\n"),
    ("ruff", "ruff", "f.py", "x=1\n", "x = 1\n"),
    ("black", "black", "f.py", "x=1\n", "x = 1\n"),
]


@pytest.mark.parametrize(("name", "binary", "filename", "src", "want"), REAL)
def test_real_tool_stdin_roundtrip(tmp_path, name, binary, filename, src, want):
    exe = fmt._which(binary)
    if exe is None:
        pytest.skip(f"{binary} is not installed")
    f = fmt.Formatter(name, exe)
    got = fmt.format_text(f, tmp_path / filename, src, cwd=tmp_path, timeout=60)
    assert got == want
    assert (
        fmt.format_text(f, tmp_path / filename, want, cwd=tmp_path, timeout=60) == want
    )  # idempotent


def test_real_gofmt_crlf_head_that_is_clean_is_formatted_and_stays_crlf(repo):
    """gofmt always emits LF. A CRLF blob that is clean apart from that used to be judged "pre-existing drift"."""
    if fmt._which("gofmt") is None:
        pytest.skip("gofmt is not installed")
    put(repo / "go.mod", "module example.com/x\n\ngo 1.21\n")
    a = put(repo / "a.go", b"package main\r\n\r\nfunc a() {\r\n}\r\n")
    commit(repo)
    a.write_bytes(
        b"package main\r\n\r\nfunc a() {\r\n}\r\n\r\nfunc   b(  ) {\r\nx:=1\r\n_ = x\r\n}\r\n"
    )
    report = fmt.format_changed(repo)
    assert report.formatted == ["a.go"] and report.skipped == [] and report.errors == []
    assert a.read_bytes() == (
        b"package main\r\n\r\nfunc a() {\r\n}\r\n\r\nfunc b() {\r\n\tx := 1\r\n\t_ = x\r\n}\r\n"
    )


def test_real_gofmt_does_not_reformat_a_drifted_file_that_was_moved(repo):
    if fmt._which("gofmt") is None:
        pytest.skip("gofmt is not installed")
    put(repo / "go.mod", "module example.com/x\n\ngo 1.21\n")
    put(repo / "a.go", "package main\nfunc  A(){}\n")  # drifted at HEAD
    commit(repo)
    git(repo, "mv", "a.go", "b.go")
    moved = repo / "b.go"
    drifted = b"package main\nfunc  A(){}\nfunc B() {}\n"
    moved.write_bytes(drifted)
    report = fmt.format_changed(repo)
    assert report.formatted == [] and report.skipped == [("b.go", fmt.DRIFT_REASON)]
    assert moved.read_bytes() == drifted


def test_real_gofmt_end_to_end_through_format_changed(repo):
    if fmt._which("gofmt") is None:
        pytest.skip("gofmt is not installed")
    put(repo / "go.mod", "module example.com/x\n\ngo 1.21\n")
    bad = put(repo / "cmd" / "main.go", "package main\nfunc  main(){}\n")
    report = fmt.format_changed(repo)
    assert report.formatted == ["cmd/main.go"] and report.formatters == ["gofmt"]
    assert bad.read_bytes() == b"package main\n\nfunc main() {}\n"
    assert fmt.format_changed(repo).formatted == []  # second run: nothing left to do

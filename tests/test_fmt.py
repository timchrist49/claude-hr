"""claude_hr.fmt: repo-aware formatting of changed files.

The formatting tests drive a fake formatter (a tiny Python script run through sys.executable) wired in through the
Formatter dataclass, against real temporary git repos. Detection tests create config files and fake executables, and
emulate both OS naming schemes (`ruff.exe` vs `ruff`) on any host. A few tests run a real tool when it is on PATH.
"""

import os
import subprocess
import sys
import time
from dataclasses import dataclass

import pytest

from claude_hr import fmt, paths

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


def test_gate_treats_an_eol_normalising_formatter_as_drift_for_a_crlf_head(repo, fake):
    fake("lf")
    f = put(repo / "win.py", "a = 1\r\n")
    commit(repo)  # HEAD is CRLF, and the formatter wants LF: formatting would rewrite every line
    f.write_bytes(b"a = 1\r\nb = 2  \r\n")
    report = fmt.format_changed(repo)
    assert report.skipped == [("win.py", fmt.DRIFT_REASON)]
    assert f.read_bytes() == b"a = 1\r\nb = 2  \r\n"


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
    report = fmt.format_changed(repo)
    assert report.formatted == [] and report.errors == []
    assert report.skipped[0][0] == "a.py" and "tracked in git" in report.skipped[0][1]
    assert f.read_bytes() == b"x = 1  \n"
    assert fmt.describe(repo)[0]["usable"] is False


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
    report = fmt.format_changed(repo)
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
    report = fmt.format_changed(repo)
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
    got = {d["name"]: d for d in fmt.describe(proj)}
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


def test_real_gofmt_end_to_end_through_format_changed(repo):
    if fmt._which("gofmt") is None:
        pytest.skip("gofmt is not installed")
    put(repo / "go.mod", "module example.com/x\n\ngo 1.21\n")
    bad = put(repo / "cmd" / "main.go", "package main\nfunc  main(){}\n")
    report = fmt.format_changed(repo)
    assert report.formatted == ["cmd/main.go"] and report.formatters == ["gofmt"]
    assert bad.read_bytes() == b"package main\n\nfunc main() {}\n"
    assert fmt.format_changed(repo).formatted == []  # second run: nothing left to do

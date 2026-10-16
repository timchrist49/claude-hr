"""The suite's own safety nets: what the sandbox hides from git, and what the real-profile breach guard can see."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import conftest
import pytest

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is needed")


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=60
    )


def git_version():
    m = re.search(r"(\d+)\.(\d+)", git(".", "--version").stdout)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


@needs_git
@pytest.mark.skipif(
    shutil.which("git") is not None and git_version() < (2, 32),
    reason="GIT_CONFIG_GLOBAL needs git 2.32",
)
def test_the_developers_own_git_configuration_does_not_reach_a_test(sandbox, tmp_path, monkeypatch):
    """A global ignore for `.claude/` in ~/.gitconfig made the test that the generated recipe is kept out of `git status`
    pass whatever the code did."""
    dev_home = tmp_path / "dev-home"
    dev_home.mkdir()
    (dev_home / "ignore").write_text(".claude/\n", encoding="utf-8")
    (dev_home / ".gitconfig").write_text(
        f"[core]\n\texcludesFile = {(dev_home / 'ignore').as_posix()}\n[user]\n\tname = Somebody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(dev_home))
    monkeypatch.setenv("USERPROFILE", str(dev_home))
    repo = tmp_path / "repo"
    repo.mkdir()
    assert git(repo, "init", "-q").returncode == 0
    (repo / ".claude").mkdir()
    (repo / ".claude" / "hr-verify.toml").write_text("x\n", encoding="utf-8")
    assert ".claude" in git(repo, "status", "--porcelain").stdout, (
        "the developer's global ignore leaked in"
    )
    assert git(repo, "config", "--get", "user.name").stdout.strip() == ""
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
    assert Path(os.environ["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8") == ""


def test_the_breach_guard_sees_a_write_below_the_real_claude_hr_folder(tmp_path):
    claude_hr = tmp_path / "real-home" / ".claude-hr"
    (claude_hr / "state").mkdir(parents=True)
    offer = claude_hr / "state" / "verify-offer.json"
    trust = claude_hr / "state" / "verify-trust.json"
    watched = [claude_hr, offer, trust]

    before = conftest._fingerprint(watched)
    offer.write_text("{}\n", encoding="utf-8")  # the folder's own mtime does not move for this
    changed = conftest._changed(before, conftest._fingerprint(watched))
    assert str(claude_hr) not in changed, "the blind spot the files are watched by name for"
    assert list(changed) == [str(offer)]

    before = conftest._fingerprint(watched)
    offer.write_text("{}\n", encoding="utf-8")
    os.utime(offer, ns=(1_000_000_000, 1_000_000_000))  # same size, another time: an edit in place
    assert list(conftest._changed(before, conftest._fingerprint(watched))) == [str(offer)]

    before = conftest._fingerprint(watched)
    trust.write_text('{"a": "b"}\n', encoding="utf-8")
    offer.unlink()
    assert set(conftest._changed(before, conftest._fingerprint(watched))) == {
        str(offer),
        str(trust),
    }
    assert conftest._changed(conftest._fingerprint(watched), conftest._fingerprint(watched)) == {}


def test_the_files_below_the_real_claude_hr_folder_that_are_watched():
    below = {
        str(p.relative_to(conftest._REAL_HOME / ".claude-hr")) for p in conftest._WATCHED_BELOW
    }
    assert {
        str(Path("state") / "verify-offer.json"),
        str(Path("state") / "verify-trust.json"),
    } <= below
    # never what the machine's own running claude-hr writes while a suite runs: that would be a false breach
    assert not {"gates.json", "headroom-proxy.json", "health.json"} & {Path(p).name for p in below}

"""State, logs and reports are owner-only and never written through a symlink (found by the commit security review)."""

import os
import stat

import pytest

from claude_hr import paths
from claude_hr.headroom import Headroom
from claude_hr.jsonio import atomic_write_text

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")


def mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def symlink_or_skip(link, target):
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")


@posix_only
def test_ensure_dirs_makes_state_owner_only(sandbox):
    home = paths.hr_home()
    home.mkdir(parents=True)
    home.chmod(0o755)  # as created by an older version or a permissive umask
    paths.ensure_dirs()
    for d in (home, paths.state_dir(), paths.logs_dir(), paths.backups_dir(), paths.cache_dir()):
        assert mode(d) == 0o700, d


def _loose_install():
    for d in (
        paths.hr_home(),
        paths.state_dir(),
        paths.logs_dir(),
        paths.backups_dir(),
        paths.cache_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o775)  # what an install from a release that predates owner-only folders looks like


@posix_only
def test_tighten_existing_fixes_loose_folders_and_creates_nothing(sandbox):
    paths.hr_home().mkdir(parents=True)
    paths.hr_home().chmod(0o755)
    paths.tighten_existing()
    assert mode(paths.hr_home()) == 0o700
    assert not paths.state_dir().exists()  # unlike ensure_dirs, nothing is created


@posix_only
def test_any_ctl_command_tightens_an_upgraded_install(sandbox):
    from claude_hr import ctl

    _loose_install()
    assert ctl._run(["config", "show"]) == 0
    for d in (
        paths.hr_home(),
        paths.state_dir(),
        paths.logs_dir(),
        paths.backups_dir(),
        paths.cache_dir(),
    ):
        assert mode(d) == 0o700, d


@posix_only
def test_launching_tightens_an_upgraded_install(sandbox):
    from claude_hr import launcher

    _loose_install()
    launcher._remember_launch(12345, None)
    for d in (
        paths.hr_home(),
        paths.state_dir(),
        paths.logs_dir(),
        paths.backups_dir(),
        paths.cache_dir(),
    ):
        assert mode(d) == 0o700, d


@posix_only
def test_ensure_dirs_never_locks_down_the_users_home(sandbox, monkeypatch):
    monkeypatch.setenv(
        "CLAUDE_HR_HOME", str(sandbox)
    )  # misconfiguration: claude-hr home == user home
    sandbox.chmod(0o755)
    paths.ensure_dirs()
    assert mode(sandbox) == 0o755


def test_atomic_write_can_refuse_to_follow_a_symlink(sandbox, tmp_path):
    target = tmp_path / "precious.txt"
    target.write_text("keep me", encoding="utf-8")
    link = tmp_path / "report.txt"
    symlink_or_skip(link, target)
    atomic_write_text(link, "new report", follow_symlinks=False)
    assert target.read_text(encoding="utf-8") == "keep me"  # the link's target is untouched
    assert not link.is_symlink() and link.read_text(encoding="utf-8") == "new report"


def test_atomic_write_still_follows_symlinks_by_default(sandbox, tmp_path):
    """Dotfiles setups symlink settings.json; managed edits must land in the real file."""
    target = tmp_path / "dotfiles-settings.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "settings.json"
    symlink_or_skip(link, target)
    atomic_write_text(link, '{"a": 1}')
    assert link.is_symlink() and target.read_text(encoding="utf-8") == '{"a": 1}'


@posix_only
def test_new_state_files_are_owner_only(sandbox):
    f = paths.state_dir() / "learn-report.txt"
    atomic_write_text(f, "x", follow_symlinks=False)
    assert mode(f) == 0o600


@posix_only
def test_proxy_log_is_owner_only_even_if_an_older_version_made_it_world_readable(sandbox):
    paths.ensure_dirs()
    log = paths.logs_dir() / "headroom-proxy.log"
    log.write_text("old", encoding="utf-8")
    log.chmod(0o644)
    with Headroom._open_log() as fh:
        fh.write(b"line\n")
    assert mode(log) == 0o600
    assert log.read_bytes() == b"oldline\n"  # appended, not truncated


@posix_only
def test_proxy_log_is_not_opened_through_a_symlink(sandbox, tmp_path):
    paths.ensure_dirs()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    symlink_or_skip(paths.logs_dir() / "headroom-proxy.log", victim)
    with pytest.raises(OSError):
        Headroom._open_log()
    assert victim.read_text(encoding="utf-8") == "keep"

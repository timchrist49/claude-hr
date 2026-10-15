"""Git worktree helpers for safe parallel agent work (one isolated checkout per task)."""

from __future__ import annotations

import re
from pathlib import Path

from . import procutil


def _git(*args: str, cwd: Path | None = None, timeout: float = 120):
    return procutil.run(["git", *args], cwd=cwd, timeout=timeout)


def repo_root(cwd: Path | None = None) -> Path | None:
    r = _git("rev-parse", "--show-toplevel", cwd=cwd)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def _safe(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9._/-]+", "-", name).strip("-/.")
    if not n or ".." in n:
        raise ValueError(f"invalid worktree name: {name!r}")
    return n


def new(name: str, base: str = "HEAD", prefix: str = "hr/", cwd: Path | None = None) -> tuple[int, str]:
    root = repo_root(cwd)
    if root is None:
        return 1, "not inside a git repository"
    slug = _safe(name)
    branch = f"{prefix}{slug}" if prefix and not slug.startswith(prefix) else slug
    path = root / ".claude" / "worktrees" / slug.replace("/", "-")
    if path.exists():
        return 1, f"{path} already exists"
    # Keep worktrees out of `git status` without touching the tracked .gitignore.
    gp = _git("rev-parse", "--git-path", "info/exclude", cwd=root)
    if gp.returncode == 0 and gp.stdout.strip():
        exclude = Path(gp.stdout.strip())
        exclude = exclude if exclude.is_absolute() else root / exclude
        try:
            existing = exclude.read_text(encoding="utf-8", errors="ignore") if exclude.exists() else ""
            if ".claude/worktrees/" not in existing:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                with exclude.open("a", encoding="utf-8") as fh:
                    sep = "" if (not existing or existing.endswith("\n")) else "\n"
                    fh.write(sep + ".claude/worktrees/\n")
        except OSError:
            pass
    r = _git("worktree", "add", "-b", branch, str(path), base, cwd=root)
    if r.returncode != 0:
        return 1, (r.stderr or r.stdout).strip()
    return 0, str(path)


def list_all(cwd: Path | None = None) -> list[dict]:
    r = _git("worktree", "list", "--porcelain", cwd=cwd)
    out, cur = [], {}
    for line in r.stdout.splitlines():
        if not line.strip():
            if cur:
                out.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v
    if cur:
        out.append(cur)
    return out


def remove(name: str, force: bool = False, delete_branch: bool = False, cwd: Path | None = None) -> tuple[int, str]:
    root = repo_root(cwd)
    if root is None:
        return 1, "not inside a git repository"
    slug = _safe(name)
    path = root / ".claude" / "worktrees" / slug.replace("/", "-")
    if not path.exists():
        return 1, f"{path} does not exist"
    st = _git("status", "--porcelain", cwd=path)
    if st.stdout.strip() and not force:
        return 1, f"{path} has uncommitted changes; commit them or pass --force"
    r = _git("worktree", "remove", *(["--force"] if force else []), str(path), cwd=root)
    if r.returncode != 0:
        return 1, (r.stderr or r.stdout).strip()
    if delete_branch:
        for branch in (f"hr/{slug}", slug):
            _git("branch", "-d", branch, cwd=root)
    return 0, f"removed {path}"

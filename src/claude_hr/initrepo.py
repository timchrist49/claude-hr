"""Scaffold the repository files an agent-driven workflow leans on: issue forms, a PR template, plan and decision
folders, editor settings, Dependabot, CODEOWNERS, REVIEW.md and a hardened CI skeleton.

Why it is safe to run anywhere: plan() only reads the repository, and apply() only creates files that are absent, so
a repo's own choices always win and a second run changes nothing. Nothing here touches git state or the network.

Templates live in data/repo-template/ under non-dot names (wheels and some tools treat dot-directories specially);
_FILES maps each template to the path it is written to.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, procutil

GROUPS = ("github", "docs", "editor", "dependabot", "codeowners", "review", "ci")

# group -> (destination in the repo, template under data/repo-template/); dependabot, codeowners and ci are built below
_FILES: dict[str, tuple[tuple[str, str], ...]] = {
    "github": (
        (".github/ISSUE_TEMPLATE/bug.yml", "github/ISSUE_TEMPLATE/bug.yml"),
        (".github/ISSUE_TEMPLATE/feature.yml", "github/ISSUE_TEMPLATE/feature.yml"),
        (".github/ISSUE_TEMPLATE/agent-task.yml", "github/ISSUE_TEMPLATE/agent-task.yml"),
        (".github/ISSUE_TEMPLATE/config.yml", "github/ISSUE_TEMPLATE/config.yml"),
        (".github/pull_request_template.md", "github/pull_request_template.md"),
    ),
    "docs": (
        ("docs/plans/README.md", "docs/plans/README.md"),
        ("docs/plans/_template.md", "docs/plans/_template.md"),
        ("docs/decisions/README.md", "docs/decisions/README.md"),
        ("docs/decisions/0000-template.md", "docs/decisions/0000-template.md"),
    ),
    "editor": ((".editorconfig", "editorconfig"), (".gitattributes", "gitattributes")),
    "review": (("REVIEW.md", "REVIEW.md"),),
}

# GitHub accepts these other spellings and locations; creating a second copy would shadow or conflict with the first
_ALTERNATES: dict[str, tuple[str, ...]] = {
    ".github/pull_request_template.md": (
        ".github/PULL_REQUEST_TEMPLATE.md",
        "pull_request_template.md",
        "PULL_REQUEST_TEMPLATE.md",
        "docs/pull_request_template.md",
        "docs/PULL_REQUEST_TEMPLATE.md",
    ),
    ".github/CODEOWNERS": ("CODEOWNERS", "docs/CODEOWNERS"),
    ".github/dependabot.yml": (".github/dependabot.yaml",),
    ".github/workflows/ci.yml": (".github/workflows/ci.yaml",),
}

_SETUP_TEMPLATES = {
    "python": "github/workflows/steps-python.yml",
    "node": "github/workflows/steps-node.yml",
    "go": "github/workflows/steps-go.yml",
}
_PIP_TOOLS = frozenset({"pytest", "ruff", "mypy", "pyright", "black", "flake8", "isort"})
_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9._-]*requirements[A-Za-z0-9._-]*\.txt$")
_MAX_COMMANDS = 12
# inherited when run from inside a git hook; they would point git at a different repository than `cwd`
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


@dataclass(frozen=True)
class Action:
    path: str  # relative to the repo root, always with forward slashes
    content: str
    exists: bool  # a file is already there (possibly at an equivalent location): it will be kept, never overwritten
    group: str


@dataclass
class ApplyResult:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # already present
    errors: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)


# ---- reading the repository -----------------------------------------------------------------------------------------


def _git(root: Path, *args: str):
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    return procutil.run(["git", *args], timeout=15, env=env, cwd=root)


_URL_REMOTE = re.compile(
    r"^(?:https?|git|ssh)://(?:[^/@\s]+@)?(?:www\.|ssh\.)?github\.com(?::\d+)?/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_SCP_REMOTE = re.compile(
    r"^[^/@:\s]+@(?:www\.|ssh\.)?github\.com:"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def parse_github_remote(url: str) -> tuple[str, str] | None:
    """(owner, repo) from a github.com remote URL (https, ssh:// or git@host:path), else None. Credentials in the URL
    are ignored and never returned."""
    text = (url or "").strip()
    m = _URL_REMOTE.match(text) or _SCP_REMOTE.match(text)
    if not m or m["repo"] in {".", ".."}:
        return None
    return m["owner"], m["repo"]


class _Repo:
    """Cached, read-only facts about the repository; git is only asked for what the selected groups need."""

    def __init__(self, root: Path):
        self.root = root

    def has(self, rel: str) -> bool:
        return os.path.lexists(self.root / rel)

    @functools.cached_property
    def requirements(self) -> list[str]:
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return []
        return [n for n in names if _REQUIREMENT_NAME.match(n) and (self.root / n).is_file()][:4]

    @functools.cached_property
    def pyproject(self) -> dict:
        try:
            with open(self.root / "pyproject.toml", "rb") as fh:
                import tomllib  # only when there is a file to parse

                data = tomllib.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @functools.cached_property
    def package_json(self) -> dict:
        try:
            with open(self.root / "package.json", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @functools.cached_property
    def python(self) -> bool:
        markers = ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile")
        return any(self.has(m) for m in markers) or bool(self.requirements)

    @functools.cached_property
    def node(self) -> bool:
        return self.has("package.json")

    @functools.cached_property
    def go(self) -> bool:
        return self.has("go.mod")

    @functools.cached_property
    def rust(self) -> bool:
        return self.has("Cargo.toml")

    @functools.cached_property
    def languages(self) -> list[str]:
        flags = (("python", self.python), ("node", self.node), ("go", self.go), ("rust", self.rust))
        return [name for name, present in flags if present]

    @functools.cached_property
    def python_package(self) -> bool:
        """`pip install -e .` only works when the repo is a package, not when pyproject.toml just holds tool settings."""
        if self.has("setup.py") or self.has("setup.cfg"):
            return True
        return "project" in self.pyproject or "build-system" in self.pyproject

    @functools.cached_property
    def has_dev_extra(self) -> bool:
        project = self.pyproject.get("project")
        extras = project.get("optional-dependencies") if isinstance(project, dict) else None
        return isinstance(extras, dict) and "dev" in extras

    @functools.cached_property
    def node_scripts(self) -> dict:
        scripts = self.package_json.get("scripts")
        return scripts if isinstance(scripts, dict) else {}

    @functools.cached_property
    def node_pm(self) -> str:
        if self.has("pnpm-lock.yaml"):
            return "pnpm"
        return "yarn" if self.has("yarn.lock") else "npm"

    @functools.cached_property
    def has_workflows(self) -> bool:
        try:
            with os.scandir(self.root / ".github" / "workflows") as it:
                return any(e.name.lower().endswith((".yml", ".yaml")) for e in it)
        except OSError:
            return False

    def ecosystems(self, *, actions: bool) -> list[str]:
        out: list[str] = []
        if self.python:
            if self.has("uv.lock"):
                out.append("uv")
                if self.requirements:
                    out.append("pip")
            else:
                out.append("pip")
        if self.node:
            out.append("npm")
        if self.go:
            out.append("gomod")
        if self.rust:
            out.append("cargo")
        if actions:
            out.append("github-actions")
        return out

    @functools.cached_property
    def origin(self) -> tuple[str, str] | None:
        r = _git(self.root, "remote", "get-url", "origin")
        return parse_github_remote(r.stdout) if r.returncode == 0 else None

    @functools.cached_property
    def repo_name(self) -> str:
        if self.origin:
            return self.origin[1]
        name = re.sub(r"[^A-Za-z0-9._ -]", "", self.root.resolve().name).strip()
        return name or "this repository"

    @functools.cached_property
    def default_branch(self) -> str:
        valid = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
        r = _git(self.root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
        remote_head = r.stdout.strip().removeprefix("origin/") if r.returncode == 0 else ""
        if remote_head and valid.match(remote_head):
            return remote_head
        r = _git(self.root, "symbolic-ref", "--quiet", "--short", "HEAD")
        current = r.stdout.strip() if r.returncode == 0 else ""
        if current in {"main", "master", "trunk"}:
            return current
        for name in ("main", "master"):
            ref = f"refs/heads/{name}"
            if _git(self.root, "show-ref", "--verify", "--quiet", ref).returncode == 0:
                return name
        return "main"


def detect_languages(root: Path) -> list[str]:
    """Languages by top-level marker file, in a fixed order: python, node, go, rust."""
    return _Repo(Path(root)).languages


def detect_ecosystems(root: Path) -> list[str]:
    """Dependabot ecosystems for the repo as it is now (github-actions only when workflow files already exist)."""
    repo = _Repo(Path(root))
    return repo.ecosystems(actions=repo.has_workflows)


# ---- rendering ------------------------------------------------------------------------------------------------------

_PLACEHOLDER = re.compile(
    r"\{\{(owner|repo|default_branch|ecosystem|ecosystems_block|steps_block|step_name|install_command|run_command)\}\}"
)
_YAML_WORDS = {"true", "false", "yes", "no", "on", "off", "y", "n", "null", "~"}
_YAML_INDICATORS = frozenset("-?:,[]{}#&*!|>'\"%@`")


def _template(name: str) -> str:
    text = (paths.package_data_dir() / "repo-template" / name).read_text(encoding="utf-8")
    return text.replace("\r\n", "\n")


def _render(text: str, **values: str) -> str:
    """One pass, so a substituted value is never scanned for further placeholders (`${{ ... }}` is left alone)."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def _yaml_scalar(text: str) -> str:
    """Plain when that is unambiguous, otherwise a double-quoted (JSON-compatible) YAML scalar."""
    plain = (
        bool(text)
        and text == text.strip()
        and text[0] not in _YAML_INDICATORS
        and not text[0].isdigit()
        and text.lower() not in _YAML_WORDS
        and ": " not in text
        and " #" not in text
        and not text.endswith(":")
        and all(" " <= c <= "~" for c in text)
    )
    return text if plain else json.dumps(text)


def _clean_commands(commands: Sequence[Sequence[str]] | None) -> tuple[list[list[str]], int]:
    """Keep only argv lists that can be written into a workflow safely; returns (kept, number dropped)."""
    kept: list[list[str]] = []
    dropped = 0
    for cmd in commands or ():
        ok = (
            isinstance(cmd, (list, tuple))
            and bool(cmd)
            and all(isinstance(a, str) for a in cmd)
            and bool(cmd[0])
        )
        argv = [str(a) for a in cmd] if ok else []
        if ok and re.match(r"^(?:[A-Za-z]:)?[\\/]", argv[0]):
            # a path from the machine that discovered the command means nothing on the runner; PATH finds it there
            exe = re.split(r"[\\/]", argv[0])[-1]
            argv[0] = exe[:-4] if exe.lower().endswith(".exe") else exe
        if not ok or not argv[0] or any(re.search(r"[\x00-\x1f\x7f]|\$\{\{", a) for a in argv):
            dropped += 1
            continue
        kept.append(argv)
    if len(kept) > _MAX_COMMANDS:
        dropped += len(kept) - _MAX_COMMANDS
        kept = kept[:_MAX_COMMANDS]
    return kept, dropped


def _pip_tools(commands: list[list[str]]) -> list[str]:
    found: set[str] = set()
    for argv in commands:
        name = argv[0]
        if name in {"python", "python3", "py"} and len(argv) > 2 and argv[1] == "-m":
            name = argv[2]
        if name in _PIP_TOOLS:
            found.add(name)
    return sorted(found)


def _default_commands(repo: _Repo, lang: str, notes: list[str]) -> list[list[str]]:
    if lang == "python":
        return [["python", "-m", "pytest", "-q"], ["ruff", "check", "."]]
    if lang == "node":
        pm, scripts = repo.node_pm, repo.node_scripts
        out = [[pm, "test"]] if "test" in scripts else []
        if "lint" in scripts:
            out.append([pm, "run", "lint"])
        if not out:
            notes.append(
                "ci: package.json has no test or lint script, so the workflow only installs dependencies"
            )
        return out
    if lang == "go":
        return [["go", "test", "./..."]]
    return [["cargo", "test"]]


def _install_command(repo: _Repo, lang: str, commands: list[list[str]]) -> str | None:
    if lang == "node":
        if repo.node_pm == "pnpm":
            return "corepack enable && pnpm install --frozen-lockfile"
        if repo.node_pm == "yarn":
            return "corepack enable && yarn install"
        return (
            "npm ci"
            if repo.has("package-lock.json") or repo.has("npm-shrinkwrap.json")
            else "npm install"
        )
    if lang != "python":
        return None
    args: list[str] = []
    if repo.python_package:
        args += ["-e", ".[dev]" if repo.has_dev_extra else "."]
    for req in repo.requirements:
        args += ["-r", req]
    args += _pip_tools(commands) if commands else ["pytest", "ruff"]
    return shlex.join(["python", "-m", "pip", "install", *args]) if args else None


# ---- group builders: (repo, wanted groups, cleaned commands, notes) -> [(path, content)] ----------------------------


def _static(group: str, repo: _Repo) -> list[tuple[str, str]]:
    out = []
    for dest, tpl in _FILES[group]:
        text = _template(tpl)
        if (
            "{{repo}}" in text
        ):  # asking for the repo name may run git; only do it when a template needs it
            text = _render(text, repo=repo.repo_name)
        out.append((dest, text))
    return out


def _dependabot(repo: _Repo, wanted: frozenset[str], notes: list[str]) -> list[tuple[str, str]]:
    # a ci.yml created in this same run uses actions, so Dependabot must watch them from day one
    actions = repo.has_workflows or ("ci" in wanted and bool(repo.languages))
    ecosystems = repo.ecosystems(actions=actions)
    if not ecosystems:
        notes.append("dependabot: no package manifest or workflow found, skipped")
        return []
    entry = _template("github/dependabot-entry.yml")
    block = "\n".join(_render(entry, ecosystem=e).rstrip("\n") for e in ecosystems)
    return [
        (
            ".github/dependabot.yml",
            _render(_template("github/dependabot.yml"), ecosystems_block=block),
        )
    ]


def _codeowners(repo: _Repo, notes: list[str]) -> list[tuple[str, str]]:
    primary = ".github/CODEOWNERS"
    if repo.origin is None:
        if not any(repo.has(p) for p in (primary, *_ALTERNATES[primary])):
            notes.append(
                "codeowners: skipped, no github.com 'origin' remote to take the owner from "
                "(add the remote, or write .github/CODEOWNERS by hand)"
            )
        return []
    owner, name = repo.origin
    return [(primary, _render(_template("github/CODEOWNERS"), owner=owner, repo=name))]


def _ci(repo: _Repo, commands: list[list[str]], notes: list[str]) -> list[tuple[str, str]]:
    langs = repo.languages
    if not langs:
        notes.append("ci: no Python, Node, Go or Rust project found, workflow skipped")
        return []
    blocks: list[str] = []
    for lang in langs:
        if lang in _SETUP_TEMPLATES:
            blocks.append(_template(_SETUP_TEMPLATES[lang]).rstrip("\n"))
        install = _install_command(repo, lang, commands)
        if install:
            step = _render(
                _template("github/workflows/step-install.yml"),
                step_name=f"Install {lang.capitalize()} dependencies",
                install_command=_yaml_scalar(install),
            )
            blocks.append(step.rstrip("\n"))
    run_commands = commands or [c for lang in langs for c in _default_commands(repo, lang, notes)]
    run_tpl = _template("github/workflows/step-run.yml")
    for argv in run_commands:
        blocks.append(_render(run_tpl, run_command=_yaml_scalar(shlex.join(argv))).rstrip("\n"))
    text = _render(
        _template("github/workflows/ci.yml"),
        default_branch=repo.default_branch,
        steps_block="\n".join(blocks),
    )
    return [(".github/workflows/ci.yml", text)]


# ---- public API -----------------------------------------------------------------------------------------------------


def parse_groups(only: str | Iterable[str] | None) -> list[str]:
    """Group names in canonical order. None, empty or 'all' selects every group; an unknown name raises ValueError."""
    if only is None:
        return list(GROUPS)
    items = [only] if isinstance(only, str) else [str(i) for i in only]
    names = [n.lower() for item in items for n in re.split(r"[,\s]+", item) if n]
    if not names or "all" in names:
        return list(GROUPS)
    unknown = sorted(set(names) - set(GROUPS))
    if unknown:
        raise ValueError(
            f"unknown group(s): {', '.join(unknown)}; valid groups: {', '.join(GROUPS)}"
        )
    return [g for g in GROUPS if g in names]


def _action(root: Path, group: str, path: str, content: str) -> Action:
    found = next((p for p in (path, *_ALTERNATES.get(path, ())) if os.path.lexists(root / p)), None)
    return Action(found or path, content, found is not None, group)


def plan_with_notes(
    root: Path,
    only: str | Iterable[str] | None = None,
    commands: Sequence[Sequence[str]] | None = None,
) -> tuple[list[Action], list[str]]:
    """The actions plus human-readable notes about anything skipped or worth a look. Reads the repo, never writes."""
    root = Path(root)
    groups = parse_groups(only)
    wanted = frozenset(groups)
    repo = _Repo(root)
    notes: list[str] = []
    cleaned, dropped = _clean_commands(commands)
    if dropped and "ci" in wanted:
        notes.append(
            f"ci: ignored {dropped} verify command(s) that cannot be written into a workflow safely"
        )
    actions: list[Action] = []
    for group in groups:
        try:
            if group == "dependabot":
                pairs = _dependabot(repo, wanted, notes)
            elif group == "codeowners":
                pairs = _codeowners(repo, notes)
            elif group == "ci":
                pairs = _ci(repo, cleaned, notes)
            else:
                pairs = _static(group, repo)
        except OSError as exc:
            notes.append(f"{group}: skipped, a template could not be read ({exc})")
            continue
        actions.extend(_action(root, group, path, content) for path, content in pairs)
    return actions, notes


def plan(
    root: Path,
    only: str | Iterable[str] | None = None,
    commands: Sequence[Sequence[str]] | None = None,
) -> list[Action]:
    """What init-repo would create, in a fixed order (group order, then template order).

    `only` limits the groups (a list, or a comma-separated string); `commands` are argv lists (for example the fast
    checks from the verify recipe) that the ci group runs instead of the per-language defaults.
    """
    return plan_with_notes(root, only, commands)[0]


def _relative_parts(path: str) -> tuple[str, ...] | None:
    norm = path.replace("\\", "/")
    if not norm or norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or "\x00" in norm:
        return None
    parts = tuple(p for p in norm.split("/") if p not in ("", "."))
    if not parts or ".." in parts or parts[0].lower() == ".git":
        return None
    return parts


def _inside(root: Path, target: Path) -> bool:
    """True if `target` (which may not exist yet) resolves under `root`, following symlinks in what does exist."""
    probe = target
    while not os.path.lexists(probe):
        if probe.parent == probe:
            return False
        probe = probe.parent
    base = os.path.normcase(os.path.realpath(root))
    real = os.path.normcase(os.path.realpath(probe))
    try:
        return os.path.commonpath([base, real]) == base
    except ValueError:  # different drives
        return False


def _write_new(dest: Path, content: str) -> bool:
    """Atomically create `dest` with LF endings; False if something appeared there in the meantime."""
    import tempfile

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".hr-init-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content.replace("\r\n", "\n"))
        try:
            os.chmod(tmp, 0o644)  # mkstemp makes it private (0600); a checked-in file should not be
        except OSError:
            pass
        try:
            os.link(
                tmp, dest
            )  # atomic, and refuses to replace a file that appeared since the check
            return True
        except FileExistsError:
            return False
        except OSError:
            pass  # no hard links on this volume (some network and FAT shares): check, then rename
        if os.path.lexists(dest):
            return False
        os.replace(tmp, dest)
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def apply_detailed(root: Path, actions: Iterable[Action]) -> ApplyResult:
    """Write every action whose file is absent. Never overwrites, never touches git, never raises for a bad path."""
    root = Path(root)
    result = ApplyResult()
    root_ok = root.is_dir()
    for action in actions:
        parts = _relative_parts(action.path)
        if parts is None or not root_ok:
            reason = (
                "not a relative path inside the repository"
                if root_ok
                else "repository root is not a directory"
            )
            result.errors.append((action.path, reason))
            continue
        rel = "/".join(parts)
        dest = root.joinpath(*parts)
        if action.exists or os.path.lexists(dest):
            result.skipped.append(rel)
            continue
        if not _inside(root, dest):
            result.errors.append((rel, "resolves outside the repository"))
            continue
        try:
            created = _write_new(dest, action.content)
        except OSError as exc:
            result.errors.append((rel, str(exc)))
            continue
        (result.written if created else result.skipped).append(rel)
    return result


def apply(root: Path, actions: Iterable[Action]) -> list[str]:
    """Relative paths of the files written. Files that already exist are left untouched; use apply_detailed() to also
    see what was skipped and which writes failed."""
    return apply_detailed(root, actions).written


def format_plan(actions: Iterable[Action], notes: Iterable[str] = ()) -> str:
    """Dry-run listing: what would be created, what is kept, then the notes."""
    lines = [
        f"{'exists (kept)' if a.exists else 'would create':<15}{a.path}  [{a.group}]"
        for a in actions
    ]
    lines += [f"note: {n}" for n in notes]
    return "\n".join(lines)

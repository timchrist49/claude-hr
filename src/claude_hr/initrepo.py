"""Scaffold the repository files an agent-driven workflow leans on: issue forms, a PR template, plan and decision
folders, editor settings, Dependabot, CODEOWNERS, REVIEW.md and a hardened CI skeleton.

Why it is safe to run anywhere: plan() only reads the repository, and apply() only creates files that are absent, so
a repo's own choices always win and a second run changes nothing. Nothing here touches git state or the network. The
repository is untrusted input: git is found through PATH only (never the current directory), and package.json,
pyproject.toml and setup.cfg are read only when they are regular files of at most 1 MB.

The command built on this module (`claude-hr-ctl init-repo`) works on the top level of a git work tree only: in a
subdirectory it leaves out the files that belong to the top level (ROOT_ONLY_GROUPS), and outside a work tree (a plain
folder, a bare repository, a `.git` directory) it refuses; see locate().

Templates live in data/repo-template/ under non-dot names (wheels and some tools treat dot-directories specially);
_FILES maps each template to the path it is written to.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, procutil

GROUPS = ("github", "docs", "editor", "dependabot", "codeowners", "review", "ci")
# Files that belong to the top level of the repository. GitHub reads .github/ (issue forms, PR template, CODEOWNERS,
# Dependabot, workflows) and reviewers read REVIEW.md only there, so a copy in a subdirectory is inert; a nested
# .editorconfig (`root = true`) or .gitattributes is worse, it silently overrides the repository's own rules for that
# whole subtree. In a subdirectory these groups are left out, and outside a work tree nothing is scaffolded at all.
ROOT_ONLY_GROUPS = ("github", "dependabot", "codeowners", "editor", "review", "ci")

# group ->(destination in the repo, template under data/repo-template/); dependabot, codeowners and ci are built below
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
# Tools the discovered commands may start with that the runner does not have: a setup action installs them
_TOOL_SETUP_TEMPLATES = {
    "uv": "github/workflows/steps-uv.yml",
    "bun": "github/workflows/steps-bun.yml",
}
# Commands the skeleton cannot make work on a fresh runner; they are left out and a note says so
_NOT_PROVISIONED = frozenset({"just"})
_PIP_TOOLS = frozenset({"pytest", "ruff", "mypy", "pyright", "black", "flake8", "isort"})
_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9._-]*requirements[A-Za-z0-9._-]*\.txt$")
_MAX_COMMANDS = 12
_COREPACK_ENABLE = "npm install -g corepack && corepack enable"
_RENOVATE_FILES = (
    "renovate.json",
    "renovate.json5",
    ".renovaterc",
    ".renovaterc.json",
    ".github/renovate.json",
    ".github/renovate.json5",
)
_JOBS_KEY = re.compile(r"^jobs[ \t]*:", re.MULTILINE)
# `on: push`, `on: [push, pull_request]`, `on: {push: ...}`, `on:\n  push:`, `on:\n  - pull_request`
_PUSH_TRIGGER = re.compile(
    r"""^["']?on["']?[ \t]*:.*\b(?:push|pull_request)\b"""
    r"|^[ \t]+(?:push|pull_request)[ \t]*:"
    r"|^[ \t]*-[ \t]*(?:push|pull_request)[ \t]*$",
    re.MULTILINE,
)
_TEST_COMMAND = re.compile(
    r"\b(?:pytest|unittest|tox|nox|jest|vitest|mocha|rspec|phpunit|ctest"
    r"|go[ \t]+test|cargo[ \t]+(?:test|nextest)|dotnet[ \t]+test|make[ \t]+(?:test|check)"
    r"|(?:npm|pnpm|yarn|bun)[ \t]+(?:run[ \t]+)?test)\b"
)
# package.json / pyproject.toml / setup.cfg come from the repository being scaffolded, so they are read like untrusted input
_MAX_MANIFEST_BYTES = 1_000_000
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


def _is_test_file(name: str) -> bool:
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _has_test_files(directory: Path) -> bool:
    """A file pytest would collect (test_*.py, *_test.py) in `directory` or in a subdirectory up to three levels down.
    Bounded (depth and entries), because the directory comes from the repository being scaffolded."""
    budget = 2000
    pending = [(str(directory), 0)]
    while pending:
        current, depth = pending.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    budget -= 1
                    if budget < 0:
                        return False
                    if _is_test_file(entry.name) and entry.is_file():
                        return True
                    if (
                        depth < 3
                        and not entry.name.startswith((".", "__"))
                        and entry.is_dir(follow_symlinks=False)
                    ):
                        pending.append((entry.path, depth + 1))
        except OSError:
            continue
    return False


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
        self.notes: list[
            str
        ] = []  # problems with the repository's own files; plan_with_notes shows them

    def has(self, rel: str) -> bool:
        return os.path.lexists(self.root / rel)

    def _note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def _read_manifest(self, name: str) -> bytes | None:
        """The bytes of a top-level repository file, or None. Only a regular file of at most _MAX_MANIFEST_BYTES is read:
        a committed symlink to /dev/zero (or a FIFO, or a multi-gigabyte file) must not hang or exhaust memory."""
        path = self.root / name
        try:
            if not stat.S_ISREG(os.stat(path).st_mode):
                self._note(f"ci: {name} is not a regular file, so it was not read")
                return None
            with open(path, "rb") as fh:
                if not stat.S_ISREG(
                    os.fstat(fh.fileno()).st_mode
                ):  # it changed between the check and the open
                    self._note(f"ci: {name} is not a regular file, so it was not read")
                    return None
                data = fh.read(_MAX_MANIFEST_BYTES + 1)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            self._note(
                f"ci: {name} could not be read ({exc.__class__.__name__}), so it was ignored"
            )
            return None
        if len(data) > _MAX_MANIFEST_BYTES:
            self._note(
                f"ci: {name} is larger than {_MAX_MANIFEST_BYTES // 1000} KB, so it was ignored"
            )
            return None
        return data

    def _parse(self, name: str, loader) -> dict:
        """A hostile or broken file degrades to 'nothing detected' plus a note; it never raises."""
        data = self._read_manifest(name)
        if data is None:
            return {}
        try:
            value = loader(data)
        except RecursionError:  # deeply nested arrays: json and tomllib recurse
            self._note(f"ci: {name} is nested too deeply to read, so it was ignored")
            return {}
        except ValueError:  # includes UnicodeDecodeError
            self._note(f"ci: {name} is not valid, so it was ignored")
            return {}
        return value if isinstance(value, dict) else {}

    @functools.cached_property
    def requirements(self) -> list[str]:
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return []
        return [n for n in names if _REQUIREMENT_NAME.match(n) and (self.root / n).is_file()][:4]

    @functools.cached_property
    def pyproject(self) -> dict:
        def load(data: bytes):
            import tomllib  # only when there is a file to parse

            return tomllib.loads(data.decode("utf-8"))

        return self._parse("pyproject.toml", load)

    @functools.cached_property
    def package_json(self) -> dict:
        return self._parse("package.json", json.loads)

    @functools.cached_property
    def setup_cfg(self) -> str:
        data = self._read_manifest("setup.cfg")
        return data.decode("utf-8", "replace") if data else ""

    @functools.cached_property
    def python_tool(self) -> dict:
        tool = self.pyproject.get("tool")
        return tool if isinstance(tool, dict) else {}

    @functools.cached_property
    def pytest_ready(self) -> bool:
        """pytest has something to run: at least one test file (test_*.py or *_test.py, pytest's own collection pattern)
        in a tests directory, or its own configuration. `pytest` exits 5, a failure, when it collects nothing, and an
        `__init__.py`, a `conftest.py` or a helper module collects nothing."""
        return (
            "pytest" in self.python_tool
            or self.has("pytest.ini")
            or "[tool:pytest]" in self.setup_cfg
            or any(_has_test_files(self.root / d) for d in ("tests", "test"))
        )

    @functools.cached_property
    def ruff_configured(self) -> bool:
        return "ruff" in self.python_tool or self.has("ruff.toml") or self.has(".ruff.toml")

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
        """The package manager the verify recipe discovery picks (lock files, bun, then the packageManager field): the
        commands it proposes and the install step this workflow writes must name the same tool."""
        from . import verify

        return verify._node_pm(self.root, self.package_json)

    @functools.cached_property
    def has_issue_templates(self) -> bool:
        """The repo already has issue templates GitHub would show: ISSUE_TEMPLATE/ (any .md/.yml/.yaml, config.yml included) or a
        single ISSUE_TEMPLATE.md, at the root, in docs/ or in .github/. Names are matched case-insensitively."""
        for base in (self.root, self.root / ".github", self.root / "docs"):
            try:
                with os.scandir(base) as it:
                    for entry in it:
                        low = entry.name.lower()
                        if low in {
                            "issue_template.md",
                            "issue_template.yml",
                            "issue_template.yaml",
                        }:
                            return True
                        if low == "issue_template" and self._has_template_files(entry.path):
                            return True
            except OSError:
                continue
        return False

    @staticmethod
    def _has_template_files(directory: str) -> bool:
        try:
            with os.scandir(directory) as sub:
                return any(s.name.lower().endswith((".md", ".yml", ".yaml")) for s in sub)
        except OSError:
            return False

    @functools.cached_property
    def has_workflows(self) -> bool:
        try:
            with os.scandir(self.root / ".github" / "workflows") as it:
                return any(e.name.lower().endswith((".yml", ".yaml")) for e in it)
        except OSError:
            return False

    @functools.cached_property
    def test_workflow(self) -> str | None:
        """File name of a workflow the repo already has (other than ci.yml itself) that runs on push or pull_request and
        starts a test command: a second workflow running the same suite would double every CI run, and this skeleton
        knows nothing of the repo's services, secrets or matrix. Deliberately simple and conservative: only a plain
        `on: push|pull_request` header followed by an obvious test command counts."""
        directory = self.root / ".github" / "workflows"
        try:
            with os.scandir(directory) as it:
                names = sorted(e.name for e in it if e.name.lower().endswith((".yml", ".yaml")))
        except OSError:
            return None
        for name in names[:50]:
            if name.lower() in {"ci.yml", "ci.yaml"}:
                continue  # the file this skeleton would write: _action reports it as already there
            data = self._read_manifest(f".github/workflows/{name}")
            if data is None:
                continue
            text = "\n".join(
                line
                for line in data.decode("utf-8", "replace").splitlines()
                if not line.lstrip().startswith("#")
            )
            jobs = _JOBS_KEY.search(text)
            if jobs and _PUSH_TRIGGER.search(text[: jobs.start()]) and _TEST_COMMAND.search(text):
                return name
        return None

    @functools.cached_property
    def uses_renovate(self) -> bool:
        """The repo already has a dependency bot configured (Renovate): a second one would open duplicate pull requests."""
        return any(self.has(p) for p in _RENOVATE_FILES) or "renovate" in self.package_json

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
    """What CI runs for a language when the verify recipe proposed nothing. Only what can pass on a fresh repository: a
    placeholder test script, a watcher, or pytest with nothing to collect would turn the first push red (or hang it)."""
    if lang == "python":
        out = []
        if repo.pytest_ready:
            out.append(["python", "-m", "pytest", "-q"])
        if repo.ruff_configured:
            out.append(["ruff", "check", "."])
        if not out:
            notes.append(
                "ci: no tests directory, pytest configuration or ruff configuration found, so the Python "
                "part of the workflow only installs dependencies (add test and lint steps when they exist)"
            )
        return out
    if lang == "node":
        from . import verify

        pm, scripts = repo.node_pm, repo.node_scripts
        out = []
        for script in ("test", "lint"):
            body = scripts.get(script)
            if not isinstance(body, str) or not body.strip():
                continue
            if "no test specified" in body or (script == "test" and verify._WATCH_RE.search(body)):
                notes.append(
                    f"ci: package.json script '{script}' is a placeholder or a watcher, so it is not run"
                )
                continue
            # `bun test` is bun's own runner, not the package script, so bun always goes through `run`
            out.append([pm, "test"] if script == "test" and pm != "bun" else [pm, "run", script])
        if not out:
            notes.append(
                "ci: package.json has no test or lint script, so the workflow only installs dependencies"
            )
        return out
    if lang == "go":
        return [["go", "test", "./..."]]
    return [["cargo", "test"]]


def _toolchain(repo: _Repo, lang: str, commands: list[list[str]]) -> tuple[list[str], str | None]:
    """(setup actions for tools the runner lacks, install command) for one language; `commands` are what the workflow
    runs, so a tool is provisioned exactly when something starts with it. Nothing here installs a tool the commands do not use."""
    tools = {argv[0] for argv in commands}
    if lang == "node":
        pm = repo.node_pm
        # corepack provides pnpm and yarn at the version the packageManager field names. It is NOT guaranteed to ship
        # with Node: Node 25 and later no longer bundle it, and `lts/*` (steps-node.yml) moves to such a release, so it
        # is installed first rather than assumed (the npm that comes with every Node does the install).
        if pm == "pnpm":
            frozen = "--frozen-lockfile" if repo.has("pnpm-lock.yaml") else "--no-frozen-lockfile"
            return [], f"{_COREPACK_ENABLE} && pnpm install {frozen}"
        if pm == "yarn":
            return [], f"{_COREPACK_ENABLE} && yarn install"
        if pm == "bun":
            locked = repo.has("bun.lock") or repo.has("bun.lockb")
            return ["bun"], "bun install --frozen-lockfile" if locked else "bun install"
        locked = repo.has("package-lock.json") or repo.has("npm-shrinkwrap.json")
        return [], "npm ci" if locked else "npm install"
    if lang != "python":
        return [], None
    if (
        "uv" in tools
    ):  # uv.lock: uv sync builds the environment (project, dev group) from the lock file
        return ["uv"], "uv sync --locked"
    for tool, flags in (("poetry", " --no-interaction"), ("pdm", "")):
        if tool in tools:
            return [], f"python -m pip install {tool} && {tool} install{flags}"
    args: list[str] = []
    if repo.python_package:
        args += ["-e", ".[dev]" if repo.has_dev_extra else "."]
    for req in repo.requirements:
        args += ["-r", req]
    args += _pip_tools(commands)
    return [], shlex.join(["python", "-m", "pip", "install", *args]) if args else None


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
    if repo.uses_renovate:
        notes.append(
            "dependabot: a Renovate configuration is present, so dependabot.yml was skipped "
            "(two dependency bots open duplicate pull requests)"
        )
        return []
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


def _github(repo: _Repo, notes: list[str]) -> list[tuple[str, str]]:
    pairs = _static("github", repo)
    if (
        repo.has_issue_templates
    ):  # the repo's own issue templates win: a second set would show up twice in the chooser
        pairs = [(d, t) for d, t in pairs if not d.startswith(".github/ISSUE_TEMPLATE/")]
        notes.append("github: issue templates already present, issue forms skipped")
    return pairs


def _codeowners(repo: _Repo, notes: list[str]) -> list[tuple[str, str]]:
    primary = ".github/CODEOWNERS"
    exists = any(repo.has(p) for p in (primary, *_ALTERNATES[primary]))
    if repo.origin is None:
        if not exists:
            notes.append(
                "codeowners: skipped, no github.com 'origin' remote to take the owner from "
                "(add the remote, or write .github/CODEOWNERS by hand)"
            )
        return []
    owner, name = repo.origin
    if not exists:
        # offline there is no telling a user account from an organization, and GitHub ignores a bare organization name
        notes.append(
            f"codeowners: the owner @{owner} was taken from the origin URL; if {owner} is an organization, edit "
            f".github/CODEOWNERS to @{owner}/team-name or a person's @username (GitHub ignores a bare organization name)"
        )
    return [(primary, _render(_template("github/CODEOWNERS"), owner=owner, repo=name))]


def _ci(
    repo: _Repo,
    commands: list[list[str]],
    notes: list[str],
    *,
    forced: bool = False,
    discovered: bool = False,
) -> list[tuple[str, str]]:
    langs = repo.languages
    if not langs:
        notes.append("ci: no Python, Node, Go or Rust project found, workflow skipped")
        return []
    existing = repo.test_workflow
    own_ci = ".github/workflows/ci.yml"  # when it is there already, _action reports "exists (kept)" instead
    if existing and not forced and not any(repo.has(p) for p in (own_ci, *_ALTERNATES[own_ci])):
        notes.append(
            f"ci: .github/workflows/{existing} already runs tests on push or pull_request, so no second "
            "workflow was written (it would run the suite twice); ask for the skeleton with `--only ci`"
        )
        return []
    chosen = commands or [c for lang in langs for c in _default_commands(repo, lang, notes)]
    if discovered and not repo.pytest_ready:
        # automatic discovery (verify.discover) counts an `__init__.py` or a conftest.py as a test suite
        kept = [c for c in chosen if "pytest" not in c[:5]]
        if len(kept) < len(chosen):
            notes.append(
                "ci: left out pytest: no test file (test_*.py or *_test.py) under tests/ or test/ and no "
                "pytest configuration, so it would exit 5 (nothing collected) and turn the first push red"
            )
        chosen = kept
    run_commands = [c for c in chosen if c[0] not in _NOT_PROVISIONED]
    if len(run_commands) < len(chosen):
        left_out = ", ".join(f"`{shlex.join(c)}`" for c in chosen if c[0] in _NOT_PROVISIONED)
        notes.append(
            f"ci: left out {left_out}: the workflow cannot install {'/'.join(sorted(_NOT_PROVISIONED))} on the runner; "
            "add a setup step for it and the command to the workflow to run it in CI"
        )
    blocks: list[str] = []
    for lang in langs:
        if lang in _SETUP_TEMPLATES:
            blocks.append(_template(_SETUP_TEMPLATES[lang]).rstrip("\n"))
        setup, install = _toolchain(repo, lang, run_commands)
        blocks.extend(_template(_TOOL_SETUP_TEMPLATES[tool]).rstrip("\n") for tool in setup)
        if install:
            step = _render(
                _template("github/workflows/step-install.yml"),
                step_name=f"Install {lang.capitalize()} dependencies",
                install_command=_yaml_scalar(install),
            )
            blocks.append(step.rstrip("\n"))
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


def _same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _in_git_dir(root: Path) -> bool:
    """`root` is a repository's own git directory (`.git` itself or anything below it) or a bare repository: nothing
    created there is part of a work tree."""
    real = Path(os.path.realpath(root))
    if any(part.lower() == ".git" for part in real.parts):
        return True
    return any(
        os.path.isfile(p / "HEAD") and os.path.isdir(p / "objects") and os.path.isdir(p / "refs")
        for p in (real, *real.parents)
    )


def _dot_git_top(root: Path) -> Path | None:
    """The nearest directory at or above `root` that has a `.git` entry (a directory, or the file of a linked worktree
    or submodule): the answer when git itself cannot be run."""
    real = Path(os.path.realpath(root))
    return next((p for p in (real, *real.parents) if os.path.lexists(p / ".git")), None)


def locate(root: Path) -> tuple[str, Path | None]:
    """How `root` relates to a git work tree, as (kind, top level):

    ("top", None)       `root` is the top level of a work tree
    ("subdir", top)     `root` is inside the work tree whose top level is `top`
    ("gitdir", None)    `root` is a git directory (a bare repository, `.git` or something below it), not a work tree
    ("none", None)      no git repository contains `root`

    git answers when it can; when it cannot be run (not installed, a timeout, `safe.directory`), the `.git` marker is
    looked up on disk, so the answer does not depend on git being usable.
    """
    root = Path(root)
    r = _git(root, "rev-parse", "--show-toplevel")
    top = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    if top is None:
        if _in_git_dir(root):
            return "gitdir", None
        top = _dot_git_top(root)
        if top is None:
            return "none", None
    return ("top", None) if _same_path(top, root) else ("subdir", top)


def git_top_level(root: Path) -> Path | None:
    """The top level of the git repository that contains `root` when `root` is a SUBDIRECTORY of it; None when `root` is the
    top level itself or is not in a work tree."""
    kind, top = locate(root)
    return top if kind == "subdir" else None


def _action(root: Path, group: str, path: str, content: str) -> Action:
    found = next((p for p in (path, *_ALTERNATES.get(path, ())) if os.path.lexists(root / p)), None)
    return Action(found or path, content, found is not None, group)


def plan_with_notes(
    root: Path,
    only: str | Iterable[str] | None = None,
    commands: Sequence[Sequence[str]] | None = None,
    *,
    discovered: bool = False,
) -> tuple[list[Action], list[str]]:
    """The actions plus human-readable notes about anything skipped or worth a look. Reads the repo, never writes.

    `discovered` says `commands` came from automatic discovery (verify.discover) rather than from a person: a pytest
    command it proposes for a tests directory that holds no test file is then left out, like the per-language default.
    """
    root = Path(root)
    groups = parse_groups(only)
    wanted = frozenset(groups)
    # naming a subset of the groups is asking for them: the "the repo already has CI" heuristic then stands aside
    forced = wanted != frozenset(GROUPS)
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
                pairs = _ci(repo, cleaned, notes, forced=forced, discovered=discovered)
            elif group == "github":
                pairs = _github(repo, notes)
            else:
                pairs = _static(group, repo)
        except OSError as exc:
            notes.append(f"{group}: skipped, a template could not be read ({exc})")
            continue
        actions.extend(_action(root, group, path, content) for path, content in pairs)
    notes.extend(
        n for n in repo.notes if n not in notes
    )  # hostile or broken package.json / pyproject.toml
    return actions, notes


def plan(
    root: Path,
    only: str | Iterable[str] | None = None,
    commands: Sequence[Sequence[str]] | None = None,
    *,
    discovered: bool = False,
) -> list[Action]:
    """What init-repo would create, in a fixed order (group order, then template order).

    `only` limits the groups (a list, or a comma-separated string); `commands` are argv lists (for example the fast
    checks from the verify recipe) that the ci group runs instead of the per-language defaults (`discovered`: see
    plan_with_notes).
    """
    return plan_with_notes(root, only, commands, discovered=discovered)[0]


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

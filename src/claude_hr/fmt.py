"""Format the files an agent just changed, with the formatter the repo itself configures.

A formatter is chosen ONLY when the repo explicitly configures it (never imposed), and it runs through
stdin/stdout so a file is rewritten only when its formatted text really differs. Meant to be called from a hook:
the public entry points never raise, nothing here touches the network, and nothing downloads a tool
(no `uv run`, `npx`, `pipx`).

Import cost matters (the stop hook imports this on every turn that changed files): subprocess, json and tomllib are
imported inside the functions that need them.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import re
import stat
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, procutil

_PER_FILE_MAX_S = 15.0  # one hung formatter must not eat the whole budget
_GIT_MAX_S = 20.0
_HEAD_BYTES = 8192  # enough to spot binary data; generated-file banners are within the first KB
_BOM = b"\xef\xbb\xbf"

_PY_EXTS = (".py", ".pyi")
_JS_EXTS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
# Markdown / YAML / MDX are only ever reached through a repo-configured prettier (detection demands its config).
_PRETTIER_EXTS = (
    *_JS_EXTS,
    ".css",
    ".scss",
    ".less",
    ".json",
    ".jsonc",
    ".md",
    ".mdx",
    ".yml",
    ".yaml",
)
_BIOME_EXTS = (*_JS_EXTS, ".json", ".jsonc", ".css")
_ALL_EXTS = frozenset((*_PY_EXTS, *_PRETTIER_EXTS, *_BIOME_EXTS, ".go", ".rs"))

_RC_SUFFIXES = ("json", "yml", "yaml", "json5", "toml", "js", "cjs", "mjs", "ts", "cts", "mts")
_CONFIG_SUFFIXES = ("js", "cjs", "mjs", "ts", "cts", "mts")
_PRETTIER_CONFIGS = (
    ".prettierrc",
    *(f".prettierrc.{s}" for s in _RC_SUFFIXES),
    *(f"prettier.config.{s}" for s in _CONFIG_SUFFIXES),
)

_SKIP_DIRS = frozenset(
    {
        "node_modules", "vendor", "dist", "build", ".venv", "venv", "__pycache__", ".git", ".tox",
        ".nox", "site-packages", ".next", ".nuxt", ".svelte-kit", ".turbo", "coverage",
        ".mypy_cache", ".ruff_cache", ".pytest_cache",
    }
)  # fmt: skip
_LOCKFILES = frozenset(
    {
        "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock",
        "bun.lockb", "cargo.lock", "poetry.lock", "uv.lock", "pdm.lock", "pipfile.lock",
        "composer.lock", "gemfile.lock", "go.sum", "go.work.sum", "flake.lock", "mix.lock",
        "pubspec.lock", "packages.lock.json", "gradle.lockfile",
    }
)  # fmt: skip
_GENERATED = re.compile(
    rb"@generated\b|DO NOT EDIT|Code generated|auto-?generated|automatically generated", re.I
)
_RUSTFMT_BANNER = re.compile(r"\Astdin:\r?\n(?:\r?\n)?")
# cmd.exe metacharacters (BatBadBut): never pass them to a .cmd/.bat shim
_CMD_UNSAFE = re.compile(r'[%^&|<>!"\r\n]')
# A formatter may execute repo-controlled configuration (a trusted repo, or format.repo_tools = always), so it gets
# the verify runner's treatment, widened: a deny-list (toolchains need too many harmless variables for an allow-list).
_SECRET_ENV = re.compile(
    r"TOKEN|SECRET|PASS|PWD|KEY|CRED|AUTH|COOKIE|DSN|CONNECTION|PRIVATE|CERT|JWT|WEBHOOK|(?:^|_)PAT(?:_|$)",
    re.I,
)
_SECRET_ENV_PREFIXES = (
    "ANTHROPIC_", "CLAUDE_CODE_", "AWS_", "AZURE_", "GOOGLE_", "GCP_", "GITHUB_", "GH_", "NPM_",
)  # fmt: skip
_SECRET_ENV_NAMES = frozenset({"DATABASE_URL", "SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS"})
# `scheme://user:password@host`: DATABASE_URL, REDIS_URL, HTTPS_PROXY, a registry URL ... whatever the variable is called
_URL_CREDENTIALS = re.compile(r"://[^/\s@:]*:[^/\s@]+@")
# never dropped, even if a name above would match: git needs the identity variables, toolchains need their homes
_KEEP_ENV = frozenset(
    {
        "PATH", "PATHEXT", "HOME", "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
        "TEMP", "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "LANG", "LANGUAGE",
        "CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOROOT", "GOCACHE", "GOMODCACHE", "VIRTUAL_ENV",
        "NODE_PATH", "PYTHONPATH", "PYTHONHOME",
    }
)  # fmt: skip
_KEEP_ENV_PREFIXES = ("GIT_AUTHOR_", "GIT_COMMITTER_")
_GIT_REDIRECT_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
)  # fmt: skip
# injected git configuration travels as a family: GIT_CONFIG_COUNT without its KEY_n makes git die, so all go together
_GIT_REDIRECT_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
# no real pyproject.toml / package.json / Cargo.toml is anywhere near this
_MAX_CONFIG_BYTES = 1_000_000
_REPLACE_TRIES = (
    4  # os.replace attempts on Windows (50, 100, 200 ms apart) before a locked file is reported
)

DRIFT_REASON = "pre-existing formatting drift"
BUDGET_REASON = "time budget"

_now = time.monotonic  # indirection so tests can drive the clock


@dataclass(frozen=True)
class Formatter:
    name: str  # ruff | black | prettier | biome | gofmt | rustfmt
    exe: str  # absolute path (PATH, or the project's own .venv / node_modules/.bin)

    def stdin_argv(self, path: Path) -> list[str]:
        """argv that reads the file's text on stdin and writes the formatted text to stdout."""
        p = str(path)
        if self.name == "ruff":
            return [self.exe, "format", "--stdin-filename", p, "-"]
        if self.name == "black":
            return [self.exe, "-q", "--stdin-filename", p, "-"]
        if self.name == "prettier":
            return [self.exe, "--stdin-filepath", p]
        if self.name == "biome":
            return [self.exe, "format", "--stdin-file-path=" + p]
        if self.name == "gofmt":
            return [self.exe]
        if self.name == "rustfmt":
            return [self.exe, "--emit", "stdout", "--edition", _rust_edition(path)]
        raise ValueError(f"unknown formatter {self.name!r}")


@dataclass
class FormatReport:
    """Paths are root-relative and posix. `formatted` lists files rewritten (with check=True: files that would
    change); `skipped` is (path, reason); `errors` is (path, message) and the file is left untouched;
    `formatters` names the formatters tried."""

    formatted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    formatters: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def summary(self) -> str:
        parts = [f"formatted {len(self.formatted)} file(s)"]
        if self.formatters:
            parts[0] += f" with {', '.join(self.formatters)}"
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        return "; ".join(parts)

    def as_dict(self) -> dict:
        return {
            "formatted": list(self.formatted),
            "skipped": [list(s) for s in self.skipped],
            "errors": [list(e) for e in self.errors],
            "formatters": list(self.formatters),
            "elapsed_s": self.elapsed_s,
        }


# ---- process helpers -------------------------------------------------------------------------------------------


@dataclass
class _Proc:
    rc: int
    out: bytes = b""
    err: bytes = b""
    timed_out: bool = False


def _env_is_dropped(name: str, value: str) -> bool:
    """True for a variable that looks like a credential, or that would redirect git to another repository / inject
    git configuration when we are called from inside a git hook. See _SECRET_ENV and _KEEP_ENV."""
    up = name.upper()
    if up in _GIT_REDIRECT_ENV or up.startswith(_GIT_REDIRECT_PREFIXES):
        return True
    if up in _KEEP_ENV or up.startswith(_KEEP_ENV_PREFIXES):
        return False
    return bool(
        up in _SECRET_ENV_NAMES
        or up.startswith(_SECRET_ENV_PREFIXES)
        or _SECRET_ENV.search(up)
        or ("@" in value and _URL_CREDENTIALS.search(value))
    )


def _clean_env() -> dict[str, str]:
    """Environment for git and formatters: no credentials (a formatter may execute repo-controlled config; the
    deny-list is the verify runner's, widened), and nothing that would redirect git to another repository or inject
    git configuration when we are called from inside a git hook."""
    env = {k: v for k, v in os.environ.items() if not _env_is_dropped(k, v)}
    if paths.IS_WINDOWS:
        # cmd.exe (which runs an npm .cmd shim) and CreateProcess look in the current directory, here the untrusted
        # repository, before PATH: a planted node.exe there would run instead of the real one
        env["NoDefaultCurrentDirectoryInExePath"] = "1"
    env.update(
        GIT_OPTIONAL_LOCKS="0",  # `git status` must not take the index lock away from the user's own git commands
        GIT_TERMINAL_PROMPT="0",
        NO_COLOR="1",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        RUSTUP_AUTO_INSTALL="0",  # a rustup shim must never download a toolchain mid-turn
    )
    return env


def _taskkill_exe() -> str:
    """taskkill.exe by absolute path under %SystemRoot%\\System32. The bare name would be searched in the current
    directory first (an untrusted repository) when NoDefaultCurrentDirectoryInExePath is not set; if System32 has
    none, the bare name still goes through procutil.run, which searches absolute PATH entries only."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or ""
    if not os.path.isabs(root):
        root = r"C:\Windows"
    exe = os.path.join(root, "System32", "taskkill.exe")
    return exe if os.path.isfile(exe) else "taskkill"


def _kill_tree(proc) -> None:
    """Kill the formatter and its children (a Windows .cmd shim leaves node running after its cmd.exe dies)."""
    try:
        if paths.IS_WINDOWS:
            procutil.run([_taskkill_exe(), "/PID", str(proc.pid), "/T", "/F"], timeout=10)
        else:
            os.killpg(proc.pid, 9)
    except (OSError, ValueError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _run(argv: list[str], *, cwd: Path, timeout: float, input_bytes: bytes | None = None) -> _Proc:
    """Run argv (no shell) with bytes in/out so newline conventions are never translated. Never raises."""
    import subprocess

    extra: dict = {}
    if paths.IS_WINDOWS:
        extra["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    else:
        extra["start_new_session"] = True  # own process group, so a timeout can kill the whole tree
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=_clean_env(),
            **extra,
        )
    except FileNotFoundError as exc:
        return _Proc(127, err=str(exc).encode())
    except (OSError, ValueError) as exc:
        return _Proc(126, err=str(exc).encode())
    try:
        out, err = proc.communicate(input_bytes, timeout=max(timeout, 0.01))
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            out = err = b""
        return _Proc(124, out, err, timed_out=True)
    except (OSError, ValueError) as exc:
        _kill_tree(proc)
        return _Proc(126, err=str(exc).encode())
    return _Proc(proc.returncode, out, err)


_GIT_EXE: dict[str, str | None] = {}


def _git_exe() -> str | None:
    """Absolute path of git (procutil.resolve_exe: absolute PATH entries only, never the repo's own directory)."""
    key = os.environ.get("PATH", "")
    if key not in _GIT_EXE:
        _GIT_EXE[key] = procutil.resolve_exe("git")
    return _GIT_EXE[key]


def _git(root: Path, *args: str, timeout: float) -> _Proc:
    exe = _git_exe()
    if exe is None:
        return _Proc(127, err=b"git not found")
    return _run([exe, *args], cwd=root, timeout=timeout)


def _tail(raw: bytes) -> str:
    lines = [ln.strip() for ln in raw.decode("utf-8", "replace").splitlines() if ln.strip()]
    return " | ".join(lines[:2])[:300]


def _cmd_shim_unsafe(argv: list[str]) -> bool:
    """A .cmd/.bat is run through cmd.exe, which would interpret metacharacters in a file name (`a&calc.ts`)."""
    return Path(argv[0]).suffix.lower() in {".cmd", ".bat"} and any(
        _CMD_UNSAFE.search(a) for a in argv
    )


# ---- locating tools --------------------------------------------------------------------------------------------

_WHICH_CACHE: dict[tuple[str, str, str], str] = {}


def _inside(p: str | Path, root: Path) -> str | None:
    """`p` relative to `root` (posix) when it lives inside it, else None. Purely lexical, like the refusals below."""
    try:
        return Path(os.path.abspath(p)).relative_to(os.path.abspath(root)).as_posix()
    except ValueError:
        return None


def _which(name: str, outside: Path | None = None) -> str | None:
    """First `name` on PATH. Relative PATH entries are ignored: the hook's cwd is the (untrusted) repo and a bare
    relative entry (or Windows' implicit current directory) would resolve a planted binary there.

    outside: skip hits that live inside this directory (an activated project .venv is on PATH, but it is the
    project's own copy, not a global one)."""
    path_env = os.pathsep.join(
        d for d in os.environ.get("PATH", "").split(os.pathsep) if d and os.path.isabs(d)
    )
    key = (name, path_env, os.path.abspath(outside) if outside is not None else "")
    hit = _WHICH_CACHE.get(key)
    if hit and os.path.isfile(hit):
        return hit
    for found in procutil.iter_which(name, path_env):
        found = os.path.abspath(found)
        if outside is not None and _inside(found, outside) is not None:
            continue
        _WHICH_CACHE[key] = found
        return found
    return None


def _find_exe(directory: Path, name: str, *, node: bool = False) -> str | None:
    if paths.IS_WINDOWS:
        # npm writes .cmd shims; the extension-less sh script next to them cannot be spawned
        names = [name + e for e in ((".cmd", ".exe", ".bat") if node else (".exe", ".cmd", ".bat"))]
    else:
        names = [name]
    for n in names:
        cand = directory / n
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return os.path.abspath(cand)
        except OSError:
            pass
    return None


def _dirs_up(start: Path, root: Path) -> list[Path]:
    """start and its parents up to and including root; empty when start is not inside root."""
    try:
        start.relative_to(root)
    except ValueError:
        return []
    out = [start]
    while out[-1] != root and out[-1].parent != out[-1]:
        out.append(out[-1].parent)
    return out


def _rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return str(p)


def _read_regular(p: Path | str, limit: int) -> bytes | None:
    """The bytes of a regular file of at most `limit` bytes, else None. Never raises.

    For files the repository controls (pyproject.toml, package.json, Cargo.toml ...): a symlink to /dev/zero would be
    read until memory runs out, and one to a FIFO or /dev/ptmx would block the hook for good. So the kind is checked
    before opening (stat never blocks), the opened descriptor is checked again (the file may have been swapped), the
    size is capped, and the read itself never takes more than limit + 1 bytes."""
    try:
        st = os.stat(p)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(p, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            chunks: list[bytes] = []
            total = 0
            while total <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return None
    return None if total > limit else b"".join(chunks)


def _read_text(p: Path) -> str | None:
    """Text of a repo-controlled configuration file (see _read_regular); None when unreadable, not a regular file, or
    bigger than _MAX_CONFIG_BYTES, which the callers treat as "not configured"."""
    data = _read_regular(p, _MAX_CONFIG_BYTES)
    if data is None:
        return None
    return data.decode("utf-8-sig", "replace").replace("\r\n", "\n").replace("\r", "\n")


def _toml_has_table(text: str, *names: str) -> bool:
    for name in names:
        parts = r"[ \t]*\.[ \t]*".join(re.escape(n) for n in name.split("."))
        if re.search(rf"^[ \t]*\[[ \t]*{parts}[ \t]*\][ \t\r]*(?:#.*)?$", text, re.M):
            return True
    return False


def _find_python_tool(
    name: str, dirs: list[Path], *, where: str = "any", root: Path | None = None
) -> str | None:
    """Where a python formatter is installed. where="any": the project's own .venv copy, else PATH (the pinned version
    wins over a global one); where="path": only a copy on PATH outside `root`, never the project's own."""
    if where == "path":
        return _which(name, outside=root)
    sub = "Scripts" if paths.IS_WINDOWS else "bin"
    for d in dirs:
        hit = _find_exe(d / ".venv" / sub, name)
        if hit:
            return hit
    return _which(name)


def _find_node_tool(name: str, dirs: list[Path]) -> str | None:
    for d in dirs:
        hit = _find_exe(d / "node_modules" / ".bin", name, node=True)
        if hit:
            return hit
    return None  # never npx: that downloads


def _detect_python(
    dirs: list[Path], root: Path, prefer_local: bool = True
) -> tuple[Formatter, str] | None:
    # An untrusted repository may not run its own copy (see _repo_code_refusal), so there a copy outside the repository
    # comes first and a refused in-repo one cannot shadow it. The in-repo copy stays the last resort: with nothing else
    # installed the run reports why nothing was formatted and how to allow it, instead of "no formatter configured".
    stages = ("any",) if prefer_local else ("path", "any")
    for d in dirs:
        found: list[tuple[str, Path]] = []
        for name in ("ruff.toml", ".ruff.toml"):
            if (d / name).is_file():
                found.append(("ruff", d / name))
        pyproject = d / "pyproject.toml"
        text = _read_text(pyproject)
        if text is not None:
            if _toml_has_table(text, "tool.ruff", "tool.ruff.format"):
                found.append(("ruff", pyproject))
            if _toml_has_table(text, "tool.black"):
                found.append(("black", pyproject))
        found.sort(key=lambda c: c[0] != "ruff")  # ruff wins over black when both are configured
        for where in stages:
            for tool, cfg in found:
                exe = _find_python_tool(tool, dirs, where=where, root=root)
                if exe:
                    return Formatter(tool, exe), _rel(cfg, root)
    return None


def _prettier_config(dirs: list[Path], root: Path) -> str | None:
    for d in dirs:
        for name in _PRETTIER_CONFIGS:
            if (d / name).is_file():
                return _rel(d / name, root)
        text = _read_text(d / "package.json")
        if text is not None and '"prettier"' in text:
            import json

            try:
                data = json.loads(text)
            except (ValueError, RecursionError):  # deeply nested JSON must not abort detection
                continue
            if isinstance(data, dict) and "prettier" in data:
                return _rel(d / "package.json", root)
    return None


def _biome_config(dirs: list[Path], root: Path) -> str | None:
    for d in dirs:
        for name in ("biome.json", "biome.jsonc"):
            if (d / name).is_file():
                return _rel(d / name, root)
    return None


def _walk_up_for(dirs: list[Path], root: Path, name: str) -> str | None:
    for d in dirs:
        if (d / name).is_file():
            return _rel(d / name, root)
    return None


def _detect(path: Path, root: Path, prefer_local: bool = True) -> tuple[Formatter, str] | None:
    """(formatter, config file that selected it) or None. See detect() for prefer_local."""
    ext = path.suffix.lower()
    dirs = _dirs_up(path.parent, root)
    if not dirs:
        return None
    if ext in _PY_EXTS:
        return _detect_python(dirs, root, prefer_local)
    if ext in _PRETTIER_EXTS or ext in _BIOME_EXTS:
        if ext in _PRETTIER_EXTS:
            cfg = _prettier_config(dirs, root)
            exe = _find_node_tool("prettier", dirs) if cfg else None
            if cfg and exe:
                return Formatter("prettier", exe), cfg
        if ext in _BIOME_EXTS:
            cfg = _biome_config(dirs, root)
            exe = _find_node_tool("biome", dirs) if cfg else None
            if cfg and exe:
                return Formatter("biome", exe), cfg
        return None
    if ext == ".go":
        cfg = _walk_up_for(dirs, root, "go.mod")
        exe = _which("gofmt") if cfg else None
        return (Formatter("gofmt", exe), cfg) if cfg and exe else None
    if ext == ".rs":
        cfg = _walk_up_for(dirs, root, "Cargo.toml")
        exe = _which("rustfmt") if cfg else None
        return (Formatter("rustfmt", exe), cfg) if cfg and exe else None
    return None


def detect(path: Path, root: Path, *, prefer_local: bool = True) -> Formatter | None:
    """The formatter for `path`, ONLY if the repo explicitly configures one and its binary is installed.

    prefer_local: True (the default) = the project's own copy of a python formatter (.venv) wins over the one on PATH.
    False = the repository is not trusted, so its own copy would be refused: a copy outside the repository on PATH
    wins, and the project's own copy is only the last resort (so a run can still say why nothing was formatted).
    Node tools are only ever the project's own (node_modules/.bin), so this changes nothing for them."""
    try:
        hit = _detect(Path(path), Path(root), prefer_local)
    except Exception:  # noqa: BLE001 - a hook must survive anything
        return None
    return hit[0] if hit else None


_REAL_DETECT = detect


def _detector(trusted: bool) -> Callable[[Path, Path], Formatter | None]:
    """detect() under the trust decision, for _process. The module-level `detect` is looked up at call time and gets
    the new keyword only while it is the real function: a stand-in for it (the tests' two-argument lambdas) is called
    as it always was."""

    def run(path: Path, root: Path) -> Formatter | None:
        if detect is _REAL_DETECT:
            return detect(path, root, prefer_local=trusted)
        return detect(path, root)

    return run


def _rust_edition(path: Path) -> str:
    """Edition rustfmt must parse with: nearest Cargo.toml (following `edition.workspace = true`), else 2021."""
    import tomllib

    def load(manifest: Path) -> dict:
        # bounded, regular files only: never a device or an endless file
        text = _read_text(manifest)
        if text is None:
            return {}
        try:
            data = tomllib.loads(text)
        except Exception:  # noqa: BLE001 - TOMLDecodeError, but also RecursionError on hostile nesting
            return {}
        return data if isinstance(data, dict) else {}

    parents = list(Path(path).parents)[:24]
    for i, d in enumerate(parents):
        pkg = load(d / "Cargo.toml").get("package")
        if not isinstance(pkg, dict):
            continue
        edition = pkg.get("edition")
        if isinstance(edition, dict) and edition.get("workspace") is True:
            edition = None
            for up in parents[i + 1 :]:
                ws = load(up / "Cargo.toml").get("workspace")
                if isinstance(ws, dict) and isinstance(ws.get("package"), dict):
                    edition = ws["package"].get("edition")
                    break
        edition = str(edition) if isinstance(edition, (str, int)) else ""
        return edition if edition in {"2015", "2018", "2021", "2024"} else "2021"
    return "2021"


# ---- running a formatter ---------------------------------------------------------------------------------------


def _format(
    f: Formatter, path: Path, text: str, *, cwd: Path, timeout: float
) -> tuple[str | None, str]:
    """(formatted text, "") or (None, why it failed)."""
    try:
        argv = f.stdin_argv(path)
    except (ValueError, OSError) as exc:
        return None, f"cannot build the command: {exc}"
    if paths.IS_WINDOWS and _cmd_shim_unsafe(argv):
        return None, "path has characters that are unsafe to pass to a .cmd shim"
    res = _run(argv, cwd=cwd, timeout=timeout, input_bytes=text.encode("utf-8"))
    if res.timed_out:
        return None, f"timeout after {timeout:g}s"
    if res.rc != 0:
        return None, _tail(res.err or res.out) or f"exit code {res.rc}"
    try:
        out = res.out.decode("utf-8")
    except UnicodeDecodeError:
        return None, "formatter output is not valid UTF-8"
    if f.name == "rustfmt" and not text.startswith("stdin:"):
        out = _RUSTFMT_BANNER.sub("", out)  # a `--emit stdout` header line is not source text
    if text.strip() and not out.strip():
        return None, "formatter produced empty output"
    return out, ""


def format_text(f: Formatter, path: Path, text: str, *, cwd: Path, timeout: float) -> str | None:
    """Formatted `text`, or None when the formatter failed, timed out or produced nothing. Never raises."""
    try:
        return _format(f, Path(path), text, cwd=Path(cwd), timeout=timeout)[0]
    except Exception:  # noqa: BLE001 - a hook must survive anything
        return None


def _eol_style(text: str) -> str | None:
    """Return 'crlf' or 'lf' when the whole text uses one convention, else None (mixed, or no line breaks)."""
    crlf = text.count("\r\n")
    lone_lf = text.count("\n") - crlf
    if crlf and not lone_lf:
        return "crlf"
    if lone_lf and not crlf:
        return "lf"
    return None


def _match_eol(original: str, formatted: str) -> str:
    """Keep the file's own line-ending convention even if the formatter normalises it."""
    want, got = _eol_style(original), _eol_style(formatted)
    if want == "crlf" and got == "lf":
        return formatted.replace("\n", "\r\n")
    if want == "lf" and got == "crlf":
        return formatted.replace("\r\n", "\n")
    return formatted


# ---- finding the files -----------------------------------------------------------------------------------------


@dataclass
class _Cand:
    path: Path  # absolute, in the caller's spelling of root
    rel: str  # posix, relative to root
    top_rel: str  # posix, relative to the git top level ("" when unknown)
    tracked: bool  # False = untracked (never has a HEAD version)
    orig_top_rel: str = ""  # rename/copy only: the path at HEAD (top-level relative)


def _parse_status_entries(raw: bytes) -> list[tuple[str, str, str]]:
    """(XY, path, original path) for each entry of `git status --porcelain=v1 -z`; paths are relative to the repo top
    level, and the original path is "" unless the entry is a rename or copy (git prints it after the new path)."""
    fields = raw.split(b"\0")
    out: list[tuple[str, str, str]] = []
    i = 0
    while i < len(fields):
        item = fields[i]
        i += 1
        if len(item) < 4 or item[2:3] != b" ":
            continue
        xy = item[:2].decode("ascii", "replace")
        orig = ""
        if "R" in xy or "C" in xy:
            if i < len(fields):
                orig = os.fsdecode(fields[i])
            i += 1  # a rename/copy entry is followed by the original path
        out.append((xy, os.fsdecode(item[3:]), orig))
    return out


def _parse_status(raw: bytes) -> list[tuple[str, str]]:
    """(XY, path) for each entry of `git status --porcelain=v1 -z` (paths are relative to the repo top level)."""
    return [(xy, path) for xy, path, _orig in _parse_status_entries(raw)]


def _glob_match(rel: str, globs: Iterable[str]) -> bool:
    for g in [globs] if isinstance(globs, str) else globs:
        g = g.replace("\\", "/")
        if fnmatch.fnmatchcase(rel, g) or (g.startswith("**/") and fnmatch.fnmatchcase(rel, g[3:])):
            return True
    return False


def _path_skip(rel: str, skip_globs: Iterable[str]) -> str | None:
    parts = rel.split("/")
    name = parts[-1].lower()
    if any(p.lower() in _SKIP_DIRS for p in parts[:-1]):
        return "generated or vendored directory"
    if name in _LOCKFILES or name.endswith(
        (".lock", ".lockb", "-lock.json", "-lock.yaml", "-lock.yml")
    ):
        return "lockfile"
    if ".min." in name:
        return "minified file"
    if _glob_match(rel, skip_globs):
        return "matches skip_globs"
    return None


def _content_skip(c: _Cand, root_res: Path, max_kb: int) -> str | None:
    try:
        if c.path.is_symlink():
            return "symlink"
        if not Path(os.path.realpath(c.path)).is_relative_to(root_res):
            return "outside the repository root"
        if c.path.stat().st_size > max_kb * 1024:
            return f"larger than {max_kb} KB"
        with open(c.path, "rb") as fh:
            head = fh.read(_HEAD_BYTES)
    except OSError as exc:
        return f"unreadable: {exc.strerror or exc}"
    if b"\0" in head:
        return "binary file"
    if _GENERATED.search(head[:1024]):
        return "generated file"
    return None


def _toplevel(root: Path, timeout: float) -> Path | None:
    r = _git(root, "rev-parse", "--show-toplevel", timeout=timeout)
    line = r.out.decode("utf-8", "replace").strip() if r.rc == 0 else ""
    return Path(line) if line else None


def _git_timeout(deadline: float) -> float:
    # at least 5 s: a tiny formatter budget must not make git itself time out
    return min(_GIT_MAX_S, max(deadline - _now(), 5.0))


def _status_candidates(
    root: Path, deadline: float, skip_globs: Iterable[str], skipped: list[tuple[str, str]]
) -> list[_Cand]:
    top = _toplevel(root, _git_timeout(deadline))
    if top is None:
        skipped.append((".", "not a git repository"))
        return []
    r = _git(
        root,
        # rename detection on whatever the user's diff.renames says: a moved file is judged against its old path
        "-c",
        "status.renames=true",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=all",
        "--",
        ".",
        timeout=_git_timeout(deadline),
    )
    if r.rc != 0:
        skipped.append((".", f"git status failed: {_tail(r.err)}"))
        return []
    top_res, root_res = top.resolve(), root.resolve()
    out: list[_Cand] = []
    seen: set[str] = set()
    # git always prints "/" separators, even on Windows
    for xy, top_rel, orig in _parse_status_entries(r.out):
        if xy == "!!" or Path(top_rel).suffix.lower() not in _ALL_EXTS or top_rel in seen:
            continue
        try:
            rel = (top_res / top_rel).relative_to(root_res).as_posix()
        except ValueError:
            continue  # outside root (root may be a subdirectory of the repo)
        seen.add(top_rel)
        if "U" in xy or xy in ("AA", "DD"):
            skipped.append((rel, "unmerged (merge conflict)"))
            continue
        reason = _path_skip(rel, skip_globs)
        if reason:
            skipped.append((rel, reason))
            continue
        path = root / rel
        if not path.is_file() and not path.is_symlink():
            continue  # deleted, or a submodule/directory entry
        out.append(_Cand(path, rel, top_rel, tracked=xy != "??", orig_top_rel=orig))
    return sorted(out, key=lambda c: c.rel)


def _path_candidates(
    root: Path,
    targets: Iterable[Path],
    deadline: float,
    skip_globs: Iterable[str],
    skipped: list[tuple[str, str]],
) -> list[_Cand]:
    top = _toplevel(root, _git_timeout(deadline))
    top_res, root_res = (top.resolve() if top else None), root.resolve()
    files: dict[str, Path] = {}
    # files the user named; the contents of a named directory are not reported when unsupported
    named: set[str] = set()
    for t in targets:
        t = Path(t)
        t = t if t.is_absolute() else root / t
        if t.is_dir():
            files.update({_rel(p, root): p for p in _expand_dir(root, t, deadline)})
        else:
            files[_rel(t, root)] = t
            named.add(_rel(t, root))
    out: list[_Cand] = []
    for rel, p in files.items():
        if p.suffix.lower() not in _ALL_EXTS:
            if rel in named:
                skipped.append((rel, "no formatter handles this file type"))
            continue
        try:
            rel = Path(os.path.abspath(p)).relative_to(os.path.abspath(root)).as_posix()
        except ValueError:
            skipped.append((str(p), "outside the repository root"))
            continue
        reason = _path_skip(rel, skip_globs)
        if reason:
            skipped.append((rel, reason))
            continue
        if not p.is_file() and not p.is_symlink():
            skipped.append((rel, "not a file"))
            continue
        top_rel = ""
        if top_res is not None:
            try:
                top_rel = (root_res / rel).relative_to(top_res).as_posix()
            except ValueError:
                pass
        out.append(_Cand(root / rel, rel, top_rel, tracked=bool(top_rel)))
    return sorted(out, key=lambda c: c.rel)


def _expand_dir(root: Path, directory: Path, deadline: float) -> list[Path]:
    """Files under `directory`: what git knows (tracked + untracked, never ignored), else a plain walk."""
    r = _git(
        root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        f":(literal){_rel(directory, root)}",
        timeout=_git_timeout(deadline),
    )
    if r.rc == 0:
        return [root / os.fsdecode(n) for n in r.out.split(b"\0") if n]
    found: list[Path] = []
    for cur, dirnames, filenames in os.walk(directory):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        found.extend(Path(cur) / n for n in filenames)
    return found


def changed_files(root: Path, *, max_files: int, max_kb: int, skip_globs=()) -> list[Path]:
    """Files git reports as modified, added, renamed or untracked (never ignored) that a formatter could handle.

    Skips lockfiles, minified/generated/vendored files, anything over max_kb, binary-looking files and skip_globs;
    only files inside `root` are returned, at most max_files. Never raises.
    """
    try:
        root = Path(os.path.abspath(root))
        root_res = root.resolve()
        skipped: list[tuple[str, str]] = []
        out: list[Path] = []
        for c in _status_candidates(root, _now() + _GIT_MAX_S, skip_globs, skipped):
            if len(out) >= max_files:
                break
            if _content_skip(c, root_res, max_kb) is None:
                out.append(c.path)
        return out
    except Exception:  # noqa: BLE001 - a hook must survive anything
        return []


# ---- formatting ------------------------------------------------------------------------------------------------


def _repo_binary_refusal(f: Formatter, root: Path, cache: dict[str, str | None]) -> str | None:
    """Refuse an executable that the repo itself ships (committed to git): a freshly cloned repo must not get code
    run on every turn. Installed copies (.venv, node_modules) are untracked, so they pass."""
    if f.exe in cache:
        return cache[f.exe]
    reason = None
    try:
        rel = Path(os.path.abspath(f.exe)).relative_to(os.path.abspath(root)).as_posix()
    except ValueError:
        cache[f.exe] = None
        return None  # lives outside the repo (PATH, global install)
    r = _git(root, "ls-files", "-z", "--", f":(literal){rel}", timeout=_GIT_MAX_S)
    if r.rc == 0 and r.out.strip(b"\0"):
        reason = "formatter binary is tracked in git (refusing to run an executable the repo ships)"
    cache[f.exe] = reason
    return reason


_TRUST_HINT = "allowed only for a trusted repository (claude-hr-ctl verify trust) or format.repo_tools = always"


def _repo_code_refusal(f: Formatter, path: Path, root: Path, trusted: bool) -> str | None:
    """The gate runs unattended, so it must not turn "Claude (or a clone) wrote a file" into "that file runs".

    Two things can: a formatter binary that lives inside the repository (.venv, node_modules/.bin: untracked, so an agent
    with write access only can create one), and prettier, whose configuration can load code (JS/TS configs, `plugins`,
    shared config packages, a `parser` that is a path). Prettier's configuration cannot be vetted by reading it: every
    check we wrote differed from prettier's own parsers somewhere (JSON escapes, YAML scalars, padding, duplicate keys),
    so prettier runs unattended only for a trusted repository. The other formatters' configuration (ruff, black, biome,
    gofmt, rustfmt) is data."""
    if trusted:
        return None
    rel = _inside(f.exe, root)
    if rel is not None:
        return f"{f.name} lives inside the repository ({rel}); {_TRUST_HINT}"
    if f.name == "prettier":
        return f"prettier configuration can run code from this repository; {_TRUST_HINT}"
    return None


def _per_file_timeout(deadline: float) -> float:
    return min(_PER_FILE_MAX_S, deadline - _now())


def _head_text(root: Path, c: _Cand, deadline: float) -> tuple[str | None, str | None]:
    """(text of HEAD's version, None) | (None, None) when HEAD has no such file | (None, reason) when unknowable.

    A renamed or copied file is looked up under its ORIGINAL path: that is the version its drift is measured against."""
    r = _git(
        root,
        "cat-file",
        "blob",
        f"HEAD:{c.orig_top_rel or c.top_rel}",
        timeout=_git_timeout(deadline),
    )
    if r.rc == 128:
        if c.orig_top_rel:
            # git says this was renamed from a file that HEAD does not have: nothing to compare with, so do not guess
            return None, "cannot read the HEAD version of the file it was renamed from"
        return None, None  # no HEAD yet, or the file is new in this change
    if r.rc != 0:
        return None, "cannot read the HEAD version"
    data = r.out[3:] if r.out.startswith(_BOM) else r.out
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "HEAD version is not valid UTF-8"


def _gate(f: Formatter, root: Path, c: _Cand, deadline: float) -> str | None:
    """Skip reason when HEAD's version of a tracked file is not already clean under the formatter (a whole-file
    reformat would bury the real change in a drift diff), else None."""
    if not c.tracked or not c.top_rel:
        return None
    head, why = _head_text(root, c, deadline)
    if why:
        return why
    if head is None:
        return None
    timeout = _per_file_timeout(deadline)
    if timeout <= 0:
        return BUDGET_REASON
    out, err = _format(f, c.path, head, cwd=_cwd_for(f, c.path, root), timeout=timeout)
    if out is None:
        return f"cannot verify HEAD formatting: {err}"
    # like with like: _format_one judges the working file after _match_eol, so judge HEAD the same way (a CRLF blob
    # that is clean apart from the formatter's LF output is clean, not drifted)
    return None if _match_eol(head, out) == head else DRIFT_REASON


def _cwd_for(f: Formatter, path: Path, root: Path) -> Path:
    # rustfmt reads stdin but looks for rustfmt.toml from its working directory; the file's own directory finds
    # a nested crate's config and still reaches the root's
    return path.parent if f.name == "rustfmt" else root


def _keeps_owner(tmp: str, before: os.stat_result) -> bool:
    """Give the replacement file the original's owner and group where we can (only root can change owner). False when
    the file would end up owned by somebody else than before, which a rewrite must not do silently."""
    if not hasattr(os, "geteuid"):
        # Windows: ownership comes from the directory's ACL, as for any file written there
        return True
    if (before.st_uid, before.st_gid) == (os.geteuid(), os.getegid()):
        return True
    try:
        os.chown(tmp, before.st_uid, before.st_gid)
        return True
    except OSError:
        return before.st_uid == os.geteuid()  # the group is best effort, the owner is not


def _replace_atomically(path: Path, data: bytes, before: os.stat_result) -> str | None:
    """Replace `path` with `data` without ever truncating it: write a temporary file in the same directory, copy the
    permission bits, then os.replace it over the original. The original is intact until that last step, so a full
    disk, a quota, a network share, a crash or a kill leaves either the old file or the new one, never half of one.

    Returns None on success, or the skip reason when the file changed since `before` (re-checked just before the
    replace). Raises OSError, with the original untouched and the temporary file removed, when anything fails."""
    if not os.access(path, os.W_OK):
        # os.replace would happily overwrite a read-only file the user protected (the directory decides, not the file)
        raise PermissionError(errno.EACCES, "file is read-only")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    directory = os.path.dirname(os.path.abspath(path))
    candidate = os.path.join(directory, f".claude-hr-fmt-{os.urandom(6).hex()}.tmp")
    tmp: str | None = None
    try:
        # O_EXCL on a 48-bit random name: it never clobbers anything, and never follows a planted symlink
        fd = os.open(candidate, flags, 0o600)
        tmp = candidate
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, stat.S_IMODE(before.st_mode))
        if not _keeps_owner(tmp, before):
            raise PermissionError(errno.EPERM, "cannot keep the file's owner")
        for attempt in range(_REPLACE_TRIES):
            now = os.lstat(path)
            if not stat.S_ISREG(now.st_mode) or (now.st_size, now.st_mtime_ns) != (
                before.st_size,
                before.st_mtime_ns,
            ):
                return "changed while formatting"
            try:
                os.replace(tmp, path)
                break
            except PermissionError as exc:
                # Windows: replacing a file fails while anything else has it open (an antivirus scan, an indexer, an
                # editor, a program that opened it without share-delete), where an in-place write would have worked.
                # Wait a moment; if the lock stays, say so and leave the file as it is. Never fall back to a truncating
                # write: that is the half-written file the atomic replace exists to prevent. Elsewhere it is final.
                if not paths.IS_WINDOWS:
                    raise
                if attempt == _REPLACE_TRIES - 1:
                    raise PermissionError(errno.EACCES, "file in use by another process") from exc
                time.sleep(0.05 * 2**attempt)
        tmp = None
        return None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _format_one(
    f: Formatter,
    c: _Cand,
    root: Path,
    report: FormatReport,
    *,
    deadline: float,
    clean_gate: bool,
    check: bool,
) -> None:
    path = c.path
    try:
        # lstat: a symlink swapped in after the candidate scan must not be written through
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            report.skipped.append((c.rel, "not a regular file"))
            return
        if before.st_nlink > 1:
            # an atomic rewrite replaces the file's directory entry, which would silently unlink it from its twin(s)
            report.skipped.append((c.rel, "hard-linked file"))
            return
        data = path.read_bytes()
    except OSError as exc:
        report.errors.append((c.rel, f"cannot read: {exc.strerror or exc}"))
        return
    bom = data.startswith(_BOM)
    try:
        text = (data[3:] if bom else data).decode("utf-8")
    except UnicodeDecodeError:
        report.skipped.append((c.rel, "not valid UTF-8"))
        return
    timeout = _per_file_timeout(deadline)
    if timeout <= 0:
        report.skipped.append((c.rel, BUDGET_REASON))
        return
    out, err = _format(f, path, text, cwd=_cwd_for(f, path, root), timeout=timeout)
    if out is None:
        report.errors.append((c.rel, err))
        return
    new_text = _match_eol(text, out)
    if new_text == text:
        return  # already formatted: no write, no gate, mtime untouched
    if clean_gate:
        reason = _gate(f, root, c, deadline)
        if reason:
            report.skipped.append((c.rel, reason))
            return
    if check:
        report.formatted.append(c.rel)
        return
    try:
        # bytes, so the newline convention is never rewritten
        changed = _replace_atomically(
            path, (_BOM if bom else b"") + new_text.encode("utf-8"), before
        )
    except OSError as exc:
        report.errors.append((c.rel, f"cannot write: {exc.strerror or exc}"))
        return
    if changed:
        report.skipped.append((c.rel, changed))
        return
    report.formatted.append(c.rel)


def _process(
    root: Path,
    cands: list[_Cand],
    report: FormatReport,
    *,
    deadline: float,
    max_files: int,
    max_kb: int,
    allow: Iterable[str] | None,
    clean_gate: bool,
    check: bool,
    detector: Callable[[Path, Path], Formatter | None],
    trusted: bool = False,
) -> None:
    root_res = root.resolve()
    allowed = {a.lower() for a in ([allow] if isinstance(allow, str) else allow or ())}
    refusals: dict[str, str | None] = {}
    attempted = capped = 0
    for c in cands:
        if attempted >= max_files:
            capped += 1
            continue
        if _now() >= deadline:
            report.skipped.append((c.rel, BUDGET_REASON))
            continue
        reason = _content_skip(c, root_res, max_kb)
        if reason:
            report.skipped.append((c.rel, reason))
            continue
        f = detector(c.path, root)
        if f is None:
            report.skipped.append((c.rel, "no formatter configured"))
            continue
        if allowed and f.name not in allowed:
            report.skipped.append((c.rel, f"{f.name} is not in the allowed formatters"))
            continue
        reason = _repo_code_refusal(f, c.path, root, trusted) or _repo_binary_refusal(
            f, root, refusals
        )
        if reason:
            report.skipped.append((c.rel, reason))
            continue
        attempted += 1
        if f.name not in report.formatters:
            report.formatters.append(f.name)
        _format_one(f, c, root, report, deadline=deadline, clean_gate=clean_gate, check=check)
    if capped:
        report.skipped.append((f"{capped} more file(s)", f"max_files cap ({max_files})"))


def format_changed(
    root: Path,
    *,
    timeout_s: float = 30.0,
    max_files: int = 40,
    max_kb: int = 512,
    skip_globs=(),
    allow=None,
    clean_gate: bool = True,
    check: bool = False,
    trusted: bool = False,
) -> FormatReport:
    """Format the files git reports as changed, with the formatter each repo configures. Never raises.

    The scope is the working tree's uncommitted files, as `git status` reports them: not only the files an agent
    edited (nothing here knows who changed a file), which is what a pre-commit formatter hook would also do.

    clean_gate: a tracked file is rewritten only if HEAD's version is already clean under the formatter (a renamed
    file is judged against the version at its old path), so the change never turns into a whole-file drift diff;
    untracked/new files are always formatted. A write replaces the file atomically; if it fails the original stays.
    allow: formatter names to use; empty/None = any detected one. timeout_s bounds the whole call.
    check=True writes nothing and lists in `formatted` the files that would change.
    trusted: the repository is trusted (the user approved its verify recipe, or chose format.repo_tools = always): only then
    may a formatter that lives inside the repository, or prettier configuration that executes code, run. Default False.
    Until then a ruff or black on PATH (outside the repository) is preferred over the project's own .venv copy, which
    would be refused; with the repository trusted the project's own pinned copy wins.
    """
    started = _now()
    report = FormatReport()
    try:
        root = Path(os.path.abspath(root))
        deadline = started + timeout_s
        cands = _status_candidates(root, deadline, skip_globs, report.skipped)
        _process(
            root,
            cands,
            report,
            deadline=deadline,
            max_files=max_files,
            max_kb=max_kb,
            allow=allow,
            clean_gate=clean_gate,
            check=check,
            detector=_detector(trusted),
            trusted=trusted,
        )
    except Exception as exc:  # noqa: BLE001 - a hook must survive anything; the failure is reported, not raised
        report.errors.append((".", f"internal error: {type(exc).__name__}: {exc}"))
    report.elapsed_s = round(_now() - started, 3)
    return report


def format_paths(
    root: Path,
    targets: Iterable[Path],
    *,
    timeout_s: float = 30.0,
    max_files: int = 400,
    max_kb: int = 512,
    skip_globs=(),
    allow=None,
    clean_gate: bool = False,
    check: bool = False,
    trusted: bool = False,
) -> FormatReport:
    """Deliberate `claude-hr-ctl fmt <paths>`: format the given files (directories expand to the files git knows
    about). Relative targets are resolved against `root`. The clean gate is off by default because the user asked."""
    started = _now()
    report = FormatReport()
    try:
        root = Path(os.path.abspath(root))
        deadline = started + timeout_s
        cands = _path_candidates(root, targets, deadline, skip_globs, report.skipped)
        _process(
            root,
            cands,
            report,
            deadline=deadline,
            max_files=max_files,
            max_kb=max_kb,
            allow=allow,
            clean_gate=clean_gate,
            check=check,
            detector=_detector(trusted),
            trusted=trusted,
        )
    except Exception as exc:  # noqa: BLE001
        report.errors.append((".", f"internal error: {type(exc).__name__}: {exc}"))
    report.elapsed_s = round(_now() - started, 3)
    return report


# ---- reporting -------------------------------------------------------------------------------------------------


def describe(root: Path, trusted: bool = False) -> list[dict]:
    """Which formatter handles which extensions at the repo root (for `claude-hr-ctl fmt`).

    Each entry: {name, exe, extensions, configured_by}, plus `usable: False` and `reason` when a run would refuse
    the binary. Only the root directory is probed; a nested config can add more at run time. Never raises.
    `trusted` decides which copy is listed, exactly as it does for a run (see format_changed).
    """
    try:
        root = Path(os.path.abspath(root))
        groups: dict[tuple[str, str, str], list[str]] = {}
        for ext in sorted(_ALL_EXTS):
            hit = _detect(root / f"x{ext}", root, trusted)
            if hit:
                groups.setdefault((hit[0].name, hit[0].exe, hit[1]), []).append(ext)
        refusals: dict[str, str | None] = {}
        out = []
        for (name, exe, cfg), exts in groups.items():
            entry: dict = {"name": name, "exe": exe, "extensions": exts, "configured_by": cfg}
            f = Formatter(name, exe)
            reason = _repo_code_refusal(f, root / f"x{exts[0]}", root, trusted) or (
                _repo_binary_refusal(f, root, refusals)
            )
            if reason:
                entry.update(usable=False, reason=reason)
            out.append(entry)
        return out
    except Exception:  # noqa: BLE001
        return []

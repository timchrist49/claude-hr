"""Format the files an agent just changed, with the formatter the repo itself configures.

A formatter is chosen ONLY when the repo explicitly configures it (never imposed), and it runs through
stdin/stdout so a file is rewritten only when its formatted text really differs. Meant to be called from a hook:
the public entry points never raise, nothing here touches the network, and nothing downloads a tool
(no `uv run`, `npx`, `pipx`).

Import cost matters (the stop hook imports this on every turn that changed files): subprocess, json and tomllib are
imported inside the functions that need them.
"""

from __future__ import annotations

import fnmatch
import os
import re
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
_SECRET_ENV = re.compile(r"token|secret|passw|credential|api[_-]?key|private[_-]?key|auth", re.I)
_GIT_REDIRECT_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE",
)  # fmt: skip

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


def _clean_env() -> dict[str, str]:
    """Environment for git and formatters: no credentials (a formatter may execute repo-controlled config), and
    nothing that would redirect git to another repository when we are called from inside a git hook."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not _SECRET_ENV.search(k)
        and not k.startswith("ANTHROPIC_")
        and k not in _GIT_REDIRECT_ENV
    }
    env.update(
        GIT_OPTIONAL_LOCKS="0",  # `git status` must not take the index lock away from the user's own git commands
        GIT_TERMINAL_PROMPT="0",
        NO_COLOR="1",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        RUSTUP_AUTO_INSTALL="0",  # a rustup shim must never download a toolchain mid-turn
    )
    return env


def _kill_tree(proc) -> None:
    """Kill the formatter and its children (a Windows .cmd shim leaves node running after its cmd.exe dies)."""
    try:
        if paths.IS_WINDOWS:
            procutil.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], timeout=10)
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
    """Absolute path of git, found on absolute PATH entries only (never the repo's own directory)."""
    key = os.environ.get("PATH", "")
    if key not in _GIT_EXE:
        _GIT_EXE[key] = _which("git")
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

_WHICH_CACHE: dict[tuple[str, str], str] = {}


def _which(name: str) -> str | None:
    """First `name` on PATH. Relative PATH entries are ignored: the hook's cwd is the (untrusted) repo and a bare
    relative entry (or Windows' implicit current directory) would resolve a planted binary there."""
    path_env = os.pathsep.join(
        d for d in os.environ.get("PATH", "").split(os.pathsep) if d and os.path.isabs(d)
    )
    key = (name, path_env)
    hit = _WHICH_CACHE.get(key)
    if hit and os.path.isfile(hit):
        return hit
    for found in procutil.iter_which(name, path_env):
        _WHICH_CACHE[key] = os.path.abspath(found)
        return _WHICH_CACHE[key]
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


def _read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def _toml_has_table(text: str, *names: str) -> bool:
    for name in names:
        parts = r"[ \t]*\.[ \t]*".join(re.escape(n) for n in name.split("."))
        if re.search(rf"^[ \t]*\[[ \t]*{parts}[ \t]*\][ \t\r]*(?:#.*)?$", text, re.M):
            return True
    return False


def _find_python_tool(name: str, dirs: list[Path]) -> str | None:
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


def _detect_python(dirs: list[Path], root: Path) -> tuple[Formatter, str] | None:
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
        for tool, cfg in found:
            exe = _find_python_tool(tool, dirs)
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
            except ValueError:
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


def _detect(path: Path, root: Path) -> tuple[Formatter, str] | None:
    """(formatter, config file that selected it) or None."""
    ext = path.suffix.lower()
    dirs = _dirs_up(path.parent, root)
    if not dirs:
        return None
    if ext in _PY_EXTS:
        return _detect_python(dirs, root)
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


def detect(path: Path, root: Path) -> Formatter | None:
    """The formatter for `path`, ONLY if the repo explicitly configures one and its binary is installed."""
    try:
        hit = _detect(Path(path), Path(root))
    except Exception:  # noqa: BLE001 - a hook must survive anything
        return None
    return hit[0] if hit else None


def _rust_edition(path: Path) -> str:
    """Edition rustfmt must parse with: nearest Cargo.toml (following `edition.workspace = true`), else 2021."""
    import tomllib

    def load(manifest: Path) -> dict:
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
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


def _parse_status(raw: bytes) -> list[tuple[str, str]]:
    """(XY, path) for each entry of `git status --porcelain=v1 -z` (paths are relative to the repo top level)."""
    fields = raw.split(b"\0")
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        item = fields[i]
        i += 1
        if len(item) < 4 or item[2:3] != b" ":
            continue
        xy = item[:2].decode("ascii", "replace")
        out.append((xy, os.fsdecode(item[3:])))
        if "R" in xy or "C" in xy:
            i += 1  # a rename/copy entry is followed by the original path
    return out


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
    for xy, top_rel in _parse_status(r.out):  # git always prints "/" separators, even on Windows
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
        out.append(_Cand(path, rel, top_rel, tracked=xy != "??"))
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


_MAX_CONFIG_READ = 256 * 1024
_JS_CONFIG = re.compile(r"^(?:prettier\.config|\.prettierrc)\.(?:js|cjs|mjs|ts|cts|mts)$")
_TRUST_HINT = "allowed only for a trusted repository (claude-hr-ctl verify trust) or format.repo_tools = always"


def _config_runs_code(p: Path) -> str | None:
    """Why this prettier configuration file can execute code (a JS/TS config, plugins, a shared config package)."""
    import json

    if _JS_CONFIG.match(p.name):
        return f"{p.name} is an executable prettier config"
    try:
        with open(p, "rb") as fh:
            text = fh.read(_MAX_CONFIG_READ).decode("utf-8-sig", errors="replace")
    except OSError:
        return None
    if p.name == "package.json":
        try:
            data = json.loads(text)
        except ValueError:
            return None
        conf = data.get("prettier") if isinstance(data, dict) else None
        if conf is None:
            return None
        if isinstance(conf, str):
            return "package.json names a shared prettier config package"
        text = json.dumps(conf)
    elif p.name == "package.yaml" and "prettier" not in text.lower():
        return None
    if re.search(r"plugins", text, re.I) or text.lstrip().startswith(('"', "'")):
        return f"{p.name} loads prettier plugins or a shared config package"
    return None


def _prettier_code_reason(path: Path, root: Path) -> str | None:
    """Prettier runs JS from a repo's configuration, so every config it could pick up is checked, not just the first."""
    for d in _dirs_up(path.parent, root):
        for name in (*_PRETTIER_CONFIGS, "package.json", "package.yaml"):
            p = d / name
            try:
                if p.is_file():
                    why = _config_runs_code(p)
                    if why:
                        return why
            except OSError:
                continue
    return None


def _repo_code_refusal(f: Formatter, path: Path, root: Path, trusted: bool) -> str | None:
    """The gate runs unattended, so it must not turn "Claude (or a clone) wrote a file" into "that file runs": a formatter
    binary that lives inside the repository (.venv, node_modules/.bin: untracked, so a write-only agent can create one) and
    prettier configuration that executes code are used only for a trusted repository."""
    if trusted:
        return None
    try:
        rel = Path(os.path.abspath(f.exe)).relative_to(os.path.abspath(root)).as_posix()
    except ValueError:
        rel = None
    if rel is not None:
        return f"{f.name} lives inside the repository ({rel}); {_TRUST_HINT}"
    if f.name == "prettier":
        why = _prettier_code_reason(path, root)
        if why:
            return f"{why}; {_TRUST_HINT}"
    return None


def _per_file_timeout(deadline: float) -> float:
    return min(_PER_FILE_MAX_S, deadline - _now())


def _head_text(root: Path, c: _Cand, deadline: float) -> tuple[str | None, str | None]:
    """(text of HEAD's version, None) | (None, None) when HEAD has no such file | (None, reason) when unknowable."""
    r = _git(root, "cat-file", "blob", f"HEAD:{c.top_rel}", timeout=_git_timeout(deadline))
    if r.rc == 128:
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
    return None if out == head else DRIFT_REASON


def _cwd_for(f: Formatter, path: Path, root: Path) -> Path:
    # rustfmt reads stdin but looks for rustfmt.toml from its working directory; the file's own directory finds
    # a nested crate's config and still reaches the root's
    return path.parent if f.name == "rustfmt" else root


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
        before = path.stat()
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
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            report.skipped.append((c.rel, "changed while formatting"))
            return
        with open(path, "wb") as fh:  # bytes, so the newline convention is never rewritten
            fh.write((_BOM if bom else b"") + new_text.encode("utf-8"))
    except OSError as exc:
        report.errors.append((c.rel, f"cannot write: {exc.strerror or exc}"))
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

    clean_gate: a tracked file is rewritten only if HEAD's version is already clean under the formatter, so the
    change never turns into a whole-file drift diff; untracked/new files are always formatted.
    allow: formatter names to use; empty/None = any detected one. timeout_s bounds the whole call.
    check=True writes nothing and lists in `formatted` the files that would change.
    trusted: the repository is trusted (the user approved its verify recipe, or chose format.repo_tools = always): only then
    may a formatter that lives inside the repository, or prettier configuration that executes code, run. Default False.
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
            detector=lambda p, r: detect(p, r),
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
            detector=lambda p, r: detect(p, r),
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
    """
    try:
        root = Path(os.path.abspath(root))
        groups: dict[tuple[str, str, str], list[str]] = {}
        for ext in sorted(_ALL_EXTS):
            hit = _detect(root / f"x{ext}", root)
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

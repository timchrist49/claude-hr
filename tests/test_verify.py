"""verify.py: discovery, strict config, trust, safe execution, fingerprint, test-weakening advisory, the gate.

Real temp git repos and tiny fake checks run through sys.executable. Nothing here touches the real profile (the
`sandbox` fixture points claude-hr at tmp_path), starts a scheduled task or uses the network.
"""

import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from claude_hr import paths, verify
from claude_hr.verify import Check, Recipe, VerifyConfigError

PY = sys.executable


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """The suite may itself run under a gate or a git hook; neither may leak into the temp repos."""
    monkeypatch.delenv(verify.ACTIVE_ENV, raising=False)
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def state(sandbox):
    d = paths.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---- helpers ----------------------------------------------------------------------------------------------------
def git(repo, *args):
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout


def put(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def commit(repo, message="change"):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def make_repo(tmp_path, name="repo", files=None, do_commit=True):
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q")
    with open(repo / ".git" / "config", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(
            "[user]\n    email = t@example.com\n    name = Test\n"
            "[core]\n    autocrlf = false\n[commit]\n    gpgsign = false\n"
        )
    for rel, text in (files or {"README.md": "hello\n"}).items():
        put(repo, rel, text)
    if do_commit:
        commit(repo, "init")
    return repo


def pycheck(name, code, timeout=30, cwd="."):
    return Check(name, [PY, "-c", code], timeout, cwd)


def write_cfg(repo, fast, full=None):
    verify.config_path(repo).parent.mkdir(parents=True, exist_ok=True)
    text = verify.render_toml(Recipe(fast, full if full is not None else [], "test"))
    verify.config_path(repo).write_text(text, encoding="utf-8", newline="\n")


def runs(marker):
    return marker.read_text().count("x") if marker.exists() else 0


def marker_check(marker, name="unit"):
    """Passes when code.txt says ok; leaves one 'x' in `marker` (outside the repo) per execution."""
    code = (
        f"import sys; open({str(marker)!r}, 'a').write('x'); t = open('code.txt').read().strip(); "
        "print('code.txt says', t); sys.exit(0 if t == 'ok' else 1)"
    )
    return pycheck(name, code)


def gated_repo(tmp_path, state, fast, full=None, files=None, name="repo"):
    base = {"code.txt": "ok\n"}
    base.update(files or {})
    repo = make_repo(tmp_path, name, base, do_commit=False)
    write_cfg(repo, fast, full)
    commit(repo, "init")
    verify.trust(repo, state)
    return repo


def alive(pid):
    if os.name == "nt":
        from claude_hr import sysinfo

        return sysinfo.pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def kill_pid(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def make_dir_link(link, target):
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
        )
        if r.returncode == 0:
            return
    pytest.skip("cannot create a directory link here")


# =================================================================================================================
# discover
# =================================================================================================================
def names(checks):
    return [c.name for c in checks]


def test_discover_node_scripts_and_package_manager(tmp_path):
    put(
        tmp_path,
        "package.json",
        json.dumps(
            {
                "scripts": {
                    "lint": "eslint .",
                    "typecheck": "tsc --noEmit",
                    "test": "vitest run",
                    "build": "vite build",
                    "dev": "vite",
                }
            }
        ),
    )
    put(tmp_path, "pnpm-lock.yaml", "lockfileVersion: 9\n")
    r = verify.discover(tmp_path)
    assert [c.argv for c in r.fast] == [
        ["pnpm", "run", "lint"],
        ["pnpm", "run", "typecheck"],
        ["pnpm", "run", "test"],
    ]
    assert [c.argv for c in r.full] == [c.argv for c in r.fast] + [["pnpm", "run", "build"]]
    assert [c.timeout_s for c in r.full] == [90, 120, 180, 300]
    assert all(c.cwd == "." for c in r.full)
    assert r.source == "discovered from package.json"
    assert not any(c.argv[2] == "dev" for c in r.full)


@pytest.mark.parametrize(
    ("extra_files", "pkg_extra", "expected"),
    [
        ({"yarn.lock": ""}, {}, "yarn"),
        ({"bun.lockb": ""}, {}, "bun"),
        ({"bun.lock": ""}, {}, "bun"),
        ({"package-lock.json": "{}"}, {}, "npm"),
        ({"pnpm-lock.yaml": "", "package-lock.json": "{}"}, {}, "pnpm"),
        ({}, {"packageManager": "yarn@4.1.0"}, "yarn"),
        ({}, {"packageManager": "evilpm@1"}, "npm"),
        ({}, {}, "npm"),
    ],
)
def test_discover_node_package_manager_choice(tmp_path, extra_files, pkg_extra, expected):
    put(tmp_path, "package.json", json.dumps({"scripts": {"test": "jest"}, **pkg_extra}))
    for rel, text in extra_files.items():
        put(tmp_path, rel, text)
    assert verify.discover(tmp_path).fast[0].argv == [expected, "run", "test"]


def test_discover_node_skips_placeholder_and_watch_scripts(tmp_path):
    put(
        tmp_path,
        "package.json",
        json.dumps(
            {"scripts": {"test": 'echo "Error: no test specified" && exit 1', "lint": "eslint ."}}
        ),
    )
    r = verify.discover(tmp_path)
    assert names(r.fast) == ["npm lint"]
    assert any("placeholder or a watcher" in n for n in r.notes)
    put(tmp_path, "package.json", json.dumps({"scripts": {"test": "jest --watch"}}))
    assert verify.discover(tmp_path).fast == []
    put(tmp_path, "package.json", json.dumps({"scripts": {"test": "jest --watchAll=false"}}))
    assert names(verify.discover(tmp_path).fast) == ["npm test"]


def test_discover_node_test_unit_is_the_fast_test(tmp_path):
    put(
        tmp_path,
        "package.json",
        json.dumps(
            {"scripts": {"test:unit": "vitest run", "test": "vitest run && playwright test"}}
        ),
    )
    r = verify.discover(tmp_path)
    assert [c.argv[2] for c in r.fast] == ["test:unit"]
    assert [c.argv[2] for c in r.full] == ["test:unit", "test"]


PYPROJECT = """\
[project]
name = "x"

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100

[tool.mypy]
strict = true
"""


def test_discover_python_tools_and_the_confirm_note(tmp_path):
    put(tmp_path, "pyproject.toml", PYPROJECT)
    r = verify.discover(tmp_path)
    assert [c.argv for c in r.fast] == [
        ["ruff", "check", "."],
        ["python", "-m", "mypy", "."],
        ["python", "-m", "pytest", "-q"],
    ]
    assert names(r.fast) == ["ruff", "mypy", "pytest"]
    assert any("confirm the executable" in n for n in r.notes)


def test_discover_python_executable_documented_by_the_repo(tmp_path):
    put(tmp_path, "pyproject.toml", PYPROJECT)
    put(tmp_path, "README.md", "Run the tests with `python3 -m pytest`.\n")
    r = verify.discover(tmp_path)
    assert r.fast[-1].argv == ["python3", "-m", "pytest", "-q"]
    assert not any("confirm the executable" in n for n in r.notes)


def test_discover_python_uses_the_environment_runner_from_the_lockfile(tmp_path):
    put(tmp_path, "pyproject.toml", PYPROJECT)
    put(tmp_path, "uv.lock", "version = 1\n")
    r = verify.discover(tmp_path)
    assert [c.argv for c in r.fast] == [
        ["uv", "run", "ruff", "check", "."],
        ["uv", "run", "mypy", "."],
        ["uv", "run", "pytest", "-q"],
    ]
    assert not any("confirm the executable" in n for n in r.notes)


def test_discover_python_mypy_files_pyright_and_tests_dir(tmp_path):
    put(
        tmp_path,
        "pyproject.toml",
        '[tool.mypy]\nfiles = ["src"]\n\n[tool.pyright]\nstrict = ["src"]\n',
    )
    put(tmp_path, "tests/test_a.py", "def test_a():\n    assert True\n")
    r = verify.discover(tmp_path)
    assert [c.argv for c in r.fast] == [
        ["python", "-m", "mypy"],
        ["pyright"],
        ["python", "-m", "pytest", "-q"],
    ]


def test_discover_python_needs_python_markers_and_real_test_files(tmp_path):
    put(tmp_path, "tests/test_a.py", "def test_a():\n    assert True\n")
    assert (
        verify.discover(tmp_path).fast == []
    )  # a tests dir alone does not make this a Python project
    put(tmp_path, "requirements.txt", "requests\n")
    assert names(verify.discover(tmp_path).fast) == ["pytest"]
    empty = tmp_path / "other"
    (empty / "tests").mkdir(parents=True)
    put(empty, "requirements.txt", "requests\n")
    assert verify.discover(empty).fast == []


def test_discover_tox_nox_and_precommit_are_notes_only(tmp_path):
    put(tmp_path, "tox.ini", "[tox]\nenvlist = py311\n")
    put(tmp_path, "noxfile.py", "import nox\n")
    put(tmp_path, ".pre-commit-config.yaml", "repos: []\n")
    r = verify.discover(tmp_path)
    assert r.fast == [] and r.full == []
    assert any("tox/nox" in n for n in r.notes)
    assert any(".pre-commit-config.yaml" in n and "not run" in n for n in r.notes)


def test_discover_makefile_and_justfile_targets(tmp_path):
    put(
        tmp_path,
        "Makefile",
        ".PHONY: test lint\ncheck := 1\n\ntest:\n\tpytest\n\nlint :\n\truff check .\n\nbuild:\n\techo b\n",
    )
    put(tmp_path, "justfile", "check:\n    echo c\n\nlint arg:\n    echo l\n")
    r = verify.discover(tmp_path)
    assert [c.argv for c in r.fast] == [["make", "lint"], ["just", "check"], ["make", "test"]]
    assert not any("build" in c.name for c in r.full)  # only test/lint/check targets are proposed
    assert r.source == "discovered from Makefile, justfile"


def test_discover_task_runner_does_not_duplicate_a_covered_category(tmp_path):
    put(tmp_path, "package.json", json.dumps({"scripts": {"test": "jest"}}))
    put(tmp_path, "Makefile", "test:\n\tnpm test\n\nlint:\n\teslint .\n")
    r = verify.discover(tmp_path)
    assert [c.argv for c in r.fast] == [["make", "lint"], ["npm", "run", "test"]]
    assert any("'make test' exists" in n for n in r.notes)


def test_discover_rust_and_go(tmp_path):
    put(tmp_path, "Cargo.toml", '[package]\nname = "x"\n')
    put(tmp_path, "go.mod", "module x\n\ngo 1.22\n")
    r = verify.discover(tmp_path)
    assert [c.argv for c in r.fast] == [
        ["cargo", "clippy", "-q"],
        ["go", "vet", "./..."],
        ["cargo", "test", "-q"],
        ["go", "test", "./..."],
    ]
    assert [c.argv for c in r.full][-2:] == [["cargo", "build", "-q"], ["go", "build", "./..."]]


CI_YAML = """\
name: ci
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Lint
        run: ruff check .
      - run: pytest -q
      - name: Several
        run: |
          pip install -e .
          # a comment about pytest
          cargo clippy -- -D warnings
          echo done
      - run: echo "hello"
      - run: "npm run lint"
      - name: After
        run: make deploy
"""


def test_discover_ci_run_lines_are_comments_never_checks(tmp_path):
    put(tmp_path, ".github/workflows/ci.yml", CI_YAML)
    r = verify.discover(tmp_path)
    assert r.fast == [] and r.full == []
    ci = [n.split(": ", 1)[1] for n in r.notes if n.startswith("CI runs")]
    assert ci == ["ruff check .", "pytest -q", "cargo clippy -- -D warnings", "npm run lint"]


def test_discover_executes_nothing(tmp_path, monkeypatch):
    put(tmp_path, "package.json", json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}}))
    put(tmp_path, "pyproject.toml", PYPROJECT)
    put(tmp_path, "Makefile", "test:\n\tpytest\n")
    put(tmp_path, ".github/workflows/ci.yml", CI_YAML)

    def boom(*args, **kwargs):
        raise AssertionError("discover must never run anything")

    with monkeypatch.context() as m:
        m.setattr(subprocess, "Popen", boom)
        m.setattr(subprocess, "run", boom)
        m.setattr(os, "system", boom)
        r = verify.discover(tmp_path)
    assert r.fast


def test_discover_tolerates_garbage_and_empty_repos(tmp_path):
    empty = verify.discover(tmp_path)
    assert (empty.fast, empty.full) == ([], [])
    assert empty.source == "discovered: nothing recognised"
    bad = tmp_path / "bad"
    bad.mkdir()
    put(bad, "package.json", "{not json")
    put(bad, "pyproject.toml", "[[[")
    (bad / "Makefile").write_bytes(b"\xff\xfe\x00test:\n")
    (bad / ".github" / "workflows").mkdir(parents=True)
    (bad / ".github" / "workflows" / "x.yml").write_bytes(b"\xff\xff")
    r = verify.discover(bad)
    assert isinstance(r, Recipe)
    assert verify.discover(tmp_path / "does-not-exist").fast == []


# =================================================================================================================
# render_toml / load
# =================================================================================================================
def polyglot(tmp_path):
    put(
        tmp_path,
        "package.json",
        json.dumps({"scripts": {"lint": "eslint .", "test": "jest", "build": "tsc"}}),
    )
    put(tmp_path, "pyproject.toml", PYPROJECT)
    put(tmp_path, "Cargo.toml", '[package]\nname = "x"\n')
    put(tmp_path, "go.mod", "module x\n")
    put(tmp_path, ".github/workflows/ci.yml", CI_YAML)
    put(tmp_path, "tox.ini", "[tox]\n")
    return verify.discover(tmp_path)


def test_render_toml_round_trips_through_load(tmp_path):
    recipe = polyglot(tmp_path)
    text = verify.render_toml(recipe)
    assert "\r" not in text and text.endswith("\n")
    verify.config_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    verify.config_path(tmp_path).write_text(text, encoding="utf-8", newline="\n")
    loaded = verify.load(tmp_path)
    assert loaded is not None
    assert loaded.fast == recipe.fast and loaded.full == recipe.full
    assert loaded.source == str(verify.config_path(tmp_path))


def test_render_toml_header_explains_the_trust_model():
    text = verify.render_toml(Recipe([], [], "nothing"))
    head = text.split("\n\n")[0].replace("\n# ", " ")
    for needle in (
        "unattended",
        "end of each Claude turn",
        "claude-hr-ctl verify trust",
        "argv",
        "hash",
    ):
        assert needle in head
    assert "# No [[fast]] checks." in text and "# No [[full]] checks." in text
    assert tomllib.loads(text) == {}


def test_render_toml_format_of_one_table():
    text = verify.render_toml(Recipe([Check("unit", ["npm", "run", "test"], 90, "web")], [], "x"))
    assert (
        '[[fast]]\nname = "unit"\nargv = ["npm", "run", "test"]\ntimeout_s = 90\ncwd = "web"\n'
        in text
    )


def test_render_toml_escapes_hostile_strings_and_notes(tmp_path):
    weird = [
        "a\\b",
        "it's",
        'say "hi"',
        "caf" + chr(0xE9) + " " + chr(0x2713),
        "tab\there",
        "del\x7fchar",
        "-c",
        "x = 1\n[[fast]]",
    ]
    check = Check('unit "q" tests', ["tool", *weird], 30, "sub dir")
    recipe = Recipe(
        [check], [check], "src\nwith\nnewlines", ["evil\n[[fast]]\nname = 'x'", "ctl\x07\x1b[31m"]
    )
    text = verify.render_toml(recipe)
    doc = tomllib.loads(text)
    assert len(doc["fast"]) == 1 and len(doc["full"]) == 1 and set(doc) == {"fast", "full"}
    (tmp_path / "sub dir").mkdir()
    verify.config_path(tmp_path).parent.mkdir(parents=True)
    verify.config_path(tmp_path).write_text(text, encoding="utf-8", newline="\n")
    loaded = verify.load(tmp_path)
    assert loaded.fast == [check] and loaded.full == [check]


def test_load_missing_is_none(tmp_path):
    assert verify.load(tmp_path) is None
    (tmp_path / ".claude").write_text("a file, not a directory", encoding="utf-8")
    assert verify.load(tmp_path) is None


def write_raw(root, text):
    p = verify.config_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))


def one(argv='["pytest", "-q"]', extra=""):
    return f'[[fast]]\nname = "unit"\nargv = {argv}\n{extra}'


@pytest.mark.parametrize(
    ("config", "fragment"),
    [
        (one('["npm run test && npm run lint"]'), "shell characters"),
        (one('["pytest;rm"]'), "shell characters"),
        (one('["$(whoami)"]'), "shell characters"),
        (one('["a|b"]'), "shell characters"),
        (one('["tool`x`"]'), "shell characters"),
        (one('["npm run test"]'), "whole command line"),
        (one('["   "]'), "blank"),
        (one('[" pytest"]'), "whitespace"),
        (one('"npm run test"'), "non-empty list"),
        (one("[]"), "non-empty list"),
        (one('["pytest", ""]'), "argv[1] must be a non-empty string"),
        (one('["pytest", 5]'), "argv[1] must be a non-empty string"),
        (one('[["pytest"]]'), "argv[0] must be a non-empty string"),
        (one(extra="timeout_s = 0\n"), "timeout_s must be an integer from 1 to 900"),
        (one(extra="timeout_s = 901\n"), "timeout_s must be an integer from 1 to 900"),
        (one(extra="timeout_s = -5\n"), "timeout_s must be an integer from 1 to 900"),
        (one(extra='timeout_s = "30"\n'), "timeout_s must be an integer"),
        (one(extra="timeout_s = true\n"), "timeout_s must be an integer"),
        (one(extra="timeout_s = 30.5\n"), "timeout_s must be an integer"),
        (one(extra='cwd = ".."\n'), "'..'"),
        (one(extra='cwd = "a/../.."\n'), "'..'"),
        (one(extra='cwd = "sub/../x"\n'), "'..'"),
        (one(extra='cwd = "/etc"\n'), "relative"),
        (one(extra='cwd = "C:\\\\Windows"\n'), "relative"),
        (one(extra='cwd = "C:foo"\n'), "relative"),
        (one(extra='cwd = "\\\\\\\\server\\\\share"\n'), "relative"),
        (one(extra='cwd = ""\n'), "non-empty relative path"),
        (one(extra="cwd = 5\n"), "non-empty relative path"),
        (one(extra="timeout = 5\n"), "unknown key"),
        ('[[fast]]\nargv = ["pytest"]\n', "name must be"),
        ('[[fast]]\nname = "x\\ny"\nargv = ["pytest"]\n', "name must be"),
        (f'[[fast]]\nname = "{"n" * 61}"\nargv = ["pytest"]\n', "name must be"),
        ("[verify]\nx = 1\n", "unknown top-level"),
        ('[fast]\nname = "x"\nargv = ["pytest"]\n', "array of tables"),
        ("[[fast", "not valid TOML"),
        ("fast = 5\n", "array of tables"),
    ],
)
def test_load_rejects_unsafe_or_malformed_config(tmp_path, config, fragment):
    write_raw(tmp_path, config)
    with pytest.raises(VerifyConfigError) as exc:
        verify.load(tmp_path)
    assert fragment in str(exc.value)
    assert isinstance(exc.value, ValueError)


def test_load_rejects_binary_and_oversized_files(tmp_path):
    write_raw(tmp_path, b"\xff\xfe\x00\x00 not utf-8")
    with pytest.raises(VerifyConfigError, match="not valid TOML"):
        verify.load(tmp_path)
    write_raw(tmp_path, "# " + "x" * (verify.MAX_CONFIG_BYTES + 10) + "\n")
    with pytest.raises(VerifyConfigError, match="larger than"):
        verify.load(tmp_path)


def test_load_check_count_limit_is_per_tier(tmp_path):
    twelve = "".join(f'[[fast]]\nname = "c{i}"\nargv = ["x"]\n' for i in range(12))
    write_raw(tmp_path, twelve)
    assert len(verify.load(tmp_path).fast) == 12
    write_raw(tmp_path, twelve + '[[fast]]\nname = "c12"\nargv = ["x"]\n')
    with pytest.raises(VerifyConfigError, match="at most 12 checks per tier"):
        verify.load(tmp_path)
    both = twelve + twelve.replace("fast", "full")
    write_raw(tmp_path, both)
    r = verify.load(tmp_path)
    assert len(r.fast) == 12 and len(r.full) == 12


def test_load_accepts_paths_with_spaces_and_defaults(tmp_path):
    cfg = (
        '[[fast]]\nname = "a"\nargv = ["C:\\\\Program Files (x86)\\\\Python\\\\python.exe", "-m", "pytest"]\n'
        '[[fast]]\nname = "b"\nargv = ["/usr/local/my tools/bin/pytest"]\ncwd = "not/created/yet"\n'
    )
    write_raw(tmp_path, cfg)
    r = verify.load(tmp_path)
    assert r.fast[0].argv[0] == "C:\\Program Files (x86)\\Python\\python.exe"
    assert r.fast[0].timeout_s == 120 and r.fast[0].cwd == "."
    assert r.fast[1].cwd == "not/created/yet" and r.full == []


def test_load_accepts_utf8_bom(tmp_path):
    write_raw(tmp_path, b"\xef\xbb\xbf" + one().encode("utf-8"))
    assert verify.load(tmp_path).fast[0].argv == ["pytest", "-q"]


def test_load_rejects_cwd_that_resolves_outside_through_a_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    make_dir_link(repo / "link", outside)
    write_raw(repo, one(extra='cwd = "link"\n'))
    with pytest.raises(VerifyConfigError, match="outside the repository"):
        verify.load(repo)
    result = verify.run(repo, [Check("x", [PY, "-c", "print(1)"], 5, "link")])
    assert result.results[0].rc == 126 and "refused" in result.results[0].output


# =================================================================================================================
# trust
# =================================================================================================================
def test_trust_lifecycle(tmp_path, state):
    repo = make_repo(tmp_path, files={"a.txt": "a\n"})
    assert verify.trust_state(repo, state) == "no-config"
    assert not verify.is_trusted(repo, state)
    write_cfg(repo, [pycheck("a", "pass")])
    assert verify.trust_state(repo, state) == "untrusted" and not verify.is_trusted(repo, state)

    digest = verify.trust(repo, state)
    import hashlib

    assert digest == hashlib.sha256(verify.config_path(repo).read_bytes()).hexdigest()
    assert verify.is_trusted(repo, state) and verify.trust_state(repo, state) == "trusted"
    stored = json.loads((state / "verify-trust.json").read_text(encoding="utf-8"))
    assert list(stored.values()) == [digest]

    with open(verify.config_path(repo), "a", encoding="utf-8", newline="\n") as fh:
        fh.write("# one more comment\n")
    assert not verify.is_trusted(repo, state) and verify.trust_state(repo, state) == "modified"
    assert verify.trust(repo, state) != digest and verify.is_trusted(repo, state)

    assert verify.untrust(repo, state) is True
    assert verify.trust_state(repo, state) == "untrusted"
    assert verify.untrust(repo, state) is False


def test_trust_is_per_repository(tmp_path, state):
    a = make_repo(tmp_path, "a")
    b = make_repo(tmp_path, "b")
    for repo in (a, b):
        write_cfg(repo, [pycheck("same", "pass")])
    verify.trust(a, state)
    assert verify.is_trusted(a, state) and not verify.is_trusted(b, state)
    verify.trust(b, state)
    assert verify.untrust(a, state) and verify.is_trusted(b, state)


def test_trust_refuses_missing_or_invalid_config(tmp_path, state):
    repo = make_repo(tmp_path)
    with pytest.raises(VerifyConfigError, match="no .* to trust"):
        verify.trust(repo, state)
    write_raw(repo, one('["a && b"]'))
    with pytest.raises(VerifyConfigError):
        verify.trust(repo, state)
    assert verify.trust_state(repo, state) == "invalid"
    assert not (state / "verify-trust.json").exists()


def test_trust_survives_a_corrupt_state_file(tmp_path, state):
    repo = make_repo(tmp_path)
    write_cfg(repo, [pycheck("a", "pass")])
    (state / "verify-trust.json").write_text("{broken", encoding="utf-8")
    assert not verify.is_trusted(repo, state)
    verify.trust(repo, state)
    assert verify.is_trusted(repo, state)
    (state / "verify-trust.json").write_text("[1, 2]", encoding="utf-8")
    assert not verify.is_trusted(repo, state)


def test_trust_state_is_bounded(tmp_path, state, monkeypatch):
    repo = make_repo(tmp_path)
    write_cfg(repo, [pycheck("a", "pass")])
    with monkeypatch.context() as m:
        m.setattr(verify, "MAX_TRUSTED_REPOS", 3)
        (state / "verify-trust.json").write_text(
            json.dumps({f"r{i}": "h" for i in range(5)}), encoding="utf-8"
        )
        verify.trust(repo, state)
    stored = json.loads((state / "verify-trust.json").read_text(encoding="utf-8"))
    assert len(stored) == 3 and verify._repo_key(repo) in stored and "r0" not in stored


def test_repo_key_is_canonical(tmp_path):
    repo = make_repo(tmp_path, "My Repo")
    assert verify._repo_key(repo) == verify._repo_key(repo / ".")
    assert verify._repo_key(repo / "..") != verify._repo_key(repo)
    if os.name == "nt":
        assert verify._repo_key(repo) == verify._repo_key(repo).lower()


# =================================================================================================================
# run
# =================================================================================================================
def test_run_reports_exit_codes_and_output(tmp_path):
    res = verify.run(
        tmp_path,
        [
            pycheck("ok", "print('fine')"),
            pycheck(
                "bad", "import sys; print('boom'); sys.stderr.write('err-line\\n'); sys.exit(3)"
            ),
        ],
    )
    assert [r.rc for r in res.results] == [0, 3]
    assert res.results[0].output == "fine" and res.results[0].ok
    assert "boom" in res.results[1].output and "err-line" in res.results[1].output
    assert res.failed is res.results[1] and not res.ok and res.skipped == []


def test_run_stop_on_fail_skips_the_rest(tmp_path):
    marker = tmp_path / "marker"
    checks = [
        pycheck("first", "import sys; sys.exit(1)"),
        pycheck("second", f"open({str(marker)!r}, 'w').write('x')"),
    ]
    res = verify.run(tmp_path, checks, stop_on_fail=True)
    assert len(res.results) == 1 and res.skipped == ["second"] and not marker.exists()
    res = verify.run(tmp_path, checks)  # the default runs everything
    assert len(res.results) == 2 and marker.exists() and res.skipped == []


def test_run_all_green_is_ok(tmp_path):
    res = verify.run(tmp_path, [pycheck("a", "pass"), pycheck("b", "pass")])
    assert res.ok and res.failed is None and res.seconds >= 0


def test_run_missing_executable_is_a_result_not_an_exception(tmp_path):
    res = verify.run(tmp_path, [Check("tool", ["definitely-not-a-real-tool-xyz", "--flag"], 5)])
    r = res.results[0]
    assert r.rc == 127 and "not found" in r.output and r.launch_failed and not r.timed_out


def test_run_refuses_invalid_checks_without_raising(tmp_path):
    for bad in (
        Check("a", []),
        Check("b", ["x", ""]),
        Check("c", [PY, "-c", "pass"], 5, "missing-dir"),
        Check("d", [PY], 5, ".."),
    ):
        r = verify.run(tmp_path, [bad]).results[0]
        assert r.rc == 126 and r.launch_failed, bad


def test_run_uses_the_requested_cwd_inside_the_repo(tmp_path):
    (tmp_path / "pkg" / "sub dir").mkdir(parents=True)
    r = verify.run(
        tmp_path, [pycheck("cwd", "import os; print(os.getcwd())", cwd="pkg/sub dir")]
    ).results[0]
    assert Path(r.output).resolve() == (tmp_path / "pkg" / "sub dir").resolve()


def test_run_paths_with_spaces(tmp_path):
    repo = tmp_path / "my repo"
    script = put(
        repo,
        "dir with space/check script.py",
        "import sys, os\nprint('ran in', os.path.basename(os.getcwd()), sys.argv[1])\n",
    )
    r = verify.run(repo, [Check("spaced", [PY, str(script), "arg with space"], 30)]).results[0]
    assert r.rc == 0 and r.output == "ran in my repo arg with space"


def test_run_bare_names_are_not_resolved_in_the_repo(tmp_path):
    """A repo must not be able to shadow a tool name with a file next to its config."""
    if os.name == "nt":
        tool = tmp_path / "plant.cmd"
        tool.write_bytes(b"@echo off\r\necho planted\r\n")
    else:
        tool = tmp_path / "plant"
        tool.write_text("#!/bin/sh\necho planted\n", encoding="utf-8")
        tool.chmod(0o755)
    assert verify.run(tmp_path, [Check("bare", ["plant"], 10)]).results[0].rc == 127
    explicit = verify.run(tmp_path, [Check("explicit", ["./plant"], 10)]).results[0]
    assert explicit.rc == 0 and "planted" in explicit.output


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe re-parses arguments of batch files")
def test_run_batch_files_refuse_arguments_cmd_would_interpret(tmp_path):
    bat = tmp_path / "echo arg.cmd"
    bat.write_bytes(b"@echo off\r\necho ARG=%1\r\n")
    ok = verify.run(tmp_path, [Check("ok", [str(bat), "hello"], 10)]).results[0]
    assert ok.rc == 0 and "ARG=hello" in ok.output
    for evil in ("a&calc", "a|b", "a>b", "%PATH%", 'a"b', "a^b"):
        r = verify.run(tmp_path, [Check("evil", [str(bat), evil], 10)]).results[0]
        assert r.rc == 126 and "cmd.exe" in r.output, evil


def test_run_stdin_is_closed(tmp_path):
    r = verify.run(
        tmp_path, [pycheck("stdin", "import sys; print(repr(sys.stdin.read()))", timeout=20)]
    ).results[0]
    assert r.rc == 0 and r.output == "''"


def test_run_timeout_kills_the_whole_process_tree(tmp_path):
    spawner = put(
        tmp_path,
        "spawner.py",
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "open(sys.argv[1], 'w').write(str(p.pid))\n"
        "time.sleep(120)\n",
    )
    pidfile = tmp_path / "grandchild.pid"
    t0 = time.monotonic()
    # the limit only has to be long enough for two interpreters to start on a slow runner
    res = verify.run(tmp_path, [Check("tree", [PY, str(spawner), str(pidfile)], 5)])
    elapsed = time.monotonic() - t0
    r = res.results[0]
    assert r.timed_out and r.rc == 124 and not r.clamped and not r.ok
    assert elapsed < 30
    grandchild = int(pidfile.read_text())
    try:
        deadline = time.monotonic() + 10
        while alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not alive(grandchild), "the grandchild survived the timeout"
    finally:
        kill_pid(grandchild)


def test_run_does_not_hang_on_a_grandchild_that_keeps_the_pipe_open(tmp_path):
    spawner = put(
        tmp_path,
        "daemonize.py",
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(sys.argv[1], 'w').write(str(p.pid))\n"
        "print('parent done')\n",
    )
    pidfile = tmp_path / "daemon.pid"
    t0 = time.monotonic()
    try:
        r = verify.run(tmp_path, [Check("daemon", [PY, str(spawner), str(pidfile)], 30)]).results[0]
        assert time.monotonic() - t0 < 15
        assert r.rc == 0 and r.output == "parent done"
    finally:
        if pidfile.exists():
            kill_pid(int(pidfile.read_text()))


def test_run_total_budget_skips_and_flags_inconclusive_timeouts(tmp_path):
    res = verify.run(
        tmp_path,
        [pycheck("slow", "import time; time.sleep(60)", 60), pycheck("later", "pass")],
        total_timeout_s=1,
    )
    assert res.results[0].timed_out and res.results[0].clamped
    assert res.skipped == ["later"] and not res.ok
    own = verify.run(
        tmp_path, [pycheck("slow", "import time; time.sleep(60)", 1)], total_timeout_s=100
    )
    assert own.results[0].timed_out and not own.results[0].clamped


def test_run_exhausted_budget_starts_nothing(tmp_path):
    marker = tmp_path / "marker"
    res = verify.run(tmp_path, [pycheck("a", f"open({str(marker)!r}, 'w')")], total_timeout_s=0)
    assert res.results == [] and res.skipped == ["a"] and not marker.exists()


def test_run_output_tail_is_capped(tmp_path):
    r = verify.run(
        tmp_path, [pycheck("many", "for i in range(500): print('line %d' % i)")]
    ).results[0]
    lines = r.output.split("\n")
    assert len(lines) <= verify.TAIL_LINES and lines[-1] == "line 499"
    assert len(r.output.encode()) <= verify.TAIL_BYTES
    long = verify.run(tmp_path, [pycheck("long", "print('a' * 5000 + 'END')")]).results[0]
    assert len(long.output.encode()) <= verify.TAIL_BYTES and long.output.endswith("aaaEND")


def test_run_huge_output_is_streamed_not_buffered(tmp_path):
    code = "import sys\nchunk = 'y' * 1023 + '\\n'\nfor _ in range(6000):\n    sys.stdout.write(chunk)\nprint('END')\n"
    t0 = time.monotonic()
    r = verify.run(tmp_path, [pycheck("huge", code, 60)]).results[0]
    assert r.rc == 0 and r.output.endswith("END") and len(r.output.encode()) <= verify.TAIL_BYTES
    assert time.monotonic() - t0 < 30


def test_run_output_is_cleaned(tmp_path):
    code = (
        "import sys\n"
        "sys.stdout.write('\\x1b[31mred\\x1b[0m \\x1b]0;title\\x07plain\\n')\n"
        "sys.stdout.write('10%\\r50%\\r100%\\n')\n"
        "sys.stdout.write('bell\\x07ctl\\x00\\n')\n"
        "sys.stdout.buffer.write(b'bad \\xff\\xfe bytes\\n')\n"
    )
    out = verify.run(tmp_path, [pycheck("clean", code)]).results[0].output
    assert "red plain" in out and "\x1b" not in out and "title" not in out
    assert "100%" in out and "10%" not in out and "50%" not in out
    assert "bellctl" in out and "\x07" not in out and "\x00" not in out
    assert "bad \ufffd\ufffd bytes" in out


def test_tail_text_boundaries():
    exact = "\n".join(f"l{i}" for i in range(40)).encode()
    assert verify._tail_text(exact).split("\n") == [f"l{i}" for i in range(40)]
    more = "\n".join(f"l{i}" for i in range(41)).encode()
    assert verify._tail_text(more).split("\n")[0] == "l1"
    wide = verify._tail_text(
        ("\u00e9" * 1000 + "a").encode()
    )  # 2 bytes per char: the 1500-byte cut lands mid-character
    assert (
        len(wide.encode()) <= verify.TAIL_BYTES
        and "\ufffd" not in wide
        and set(wide) == {"\u00e9", "a"}
    )
    assert verify._tail_text(b"") == "" and verify._tail_text(b"\n\n\n") == ""
    kept = verify._tail_text(("x" * 1400 + "\n" + "y" * 200).encode())
    assert kept == "y" * 200  # the partial first line is dropped, the whole last line kept


def test_run_environment_is_scrubbed(tmp_path, monkeypatch):
    secrets = {
        "MY_API_TOKEN": "t",
        "ANTHROPIC_API_KEY": "a",
        "ANTHROPIC_BASE_URL": "http://x",
        "DB_PASSWORD": "p",
        "AWS_SECRET_ACCESS_KEY": "s",
        "SOME_CREDENTIALS": "c",
        "GITHUB_TOKEN": "g",
        "SSH_AUTH_SOCK": "/tmp/agent",
    }
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HARMLESS_SETTING", "keep-me")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("CI", "false")
    probe = [*secrets, "HARMLESS_SETTING", "LC_ALL", "CI", verify.ACTIVE_ENV]
    code = (  # PATH is reported as a bool: its value alone would overflow the output tail
        "import os, json; "
        f"out = {{k: os.environ.get(k) for k in {probe!r}}}; "
        "out['PATH'] = bool(os.environ.get('PATH')); print(json.dumps(out))"
    )
    r = verify.run(tmp_path, [pycheck("env", code)]).results[0]
    seen = json.loads(r.output)
    assert all(seen[k] is None for k in secrets), seen
    assert seen["HARMLESS_SETTING"] == "keep-me" and seen["LC_ALL"] == "C" and seen["PATH"] is True
    assert seen["CI"] == "1" and seen[verify.ACTIVE_ENV] == "1"


def test_scrubbed_env_is_a_copy_and_honours_the_keep_list():
    base = {
        "PATH": "/bin",
        "HOME": "/h",
        "USERPROFILE": "C:\\u",
        "TEMP": "t",
        "SYSTEMROOT": "C:\\W",
        "Api_Key": "k",
        "x_secret": "s",
        "LANG": "en",
    }
    env = verify.scrubbed_env(base)
    assert set(env) == {
        "PATH",
        "HOME",
        "USERPROFILE",
        "TEMP",
        "SYSTEMROOT",
        "LANG",
        "CI",
        verify.ACTIVE_ENV,
    }
    assert "Api_Key" in base  # the input is untouched


def test_command_line_display_quotes_spaces():
    assert verify.command_line(Check("x", ["my tool", "a b", "c"])) == "'my tool' 'a b' c"


# =================================================================================================================
# fingerprint
# =================================================================================================================
def test_fingerprint_is_stable_and_tracks_changes(tmp_path):
    repo = make_repo(tmp_path, files={"a.txt": "one\n", "b.txt": "two\n"})
    base = verify.fingerprint(repo)
    assert len(base) == 40 and verify.fingerprint(repo) == base

    (repo / "a.txt").write_text("one changed\n", encoding="utf-8", newline="\n")
    edited = verify.fingerprint(repo)
    assert edited != base

    (repo / "a.txt").write_text("one\n", encoding="utf-8", newline="\n")
    assert verify.fingerprint(repo) == base  # back to the same tree, same value

    put(repo, "new.txt", "untracked\n")
    added = verify.fingerprint(repo)
    assert added != base
    put(repo, "new.txt", "untracked and longer\n")
    assert verify.fingerprint(repo) != added  # (size, mtime) of untracked files counts

    (repo / "new.txt").unlink()
    assert verify.fingerprint(repo) == base


def test_fingerprint_ignores_rewrites_that_change_nothing(tmp_path):
    repo = make_repo(tmp_path, files={"a.txt": "one\n"})
    before = verify.fingerprint(repo)
    (repo / "a.txt").write_text("one\n", encoding="utf-8", newline="\n")
    os.utime(repo / "a.txt", (1_000_000_000, 1_000_000_000))
    assert verify.fingerprint(repo) == before


def test_fingerprint_moves_on_commit_and_staging(tmp_path):
    repo = make_repo(tmp_path, files={"a.txt": "one\n"})
    base = verify.fingerprint(repo)
    put(repo, "a.txt", "two\n")
    unstaged = verify.fingerprint(repo)
    git(repo, "add", "a.txt")
    staged = verify.fingerprint(repo)
    assert len({base, unstaged, staged}) == 3
    commit(repo, "second")
    assert verify.fingerprint(repo) not in {base, unstaged, staged}


def test_fingerprint_without_git_or_commits(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert verify.fingerprint(plain) == ""
    assert verify.fingerprint(tmp_path / "missing") == ""
    unborn = make_repo(tmp_path, "unborn", files={"a.txt": "x\n"}, do_commit=False)
    first = verify.fingerprint(unborn)
    assert first and verify.fingerprint(unborn) == first
    put(unborn, "b.txt", "y\n")
    assert verify.fingerprint(unborn) != first


def test_fingerprint_from_a_subdirectory_and_odd_names(tmp_path):
    repo = make_repo(tmp_path, "my repo", files={"sub/a.txt": "x\n"})
    put(repo, "sub/new file \u00e9.txt", "untracked\n")
    top = verify.fingerprint(repo)
    assert verify.fingerprint(repo / "sub") == top
    put(repo, "sub/new file \u00e9.txt", "untracked, now longer\n")
    assert verify.fingerprint(repo / "sub") != top


def test_fingerprint_oversize_diff_still_sees_later_changes(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, files={"a.txt": "A" * 400 + "\n", "b.txt": "B" * 400 + "\n"})
    put(repo, "a.txt", "a" * 400 + "\n")
    put(repo, "b.txt", "b" * 400 + "\n")
    with monkeypatch.context() as m:
        m.setattr(verify, "_DIFF_CAP", 200)  # only a.txt's diff header fits under the cap
        first = verify.fingerprint(repo)
        put(repo, "b.txt", "c" * 400 + "\n")  # same size, beyond the cap
        os.utime(repo / "b.txt", (2_000_000_000, 2_000_000_000))
        assert verify.fingerprint(repo) != first


def test_fingerprint_never_raises(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(verify, "_git", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert verify.fingerprint(repo) == ""


# =================================================================================================================
# test_weakening
# =================================================================================================================
BASE_PY = "def test_x():\n    assert f(1) == 1\n    assert f(2) == 4\n    assert f(3) == 9\n"
BASE_JS = "it('a', () => {\n  expect(f(1)).toBe(1)\n})\n"


def weak(tmp_path, base, changed=(), deleted=(), moved=(), name="repo"):
    repo = make_repo(tmp_path, name, base)
    for rel, text in dict(changed).items():
        put(repo, rel, text)
    for rel in deleted:
        (repo / rel).unlink()
    for src, dst in moved:
        (repo / dst).parent.mkdir(parents=True, exist_ok=True)
        git(repo, "mv", src, dst)
    return verify.test_weakening(repo)


def test_weakening_clean_repo_and_non_git(tmp_path):
    assert weak(tmp_path, {"tests/test_a.py": BASE_PY}) == []
    plain = tmp_path / "plain"
    plain.mkdir()
    assert verify.test_weakening(plain) == []
    unborn = make_repo(tmp_path, "unborn", {"tests/test_a.py": BASE_PY}, do_commit=False)
    assert verify.test_weakening(unborn) == []


def test_weakening_removed_assertion_without_replacement(tmp_path):
    new = "def test_x():\n    assert f(1) == 1\n    assert f(2) == 4\n"
    assert weak(tmp_path, {"tests/test_a.py": BASE_PY}, {"tests/test_a.py": new}) == [
        "tests/test_a.py: 1 assertion line(s) removed without a replacement"
    ]


def test_weakening_changed_assertion_is_not_weakening(tmp_path):
    edited = BASE_PY.replace("== 9", "== 10")
    assert weak(tmp_path, {"tests/test_a.py": BASE_PY}, {"tests/test_a.py": edited}) == []
    moved_out = "def test_x():\n    assert f(1) == 1\n\n\ndef test_y():\n    assert f(2) == 4\n    assert f(3) == 9\n"
    assert (
        weak(tmp_path, {"tests/test_a.py": BASE_PY}, {"tests/test_a.py": moved_out}, name="r2")
        == []
    )


@pytest.mark.parametrize(
    ("path", "base", "new", "marker"),
    [
        (
            "tests/test_a.py",
            BASE_PY,
            "@pytest.mark.skip(reason='later')\n" + BASE_PY,
            "@pytest.mark.skip",
        ),
        ("tests/test_a.py", BASE_PY, "@pytest.mark.xfail\n" + BASE_PY, "@pytest.mark.xfail"),
        (
            "tests/test_a.py",
            BASE_PY,
            "@pytest.mark.skipif(True, reason='x')\n" + BASE_PY,
            "@pytest.mark.skipif",
        ),
        (
            "tests/test_a.py",
            BASE_PY,
            BASE_PY.replace("    assert f(1)", "    pytest.skip('no')\n    assert f(1)"),
            "pytest.skip(",
        ),
        ("tests/test_a.py", BASE_PY, "@unittest.skip('x')\n" + BASE_PY, "@unittest.skip"),
        ("src/foo.test.js", BASE_JS, BASE_JS.replace("it(", "it.skip("), "it.skip"),
        (
            "__tests__/foo.js",
            "describe('d', () => {})\n",
            "describe.skip('d', () => {})\n",
            "describe.skip",
        ),
        ("spec/foo.spec.ts", BASE_JS, BASE_JS.replace("it(", "xit("), "xit"),
        (
            "spec/foo.spec.ts",
            "describe('d', () => {})\n",
            "xdescribe('d', () => {})\n",
            "xdescribe",
        ),
        ("web/app.test.tsx", BASE_JS, BASE_JS.replace("it(", "test.skip("), "test.skip"),
        ("web/app.test.tsx", BASE_JS, BASE_JS.replace("it(", "it.only("), "it.only("),
        (
            "pkg/foo_test.go",
            "package p\n\nfunc TestA(t *testing.T) {\n}\n",
            'package p\n\nfunc TestA(t *testing.T) {\n\tt.Skip("flaky")\n}\n',
            "t.Skip(",
        ),
        (
            "src/test/java/FooTest.java",
            "class FooTest {\n  @Test void a() {}\n}\n",
            "class FooTest {\n  @Disabled\n  @Test void a() {}\n}\n",
            "@Disabled",
        ),
        (
            "src/test/java/FooTest.java",
            "class FooTest {\n  @Test void a() {}\n}\n",
            "class FooTest {\n  @Ignore\n  @Test void a() {}\n}\n",
            "@Ignore",
        ),
        ("tests/it.rs", "#[test]\nfn a() {}\n", "#[test]\n#[ignore]\nfn a() {}\n", "#[ignore"),
        ("test/foo_spec.rb", "it 'a' do\nend\n", "xit 'a' do\nend\n", "xit"),
    ],
)
def test_weakening_newly_added_skip_markers(tmp_path, path, base, new, marker):
    out = weak(tmp_path, {path: base}, {path: new})
    assert (
        len(out) == 1 and out[0].startswith(f"{path}: skip/xfail added (") and marker in out[0]
    ), out


def test_weakening_a_moved_skip_marker_is_not_new(tmp_path):
    base = "@pytest.mark.skip\ndef test_a():\n    assert 1\n\n\ndef test_b():\n    assert 1\n"
    new = "def test_a():\n    assert 1\n\n\n@pytest.mark.skip\ndef test_b():\n    assert 1\n"
    assert weak(tmp_path, {"tests/test_a.py": base}, {"tests/test_a.py": new}) == []


def test_weakening_counts_several_markers(tmp_path):
    new = "@pytest.mark.skip\n" + BASE_PY + "\n\n@pytest.mark.xfail\ndef test_y():\n    pass\n"
    out = weak(tmp_path, {"tests/test_a.py": BASE_PY}, {"tests/test_a.py": new})
    assert out == ["tests/test_a.py: skip/xfail added (@pytest.mark.skip +1 more)"]


@pytest.mark.parametrize(
    ("path", "before", "after", "word"),
    [
        (
            "tests/test_a.py",
            "assert x == pytest.approx(1.0, abs=1e-6)\n",
            "assert x == pytest.approx(1.0, abs=1e-2)\n",
            "abs",
        ),
        (
            "tests/test_a.py",
            "assert x == pytest.approx(1.0, rel=0.001)\n",
            "assert x == pytest.approx(1.0, rel=0.1)\n",
            "rel",
        ),
        (
            "tests/test_a.py",
            "assert x == pytest.approx(1.0, 0.001)\n",
            "assert x == pytest.approx(1.0, 0.5)\n",
            "approx",
        ),
        ("tests/test_a.py", "@pytest.mark.timeout(5)\n", "@pytest.mark.timeout(60)\n", "timeout"),
        (
            "tests/test_a.py",
            "client.get(url, timeout=5)\n",
            "client.get(url, timeout=120)\n",
            "timeout",
        ),
        (
            "tests/test_a.py",
            "self.assertAlmostEqual(a, b, places=7)\n",
            "self.assertAlmostEqual(a, b, places=3)\n",
            "places",
        ),
        (
            "tests/test_a.py",
            "self.assertAlmostEqual(a, b, delta=0.01)\n",
            "self.assertAlmostEqual(a, b, delta=1)\n",
            "delta",
        ),
        (
            "src/a.test.js",
            "expect(x).toBeCloseTo(0.3, 5)\n",
            "expect(x).toBeCloseTo(0.3, 2)\n",
            "tobecloseto",
        ),
        ("src/a.test.js", "jest.setTimeout(5000)\n", "jest.setTimeout(30000)\n", "settimeout"),
    ],
)
def test_weakening_widened_tolerance(tmp_path, path, before, after, word):
    out = weak(tmp_path, {path: before}, {path: after})
    assert len(out) == 1 and "tolerance/timeout widened" in out[0] and word in out[0].lower(), out


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "assert x == pytest.approx(1.0, abs=1e-2)\n",
            "assert x == pytest.approx(1.0, abs=1e-6)\n",
        ),
        (
            "assert x == pytest.approx(1.0, abs=1e-6)\n",
            "assert x == pytest.approx(2.0, abs=1e-6)\n",
        ),
        ("self.assertAlmostEqual(a, b, places=3)\n", "self.assertAlmostEqual(a, b, places=7)\n"),
        ("client.get(url, timeout=60)\n", "client.get(url, timeout=5)\n"),
        ("x = compute(7)\n", "x = compute(70)\n"),
    ],
)
def test_weakening_tighter_or_unrelated_number_changes_are_fine(tmp_path, before, after):
    assert weak(tmp_path, {"tests/test_a.py": before}, {"tests/test_a.py": after}) == []


def test_weakening_deleted_and_renamed_test_files(tmp_path):
    base = {"tests/test_old.py": BASE_PY, "tests/test_keep.py": BASE_PY, "src/mod.py": "x = 1\n"}
    assert weak(tmp_path, base, deleted=["tests/test_old.py"]) == [
        "tests/test_old.py: test file deleted"
    ]
    assert (
        weak(tmp_path, base, moved=[("tests/test_old.py", "tests/test_renamed.py")], name="r2")
        == []
    )
    assert weak(tmp_path, base, deleted=["src/mod.py"], name="r3") == []


def test_weakening_suppression_comments_in_tests(tmp_path):
    base = {"tests/test_a.py": "import os\nx = f()  # noqa: E501\n"}
    out = weak(
        tmp_path, base, {"tests/test_a.py": "import os  # noqa: F401\nx = f()  # noqa: E501\n"}
    )
    assert out == ["tests/test_a.py: lint/type suppression added (# noqa)"]
    out = weak(
        tmp_path,
        base,
        {"tests/test_a.py": "import os\nx = f()  # type: ignore[arg-type]\n"},
        name="r2",
    )
    assert out == []  # one suppression replaced by another: net zero
    out = weak(
        tmp_path,
        {"tests/test_a.py": "x = f()\n"},
        {"tests/test_a.py": "x = f()  # type: ignore\n"},
        name="r3",
    )
    assert out == ["tests/test_a.py: lint/type suppression added (# type: ignore)"]
    out = weak(
        tmp_path,
        {"web/a.test.ts": "const x = f()\n"},
        {"web/a.test.ts": "// @ts-ignore\nconst x = f()\n"},
        name="r4",
    )
    assert out and "suppression" in out[0]


def test_weakening_ignores_non_test_files_and_non_code_files(tmp_path):
    base = {
        "src/app.py": BASE_PY,
        "tests/data/fixture.json": '{"a": 1}\n',
        "tests/notes.md": "assert nothing\n",
        "docs/spec/readme.md": "@pytest.mark.skip\n",
    }
    changed = {
        "src/app.py": "@pytest.mark.skip\ndef f():\n    pass  # noqa\n",
        "tests/data/fixture.json": "{}\n",
        "tests/notes.md": "@pytest.mark.skip\n",
    }
    assert weak(tmp_path, base, changed, deleted=["docs/spec/readme.md"]) == []


def test_weakening_comment_only_changes_are_not_assertions(tmp_path):
    base = {"tests/test_a.py": "def test_x():\n    # assert old == new\n    assert f() == 1\n"}
    assert weak(tmp_path, base, {"tests/test_a.py": "def test_x():\n    assert f() == 1\n"}) == []
    new = "def test_x():\n    assert f() == 1\n    # @pytest.mark.skip\n"
    assert weak(tmp_path, base, {"tests/test_a.py": new}, name="r2") == []


def test_weakening_new_untracked_test_files_are_not_scanned(tmp_path):
    repo = make_repo(tmp_path)
    put(repo, "tests/test_new.py", "@pytest.mark.skip\ndef test_a():\n    pass\n")
    assert verify.test_weakening(repo) == []
    git(repo, "add", "tests/test_new.py")  # staged files are part of `diff HEAD`
    assert verify.test_weakening(repo)[0].startswith("tests/test_new.py: skip/xfail added")


def test_weakening_is_capped_ranked_and_short(tmp_path):
    base = {f"tests/test_{i}.py": BASE_PY for i in range(8)}
    base["tests/test_gone.py"] = BASE_PY
    changed = {f"tests/test_{i}.py": "@pytest.mark.skip\n" + BASE_PY for i in range(8)}
    out = weak(tmp_path, base, changed, deleted=["tests/test_gone.py"])
    assert len(out) == verify._ADVISORY_CAP == 5
    assert out[0] == "tests/test_gone.py: test file deleted"  # most serious first
    assert all(len(s) < 120 for s in out)


def test_weakening_several_findings_in_one_file_are_ordered(tmp_path):
    new = "@pytest.mark.skip\ndef test_x():\n    assert f(1) == 1\n"
    out = weak(tmp_path, {"tests/test_a.py": BASE_PY}, {"tests/test_a.py": new})
    assert [s.split(": ", 1)[1].split(" ")[0] for s in out] == ["skip/xfail", "2"]


def test_weakening_paths_with_spaces(tmp_path):
    base = {"tests/my test.py": BASE_PY, "tests/sub dir/other file.py": BASE_PY}
    changed = {
        "tests/my test.py": "@pytest.mark.skip\n" + BASE_PY,
        "tests/sub dir/other file.py": "def test_x():\n    pass\n",
    }
    out = weak(tmp_path, base, changed)
    assert any(s.startswith("tests/my test.py: skip/xfail added") for s in out)
    assert any(
        s.startswith("tests/sub dir/other file.py: 3 assertion line(s) removed") for s in out
    )


def test_weakening_diff_parser_trusts_headers_only_before_the_first_hunk():
    diff = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "index 111..222 100644\n"
        "--- a/tests/test_a.py\n"
        "+++ b/tests/test_a.py\n"
        "@@ -1 +1,2 @@\n"
        "+++ b/not/a/header.py\n"
        "--- a/also/not.py\n"
        "+    assert x\n"
    )
    files = verify._parse_diff(diff)
    assert [f.path for f in files] == ["tests/test_a.py"]
    assert files[0].added == ["++ b/not/a/header.py", "    assert x"] and files[0].removed == [
        "-- a/also/not.py"
    ]


def test_weakening_never_raises(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(verify, "_advisories", lambda text: (_ for _ in ()).throw(RuntimeError("boom")))
        assert verify.test_weakening(repo) == []


# =================================================================================================================
# gate
# =================================================================================================================
def test_gate_skips_when_untrusted_or_unconfigured(tmp_path, state):
    marker = tmp_path / "marker"
    repo = make_repo(tmp_path, files={"code.txt": "ok\n"}, do_commit=False)
    out = verify.gate(repo, state)
    assert out.action == "skip" and "no .claude/hr-verify.toml" in out.reason

    write_cfg(repo, [marker_check(marker)])
    commit(repo)
    out = verify.gate(repo, state)
    assert (
        out.action == "skip"
        and "not trusted" in out.reason
        and "claude-hr-ctl verify trust" in out.reason
    )
    assert runs(marker) == 0, "an untrusted config must never run"

    write_cfg(repo, [])
    verify.trust(repo, state)
    out = verify.gate(repo, state)
    assert out.action == "skip" and "no fast checks" in out.reason


def test_gate_pass_then_skip_while_unchanged(tmp_path, state):
    marker = tmp_path / "marker"
    repo = gated_repo(tmp_path, state, [marker_check(marker)])
    out = verify.gate(repo, state)
    assert out.action == "pass" and out.fingerprint and out.seconds > 0
    assert [r.name for r in out.results] == ["unit"] and out.results[0].ok
    assert runs(marker) == 1

    again = verify.gate(repo, state)
    assert again.action == "skip" and "unchanged since the last passing run" in again.reason
    assert again.fingerprint == out.fingerprint and runs(marker) == 1

    put(repo, "code.txt", "edited\n")
    assert verify.gate(repo, state).action == "block" and runs(marker) == 2
    stored = json.loads((state / "verify-gate.json").read_text(encoding="utf-8"))
    assert stored[verify._repo_key(repo)]["last_green"] == out.fingerprint, (
        "a failure keeps the last green tree"
    )


def test_gate_block_text_and_block_cap(tmp_path, state):
    marker = tmp_path / "marker"
    repo = gated_repo(tmp_path, state, [marker_check(marker, "unit")])
    put(repo, "code.txt", "broken\n")

    first = verify.gate(repo, state, max_blocks=2)
    assert first.action == "block"
    lines = first.reason.split("\n")
    assert lines[0] == "claude-hr verify: unit failed (exit 1)"
    assert "code.txt says broken" in first.reason
    assert runs(marker) == 1

    second = verify.gate(repo, state, max_blocks=2)
    assert second.action == "block" and runs(marker) == 2

    third = verify.gate(repo, state, max_blocks=2)
    assert third.action == "skip" and runs(marker) == 2, "the cap must stop re-running too"
    assert "block cap reached (2/2)" in third.reason
    assert "claude-hr verify: unit failed (exit 1)" in third.reason, "the failure stays visible"

    put(repo, "code.txt", "broken differently\n")  # a new change gets a fresh budget
    assert verify.gate(repo, state, max_blocks=2).action == "block" and runs(marker) == 3
    put(repo, "code.txt", "ok\n")
    assert verify.gate(repo, state, max_blocks=2).action == "pass"

    stored = json.loads((state / "verify-gate.json").read_text(encoding="utf-8"))
    entry = stored[verify._repo_key(repo)]
    assert entry["blocks"] == {} and "last_block" not in entry and entry["last_green"]


def test_gate_cap_of_one_and_disabled_blocking(tmp_path, state):
    marker = tmp_path / "marker"
    repo = gated_repo(tmp_path, state, [marker_check(marker)])
    put(repo, "code.txt", "nope\n")
    assert verify.gate(repo, state, max_blocks=1).action == "block"
    capped = verify.gate(repo, state, max_blocks=1)
    assert capped.action == "skip" and "1/1" in capped.reason
    off = verify.gate(repo, state, max_blocks=0)
    assert off.action == "skip" and "disabled" in off.reason and runs(marker) == 1


def test_gate_a_fix_unblocks_and_a_revert_to_green_is_remembered(tmp_path, state):
    marker = tmp_path / "marker"
    repo = gated_repo(tmp_path, state, [marker_check(marker)])
    assert verify.gate(repo, state).action == "pass"
    put(repo, "code.txt", "bad\n")
    assert verify.gate(repo, state).action == "block"
    put(repo, "code.txt", "ok\n")  # back to the committed tree = the fingerprint that was green
    out = verify.gate(repo, state)
    assert out.action == "skip" and "unchanged" in out.reason


def test_gate_modified_config_is_not_trusted_until_retrusted(tmp_path, state):
    marker = tmp_path / "marker"
    repo = gated_repo(tmp_path, state, [marker_check(marker)])
    with open(verify.config_path(repo), "a", encoding="utf-8", newline="\n") as fh:
        fh.write("# edited after trusting\n")
    out = verify.gate(repo, state)
    assert (
        out.action == "skip" and "changed since it was trusted" in out.reason and runs(marker) == 0
    )
    verify.trust(repo, state)
    assert verify.gate(repo, state).action == "pass" and runs(marker) == 1


def test_gate_trust_is_not_shared_between_repos_with_the_same_config(tmp_path, state):
    marker = tmp_path / "marker"
    trusted = gated_repo(tmp_path, state, [marker_check(marker)], name="trusted")
    other = make_repo(tmp_path, "other", {"code.txt": "ok\n"}, do_commit=False)
    write_cfg(other, [marker_check(marker)])
    commit(other)
    assert verify.gate(other, state).action == "skip" and runs(marker) == 0
    assert verify.gate(trusted, state).action == "pass" and runs(marker) == 1


def test_gate_stops_at_the_first_failing_check(tmp_path, state):
    marker = tmp_path / "marker"
    fast = [
        pycheck("lint", "import sys; print('lint complaint'); sys.exit(2)"),
        marker_check(marker, "unit"),
    ]
    repo = gated_repo(tmp_path, state, fast)
    out = verify.gate(repo, state)
    assert out.action == "block" and out.reason.startswith(
        "claude-hr verify: lint failed (exit 2)\nlint complaint"
    )
    assert runs(marker) == 0 and len(out.results) == 1


def test_gate_tiers(tmp_path, state):
    f_marker, full_marker = tmp_path / "f", tmp_path / "full"
    fast = [marker_check(f_marker, "fast-unit")]
    full = [marker_check(f_marker, "fast-unit"), marker_check(full_marker, "slow-build")]
    repo = gated_repo(tmp_path, state, fast, full)
    assert verify.gate(repo, state, tier="full").action == "pass"
    assert (runs(f_marker), runs(full_marker)) == (1, 1)
    bad = verify.gate(repo, state, tier="medium")
    assert bad.action == "error" and "unknown verify tier" in bad.reason

    only_fast = gated_repo(tmp_path, state, fast, None, name="only-fast")
    out = verify.gate(only_fast, state, tier="full")  # no [[full]] list: fall back to the fast one
    assert out.action == "pass" and [r.name for r in out.results] == ["fast-unit"]


def test_gate_missing_tool_is_an_error_not_a_block(tmp_path, state):
    repo = gated_repo(tmp_path, state, [Check("lint", ["definitely-not-a-real-tool-xyz"], 10)])
    out = verify.gate(repo, state)
    assert out.action == "error" and "cannot run lint" in out.reason and "not found" in out.reason
    assert not (state / "verify-gate.json").exists(), "a setup problem must not count as a block"


def test_gate_a_check_that_times_out_blocks(tmp_path, state):
    repo = gated_repo(tmp_path, state, [pycheck("hang", "import time; time.sleep(60)", timeout=1)])
    out = verify.gate(repo, state, timeout_s=30)
    assert out.action == "block" and out.reason.startswith(
        "claude-hr verify: hang failed (exit 124, timed out after "
    )
    assert out.results[0].timed_out


def test_gate_budget_exhaustion_is_inconclusive_not_a_failure(tmp_path, state):
    repo = gated_repo(tmp_path, state, [pycheck("slow", "import time; time.sleep(60)", timeout=60)])
    out = verify.gate(repo, state, timeout_s=1)
    assert out.action == "skip" and "inconclusive" in out.reason and out.fingerprint
    assert not (state / "verify-gate.json").exists()

    two = gated_repo(
        tmp_path, state, [pycheck("quick", "pass"), pycheck("never", "pass")], name="two"
    )
    out = verify.gate(two, state, timeout_s=0)
    assert out.action == "skip" and "time budget" in out.reason
    assert not (state / "verify-gate.json").exists(), (
        "a partial run must never be recorded as green"
    )


def test_gate_appends_test_weakening_advisories_to_a_block(tmp_path, state):
    files = {"tests/test_core.py": BASE_PY, "src/core.py": "def f(x):\n    return x * x\n"}
    repo = gated_repo(
        tmp_path,
        state,
        [pycheck("unit", "import sys; print('1 failed'); sys.exit(1)")],
        files=files,
    )
    put(repo, "tests/test_core.py", "def test_x():\n    assert f(1) == 1\n")
    out = verify.gate(repo, state)
    lines = out.reason.split("\n")
    assert lines[0] == "claude-hr verify: unit failed (exit 1)" and lines[1] == "1 failed"
    assert (
        lines[2]
        == "Possible test weakening: tests/test_core.py: 2 assertion line(s) removed without a replacement"
    )
    quiet = verify.gate(repo, state, test_guard=False, max_blocks=3)
    assert quiet.action == "block" and "Possible test weakening" not in quiet.reason


def test_gate_paths_with_spaces_and_a_check_cwd(tmp_path, state):
    script = put(
        tmp_path,
        "tool dir/check script.py",
        "import os, sys\nsys.exit(0 if os.path.basename(os.getcwd()) == 'web app' else 5)\n",
    )
    repo = gated_repo(
        tmp_path,
        state,
        [Check("spaced", [PY, str(script)], 30, "web app")],
        files={"web app/index.txt": "x\n"},
        name="my repo",
    )
    out = verify.gate(repo, state)
    assert out.action == "pass", out.reason


def test_gate_never_raises(tmp_path, state, monkeypatch):
    marker = tmp_path / "marker"
    repo = gated_repo(tmp_path, state, [marker_check(marker)])
    with monkeypatch.context() as m:
        m.setattr(verify, "fingerprint", lambda root: (_ for _ in ()).throw(RuntimeError("boom")))
        out = verify.gate(repo, state)
    assert out.action == "error" and "RuntimeError: boom" in out.reason and runs(marker) == 0
    garbage = tmp_path / "garbage"
    garbage.mkdir()
    assert verify.gate(garbage, state / "nope").action == "skip"
    assert verify.gate(tmp_path / "missing", state).action == "skip"


def test_gate_invalid_trusted_config_is_an_error(tmp_path, state):
    repo = make_repo(tmp_path, files={"code.txt": "ok\n"})
    write_raw(repo, one('["a && b"]'))
    out = verify.gate(repo, state)
    assert (
        out.action == "skip" and "not trusted" in out.reason
    )  # untrusted is checked before anything is parsed
    # a file that is trusted by hash but invalid cannot happen via `trust`; simulate a hand-edited state file
    import hashlib

    (state / "verify-trust.json").write_text(
        json.dumps(
            {
                verify._repo_key(repo): hashlib.sha256(
                    verify.config_path(repo).read_bytes()
                ).hexdigest()
            }
        ),
        encoding="utf-8",
    )
    out = verify.gate(repo, state)
    assert out.action == "error" and "shell characters" in out.reason


def test_gate_does_not_nest(tmp_path, state, monkeypatch):
    marker = tmp_path / "marker"
    repo = gated_repo(tmp_path, state, [marker_check(marker)])
    with monkeypatch.context() as m:
        m.setenv(verify.ACTIVE_ENV, "1")
        out = verify.gate(repo, state)
    assert out.action == "skip" and "not nesting" in out.reason and runs(marker) == 0
    child = pycheck("child", f"import os; print(os.environ.get({verify.ACTIVE_ENV!r}))")
    assert verify.run(repo, [child]).results[0].output == "1"


def test_gate_survives_corrupt_state(tmp_path, state):
    marker = tmp_path / "marker"
    repo = gated_repo(tmp_path, state, [marker_check(marker)])
    (state / "verify-gate.json").write_text("{corrupt", encoding="utf-8")
    assert verify.gate(repo, state).action == "pass"
    (state / "verify-gate.json").write_text(
        json.dumps({verify._repo_key(repo): "not-a-dict"}), encoding="utf-8"
    )
    put(repo, "code.txt", "ok\n\n")
    assert verify.gate(repo, state).action == "pass"
    (state / "verify-gate.json").write_text(
        json.dumps({verify._repo_key(repo): {"blocks": {"x": "many"}, "last_green": 5}}),
        encoding="utf-8",
    )
    put(repo, "code.txt", "ok\n\n\n")
    assert verify.gate(repo, state).action == "pass"


def test_gate_state_failures_fail_open(tmp_path, state, monkeypatch):
    marker = tmp_path / "marker"
    repo = gated_repo(tmp_path, state, [marker_check(marker)])
    with monkeypatch.context() as m:
        m.setattr(
            verify, "atomic_write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        )
        assert (
            verify.gate(repo, state).action == "pass"
        )  # not remembering a green run only costs a re-run
        put(repo, "code.txt", "bad\n")
        out = verify.gate(repo, state)
    assert out.action == "error" and "cannot record gate state" in out.reason, (
        "no block counter, no blocking"
    )


def test_gate_state_keeps_at_most_50_repos_and_20_fingerprints(state):
    for i in range(60):
        verify._record(state, f"repo{i}", verify._mark_green(f"fp{i}"))
    data = json.loads((state / "verify-gate.json").read_text(encoding="utf-8"))
    assert (
        len(data) == verify.MAX_REPOS == 50
        and "repo0" not in data
        and "repo59" in data
        and "repo10" in data
    )
    verify._record(
        state, "repo10", verify._mark_green("again")
    )  # recently used survives the next prune
    for i in range(60, 108):
        verify._record(state, f"repo{i}", verify._mark_green(f"fp{i}"))
    data = json.loads((state / "verify-gate.json").read_text(encoding="utf-8"))
    assert len(data) == 50 and "repo10" in data and "repo11" not in data

    for i in range(30):
        verify._record(state, "busy", verify._mark_block(f"f{i}", f"summary {i}"))
    verify._record(state, "busy", verify._mark_block("f29", "summary 29"))
    busy = json.loads((state / "verify-gate.json").read_text(encoding="utf-8"))["busy"]
    assert (
        len(busy["blocks"]) == verify.MAX_BLOCK_FPS == 20
        and busy["blocks"]["f29"] == 2
        and "f0" not in busy["blocks"]
    )
    assert busy["last_block"] == {"fp": "f29", "summary": "summary 29"}


def test_gate_state_files_are_written_privately_and_atomically(tmp_path, state):
    marker = tmp_path / "marker"
    repo = gated_repo(tmp_path, state, [marker_check(marker)])
    verify.gate(repo, state)
    assert sorted(p.name for p in state.iterdir() if p.is_file()) == [
        "verify-gate.json",
        "verify-trust.json",
    ]
    if os.name != "nt":
        assert (state / "verify-gate.json").stat().st_mode & 0o777 == 0o600


def test_gate_result_objects_carry_the_evidence(tmp_path, state):
    repo = gated_repo(
        tmp_path, state, [pycheck("a", "print('hi')"), pycheck("b", "print('there')")]
    )
    out = verify.gate(repo, state)
    assert out.action == "pass" and [r.output for r in out.results] == ["hi", "there"]
    assert (
        "2 check(s) passed" in out.reason
        and out.seconds >= sum(r.seconds for r in out.results) * 0.5
    )

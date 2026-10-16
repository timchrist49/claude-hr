"""claude_hr.initrepo: plan() only reads, apply() only creates absent files, and every template is valid by construction."""

import json
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from claude_hr import initrepo

TEMPLATES = Path(initrepo.__file__).resolve().parent / "data" / "repo-template"
GIT_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


@pytest.fixture(autouse=True)
def isolated(sandbox, tmp_path, monkeypatch):
    """No real home, and git must not read the developer's own config (insteadOf rewrites, default branch, ...)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-such-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv(
        "HOME", str(sandbox)
    )  # older gits ignore GIT_CONFIG_GLOBAL and read ~/.gitconfig instead
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox / ".config"))
    for var in GIT_VARS:
        monkeypatch.delenv(var, raising=False)


# ---- helpers --------------------------------------------------------------------------------------------------------


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def write(root, rel, text=""):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def make_repo(path, files=None, origin=None, branch="main"):
    """A real `git init`; the local config and HEAD are written directly (one git process instead of seven, which is
    what keeps this file's runtime sane on Windows)."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    head = path / ".git" / "HEAD"
    head.write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8", newline="\n")
    config = "[user]\n\temail = t@example.com\n\tname = t\n[core]\n\tautocrlf = false\n"
    if origin:
        config += (
            f'[remote "origin"]\n\turl = {origin}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
        )
    with open(path / ".git" / "config", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(config)
    for rel, text in (files or {}).items():
        write(path, rel, text)
    return path


PYPROJECT = '[project]\nname = "demo"\nversion = "0.1"\n'


@pytest.fixture
def rich(tmp_path):
    """Every supported language, an existing workflow and a GitHub origin."""
    return make_repo(
        tmp_path / "rich",
        {
            "pyproject.toml": PYPROJECT,
            "package.json": json.dumps({"scripts": {"test": "jest"}}),
            "package-lock.json": "{}",
            "go.mod": "module demo\n",
            "Cargo.toml": '[package]\nname = "demo"\n',
            ".github/workflows/lint.yml": "name: lint\n",
        },
        origin="https://github.com/acme/demo.git",
    )


def by_path(actions):
    return {a.path: a for a in actions}


def content_of(root, path, **kw):
    return by_path(initrepo.plan(root, **kw))[path].content


def snapshot(root):
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in Path(root).rglob("*")
    }


# ---- a tiny YAML-subset reader (no YAML library allowed): mappings, lists, quoted/plain scalars, flow lists, | blocks --


def _strip_comment(s):
    quote = None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'" and (i == 0 or s[i - 1] in " [,:"):
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1] == " "):
            return s[:i].rstrip()
    return s


def _split_flow(s):
    items, buf, quote = [], "", None
    for ch in s:
        if quote:
            buf += ch
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote, buf = ch, buf + ch
        elif ch == ",":
            items.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        items.append(buf.strip())
    return items


def _scalar(s):
    s = s.strip()
    if s.startswith('"'):
        return json.loads(s)
    if s.startswith("'"):
        assert s.endswith("'") and len(s) > 1, s
        return s[1:-1].replace("''", "'")
    if s.startswith("["):
        assert s.endswith("]"), s
        return [_scalar(x) for x in _split_flow(s[1:-1])]
    assert ": " not in s and not s.endswith(":"), f"unquoted colon in plain scalar: {s!r}"
    return s


_KEY = re.compile(r'^("[^"]*"|[A-Za-z0-9_.-]+):(?: +(.*))?$')
_LITERAL = re.compile(r"^(- )?([A-Za-z0-9_.-]+): *[|>][+-]?$")


def _prep(text):
    raw, lines, i = text.split("\n"), [], 0
    while i < len(raw):
        line = raw[i]
        i += 1
        assert "\t" not in line, "tab character"
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = _strip_comment(line.strip())
        m = _LITERAL.match(body)
        if m:
            block = []
            while i < len(raw) and (
                not raw[i].strip() or len(raw[i]) - len(raw[i].lstrip(" ")) > indent
            ):
                block.append(raw[i].strip())
                i += 1
            body = f"{m.group(1) or ''}{m.group(2)}: " + json.dumps("\n".join(block).strip())
        lines.append((indent, body))
    return lines


def _is_item(body):
    return body.startswith("- ") or body == "-"


def _parse_block(lines, i, indent):
    return (_parse_seq if _is_item(lines[i][1]) else _parse_map)(lines, i, indent)


def _parse_seq(lines, i, indent):
    out = []
    while i < len(lines) and lines[i][0] == indent and _is_item(lines[i][1]):
        body = lines[i][1]
        rest = body[1:].lstrip()
        if not rest:
            i += 1
            assert i < len(lines) and lines[i][0] > indent, "empty list item"
            val, i = _parse_block(lines, i, lines[i][0])
        elif _KEY.match(rest):
            lines[i] = (indent + len(body) - len(rest), rest)
            val, i = _parse_map(lines, i, lines[i][0])
        else:
            val, i = _scalar(rest), i + 1
        out.append(val)
    return out, i


def _parse_map(lines, i, indent):
    out = {}
    while i < len(lines) and lines[i][0] == indent and not _is_item(lines[i][1]):
        m = _KEY.match(lines[i][1])
        assert m, f"not a mapping line: {lines[i][1]!r}"
        key = json.loads(m[1]) if m[1].startswith('"') else m[1]
        assert key not in out, f"duplicate key {key!r}"
        i += 1
        if m[2]:
            out[key] = _scalar(m[2])
        elif i < len(lines) and lines[i][0] > indent:
            out[key], i = _parse_block(lines, i, lines[i][0])
        elif i < len(lines) and lines[i][0] == indent and _is_item(lines[i][1]):
            out[key], i = _parse_seq(lines, i, indent)
        else:
            out[key] = None
    return out, i


def parse_yaml(text):
    lines = _prep(text)
    value, i = _parse_block(lines, 0, lines[0][0])
    assert i == len(lines), f"unparsed (bad indentation?): {lines[i]!r}"
    return value


def test_the_yaml_reader_itself():
    doc = parse_yaml(
        "a: 1  # note\nb:\n  - x: 1\n    y: [p, \"q, r\"]\n  - z\nc: |\n  # not a comment\n  text\nd: 'it''s'\n"
    )
    assert doc == {
        "a": "1",
        "b": [{"x": "1", "y": ["p", "q, r"]}, "z"],
        "c": "# not a comment\ntext",
        "d": "it's",
    }
    for bad in ("a:\n\tb: 1\n", "a: b: c\n", "a: 1\n  b: 2\n", "a: 1\na: 2\n"):
        with pytest.raises(AssertionError):
            parse_yaml(bad)


# ---- plan(): read-only, deterministic, complete -----------------------------------------------------------------------


def test_plan_never_writes_anything(rich):
    before = snapshot(rich)
    first = initrepo.plan(rich)
    assert snapshot(rich) == before
    assert initrepo.plan(rich) == first  # deterministic, including order


def test_plan_covers_every_group_in_a_fixed_order(rich):
    actions = initrepo.plan(rich)
    assert {a.group for a in actions} == set(initrepo.GROUPS)
    order = [initrepo.GROUPS.index(a.group) for a in actions]
    assert order == sorted(order)
    paths = [a.path for a in actions]
    assert len(paths) == len(set(paths))
    assert {
        ".github/ISSUE_TEMPLATE/bug.yml",
        ".github/ISSUE_TEMPLATE/feature.yml",
        ".github/ISSUE_TEMPLATE/agent-task.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/pull_request_template.md",
        "docs/plans/README.md",
        "docs/plans/_template.md",
        "docs/decisions/README.md",
        "docs/decisions/0000-template.md",
        ".editorconfig",
        ".gitattributes",
        ".github/dependabot.yml",
        ".github/CODEOWNERS",
        "REVIEW.md",
        ".github/workflows/ci.yml",
    } == set(paths)
    assert all(not a.exists for a in actions)


def test_only_filters_groups_and_keeps_canonical_order(rich):
    assert {a.group for a in initrepo.plan(rich, only="docs")} == {"docs"}
    got = [a.group for a in initrepo.plan(rich, only=["ci", "editor"])]
    assert got == ["editor", "editor", "ci"]
    assert [a.path for a in initrepo.plan(rich, only="github, review")][-1] == "REVIEW.md"
    assert (
        len(initrepo.plan(rich, only=None))
        == len(initrepo.plan(rich, only=[]))
        == len(initrepo.plan(rich, only="all"))
    )
    assert initrepo.parse_groups("CI,Docs") == ["docs", "ci"]


def test_unknown_group_is_rejected_with_the_valid_names(rich):
    with pytest.raises(ValueError, match="unknown group.*gtihub.*valid groups: github, docs"):
        initrepo.plan(rich, only="docs,gtihub")


def test_groups_that_need_no_git_never_run_git(rich, monkeypatch):
    def no_git(*a, **k):
        raise AssertionError("git was run")

    with monkeypatch.context() as m:
        m.setattr(initrepo, "_git", no_git)
        assert initrepo.plan(rich, only="github,docs,editor,dependabot")


def test_plan_survives_a_missing_git(rich, monkeypatch):
    gone = subprocess.CompletedProcess([], 127, "", "git not found")
    with monkeypatch.context() as m:
        m.setattr(initrepo, "_git", lambda *a, **k: gone)
        actions, notes = initrepo.plan_with_notes(rich)
    assert ".github/CODEOWNERS" not in by_path(actions)
    assert any(n.startswith("codeowners:") for n in notes)
    assert 'branches: ["main"]' in by_path(actions)[".github/workflows/ci.yml"].content


def test_empty_repo_gets_the_language_independent_files_and_explains_the_rest(tmp_path):
    root = make_repo(tmp_path / "empty")
    actions, notes = initrepo.plan_with_notes(root)
    paths = {a.path for a in actions}
    assert ".github/workflows/ci.yml" not in paths and ".github/dependabot.yml" not in paths
    assert ".github/CODEOWNERS" not in paths
    assert {"REVIEW.md", ".editorconfig", "docs/plans/_template.md"} <= paths
    joined = "\n".join(notes)
    assert "ci:" in joined and "dependabot:" in joined and "codeowners:" in joined


def test_a_directory_that_is_not_a_repo_still_plans(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    actions, notes = initrepo.plan_with_notes(root)
    assert actions and any(n.startswith("codeowners:") for n in notes)
    assert not any(root.iterdir())


# ---- exists / never overwrite / idempotence ----------------------------------------------------------------------------


def test_existing_files_are_marked_kept_and_never_overwritten(tmp_path):
    keep = {
        ".editorconfig": "mine = 1\n",
        "REVIEW.md": "my review rules\n",
        ".gitattributes": "* text=auto\n",
        ".github/ISSUE_TEMPLATE/bug.yml": "name: my bug\n",
        "docs/plans/README.md": "my plans\n",
    }
    root = make_repo(
        tmp_path / "r", {"pyproject.toml": PYPROJECT, **keep}, origin="git@github.com:acme/r.git"
    )
    actions = by_path(initrepo.plan(root))
    for rel in keep:
        assert actions[rel].exists, rel
    assert not actions[".github/ISSUE_TEMPLATE/feature.yml"].exists
    written = initrepo.apply(root, initrepo.plan(root))
    assert not set(written) & set(keep)
    assert ".github/ISSUE_TEMPLATE/feature.yml" in written
    for rel, text in keep.items():
        assert (root / rel).read_text(encoding="utf-8") == text


def test_equivalent_locations_count_as_existing(tmp_path):
    root = make_repo(
        tmp_path / "r",
        {
            "pyproject.toml": PYPROJECT,
            "CODEOWNERS": "* @someone\n",
            ".github/dependabot.yaml": "version: 2\n",
            ".github/PULL_REQUEST_TEMPLATE.md": "pr\n",
            ".github/workflows/ci.yaml": "name: ci\n",
        },
        origin="https://github.com/acme/r",
    )
    actions = by_path(initrepo.plan(root))
    assert actions["CODEOWNERS"].exists and ".github/CODEOWNERS" not in actions
    assert actions[".github/dependabot.yaml"].exists and ".github/dependabot.yml" not in actions
    # which spelling is reported depends on whether the filesystem is case-sensitive; either way it is kept
    pr = [a for a in actions.values() if a.path.lower() == ".github/pull_request_template.md"]
    assert len(pr) == 1 and pr[0].exists
    assert actions[".github/workflows/ci.yaml"].exists and ".github/workflows/ci.yml" not in actions
    initrepo.apply(root, initrepo.plan(root))
    names = [p.name for p in (root / ".github").iterdir() if p.name.lower().startswith("pull_")]
    assert names == ["PULL_REQUEST_TEMPLATE.md"]
    assert not (root / ".github" / "CODEOWNERS").exists()
    assert not (root / ".github" / "dependabot.yml").exists()
    assert not (root / ".github" / "workflows" / "ci.yml").exists()


def test_existing_codeowners_without_an_origin_is_not_a_noted_problem(tmp_path):
    root = make_repo(tmp_path / "r", {"CODEOWNERS": "* @x\n"})
    _, notes = initrepo.plan_with_notes(root, only="codeowners")
    assert notes == []


def test_apply_writes_exactly_the_planned_content_with_lf_endings_and_is_idempotent(rich):
    actions = initrepo.plan(rich)
    written = initrepo.apply(rich, actions)
    assert set(written) == {a.path for a in actions if not a.exists}
    assert written == [a.path for a in actions if not a.exists]
    for a in actions:
        if not a.exists:
            data = (rich / a.path).read_bytes()
            assert data == a.content.encode("utf-8") and b"\r" not in data
    assert initrepo.apply(rich, initrepo.plan(rich)) == []
    assert all(a.exists for a in initrepo.plan(rich))
    assert (
        initrepo.apply(rich, actions) == []
    )  # stale actions (exists=False) are re-checked against the disk
    assert not list(rich.rglob(".hr-init-*"))


def test_apply_does_not_clobber_a_file_created_after_planning(tmp_path):
    root = make_repo(tmp_path / "r")
    actions = initrepo.plan(root, only="editor")
    write(root, ".editorconfig", "created meanwhile\n")
    result = initrepo.apply_detailed(root, actions)
    assert ".editorconfig" in result.skipped and ".gitattributes" in result.written
    assert (root / ".editorconfig").read_text(encoding="utf-8") == "created meanwhile\n"


def test_a_failed_write_is_reported_and_leaves_no_temp_file(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "r")
    actions = initrepo.plan(root, only="review")

    def no_links(src, dst):
        raise OSError("no hard links here")

    def boom(src, dst):
        raise OSError("disk says no")

    with monkeypatch.context() as m:
        m.setattr(initrepo.os, "link", no_links)
        m.setattr(initrepo.os, "replace", boom)
        result = initrepo.apply_detailed(root, actions)
    assert result.written == [] and result.errors == [("REVIEW.md", "disk says no")]
    assert not (root / "REVIEW.md").exists() and not list(root.rglob(".hr-init-*"))
    assert initrepo.apply(root, actions) == ["REVIEW.md"]


def test_without_hard_links_it_falls_back_to_check_then_rename(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "r")
    actions = initrepo.plan(root, only="editor")

    def no_links(src, dst):
        raise OSError("no hard links here")

    with monkeypatch.context() as m:
        m.setattr(initrepo.os, "link", no_links)
        first = initrepo.apply_detailed(root, actions)
        second = initrepo.apply_detailed(root, actions)  # stale actions: the files exist now
    assert first.written == [".editorconfig", ".gitattributes"] and second.written == []
    assert second.skipped == [".editorconfig", ".gitattributes"]
    assert (root / ".editorconfig").read_text(encoding="utf-8") == actions[0].content
    assert not list(root.rglob(".hr-init-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_written_files_are_readable_by_others_not_private_temp_files(tmp_path):
    root = make_repo(tmp_path / "r")
    initrepo.apply(root, initrepo.plan(root, only="review"))
    assert stat.S_IMODE((root / "REVIEW.md").stat().st_mode) == 0o644


def test_a_file_that_appears_between_the_check_and_the_write_survives(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "r")
    actions = initrepo.plan(root, only="review")
    real_link = os.link

    def racing_link(src, dst):
        write(
            root, "REVIEW.md", "created by someone else\n"
        )  # lands after apply() looked, before it wrote
        real_link(src, dst)

    with monkeypatch.context() as m:
        m.setattr(initrepo.os, "link", racing_link)
        result = initrepo.apply_detailed(root, actions)
    assert result.written == [] and result.skipped == ["REVIEW.md"] and result.errors == []
    assert (root / "REVIEW.md").read_text(encoding="utf-8") == "created by someone else\n"
    assert not list(root.rglob(".hr-init-*"))


def test_a_file_in_the_way_of_a_directory_is_an_error_not_a_crash(tmp_path):
    root = make_repo(tmp_path / "r", {"docs": "i am a file\n"})
    result = initrepo.apply_detailed(root, initrepo.plan(root, only="docs,review"))
    assert [p for p, _ in result.errors] == [
        "docs/plans/README.md",
        "docs/plans/_template.md",
        "docs/decisions/README.md",
        "docs/decisions/0000-template.md",
    ]
    assert result.written == ["REVIEW.md"]


@pytest.mark.parametrize(
    "bad",
    [
        "../evil.txt",
        "a/../../evil.txt",
        "/etc/evil.txt",
        "C:/evil.txt",
        "C:\\evil.txt",
        ".git/hooks/pre-commit",
        ".GIT/config",
        "",
    ],
)
def test_apply_refuses_paths_outside_the_repo_and_inside_dot_git(tmp_path, bad):
    root = make_repo(tmp_path / "r")
    result = initrepo.apply_detailed(root, [initrepo.Action(bad, "x\n", False, "review")])
    assert result.written == [] and len(result.errors) == 1
    assert (
        not (tmp_path / "evil.txt").exists()
        and not (root / ".git" / "hooks" / "pre-commit").exists()
    )


def test_apply_refuses_a_symlinked_directory_that_leaves_the_repo(tmp_path):
    root = make_repo(tmp_path / "r")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / ".github"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("symlinks not available here")
        # a Windows directory junction needs no privilege and is resolved by realpath just like a symlink
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True
        )
        if made.returncode != 0:
            pytest.skip("neither symlinks nor junctions available here")
    result = initrepo.apply_detailed(root, initrepo.plan(root, only="github"))
    assert result.written == [] and result.errors
    assert not any(outside.iterdir())


def test_apply_will_not_create_the_repo_root(tmp_path):
    missing = tmp_path / "nope"
    result = initrepo.apply_detailed(missing, initrepo.plan(missing, only="review"))
    assert result.written == [] and result.errors and not missing.exists()


def test_paths_with_spaces_and_parentheses_work(tmp_path):
    root = make_repo(
        tmp_path / "my repo (v2)" / "sub dir",
        {"pyproject.toml": PYPROJECT},
        origin="git@github.com:acme/spaced.git",
    )
    written = initrepo.apply(root, initrepo.plan(root))
    assert ".github/CODEOWNERS" in written and ".github/workflows/ci.yml" in written
    assert (root / ".github" / "CODEOWNERS").read_text(encoding="utf-8").endswith("* @acme\n")
    assert "spaced" in (root / "REVIEW.md").read_text(encoding="utf-8").splitlines()[0]


def test_format_plan_lists_creates_kept_files_and_notes(tmp_path):
    root = make_repo(tmp_path / "r", {".editorconfig": "x\n"})
    actions, notes = initrepo.plan_with_notes(root, only="editor,codeowners")
    text = initrepo.format_plan(actions, notes)
    assert "exists (kept)  .editorconfig  [editor]" in text
    assert "would create   .gitattributes  [editor]" in text
    assert "note: codeowners:" in text


# ---- owner / remote parsing ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/acme/widgets.git", ("acme", "widgets")),
        ("https://github.com/acme/widgets", ("acme", "widgets")),
        ("https://github.com/acme/widgets/", ("acme", "widgets")),
        ("HTTPS://GitHub.com/Acme/Widgets.git", ("Acme", "Widgets")),
        ("http://github.com/acme/widgets.git", ("acme", "widgets")),
        ("https://www.github.com/acme/widgets", ("acme", "widgets")),
        ("https://user:s3cret@github.com/acme/widgets.git", ("acme", "widgets")),
        ("https://x-access-token:ghp_abc@github.com/acme/widgets", ("acme", "widgets")),
        ("git@github.com:acme/widgets.git", ("acme", "widgets")),
        ("git@github.com:acme/widgets", ("acme", "widgets")),
        ("git@github.com:123/widgets.git", ("123", "widgets")),
        ("ssh://git@github.com/acme/widgets.git", ("acme", "widgets")),
        ("ssh://git@ssh.github.com:443/acme/widgets.git", ("acme", "widgets")),
        ("git://github.com/acme/widgets.git", ("acme", "widgets")),
        ("https://github.com/my-org/my.repo-name_v2.git", ("my-org", "my.repo-name_v2")),
        ("  https://github.com/acme/widgets.git\n", ("acme", "widgets")),
        ("https://gitlab.com/acme/widgets.git", None),
        ("git@gitlab.com:acme/widgets.git", None),
        ("https://github.example.com/acme/widgets.git", None),
        ("https://notgithub.com/acme/widgets.git", None),
        ("https://github.com@evil.example/acme/widgets.git", None),
        ("https://evil.example/github.com/acme/widgets.git", None),
        ("https://github.com/acme", None),
        ("https://github.com/acme/widgets/tree/main", None),
        ("https://github.com/-acme/widgets", None),
        ("https://github.com/acme/..", None),
        ("/srv/git/widgets.git", None),
        ("C:\\repos\\widgets", None),
        ("", None),
    ],
)
def test_parse_github_remote(url, expected):
    assert initrepo.parse_github_remote(url) == expected


@pytest.mark.parametrize(
    "origin", ["https://github.com/acme-co/widgets.git", "git@github.com:acme-co/widgets.git"]
)
def test_codeowners_owner_comes_from_the_origin_remote(tmp_path, origin):
    root = make_repo(tmp_path / "r", origin=origin)
    action = by_path(initrepo.plan(root))[".github/CODEOWNERS"]
    lines = [ln for ln in action.content.splitlines() if ln and not ln.startswith("#")]
    assert lines == ["* @acme-co"]
    assert (
        "@org/team-name" in action.content
    )  # warns that a bare organization name is not a valid owner


def test_codeowners_is_skipped_with_a_note_when_there_is_no_usable_origin(tmp_path):
    for name, origin in (("none", None), ("gitlab", "https://gitlab.com/acme/w.git")):
        root = make_repo(tmp_path / name, origin=origin)
        actions, notes = initrepo.plan_with_notes(root, only="codeowners")
        assert actions == [] and len(notes) == 1 and "origin" in notes[0]


def test_only_the_origin_remote_counts(tmp_path):
    root = make_repo(tmp_path / "r")
    git(root, "remote", "add", "upstream", "https://github.com/someone-else/w.git")
    assert initrepo.plan(root, only="codeowners") == []


def test_credentials_in_the_remote_never_reach_a_file(tmp_path):
    root = make_repo(tmp_path / "r", origin="https://user:hunter2@github.com/acme/widgets.git")
    for action in initrepo.plan(root):
        assert "hunter2" not in action.content


# ---- language and ecosystem detection ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "files, languages",
    [
        ({}, []),
        ({"pyproject.toml": PYPROJECT}, ["python"]),
        ({"requirements.txt": "pytest\n"}, ["python"]),
        ({"setup.py": "", "Pipfile": ""}, ["python"]),
        ({"package.json": "{}"}, ["node"]),
        ({"go.mod": "module x\n"}, ["go"]),
        ({"Cargo.toml": ""}, ["rust"]),
        ({"uv.lock": ""}, []),
        (
            {"Cargo.toml": "", "go.mod": "", "package.json": "{}", "pyproject.toml": ""},
            ["python", "node", "go", "rust"],
        ),
    ],
)
def test_detect_languages(tmp_path, files, languages):
    root = make_repo(tmp_path / "r", files)
    assert initrepo.detect_languages(root) == languages


def ecosystems_in(root, **kw):
    text = content_of(root, ".github/dependabot.yml", **kw)
    return re.findall(r'package-ecosystem: "([^"]+)"', text)


@pytest.mark.parametrize(
    "files, expected",
    [
        ({"pyproject.toml": PYPROJECT}, ["pip"]),
        ({"requirements.txt": "x\n"}, ["pip"]),
        ({"dev-requirements.txt": "x\n"}, ["pip"]),
        ({"pyproject.toml": PYPROJECT, "uv.lock": ""}, ["uv"]),
        ({"pyproject.toml": PYPROJECT, "uv.lock": "", "requirements.txt": "x\n"}, ["uv", "pip"]),
        ({"package.json": "{}", "pnpm-lock.yaml": ""}, ["npm"]),
        ({"Cargo.toml": ""}, ["cargo"]),
        ({"go.mod": "module x\n"}, ["gomod"]),
        (
            {"go.mod": "", "Cargo.toml": "", "package.json": "{}", "pyproject.toml": ""},
            ["pip", "npm", "gomod", "cargo"],
        ),
        (
            {"package.json": "{}", ".github/workflows/x.yaml": "name: x\n"},
            ["npm", "github-actions"],
        ),
        ({".github/workflows/x.yml": "name: x\n"}, ["github-actions"]),
    ],
)
def test_dependabot_ecosystems_follow_the_repo(tmp_path, files, expected):
    root = make_repo(tmp_path / "r", files)
    assert ecosystems_in(root, only="dependabot") == expected
    assert initrepo.detect_ecosystems(root) == expected


def test_dependabot_watches_actions_when_ci_is_created_in_the_same_run(tmp_path):
    root = make_repo(tmp_path / "r", {"package.json": "{}"})
    assert ecosystems_in(root, only="dependabot") == ["npm"]
    assert ecosystems_in(root) == ["npm", "github-actions"]
    assert ecosystems_in(root, only="dependabot,ci") == ["npm", "github-actions"]


def test_an_empty_workflows_directory_does_not_count(tmp_path):
    root = make_repo(tmp_path / "r", {"package.json": "{}"})
    (root / ".github" / "workflows").mkdir(parents=True)
    assert ecosystems_in(root, only="dependabot") == ["npm"]


def test_dependabot_file_shape(rich):
    text = content_of(rich, ".github/dependabot.yml")
    doc = parse_yaml(text)
    assert doc["version"] == "2" and "version: 2\n" in text
    updates = doc["updates"]
    assert [u["package-ecosystem"] for u in updates] == [
        "pip",
        "npm",
        "gomod",
        "cargo",
        "github-actions",
    ]
    for u in updates:
        assert u["directory"] == "/"
        assert u["schedule"] == {"interval": "weekly"}
        assert u["cooldown"] == {"default-days": "7"}
        assert u["open-pull-requests-limit"] == "5"
        group = u["groups"]["minor-and-patch"]
        assert group["patterns"] == ["*"] and group["update-types"] == ["minor", "patch"]


# ---- CI skeleton --------------------------------------------------------------------------------------------------------


def ci_steps(root, **kw):
    text = content_of(root, ".github/workflows/ci.yml", only="ci", **kw)
    doc = parse_yaml(text)
    return doc, doc["jobs"]["verify"]["steps"]


def run_lines(steps):
    return [s["run"] for s in steps if "run" in s]


def test_ci_is_hardened(rich):
    text = content_of(rich, ".github/workflows/ci.yml")
    doc, steps = ci_steps(rich)
    assert doc["permissions"] == {"contents": "read"}
    job = doc["jobs"]["verify"]
    assert int(job["timeout-minutes"]) > 0 and job["runs-on"] == "ubuntu-latest"
    assert doc["concurrency"]["group"].startswith("ci-")
    assert "cancel-in-progress" in doc["concurrency"]
    assert doc["on"]["push"]["branches"] == ["main"] and "pull_request" in doc["on"]
    assert "pin to a commit SHA (Dependabot keeps them updated)" in text
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses[0].startswith("actions/checkout@")
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+", ref), f"{ref} must be referenced by tag"
    assert steps[0]["with"] == {"persist-credentials": "false"}
    for forbidden in ("pull_request_target", "secrets.", "write-all", "continue-on-error"):
        assert forbidden not in text


def test_ci_for_python(tmp_path):
    root = make_repo(tmp_path / "r", {"pyproject.toml": PYPROJECT})
    _, steps = ci_steps(root)
    assert [s["uses"] for s in steps if "uses" in s] == [
        "actions/checkout@v4",
        "actions/setup-python@v5",
    ]
    assert run_lines(steps) == [
        "python -m pip install -e . pytest ruff",
        "python -m pytest -q",
        "ruff check .",
    ]


@pytest.mark.parametrize(
    "files, install",
    [
        (
            {"pyproject.toml": PYPROJECT + '[project.optional-dependencies]\ndev = ["pytest"]\n'},
            "-e '.[dev]' pytest ruff",
        ),
        ({"pyproject.toml": "[tool.ruff]\nline-length = 100\n"}, "pytest ruff"),
        ({"pyproject.toml": "this is [not toml"}, "pytest ruff"),
        (
            {"pyproject.toml": PYPROJECT, "requirements.txt": "x\n", "requirements-dev.txt": "y\n"},
            "-e . -r requirements-dev.txt -r requirements.txt pytest ruff",
        ),
        ({"setup.py": "", "requirements.txt": "x\n"}, "-e . -r requirements.txt pytest ruff"),
        ({"Pipfile": ""}, "pytest ruff"),
    ],
)
def test_python_install_command_matches_how_the_project_is_packaged(tmp_path, files, install):
    root = make_repo(tmp_path / "r", files)
    _, steps = ci_steps(root)
    assert run_lines(steps)[0] == f"python -m pip install {install}"


@pytest.mark.parametrize(
    "files, install, runs",
    [
        ({"package-lock.json": "{}"}, "npm ci", ["npm test", "npm run lint"]),
        ({}, "npm install", ["npm test", "npm run lint"]),
        (
            {"pnpm-lock.yaml": ""},
            "corepack enable && pnpm install --frozen-lockfile",
            ["pnpm test", "pnpm run lint"],
        ),
        ({"yarn.lock": ""}, "corepack enable && yarn install", ["yarn test", "yarn run lint"]),
    ],
)
def test_ci_for_node_uses_the_repos_package_manager(tmp_path, files, install, runs):
    scripts = {"scripts": {"test": "jest", "lint": "eslint ."}}
    root = make_repo(tmp_path / "r", {"package.json": json.dumps(scripts), **files})
    _, steps = ci_steps(root)
    assert [s["uses"] for s in steps if "uses" in s] == [
        "actions/checkout@v4",
        "actions/setup-node@v4",
    ]
    assert run_lines(steps) == [install, *runs]


def test_node_without_a_test_script_installs_only_and_says_so(tmp_path):
    for name, package in (("none", '{"name": "x"}'), ("broken", "{not json")):
        root = make_repo(tmp_path / name, {"package.json": package})
        actions, notes = initrepo.plan_with_notes(root, only="ci")
        _, steps = ci_steps(root)
        assert run_lines(steps) == ["npm install"]
        assert any("no test or lint script" in n for n in notes)
        assert actions


def test_ci_for_go_and_rust(tmp_path):
    go = make_repo(tmp_path / "go", {"go.mod": "module x\n"})
    _, steps = ci_steps(go)
    assert [s.get("uses") for s in steps][1] == "actions/setup-go@v5"
    assert steps[1]["with"] == {"go-version-file": "go.mod"}
    assert run_lines(steps) == ["go test ./..."]
    rust = make_repo(tmp_path / "rust", {"Cargo.toml": ""})
    _, steps = ci_steps(rust)
    assert [s["uses"] for s in steps if "uses" in s] == ["actions/checkout@v4"]
    assert run_lines(steps) == ["cargo test"]


def test_ci_for_several_languages_is_one_job_with_every_toolchain(rich):
    _, steps = ci_steps(rich)
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses == [
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "actions/setup-node@v4",
        "actions/setup-go@v5",
    ]
    assert run_lines(steps) == [
        "python -m pip install -e . pytest ruff",
        "npm ci",
        "python -m pytest -q",
        "ruff check .",
        "npm test",
        "go test ./...",
        "cargo test",
    ]
    assert len({s["name"] for s in steps if "name" in s}) == len([s for s in steps if "name" in s])


def test_default_branch_follows_the_repo(tmp_path):
    def branches(root):
        return ci_steps(root)[0]["on"]["push"]["branches"]

    assert branches(make_repo(tmp_path / "a", {"go.mod": ""}, branch="master")) == ["master"]
    assert branches(make_repo(tmp_path / "b", {"go.mod": ""}, branch="feature/x")) == ["main"]
    c = make_repo(tmp_path / "c", {"go.mod": ""}, branch="feature/x")
    git(c, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk-2")
    assert branches(c) == ["trunk-2"]
    d = make_repo(tmp_path / "d", {"go.mod": ""}, branch="feature/x")
    git(d, "commit", "-q", "--allow-empty", "-m", "init")
    git(d, "branch", "master")
    assert branches(d) == ["master"]


def test_commands_replace_the_language_defaults(tmp_path):
    root = make_repo(tmp_path / "r", {"pyproject.toml": PYPROJECT, "package.json": "{}"})
    commands = [
        ["python", "-m", "mypy", "src"],
        ["make", "check"],
        ["pytest", "-k", "a and b", "-m", "not slow"],
    ]
    _, steps = ci_steps(root, commands=commands)
    assert run_lines(steps) == [
        "python -m pip install -e . mypy pytest",
        "npm install",
        "python -m mypy src",
        "make check",
        "pytest -k 'a and b' -m 'not slow'",
    ]


def test_empty_commands_fall_back_to_the_defaults(tmp_path):
    root = make_repo(tmp_path / "r", {"go.mod": ""})
    assert run_lines(ci_steps(root, commands=[])[1]) == ["go test ./..."]


def test_python_without_tools_in_the_commands_installs_only_the_project(tmp_path):
    root = make_repo(tmp_path / "r", {"pyproject.toml": PYPROJECT})
    assert run_lines(ci_steps(root, commands=[["make", "test"]])[1]) == [
        "python -m pip install -e .",
        "make test",
    ]
    bare = make_repo(tmp_path / "b", {"Pipfile": ""})
    assert run_lines(ci_steps(bare, commands=[["make", "test"]])[1]) == ["make test"]


def test_commands_are_quoted_so_they_stay_one_yaml_string_and_one_shell_command(tmp_path):
    root = make_repo(tmp_path / "r", {"go.mod": ""})
    argvs = [
        ["true"],
        ["echo", "a: b"],
        ["echo", "x # y"],
        ["echo", "it's $HOME `x`"],
        ["echo", "caf\u00e9"],
        ["./gradlew", "test"],
    ]
    _, steps = ci_steps(root, commands=argvs)
    assert run_lines(steps) == [shlex.join(a) for a in argvs]  # Go has no install step
    assert 'run: "true"' in content_of(root, ".github/workflows/ci.yml", commands=argvs)


def test_unsafe_or_malformed_commands_are_dropped_with_a_note(tmp_path):
    root = make_repo(tmp_path / "r", {"go.mod": ""})
    commands = [
        ["echo", "${{ secrets.TOKEN }}"],
        ["echo", "two\nlines"],
        ["echo", "nul\x00byte"],
        [""],
        [],
        "go test ./...",
        [1, 2],
        ["go", "vet", "./..."],
    ]
    actions, notes = initrepo.plan_with_notes(root, only="ci", commands=commands)
    text = actions[0].content
    assert run_lines(ci_steps(root, commands=commands)[1]) == ["go vet ./..."]
    assert "secrets" not in text and "\x00" not in text
    assert any("ignored 7 verify command(s)" in n for n in notes)
    _, notes = initrepo.plan_with_notes(root, only="docs", commands=commands)
    assert notes == []  # commands only matter to the ci group


def test_too_many_commands_are_capped(tmp_path):
    root = make_repo(tmp_path / "r", {"go.mod": ""})
    commands = [["echo", str(i)] for i in range(30)]
    assert len(run_lines(ci_steps(root, commands=commands)[1])) == initrepo._MAX_COMMANDS


def test_absolute_executables_from_another_machine_become_plain_names(tmp_path):
    root = make_repo(tmp_path / "r", {"go.mod": ""})
    commands = [
        ["C:\\Users\\bob\\venv\\Scripts\\ruff.exe", "check", "."],
        ["/home/bob/.venv/bin/pytest", "-q"],
        ["./local.sh"],
    ]
    assert run_lines(ci_steps(root, commands=commands)[1]) == [
        "ruff check .",
        "pytest -q",
        "./local.sh",
    ]


@pytest.mark.parametrize(
    "text, quoted",
    [
        ("pytest -q", False),
        ("echo 'a b'", False),
        ("python -m pip install -e '.[dev]' pytest", False),
        ("corepack enable && pnpm install", False),
        ("./gradlew test", False),
        ("true", True),
        ("No", True),
        ("null", True),
        ("a: b", True),
        ("x #y", True),
        ("ends:", True),
        ("*.py", True),
        ("-x", True),
        ("[a]", True),
        ("3.13", True),
        ("0x10", True),
        (" lead", True),
        ("trail ", True),
        ("", True),
        ("caf\u00e9", True),
        ("tab\there", True),
    ],
)
def test_yaml_scalar_quotes_exactly_when_needed_and_round_trips(text, quoted):
    out = initrepo._yaml_scalar(text)
    assert out.startswith('"') == quoted
    assert _scalar(out) == text


# ---- templates ----------------------------------------------------------------------------------------------------------


def all_rendered(tmp_path, rich):
    """Content of every group for several repo shapes, so each placeholder and fragment path is exercised."""
    pm = make_repo(
        tmp_path / "pnpm",
        {"package.json": json.dumps({"scripts": {"test": "x"}}), "pnpm-lock.yaml": ""},
        origin="git@github.com:o/r.git",
    )
    uv = make_repo(
        tmp_path / "uv", {"pyproject.toml": PYPROJECT, "uv.lock": "", "requirements.txt": "x\n"}
    )
    out = {}
    for root in (rich, pm, uv):
        for a in initrepo.plan(root, commands=[["make", "check"]] if root == pm else None):
            out[(root.name, a.path)] = a.content
    return out


def test_every_rendered_file_is_clean_text(tmp_path, rich):
    rendered = all_rendered(tmp_path, rich)
    assert len(rendered) > 40
    for (repo, path), text in rendered.items():
        where = f"{repo}:{path}"
        assert text.endswith("\n") and not text.endswith("\n\n"), where
        assert "\r" not in text and "\t" not in text and "\x00" not in text, where
        assert text.isascii(), f"{where}: non-ASCII text"
        for n, line in enumerate(text.splitlines(), 1):
            assert line == line.rstrip(), f"{where}:{n} trailing whitespace"
        assert not re.search(
            r"\{\{(owner|repo|default_branch|ecosystems?|ecosystems_block|steps_block|step_name|install_command|run_command)\}\}",
            text,
        ), where
        if path.endswith((".yml", ".yaml")):
            lines = [
                ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
            ]
            assert all((len(ln) - len(ln.lstrip(" "))) % 2 == 0 for ln in lines), (
                f"{where}: odd indentation"
            )
            parse_yaml(text)


def test_template_files_on_disk_are_all_used_and_all_exist(rich, monkeypatch):
    seen = []
    real = initrepo._template

    def spy(name):
        seen.append(name)
        return real(name)

    with monkeypatch.context() as m:
        m.setattr(initrepo, "_template", spy)
        initrepo.plan(rich)
    on_disk = {p.relative_to(TEMPLATES).as_posix() for p in TEMPLATES.rglob("*") if p.is_file()}
    assert on_disk == set(seen)


def test_a_missing_template_skips_that_group_with_a_note(rich, monkeypatch):
    real = initrepo._template

    def broken(name):
        if name == "REVIEW.md":
            raise FileNotFoundError(name)
        return real(name)

    with monkeypatch.context() as m:
        m.setattr(initrepo, "_template", broken)
        actions, notes = initrepo.plan_with_notes(rich)
    assert "REVIEW.md" not in by_path(actions) and ".editorconfig" in by_path(actions)
    assert any(n.startswith("review: skipped") for n in notes)


def test_issue_forms_are_valid_github_forms(rich):
    kinds = {"markdown", "textarea", "input", "dropdown", "checkboxes"}
    for name in ("bug", "feature", "agent-task"):
        text = content_of(rich, f".github/ISSUE_TEMPLATE/{name}.yml")
        for key in ("name:", "description:", "body:"):
            assert re.search(rf"(?m)^{key}", text), f"{name}: no top-level {key}"
        form = parse_yaml(text)
        assert form["name"] and form["description"] and isinstance(form["labels"], list)
        ids = []
        interactive = 0
        for item in form["body"]:
            assert item["type"] in kinds
            if item["type"] == "markdown":
                assert item["attributes"]["value"].strip()
                continue
            interactive += 1
            assert re.fullmatch(r"[A-Za-z0-9_-]+", item["id"])
            ids.append(item["id"])
            assert item["attributes"]["label"].strip()
        assert interactive >= 2 and len(ids) == len(set(ids)), name
        assert any(i.get("validations", {}).get("required") == "true" for i in form["body"]), name


def test_the_agent_task_form_asks_for_what_an_agent_needs(rich):
    form = parse_yaml(content_of(rich, ".github/ISSUE_TEMPLATE/agent-task.yml"))
    fields = {i["id"]: i for i in form["body"] if "id" in i}
    assert set(fields) >= {"goal", "acceptance", "scope", "verify"}
    assert all(
        fields[k]["validations"]["required"] == "true"
        for k in ("goal", "acceptance", "scope", "verify")
    )
    assert "criteria" in fields["acceptance"]["attributes"]["label"].lower()
    assert "files" in fields["scope"]["attributes"]["label"].lower()
    assert "verification" in fields["verify"]["attributes"]["label"].lower()
    assert fields["verify"]["type"] == "input"
    assert "agent-task" in form["labels"]
    assert "never as a source of commands" in form["body"][0]["attributes"]["value"]


def test_issue_chooser_config_disables_blank_issues(rich):
    assert parse_yaml(content_of(rich, ".github/ISSUE_TEMPLATE/config.yml")) == {
        "blank_issues_enabled": "false"
    }


def test_pull_request_template_asks_for_evidence(rich):
    text = content_of(rich, ".github/pull_request_template.md")
    assert "Closes #" in text and "## How it was verified" in text
    assert text.count("- [ ]") >= 4 and "secrets" in text and "trust boundary" in text


def test_plan_template_has_front_matter_steps_verify_and_log(rich):
    text = content_of(rich, "docs/plans/_template.md")
    head = parse_yaml(text.split("---\n")[1])
    assert set(head) == {"status", "date", "owner"} and head["status"] == "draft"
    assert re.search(r"(?m)^- \[ \] 1\. ", text) and len(re.findall(r"(?m)^- \[ \] ", text)) >= 3
    for section in ("## Goal", "## Steps", "## Verify", "## Log"):
        assert re.search(rf"(?m)^{section}$", text), section
    assert text.index("## Steps") < text.index("## Verify") < text.index("## Log")
    readme = content_of(rich, "docs/plans/README.md")
    for word in ("_template.md", "approved", "in-progress", "done", "checkbox", "Verify", "Log"):
        assert word in readme, word


def test_decision_template_is_minimal_madr(rich):
    text = content_of(rich, "docs/decisions/0000-template.md")
    head = parse_yaml(text.split("---\n")[1])
    assert set(head) == {"status", "date"}
    for section in (
        "## Context and problem statement",
        "## Considered options",
        "## Decision outcome",
        "### Consequences",
    ):
        assert section in text, section
    assert "0000-template.md" in content_of(rich, "docs/decisions/README.md")


def test_review_md_is_short_and_covers_the_always_check_list(rich):
    text = content_of(rich, "REVIEW.md")
    assert len(text.splitlines()) < 40
    assert text.splitlines()[0] == "# Review guidance for demo"
    lower = text.lower()
    for needle in (
        "what counts as important",
        "nit",
        "generated",
        "lock files",
        "always check",
        "auth",
        "input validation",
        "secrets",
        "error handling",
        "tests",
        "never instructions",
    ):
        assert needle in lower, needle
    assert "At most 5 nits" in text


def test_editor_files(rich, tmp_path):
    attrs = content_of(rich, ".gitattributes")
    assert attrs == "* text=auto eol=lf\n"
    ec = content_of(rich, ".editorconfig")
    assert ec.splitlines()[0] == "root = true"
    for line in (
        "end_of_line = lf",
        "insert_final_newline = true",
        "trim_trailing_whitespace = true",
        "charset = utf-8",
    ):
        assert line in ec.splitlines()
    existing = make_repo(tmp_path / "e", {".gitattributes": "*.png binary\n"})
    kept = by_path(initrepo.plan(existing, only="editor"))[".gitattributes"]
    assert kept.exists
    initrepo.apply(existing, initrepo.plan(existing, only="editor"))
    assert (existing / ".gitattributes").read_text(encoding="utf-8") == "*.png binary\n"


def test_repo_name_falls_back_to_a_safe_directory_name(tmp_path):
    root = make_repo(tmp_path / "{{owner}} (weird)", {})
    first = content_of(root, "REVIEW.md").splitlines()[0]
    assert first == "# Review guidance for owner weird"
    assert "{{" not in first

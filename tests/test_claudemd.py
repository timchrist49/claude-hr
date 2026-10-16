from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_hr import claudemd, config


@pytest.fixture(autouse=True)
def confine(tmp_path, monkeypatch):
    """Every test reads only the files below its own folder (`confine.root`).

    Claude Code also reads the CLAUDE.md files of every parent folder, and discover() walks them, so a report made under
    pytest's tmp_path (below the real home on Windows) includes the machine's own CLAUDE.md files: a contributor's long
    ~/.claude/CLAUDE.md or one with an unresolved import failed tests that are about something else, and an assertion
    that a finding exists passed on a finding from a file the test never wrote."""
    box = SimpleNamespace(root=tmp_path.resolve())
    real = claudemd._read

    def read(path: Path):
        try:
            p = Path(path).resolve()
        except OSError:
            return None
        return real(path) if p == box.root or box.root in p.parents else None

    monkeypatch.setattr(claudemd, "_read", read)
    return box


def lint(tmp_path: Path):
    return claudemd.lint(tmp_path, config.load())


def rules(rep):
    return {(f.rule, f.severity) for f in rep.findings}


def test_clean_file_has_no_failures(sandbox, tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "# Project\n- Run tests with `pytest -q`.\n- Use tabs for Makefiles.\n"
    )
    rep = lint(tmp_path)
    assert not [f for f in rep.findings if f.severity == "fail"]
    assert [s.path for s in rep.sources] == [(tmp_path / "CLAUDE.md").resolve()]


def test_files_outside_the_folder_are_not_read(sandbox, tmp_path, confine):
    """A broken CLAUDE.md in a parent folder is in the report when the root includes it, and is not when it does not."""
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / "CLAUDE.md").write_text("See @notes/missing.md. CRITICAL: you MUST ALWAYS NEVER.\n")
    (inner / "CLAUDE.md").write_text("# Project\n- Run tests with `pytest -q`.\n")
    confine.root = inner.resolve()
    rep = claudemd.lint(inner, config.load())
    assert [s.path for s in rep.sources] == [(inner / "CLAUDE.md").resolve()]
    assert rep.findings == []
    confine.root = (
        tmp_path.resolve()
    )  # the same folders with the root widened: the parent file is there
    wide = claudemd.lint(inner, config.load())
    assert len(wide.sources) == 2 and ("L8", "fail") in rules(wide)


def test_too_long_fails(sandbox, tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "\n".join(f"- rule number {i} about thing {i}" for i in range(260))
    )
    r = rules(lint(tmp_path))
    assert ("L1", "fail") in r
    assert ("L2", "fail") in r


def test_emphasis_tree_and_secret(sandbox, tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "CRITICAL: you MUST ALWAYS NEVER do X. IMPORTANT MANDATORY FORBIDDEN rules.\n"
        "├── src\n├── tests\n├── docs\n└── scripts\n"
        "token ghp_" + "a" * 36 + "\n",
        encoding="utf-8",
    )
    r = rules(lint(tmp_path))
    assert ("L4", "fail") in r
    assert ("L6", "fail") in r
    assert ("L13", "fail") in r


def test_dangling_import_and_missing_path(sandbox, tmp_path):
    (tmp_path / "CLAUDE.md").write_text("See @docs/missing.md and `src/nope/file.py`.\n")
    msgs = [f.message for f in lint(tmp_path).findings]
    assert any("does not resolve" in m for m in msgs)
    assert any("does not exist" in m for m in msgs)


def test_conflicting_package_managers(sandbox, tmp_path):
    (tmp_path / "CLAUDE.md").write_text("Use pnpm for installs.\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "CLAUDE.md").write_text("Always use yarn here.\n")
    rep = claudemd.lint(tmp_path / "sub", config.load())
    assert ("L9", "fail") in rules(rep)
    assert any("pnpm, yarn" in f.message for f in rep.findings if f.rule == "L9")


def test_path_scoped_rules_do_not_count_as_always_loaded(sandbox, tmp_path):
    rd = tmp_path / ".claude" / "rules"
    rd.mkdir(parents=True)
    (rd / "py.md").write_text(
        '---\npaths: "**/*.py"\n---\n' + "\n".join(f"- line {i}" for i in range(40))
    )
    rep = lint(tmp_path)
    src = next(s for s in rep.sources if s.path.name == "py.md")
    assert src.conditional
    assert sum(s.lines for s in rep.sources if not s.conditional) < 5


def test_local_file_must_be_gitignored(sandbox, tmp_path):
    (tmp_path / "CLAUDE.local.md").write_text("- my path is /home/me\n")
    (tmp_path / ".gitignore").write_text("node_modules\n")
    assert ("L13", "fail") in rules(lint(tmp_path))


def test_learned_block_budget(sandbox, tmp_path):
    body = "\n".join(f"- learned {i}" for i in range(60))
    block = f"<!-- headroom:learn:start -->\n{body}\n<!-- headroom:learn:end -->"
    (tmp_path / "CLAUDE.local.md").write_text(block)
    (tmp_path / ".gitignore").write_text("CLAUDE.local.md\n")
    assert any("auto-learned" in f.message for f in lint(tmp_path).findings)

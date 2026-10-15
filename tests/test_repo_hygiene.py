"""Guards against mistakes that only bite on another OS or at install time."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "__pycache__", ".venv", ".pytest_cache", ".ruff_cache", "dist", "build"}


def text_files():
    for p in ROOT.rglob("*"):
        if p.is_file() and not any(part in SKIP for part in p.parts) and p.suffix not in {".png", ".gif", ".ico", ".whl", ".pyc"}:
            yield p


def test_no_crlf_in_repo():
    bad = [str(p.relative_to(ROOT)) for p in text_files() if b"\r\n" in p.read_bytes()]
    assert not bad, f"CRLF line endings break shell scripts on Linux/macOS: {bad}"


def test_shell_scripts_have_shebang_and_no_bom():
    for p in ROOT.rglob("*.sh"):
        if any(part in SKIP for part in p.parts):
            continue
        head = p.read_bytes()[:3]
        assert head[:2] == b"#!", f"{p} lacks a shebang"


def test_plugin_and_marketplace_manifests_are_consistent():
    mk = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert mk["name"] == "claude-hr"
    for entry in mk["plugins"]:
        if isinstance(entry["source"], dict):  # third-party: must be pinned to a full commit SHA
            assert re.fullmatch(r"[0-9a-f]{40}", entry["source"].get("sha", "")), f"{entry['name']} is not SHA-pinned"
            continue
        src = ROOT / entry["source"]
        manifest = json.loads((src / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        assert manifest["name"] == entry["name"]
        assert manifest["version"] == entry["version"], "marketplace and plugin.json versions must match"


def test_versions_agree():
    init = (ROOT / "src" / "claude_hr" / "__init__.py").read_text(encoding="utf-8")
    py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'__version__ = "([^"]+)"', init).group(1) == re.search(r'(?m)^version = "([^"]+)"', py).group(1)


def test_every_skill_and_agent_has_frontmatter_description():
    for md in list((ROOT / "plugins").rglob("SKILL.md")) + list((ROOT / "plugins").glob("*/agents/*.md")):
        text = md.read_text(encoding="utf-8")
        assert text.startswith("---\n"), md
        fm = text.split("---", 2)[1]
        assert re.search(r"(?m)^description:\s*\S", fm), f"{md} has no description"
        assert re.search(r"(?m)^name:\s*\S", fm), f"{md} has no name"


def test_skill_and_agent_names_match_their_location():
    """A skill's frontmatter name must equal its directory, an agent's its file name (a rename once garbled one)."""
    for md in (ROOT / "plugins").rglob("SKILL.md"):
        name = re.search(r"(?m)^name:\s*(\S+)", md.read_text(encoding="utf-8").split("---", 2)[1]).group(1)
        assert name == md.parent.name, f"{md}: name {name!r} != directory {md.parent.name!r}"
    for md in (ROOT / "plugins").glob("*/agents/*.md"):
        name = re.search(r"(?m)^name:\s*(\S+)", md.read_text(encoding="utf-8").split("---", 2)[1]).group(1)
        assert name == md.stem, f"{md}: name {name!r} != file {md.stem!r}"


def test_agents_do_not_list_bash_command_filters_in_tools():
    """Claude Code ignores `Bash(git diff *)` in a subagent's tools list and grants the whole Bash tool; keep agents read-only."""
    for md in (ROOT / "plugins").glob("*/agents/*.md"):
        tools = re.search(r"(?m)^tools:\s*(.+)$", md.read_text(encoding="utf-8").split("---", 2)[1])
        assert tools and "Bash" not in tools.group(1), f"{md.name}: grants the Bash tool"


def test_side_effecting_skills_are_manual_only():
    for name in ("hr-status", "hr-parallel-worktrees"):
        fm = (ROOT / "plugins" / "hr-core" / "skills" / name / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[1]
        assert re.search(r"(?m)^disable-model-invocation:\s*true", fm), name


def test_no_stray_control_characters_in_text_files():
    """A scripted edit once turned a backslash-v in a path into a vertical tab; also catches crash dumps and binaries."""
    bad = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")
    found = [str(p.relative_to(ROOT)) for p in text_files() if bad.search(p.read_bytes())]
    assert not found, f"control characters in: {found}"


def test_relative_markdown_links_resolve():
    link = re.compile(r"\]\((?!https?:|mailto:|#)([^)#\s]+)(?:#[^)\s]*)?\)")
    broken = []
    for md in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        for target in link.findall(md.read_text(encoding="utf-8")):
            if not (md.parent / target).exists():
                broken.append(f"{md.relative_to(ROOT)} -> {target}")
    assert not broken, broken


def test_hook_commands_fail_open_when_hr_is_missing():
    hooks = json.loads((ROOT / "plugins" / "hr-core" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    for groups in hooks["hooks"].values():
        for g in groups:
            for h in g["hooks"]:
                assert "claude-hr-hook" in h["command"] and "exit 0" in h["command"], "must fail open when claude-hr is absent"
                assert h["timeout"] <= 15
            matchers = {g["matcher"] for g in groups}
            assert {"Bash", "PowerShell"} <= matchers


def test_license_is_present_and_consistent():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License\n") and "Copyright (c) 2026 timchrist49" in text
    assert "Permission is hereby granted, free of charge" in text  # untouched standard text, so GitHub detects it
    assert 'license = { text = "MIT" }' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    plugin = json.loads((ROOT / "plugins" / "hr-core" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["license"] == "MIT"

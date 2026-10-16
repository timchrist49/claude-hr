"""Guards against mistakes that only bite on another OS or at install time."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "__pycache__", ".venv", ".pytest_cache", ".ruff_cache", "dist", "build"}


def text_files():
    for p in ROOT.rglob("*"):
        if (
            p.is_file()
            and not any(part in SKIP for part in p.parts)
            and p.suffix not in {".png", ".gif", ".ico", ".whl", ".pyc"}
        ):
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
            assert re.fullmatch(r"[0-9a-f]{40}", entry["source"].get("sha", "")), (
                f"{entry['name']} is not SHA-pinned"
            )
            continue
        src = ROOT / entry["source"]
        manifest = json.loads((src / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        assert manifest["name"] == entry["name"]
        assert manifest["version"] == entry["version"], (
            "marketplace and plugin.json versions must match"
        )


def test_versions_agree():
    """__init__, pyproject, every plugin.json and the marketplace move together (CLAUDE.md: version bumps)."""
    init = (ROOT / "src" / "claude_hr" / "__init__.py").read_text(encoding="utf-8")
    py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'__version__ = "([^"]+)"', init).group(1)
    assert version == re.search(r'(?m)^version = "([^"]+)"', py).group(1)
    for manifest in (ROOT / "plugins").glob("*/.claude-plugin/plugin.json"):
        assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == version, manifest
    mk = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    own = [e for e in mk["plugins"] if isinstance(e["source"], str)]
    assert {e["name"] for e in own} == {p.name for p in (ROOT / "plugins").iterdir() if p.is_dir()}
    assert all(e["version"] == version for e in own)


def test_every_skill_and_agent_has_frontmatter_description():
    for md in list((ROOT / "plugins").rglob("SKILL.md")) + list(
        (ROOT / "plugins").glob("*/agents/*.md")
    ):
        text = md.read_text(encoding="utf-8")
        assert text.startswith("---\n"), md
        fm = text.split("---", 2)[1]
        assert re.search(r"(?m)^description:\s*\S", fm), f"{md} has no description"
        assert re.search(r"(?m)^name:\s*\S", fm), f"{md} has no name"


def test_skill_and_agent_names_match_their_location():
    """A skill's frontmatter name must equal its directory, an agent's its file name (a rename once garbled one)."""
    for md in (ROOT / "plugins").rglob("SKILL.md"):
        name = re.search(
            r"(?m)^name:\s*(\S+)", md.read_text(encoding="utf-8").split("---", 2)[1]
        ).group(1)
        assert name == md.parent.name, f"{md}: name {name!r} != directory {md.parent.name!r}"
    for md in (ROOT / "plugins").glob("*/agents/*.md"):
        name = re.search(
            r"(?m)^name:\s*(\S+)", md.read_text(encoding="utf-8").split("---", 2)[1]
        ).group(1)
        assert name == md.stem, f"{md}: name {name!r} != file {md.stem!r}"


def test_agents_do_not_list_bash_command_filters_in_tools():
    """Claude Code ignores `Bash(git diff *)` in a subagent's tools list and grants the whole Bash tool; keep agents read-only."""
    for md in (ROOT / "plugins").glob("*/agents/*.md"):
        tools = re.search(r"(?m)^tools:\s*(.+)$", md.read_text(encoding="utf-8").split("---", 2)[1])
        assert tools and "Bash" not in tools.group(1), f"{md.name}: grants the Bash tool"


# The only skill the model may pick by itself: it is cheap, read-only and what keeps CLAUDE.md small.
MODEL_INVOKED_SKILLS = {"hr-claudemd-hygiene"}


def skill_frontmatter(md: Path) -> str:
    return md.read_text(encoding="utf-8").split("---", 2)[1]


def test_every_skill_but_one_is_manual_only():
    """A manual-only skill (disable-model-invocation) is not in the skill listing: zero always-on tokens, and the
    model cannot start a side effect on its own. Add a model-invoked skill only by extending MODEL_INVOKED_SKILLS."""
    skills = {md.parent.name: md for md in (ROOT / "plugins").rglob("SKILL.md")}
    assert MODEL_INVOKED_SKILLS <= set(skills)
    for name, md in skills.items():
        manual = re.search(r"(?m)^disable-model-invocation:\s*true", skill_frontmatter(md))
        assert bool(manual) == (name not in MODEL_INVOKED_SKILLS), name


def test_skill_descriptions_are_short_and_calm():
    """Listed descriptions cost tokens on every turn, and Claude 5 over-triggers on shouted instructions."""
    for md in (ROOT / "plugins").rglob("SKILL.md"):
        fm = skill_frontmatter(md)
        desc = re.search(r"(?m)^description:\s*(.+)$", fm).group(1)
        assert len(desc) <= 300, f"{md.parent.name}: description is {len(desc)} characters"
        assert not re.search(r"\b(ALWAYS|NEVER|MUST|CRITICAL|IMPORTANT)\b", desc), md.parent.name
        assert ": " not in desc and " #" not in desc, f"{md.parent.name}: not a safe YAML scalar"


def test_hr_gates_is_one_silent_stop_hook_and_nothing_else():
    """The gate plugin must cost no tokens (no skills, agents, commands) and one process per turn."""
    base = ROOT / "plugins" / "hr-gates"
    assert not list(base.rglob("SKILL.md")) and not (base / "agents").exists()
    assert not (base / "commands").exists() and not (base / ".mcp.json").exists()
    hooks = json.loads((base / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert list(hooks) == ["Stop"], "any other event would spawn a process per tool call or prompt"
    (group,) = hooks["Stop"]
    (hook,) = group["hooks"]
    assert hook["type"] == "command" and hook["command"] == "claude-hr-hook"
    assert hook["args"] == ["stop"], (
        "exec form: no shell, so it also works under PowerShell and cmd"
    )
    assert 60 <= hook["timeout"] <= 180


def test_our_plugins_stay_inside_the_always_on_token_budget():
    """The admission rule in docs/ADDONS.md, measured with the same estimator `claude-hr-ctl footprint` uses."""
    from claude_hr import footprint

    entries = [
        {"id": f"{p.name}@claude-hr", "installPath": str(p), "enabled": True, "scope": "user"}
        for p in sorted((ROOT / "plugins").iterdir())
        if p.is_dir()
    ]
    fps = {f.name.split("@")[0]: f for f in footprint.scan_installed(entries)}
    assert fps["hr-gates"].always_on_tokens_est == 0
    assert fps["hr-core"].always_on_tokens_est <= 250
    assert not any(f.injects_context for f in fps.values()), "our hooks must not add context"


def test_the_hook_binary_is_called_by_name_with_no_shell_in_hr_gates():
    cmd = json.loads(
        (ROOT / "plugins" / "hr-gates" / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"]["Stop"][0]["hooks"][0]
    assert "sh -c" not in cmd["command"] and "powershell" not in cmd["command"].lower()


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
    hooks = json.loads(
        (ROOT / "plugins" / "hr-core" / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )
    for groups in hooks["hooks"].values():
        for g in groups:
            for h in g["hooks"]:
                assert "claude-hr-hook" in h["command"] and "exit 0" in h["command"], (
                    "must fail open when claude-hr is absent"
                )
                assert h["timeout"] <= 15
            matchers = {g["matcher"] for g in groups}
            assert {"Bash", "PowerShell"} <= matchers


def test_license_is_present_and_consistent():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License\n") and "Copyright (c) 2026 timchrist49" in text
    assert (
        "Permission is hereby granted, free of charge" in text
    )  # untouched standard text, so GitHub detects it
    assert 'license = { text = "MIT" }' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    plugin = json.loads(
        (ROOT / "plugins" / "hr-core" / ".claude-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    assert plugin["license"] == "MIT"

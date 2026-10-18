"""Guards against mistakes that only bite on another OS or at install time."""

import json
import re
from pathlib import Path

import pytest

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


def test_ecc_is_not_wired_into_any_profile_or_the_marketplace():
    """Everything Claude Code (affaan-m/ECC) was reviewed and rejected (docs/ADDONS.md). This keeps a later edit from adding
    it without a decision: relax the test only together with a reviewed change to the admission rules (rule 6). It looks at
    the plugins of every profile, [project_scoped], the marketplaces the plan adds, and every marketplace.json entry. The
    [redundant] table is deliberately excluded: that is where an installed ECC gets flagged."""
    import tomllib

    plan = tomllib.loads(
        (ROOT / "src" / "claude_hr" / "data" / "plugins.toml").read_text(encoding="utf-8")
    )
    ids = [
        pid for profile in plan.get("profiles", {}).values() for pid in profile.get("plugins", [])
    ]
    ids += list(plan.get("project_scoped", {}))
    mk = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    texts = [pid.lower() for pid in ids] + [json.dumps(entry).lower() for entry in mk["plugins"]]
    # a marketplace the plan adds (name and source) is how a plugin from it would be installed
    sources = plan.get("marketplaces", {})
    texts += [json.dumps({name: spec}).lower() for name, spec in sources.items()]
    assert len(ids) >= 5 and mk["plugins"] and sources  # the parse found something to look at
    banned = re.compile(r"(?<![a-z0-9])ecc(?![a-z0-9])|everything-claude-code|affaan-m")
    hits = [t[:80] for t in texts if banned.search(t)]
    assert not hits, (
        f"ECC appears in a profile or the marketplace: {hits}. It was reviewed and rejected; change this only through a "
        "deliberate, reviewed decision (docs/ADDONS.md, admission rule 6)."
    )


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


# What a skill may pre-approve (its `allowed-tools` skips the permission prompt while the skill is active, so an injected
# agent inherits it). Only verbs that read state, or run the repository's own checks (`verify run` refuses an untrusted
# recipe from an agent shell). Never: config, setup, update, uninstall, cleanup, schedule, shim, wt, workflows,
# init-repo, creds, verify trust/untrust/init, monitor, job.
READ_ONLY_CTL_VERBS = {
    "status",
    "doctor",
    "usage",
    "plugins audit",
    "plugins list",
    "claudemd lint",
    "skills",
    "audit",
    "verify status",
    "verify run",
    "fmt --check",
}
# Verbs that are read-only only WITHOUT their flags: `Bash(claude-hr-ctl trail *)` would also pre-approve `trail --prune`
# (deletes audit files) and `footprint --probe` (runs a real `claude -p` and spends tokens). A trailing ` *` cannot exclude
# a flag, so these may be pre-approved only as the exact, argument-free command `Bash(claude-hr-ctl trail)`.
READ_ONLY_CTL_BARE = {"trail", "footprint"}
READ_ONLY_GH = {"gh issue view", "gh issue list", "gh pr view", "gh pr diff", "gh pr checks"}


def _unquote(item: str) -> str:
    item = item.strip()
    if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'":
        return item[1:-1].strip()
    return item


def allowed_tools_value(front: str) -> str | None:
    """A skill's `allowed-tools` as one space-joined string, whichever YAML form it is written in: an inline value, a flow
    list `[a, b]`, a block list (`- a` lines, indented or not) or a block scalar (`>` / `|`). None when the key is absent.
    The check below used to read only the text after `allowed-tools:` on the same line, so the block-list form
    (`allowed-tools:` then `  - Bash(claude-hr-ctl *)`) looked empty and passed."""
    lines = front.splitlines()
    for at, line in enumerate(lines):
        m = re.match(r"allowed-tools:[ \t]*(.*?)[ \t]*$", line)
        if not m:
            continue
        value, items = m.group(1), []
        if value.startswith("["):  # flow list: split on the commas outside (...)
            depth, buf = 0, ""
            for ch in value.strip("[] \t"):
                depth += (ch == "(") - (ch == ")")
                if ch == "," and depth == 0:
                    items.append(buf)
                    buf = ""
                else:
                    buf += ch
            items.append(buf)
        elif value and not re.fullmatch(r"[>|][+-]?", value):
            items.append(value)
        for nxt in lines[at + 1 :]:
            if nxt.strip() and not re.match(r"[ \t]|-([ \t]|$)", nxt):
                break  # the next key
            text = nxt.strip()
            if not text or text.startswith("#"):
                continue
            items.append(text[1:] if text.startswith("-") else text)
        return " ".join(x for x in (_unquote(i) for i in items) if x)
    return None


def allowed_tools_problems(value: str) -> list[str]:
    """Why a skill's `allowed-tools` is broader than a fixed read-only command prefix (empty = fine)."""
    problems = []
    leftover = re.sub(r"Bash\([^)]*\)", "", value).strip()
    if leftover:
        problems.append(f"not a Bash(<prefix> *) entry: {leftover!r}")
    for entry in re.findall(r"Bash\(([^)]*)\)", value):
        if entry in {f"claude-hr-ctl {verb}" for verb in READ_ONLY_CTL_BARE}:
            continue  # exactly the argument-free command: no flag can be added to it
        if not entry.endswith(" *") or re.search(r"[*?;&|<>`$()]", entry[:-2]):
            problems.append(f"Bash({entry}) is not a plain `<command prefix> *` pattern")
            continue
        prefix = entry[:-2]
        if prefix.startswith("claude-hr-ctl "):
            if prefix[len("claude-hr-ctl ") :] not in READ_ONLY_CTL_VERBS:
                problems.append(
                    f"Bash({entry}) pre-approves a verb that is not on the read-only list"
                )
        elif prefix not in READ_ONLY_GH:
            problems.append(f"Bash({entry}) is not a read-only claude-hr-ctl or gh command")
    return problems


def test_allowed_tools_check_rejects_broad_grants():
    for bad in (
        "Bash(claude-hr-ctl *)",
        "Bash(*)",
        "Bash",
        "Write Edit",
        "Bash(claude-hr-ctl config set *)",
        "Bash(claude-hr-ctl config *)",
        "Bash(claude-hr-ctl verify trust *)",
        "Bash(claude-hr-ctl status *) Bash(claude-hr-ctl update *)",
        "Bash(claude-hr-ctl status)",
        "Bash(claude-hr-ctl status; rm -rf * *)",
        "Bash(gh pr create *)",
        "Bash(gh api *)",
        "Bash(gh *)",
        # read-only only without their flags (`trail --prune` deletes files, `footprint --probe` spends tokens)
        "Bash(claude-hr-ctl trail *)",
        "Bash(claude-hr-ctl footprint *)",
        "Bash(claude-hr-ctl trail --prune *)",
        "Bash(claude-hr-ctl trail --tail 5 *)",
        "Bash(claude-hr-ctl trail --prune)",
        "Bash(claude-hr-ctl cleanup *)",
        "Bash(claude-hr-ctl usage --snapshot *)",
        "Bash(claude-hr-ctl status)",
    ):
        assert allowed_tools_problems(bad), bad
    for good in (
        "",
        "Bash(claude-hr-ctl status *) Bash(claude-hr-ctl doctor *)",
        "Bash(claude-hr-ctl fmt --check *)",
        "Bash(gh issue view *) Bash(gh pr checks *)",
        "Bash(claude-hr-ctl plugins audit *) Bash(claude-hr-ctl usage *)",
        "Bash(claude-hr-ctl trail) Bash(claude-hr-ctl footprint)",  # exactly the argument-free form
    ):
        assert not allowed_tools_problems(good), good


FRONT = "name: x\ndescription: d\n"


@pytest.mark.parametrize(
    "block, expected",
    [
        ("", None),
        ("allowed-tools:\n", ""),
        (
            "allowed-tools: Bash(claude-hr-ctl status *) Bash(claude-hr-ctl doctor *)\n",
            "Bash(claude-hr-ctl status *) Bash(claude-hr-ctl doctor *)",
        ),
        (
            "allowed-tools: [Bash(claude-hr-ctl status *), 'Bash(claude-hr-ctl doctor *)']\n",
            "Bash(claude-hr-ctl status *) Bash(claude-hr-ctl doctor *)",
        ),
        (
            'allowed-tools:\n  - Bash(claude-hr-ctl status *)\n  - "Bash(claude-hr-ctl doctor *)"\n',
            "Bash(claude-hr-ctl status *) Bash(claude-hr-ctl doctor *)",
        ),
        (
            "allowed-tools:\n- Bash(claude-hr-ctl status *)\n- Write\n",
            "Bash(claude-hr-ctl status *) Write",
        ),
        (
            "allowed-tools:\n  # a comment\n  - Bash(claude-hr-ctl status *)\n\n  - Edit\n",
            "Bash(claude-hr-ctl status *) Edit",
        ),
        (
            "allowed-tools: >\n  Bash(claude-hr-ctl status *)\n  Bash(claude-hr-ctl doctor *)\n",
            "Bash(claude-hr-ctl status *) Bash(claude-hr-ctl doctor *)",
        ),
        (
            "allowed-tools:\n  - Bash(claude-hr-ctl status *)\ndisable-model-invocation: true\n",
            "Bash(claude-hr-ctl status *)",
        ),
    ],
)
def test_allowed_tools_value_reads_every_yaml_form(block, expected):
    assert allowed_tools_value(FRONT + block) == expected


@pytest.mark.parametrize(
    "block",
    [
        "allowed-tools:\n  - Bash(claude-hr-ctl *)\n  - Bash(*)\n  - Write\n",
        "allowed-tools:\n- Bash(claude-hr-ctl update *)\n- Bash(claude-hr-ctl setup *)\n",
        "allowed-tools: [Bash(claude-hr-ctl *)]\n",
        "allowed-tools: ['Bash(claude-hr-ctl *)', Write]\n",
        "allowed-tools: >\n  Bash(claude-hr-ctl *)\n",
        "allowed-tools:\n  - Bash(claude-hr-ctl trail *)\n",
        "allowed-tools: Bash(claude-hr-ctl *)\n",
    ],
)
def test_a_broad_grant_is_caught_in_every_yaml_form(block):
    """The block-list form used to read as an empty value and pass."""
    assert allowed_tools_problems(allowed_tools_value(FRONT + block)), block


def test_every_skills_allowed_tools_is_an_explicit_read_only_list():
    """hr-status once pre-approved `Bash(claude-hr-ctl *)`: after /hr-core:hr-status a prompt-injected agent could run
    `config set`, `update` or `setup` without a prompt."""
    seen = 0
    for md in (ROOT / "plugins").rglob("SKILL.md"):
        value = allowed_tools_value(skill_frontmatter(md))
        if value is None:
            continue
        seen += 1
        assert value, f"{md.parent.name}: an allowed-tools key with no entries"
        assert not allowed_tools_problems(value), (md.parent.name, allowed_tools_problems(value))
    assert seen >= 4, "the skills that pre-approve commands were not found"


def test_hr_status_pre_approves_exactly_status_and_doctor():
    fm = skill_frontmatter(ROOT / "plugins" / "hr-core" / "skills" / "hr-status" / "SKILL.md")
    assert allowed_tools_value(fm) == "Bash(claude-hr-ctl status *) Bash(claude-hr-ctl doctor *)"


def test_manual_only_skills_do_not_tell_the_model_to_start_each_other():
    """The Skill tool refuses a skill with disable-model-invocation, so `Then run /hr-core:hr-done` addressed to the model
    is dead text (it falls back to ad-hoc commands without the skill's allowed-tools or its untrusted-data recipe).
    A reference must address the user (ask / tell / they) or only point at the skill (see)."""
    reference = re.compile(r"/hr-core:hr-[a-z-]+")
    addressed = re.compile(r"(?i)\b(ask|tell|user|they|see)\b")
    for md in (ROOT / "plugins").rglob("SKILL.md"):
        body = md.read_text(encoding="utf-8").split("---", 2)[2]
        for line in body.splitlines():
            if reference.search(line):
                assert addressed.search(line), (
                    f"{md.parent.name}: tells the model to start a skill: {line!r}"
                )


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


def test_our_hooks_add_no_context_and_hr_gates_costs_nothing():
    """Admission rule 4 in docs/ADDONS.md, measured with the same estimator `claude-hr-ctl footprint` uses. There is no cap on
    how many tokens hr-core's skills may use (rule 5: measure, and justify what they add); a hook that injects text on every
    turn or start, and a gate that is not silent, would be a design change, so those stay pinned."""
    from claude_hr import footprint

    entries = [
        {"id": f"{p.name}@claude-hr", "installPath": str(p), "enabled": True, "scope": "user"}
        for p in sorted((ROOT / "plugins").iterdir())
        if p.is_dir()
    ]
    fps = {f.name.split("@")[0]: f for f in footprint.scan_installed(entries)}
    assert fps["hr-gates"].always_on_tokens_est == 0
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


def test_markdown_table_rows_have_the_headers_cell_count():
    """A literal backslash-n once glued two rows of the SECURITY table together; the second row vanished when rendered."""

    def cells(line: str) -> int:
        return (
            len(re.split(r"(?<!\\)\|", line.strip())) - 2
        )  # drop the empty parts before the first and after the last |

    bad = []
    for md in [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "docs").glob("*.md"))]:
        width = None
        for no, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
            if not line.startswith("|"):
                width = None
                continue
            if width is None:
                width = cells(line)
            elif cells(line) != width:
                bad.append(
                    f"{md.relative_to(ROOT)}:{no} has {cells(line)} cells, the table has {width}"
                )
    assert not bad, bad

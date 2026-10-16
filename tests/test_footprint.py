import json
import math
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from claude_hr import footprint, procutil
from claude_hr.footprint import PluginFootprint


def put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def put_json(path: Path, data) -> Path:
    return put(path, json.dumps(data))


# ---- fixture plugins modelled on real ones -----------------------------------------------------------------------
ALPHA_DESC = "Use for alpha work."
ALPHA_WHEN = "When alpha is needed."
BETA_FOLDED = "First line of beta second line."  # what the folded block below turns into
AGENT_DESC = "Reviews code carefully."
EXPLORER_DESC = 'Explores "things".'  # written with JSON escapes in the frontmatter
SHIP_DESC = "Ship it"
DEPLOY_FALLBACK = "Deploy the app now."  # first paragraph of a command without frontmatter

RICH_CHARS = (
    len("alpha")
    + len(ALPHA_DESC)
    + len(ALPHA_WHEN)
    + len("beta")
    + len(BETA_FOLDED)
    + len("reviewer")
    + len(AGENT_DESC)
    + len("explorer")
    + len(EXPLORER_DESC)
    + len("ship")
    + len(SHIP_DESC)
    + len("deploy")
    + len(DEPLOY_FALLBACK)
)

INJECT_PY = textwrap.dedent(
    """\
    import json, os
    root = os.environ["CLAUDE_PLUGIN_ROOT"]
    text = open(os.path.join(root, "context/rules.md")).read()
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    """
)


def make_rich_plugin(tmp_path: Path, name: str = "rich") -> Path:
    """3 skills (one manual-only), 2 agents, 2 commands, a SessionStart hook that injects a file, 2 MCP servers."""
    root = tmp_path / name
    put_json(root / ".claude-plugin" / "plugin.json", {"name": "rich", "version": "1.2.3"})
    put(
        root / "skills" / "alpha" / "SKILL.md",
        f"---\nname: alpha\ndescription: {ALPHA_DESC}\nwhen_to_use: {ALPHA_WHEN}\n---\n\n# Alpha\nBody.\n",
    )
    put(
        root / "skills" / "beta" / "SKILL.md",
        "---\nname: beta\ndescription: >\n  First line of beta\n  second line.\n---\nBody\n",
    )
    put(
        root / "skills" / "gamma" / "SKILL.md",
        "---\nname: gamma\ndescription: "
        + "G" * 300
        + "\ndisable-model-invocation: true\n---\nBody\n",
    )
    put(
        root / "agents" / "reviewer.md",
        f"---\nname: reviewer\ndescription: {AGENT_DESC}\n---\nYou review.\n",
    )
    put(
        root / "agents" / "explorer.md",
        '---\ndescription: "Explores \\"things\\"."\n---\nYou explore.\n',
    )
    put(root / "commands" / "ship.md", f"---\ndescription: {SHIP_DESC}\n---\nRun the ship steps.\n")
    put(
        root / "commands" / "ns" / "deploy.md",
        f"{DEPLOY_FALLBACK}\n\nMore text that is not counted.\n",
    )
    put(root / "context" / "rules.md", "x" * 400)  # 100 tokens
    put(root / "hooks" / "inject.py", INJECT_PY)
    put(root / "hooks" / "fmt.sh", '#!/bin/sh\nruff format "$@"\n')
    put_json(
        root / "hooks" / "hooks.json",
        {
            "description": "rich hooks",
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/inject.py"',
                            }
                        ],
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/fmt.sh"',
                            }
                        ],
                    }
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "claude-hr-hook stop"}]}],
            },
        },
    )
    put_json(
        root / ".mcp.json",
        {
            "mcpServers": {
                "docs": {"command": "npx", "args": ["-y", "docs-server"]},
                "search": {"type": "http", "url": "https://example.invalid/mcp"},
            }
        },
    )
    return root


def hook_plugin(
    tmp_path: Path,
    event: str,
    command: str | None,
    files: dict | None = None,
    hook_type: str = "command",
) -> Path:
    root = tmp_path / "hp"
    for rel, text in (files or {}).items():
        put(root / rel, text)
    entry: dict = {"type": hook_type}
    if command is not None:
        entry["command"] = command
    put_json(root / "hooks" / "hooks.json", {"hooks": {event: [{"hooks": [entry]}]}})
    return root


# ---- estimates and frontmatter -------------------------------------------------------------------------------------
def test_estimate_tokens_rounds_up():
    assert [footprint.estimate_tokens(n) for n in (0, 1, 4, 5, 8, 9)] == [0, 1, 1, 2, 2, 3]
    assert footprint.estimate_tokens(-3) == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "---\nname: a\ndescription: plain text\n---\nbody",
            {"name": "a", "description": "plain text"},
        ),
        ('---\ndescription: "quoted: yes \\"x\\""\n---\n', {"description": 'quoted: yes "x"'}),
        ("---\ndescription: 'it''s'\n---\n", {"description": "it's"}),
        (
            "---\ndescription: >-\n  folded\n  lines\n\n  more\n---\n",
            {"description": "folded lines more"},
        ),
        ("---\ndescription: |\n  one\n  two\n---\n", {"description": "one\ntwo"}),
        ("---\ndescription: a\n  continued\n---\n", {"description": "a continued"}),
        (
            "---\r\nname: crlf\r\ndescription: win\r\n---\r\nbody\r\n",
            {"name": "crlf", "description": "win"},
        ),
        ("\ufeff---\nname: bom\n---\n", {"name": "bom"}),
        (
            "---\nWhen_To_Use: caps\ndisable-model-invocation: true\n---\n",
            {"when_to_use": "caps", "disable-model-invocation": "true"},
        ),
        ("no frontmatter at all", {}),
        ("---\nunterminated: true\n", {}),
    ],
)
def test_frontmatter_parsing(text, expected):
    assert footprint._frontmatter(text)[0] == expected


def test_frontmatter_returns_body():
    assert footprint._frontmatter("---\nname: a\n---\nthe body\n")[1] == "the body\n"


def test_skill_listing_text_is_capped(tmp_path):
    root = tmp_path / "long"
    put(root / "skills" / "wordy" / "SKILL.md", "---\ndescription: " + "w" * 5000 + "\n---\n")
    fp = footprint.scan_plugin_dir(root)
    assert fp.description_tokens_est == math.ceil((len("wordy") + 1536) / 4)


# ---- scan_plugin_dir -----------------------------------------------------------------------------------------------
def test_scan_rich_plugin(tmp_path):
    fp = footprint.scan_plugin_dir(make_rich_plugin(tmp_path))
    assert (fp.name, fp.version) == ("rich", "1.2.3")
    assert (fp.skills, fp.manual_only_skills, fp.agents, fp.commands) == (3, 1, 2, 2)
    # arithmetic: one ceil over the characters of every non-manual-only item
    assert fp.description_tokens_est == math.ceil(RICH_CHARS / 4)
    assert fp.hook_events == ["SessionStart", "PostToolUse", "Stop"]
    assert fp.injects_context is True
    assert fp.injected_tokens_est == 100
    assert fp.always_on_tokens_est == fp.description_tokens_est + 100
    assert fp.mcp_servers == 2
    assert "SessionStart hook injects context/rules.md (about 100 tokens)" in fp.notes
    assert any("2 MCP server(s)" in n for n in fp.notes)
    procs = next(n for n in fp.notes if n.startswith("hook processes"))
    assert "SessionStart x1, PostToolUse x1, Stop x1" in procs


def test_manual_only_skill_description_is_not_counted(tmp_path):
    root = tmp_path / "m"
    put(root / "skills" / "a" / "SKILL.md", "---\nname: a\ndescription: seen\n---\n")
    put(
        root / "skills" / "b" / "SKILL.md",
        "---\nname: b\ndescription: "
        + "hidden " * 50
        + '\ndisable-model-invocation: "True"\n---\n',
    )
    fp = footprint.scan_plugin_dir(root)
    assert (fp.skills, fp.manual_only_skills) == (2, 1)
    assert fp.description_tokens_est == math.ceil((len("a") + len("seen")) / 4)


def test_manual_only_command_is_noted_not_counted(tmp_path):
    root = tmp_path / "m"
    put(root / "commands" / "x.md", "---\ndescription: shown\n---\n")
    put(
        root / "commands" / "y.md",
        "---\ndescription: hidden\ndisable-model-invocation: true\n---\n",
    )
    fp = footprint.scan_plugin_dir(root)
    assert fp.commands == 2
    assert fp.description_tokens_est == math.ceil((len("x") + len("shown")) / 4)
    assert any("1 command(s) are manual-only" in n for n in fp.notes)


def test_skill_without_name_uses_folder_and_without_description_uses_body(tmp_path):
    root = tmp_path / "m"
    put(
        root / "skills" / "folder-name" / "SKILL.md",
        "# Title\n\nFirst paragraph\nsecond line.\n\nSecond paragraph.\n",
    )
    fp = footprint.scan_plugin_dir(root)
    assert fp.skills == 1
    assert fp.description_tokens_est == math.ceil(
        (len("folder-name") + len("Title")) / 4  # the heading is the first paragraph here
    )


def test_hooks_only_plugin(tmp_path):
    root = tmp_path / "gates"
    put(root / "hooks" / "format.sh", "#!/bin/sh\nruff format .\n")
    put_json(
        root / "hooks" / "hooks.json",
        {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Edit",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/format.sh"',
                            }
                        ],
                    }
                ]
            }
        },
    )
    fp = footprint.scan_plugin_dir(root)
    assert fp.name == "gates"
    assert (fp.skills, fp.agents, fp.commands, fp.mcp_servers) == (0, 0, 0, 0)
    assert fp.description_tokens_est == 0
    assert fp.hook_events == ["PostToolUse"]
    assert fp.injects_context is False
    assert fp.injected_tokens_est == 0
    assert not any("nothing found" in n for n in fp.notes)


def test_empty_plugin(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    fp = footprint.scan_plugin_dir(root)
    assert fp == PluginFootprint(
        name="empty", notes=["nothing found (empty plugin or an unsupported layout)"]
    )
    assert fp.injects_context is False
    assert fp.hook_events == []


def test_missing_directory_is_a_note(tmp_path):
    fp = footprint.scan_plugin_dir(tmp_path / "does-not-exist")
    assert fp.notes == ["install path not found"]
    assert fp.description_tokens_est == 0


def test_cache_layout_name_and_version_without_manifest(tmp_path):
    root = tmp_path / "cache" / "market" / "superlike" / "6.4.1"
    put(root / "skills" / "s" / "SKILL.md", "---\ndescription: d\n---\n")
    fp = footprint.scan_plugin_dir(root)
    assert (fp.name, fp.version) == ("superlike", "6.4.1")


def test_garbage_files_never_raise(tmp_path):
    root = tmp_path / "bad"
    put(root / ".claude-plugin" / "plugin.json", "{not json")
    put(root / "hooks" / "hooks.json", "[1, 2, 3]")
    put(root / ".mcp.json", '"just a string"')
    (root / "skills" / "bin").mkdir(parents=True)
    (root / "skills" / "bin" / "SKILL.md").write_bytes(b"\xff\xfe\x00 not utf-8 \x80")
    put(root / "agents" / "ok.md", "---\nname: ok\ndescription: fine\n---\n")
    fp = footprint.scan_plugin_dir(root)
    assert fp.agents == 1
    assert fp.skills == 1
    assert fp.hook_events == []
    assert fp.mcp_servers == 0


def test_hooks_json_with_odd_shapes(tmp_path):
    root = tmp_path / "odd"
    put_json(
        root / "hooks" / "hooks.json",
        {
            "hooks": {
                "SessionStart": "not a list",
                "Stop": [None, 5, {"hooks": "nope"}, {"hooks": [7, {"type": "command"}]}],
                "PreToolUse": [{"command": "echo flat"}],
            }
        },
    )
    fp = footprint.scan_plugin_dir(root)
    # Stop only has an entry without a command: nothing would run, so the event is not listed
    assert fp.hook_events == ["PreToolUse"]
    assert fp.injects_context is None  # an inline command we cannot inspect


def test_deeply_nested_json_does_not_raise(tmp_path):
    root = tmp_path / "deep"
    nested = "[" * 200_000 + "]" * 200_000
    put(root / ".claude-plugin" / "plugin.json", nested)
    put(root / "hooks" / "hooks.json", nested)
    put(root / ".mcp.json", nested)
    fp = footprint.scan_plugin_dir(root)
    assert fp.hook_events == [] and fp.mcp_servers == 0


@pytest.mark.parametrize(
    "script",
    ["cat " * 150_000, "cat " * 100_000 + ".md", ("cat x " * 300 + ".md\n") * 100],
    ids=["one-huge-line", "huge-line-ending-in-md", "many-long-lines-mentioning-md"],
)
def test_huge_hook_scripts_stay_fast(tmp_path, script):
    """Minified bundles must not make the regexes quadratic."""
    root = hook_plugin(
        tmp_path,
        "SessionStart",
        'node "${CLAUDE_PLUGIN_ROOT}/hooks/big.js"',
        {"hooks/big.js": script},
    )
    started = time.perf_counter()
    fp = footprint.scan_plugin_dir(root)
    assert time.perf_counter() - started < 5
    assert fp.injects_context in (True, False)  # inspected, whatever the verdict


def test_plugin_json_custom_paths(tmp_path):
    root = tmp_path / "custom"
    put_json(
        root / ".claude-plugin" / "plugin.json",
        {
            "name": "custom",
            "skills": ["./extra-skills"],
            "commands": ["./misc/one.md"],
            "agents": "./more-agents",
            "mcpServers": {"inline": {"command": "x"}},
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        },
    )
    put(root / "extra-skills" / "s1" / "SKILL.md", "---\ndescription: one\n---\n")
    put(root / "misc" / "one.md", "---\ndescription: cmd\n---\n")
    put(root / "more-agents" / "a.md", "---\ndescription: agent\n---\n")
    fp = footprint.scan_plugin_dir(root)
    assert (fp.skills, fp.commands, fp.agents, fp.mcp_servers) == (1, 1, 1, 1)
    assert fp.hook_events == ["SessionStart"]


def test_plugin_json_paths_cannot_escape_the_plugin(tmp_path):
    outside = tmp_path / "outside"
    put(outside / "s" / "SKILL.md", "---\ndescription: secret\n---\n")
    root = tmp_path / "p"
    put_json(root / ".claude-plugin" / "plugin.json", {"name": "p", "skills": ["../outside"]})
    assert footprint.scan_plugin_dir(root).skills == 0


@pytest.mark.parametrize(
    ("mcp", "count"),
    [
        ({"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}, 2),
        ({"a": {"command": "x"}, "b": {"url": "u"}, "c": {"command": "z"}}, 3),  # flat form
        ({"mcpServers": {}}, 0),
        ({"mcpServers": {"a": "not a dict"}}, 0),
    ],
)
def test_mcp_server_counting(tmp_path, mcp, count):
    root = tmp_path / "mcp"
    put_json(root / ".mcp.json", mcp)
    assert footprint.scan_plugin_dir(root).mcp_servers == count


def test_mcp_servers_in_manifest_and_file_are_deduplicated(tmp_path):
    root = tmp_path / "mcp"
    put_json(
        root / ".mcp.json",
        {"mcpServers": {"shared": {"command": "x"}, "only-file": {"command": "y"}}},
    )
    put_json(
        root / ".claude-plugin" / "plugin.json",
        {
            "name": "mcp",
            "mcpServers": {"shared": {"command": "x"}, "only-manifest": {"command": "z"}},
        },
    )
    assert footprint.scan_plugin_dir(root).mcp_servers == 3


def test_mcp_servers_manifest_may_point_to_a_file(tmp_path):
    root = tmp_path / "mcp"
    put_json(root / "servers.json", {"mcpServers": {"a": {"command": "x"}}})
    put_json(
        root / ".claude-plugin" / "plugin.json", {"name": "mcp", "mcpServers": "./servers.json"}
    )
    assert footprint.scan_plugin_dir(root).mcp_servers == 1


# ---- injects_context ---------------------------------------------------------------------------------------------
CLEAN_SH = "#!/bin/sh\necho formatting\n"
CAT_SH = '#!/bin/sh\nDIR="$(dirname "$0")"\ncat "$DIR/../notes.md"\n'


@pytest.mark.parametrize(
    ("event", "command", "files", "expected"),
    [
        pytest.param(
            "SessionStart",
            'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/a.py"',
            {"hooks/a.py": 'print(\'{"hookSpecificOutput": {"additionalContext": "x"}}\')'},
            True,
            id="script-emits-hookSpecificOutput",
        ),
        pytest.param(
            "UserPromptSubmit",
            "python3 $CLAUDE_PLUGIN_ROOT/hooks/a.py",
            {"hooks/a.py": "print(json.dumps({'additionalContext': 'x'}))"},
            True,
            id="user-prompt-additionalContext",
        ),
        pytest.param(
            "PreToolUse",
            'sh "${CLAUDE_PLUGIN_ROOT}/hooks/c.sh"',
            {"hooks/c.sh": CAT_SH, "notes.md": "n" * 40},
            True,
            id="script-cats-markdown",
        ),
        pytest.param(
            "SessionStart",
            'cat "${CLAUDE_PLUGIN_ROOT}/NOTES.md"',
            {"NOTES.md": "n" * 40},
            True,
            id="inline-cat-markdown",
        ),
        pytest.param("SessionStart", "echo hello", None, None, id="inline-command-unknown"),
        pytest.param(
            "SessionStart",
            'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/missing.py"',
            None,
            None,
            id="missing-script",
        ),
        pytest.param(
            "SessionStart",
            "python3 -m some.module",
            None,
            None,
            id="module-not-a-file",
        ),
        pytest.param(
            "SessionStart",
            'sh "${CLAUDE_PLUGIN_ROOT}/hooks/clean.sh"',
            {"hooks/clean.sh": CLEAN_SH},
            False,
            id="inspected-clean-script",
        ),
        pytest.param(
            "PostToolUse",
            'sh "${CLAUDE_PLUGIN_ROOT}/hooks/clean.sh"',
            {"hooks/clean.sh": CLEAN_SH},
            False,
            id="no-context-capable-hook",
        ),
        pytest.param(
            "Stop",
            "echo bye",
            None,
            False,
            id="uninspectable-but-not-context-capable-event",
        ),
        pytest.param(
            "PreToolUse",
            "sh -c 'command -v claude-hr-hook >/dev/null 2>&1 || exit 0; exec claude-hr-hook bash-guard'",
            None,
            False,
            id="claude-hr-own-hook",
        ),
        pytest.param("SessionStart", None, None, None, id="http-hook-cannot-be-inspected"),
    ],
)
def test_injects_context(tmp_path, event, command, files, expected):
    kind = "http" if command is None else "command"
    root = hook_plugin(tmp_path, event, command, files, hook_type=kind)
    assert footprint.scan_plugin_dir(root).injects_context is expected


def test_prompt_hooks_are_noted_and_not_context(tmp_path):
    root = hook_plugin(tmp_path, "SessionStart", None, hook_type="prompt")
    fp = footprint.scan_plugin_dir(root)
    assert fp.injects_context is False
    assert any("1 prompt/agent hook(s)" in n for n in fp.notes)


def test_hook_outside_context_events_that_emits_context_is_noted(tmp_path):
    root = hook_plugin(
        tmp_path,
        "PostToolUse",
        'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/w.py"',
        {"hooks/w.py": "out = {'hookSpecificOutput': {'additionalContext': 'warn'}}"},
    )
    fp = footprint.scan_plugin_dir(root)
    assert (
        fp.injects_context is False
    )  # the flag follows the contract: only SessionStart/UserPromptSubmit/PreToolUse
    assert any("PostToolUse hook output can add context" in n for n in fp.notes)


def test_true_beats_unknown_and_unknown_beats_clean(tmp_path):
    root = tmp_path / "mixed"
    put(root / "hooks" / "a.py", "print('additionalContext')")
    put(root / "hooks" / "clean.sh", CLEAN_SH)
    put_json(
        root / "hooks" / "hooks.json",
        {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "echo opaque"}]}],
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/a.py",
                            }
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "sh ${CLAUDE_PLUGIN_ROOT}/hooks/clean.sh",
                            }
                        ]
                    }
                ],
            }
        },
    )
    assert footprint.scan_plugin_dir(root).injects_context is True
    put_json(
        root / "hooks" / "hooks.json",
        {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "echo opaque"}]}],
                "PreToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "sh ${CLAUDE_PLUGIN_ROOT}/hooks/clean.sh",
                            }
                        ]
                    }
                ],
            }
        },
    )
    assert footprint.scan_plugin_dir(root).injects_context is None


def test_script_outside_the_plugin_is_never_read(tmp_path):
    put(tmp_path / "outside.py", "print('additionalContext')")
    root = hook_plugin(tmp_path, "SessionStart", 'python3 "${CLAUDE_PLUGIN_ROOT}/../outside.py"')
    fp = footprint.scan_plugin_dir(root)
    assert fp.injects_context is None  # could not be inspected, so it must not be treated as a hit


def test_superpowers_style_wrapper_and_sibling_script(tmp_path):
    """`run-hook.cmd session-start`: the wrapper has no marker, the sibling `session-start` cats a skill file."""
    root = tmp_path / "superlike" / "6.4.1"
    skill = (
        "---\nname: using-superlike\ndescription: Use when starting any conversation\n---\n"
        + "s" * 3200
    )
    put(root / "skills" / "using-superlike" / "SKILL.md", skill)
    put(
        root / "hooks" / "run-hook.cmd",
        ": << 'CMDBLOCK'\n@echo off\nbash \"%HOOK_DIR%%~1\"\nCMDBLOCK\n",
    )
    put(
        root / "hooks" / "session-start",
        '#!/usr/bin/env bash\nPLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"\n'
        'content=$(cat "${PLUGIN_ROOT}/skills/using-superlike/SKILL.md" 2>&1 || echo "Error")\n'
        "# the README.md is only mentioned in this comment\n"
        'printf \'{"hookSpecificOutput": {"additionalContext": "%s"}}\' "$content"\n',
    )
    put_json(
        root / "hooks" / "hooks.json",
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup|clear|compact",
                        "hooks": [
                            {
                                "type": "command",
                                "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd" session-start',
                                "async": False,
                            }
                        ],
                    }
                ]
            }
        },
    )
    put(root / "README.md", "r" * 4000)  # must not be counted
    fp = footprint.scan_plugin_dir(root)
    assert fp.injects_context is True
    expected = math.ceil(len(skill) / 4)
    assert fp.injected_tokens_est == expected
    assert (
        f"SessionStart hook injects skills/using-superlike/SKILL.md (about {expected} tokens)"
        in fp.notes
    )
    assert not any("README" in n for n in fp.notes)


def test_markdown_in_comments_or_without_a_read_is_not_counted(tmp_path):
    root = hook_plugin(
        tmp_path,
        "SessionStart",
        'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/a.py"',
        {
            "hooks/a.py": "# see README.md for details\nprint('additionalContext')\nDOC = 'docs/guide.md'\n",
            "README.md": "r" * 400,
            "docs/guide.md": "g" * 400,
        },
    )
    fp = footprint.scan_plugin_dir(root)
    assert fp.injects_context is True
    assert fp.injected_tokens_est == 0
    assert any("can inject context" in n and "size not measurable" in n for n in fp.notes)


def test_user_prompt_injection_is_noted_per_prompt_and_not_in_always_on(tmp_path):
    root = hook_plugin(
        tmp_path,
        "UserPromptSubmit",
        'cat "${CLAUDE_PLUGIN_ROOT}/reminder.md"',
        {"reminder.md": "x" * 80},
    )
    fp = footprint.scan_plugin_dir(root)
    assert fp.injects_context is True
    assert fp.injected_tokens_est == 0
    assert (
        "UserPromptSubmit hook injects reminder.md (about 20 tokens) each time it fires" in fp.notes
    )


# ---- Windows paths and paths with spaces ----------------------------------------------------------------------------
def test_plugin_directory_and_script_names_with_spaces(tmp_path):
    root = tmp_path / "Program Files" / "My Plugins" / "spacey"
    put(root / "hooks" / "my hook.py", "print('additionalContext')")
    put(root / "skills" / "s" / "SKILL.md", "---\ndescription: spaced\n---\n")
    put_json(
        root / "hooks" / "hooks.json",
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'python "${CLAUDE_PLUGIN_ROOT}/hooks/my hook.py"',
                            }
                        ]
                    }
                ]
            }
        },
    )
    fp = footprint.scan_plugin_dir(root)
    assert fp.injects_context is True
    assert fp.skills == 1


@pytest.mark.skipif(os.name != "nt", reason="backslash separators only mean something on Windows")
def test_windows_backslash_and_percent_variable_commands(tmp_path):
    root = tmp_path / "Program Files" / "Win Plugin"
    put(root / "hooks" / "inject.cmd", '@echo off\r\necho {"additionalContext": "x"}\r\n')
    cmd = '"%CLAUDE_PLUGIN_ROOT%\\hooks\\inject.cmd"'
    put_json(
        root / "hooks" / "hooks.json",
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": cmd}]}]}},
    )
    assert footprint.scan_plugin_dir(root).injects_context is True
    absolute = f'"{root}\\hooks\\inject.cmd"'  # an absolute Windows path inside the plugin
    put_json(
        root / "hooks" / "hooks.json",
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": absolute}]}]}},
    )
    assert footprint.scan_plugin_dir(root).injects_context is True


def test_split_command_keeps_backslashes_and_quotes():
    toks = footprint._split_command(
        r'"C:\Program Files\py\python.exe" --flag "a b" plain C:\x\y.py'
    )
    assert toks == [r"C:\Program Files\py\python.exe", "--flag", "a b", "plain", r"C:\x\y.py"]
    assert footprint._split_command("") == []
    assert footprint._split_command('""') == [""]


# ---- scan_installed ------------------------------------------------------------------------------------------------
def test_scan_installed(tmp_path):
    rich = make_rich_plugin(tmp_path / "Program Files" / "My Cache", "rich")
    entries = [
        {
            "id": "rich@mk",
            "version": "1.2.3",
            "scope": "user",
            "enabled": True,
            "installPath": str(rich),
        },
        {
            "id": "off@mk",
            "version": "1",
            "scope": "user",
            "enabled": False,
            "installPath": str(rich),
        },
        {
            "id": "gone@mk",
            "version": "9.9.9",
            "scope": "user",
            "enabled": True,
            "installPath": str(tmp_path / "nope"),
        },
        {"id": "nopath@mk", "enabled": True},
        {
            "id": "rich@mk",
            "version": "1.2.3",
            "scope": "project",
            "enabled": True,
            "installPath": str(rich),
        },
        {"id": "posix@mk", "version": "2.0", "enabled": True, "installPath": rich.as_posix()},
        "garbage",
        42,
        None,
    ]
    fps = footprint.scan_installed(entries)
    assert [f.name for f in fps] == ["rich@mk", "gone@mk", "nopath@mk", "posix@mk"]
    rich_fp, gone, nopath, posix = fps
    assert rich_fp.skills == 3
    assert rich_fp.version == "1.2.3"
    assert any("listed 2 times" in n for n in rich_fp.notes)
    assert gone.notes == ["install path not found"]
    assert gone.version == "9.9.9"
    assert nopath.notes == ["install path not found"]
    assert posix.skills == 3
    assert posix.version == "2.0"  # the list entry's version wins over the manifest's


def test_scan_installed_accepts_wrapper_and_garbage():
    assert footprint.scan_installed([]) == []
    assert footprint.scan_installed({"plugins": []}) == []
    assert footprint.scan_installed({"nope": 1}) == []  # type: ignore[arg-type]
    assert footprint.scan_installed(None) == []  # type: ignore[arg-type]


def test_scan_installed_defaults_enabled_when_the_key_is_missing(tmp_path):
    root = make_rich_plugin(tmp_path)
    fps = footprint.scan_installed([{"id": "x@y", "installPath": str(root)}])
    assert [f.skills for f in fps] == [3]


def test_scan_installed_counts_a_repeated_id_once_and_prefers_the_user_scope(tmp_path):
    old = make_rich_plugin(tmp_path, "old")  # a project entry pointing at another cached version
    new = tmp_path / "new"
    put(new / "skills" / "only" / "SKILL.md", "---\ndescription: d\n---\n")
    fps = footprint.scan_installed(
        [
            {
                "id": "p@m",
                "scope": "project",
                "enabled": True,
                "version": "1",
                "installPath": str(old),
            },
            {
                "id": "p@m",
                "scope": "user",
                "enabled": True,
                "version": "2",
                "installPath": str(new),
            },
            {
                "id": "p@m",
                "scope": "project",
                "enabled": False,
                "version": "3",
                "installPath": str(old),
            },
        ]
    )
    assert len(fps) == 1
    assert (fps[0].skills, fps[0].version) == (1, "2")
    assert "listed 2 times (several scopes or projects); counted once" in fps[0].notes


def test_scan_installed_uses_the_servers_plugin_list_resolved(tmp_path):
    """`claude plugin list --json` reports `mcpServers` itself, which covers a missing or unusual install path."""
    http = {"vercel": {"type": "http", "url": "https://example.invalid/mcp"}}
    root = make_rich_plugin(tmp_path)  # two servers on disk
    fps = footprint.scan_installed(
        [
            {"id": "gone@m", "installPath": str(tmp_path / "nope"), "mcpServers": http},
            {
                "id": "rich@m",
                "installPath": str(root),
                "mcpServers": http,
            },  # fewer than on disk: ignored
        ]
    )
    gone, rich = fps
    assert gone.mcp_servers == 1
    assert gone.notes[0] == "install path not found"
    assert sum("MCP server(s)" in n for n in gone.notes) == 1
    assert rich.mcp_servers == 2
    assert sum("MCP server(s)" in n for n in rich.notes) == 1


# ---- render --------------------------------------------------------------------------------------------------------
def sample_fps() -> list[PluginFootprint]:
    return [
        PluginFootprint(
            name="small@m",
            skills=1,
            description_tokens_est=50,
        ),
        PluginFootprint(
            name="big@m",
            skills=3,
            manual_only_skills=1,
            agents=2,
            commands=1,
            description_tokens_est=900,
            hook_events=["SessionStart", "PostToolUse", "Stop", "SubagentStop"],
            injects_context=True,
            injected_tokens_est=300,
            mcp_servers=2,
            notes=["note one", "note two"],
        ),
        PluginFootprint(name="odd@m", injects_context=None, hook_events=["PreToolUse"]),
    ]


def test_render_table_and_totals():
    text = footprint.render(sample_fps(), None)
    lines = text.splitlines()
    assert lines[0].split() == [
        "plugin",
        "skills/agents/cmds",
        "desc",
        "tokens",
        "hook",
        "events",
        "injects",
        "mcp",
    ]
    big = next(i for i, ln in enumerate(lines) if ln.startswith("big@m"))
    assert lines[big].split() == [
        "big@m",
        "3(1m)/2/1",
        "~900",
        "SessionStart,",
        "PostToolUse,",
        "Stop",
        "+1",
        "yes",
        "~300",
        "2",
    ]
    assert lines[big + 1 : big + 3] == ["    - note one", "    - note two"]
    assert text.index("big@m") < text.index("small@m")  # sorted by always-on cost, biggest first
    assert (
        "Estimated always-on: ~1,250 tokens (descriptions 950 + injected 300) from 3 plugin(s)"
        in text
    )
    assert "budget" not in text
    assert "1 plugin(s) have hooks that could not be inspected" in text
    assert "unknown" in text
    assert "Hidden costs not counted by `claude plugin details`" in text
    assert "harness-only" in text
    assert "resolved at runtime" in text
    assert all(ln == ln.rstrip() for ln in lines)


def test_render_budget_ok_and_over():
    ok = footprint.render(sample_fps(), 2000)
    assert "budget 2,000: OK (750 under)" in ok
    exact = footprint.render(sample_fps(), 1250)
    assert "budget 1,250: OK (0 under)" in exact
    over = footprint.render(sample_fps(), 1000)
    assert "budget 1,000: OVER by 250" in over
    assert "OK" not in over.split("Hidden costs")[0]


def test_render_empty_list():
    text = footprint.render([], 100)
    assert "No enabled plugins found." in text
    assert "~0 tokens" in text
    assert "budget 100: OK (100 under)" in text


def test_render_real_scan_end_to_end(tmp_path):
    rich = make_rich_plugin(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    fps = footprint.scan_installed(
        [
            {"id": "rich@mk", "version": "1.2.3", "installPath": str(rich)},
            {"id": "empty@mk", "installPath": str(empty)},
            {"id": "gone@mk", "installPath": str(tmp_path / "gone")},
        ]
    )
    text = footprint.render(fps, 10_000)
    total = footprint.always_on_tokens(fps)
    assert total == math.ceil(RICH_CHARS / 4) + 100
    assert f"~{total:,} tokens" in text
    assert "install path not found" in text
    assert "yes ~100" in text


# ---- probe ---------------------------------------------------------------------------------------------------------
RESULT_JSON = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "OK",
    "total_cost_usd": 0.0421,
    "usage": {
        "input_tokens": 3,
        "cache_creation_input_tokens": 12000,
        "cache_read_input_tokens": 50,
        "output_tokens": 5,
    },
}


def fake_runner(stdout="", returncode=0, stderr="", calls=None):
    def run(argv):
        if calls is not None:
            calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return run


def test_probe_ok_and_argv():
    calls: list = []
    claude = r"C:\Program Files\Claude Code\claude.exe"
    res = footprint.probe(claude, runner=fake_runner(json.dumps(RESULT_JSON), calls=calls))
    assert res == {
        "input_tokens": 3,
        "cache_creation_tokens": 12000,
        "cache_read_tokens": 50,
        "output_tokens": 5,
        "total_cost_usd": 0.0421,
        "ok": True,
        "error": "",
    }
    assert calls == [
        [
            claude,
            "-p",
            "Reply with OK",
            "--output-format",
            "json",
            "--max-turns",
            "1",
            "--no-session-persistence",
        ]
    ]
    assert footprint.session_input_tokens(res) == 12053


def test_probe_accepts_positional_runner_bytes_and_event_lists():
    events = [{"type": "system"}, RESULT_JSON]
    res = footprint.probe("claude", fake_runner(json.dumps(events)))
    assert res["ok"] and res["cache_creation_tokens"] == 12000
    as_bytes = subprocess.CompletedProcess([], 0, json.dumps(RESULT_JSON).encode(), b"")
    assert footprint.probe("claude", lambda argv: as_bytes)["ok"]
    noisy = "warning: something\n" + json.dumps(RESULT_JSON) + "\n"
    assert footprint.probe("claude", fake_runner(noisy))["ok"]


def test_probe_tolerates_missing_keys():
    res = footprint.probe(
        "claude", fake_runner(json.dumps({"usage": {"input_tokens": 7, "output_tokens": "x"}}))
    )
    assert res["ok"] is True
    assert res["input_tokens"] == 7
    assert (res["cache_creation_tokens"], res["cache_read_tokens"], res["output_tokens"]) == (
        0,
        0,
        0,
    )
    assert res["total_cost_usd"] == 0.0


def test_probe_without_usage_is_not_ok():
    res = footprint.probe(
        "claude", fake_runner(json.dumps({"result": "OK", "total_cost_usd": 0.01}))
    )
    assert res["ok"] is False
    assert "no usage" in res["error"]
    assert res["total_cost_usd"] == 0.01


@pytest.mark.parametrize("stdout", ["", "not json at all", "{broken", "[1, 2]", "null"])
def test_probe_invalid_json(stdout):
    res = footprint.probe("claude", fake_runner(stdout))
    assert res["ok"] is False
    assert res["error"]
    assert res["input_tokens"] == 0


def test_probe_nonzero_exit():
    res = footprint.probe(
        "claude", fake_runner("", returncode=1, stderr="error: unknown option '--max-turns'\n")
    )
    assert res["ok"] is False
    assert "exited with code 1" in res["error"]
    assert "unknown option" in res["error"]
    res = footprint.probe(
        "claude", fake_runner(json.dumps(RESULT_JSON), returncode=2, stderr="boom")
    )
    assert res["ok"] is False  # exit status wins over a parseable body, but the numbers are kept
    assert res["input_tokens"] == 3
    assert "exited with code 2" in res["error"]


def test_probe_reports_is_error():
    body = dict(RESULT_JSON, is_error=True, result="Credit balance is too low")
    res = footprint.probe("claude", fake_runner(json.dumps(body)))
    assert res["ok"] is False
    assert "Credit balance is too low" in res["error"]


def test_probe_never_raises():
    def boom(argv):
        raise OSError("no such file")

    res = footprint.probe("claude", boom)
    assert res["ok"] is False
    assert "no such file" in res["error"]


def test_render_probe():
    ok = footprint.render_probe(footprint.probe("claude", fake_runner(json.dumps(RESULT_JSON))))
    assert "12,053 input tokens" in ok
    assert "cache write 12,000" in ok
    assert "$0.0421" in ok
    failed = footprint.render_probe({"ok": False, "error": "boom"})
    assert failed == "Probe failed: boom"


def test_default_runner_uses_procutil_with_timeout_and_empty_cwd(sandbox, monkeypatch):
    seen: dict = {}

    def fake_run(cmd, timeout=60, env=None, cwd=None, input_text=None):
        seen.update(
            cmd=cmd,
            timeout=timeout,
            cwd=cwd,
            empty=cwd is not None and not any(Path(cwd).iterdir()),
        )
        return subprocess.CompletedProcess(cmd, 0, json.dumps(RESULT_JSON), "")

    with monkeypatch.context() as m:
        m.setattr(procutil, "run", fake_run)
        res = footprint.probe("claude")
    assert res["ok"] is True
    assert seen["timeout"] == 120
    assert seen["empty"] is True
    assert seen["cmd"][:2] == ["claude", "-p"]
    assert not Path(seen["cwd"]).exists()  # the temp directory is cleaned up afterwards


def test_default_runner_end_to_end_with_a_fake_claude(sandbox, tmp_path, monkeypatch):
    """Runs a real subprocess: a tiny Python script posing as the claude binary."""
    record = tmp_path / "record.json"
    script = put(
        tmp_path / "fake_claude.py",
        textwrap.dedent(
            """\
            import json, os, sys
            with open(os.environ["FAKE_CLAUDE_RECORD"], "w", encoding="utf-8") as fh:
                json.dump({"argv": sys.argv[1:], "cwd": os.getcwd()}, fh)
            print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.5,
                              "usage": {"input_tokens": 1, "cache_creation_input_tokens": 2,
                                        "cache_read_input_tokens": 3, "output_tokens": 4}}))
            """
        ),
    )
    if os.name == "nt":
        exe = put(tmp_path / "fake-claude.cmd", f'@"{sys.executable}" "{script}" %*\n')
    else:
        exe = put(tmp_path / "fake-claude", f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    with monkeypatch.context() as m:
        m.setenv("FAKE_CLAUDE_RECORD", str(record))
        res = footprint.probe(str(exe))
    assert res["ok"] is True, res
    assert (
        res["input_tokens"],
        res["cache_creation_tokens"],
        res["cache_read_tokens"],
        res["output_tokens"],
    ) == (1, 2, 3, 4)
    assert res["total_cost_usd"] == 0.5
    rec = json.loads(record.read_text(encoding="utf-8"))
    assert rec["argv"][:2] == ["-p", "Reply with OK"]
    assert "--no-session-persistence" in rec["argv"]
    assert Path(rec["cwd"]).resolve() != Path.cwd().resolve()


# ---- security_guidance_health --------------------------------------------------------------------------------------
SG_LOG = """\
[2026-10-16 01:00:00.000] Stop hook: LLM review disabled or no API credentials
[2026-10-16 01:00:01.000] Commit review: LLM review disabled or no API credentials
[2026-10-16 01:00:02.000] Commit review: no API credentials configured
[2026-10-16 01:01:00.000] Stop hook: reviewing 3 changed files (standard diff)
[2026-10-16 01:01:05.000] Stop hook: LLM reviews took 4.2s total est_cost_usd=0.0123
[2026-10-16 01:01:06.000] Stop hook: no security issues found
[2026-10-16 01:02:00.000] LLM code review found 2 high/critical vulnerabilities cost_usd=0.5
[2026-10-16 01:02:01.000] unrelated x_cost_usd=7 must not count
[2026-10-16 01:02:02.000] est_cost_usd=oops
[2026-10-16 01:02:03.000] Processing hook_event=PostToolUse, tool=Bash
"""


def test_security_guidance_health_counts_skips_completions_and_cost(tmp_path):
    log = put(tmp_path / "log.txt", SG_LOG)
    h = footprint.security_guidance_health(log)
    assert h == {"skipped_reviews": 3, "completed_reviews": 3, "est_cost_usd": 0.5123, "lines": 10}


def test_security_guidance_health_without_skips(tmp_path):
    log = put(
        tmp_path / "log.txt",
        "[t] Stop hook: LLM reviews took 1.0s total\n[t] Commit review: no security issues found\n[t] noise\n",
    )
    h = footprint.security_guidance_health(log)
    assert h["skipped_reviews"] == 0
    assert h["completed_reviews"] == 2
    assert h["est_cost_usd"] == 0.0


def test_security_guidance_health_tolerates_malformed_lines(tmp_path):
    log = tmp_path / "log.txt"
    log.write_bytes(
        b"\xff\xfe\x00 binary garbage\n"
        b"\n"
        b"Stop hook: LLM review disabled or no API credentials\n"
        b"est_cost_usd=1e999\n"
        b"est_cost_usd=0.25 trailing words\n"
        b"[unterminated bracket LLM review disabled"
    )
    h = footprint.security_guidance_health(log)
    assert h["skipped_reviews"] == 2
    assert h["est_cost_usd"] == 0.25  # the infinite value is ignored
    assert h["lines"] == 6


def test_security_guidance_health_empty_file(tmp_path):
    h = footprint.security_guidance_health(put(tmp_path / "log.txt", ""))
    assert h == {"skipped_reviews": 0, "completed_reviews": 0, "est_cost_usd": 0.0, "lines": 0}


def test_security_guidance_health_missing_or_unreadable_is_none(tmp_path):
    assert footprint.security_guidance_health(tmp_path / "missing.txt") is None
    assert footprint.security_guidance_health(tmp_path) is None  # a directory
    assert footprint.security_guidance_health(str(tmp_path / "also-missing")) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("window", [120, 130])
def test_security_guidance_health_reads_only_the_tail(tmp_path, monkeypatch, window):
    # 10 lines of 24 bytes each; the window holds the last 5 whole lines (120) or 5 plus a cut-off line (130)
    log = put(tmp_path / "log.txt", "".join(f"L{i:02d} LLM review disabled\n" for i in range(10)))
    with monkeypatch.context() as m:
        m.setattr(footprint, "_MAX_LOG_READ", window)
        h = footprint.security_guidance_health(log)
    assert h["skipped_reviews"] == 5
    assert h["lines"] == 5


def test_security_log_path_follows_the_plugins_own_precedence(sandbox, monkeypatch, tmp_path):
    with monkeypatch.context() as m:
        m.delenv("SECURITY_GUIDANCE_DEBUG_LOG", raising=False)
        m.delenv("SECURITY_WARNINGS_STATE_DIR", raising=False)
        assert footprint.security_log_path() == sandbox / ".claude" / "security" / "log.txt"
        m.setenv("SECURITY_WARNINGS_STATE_DIR", str(tmp_path / "state"))
        assert footprint.security_log_path() == tmp_path / "state" / "log.txt"
        m.setenv("SECURITY_GUIDANCE_DEBUG_LOG", str(tmp_path / "custom.log"))
        assert footprint.security_log_path() == tmp_path / "custom.log"


# ---- hook_binary_on_path -------------------------------------------------------------------------------------------
def test_hook_binary_on_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin dir"
    bindir.mkdir()
    exe = bindir / ("claude-hr-hook.cmd" if os.name == "nt" else "claude-hr-hook")
    put(exe, "@echo off\n" if os.name == "nt" else "#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    with monkeypatch.context() as m:
        m.chdir(tmp_path)
        m.setenv("PATH", str(bindir))
        found = footprint.hook_binary_on_path()
        assert found is not None
        assert Path(found).resolve() == exe.resolve()
        assert footprint.hook_binary_on_path("definitely-not-installed-xyz") is None
        m.setenv("PATH", str(tmp_path / "empty"))
        assert footprint.hook_binary_on_path() is None

"""Credential hygiene: the literal-secret scan of Claude's configuration (project-scoped MCP servers included) and the
transcript scan. No test may print, log or store a secret value: every one asserts the raw values stay out."""

import dataclasses
import json
import logging
import os
import random
import string
import time
from pathlib import Path

import pytest

from claude_hr import config, creds, ctl, paths
from claude_hr.checks import OK, WARN


def fake(
    prefix: str, n: int, alphabet: str = string.ascii_letters + string.digits, seed: int = 1
) -> str:
    """A random-looking value built at run time, so no token-shaped string sits in this file's source."""
    rnd = random.Random(f"{prefix}{seed}")
    return prefix + "".join(rnd.choice(alphabet) for _ in range(n))


UPPER = string.ascii_uppercase + string.digits
AWS_TEMP_ID = fake("ASIA", 16, UPPER)  # an STS access key id
AWS_LONG_ID = fake("AKIA", 16, UPPER)  # a long-lived access key id
AWS_SECRET = fake("", 40, string.ascii_letters + string.digits + "/+")
AWS_SESSION = fake("IQoJ", 300, string.ascii_letters + string.digits + "/+=")
BEARER = fake("", 32)
GH_TOKEN = fake("ghp_", 36)
GH_TOKEN_2 = fake("ghs_", 36, seed=2)
RAW = [AWS_TEMP_ID, AWS_LONG_ID, AWS_SECRET, AWS_SESSION, BEARER, GH_TOKEN, GH_TOKEN_2]
assert not any(
    w in v.lower() for v in RAW for w in creds._PLACEHOLDER_WORDS
)  # a placeholder would not be flagged


def no_raw(*texts: str) -> None:
    """Neither a value nor even five characters of it (the mask shows at most four) is in any text."""
    for text in texts:
        for v in RAW:
            assert v not in text and v[:5] not in text, "a secret value reached the output"


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    """`gh` and `op` on the machine running the suite must not change what run_checks reports."""
    monkeypatch.setattr(creds.procutil, "cmd_exists", lambda name: False)


def claude_json(data) -> None:
    paths.claude_global_json().write_text(json.dumps(data), encoding="utf-8")


def aws_env(**extra):
    return {
        "AWS_ACCESS_KEY_ID": AWS_TEMP_ID,
        "AWS_SECRET_ACCESS_KEY": AWS_SECRET,
        "AWS_SESSION_TOKEN": AWS_SESSION,
        "AWS_REGION": "ap-southeast-2",
        **extra,
    }


def project_with(servers) -> dict:
    return {"mcpServers": {}, "projects": {"C:/work/HalfCourt - AWS": {"mcpServers": servers}}}


def check(name: str):
    return next(c for c in creds.run_checks(config.load()) if c.name == name)


# ---- project-scoped MCP servers ---------------------------------------------------------------------------------


def test_temporary_aws_credentials_in_a_project_scoped_server_are_found_and_masked():
    """doctor said 'MCP literal secrets: none' while ~/.claude.json held these under projects[<path>]."""
    claude_json(
        project_with({"aws-api": {"command": "uvx", "args": ["aws-api-mcp"], "env": aws_env()}})
    )
    found = creds.scan_config_secrets()
    assert [(f.project, f.server, f.field, f.name, f.kind) for f in found] == [
        ("C:/work/HalfCourt - AWS", "aws-api", "env", "AWS_ACCESS_KEY_ID", "AWS access key id"),
        (
            "C:/work/HalfCourt - AWS",
            "aws-api",
            "env",
            "AWS_SECRET_ACCESS_KEY",
            "AWS secret access key",
        ),
        ("C:/work/HalfCourt - AWS", "aws-api", "env", "AWS_SESSION_TOKEN", "AWS session token"),
    ]  # AWS_REGION is not a secret
    assert all(f.temporary for f in found)
    for f, raw in zip(found, (AWS_TEMP_ID, AWS_SECRET, AWS_SESSION), strict=True):
        assert f.masked.split("…")[0] == raw[:4]  # the first four characters at most
        assert f"({len(raw)} chars)" in f.masked
        assert "probably expired" in f.fix and "delete" in f.fix
    no_raw(repr(found), *(f.fix + f.where for f in found))


def test_top_level_servers_are_still_scanned_and_name_their_kind():
    claude_json(
        {
            "mcpServers": {
                "gh": {"env": {"GITHUB_TOKEN": GH_TOKEN}},
                "api": {"headers": {"Authorization": f"Bearer {BEARER}", "X-Trace": "on"}},
            }
        }
    )
    found = creds.scan_config_secrets()
    assert [(f.project, f.server, f.field, f.name, f.kind) for f in found] == [
        ("", "gh", "env", "GITHUB_TOKEN", "GitHub token"),
        ("", "api", "headers", "Authorization", "bearer token"),
    ]
    assert found[0].where == ".claude.json [user] gh env.GITHUB_TOKEN"
    assert not any(f.temporary for f in found)
    assert "rotate" in found[0].fix and "expired" not in found[0].fix
    assert found[1].masked.startswith(BEARER[:4]) and "Bear" not in found[1].masked
    no_raw(repr(found))


def test_references_placeholders_and_non_secrets_are_not_flagged():
    env = {
        "API_TOKEN_1": "${GITHUB_TOKEN}",
        "API_TOKEN_2": "${GITHUB_TOKEN:-}",
        "API_TOKEN_3": "$GITHUB_TOKEN",
        "API_TOKEN_4": "%GITHUB_TOKEN%",
        "API_TOKEN_5": "op://Private/GitHub/token",
        "API_TOKEN_6": "{{ secrets.token }}",
        "API_TOKEN_7": "<paste-your-token>",
        "API_TOKEN_8": "your-api-key-goes-here",
        "API_TOKEN_9": "changeme-please",
        "MAX_TOKENS": "4096",
        "TOKEN_DEBUG": "true",
        "EMPTY_SECRET": "",
        "AWS_SECRET_ACCESS_KEY": "${AWS_SECRET_ACCESS_KEY}",
        "AWS_REGION": "ap-southeast-2",
    }
    server = {
        "env": env,
        "headers": {"Authorization": "Bearer ${TOKEN}", "X-Api-Key": "$KEY"},
        "args": [
            "--token=${TOKEN}",
            "--api-key",
            "${KEY}",
            "-e",
            "AWS_SECRET_ACCESS_KEY",
            "--password=op://Private/db/password",
            "--verbose",
        ],
        "url": "https://mcp.example.test/sse?api_key=${KEY}",
    }
    claude_json(
        {"mcpServers": {"clean": server}, "projects": {"C:/p": {"mcpServers": {"clean": server}}}}
    )
    assert creds.scan_config_secrets() == []
    c = check("MCP literal secrets")
    assert c.status == OK and c.detail == "none"


def test_args_and_urls_are_scanned_too():
    claude_json(
        {
            "projects": {
                "C:/p": {
                    "mcpServers": {
                        "s": {
                            "command": "npx",
                            "args": [
                                f"--api-key={BEARER}",
                                "--password",
                                BEARER,  # the value of the flag before it: reported once, with the flag
                                "-e",
                                f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}",
                                GH_TOKEN_2,
                                "--region=ap-southeast-2",
                            ],
                            "url": f"https://user:{BEARER}@mcp.example.test/sse",
                        },
                        "q": {"url": f"https://mcp.example.test/sse?api_key={BEARER}&mode=fast"},
                    }
                }
            }
        }
    )
    found = creds.scan_config_secrets()
    assert [(f.server, f.field, f.name, f.kind) for f in found] == [
        ("s", "args", "api-key", "API key"),
        ("s", "args", "password", "password"),
        ("s", "args", "AWS_SECRET_ACCESS_KEY", "AWS secret access key"),
        ("s", "args", "[5]", "GitHub token"),
        ("s", "url", "password", "password"),
        ("q", "url", "?api_key", "API key"),
    ]
    no_raw(repr(found))


# ---- settings files and project files ---------------------------------------------------------------------------


def test_settings_env_and_mcp_servers_are_scanned_at_user_level():
    (paths.claude_home() / "settings.json").write_text(
        json.dumps({"env": {"DB_PASSWORD": BEARER, "DISABLE_ERROR_REPORTING": "1"}}),
        encoding="utf-8",
    )
    (paths.claude_home() / "settings.local.json").write_text(
        json.dumps(
            {
                "env": {
                    "SOME_NEUTRAL_NAME": GH_TOKEN
                },  # no secret-looking name, but a token's shape
                "mcpServers": {"s": {"env": {"AUTH_TOKEN": BEARER}}},
            }
        ),
        encoding="utf-8",
    )
    found = creds.scan_config_secrets()
    assert [(f.file, f.server, f.field, f.name) for f in found] == [
        ("settings.json", "", "env", "DB_PASSWORD"),
        ("settings.local.json", "", "env", "SOME_NEUTRAL_NAME"),
        ("settings.local.json", "s", "env", "AUTH_TOKEN"),
    ]
    env_check, mcp_check = check("settings.json env secrets"), check("MCP literal secrets")
    assert env_check.status == WARN
    assert (
        env_check.detail
        == "secret-looking variable(s) stored in plaintext: DB_PASSWORD, SOME_NEUTRAL_NAME"
    )
    assert mcp_check.status == WARN and "1 literal secret(s) found" in mcp_check.detail
    no_raw(repr(found), env_check.line(), mcp_check.line())


def test_a_projects_own_files_are_scanned(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"db": {"env": {"DB_PASSWORD": BEARER}}}}), encoding="utf-8"
    )
    (proj / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"GH_TOKEN": GH_TOKEN}}), encoding="utf-8"
    )
    (proj / ".claude" / "settings.local.json").write_text(
        json.dumps({"mcpServers": {"s": {"headers": {"X-Api-Key": BEARER}}}}), encoding="utf-8"
    )
    claude_json({"projects": {str(proj): {}}})
    found = creds.scan_config_secrets()
    assert [(f.file, f.project, f.server, f.name) for f in found] == [
        (".mcp.json", str(proj), "db", "DB_PASSWORD"),
        ("settings.json", str(proj), "", "GH_TOKEN"),
        ("settings.local.json", str(proj), "s", "X-Api-Key"),
    ]
    env_check = check("settings.json env secrets")
    assert "GH_TOKEN (project " in env_check.detail
    no_raw(repr(found), env_check.line())


# ---- temporary AWS credentials ----------------------------------------------------------------------------------


def test_aws_values_beside_a_session_token_or_an_asia_key_are_temporary_and_others_are_not():
    claude_json(
        {
            "mcpServers": {
                "sts": {
                    "env": {"AWS_ACCESS_KEY_ID": AWS_LONG_ID, "AWS_SESSION_TOKEN": AWS_SESSION}
                },
                "key-only": {
                    "env": {"AWS_ACCESS_KEY_ID": AWS_TEMP_ID, "AWS_SECRET_ACCESS_KEY": AWS_SECRET}
                },
                "long": {
                    "env": {"AWS_ACCESS_KEY_ID": AWS_LONG_ID, "AWS_SECRET_ACCESS_KEY": AWS_SECRET}
                },
            }
        }
    )
    got = {(f.server, f.name): f.temporary for f in creds.scan_config_secrets()}
    assert got == {
        (
            "sts",
            "AWS_ACCESS_KEY_ID",
        ): True,  # next to a session token, so it is an STS key whatever its prefix
        ("sts", "AWS_SESSION_TOKEN"): True,
        ("key-only", "AWS_ACCESS_KEY_ID"): True,  # ASIA
        ("key-only", "AWS_SECRET_ACCESS_KEY"): True,
        ("long", "AWS_ACCESS_KEY_ID"): False,
        ("long", "AWS_SECRET_ACCESS_KEY"): False,
    }
    long_lived = [f for f in creds.scan_config_secrets() if f.server == "long"]
    assert all("expired" not in f.fix and "profile" in f.fix for f in long_lived)


# ---- doctor's line ----------------------------------------------------------------------------------------------


def test_doctors_line_counts_the_secrets_and_names_the_first_two_places():
    claude_json(project_with({"aws-api": {"env": aws_env()}}))
    c = check("MCP literal secrets")
    wheres = [f.where for f in creds.scan_config_secrets()]
    assert c.status == WARN
    assert c.detail == f"3 literal secret(s) found: {wheres[0]}; {wheres[1]}; +1 more"
    assert "HalfCourt - AWS" in c.detail and "aws-api" in c.detail
    assert (
        "AWS_ACCESS_KEY_ID" in c.detail and "AWS_SESSION_TOKEN" not in c.detail
    )  # only the first two
    assert "creds doctor" in c.fix and "probably expired" in c.fix
    no_raw(c.line(), json.dumps(dataclasses.asdict(c)))


def test_doctors_line_is_ok_when_there_is_nothing():
    assert (check("MCP literal secrets").status, check("MCP literal secrets").detail) == (
        OK,
        "none",
    )
    claude_json({"projects": {"C:/p": {"mcpServers": {"s": {"env": {"LOG_LEVEL": "debug"}}}}}})
    assert check("MCP literal secrets").detail == "none"


def test_creds_doctor_prints_each_finding_masked_with_its_fix(capsys, caplog):
    claude_json(project_with({"aws-api": {"env": aws_env()}}))
    with caplog.at_level(logging.DEBUG):
        rc = ctl._run(["creds", "doctor"])
    captured = capsys.readouterr()
    text = captured.out
    assert rc == 0  # a warning is not a failure
    assert "3 literal secret(s) in Claude's configuration (values are never printed):" in text
    assert "1. .claude.json [project C:/work/HalfCourt - AWS] aws-api env.AWS_ACCESS_KEY_ID" in text
    assert "project: C:/work/HalfCourt - AWS" in text
    assert "kind: AWS session token (temporary)" in text and "value: IQoJ…(304 chars)" in text
    assert "probably expired" in text and "delete" in text
    no_raw(text, captured.err, caplog.text)


# ---- never fail, never stall ------------------------------------------------------------------------------------


def test_a_broken_or_odd_config_file_finds_nothing_and_does_not_raise():
    paths.claude_global_json().write_text("{not json", encoding="utf-8")
    assert creds.scan_config_secrets() == []
    assert check("MCP literal secrets").status == OK
    claude_json(
        {
            "mcpServers": ["not", "a", "dict"],
            "projects": {
                "p": "a string",
                "q": {
                    "mcpServers": {
                        "s": "a string",
                        "t": {"env": "x", "args": "y", "url": 5, "headers": [1]},
                    }
                },
                7: {},
            },
        }
    )
    assert creds.scan_config_secrets() == []
    claude_json([1, 2, 3])
    assert creds.scan_config_secrets() == []


def test_a_huge_claude_json_is_not_read_and_doctor_says_so(monkeypatch):
    claude_json(project_with({"aws-api": {"env": aws_env()}}))
    assert len(creds.scan_config_secrets()) == 3
    monkeypatch.setattr(creds, "MAX_CONFIG_BYTES", 100)
    skipped: list[str] = []
    assert creds.scan_config_secrets(skipped) == []
    assert skipped == [".claude.json is larger than 100 bytes"]
    c = check("credentials")
    assert c.status == WARN and ".claude.json is larger than 100 bytes" in c.detail


def test_an_unparsable_claude_json_is_reported_as_not_scanned_not_as_clean():
    """A scan that could not read the file must not look like a scan that found nothing."""
    paths.claude_global_json().write_text(
        '{"mcpServers": {"s": {"env": {"API_TOKEN": "', encoding="utf-8"
    )
    skipped: list[str] = []
    assert creds.scan_config_secrets(skipped) == []
    assert skipped == [".claude.json is not valid JSON"]
    c = check("credentials")
    assert (
        c.status == WARN
        and c.detail == "the credential scan could not read: .claude.json is not valid JSON"
    )


def test_a_missing_file_and_a_projects_unparsable_file_are_not_reported(tmp_path):
    skipped: list[str] = []
    assert creds.scan_config_secrets(skipped) == [] and skipped == []  # no files at all
    assert not any(c.name == "credentials" for c in creds.run_checks(config.load()))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".mcp.json").write_text(
        "{ // a comment: not JSON", encoding="utf-8"
    )  # a repo's own file
    claude_json({"projects": {str(proj): {}}})
    assert creds.scan_config_secrets(skipped) == [] and skipped == []
    assert not any(c.name == "credentials" for c in creds.run_checks(config.load()))


def test_only_the_most_recent_project_folders_are_read_and_findings_are_capped(
    tmp_path, monkeypatch
):
    projs = []
    for i in range(5):
        p = tmp_path / f"proj{i}"
        p.mkdir()
        (p / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"s": {"env": {"API_TOKEN": BEARER}}}}), encoding="utf-8"
        )
        projs.append(p)
    claude_json({"projects": {str(p): {} for p in projs}})
    assert len(creds.scan_config_secrets()) == 5
    monkeypatch.setattr(creds, "MAX_PROJECT_DIRS", 2)
    assert [f.project for f in creds.scan_config_secrets()] == [str(projs[3]), str(projs[4])]
    monkeypatch.setattr(creds, "MAX_PROJECT_DIRS", 60)
    monkeypatch.setattr(creds, "MAX_FINDINGS", 3)
    assert len(creds.scan_config_secrets()) == 3


def test_the_scan_stops_reading_project_folders_when_its_time_is_spent(tmp_path, monkeypatch):
    p = tmp_path / "proj"
    p.mkdir()
    (p / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"disk": {"env": {"API_TOKEN": BEARER}}}}), encoding="utf-8"
    )
    claude_json({"projects": {str(p): {"mcpServers": {"memory": {"env": {"API_TOKEN": BEARER}}}}}})
    assert [f.server for f in creds.scan_config_secrets()] == ["memory", "disk"]
    monkeypatch.setattr(creds, "SCAN_SECONDS", -1)
    assert [f.server for f in creds.scan_config_secrets()] == ["memory"]


def test_text_from_the_config_cannot_inject_terminal_escapes():
    evil = "evil\x1b[31m\nname"
    claude_json({"projects": {evil: {"mcpServers": {evil: {"env": {evil + "_TOKEN": BEARER}}}}}})
    (found,) = creds.scan_config_secrets()
    assert "\x1b" not in found.where and "\n" not in found.where and "\x1b" not in found.project
    assert "\x1b" not in "\n".join(creds.format_findings([found]))


def test_mask_never_shows_more_than_a_quarter_or_four_characters():
    assert creds.mask("ABCDEFGHIJ") == "AB…(10 chars)"
    assert creds.mask("abc") == "…(3 chars)"
    assert creds.mask("abcdefghijklmnop" * 4).startswith("abcd…")


# ---- the transcript scan ----------------------------------------------------------------------------------------


def transcript(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "user", "message": {"content": text}}) + "\n", encoding="utf-8"
    )
    return path


def test_the_transcript_scan_reads_subagent_and_workflow_transcripts_too(capsys):
    root = paths.claude_home() / "projects" / "proj"
    transcript(root / "s.jsonl", f"token {GH_TOKEN}")
    transcript(root / "s" / "subagents" / "agent-1.jsonl", f"key {AWS_LONG_ID}")
    transcript(
        root / "s" / "subagents" / "workflows" / "wf_1" / "agent-2.jsonl", f"again {GH_TOKEN_2}"
    )
    old = transcript(root / "old" / "subagents" / "agent-3.jsonl", f"stale {GH_TOKEN}")
    t = time.time() - 40 * 86400
    os.utime(old, (t, t))
    hits, scanned = creds.scan_transcripts(7)
    assert scanned == 3
    assert sorted((Path(f).name, kind) for f, kind, n in hits) == [
        ("agent-1.jsonl", "AWS access key id"),
        ("agent-2.jsonl", "GitHub token"),
        ("s.jsonl", "GitHub token"),
    ]
    assert creds.scan_transcripts(7, max_mb=0) == ([], 0)
    rc = ctl._run(["creds", "scan"])
    out = capsys.readouterr().out
    assert rc == 1 and "scanned 3 transcript file(s)" in out and "agent-2.jsonl" in out
    no_raw(out, repr(hits))

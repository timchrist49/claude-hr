from claude_hr import config


def test_defaults_load_and_have_expected_shape(sandbox):
    cfg = config.load()
    assert cfg.get("headroom.port") == 8787
    assert cfg.get("headroom.proxy_mode") == "cache"
    assert cfg.flag("headroom.fail_open") is True
    assert cfg.get("resources.session_cap") == 6


def test_env_overrides_parse_types(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__PORT", "9000")
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__ENABLED", "false")
    monkeypatch.setenv("CLAUDE_HR_CLAUDE__CHANNEL", "latest")
    cfg = config.load()
    assert cfg.get("headroom.port") == 9000
    assert cfg.flag("headroom.enabled") is False
    assert cfg.get("claude.channel") == "latest"


def test_user_file_overrides_defaults_but_env_wins(sandbox, monkeypatch):
    (sandbox / ".claude-hr").mkdir()
    (sandbox / ".claude-hr" / "config.toml").write_text('[headroom]\nport = 9100\nmode = "wrap"\n')
    assert config.load().get("headroom.port") == 9100
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__PORT", "9200")
    cfg = config.load()
    assert cfg.get("headroom.port") == 9200 and cfg.get("headroom.mode") == "wrap"
    assert cfg.get("headroom.savings_profile") == "coding"  # untouched keys survive the merge


def test_broken_user_config_is_reported(sandbox):
    (sandbox / ".claude-hr").mkdir()
    (sandbox / ".claude-hr" / "config.toml").write_text("[headroom\nport=")
    try:
        config.load()
    except ValueError as exc:
        assert "invalid TOML" in str(exc)
    else:
        raise AssertionError("expected ValueError")
    assert config.load(with_user=False).get("headroom.port") == 8787

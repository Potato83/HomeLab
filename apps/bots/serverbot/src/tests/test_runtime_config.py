import pytest

from serverbot.config import ConfigError, load_config, load_env_config


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    for name in ("ALLOWED_USER_IDS", "ADMIN_ID", "DAILY_REPORT_IDS", "VPN"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("ids", ["", "123,bad", "0", "-123", "123,"])
def test_invalid_whitelist_prevents_startup(env, ids):
    env.setenv("ALLOWED_USER_IDS", ids)
    with pytest.raises(ConfigError):
        load_env_config()


def test_daily_recipients_default_to_whitelist(env):
    env.setenv("ALLOWED_USER_IDS", "123, 456,123")
    config = load_env_config()
    assert config.allowed_user_ids == frozenset({123, 456})
    assert config.report_user_ids == config.allowed_user_ids


def test_daily_recipients_must_be_allowed(env):
    env.setenv("ALLOWED_USER_IDS", "123")
    env.setenv("DAILY_REPORT_IDS", "456")
    with pytest.raises(ConfigError):
        load_env_config()


def test_legacy_admin_id_still_works(env):
    env.setenv("ADMIN_ID", "123")
    assert load_env_config().allowed_user_ids == frozenset({123})


@pytest.mark.parametrize("settings", [
    'url = "ftp://localhost/health"',
    'url = "http://user:password@localhost/health"',
    'url = "http://localhost/health"\ntimeout = 0',
    'url = "http://localhost/health"\ntimeout = nan',
    'url = "http://localhost/health"\nexpected_status = 999',
])
def test_invalid_http_config_is_rejected(tmp_path, settings):
    path = tmp_path / "config.toml"
    path.write_text('[[checks.disk]]\npath = "/"\n[[checks.http]]\nname = "Service"\n' + settings)
    with pytest.raises(ConfigError):
        load_config(path)


def test_http_checks_are_optional_and_extendable(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[checks.disk]]\npath = "/"\n')
    assert load_config(path).http_checks == ()
    with path.open("a") as file:
        file.write('[[checks.http]]\nname = "Service"\nurl = "http://localhost/health"\n')
    assert load_config(path).http_checks[0].timeout == 5.0

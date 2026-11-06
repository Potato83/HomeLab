import pytest

from serverbot.config import ConfigError, load_config


def test_invalid_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[[checks.disk]\n")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("contents", [
    '[[checks.disk]]\npath = ""',
    '[[checks.disk]]\npath = "/"\nwarning = 90\ncritical = 90',
    '[[checks.disk]]\npath = "/"\nwarning = -1',
    '[[checks.disk]]\npath = "/"\ncritical = 101',
    '[[checks.disk]]\npath = "/"\n[[checks.disk]]\npath = "/"',
    '[monitoring]\ninterval_seconds = 0\n[[checks.disk]]\npath = "/"',
    '[monitoring]\ninterval_seconds = true\n[[checks.disk]]\npath = "/"',
])
def test_invalid_config(tmp_path, contents):
    path = tmp_path / "config.toml"
    path.write_text(contents)
    with pytest.raises(ConfigError):
        load_config(path)


def test_monitoring_settings_and_relative_state_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[monitoring]\ninterval_seconds = 60\nstate_file = "var/state.json"\n[[checks.disk]]\npath = "/"')
    config = load_config(path)
    assert config.check_interval_seconds == 60
    assert config.state_file == tmp_path / "var/state.json"


def test_temperature_config_and_thresholds(tmp_path):
    path = tmp_path / "config.toml"
    contents = '''[[checks.disk]]
path = "/"
[[checks.temperature]]
name = "CPU"
source = "hwmon"
driver = "k10temp"
sensor = "temp1"
warning = 70.0
critical = 85.0
'''
    path.write_text(contents)
    assert load_config(path).temperatures[0].warning == 70.0
    path.write_text(contents.replace("warning = 70.0", "warning = 85.0"))
    with pytest.raises(ConfigError):
        load_config(path)


def test_duplicate_http_names_are_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[checks.disk]]\npath = "/"\n' +
                    '[[checks.http]]\nname = "API"\nurl = "http://localhost/health"\n' * 2)
    with pytest.raises(ConfigError, match="must be unique"):
        load_config(path)

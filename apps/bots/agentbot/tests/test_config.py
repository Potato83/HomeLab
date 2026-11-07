import pytest

from agentbot.config import load


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv('OPENCODE_COMMAND', raising=False)
    monkeypatch.delenv('PROXY_URL', raising=False)
    monkeypatch.setenv('BOT_TOKEN', 'dummy-token')
    monkeypatch.setenv('OWNER_ID', '123')
    monkeypatch.setenv('OPENCODE_DIRECTORY', '/dummy/project')
    monkeypatch.setenv('OPENCODE_SERVER_PASSWORD', 'dummy-password')
    monkeypatch.setenv('OPENCODE_URL', 'http://127.0.0.1:4096')


def test_config_repr_excludes_credentials(env):
    text = repr(load())
    assert 'dummy-token' not in text and 'dummy-password' not in text


@pytest.mark.parametrize('url', ['http://10.0.0.1:4096', 'https://user:pass@example.test', 'https://example.test/?key=secret', 'file:///tmp/api'])
def test_unsafe_url_rejected(env, monkeypatch, url):
    monkeypatch.setenv('OPENCODE_URL', url)
    with pytest.raises(ValueError):
        load()


def test_command_transport_requires_no_api_password(env, monkeypatch):
    monkeypatch.delenv('OPENCODE_SERVER_PASSWORD')
    monkeypatch.setenv('OPENCODE_COMMAND', 'ssh -F "/example/config with spaces" api-helper')
    settings = load()
    assert settings.password == ''
    assert settings.command == ('ssh', '-F', '/example/config with spaces', 'api-helper')
    assert '/example/config with spaces' not in repr(settings)


@pytest.mark.parametrize('command', ['', ' ', 'ssh "unterminated'])
def test_missing_or_malformed_transport_rejected(env, monkeypatch, command):
    monkeypatch.delenv('OPENCODE_SERVER_PASSWORD')
    monkeypatch.setenv('OPENCODE_COMMAND', command)
    with pytest.raises(ValueError):
        load()


def test_command_transport_still_requires_absolute_directory(env, monkeypatch):
    monkeypatch.setenv('OPENCODE_COMMAND', 'api-helper')
    monkeypatch.setenv('OPENCODE_DIRECTORY', 'relative')
    with pytest.raises(ValueError):
        load()


def test_proxy_optional_and_credentials_hidden(env, monkeypatch):
    assert load().proxy == ''
    monkeypatch.setenv('PROXY_URL', 'http://dummy-user:private-password@localhost:2080')
    settings = load()
    assert settings.proxy.startswith('http://dummy-user:')
    assert 'dummy-user' not in repr(settings)
    assert 'private-password' not in repr(settings)


@pytest.mark.parametrize('scheme', ['http', 'https', 'socks4', 'socks5'])
def test_proxy_supported_scheme(env, monkeypatch, scheme):
    monkeypatch.setenv('PROXY_URL', f'{scheme}://localhost:2080')
    assert load().proxy == f'{scheme}://localhost:2080'


@pytest.mark.parametrize('url', ['file:///tmp/proxy', 'ftp://localhost', 'http://',
                               'http://localhost:not-a-port', 'http://localhost:2080/?secret=value'])
def test_invalid_proxy_rejected_without_disclosure(env, monkeypatch, url):
    monkeypatch.setenv('PROXY_URL', url)
    with pytest.raises(ValueError, match='Invalid PROXY_URL'):
        load()

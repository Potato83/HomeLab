import logging

from potato_bot.logging_setup import SafeFormatter


def test_environment_token_and_proxy_are_redacted(monkeypatch):
    token = "123456789:example_test_token"
    proxy = "socks5://user:password@localhost:2080"
    monkeypatch.setenv("BOT_TOKEN", token)
    monkeypatch.setenv("PROXY_URL", proxy)
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "failed %s %s", (token, proxy), None)
    result = SafeFormatter("%(message)s").format(record)
    assert token not in result
    assert proxy not in result
    assert result.count("[REDACTED]") == 2


def test_exception_traceback_is_redacted(monkeypatch):
    secret = "example-private-secret"
    monkeypatch.setenv("EXAMPLE_API_KEY", secret)
    try:
        raise ValueError(secret)
    except ValueError as error:
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "failure", (), (type(error), error, error.__traceback__))
    result = SafeFormatter("%(message)s").format(record)
    assert secret not in result
    assert "ValueError: [REDACTED]" in result

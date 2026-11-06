import asyncio
from unittest.mock import AsyncMock, MagicMock

from serverbot.collectors.http import HttpReading
from serverbot.config import AppConfig, HttpCheckConfig
from serverbot.handlers import build_status_report, format_report_messages


def test_failed_service_does_not_hide_other_services(monkeypatch):
    monkeypatch.setattr("serverbot.handlers.build_disk_report", lambda disks: "Disk report")
    reader = AsyncMock(side_effect=[
        HttpReading("First", False, None, "Timeout"),
        HttpReading("Second", True, 200, None),
    ])
    monkeypatch.setattr("serverbot.handlers.get_http_status", reader)
    config = AppConfig((), http_checks=(
        HttpCheckConfig("First", "http://localhost/first"),
        HttpCheckConfig("Second", "http://localhost/second"),
    ))
    report = asyncio.run(build_status_report(config, MagicMock()))
    assert "Disk report" in report
    assert "HTTP/API:" in report
    assert "Timeout" in report
    assert "HTTP 200" in report
    assert reader.await_count == 2


def test_no_http_checks_are_reported_explicitly(monkeypatch):
    monkeypatch.setattr("serverbot.handlers.build_disk_report", lambda disks: "Disk report")
    reader = AsyncMock()
    monkeypatch.setattr("serverbot.handlers.get_http_status", reader)
    report = asyncio.run(build_status_report(AppConfig(()), MagicMock()))
    assert "HTTP/API:\nNo HTTP checks configured" in report
    reader.assert_not_awaited()


def test_unexpected_http_status_shows_expected_code(monkeypatch):
    monkeypatch.setattr("serverbot.handlers.build_disk_report", lambda disks: "Disk report")
    reader = AsyncMock(return_value=HttpReading("Service", False, 401, "Unexpected HTTP status"))
    monkeypatch.setattr("serverbot.handlers.get_http_status", reader)
    config = AppConfig((), http_checks=(HttpCheckConfig("Service", "http://localhost/health"),))
    report = asyncio.run(build_status_report(config, MagicMock()))
    assert "!!!" in report
    assert "HTTP 401, expected 200" in report


def test_report_is_escaped_and_split_before_sending():
    report = "<service>&" * 800
    messages = format_report_messages(report)
    assert len(messages) > 1
    for text in messages:
        assert text.startswith("<pre>") and text.endswith("</pre>")
        assert "<service>" not in text
    from html import unescape
    assert "".join(unescape(text[5:-6]) for text in messages) == report

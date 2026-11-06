import asyncio
from unittest.mock import AsyncMock, MagicMock

from serverbot.collectors.disk import DiskUsageInfo
from serverbot.collectors.http import HttpReading
from serverbot.collectors.temperature import TemperatureReading
from serverbot.config import (
    AppConfig,
    DiskMountConfig,
    HttpCheckConfig,
    TemperatureCheckConfig,
)
from serverbot.monitoring import CheckResult, check_and_notify, collect_checks
from serverbot.state import StateError, StateStore


def result(status):
    return CheckResult("disk:/", "Disk /", status, "Test reading")


def test_alerts_only_on_transitions_and_recovery(tmp_path, monkeypatch):
    async def run():
        readings = AsyncMock(side_effect=[[result(status)] for status in ("ok", "warning", "warning", "critical", "critical", "warning", "ok", "ok")])
        monkeypatch.setattr("serverbot.monitoring.collect_checks", readings)
        bot = MagicMock(send_message=AsyncMock())
        state = StateStore(tmp_path / "state.json")
        for _ in range(8):
            await check_and_notify(bot, AppConfig(()), MagicMock(), frozenset({123}), state)
        texts = [call.args[1] for call in bot.send_message.await_args_list]
        assert len(texts) == 4
        assert "OK -&gt; WARNING" in texts[0]
        assert "WARNING -&gt; CRITICAL" in texts[1]
        assert "CRITICAL -&gt; WARNING" in texts[2]
        assert "RECOVERED" in texts[3]
        assert state.states["123"]["disk:/"] == "ok"
    asyncio.run(run())


def test_initial_problem_is_sent_once_across_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("serverbot.monitoring.collect_checks", AsyncMock(return_value=[result("error")]))
    path = tmp_path / "state.json"
    bot = MagicMock(send_message=AsyncMock())
    asyncio.run(check_and_notify(bot, AppConfig(()), MagicMock(), frozenset({123}), StateStore(path)))
    asyncio.run(check_and_notify(bot, AppConfig(()), MagicMock(), frozenset({123}), StateStore(path)))
    assert bot.send_message.await_count == 1


def test_failed_recipient_is_retried_without_repeating_successful_delivery(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr("serverbot.monitoring.collect_checks", AsyncMock(return_value=[result("critical")]))
        bot = MagicMock(send_message=AsyncMock(side_effect=[OSError("Failed"), None, None]))
        state = StateStore(tmp_path / "state.json")
        await check_and_notify(bot, AppConfig(()), MagicMock(), frozenset({123, 456}), state)
        assert "disk:/" not in state.states["123"]
        assert state.states["456"]["disk:/"] == "critical"
        restarted = StateStore(state.path)
        await check_and_notify(bot, AppConfig(()), MagicMock(), frozenset({123, 456}), restarted)
        assert [call.args[0] for call in bot.send_message.await_args_list] == [123, 456, 123]
    asyncio.run(run())


def test_removed_checks_and_recipients_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr("serverbot.monitoring.collect_checks", AsyncMock(return_value=[result("ok")]))
    state = StateStore(tmp_path / "state.json")
    state.save({"123": {"disk:/": "ok", "old": "error"}, "456": {"disk:/": "warning"}})
    bot = MagicMock(send_message=AsyncMock())
    asyncio.run(check_and_notify(bot, AppConfig(()), MagicMock(), frozenset({123}), state))
    assert state.states == {"123": {"disk:/": "ok"}}
    bot.send_message.assert_not_awaited()


def test_all_collector_results_are_classified(monkeypatch):
    monkeypatch.setattr("serverbot.monitoring.GetDiskUsage", lambda path: DiskUsageInfo(path, 100, 90, 10, 90))
    monkeypatch.setattr("serverbot.monitoring.get_temperature", lambda check: TemperatureReading(check.name, 70, None))
    monkeypatch.setattr("serverbot.monitoring.get_http_status", AsyncMock(side_effect=[
        HttpReading("Web", False, 503, "Unexpected HTTP status"),
        HttpReading("API", False, None, "Timeout"),
    ]))
    config = AppConfig(
        (DiskMountConfig("/", 80, 90),),
        (TemperatureCheckConfig("CPU", "hwmon", "k10temp", "temp1", 70, 85),),
        (HttpCheckConfig("Web", "http://localhost/web"), HttpCheckConfig("API", "http://localhost/api")),
    )
    readings = asyncio.run(collect_checks(config, MagicMock()))
    assert [reading.status for reading in readings] == ["critical", "warning", "critical", "error"]


def test_disk_error_does_not_hide_other_checks(monkeypatch):
    def disk_usage(path):
        raise PermissionError("Denied")
    monkeypatch.setattr("serverbot.monitoring.GetDiskUsage", disk_usage)
    monkeypatch.setattr("serverbot.monitoring.get_temperature", lambda check: TemperatureReading(check.name, None, "Sensor not found"))
    config = AppConfig((DiskMountConfig("/", 80, 90),), (TemperatureCheckConfig("CPU", "hwmon", "k10temp", "temp1", 70, 85),))
    readings = asyncio.run(collect_checks(config, MagicMock()))
    assert len(readings) == 2
    assert all(reading.status == "error" for reading in readings)


def test_failed_state_write_is_logged_and_does_not_advance_memory(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("serverbot.monitoring.collect_checks", AsyncMock(return_value=[result("warning")]))
    state = StateStore(tmp_path / "state.json")

    def save(states):
        raise StateError("private-error-details")
    monkeypatch.setattr(state, "save", save)
    bot = MagicMock(send_message=AsyncMock())
    asyncio.run(check_and_notify(bot, AppConfig(()), MagicMock(), frozenset({123}), state))
    assert state.states == {}
    assert "monitoring_state_save_failed" in caplog.text
    assert "private-error-details" not in caplog.text

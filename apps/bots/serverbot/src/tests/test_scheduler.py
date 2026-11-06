import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from serverbot.config import AppConfig
from serverbot.logging_setup import SafeFormatter
from serverbot.scheduler import create_scheduler, send_daily_report
from serverbot.state import StateStore


def test_job_runs_at_eight_moscow_independent_of_host_timezone():
    scheduler = create_scheduler(MagicMock(), AppConfig(()), MagicMock(), frozenset({123}))
    job = scheduler.get_job("daily_report")
    now = datetime(2026, 10, 12, 4, 59, tzinfo=UTC)
    next_run = job.trigger.get_next_fire_time(None, now)
    assert next_run.astimezone(ZoneInfo("Europe/Moscow")).hour == 8
    assert next_run.astimezone(UTC).hour == 5
    assert next_run.minute == 0
    after = datetime(2026, 10, 12, 5, 1, tzinfo=UTC)
    assert job.trigger.get_next_fire_time(None, after).day == 13
    assert job.max_instances == 1


def test_scheduler_starts_and_stops_in_asyncio():
    async def run():
        scheduler = create_scheduler(MagicMock(), AppConfig(()), MagicMock(), frozenset({123}))
        scheduler.start()
        assert scheduler.running
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)
        assert not scheduler.running
    asyncio.run(run())


def test_periodic_monitoring_uses_interval_and_alert_recipients(tmp_path):
    state = StateStore(tmp_path / "state.json")
    scheduler = create_scheduler(MagicMock(), AppConfig((), check_interval_seconds=60), MagicMock(),
                                 frozenset({123}), state=state, alert_user_ids=frozenset({123, 456}))
    job = scheduler.get_job("monitoring")
    assert job.trigger.interval.total_seconds() == 60
    assert job.args[3] == frozenset({123, 456})
    assert job.next_run_time is not None
    assert job.max_instances == 1


def test_one_failed_recipient_does_not_block_others(monkeypatch):
    monkeypatch.setattr("serverbot.scheduler.build_mobile_status", AsyncMock(return_value="<b>HomeLab</b> &lt;service&gt; ok"))
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[OSError("Failed"), None])
    asyncio.run(send_daily_report(bot, AppConfig(()), MagicMock(), frozenset({123, 456})))
    assert [call.args[0] for call in bot.send_message.await_args_list] == [123, 456]
    assert "&lt;service&gt;" in bot.send_message.await_args_list[1].args[1]


def test_formatter_redacts_tokens_and_service_keys(monkeypatch):
    import logging
    monkeypatch.setenv("BOT_TOKEN", "my-private-token")
    monkeypatch.setenv("SERVICE_API_KEY", "my-private-key")
    record = logging.LogRecord("test", logging.ERROR, "", 0, "my-private-token my-private-key", (), None)
    text = SafeFormatter("%(message)s").format(record)
    assert "my-private-token" not in text
    assert "my-private-key" not in text

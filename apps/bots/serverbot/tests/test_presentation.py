import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path
from serverbot.config import AppConfig, SystemConfig
from serverbot.collectors.host import HostReading
from serverbot import presentation


def system():
    return SimpleNamespace(cpu_percent=5.0,memory_used_bytes=6*1024**3,
                           memory_total_bytes=16*1024**3,memory_available_bytes=10*1024**3,uptime_seconds=4000)


def test_mobile_status_is_compact_and_does_not_call_missing_services_healthy(monkeypatch):
    monkeypatch.setattr(presentation,'get_system_reading',AsyncMock(return_value=system()))
    text=asyncio.run(presentation.build_mobile_status(AppConfig((),system=SystemConfig()),None))
    assert '<pre>' not in text
    assert '<b>Ресурсы</b>' in text
    assert '⚪ ServerBot' in text
    assert 'RAM · 38%' in text
    assert len(text.encode('utf-16-le'))//2 < 3500


def test_mobile_status_puts_problems_first_and_escapes_external_text(monkeypatch):
    monkeypatch.setattr(presentation,'get_system_reading',AsyncMock(return_value=system()))
    monkeypatch.setattr(presentation,'read_host_snapshot',lambda p:[
        HostReading('test','<secret>','critical','<script>&')])
    text=asyncio.run(presentation.build_mobile_status(AppConfig((),host_snapshot=Path('/host.json')),None))
    assert text.index('Что проверить') < text.index('Ресурсы')
    assert '&lt;secret&gt;' in text and '&lt;script&gt;&amp;' in text
    assert '<script>' not in text and '🟢 <b>HomeLab</b>' not in text


def test_mobile_status_reports_stale_collector_as_unknown(monkeypatch):
    monkeypatch.setattr(presentation,'get_system_reading',AsyncMock(side_effect=OSError))
    monkeypatch.setattr(presentation,'read_host_snapshot',lambda p:[
        HostReading('host:collector','Host collector','error','stale')])
    text=asyncio.run(presentation.build_mobile_status(AppConfig((),host_snapshot=Path('/host.json')),None))
    assert 'Не удалось получить показатели' in text
    assert 'Состояние служб сейчас неизвестно' in text
    assert '⚪ Kali' in text


def detail_system():
    s=system()
    s.load_average=(0.1,0.2,0.3)
    s.top_cpu=(SimpleNamespace(name='<process>&',cpu_percent=7.5,memory_bytes=1024**2),)
    s.top_memory=s.top_cpu
    return s


def test_detail_pages_are_bounded_valid_html_and_escape_process_names(monkeypatch):
    from html.parser import HTMLParser
    class Markup(HTMLParser):
        def __init__(self): super().__init__(); self.stack=[]
        def handle_starttag(self,tag,attrs):
            assert tag in {'b','i'}
            self.stack.append(tag)
        def handle_endtag(self,tag): assert self.stack.pop()==tag
    monkeypatch.setattr(presentation,'get_system_reading',AsyncMock(return_value=detail_system()))
    monkeypatch.setattr(presentation,'read_host_snapshot',lambda p:[])
    for page in presentation.DETAIL_PAGES:
        text=asyncio.run(presentation.build_detail_page(AppConfig(()),None,page))
        parser=Markup();parser.feed(text);assert not parser.stack
        assert '<pre>' not in text and '\t' not in text
        assert len(text.encode('utf-16-le'))//2 < 3500
        if page=='resources':
            assert '&lt;process&gt;&amp;' in text
            assert '▰' in text


def test_detail_bots_expose_source_failure_without_marking_process_dead(monkeypatch):
    monkeypatch.setattr(presentation,'read_host_snapshot',lambda p:[
        HostReading('service:workbot','WorkBot','ok','OpenRC running'),
        HostReading('bot:workbot','WorkBot process','ok','Expected one app process; found 1'),
        HostReading('bot:workbot-db','Sources','warning','Source errors: 2; database readable')])
    text=asyncio.run(presentation.build_detail_page(AppConfig((),host_snapshot=Path('/host.json')),None,'bots'))
    assert '🟢 Служба запущена' in text and '🟢 Процессов: 1' in text
    assert '🟡 Ошибок источников: 2' in text


def test_detail_navigation_only_contains_known_pages():
    from serverbot.handlers import detail_keyboard
    buttons=[b for row in detail_keyboard('resources').inline_keyboard for b in row]
    allowed={'status:summary',*('status:page:'+p for p in presentation.DETAIL_PAGES)}
    assert {b.callback_data for b in buttons} == allowed
    import pytest
    with pytest.raises(ValueError):
        asyncio.run(presentation.build_detail_page(AppConfig(()),None,'/etc/shadow'))


def test_details_callback_edits_existing_message_with_card_navigation(monkeypatch):
    from aiogram.types import Message
    from unittest.mock import MagicMock
    from serverbot import handlers
    handlers._last_command_at.clear()
    reader=AsyncMock(return_value='<b>Ресурсы</b>')
    monkeypatch.setattr(handlers,'build_detail_page',reader)
    message=MagicMock(spec=Message)
    message.edit_text=AsyncMock()
    callback=SimpleNamespace(data='status:details',message=message,
                             from_user=SimpleNamespace(id=123),answer=AsyncMock())
    asyncio.run(handlers.status_callback_handler(callback,AppConfig(()),None))
    reader.assert_awaited_once()
    assert reader.await_args.args[2]=='resources'
    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.kwargs['parse_mode']=='HTML'
    callback.answer.assert_awaited_once()
    handlers._last_command_at.clear()


def test_unknown_callback_never_reads_host_or_builds_card(monkeypatch):
    from serverbot import handlers
    reader=AsyncMock()
    monkeypatch.setattr(handlers,'build_detail_page',reader)
    callback=SimpleNamespace(data='status:page:/etc/shadow',answer=AsyncMock())
    asyncio.run(handlers.status_callback_handler(callback,AppConfig(()),None))
    reader.assert_not_awaited()
    callback.answer.assert_awaited_once()


def test_gateway_warning_is_visible_in_summary_and_details(monkeypatch):
    monkeypatch.setattr(presentation,'get_system_reading',AsyncMock(return_value=system()))
    monkeypatch.setattr(presentation,'read_host_snapshot',lambda p:[
        HostReading('service:omniroute','OmniRoute','ok','OpenRC running'),
        HostReading('service:nginx','Nginx','ok','OpenRC running'),
        HostReading('gateway:health','OmniRoute API','critical','Unexpected gateway response')])
    config=AppConfig((),host_snapshot=Path('/host.json'))
    summary=asyncio.run(presentation.build_mobile_status(config,None))
    assert '🔴 OmniRoute · API' in summary
    details=asyncio.run(presentation.build_detail_page(config,None,'network'))
    assert '🔴 <b>OmniRoute · API</b>' in details


def test_manual_stop_is_neutral_and_explicit_in_status_and_details(monkeypatch):
    monkeypatch.setattr(presentation, 'get_system_reading', AsyncMock(return_value=system()))
    keys = ['service:chatbot', 'bot:chatbot', 'bot:chatbot-db', 'vm:kali', 'vm:ssh',
            'service:omniroute', 'gateway:health']
    monkeypatch.setattr(presentation, 'read_host_snapshot', lambda p: [
        HostReading(key, key, 'ok', 'Stopped manually') for key in keys])
    config = AppConfig((), host_snapshot=Path('/host.json'))
    summary = asyncio.run(presentation.build_mobile_status(config, None))
    assert 'Что проверить' not in summary
    assert '⚪ ChatBot · остановлен вручную' in summary
    assert '⚪ Kali · остановлена вручную' in summary
    assert '⚪ OmniRoute · остановлен вручную' in summary
    details = asyncio.run(presentation.build_detail_page(config, None, 'bots'))
    assert '⚪ Остановлен вручную' in details

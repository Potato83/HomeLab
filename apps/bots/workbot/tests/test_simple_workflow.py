"""The reduced user workflow and its reliability boundaries."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from workbot.config import load_config, validate_config
from workbot.http import HttpClient, FetchError
from workbot.matching import decide
from workbot.models import Vacancy
from workbot.runner import Monitor
from workbot.service import scan
from workbot.sources import parse_kaspersky_detail, ParseError
from workbot.storage import Store
from workbot.structured import enrich_job
from workbot.telegram import Telegram, deliver

@pytest.fixture
def config():
    result = load_config(str(Path(__file__).parents[1] / 'config.toml'))
    result['sources'] = {k: v for k, v in result['sources'].items() if k in ('kaspersky','bastion')}
    return result

@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / 'private' / 'state.sqlite3')
    yield db
    db.close()

def job(**changes):
    return Vacancy(**(dict(source='kaspersky', external_id='1', title='Senior SOC', company='Example',
                           url='https://careers.kaspersky.ru/vacancy/1') | changes))

def test_keywords_are_the_only_role_and_location_filters(config):
    rules = config['filters']
    for changes in [dict(level='experienced'), dict(cities=['Москва'], work_format='office'),
                    dict(title='Backend', description='Разработка SIEM')]:
        assert decide(job(**changes), rules).matched
    assert not decide(job(title='Accountant'), rules).matched
    assert not decide(job(active=False), rules).matched
    assert not decide(job(), rules | {'exclude_words':['SOC']}).matched
    assert not decide(job(title='Ошибка', description=''), rules | {'include_words':['ИБ']}).matched

@pytest.mark.parametrize('value', [[], [' '], ['a'*61], ['a']*33, 'SOC', [1]])
def test_invalid_keywords(value, config):
    config['keywords']['include'] = value
    with pytest.raises(ValueError):
        validate_config(config)

def test_literal_words_and_unicode(config):
    rules = config['filters'] | {'include_words':['C++', 'стажёр']}
    assert decide(job(title='C++ engineer'), rules).matched
    assert decide(job(title='СТАЖЕР'), rules).matched
    assert not decide(job(title='CCCC engineer'), rules).matched

def test_unknown_registration_is_visible_and_expired_event_is_hidden(config):
    v = job(kind='event', title='CTF', recruitment_status='unknown',
            start_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
            end_at=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat())
    assert decide(v, config['filters']).warnings
    v.end_at = '2020-01-01T00:00:00Z'
    assert not decide(v, config['filters']).matched

def test_delivery_history_survives_old_state_and_restart(config, store):
    v = job()
    store.save_snapshot(v.source, [(v, decide(v,config['filters']))])
    with store.db:
        store.db.execute('CREATE TABLE career_details (note TEXT)')
        store.db.execute("INSERT INTO career_details VALUES('private legacy note')")
        store.db.execute('UPDATE vacancies SET generation=4,sent_generation=4')
    store.save_snapshot(v.source, [(v, decide(v,config['filters']))])
    assert store.pending([v.source], 20) == []
    assert store.db.execute('SELECT note FROM career_details').fetchone()[0] == 'private legacy note'
    path = Path(store.db.execute('PRAGMA database_list').fetchone()[2])
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700

def test_failed_delivery_retries_and_cross_source_duplicates_are_suppressed(config, store):
    v = job(cities=['СПб'],work_format='remote')
    store.save_snapshot(v.source, [(v,decide(v,config['filters']))])
    telegram = Mock()
    telegram.send.side_effect = FetchError('offline')
    with pytest.raises(FetchError):
        deliver(store,telegram,[v.source],10,delay=0,rules=config['filters'])
    assert len(store.pending([v.source],10)) == 1
    telegram.send.side_effect = None
    assert deliver(store,telegram,[v.source],10,delay=0,rules=config['filters']) == 1
    duplicate = job(source='bastion',external_id='other',cities=['СПб'],work_format='remote')
    store.save_snapshot('bastion',[(duplicate,decide(duplicate,config['filters']))])
    assert deliver(store,telegram,['kaspersky','bastion'],10,delay=0,rules=config['filters']) == 0
    assert telegram.send.call_count == 2

def test_source_error_preserves_snapshot_and_other_source_continues(config,store):
    v = job()
    store.save_snapshot(v.source,[(v,decide(v,config['filters']))])
    with patch('workbot.service.collect',side_effect=[KeyError('SECRET'),[]]):
        result = scan(config,store,client=Mock())
    assert 'SECRET' not in result['sources']['kaspersky']['error']
    assert result['sources']['bastion']['error'] is None
    assert store.db.execute('SELECT active FROM vacancies').fetchone()[0] == 1
    assert store.pending(['kaspersky'],10) == []

def test_owner_commands_work_during_scan_and_stale_results_are_discarded(config,store):
    tg = Telegram('dummy','42')
    tg.send = Mock()
    monitor = Monitor(config,store,tg)
    worker = Mock(revision=0,due=['kaspersky'])
    worker.poll.return_value = ({'sources':{},'vacancies':[]},None)
    monitor.background = worker
    update = {'message':{'chat':{'id':42,'type':'private'},'from':{'id':42},
                         'text':'/keywords Python, CTF'}}
    monitor.handle(update)
    assert config['keywords']['include'] == ['Python','CTF']
    worker.close.assert_called_once()
    monitor.run_background()
    assert monitor.background is None
    tg.send.assert_called_once()
    restarted = Monitor(deepcopy(config),store,tg)
    assert restarted.config['keywords']['include'] == ['Python','CTF']
    monitor.close()

def test_jobs_and_events_use_saved_snapshots_without_new_fetch(config,store):
    v = job()
    store.save_snapshot(v.source,[(v,decide(v,config['filters']))])
    tg = Telegram('dummy','42');tg.send = Mock()
    monitor = Monitor(config,store,tg)
    with patch('workbot.runner.BackgroundScan') as background:
        monitor.handle({'message':{'chat':{'id':42,'type':'private'},'from':{'id':42},'text':'/jobs SOC'}})
        background.assert_not_called()
    assert 'Senior SOC' in tg.send.call_args.args[0]

def test_jsonld_graph_survives_changed_css_and_is_scoped():
    node = {'@type':'JobPosting','title':'SOC analyst','description':'<p>SIEM &amp; logs</p>',
            'url':'https://careers.kaspersky.ru/vacancy/1','jobLocationType':'TELECOMMUTE'}
    html = '<script type="application/ld+json">' + json.dumps({'@graph':[node]}) + '</script>'
    v = parse_kaspersky_detail(html,job())
    assert v.title == 'SOC analyst' and v.work_format == 'remote'
    assert v.description == 'SIEM & logs'
    assert not enrich_job(html,job(url='https://careers.kaspersky.ru/vacancy/2'))
    assert not enrich_job('<script type="application/ld+json">{broken</script>',job())

def test_bounded_retry_and_long_retry_after_are_respected():
    client = HttpClient(delay=0)
    with patch.object(client,'_get_once',side_effect=[FetchError('HTTP 429',retry_after=3),'ok']) as fetch, patch.object(client,'_wait') as wait:
        assert client.get('https://example.org') == 'ok'
        wait.assert_called_once_with(3)
        assert fetch.call_count == 2
    with patch.object(client,'_get_once',side_effect=FetchError('HTTP 429',retry_after=120)) as fetch, patch.object(client,'_wait') as wait:
        with pytest.raises(FetchError):client.get('https://example.org')
        wait.assert_not_called()
        assert fetch.call_count == 1
    with patch.object(client,'_get_once',side_effect=FetchError('HTTP 403')) as fetch:
        with pytest.raises(FetchError):client.get('https://example.org')
        assert fetch.call_count == 1

def test_yandex_programme_without_detail_link_keeps_own_identity():
    from workbot.sources import parse_yandex
    html = '<section class="lc-features__item"><div class="card"><div class="recruitment">Набор открыт</div><h4 class="card-heading">DevOps</h4></div></section>'
    first = parse_yandex(html, 'https://yandex.ru/yaintern/internship')[0]
    assert first.url == 'https://yandex.ru/yaintern/internship'
    assert first.external_id.startswith('direction-')
    assert first.external_id == parse_yandex(html,'https://yandex.ru/yaintern/internship')[0].external_id

def test_slow_provider_cannot_use_the_next_providers_time():
    client = HttpClient(scan_timeout=600)
    original = client.deadline
    with client.source_window(3):
        assert 190 < original - client.deadline < 410
    assert client.deadline == original

def test_expired_queue_entry_does_not_block_live_announcements(config,store):
    v = job(kind='event', title='CTF', start_at='2020-01-01T00:00:00Z', end_at='2020-01-02T00:00:00Z')
    from workbot.models import Decision
    store.save_snapshot(v.source,[(v,Decision(True,'CTF'))])
    telegram = Mock()
    assert deliver(store,telegram,[v.source],10,delay=0,rules=config['filters']) == 0
    assert store.pending([v.source],10) == []
    telegram.send.assert_not_called()

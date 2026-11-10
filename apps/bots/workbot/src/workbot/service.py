import logging
from .http import FetchError, HttpClient, RequestCancelled
from .matching import decide
from .models import Vacancy, Decision
from .sources import ParseError, collect
from .directions import collection_rules, source_settings

log = logging.getLogger(__name__)

def scan(config, store=None, client=None):
    m = config['monitor']
    client = client or HttpClient(m['request_timeout_seconds'], m['request_delay_seconds'],
                                  scan_timeout=m['scan_timeout_seconds'])
    output = {'sources': {}, 'vacancies': []}
    rules = collection_rules(config['filters'])
    for name, source in config['sources'].items():
        if not source.get('enabled'):
            continue
        try:
            rows = collect(name, source_settings(name, source, rules), client, rules)
            records = [(v, decide(v, rules)) for v in rows]
            if len({v.external_id for v, _ in records}) != len(records):
                raise ParseError('Источник вернул повторные идентификаторы')
            if any(v.source != name for v, _ in records):
                raise ParseError('Источник вернул чужие карточки')
            summary = {'found': len(records), 'matched': sum(d.matched for _, d in records), 'error': None}
            output['vacancies'].extend({'vacancy': v.to_dict(), 'decision': d.__dict__} for v, d in records)
        except RequestCancelled:
            raise
        except (FetchError, ParseError) as exc:
            summary = {'error': str(exc)}
        except Exception as exc:
            summary = {'error': f'Неизвестная структура ответа ({type(exc).__name__})'}
        output['sources'][name] = summary
        log.info('%s: %s', name, summary)
    if store:
        apply_result(store, output)
    return output

def apply_result(store, result):
    for name, state in result['sources'].items():
        if state.get('error'):
            store.source_error(name, state['error'])
        else:
            rows = [(Vacancy(**p['vacancy']), Decision(**p['decision']))
                    for p in result['vacancies'] if p['vacancy']['source'] == name]
            store.save_snapshot(name, rows)

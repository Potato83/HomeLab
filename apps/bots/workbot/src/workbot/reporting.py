from html import escape

def status_text(store, config):
    states = {r['source']: r for r in store.status()}
    lines = ['Источники · проверка раз в ' + str(config['monitor']['interval_seconds'] // 60) + ' мин.']
    for name, source in config['sources'].items():
        if source.get('enabled'):
            state = states.get(name, {})
            detail = state.get('error') or ('найдено ' + str(state.get('found', 0)) + ', по словам ' + str(state.get('matched', 0)) if state.get('last_success') else 'ещё не проверен')
            lines.append(escape(name + ': ' + detail))
    return '\n'.join(lines)

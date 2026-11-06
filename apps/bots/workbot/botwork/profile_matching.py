"""Evaluate each saved filter profile while keeping one vacancy record."""
from .matching import decide
from .models import Decision


def configured_profiles(config):
    profiles = config.get('_search_profiles')
    return profiles or [{'name': 'Текущий поиск', 'filters': config['filters']}]


def combined_collection_filters(config):
    """Use the union of role directions when collecting source candidates."""
    profiles = configured_profiles(config)
    result = dict(config['filters'])
    result['directions'] = list(dict.fromkeys(
        direction
        for profile in profiles
        for direction in profile['filters'].get('directions', ['security'])
    ))
    result['events_enabled'] = any(
        profile['filters'].get('events_enabled', False) for profile in profiles
    )
    event_filters = [profile['filters'] for profile in profiles if profile['filters'].get('events_enabled', False)]
    if event_filters:
        result['event_days'] = max(rules.get('event_days', 90) for rules in event_filters)
        for key in ('event_cancellations', 'event_updates'):
            result[key] = any(rules.get(key, True) for rules in event_filters)
    return result


def decide_profiles(vacancy, config, allow_unverified=False):
    profiles = configured_profiles(config)
    decisions = [
        (profile['name'], decide(vacancy, profile['filters'], allow_unverified))
        for profile in profiles
    ]
    matches = [(name, decision) for name, decision in decisions if decision.matched]
    if matches:
        names = [name for name, _ in matches]
        reasons = list(dict.fromkeys(decision.reason for _, decision in matches))
        warnings = list(dict.fromkeys(warning for _, decision in matches for warning in decision.warnings))
        return Decision(True, ' / '.join(reasons), warnings, names)
    # Keep the active profile's explanation, matching the legacy single-profile behavior.
    return decisions[0][1]

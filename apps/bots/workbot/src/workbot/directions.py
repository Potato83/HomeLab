"""Translate literal keywords to provider search terms."""
import re
from copy import deepcopy
from .models import normalize

def selected(rules):
    return ['security']  # Habr also reads the broad infosec catalogue.

def search_terms(rules):
    return list(rules.get('include_words', []))

def matches(text, rules):
    from .matching import keyword_hit
    return [w for w in search_terms(rules) if keyword_hit(w, normalize(text))]

def collection_rules(rules):
    result = deepcopy(rules)
    result['security_patterns'] = [re.escape(normalize(w)) for w in search_terms(rules)]
    result['senior_title_patterns'] = []
    return result

def source_settings(name, source, rules):
    result = deepcopy(source)
    if name in ('mts', 'sber', 'hh'):
        result['search_terms'] = search_terms(rules)
    return result

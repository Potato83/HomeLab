"""Explicit role families; a company name or body keyword is not a role."""
import re
from copy import deepcopy
from .models import normalize

LABELS = {'security': 'ИБ', 'devsecops': 'DevSecOps',
          'devops': 'DevOps / SRE / Platform', 'mlops': 'MLOps / ML Platform / Data Engineering'}
PATTERNS = {
    'devsecops': [r'\bdevsecops\b', r'безопасност.{0,25}(ci/cd|пайплайн)', r'\bapplication security\b'],
    'devops': [r'\b(devops|sre)\b', r'site reliability', r'platform engineer', r'платформенн.{0,12}инженер', r'инженер.{0,15}(платформ|инфраструктур)', r'администратор.{0,12}(linux|kubernetes)'],
    'mlops': [r'\b(mlops|llmops)\b', r'ml[ -](platform|infra)', r'инфраструктур.{0,15}(ml|машинн)', r'\bdata engineer', r'инженер.{0,10}данных', r'инженер.{0,15}ml', r'machine learning infrastructure'],
}
TERMS = {'devsecops': ['DevSecOps'], 'devops': ['DevOps', 'SRE', 'Platform Engineer', 'инфраструктур'],
         'mlops': ['MLOps', 'ML Platform', 'Data Engineer', 'инженер данных']}


def selected(rules):
    return rules.get('directions', ['security'])


def patterns(rules):
    return [p for key in selected(rules) for p in (rules['security_patterns'] if key == 'security' else PATTERNS[key])]


def matches(text, rules):
    return [LABELS[key] for key in selected(rules)
            if any(re.search(p, normalize(text), re.I) for p in (rules['security_patterns'] if key == 'security' else PATTERNS[key]))]


def collection_rules(rules):
    result = deepcopy(rules)
    result['security_patterns'] = patterns(rules)
    return result


def search_terms(rules):
    return list(dict.fromkeys(term for key in selected(rules) for term in TERMS.get(key, [])))


def source_settings(name, source, rules):
    result = deepcopy(source)
    if name in ('mts', 'sber', 'hh'):
        terms = search_terms(rules)
        if name in ('mts', 'sber'):
            # The public search tokenizes multiword input. Single distinctive
            # terms preserve the adapter's verifiable containsi contract.
            replacements = {'Platform Engineer': 'Platform', 'ML Platform': 'Platform',
                            'Data Engineer': 'Data', 'инженер данных': 'данных'}
            terms = [replacements.get(term, term) for term in terms]
            if name == 'mts':
                # This stem also returns semantic matches absent from the
                # public card. Other explicit role terms retain verifiability.
                terms = [term for term in terms if term != 'инфраструктур']
        result['search_terms'] = list(dict.fromkeys([*source.get('search_terms', []), *terms]))
    return result

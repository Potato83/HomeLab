"""Conservative programme signals from CROC's official internship page."""
from urllib.parse import parse_qs, urlsplit

from .models import Vacancy, normalize
from .sources import ParseError, soup

URL = 'https://internship.croc.ru/'
DEPARTMENTS = {
    'cybersecurity-ru': ('security', 'Кибербезопасность'),
    'system-integration-and-infrastructure-solutions-ru': ('devops', 'Программная и вычислительная инфраструктура'),
}


def parse_programmes(html):
    doc = soup(html)
    text = normalize(doc.get_text(' ', strip=True))
    if not all(marker in text for marker in ('стажировк', 'направления', 'кибербезопасность')):
        raise ParseError('КРОК стажировки: не подтверждена страница программы')
    found = {}
    for link in doc.select('a[href]'):
        parsed = urlsplit(link['href'])
        if parsed.scheme != 'https' or parsed.netloc not in ('www.croc.ru', 'careers.croc.ru') or parsed.path not in ('/career/job-search/', '/vacancies/'):
            continue
        query = parse_qs(parsed.query)
        departments = query.get('_sft_departament', [])
        if query.get('_sft_expirience') != ['no-work-experience-ru'] or len(departments) != 1 or departments[0] not in DEPARTMENTS:
            continue
        key = departments[0]
        direction, label = DEPARTMENTS[key]
        if normalize(label) not in text:
            raise ParseError('КРОК стажировки: ссылка направления не подтверждена содержимым')
        category = 'Информационная безопасность' if direction == 'security' else 'DevOps / инфраструктура'
        found[key] = Vacancy('croc_internships', key, 'Стажировка — ' + label, 'КРОК', URL,
            categories=[category], level='intern', recruitment_status='unknown',
            application_url=link['href'], description=label,
            notes=['Страница программы, а не подтверждённое свободное место. Проверьте текущие вакансии по ссылке направления.'])
    if set(found) != set(DEPARTMENTS):
        raise ParseError('КРОК стажировки: изменились ссылки направлений; неполный снимок не сохраняется')
    return list(found.values())

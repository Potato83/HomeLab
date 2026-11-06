"""Explain recurring title words in the owner's vacancy feedback."""
from collections import Counter
from html import escape
import re

from .models import normalize

STOP_WORDS = {
    'для', 'или', 'при', 'под', 'над', 'без', 'как', 'это', 'что', 'его', 'еще',
    'вакансия', 'стажер', 'стажировка', 'стажировки', 'junior', 'intern', 'internship',
    'trainee', 'the', 'and', 'for', 'with', 'from',
}


def common_words(rows):
    counts = Counter()
    for row in rows:
        words = set(re.findall(r'[a-zа-я0-9]{3,}', normalize(row.get('title', ''))))
        counts.update(word for word in words if word not in STOP_WORDS and not word.isdigit())
    return sorted(((word, count) for word, count in counts.items() if count >= 2), key=lambda item: (-item[1], item[0]))[:10]


def insights_text(store):
    tracked = store.tracked_vacancies()
    ignored = store.tracked_vacancies(ignored=True)
    lines = ['<b>Сводка по вакансиям</b>']
    for title, rows in [('В списке откликов', tracked), ('Отмечены как неподходящие', ignored)]:
        lines.append(f'\n{title}: {len(rows)}')
        words = common_words(rows)
        lines.append('Частые слова в названиях: ' + (', '.join(f'{escape(word)} ({count})' for word, count in words) if words else 'пока недостаточно повторений'))
    lines.append('\nЧисло рядом со словом — количество карточек с ним. Слова могут встречаться в обеих группах; проверьте вакансии перед изменением фильтров через /set.')
    return '\n'.join(lines)


def handle_insights(telegram, store, update):
    if telegram.command(update) != '/insights':
        return False
    telegram.send(insights_text(store))
    return True

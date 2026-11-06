"""Public MTS vacancy catalogue: paginated API plus scoped job descriptions."""

import json
import math
import re
from urllib.parse import urlencode, urlsplit

from .matching import infer_format
from .models import Vacancy, normalize
from .limits import MAX_SOURCE_PAGES
from .employment import employment_types as parse_employment
from .sources import ParseError, experience_level, needs_detail, soup


def titles(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ParseError("МТС: неизвестная структура меток")
    result = []
    for entry in value:
        if not isinstance(entry, dict) or not isinstance(entry.get("title"), str):
            raise ParseError("МТС: некорректная метка")
        result.append(entry["title"])
    return result


def salary_text(row):
    lower, upper = row.get("salaryFrom"), row.get("salaryTo")
    if lower is None and upper is None:
        return "не указана"
    for value in (lower, upper):
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ParseError("МТС: некорректная зарплата")
    unit = (row.get("currency") or {}).get("title") or "валюта не указана"
    unit = "₽" if unit == "RUB" else str(unit)
    amount = lambda value: f"{value:,.0f}".replace(",", " ")
    if lower is not None and upper is not None:
        return f"{amount(lower)} {unit}" if lower == upper else f"от {amount(lower)} до {amount(upper)} {unit}"
    return f"от {amount(lower)} {unit}" if lower is not None else f"до {amount(upper)} {unit}"


def parse_mts_page(text, base_url, requested_page, page_size):
    try:
        payload = json.loads(text)
        rows = payload["data"]
        paging = payload["meta"]["pagination"]
        numbers = [paging[key] for key in ("page", "pageSize", "pageCount", "total")]
    except (ValueError, KeyError, TypeError):
        raise ParseError("МТС: неизвестный ответ API") from None
    if not isinstance(rows, list) or any(type(n) is not int for n in numbers):
        raise ParseError("МТС: некорректная пагинация")
    page, size, page_count, total = numbers
    if page != requested_page or size != page_size or total < 0 or page_count < 0:
        raise ParseError("МТС: API вернул другую страницу или размер выдачи")
    if page_count != math.ceil(total / size) and not (total == 0 and page_count == 1):
        raise ParseError("МТС: противоречивые счётчики выдачи")
    expected = min(size, max(0, total - (page - 1) * size))
    if len(rows) != expected:
        raise ParseError("МТС: неполная страница вакансий")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ParseError("МТС: некорректная вакансия")
        # The API's numeric `id` is already rounded for some large identifiers.
        # The string slug/mtsId preserves the exact public vacancy URL.
        identifier = row.get("slug") or row.get("mtsId")
        if not isinstance(identifier, str) or not re.fullmatch(r"\d{10,25}", identifier):
            raise ParseError("МТС: отсутствует точный строковый идентификатор")
        if not isinstance(row.get("title"), str) or not row["title"].strip() or type(row.get("isActive")) is not bool:
            raise ParseError("МТС: отсутствует название или статус вакансии")
        for field in ("region", "experience", "organization", "currency"):
            if row.get(field) is not None and not isinstance(row[field], dict):
                raise ParseError("МТС: неизвестная структура условий вакансии")
        category_labels = titles(row.get("categories"))
        format_labels = titles(row.get("workFormats"))
        type_labels = titles(row.get("workTypes"))
        formats = [infer_format(label) for label in format_labels]
        work_format = next((f for f in ("remote", "hybrid", "office") if f in formats), "unknown")
        experience = (row.get("experience") or {}).get("title") or ""
        city = (row.get("region") or {}).get("title") or ""
        company = (row.get("organization") or {}).get("title") or "МТС"
        if any(not isinstance(value, str) for value in (experience, city, company)):
            raise ParseError("МТС: некорректные текстовые условия")
        level = experience_level(experience)
        if level != "experienced" and any("стажиров" in label.lower() for label in type_labels):
            level = "intern"
        description = row.get("description")
        if description is not None and not isinstance(description, str):
            raise ParseError("МТС: неизвестный формат описания")
        employment, employment_known = parse_employment(type_labels)
        currency = str((row.get('currency') or {}).get('title') or '')
        result.append(Vacancy("mts", identifier, row["title"], company,
            base_url.rstrip("/") + "/vacancy/" + identifier,
            description=soup(description).get_text("\n", strip=True) if description else "",
            categories=category_labels, cities=[city] if city else [],
            work_format=work_format, work_format_label="; ".join(format_labels),
            level=level, active=row["isActive"], experience=experience, salary=salary_text(row),
            salary_min=row.get('salaryFrom'), salary_max=row.get('salaryTo'), salary_currency=currency,
            salary_period='monthly' if row.get('salaryFrom') is not None or row.get('salaryTo') is not None else 'unknown',
            employment_types=employment, employment_known=employment_known))
    if len({v.external_id for v in result}) != len(result):
        raise ParseError("МТС: повторные идентификаторы внутри страницы")
    return result, {"page_count": page_count, "page_size": size, "total": total}


def parse_mts_detail(html, vacancy):
    doc = soup(html)
    title = doc.select_one('.vacancy__header__title')
    body = doc.select_one('.vacancy__content .vacancy__data')
    if not title or not body:
        raise ParseError("МТС: отсутствует собственное описание вакансии")
    vacancy.title = title.get_text(" ", strip=True)
    vacancy.description = body.get_text("\n", strip=True)
    if vacancy.work_format == "unknown":
        vacancy.work_format = infer_format(vacancy.description)
    return vacancy


def collect_mts(settings, client, rules):
    url = settings["api_url"]
    base_url = settings["url"]
    if urlsplit(url).netloc != urlsplit(base_url).netloc:
        raise ParseError("МТС: API и карьерный сайт должны иметь один хост")
    page_size = settings.get("page_size", 100)
    max_pages = settings.get("max_pages", 50)
    if type(page_size) is not int or not 1 <= page_size <= 100 or type(max_pages) is not int or not 1 <= max_pages <= MAX_SOURCE_PAGES:
        raise ValueError(f"МТС: page_size должен быть от 1 до 100, max_pages — от 1 до {MAX_SOURCE_PAGES}")
    terms = settings.get("search_terms")
    if not isinstance(terms, list) or not terms or len(terms) > 32 or any(not isinstance(t, str) or not 2 <= len(t.strip()) <= 60 for t in terms):
        raise ValueError("МТС: задайте от 1 до 32 непустых поисковых слов длиной 2–60 символов")
    unique = {}
    detail_pages = {}
    def detail(v):
        if v.external_id not in detail_pages:
            detail_pages[v.external_id] = client.get(v.url)
        return parse_mts_detail(detail_pages[v.external_id], v)
    # This public API treats a containsi query as a search over titles and
    # categories. Nested OR/category queries may be ignored. Search separately
    # and verify that results actually match one of those metadata fields.
    for term in dict.fromkeys(terms):
        def fetch_page(page):
            query = urlencode({"pagination[page]": page, "pagination[pageSize]": page_size,
                               "filters[title][$containsi]": term})
            batch, paging = parse_mts_page(client.get(url + "?" + query), base_url, page, page_size)
            for v in batch:
                if normalize(term) not in normalize(' '.join([v.title, *v.categories, v.description])):
                    if not v.description:
                        detail(v)
                    if normalize(term) not in normalize(' '.join([v.title, *v.categories, v.description])):
                        raise ParseError("МТС: API проигнорировал поисковый фильтр")
            return batch, paging
        rows, paging = fetch_page(1)
        if paging["page_count"] > max_pages:
            raise ParseError("МТС: превышен max_pages; поисковая выдача неполна")
        seen = {v.external_id for v in rows}
        for page in range(2, paging["page_count"] + 1):
            batch, current = fetch_page(page)
            if current != paging:
                raise ParseError("МТС: выдача изменилась во время загрузки; повторим позже")
            if any(v.external_id in seen for v in batch):
                raise ParseError("МТС: API повторил карточки между страницами")
            rows.extend(batch)
            seen.update(v.external_id for v in batch)
        if len(rows) != paging["total"]:
            raise ParseError("МТС: получена неполная поисковая выдача")
        unique.update((v.external_id, v) for v in rows)
    # Only fetch individual descriptions for plausible security candidates.
    return [detail(v) if v.active and needs_detail(v, rules) else v for v in unique.values()]

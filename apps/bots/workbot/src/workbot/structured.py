"""Read embedded JobPosting metadata without following external contexts or URLs."""
from datetime import date
import json
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from .matching import infer_format


def job_nodes(html):
    doc = BeautifulSoup(html, 'html.parser')
    found, visited = [], 0
    for script in doc.select('script[type="application/ld+json"]')[:100]:
        try:
            stack = [(json.loads(script.get_text()), 0)]
        except (ValueError, RecursionError):
            continue
        while stack:
            node, depth = stack.pop()
            visited += 1
            if visited > 10000:
                return []
            if depth > 20:
                continue
            if isinstance(node, list):
                stack.extend((v, depth + 1) for v in node)
            elif isinstance(node, dict):
                types = node.get('@type', [])
                if isinstance(types, str):
                    types = [types]
                if isinstance(types, list) and 'JobPosting' in types:
                    found.append(node)
                for key in ('@graph', 'itemListElement', 'item'):
                    if key in node:
                        stack.append((node[key], depth + 1))
    return found


def enrich_job(html, vacancy):
    """Use only a unique complete job belonging to this exact detail page."""
    candidates = []
    for node in job_nodes(html):
        title, body = node.get('title'), node.get('description')
        if not isinstance(title, str) or not title.strip() or not isinstance(body, str) or not body.strip():
            continue
        url = node.get('url')
        if url is not None:
            if not isinstance(url, str):
                continue
            parts = urlsplit(urljoin(vacancy.url, url))
            expected = urlsplit(vacancy.url)
            if (parts.scheme, parts.netloc, parts.path.rstrip('/')) != (expected.scheme, expected.netloc, expected.path.rstrip('/')):
                continue
        candidates.append((node, title, body))
    if len(candidates) != 1:
        return False  # Fall back to the provider's scoped HTML parser.
    node, title, body = candidates[0]
    vacancy.title = title.strip()
    vacancy.description = BeautifulSoup(body, 'html.parser').get_text('\n', strip=True)
    vacancy.work_format = infer_format(vacancy.description)
    if str(node.get('jobLocationType', '')).upper() == 'TELECOMMUTE':
        vacancy.work_format = 'remote'
    expires = node.get('validThrough')
    if isinstance(expires, str):
        try:
            if date.fromisoformat(expires[:10]) < date.today():
                vacancy.active = False
        except ValueError:
            pass
    return True

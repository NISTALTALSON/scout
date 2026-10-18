"""Public GitHub repository search with a small, explicit provider adapter seam."""

import json
from typing import Protocol
from urllib.parse import urlencode

from scout.urls import normalize_url


class SearchProvider(Protocol):
    def search(self, query: str, http, budget, now) -> list[str]: ...


class GitHubRepositorySearch:
    def search(self, query: str, http, budget, now) -> list[str]:
        url = "https://api.github.com/search/repositories?" + urlencode({
            "q": query, "sort": "updated", "order": "desc", "per_page": 10})
        result = http.get(url, headers={"Accept": "application/vnd.github+json"})
        if result.status_code != 200:
            from scout.http import FetchError

            raise FetchError("GitHub repository search unavailable", result.status_code, 3600)
        payload = json.loads(result.content)
        return [item["html_url"] for item in payload.get("items", [])[:10] if
                isinstance(item, dict) and isinstance(item.get("html_url"), str)]


def run_queries(http, budget, now, queries=None, provider: SearchProvider | None = None) -> list[str]:
    terms = queries if queries is not None else ("student developer hackathon", "open source student program")
    output = []
    for query in list(terms)[:2]:
        if not isinstance(query, str) or not query.strip():
            continue
        budget.check()
        for url in (provider or GitHubRepositorySearch()).search(query[:160], http, budget, now)[:10]:
            try:
                normalized = normalize_url(url)
            except ValueError:
                continue
            if normalized not in output:
                output.append(normalized)
    return output[:20]

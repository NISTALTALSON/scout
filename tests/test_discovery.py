import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from scout.budgets import Budget
from scout.discovery.planner import plan_sources
from scout.discovery.query_engine import run_queries
from scout.discovery.source_discovery import discover_sources
from scout.http import FetchError, SafeHttpClient
from scout.models import FetchResult, Opportunity, Source

NOW = datetime(2026, 10, 19, tzinfo=UTC)
FEED = b'<rss><channel><item><title>Student challenge</title><link>https://publisher.org/event</link></item></channel></rss>'


def publisher(**kwargs):
    return Source(id="publisher", name="Publisher", url="https://publisher.org/news",
                  type="rss", categories=["hackathons"], trust="official",
                  official_domains=["publisher.org"], enabled=True,
                  validation_status="validated", **kwargs)


class StubHTTP:
    def __init__(self, responses):
        self.responses = responses
        self.seen = []

    def get(self, url, **kwargs):
        self.seen.append(url)
        return self.responses[url]


def test_unknown_domain_quarantined_no_official_trust():
    url = "https://other.org/events/feed.xml"
    stub = StubHTTP({url: FetchResult(url=url, status_code=200,
                                     headers={"content-type": "application/rss+xml"}, content=FEED)})
    budget = Budget()
    found = discover_sources([url], publisher(), [publisher()], stub, budget, NOW)
    assert len(found) == 1 and found[0].validation_status == "quarantined"
    assert not found[0].enabled and found[0].trust == "unknown"
    assert budget.new_sources == 1


def test_known_publisher_useful_probation_autoenables_and_id_stable():
    url = "https://publisher.org/feed.xml"
    stub = StubHTTP({url: FetchResult(url=url, status_code=200,
                                     headers={"content-type": "application/rss+xml"}, content=FEED)})
    found = discover_sources([url], publisher(), [publisher()], stub, Budget(), NOW)
    assert len(found) == 1 and found[0].enabled and found[0].auto_enabled
    assert found[0].trust == "official" and found[0].probation_successes == 1
    assert found[0].id == discover_sources([url], publisher(), [publisher()], stub, Budget(), NOW)[0].id


def test_onboarding_at_most_three_and_budget_accounted():
    urls = [f"https://publisher.org/events/{i}" for i in range(7)]
    stub = StubHTTP({url: FetchResult(url=url, status_code=200,
                    headers={"content-type": "application/rss+xml"}, content=FEED) for url in urls})
    budget = Budget(max_new_sources=5)
    found = discover_sources(urls, publisher(), [publisher()], stub, budget, NOW)
    assert len(found) == budget.new_sources == 3
    assert len(stub.seen) == 3


def test_alternate_feed_is_fetched_before_onboarding_listing():
    listing, feed = "https://publisher.org/news", "https://publisher.org/atom.xml"
    html = b'<link rel="alternate" type="application/atom+xml" href="/atom.xml">'
    stub = StubHTTP({listing: FetchResult(url=listing, status_code=200, content=html),
                     feed: FetchResult(url=feed, status_code=200,
                                       headers={"content-type": "application/rss+xml"}, content=FEED)})
    base = publisher()
    base.url = "https://publisher.org/original"
    found = discover_sources([listing], base, [base], stub, Budget(), NOW)
    assert stub.seen == [listing, feed] and [s.url for s in found] == [feed]


def test_scheduler_due_backoff_fairness_and_urgent_reliability():
    alpha = publisher(last_attempt_at=NOW - timedelta(hours=7))
    beta = Source(id="beta", name="Beta", url="https://beta.org/events", type="html",
                  categories=["credits"], enabled=True, validation_status="validated",
                  last_attempt_at=NOW - timedelta(hours=7))
    failed = Source(id="failed", name="Failed", url="https://fail.org/events", type="html",
                    categories=["hackathons"], enabled=True, validation_status="validated",
                    failure_count=2, last_attempt_at=NOW - timedelta(hours=7))
    probation = Source(id="probation", name="Probation", url="https://new.org/feed",
                       type="rss", categories=["credits"])
    assert [s.id for s in plan_sources([alpha, beta, failed, probation], NOW)] == [
        "probation", "publisher", "beta"]
    opportunity = Opportunity(id="event-1", title="Event", canonical_url="https://publisher.org/e",
                              source_url=alpha.url, source_name=alpha.name, source_type="rss",
                              source_id=alpha.id, category="hackathons", deadline_at=NOW + timedelta(days=2),
                              deadline_verified=True, verification_status="official_verified")
    assert [s.id for s in plan_sources([alpha, beta], NOW, urgent_only=True,
                                       opportunities=[opportunity])] == [alpha.id]
    opportunity.deadline_verified = False
    assert plan_sources([alpha, beta], NOW, urgent_only=True, opportunities=[opportunity]) == []


def test_github_search_bounded_and_403_visible():
    seen = []

    def respond(request):
        seen.append(str(request.url))
        assert request.url.host == "api.github.com"
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert request.url.path == "/search/repositories"
        return httpx.Response(200, json={"items": [{"html_url": "https://github.com/org/project"},
                                           {"html_url": "http://127.0.0.1/internal"}]})

    budget = Budget(max_requests=3)
    client = SafeHttpClient(budget, transport=httpx.MockTransport(respond))
    try:
        assert run_queries(client, budget, NOW) == ["https://github.com/org/project"]
    finally:
        client.close()
    assert len(seen) == 3 and budget.requests == 3

    client = SafeHttpClient(Budget(), transport=httpx.MockTransport(
        lambda request: httpx.Response(404) if request.url.path == "/robots.txt" else
        httpx.Response(403, headers={"retry-after": "600"})))
    try:
        with pytest.raises(FetchError) as error:
            run_queries(client, client.budget, NOW, queries=["student hackathon"])
        assert error.value.status_code == 403 and error.value.backoff_seconds == 600
    finally:
        client.close()


def test_query_adapter_and_generic_api_parse():
    class Provider:
        def search(self, query, http, budget, now):
            return ["https://events.org/new", "http://localhost/private"]

    assert run_queries(None, Budget(), NOW, queries=["one"], provider=Provider()) == [
        "https://events.org/new"]
    from scout.collectors import parse

    src = Source(id="api-src", name="API", url="https://api.events.org/v1", type="api",
                 categories=["events"], options={"items_path": "data.items", "title_field": "name",
                                                "url_field": "link", "date_field": "when"})
    result = parse(src, FetchResult(url=src.url, status_code=200,
                                    content=json.dumps({"data": {"items": [{"name": "Event",
                                    "link": "https://events.org/new", "when": "2026-12-04"}]}}).encode()), NOW)
    assert result.candidates[0].metadata["date_excerpt"] == "2026-12-04"
    assert result.candidates[0].published_at is None

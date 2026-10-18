from datetime import UTC, datetime, timedelta

import httpx
import pytest

from scout.budgets import Budget
from scout.collectors import parse
from scout.http import FetchError, SafeHttpClient
from scout.intelligence.deduplication import find_duplicate
from scout.intelligence.extraction import _date_value, extract
from scout.intelligence.verification import verify
from scout.models import Candidate, FetchResult, Source

NOW = datetime(2026, 10, 19, tzinfo=UTC)
TAX = {"hackathons": ["hackathon"], "student_benefits": ["student"]}


def publisher(**changes):
    return Source(**(dict(id="official", name="Official", url="https://example.org/rss",
                         type="rss", trust="official", official_domains=["example.org"],
                         categories=["hackathons"], enabled=True, validation_status="validated") | changes))


def test_natural_numeric_zone_and_dst_unknown():
    assert _date_value("20 October 2026 at 17:00 +05:30") == NOW + timedelta(days=1, hours=11, minutes=30)
    assert _date_value("1 November 2026 at 01:30 America/New_York") is None


def test_unverified_free_is_not_presented_as_verified():
    source = publisher(trust="community", official_domains=[])
    item = extract(Candidate(source_id=source.id, title="Hackathon training",
                             url="https://example.org/a", text="Register for free training."),
                   source, NOW, TAX)
    assert verify(item, source, NOW).price_status == "unknown"


def test_original_page_enriches_and_updates_deadline():
    source = publisher()
    item = extract(Candidate(source_id=source.id, title="Maker Hackathon 2026",
                             url="https://example.org/a", text="Register for a hackathon."),
                   source, NOW, TAX)

    class Http:
        def get(self, url):
            return FetchResult(url=url, status_code=200, content=b"<main><h1>Maker Hackathon 2026</h1>"
                               b"<p>Registration closes on 20 October 2026 at 17:00 UTC. "
                               b"Registration is free. Available worldwide. Register now.</p></main>")

    checked = verify(item, source, NOW, Http())
    assert checked.deadline_verified and checked.price_status == "verified_free"
    assert checked.deadline_at == NOW + timedelta(days=1, hours=17)


def test_edition_identity_and_student_benefits_do_not_collapse():
    source = publisher(adapter="student_pack", type="html", categories=["student_benefits"])
    html = b"<h3>Tool One</h3><h4>About</h4><p>Developer tools</p><h4>Offers</h4><p>Free access for students</p>" \
           b"<h3>Tool Two</h3><h4>About</h4><p>Developer tools</p><h4>Offers</h4><p>Free access for students</p>"
    parsed = parse(source, FetchResult(url=source.url, status_code=200, content=html), NOW)
    items = [extract(c, source, NOW, TAX) for c in parsed.candidates]
    assert len(items) == 2 and items[0].id != items[1].id
    assert find_duplicate(items[1], [items[0]]) is None
    years = [extract(Candidate(source_id=source.id, title=f"Hackathon {year}", url="https://example.org/event",
                               text="Register for the hackathon"), publisher(), NOW, TAX)
             for year in [2026, 2027]]
    assert years[0].id != years[1].id


def test_media_type_and_user_agent_and_public_run_cache():
    seen = []

    def respond(request):
        seen.append(request.url.path)
        assert request.headers["user-agent"].startswith("ScoutOpportunityAgent/")
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text="<h1>OK</h1>")

    client = SafeHttpClient(Budget(), transport=httpx.MockTransport(respond))
    client.get("https://example.org/a", rate_limit_seconds=0)
    client.get("https://example.org/a", rate_limit_seconds=0)
    assert seen == ["/robots.txt", "/a"]
    client.close()
    bad = SafeHttpClient(Budget(), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"Content-Type": "application/octet-stream"}, content=b"EXE")))
    with pytest.raises(FetchError, match="content type"):
        bad.get("https://example.org/a", respect_robots=False)
    bad.close()

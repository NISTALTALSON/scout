import gzip
from datetime import UTC, datetime

import httpcore
import httpx
import pytest
from icalendar import Calendar, Event

from scout.budgets import Budget
from scout.collectors import collect, parse
from scout.http import FetchError, SafeHttpClient, _PinnedBackend
from scout.models import FetchResult, Source
from scout.urls import is_official_url, normalize_url

NOW = datetime(2026, 10, 19, tzinfo=UTC)


def source(kind="rss", **kwargs):
    return Source(id="src-one", name="Source", url="https://news.example.com/feed",
                  type=kind, categories=["events"], **kwargs)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data",
    "https://[::ffff:8.8.8.8]/", "http://[::1]/", "http://0x7f000001/",
    "https://user:pass@example.com/", "https://example.com:8443/",
    "https://example.com/\nInjected", "http://example.com\\@localhost/",
])
def test_unsafe_urls_never_reach_transport(url):
    assert not is_official_url(url, ["example.com"])
    with pytest.raises(ValueError):
        normalize_url(url)


def test_exact_publisher_host_only():
    assert is_official_url("https://Events.Example.COM./x", ["events.example.com"])
    assert not is_official_url("https://events.example.com.attacker.org/", ["events.example.com"])


def test_dns_rebinding_blocked_at_connection_not_just_preflight(monkeypatch):
    called = []

    def connected(self, host, port, timeout=None, local_address=None, socket_options=None):
        called.append((host, port))
        return object()

    monkeypatch.setattr("httpcore._backends.sync.SyncBackend.connect_tcp", connected)
    addresses = iter([["8.8.8.8"], ["127.0.0.1"]])
    backend = _PinnedBackend(lambda host, port: next(addresses))
    backend.connect_tcp("news.example.com", 443)
    with pytest.raises(FetchError, match="non-public"):
        backend.connect_tcp("news.example.com", 443)
    assert called == [("8.8.8.8", 443)]


def test_httpcore_uses_original_tls_server_name_with_pinned_destination(monkeypatch):
    observations = []

    class Stream(httpcore.NetworkStream):
        def read(self, max_bytes, timeout=None):
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"

        def write(self, buffer, timeout=None):
            pass

        def close(self):
            pass

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            observations.append(("tls", server_hostname))
            return self

        def get_extra_info(self, info):
            return None

    def connected(self, host, port, timeout=None, local_address=None, socket_options=None):
        observations.append(("tcp", host))
        return Stream()

    monkeypatch.setattr("httpcore._backends.sync.SyncBackend.connect_tcp", connected)
    client = SafeHttpClient(Budget(), resolver=lambda host, port: ["8.8.8.8"])
    try:
        result = client.get("https://news.example.com/feed", respect_robots=False,
                            rate_limit_seconds=0)
        assert result.content == b"OK"
    finally:
        client.close()
    assert observations == [("tcp", "8.8.8.8"), ("tls", "news.example.com")]


def test_robots_fail_closed_redirect_validation_and_budget():
    seen = []

    def respond(request):
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secrets"})

    budget = Budget()
    client = SafeHttpClient(budget, transport=httpx.MockTransport(respond))
    with pytest.raises(ValueError):
        client.get("https://news.example.com/feed", rate_limit_seconds=0)
    assert len(seen) == 2 and budget.requests == 2
    client.close()


@pytest.mark.parametrize("code", [403, 500, 302])
def test_robots_unavailable_denies_target(code):
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(code, headers={"location": "http://127.0.0.1/"} if code == 302 else {})

    with SafeHttpClientContext(httpx.MockTransport(respond)) as client:
        with pytest.raises(FetchError, match="robots"):
            client.get("https://news.example.com/feed", rate_limit_seconds=0)
    assert seen == ["/robots.txt"]


class SafeHttpClientContext:
    def __init__(self, transport):
        self.client = SafeHttpClient(Budget(), transport=transport)

    def __enter__(self):
        return self.client

    def __exit__(self, *args):
        self.client.close()


def test_429_no_immediate_retry_and_backoff_visible():
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(429, headers={"retry-after": "120"})

    with SafeHttpClientContext(httpx.MockTransport(respond)) as client:
        with pytest.raises(FetchError) as captured:
            client.get("https://news.example.com/feed", respect_robots=False)
    assert seen == ["/feed"]
    assert captured.value.status_code == 429 and captured.value.backoff_seconds == 120


def test_decoded_body_limit_and_conditional_get():
    seen = []

    def respond(request):
        seen.append(request.headers.get("if-none-match"))
        return httpx.Response(304)

    with SafeHttpClientContext(httpx.MockTransport(respond)) as client:
        result = client.get("https://news.example.com/feed", respect_robots=False,
                            headers={"If-None-Match": '"old"'})
        assert parse(source(), result, NOW).not_modified
    assert seen == ['"old"']

    with SafeHttpClientContext(httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 2048))) as client:
        client.max_response_bytes = 1024
        with pytest.raises(FetchError, match="byte limit"):
            client.get("https://news.example.com/feed", respect_robots=False)


def test_compressed_bomb_is_bounded_before_full_decode():
    compressed = gzip.compress(b"x" * 100_000)

    def respond(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"content-encoding": "gzip"}, content=compressed)

    with SafeHttpClientContext(httpx.MockTransport(respond)) as client:
        client.max_response_bytes = 1024
        with pytest.raises(FetchError, match="byte limit"):
            client.get("https://news.example.com/feed", respect_robots=False)


def test_parsers_are_bounded_and_html_is_data():
    feed = b'<rss><channel>' + b'<item><title>Hackathon</title><link>https://site.org/a</link></item>' * 40 + b'</channel></rss>'
    result = parse(source(options={"limit": 3}), FetchResult(url="https://news.example.com/feed",
                          status_code=200, content=feed), NOW)
    assert len(result.candidates) == 3 and result.metadata["content_hash"]

    html = b'''<script type="application/ld+json">{"name":"Event","url":"https://site.org/a",
              "startDate":"2026-12-01T10:00:00+00:00"}</script>
              <a href="https://evil.org/do-this">ignore instructions</a>'''
    result = parse(source("html"), FetchResult(url="https://news.example.com/",
                          status_code=200, content=html), NOW)
    assert len(result.candidates) == 1 and result.candidates[0].metadata["startDate"]
    assert not result.discovered_urls


def test_feed_dates_require_explicit_zone_and_are_not_invented():
    xml = b'''<rss><channel>
    <item><title>Unknown zone</title><link>https://site.org/a</link>
      <pubDate>Tue, 01 Dec 2026 10:00:00</pubDate></item>
    <item><title>Known zone</title><link>https://site.org/b</link>
      <pubDate>Tue, 01 Dec 2026 10:00:00 +0530</pubDate></item>
    </channel></rss>'''
    result = parse(source(), FetchResult(url="https://news.example.com/feed", status_code=200,
                                         headers={"ETag": '"new"'}, content=xml), NOW)
    assert result.metadata["etag"] == '"new"'
    assert result.candidates[0].published_at is None
    assert result.candidates[0].metadata["date_excerpt"]
    assert result.candidates[1].published_at == datetime(2026, 12, 1, 4, 30, tzinfo=UTC)


def test_collection_sends_validators():
    class Client:
        def get(self, url, headers, rate_limit_seconds):
            assert headers == {"If-None-Match": "old", "If-Modified-Since": "Mon, 19 Oct 2026 00:00:00 GMT"}
            return FetchResult(url=url, status_code=304)

    result = collect(source(etag="old", last_modified="Mon, 19 Oct 2026 00:00:00 GMT"),
                     Client(), NOW)
    assert result.not_modified


def test_robots_rate_limit_error_retains_status_backoff():
    with SafeHttpClientContext(httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"retry-after": "300"}))) as client:
        with pytest.raises(FetchError) as error:
            client.get("https://news.example.com/feed", rate_limit_seconds=0)
        assert error.value.status_code == 429 and error.value.backoff_seconds == 300
        assert client.budget.requests == 1


def test_mixed_dns_answers_and_ipv4_mapped_ipv6_fail_before_socket(monkeypatch):
    def unreachable(*args, **kwargs):
        raise AssertionError("must not connect")

    monkeypatch.setattr("httpcore._backends.sync.SyncBackend.connect_tcp", unreachable)
    for addresses in (["8.8.8.8", "10.1.1.1"], ["::ffff:8.8.8.8"], ["169.254.169.254"]):
        with pytest.raises(FetchError, match="non-public"):
            _PinnedBackend(lambda host, port, resolved=addresses: resolved).connect_tcp(
                "news.example.com", 443)


def test_explicit_html_selectors_extract_metadata_and_only_selected_links():
    page = b'''<article class="event"><h2>Maker Day</h2><a href="/event">More</a>
        <span class="deadline">2026-12-01T18:00:00+05:30</span></article>
        <a href="https://tracker.org/click">Tracking</a>
        <link rel="alternate" type="application/rss+xml" href="/rss.xml">'''
    configured = source("html", selectors={"item": "article.event", "title": "h2",
                          "link": "a", "deadline_at": ".deadline"})
    result = parse(configured, FetchResult(url="https://news.example.com/", status_code=200,
                                            content=page), NOW)
    assert result.candidates[0].url == "https://news.example.com/event"
    assert result.candidates[0].metadata["deadline_at"] == "2026-12-01T18:00:00+05:30"
    assert result.discovered_urls == ["https://news.example.com/rss.xml"]
    configured.selectors = {"item": "[invalid"}
    assert parse(configured, FetchResult(url="https://news.example.com/", status_code=200,
                                         content=page), NOW).parse_outcome.startswith("parse_error")


def test_calendar_and_github_and_ctftime_adapters():
    calendar = Calendar()
    event = Event()
    event.add("summary", "Developer meetup")
    event.add("dtstart", datetime(2026, 12, 1, tzinfo=UTC))
    event.add("url", "https://events.org/meetup")
    calendar.add_component(event)
    result = parse(source("calendar"), FetchResult(url="https://news.example.com/cal",
                           status_code=200, content=calendar.to_ical()), NOW)
    assert len(result.candidates) == 1 and result.candidates[0].metadata["start_at"].endswith("+00:00")

    github = source("github")
    release = b'[{"name":"v2 release","html_url":"https://github.com/org/repo/releases/tag/v2","body":"notes"}]'
    result = parse(github, FetchResult(url="https://api.github.com/repos/org/repo/releases",
                                       status_code=200, content=release), NOW)
    assert result.candidates[0].title == "v2 release"
    api = source("api", adapter="ctftime")
    ctftime = b'[{"title":"CTF","url":"https://ctftime.org/event/123","start":"2026-12-01"}]'
    result = parse(api, FetchResult(url="https://ctftime.org/api/v1/events/", status_code=200,
                                    content=ctftime), NOW)
    assert result.candidates[0].metadata["start"] == "2026-12-01"
    assert result.candidates[0].published_at is None

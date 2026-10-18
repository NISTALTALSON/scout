"""Deterministic functional edges for deadlines, delivery and source isolation."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scout.budgets import Budget
from scout.config import Limits, Preferences, Scoring
from scout.http import FetchError, FetchResult
from scout.intelligence.deadlines import alert_window
from scout.models import Opportunity, RunMetrics, Source
from scout.notifications.email_sender import DeliveryError
from scout.notifications.outbox import Outbox, make_notification
from scout.orchestrator import Scout
from scout.reporting.health import health_report
from scout.storage.sqlite_store import Store

NOW = datetime(2026, 10, 19, 11, 30, tzinfo=UTC)
TAXONOMY = {
    "hackathons": ["hackathon"],
    "cybersecurity": ["ctf", "security"],
    "learning": ["course", "learning"],
}
GOOD_FEED = "https://good.example/feed"
GOOD_ITEM = "https://good.example/event"
GOOD_FEED_BYTES = (
    b"<rss><channel><item><title>Good Hackathon 2026</title>"
    b"<link>https://good.example/event</link>"
    b"<description>Register now for a free hackathon. Available worldwide. "
    b"Registration closes on 20 October 2026 at 11:30 UTC.</description>"
    b"</item></channel></rss>"
)
GOOD_PAGE_BYTES = (
    b"<main><h1>Good Hackathon 2026</h1><p>Register now for a free hackathon. "
    b"Available worldwide. Registration closes on 20 October 2026 at 11:30 UTC.</p>"
    b"<a href='/register'>Register</a></main>"
)


def source(source_id, url=GOOD_FEED, source_type="rss", **changes):
    values = dict(
        id=source_id,
        name=source_id.title(),
        url=url,
        type=source_type,
        categories=["hackathons"],
        trust="official",
        official_domains=[url.split("/")[2]],
        enabled=True,
        validation_status="validated",
        rate_limit_seconds=0,
        scan_interval_hours=1,
        min_interval_hours=0.5,
        max_interval_hours=24,
    )
    values.update(changes)
    return Source(**values)


def configuration(tmp_path, sources, limits=None):
    limits = limits or Limits(max_requests=50, max_candidates=20, max_new_sources=0)
    return SimpleNamespace(
        directory=tmp_path,
        sources=sources,
        preferences=Preferences(
            categories={"hackathons": 1, "cybersecurity": 1, "learning": 1},
            keywords=["hackathon", "free", "course"],
            min_digest_score=0,
            min_urgent_score=0,
            min_urgent_confidence=0,
            auto_enable_trusted_sources=False,
            limits=limits,
        ),
        scoring=Scoring(),
        taxonomy=TAXONOMY,
    )


class IsolatedHTTP:
    def __init__(self, budget):
        self.budget = budget
        self.calls = []

    def get(self, url, **_kwargs):
        self.budget.consume_http()
        self.calls.append(url)
        if url == GOOD_FEED:
            return FetchResult(url=url, status_code=200,
                               headers={"content-type": "application/rss+xml"},
                               content=GOOD_FEED_BYTES, fetched_at=NOW)
        if url == GOOD_ITEM:
            return FetchResult(url=url, status_code=200,
                               headers={"content-type": "text/html"},
                               content=GOOD_PAGE_BYTES, fetched_at=NOW)
        if url == "https://broken.example/feed":
            raise OSError("offline socket failure")
        if url == "https://limited.example/feed":
            raise FetchError("offline rate limit", status_code=429, backoff_seconds=300)
        if url == "https://malformed.example/api":
            return FetchResult(url=url, status_code=200,
                               headers={"content-type": "application/json"},
                               content=b"{not valid json", fetched_at=NOW)
        raise AssertionError(f"unexpected offline URL: {url}")


def test_scan_isolates_broken_rate_limited_and_malformed_sources(tmp_path):
    sources = [
        source("good-source"),
        source("broken-source", url="https://broken.example/feed"),
        source("limited-source", url="https://limited.example/feed"),
        source("malformed-source", url="https://malformed.example/api", source_type="api"),
    ]
    budget = Budget(max_requests=20, max_candidates=20, max_new_sources=0)
    http = IsolatedHTTP(budget)
    with Store(tmp_path / "isolation.db", create=True) as store:
        scout = Scout(store, configuration(tmp_path, sources), http=http, budget=budget, now=NOW)
        try:
            metrics = scout.scan(discover=False)
        finally:
            scout.close()

        assert metrics.sources_planned == 4
        assert (metrics.sources_ok, metrics.sources_failed, metrics.new_opportunities) == (1, 3, 1)
        assert store.list_opportunities()[0].title == "Good Hackathon 2026"
        records = {item.id: item for item in store.list_sources()}
        assert records["broken-source"].last_error == "OSError"
        assert records["limited-source"].last_error == "offline rate limit"
        assert records["limited-source"].last_response_status is None
        assert records["limited-source"].suspended_until > NOW
        assert records["malformed-source"].last_error == "collector parse_error:JSONDecodeError"
        assert records["malformed-source"].last_response_status == 200
        assert len(http.calls) == 5


def test_scan_persists_good_work_before_http_budget_stops_next_source(tmp_path):
    first = source("first-source")
    second = source("second-source", url="https://second.example/feed")

    class BudgetedHTTP(IsolatedHTTP):
        def get(self, url, **kwargs):
            if url == second.url:
                self.budget.consume_http()
                self.calls.append(url)
                return FetchResult(url=url, status_code=200,
                                   headers={"content-type": "application/rss+xml"},
                                   content=GOOD_FEED_BYTES, fetched_at=NOW)
            return super().get(url, **kwargs)

    budget = Budget(max_requests=2, max_candidates=20, max_new_sources=0)
    http = BudgetedHTTP(budget)
    with Store(tmp_path / "budget.db", create=True) as store:
        scout = Scout(store, configuration(tmp_path, [first, second]), http=http,
                      budget=budget, now=NOW)
        try:
            metrics = scout.scan(discover=False)
        finally:
            scout.close()

        assert metrics.stop_reason == "http_budget"
        assert metrics.warnings == ["Partial coverage saved; unprocessed sources remain due."]
        assert (metrics.sources_ok, metrics.sources_failed, metrics.candidates) == (1, 0, 1)
        assert metrics.http_requests == 2
        assert len(store.list_opportunities()) == 1


def test_health_distinguishes_partial_scan_from_complete_coverage(tmp_path):
    with Store(tmp_path / "health.db", create=True) as store:
        store.record_run(RunMetrics(
            id="partial", started_at=NOW - timedelta(hours=1), finished_at=NOW,
            mode="scan", sources_planned=4, sources_ok=2, stop_reason="http_budget"))
        partial = health_report(store, NOW)
        assert partial["last_successful_scan"] is None
        assert partial["coverage"] == {
            "status": "partial", "planned": 4, "completed": 2,
            "ok": 2, "failed": 0, "ratio": 0.5,
        }
        assert partial["partial_scan_warning"] is True
        assert partial["missed_run_warning"] is True

        store.record_run(RunMetrics(
            id="complete", started_at=NOW, finished_at=NOW,
            mode="scan", sources_planned=4, sources_ok=3, sources_failed=1))
        complete = health_report(store, NOW)
        assert complete["last_successful_scan"] == NOW.isoformat()
        assert complete["coverage"]["status"] == "complete"
        assert complete["partial_scan_warning"] is False
        assert complete["missed_run_warning"] is False


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [(24, 24), (6, 6), (1, 1), (24.001, None), (6.001, 24), (1.001, 6)],
)
def test_deadline_alert_window_boundaries(remaining, expected):
    item = Opportunity(
        id="deadline-item",
        title="Deadline test",
        canonical_url=GOOD_ITEM,
        source_url=GOOD_FEED,
        source_name="Good",
        source_type="rss",
        source_id="good-source",
        category="hackathons",
        deadline_at=NOW + timedelta(hours=24),
        deadline_type="registration",
        deadline_verified=True,
        availability_status="open",
        eligibility_status="eligible",
        verification_status="official_verified",
    )
    at = item.deadline_at - timedelta(hours=remaining)
    assert alert_window(item, at, [24, 6, 1]) == expected


class SequenceSender:
    def __init__(self, failures):
        self.failures = iter(failures)
        self.calls = 0

    def send(self, _notification):
        self.calls += 1
        failure = next(self.failures, None)
        if failure:
            raise failure


def test_delivery_distinguishes_retryable_and_permanent_failures(tmp_path):
    item = Opportunity(
        id="delivery-item",
        title="Delivery test",
        canonical_url=GOOD_ITEM,
        source_url=GOOD_FEED,
        source_name="Good",
        source_type="rss",
        source_id="good-source",
        category="hackathons",
    )
    with Store(tmp_path / "delivery.db", create=True) as store:
        store.upsert_source(source("good-source"))
        store.upsert_opportunity(item)
        retry_note = make_notification("update", [item], NOW, "retry-key")
        assert store.enqueue(retry_note)

        retry_sender = SequenceSender([DeliveryError("temporary", retryable=True), None])
        retry_box = Outbox(store, retry_sender)
        assert retry_box.deliver(NOW, limit=1)["failed"] == 1
        retry_record = next(n for n in store.list_notifications()
                            if n.metadata.get("caller_key") == "retry-key")
        assert retry_record.status == "retryable_failure"
        assert retry_record.next_attempt_at == NOW + timedelta(minutes=5)
        assert retry_box.deliver(NOW + timedelta(minutes=5))["sent"] == 1
        assert retry_sender.calls == 2

        permanent_note = make_notification("update", [item], NOW, "permanent-key")
        assert store.enqueue(permanent_note)
        permanent_sender = SequenceSender([DeliveryError("rejected", retryable=False)])
        permanent_box = Outbox(store, permanent_sender)
        assert permanent_box.deliver(NOW, limit=1)["failed"] == 1
        permanent_record = next(n for n in store.list_notifications()
                                if n.metadata.get("caller_key") == "permanent-key")
        assert permanent_record.status == "permanent_failure"
        assert permanent_box.deliver(NOW + timedelta(hours=1))["sent"] == 0
        assert permanent_sender.calls == 1


def test_offline_demo_command_is_fixture_only_and_side_effect_free():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "scripts/demo.py"], cwd=root,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "Collect -> Normalize -> Verify -> Score -> Store -> Alert" in result.stdout
    assert "price=conditional_free | eligibility=conditional" in result.stdout
    assert "price=unknown | eligibility=unknown" in result.stdout
    assert "verification=community_reported" in result.stdout
    assert "Repeat result: new=0, updated=0, records=5, notifications=2" in result.stdout
    assert "Material update: updated=1" in result.stdout
    assert "SMTP calls: 0" in result.stdout
    assert "default production data touched: no" in result.stdout

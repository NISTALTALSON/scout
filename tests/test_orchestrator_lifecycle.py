"""Offline, deterministic end-to-end coverage for the Scout lifecycle."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from scout.config import Limits, Preferences, Scoring
from scout.http import FetchResult
from scout.models import Source
from scout.notifications.email_sender import DeliveryError
from scout.orchestrator import Scout
from scout.storage.sqlite_store import Store

DAY_ONE = datetime(2026, 10, 19, 4, tzinfo=UTC)
DAY_TWO = DAY_ONE + timedelta(days=1)
DAY_THREE = DAY_ONE + timedelta(days=2)
DEADLINE_DAY = DAY_ONE + timedelta(days=4)

FEED_URL = "https://publisher.example/feed.xml"
DISCOVERED_FEED_URL = "https://publisher.example/events/discovered.xml"
ITEM_URL = "https://publisher.example/p/2026"
TITLE = "India Student Hackathon 2026"


def configured_source():
    return Source(
        id="publisher",
        name="Publisher",
        url=FEED_URL,
        type="rss",
        categories=["hackathons"],
        trust="official",
        official_domains=["publisher.example"],
        enabled=True,
        validation_status="validated",
        validation_note="offline test source",
        scan_interval_hours=6,
        min_interval_hours=3,
        max_interval_hours=24,
        rate_limit_seconds=0,
        discovered_at=DAY_ONE,
    )


def configuration(tmp_path, source):
    return SimpleNamespace(
        directory=tmp_path,
        sources=[source],
        preferences=Preferences(
            timezone="Asia/Kolkata",
            categories={"hackathons": 1.0},
            keywords=["hackathon", "India", "student", "free"],
            min_digest_score=50,
            min_urgent_score=65,
            min_urgent_confidence=75,
            alert_windows_hours=[24, 6, 1],
            limits=Limits(max_seconds=90, max_requests=80, max_candidates=120,
                          max_new_sources=3, max_emails_per_run=5, max_emails_per_day=10),
        ),
        scoring=Scoring(),
        taxonomy={"hackathons": ["hackathon", "competition"]},
    )


class LifecycleHTTP:
    """A phase-controlled HTTP fake; every response is local fixture data."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def get(self, url, **_kwargs):
        self.calls.append((self.clock[0], url))
        if url == FEED_URL:
            return self._feed(url, include_discovery=True)
        if url == DISCOVERED_FEED_URL:
            return self._feed(url, include_discovery=False)
        if url == ITEM_URL:
            return FetchResult(url=url, status_code=200,
                               headers={"content-type": "text/html"},
                               content=self._page().encode(), fetched_at=self.clock[0])
        raise AssertionError(f"unexpected offline HTTP URL: {url}")

    def _description(self):
        deadline = ("22 October 2026 at 5:00 pm IST" if self.clock[0] < DAY_THREE
                    else "23 October 2026 at 5:00 pm IST")
        eligibility = ("Available worldwide."
                       if self.clock[0] < DAY_THREE
                       else "Available to Indian students. Student verification required.")
        return (f"Register now for the {TITLE}. Registration is free. {eligibility} "
                f"Registration closes on {deadline}.")

    def _page(self):
        return f"<main><h1>{TITLE}</h1><p>{self._description()}</p></main>"

    def _feed(self, url, include_discovery):
        alternate = (f'<atom:link href="{DISCOVERED_FEED_URL}" rel="alternate" '
                     'type="application/rss+xml"/>' if include_discovery else "")
        xml = (f'<?xml version="1.0"?><rss version="2.0" '
               'xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
               f"<title>Publisher</title><link>{url}</link><item>"
               f"<title>{TITLE}</title><link>{ITEM_URL}</link>"
               f"<guid>{ITEM_URL}</guid><description>{self._description()}</description>"
               f"{alternate}</item></channel></rss>")
        return FetchResult(url=url, status_code=200,
                           headers={"content-type": "application/rss+xml"},
                           content=xml.encode(), fetched_at=self.clock[0])


class RetryableSender:
    def __init__(self):
        self.attempts = 0
        self.sent = []

    def send(self, notification):
        self.attempts += 1
        if self.attempts == 1:
            raise DeliveryError("temporary offline failure", retryable=True)
        self.sent.append(notification.idempotency_key)


def run_scan(store, config, http, now, **kwargs):
    scout = Scout(store, config, http=http, now=now)
    try:
        return scout.scan(**kwargs)
    finally:
        scout.close()


def test_orchestrator_multi_day_lifecycle_is_offline_and_restart_safe(tmp_path, monkeypatch):
    """Discover, persist, repeat, update, alert, and retry without external services."""
    clock = [DAY_ONE]
    monkeypatch.setattr("scout.orchestrator.utcnow", lambda: clock[0])
    monkeypatch.setattr("scout.orchestrator.time.monotonic", lambda: 0.0)
    source = configured_source()
    config = configuration(tmp_path, source)
    http = LifecycleHTTP(clock)
    db_path = tmp_path / "state.db"

    with Store(db_path, create=True) as store:
        initial = run_scan(store, config, http, DAY_ONE)
        assert initial.sources_ok == 1
        assert initial.new_opportunities == 1
        assert initial.discovered_sources == 1
        item = store.list_opportunities()[0]
        assert item.verification_status == "official_verified"
        assert item.deadline_verified and item.price_status == "verified_free"
        assert item.baseline
        assert len(store.list_sources()) == 2
        discovered = next(item for item in store.list_sources() if item.id != "publisher")
        assert discovered.enabled and discovered.validation_status == "validated"
        assert store.list_runs()[0].new_opportunities == 1

    # Reopen the real SQLite store: the next day starts from persisted state.
    with Store(db_path) as store:
        clock[0] = DAY_TWO
        repeat = run_scan(store, config, http, DAY_TWO)
        assert repeat.new_opportunities == 0
        assert repeat.updated_opportunities == 0
        item = store.list_opportunities()[0]
        assert item.record_version == 1
        assert len(store.list_opportunities()) == 1
        assert not [note for note in store.list_notifications() if note.kind == "update"]

        # The third day changes both the evidenced deadline and eligibility.
        clock[0] = DAY_THREE
        changed = run_scan(store, config, http, DAY_THREE)
        assert changed.updated_opportunities == 1
        item = store.list_opportunities()[0]
        assert item.record_version == 2
        assert item.eligibility_status == "conditional"
        assert item.deadline_at == datetime(2026, 10, 23, 11, 30, tzinfo=UTC)
        updates = [note for note in store.list_notifications() if note.kind == "update"]
        assert len(updates) == 1
        assert updates[0].metadata["records"][0]["record_version"] == 2

        # A deterministic temporary delivery failure remains retryable, then succeeds
        # once the recorded backoff has elapsed.
        sender = RetryableSender()
        failing = Scout(store, config, http=http, sender=sender, now=DAY_THREE)
        assert failing.deliver() == {"sent": 0, "failed": 1, "pending": 1, "dry_run": False}
        failed_note = next(note for note in store.list_notifications() if note.kind == "update")
        assert failed_note.status == "retryable_failure"
        assert failed_note.next_attempt_at == DAY_THREE + timedelta(minutes=5)
        failing.close()

        retry = Scout(store, config, http=http, sender=sender,
                      now=DAY_THREE + timedelta(minutes=5))
        assert retry.deliver()["sent"] == 1
        retry.close()
        assert next(note for note in store.list_notifications()
                    if note.kind == "update").status == "sent"

        # Four days after discovery the persisted deadline is inside its 24-hour
        # window.  Historical notification mode is explicit for this test fixture.
        clock[0] = DEADLINE_DAY
        urgent = run_scan(store, config, http, DEADLINE_DAY, urgent_only=True,
                          discover=False, notify_historical=True)
        assert urgent.sources_planned == 1
        alerts = [note for note in store.list_notifications() if note.kind == "urgent"]
        assert len(alerts) == 1
        assert alerts[0].metadata["alert_at"] == "2026-10-23T11:30:00+00:00"
        assert ":registration:24" in alerts[0].metadata["caller_key"]

    assert {url for _, url in http.calls} <= {FEED_URL, DISCOVERED_FEED_URL, ITEM_URL}


def test_maintenance_queues_weekly_health_once(tmp_path):
    source = configured_source()
    config = configuration(tmp_path, source)
    with Store(tmp_path / "maintenance.db", create=True) as store:
        scout = Scout(store, config, http=LifecycleHTTP([DAY_ONE]), now=DAY_ONE)
        try:
            first = scout.maintain()
            second = scout.maintain()
        finally:
            scout.close()

        assert first["weekly_health_queued"] is True
        assert second["weekly_health_queued"] is False
        assert len([note for note in store.list_notifications() if note.kind == "health"]) == 1

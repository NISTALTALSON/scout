"""Run the complete Scout pipeline against synthetic, offline DEMO DATA.

The command deliberately uses a temporary SQLite database and a local fixture
transport. It does not load the default production configuration/state and never
constructs an SMTP sender.
"""

# The direct ``python scripts/demo.py`` form is documented for convenience; make
# the project package importable even though Python starts with ``scripts/`` on
# sys.path for that form.
# ruff: noqa: E402

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scout.config import Limits, Preferences, Scoring
from scout.http import FetchResult
from scout.intelligence.deadlines import alert_window
from scout.models import Source
from scout.orchestrator import Scout, opportunity_report
from scout.storage.sqlite_store import Store

FIXTURES = ROOT / "demo" / "fixtures"
NOW = datetime(2026, 10, 19, 11, 30, tzinfo=UTC)
REPEAT_AT = NOW + timedelta(hours=1)
UPDATE_AT = NOW + timedelta(hours=4)
FEED_URL = "https://demo.publisher.test/feed.xml"
COMMUNITY_FEED_URL = "https://demo.community.test/feed.xml"
PAGE_URLS = {
    "https://demo.publisher.test/hackathon": "hackathon.html",
    "https://demo.publisher.test/ctf": "ctf.html",
    "https://demo.publisher.test/grant": "grant.html",
    "https://demo.publisher.test/learning": "learning-v1.html",
    "https://demo.community.test/community-claim": "community.html",
}


class FixtureHTTP:
    """HTTP-shaped transport that can only return checked-in fixture bytes."""

    def __init__(self):
        self.phase = 1
        self.calls: list[tuple[str, str]] = []

    def get(self, url, **_kwargs):
        self.calls.append((str(self.phase), url))
        if url == FEED_URL:
            return FetchResult(url=url, status_code=200,
                               headers={"content-type": "application/rss+xml"},
                               content=(FIXTURES / "feed-v1.xml").read_bytes(), fetched_at=NOW)
        if url == COMMUNITY_FEED_URL:
            return FetchResult(url=url, status_code=200,
                               headers={"content-type": "application/rss+xml"},
                               content=(FIXTURES / "community-feed.xml").read_bytes(), fetched_at=NOW)
        if url in PAGE_URLS:
            fixture = PAGE_URLS[url]
            if url.endswith("/learning") and self.phase >= 3:
                fixture = "learning-v2.html"
            return FetchResult(url=url, status_code=200,
                               headers={"content-type": "text/html"},
                               content=(FIXTURES / fixture).read_bytes(), fetched_at=NOW)
        raise AssertionError(f"unexpected DEMO DATA URL: {url}")


def demo_configuration():
    source = Source(
        id="demo-publisher",
        name="DEMO DATA Publisher",
        url=FEED_URL,
        type="rss",
        categories=["hackathons", "cybersecurity", "startups", "learning"],
        trust="official",
        official_domains=["demo.publisher.test"],
        enabled=True,
        validation_status="validated",
        validation_note="Synthetic DEMO DATA fixture",
        scan_interval_hours=0.5,
        min_interval_hours=0.5,
        max_interval_hours=24,
        rate_limit_seconds=0,
        discovered_at=NOW,
    )
    community = Source(
        id="demo-community",
        name="DEMO DATA Community Claims",
        url=COMMUNITY_FEED_URL,
        type="rss",
        categories=["ai_tools"],
        trust="community",
        official_domains=[],
        enabled=True,
        validation_status="validated",
        validation_note="Synthetic low-quality DEMO DATA claim",
        scan_interval_hours=0.5,
        min_interval_hours=0.5,
        max_interval_hours=24,
        rate_limit_seconds=0,
        discovered_at=NOW,
    )
    preferences = Preferences(
        timezone="Asia/Kolkata",
        categories={"hackathons": 1.2, "cybersecurity": 1.1, "startups": 1.0, "learning": 0.9},
        keywords=["hackathon", "cyber", "student", "free", "course", "grant"],
        min_digest_score=0,
        min_urgent_score=0,
        min_urgent_confidence=0,
        alert_windows_hours=[24, 6, 1],
        auto_enable_trusted_sources=False,
        limits=Limits(max_seconds=90, max_requests=50, max_candidates=20, max_new_sources=0,
                      max_emails_per_run=10, max_emails_per_day=10),
    )
    return SimpleNamespace(
        directory=FIXTURES,
        sources=[source, community],
        preferences=preferences,
        scoring=Scoring(),
        taxonomy={
            "ai_tools": ["ai"],
            "cybersecurity": ["ctf", "capture the flag", "cyber", "security"],
            "hackathons": ["hackathon", "developer event", "workshop"],
            "student_benefits": ["student", "education", "voucher"],
            "startups": ["startup", "grant", "accelerator"],
            "career": ["internship", "fellowship", "scholarship"],
            "learning": ["learning", "course", "training"],
            "additional": ["competition", "challenge"],
        },
    )


def run_phase(store, configuration, http, now, phase, label):
    http.phase = phase
    scout = Scout(store, configuration, http=http, now=now)
    try:
        metrics = scout.scan(discover=False, notify_historical=True)
        preview = scout.deliver(dry_run=True)
    finally:
        scout.close()
    print(f"\n{label}")
    print(f"  Collect: {metrics.candidates} candidates from {metrics.sources_ok} source(s); "
          f"{metrics.sources_failed} source failure(s)")
    print(f"  Normalize: {metrics.new_opportunities} new record(s), "
          f"{metrics.updated_opportunities} material update(s)")
    print(f"  Verify: {sum(item.verification_status == 'official_verified' for item in store.list_opportunities())} "
          "officially verified record(s)")
    print(f"  Score: ranked {len(store.list_opportunities())} record(s)")
    print(f"  Store: {len(store.list_opportunities())} opportunity record(s), "
          f"{len(store.list_notifications())} notification(s)")
    print(f"  Alert: {[note.metadata.get('caller_key') for note in store.list_notifications() if note.kind == 'urgent']}")
    print(f"  Simulated email preview: {len(preview.get('previews', []))} message(s); SMTP calls: 0")
    return metrics, preview


def print_records(store, generated_at, title="REPORT"):
    print(f"\n{title} (DEMO DATA)")
    report = opportunity_report(store, generated_at, limit=10)
    for rank, item in enumerate(report["opportunities"], 1):
        evidence = sum(claim["reliable"] for claim in item["claim_evidence"])
        print(f"  #{rank} {item['title']} | category={item['category']} | "
              f"price={item['price_status']} | eligibility={item['eligibility_status']} | "
              f"verification={item['verification_status']} | score={item['total_score']:.2f} | "
              f"evidence={evidence}/{len(item['claim_evidence'])} | "
              f"version={item['record_version']}")


def main() -> int:
    print("SCOUT OFFLINE DEMO - DEMO DATA ONLY")
    print("Synthetic fixture transport; temporary state; real email is disabled.")
    configuration = demo_configuration()
    http = FixtureHTTP()
    with tempfile.TemporaryDirectory(prefix="scout-demo-") as temporary:
        state_path = Path(temporary) / "demo.sqlite3"
        print(f"Temporary state: {state_path}")
        with Store(state_path, create=True) as store:
            initial, initial_preview = run_phase(
                store, configuration, http, NOW, 1, "PHASE 1 - Collect -> Normalize -> Verify -> Score -> Store -> Alert"
            )
            items = store.list_opportunities()
            hackathon = next(item for item in items if "Hackathon" in item.title)
            print("  Deadline policy decisions:")
            for hours in (24, 6, 1):
                boundary = hackathon.deadline_at - timedelta(hours=hours)
                print(f"    remaining={hours}h -> alert_window={alert_window(hackathon, boundary, [24, 6, 1])}h")
            print_records(store, NOW, title="INITIAL REPORT")
            print(f"  Initial preview subjects: {[preview['subject'] for preview in initial_preview['previews']]}")
            print(f"  Initial run: new={initial.new_opportunities}, notifications={len(store.list_notifications())}")

            repeat, repeat_preview = run_phase(
                store, configuration, http, REPEAT_AT, 2, "PHASE 2 - Repeat scan (duplicate suppression)"
            )
            print(f"  Repeat result: new={repeat.new_opportunities}, updated={repeat.updated_opportunities}, "
                  f"records={len(store.list_opportunities())}, notifications={len(store.list_notifications())}")
            print(f"  Repeat preview remains side-effect free: {len(repeat_preview['previews'])} message(s)")

            updated, updated_preview = run_phase(
                store, configuration, http, UPDATE_AT, 3, "PHASE 3 - Material source update + preview"
            )
            learning = next(item for item in store.list_opportunities() if "Learning" in item.title)
            print(f"  Material update: updated={updated.updated_opportunities}, "
                  f"learning price={learning.price_status}, eligibility={learning.eligibility_status}, "
                  f"version={learning.record_version}")
            print(f"  Final preview subjects: {[preview['subject'] for preview in updated_preview['previews']]}")
            print_records(store, UPDATE_AT)

            print(f"\nHTTP fixture calls: {len(http.calls)}; default production data touched: no")
    print("Demo complete: no SMTP sender was created and temporary state was removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

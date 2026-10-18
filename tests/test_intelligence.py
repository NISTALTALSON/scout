from datetime import UTC, datetime, timedelta

import pytest

from scout.budgets import BudgetExceeded
from scout.config import Preferences, Scoring
from scout.intelligence.change_detection import meaningful_changes
from scout.intelligence.deadlines import alert_window, hours_remaining
from scout.intelligence.deduplication import find_duplicate, merge_opportunities
from scout.intelligence.extraction import extract
from scout.intelligence.scoring import score
from scout.intelligence.verification import verify
from scout.models import Candidate, FetchResult, Source

NOW = datetime(2026, 12, 30, 11, 30, tzinfo=UTC)
TAXONOMY = {"hackathons": ["hackathon", "competition"], "learning": ["training", "course"]}


def source(**overrides):
    data = dict(id="org", name="Publisher", url="https://example.org/feed", type="rss",
                categories=["hackathons", "learning"], organization="Example", trust="official",
                validation_status="validated", official_domains=["example.org"])
    data.update(overrides)
    return Source(**data)


def candidate(text="Register now for a free hackathon. Available worldwide. "
                   "Registration closes on 31 December 2026 at 5:00 pm IST.", **overrides):
    data = dict(source_id="org", title="Developer Hackathon 2026", url="https://example.org/2026",
                text=text, metadata={})
    data.update(overrides)
    return Candidate(**data)


class FakeHttp:
    def __init__(self, content: str, status=200, final_url=None):
        self.content, self.status, self.final_url, self.calls = content, status, final_url, []

    def get(self, url):
        self.calls.append(url)
        return FetchResult(url=self.final_url or url, status_code=self.status,
                           content=self.content.encode())


def test_strict_typed_dates_and_official_page_evidence():
    text = ("Register now for a free hackathon. Available worldwide. "
            "Registration closes on 31 December 2026 at 5:00 pm IST.")
    item = extract(candidate(text), source(), NOW, TAXONOMY)
    assert item.deadline_at == datetime(2026, 12, 31, 11, 30, tzinfo=UTC)
    assert item.deadline_type == "registration"
    assert item.price_status == "unknown"  # 'free hackathon' is not evidence of a free entry fee.
    assert item.eligibility_status == "eligible"
    assert hours_remaining(item, NOW) is None
    verified = verify(item, source(), NOW, FakeHttp(f"<h1>{item.title}</h1><p>{text}</p>"))
    assert verified.verification_status == "official_verified"
    assert verified.deadline_verified
    assert hours_remaining(verified, NOW) == 24
    assert alert_window(verified, NOW + timedelta(hours=18), [24, 6, 1]) == 6
    assert alert_window(verified, NOW + timedelta(hours=23, minutes=30), [24, 6, 1]) == 1


@pytest.mark.parametrize("text", [
    "Register for the hackathon. Registration closes on 31 December 2026.",
    "Register for the hackathon. Registration closes on 31 December 2026 at 5:00 pm.",
    "Register for the hackathon. Registration closes on 31 December at 5:00 pm IST.",
    "Register for the hackathon. Limited time! Event starts on 31 December 2026 at 5:00 pm IST.",
])
def test_missing_or_non_deadline_date_cannot_start_countdown(text):
    item = extract(candidate(text), source(), NOW, TAXONOMY)
    checked = verify(item, source(), NOW, FakeHttp(f"<h1>{item.title}</h1><p>{text}</p>"))
    assert hours_remaining(checked, NOW) is None
    assert checked.date_notes or checked.deadline_type == "event_start"


def test_structured_ics_event_start_and_unzoned_metadata_stay_unknown():
    item = extract(candidate(text="Hackathon registration now open.", metadata={
        "start_at": "2027-01-09T09:00:00+05:30", "deadline_at": "2027-01-08"}), source(), NOW,
        TAXONOMY)
    assert item.start_at is not None and item.deadline_at is None
    assert item.date_notes
    assert verify(item, source(), NOW).deadline_verified is False


def test_price_and_eligibility_context_and_exclusions():
    trial = extract(candidate("Register for a hackathon free trial. Free training, "
                              "but certification requires a purchase. Available worldwide except India."),
                    source(), NOW, TAXONOMY)
    assert trial.price_status == "paid"
    assert trial.eligibility_status == "ineligible"
    assert trial.india_eligible is False
    unknown = extract(candidate("Register for a hackathon: free tips and resources. Limited time."),
                      source(), NOW, TAXONOMY)
    assert unknown.price_status == "unknown"
    assert unknown.eligibility_status == "unknown"
    assert unknown.deadline_at is None
    training = extract(candidate("Register for free training at our hackathon."), source(), NOW, TAXONOMY)
    assert training.price_status == "verified_free"
    assert "training" in training.benefit_summary.lower()
    tier = extract(candidate("Sign up for our free tier hackathon."), source(), NOW, TAXONOMY)
    assert tier.price_status == "free_tier"


def test_no_feed_laundering_or_reachable_page_promotion():
    external = extract(candidate(url="https://other.org/promo", text="Register for free training hackathon."),
                       source(), NOW, TAXONOMY)
    assert verify(external, source(), NOW).verification_status != "official_verified"
    assert verify(external, source(), NOW, FakeHttp("<p>Welcome home</p>")).verification_status == "stale"
    official = extract(candidate(), source(), NOW, TAXONOMY)
    assert verify(official, source(), NOW, FakeHttp("<p>Welcome home</p>")).deadline_verified is False
    assert verify(official, source(), NOW, FakeHttp("", 404)).verification_status == "stale"
    redirect = verify(official, source(), NOW, FakeHttp(
        f"<h1>{official.title}</h1><p>{official.summary}</p>", final_url="https://other.org/promo"))
    assert redirect.verification_status != "official_verified"
    assert not redirect.deadline_verified
    community = source(trust="community", official_domains=[])
    assert verify(official, community, NOW).verification_status == "unverified"
    misconfigured = source(url="https://other.org/feed")
    assert verify(official, misconfigured, NOW).verification_status != "official_verified"


def test_unrelated_newsletter_filtered_and_unclear_exclusions_stay_unknown():
    assert extract(candidate(title="Our newsletter", text="Register for our newsletter."),
                   source(), NOW, TAXONOMY) is None
    unclear = extract(candidate("Register for free training hackathon, available worldwide "
                                "except certain regions."), source(), NOW, TAXONOMY)
    assert unclear.eligibility_status == "unknown"
    allowed = extract(candidate("Register for free training hackathon, available worldwide "
                                "except Canada."), source(), NOW, TAXONOMY)
    assert allowed.eligibility_status == "eligible"
    assert extract(candidate("Register for hackathon.", metadata={"price": "Free"}),
                   source(), NOW, TAXONOMY).price_status == "verified_free"
    assert extract(candidate("Register for hackathon.", metadata={"eligibility": "India"}),
                   source(), NOW, TAXONOMY).eligibility_status == "eligible"


def test_explicit_iso_typed_deadline_parses_with_seconds():
    item = extract(candidate("Registration deadline: 2026-12-31T17:00:00+05:30 for hackathon."),
                   source(), NOW, TAXONOMY)
    assert item.deadline_at == datetime(2026, 12, 31, 11, 30, tzinfo=UTC)


def test_no_countdown_after_withdrawal_or_expiry():
    item = verify(extract(candidate(), source(), NOW, TAXONOMY), source(), NOW)
    item.availability_status = "withdrawn"
    assert alert_window(item, NOW, [24, 6, 1]) is None
    assert verify(item, source(), NOW).verification_status == "withdrawn"
    item.availability_status = "open"
    checked = verify(item, source(), NOW + timedelta(hours=25))
    assert checked.verification_status == "expired"
    assert alert_window(checked, NOW + timedelta(hours=25), [24, 6, 1]) is None


def test_registration_link_requires_actual_safe_first_party_anchor():
    item = extract(candidate(metadata={"registration_url": "https://example.org/register"}),
                   source(), NOW, TAXONOMY)
    assert not verify(item, source(), NOW).registration_verified
    html = f'<h1>{item.title}</h1><a href="/register">Register</a><p>{item.summary}</p>'
    assert verify(item, source(), NOW, FakeHttp(html)).registration_verified
    external = extract(candidate(metadata={"registration_url": "https://elsewhere.org/register"}),
                       source(), NOW, TAXONOMY)
    assert not verify(external, source(), NOW, FakeHttp(html.replace("/register", "https://elsewhere.org/register"))).registration_verified


def test_budget_exhaustion_is_not_swallowed():
    class Exhausted:
        def get(self, url):
            raise BudgetExceeded("http_budget")

    with pytest.raises(BudgetExceeded, match="http_budget"):
        verify(extract(candidate(), source(), NOW, TAXONOMY), source(), NOW, Exhausted())


def test_scoring_weights_preferences_penalties_and_confidence():
    verified = verify(extract(candidate(), source(), NOW, TAXONOMY), source(), NOW)
    weighted = Scoring(weights={**Scoring().weights, "relevance": 1000})
    prefs = Preferences(categories={"hackathons": 2}, keywords=["developer"])
    high = score(verified, prefs, weighted, source(), NOW)
    low = score(verified, Preferences(categories={"hackathons": 0.1}), weighted, source(), NOW)
    assert set(high.score_breakdown) == set(Scoring().weights)
    assert high.total_score > low.total_score
    assert high.confidence_score == low.confidence_score
    paid = verified.model_copy(update={"price_status": "paid", "eligibility_status": "ineligible"})
    assert score(paid, prefs, Scoring(), source(), NOW).total_score < score(verified, prefs, Scoring(), source(), NOW).total_score


def test_dedup_conservative_merge_and_material_versions():
    old = extract(candidate(), source(), NOW, TAXONOMY)
    old.notification_history = [{"kind": "digest"}]
    old.feedback, old.baseline = "save", True
    tracking = candidate(url="https://example.org/2026?utm_source=feed&fbclid=1")
    new = extract(tracking, source(), NOW + timedelta(hours=1), TAXONOMY)
    assert find_duplicate(new, [old]) == old
    new.title = "Developer Hackathon 2026!"
    new.summary += " Extra formatting."
    merged, changes = merge_opportunities(old, new, NOW + timedelta(hours=1))
    assert changes == []
    assert merged.record_version == 1
    assert (merged.id, merged.first_seen_at, merged.notification_history, merged.feedback, merged.baseline) == (
        old.id, old.first_seen_at, old.notification_history, "save", True)
    new.price_status = "paid"
    merged, changes = merge_opportunities(old, new, NOW + timedelta(hours=1))
    assert "price_status" in changes and merged.record_version == 2
    assert "price_status" in meaningful_changes(old, merged)
    next_year = extract(candidate(title="Developer Hackathon 2027", url="https://example.org/2026"),
                        source(), NOW, TAXONOMY)
    assert find_duplicate(next_year, [old]) is None
    similar = extract(candidate(url="https://example.org/second", title="Developer Hackathon 2026", metadata={
        "organizer": "Example", "start_at": "2027-01-01T11:00:00Z"}), source(), NOW, TAXONOMY)
    old.organizer, old.start_at = "Example", datetime(2027, 1, 1, 11, tzinfo=UTC)
    assert find_duplicate(similar, [old]) == old

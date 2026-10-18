"""Conservative URL, organizer, edition and title identity matching."""

import re
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from scout.intelligence.change_detection import meaningful_changes
from scout.models import Opportunity
from scout.urls import normalize_url


def _title(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", title.lower()))


def _edition(item: Opportunity) -> str | None:
    years = re.findall(r"\b20\d{2}\b", item.title)
    return years[0] if years else None


def _date(item: Opportunity):
    return item.start_at or item.deadline_at


def _identity_url(value: str) -> str:
    parsed = urlsplit(normalize_url(value))
    query = urlencode(sorted((key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                           if not key.lower().startswith("utm_") and key.lower() not in {
                               "gclid", "fbclid", "mc_cid", "mc_eid"}))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", query, ""))


def find_duplicate(item: Opportunity, existing: list[Opportunity]) -> Opportunity | None:
    """Never conflate explicitly different event editions or unknown organizers by title."""
    url = _identity_url(item.canonical_url)
    for old in existing:
        if item.edition_id and old.edition_id and item.edition_id != old.edition_id:
            continue
        if item.opportunity_type == old.opportunity_type == "student_benefit" and _title(item.title) != _title(old.title):
            continue
        if _edition(item) and _edition(old) and _edition(item) != _edition(old):
            continue
        if (_date(item) and _date(old) and _date(item).year != _date(old).year):
            continue
        if url == _identity_url(old.canonical_url):
            return old
    for old in existing:
        if not item.organizer or not old.organizer or _title(item.organizer) != _title(old.organizer):
            continue
        if not _date(item) or not _date(old) or _date(item).date() != _date(old).date():
            continue
        if _edition(item) and _edition(old) and _edition(item) != _edition(old):
            continue
        if SequenceMatcher(None, _title(item.title), _title(old.title)).ratio() >= 0.88:
            return old
    return None


def merge_opportunities(old: Opportunity, new: Opportunity, now: datetime) -> tuple[Opportunity, list[str]]:
    """Retain persisted identity/history and bump version only for material changes."""
    changes = meaningful_changes(old, new)
    merged = new.model_copy(deep=True)
    merged.id = old.id
    merged.first_seen_at = old.first_seen_at
    merged.last_seen_at = max(now, old.last_seen_at)
    merged.last_changed_at = now if changes else old.last_changed_at
    merged.record_version = old.record_version + int(bool(changes))
    merged.notification_history = [*old.notification_history]
    merged.feedback = old.feedback
    merged.baseline = old.baseline
    merged.related_ids = list(dict.fromkeys([*old.related_ids, *new.related_ids]))
    merged.evidence_urls = list(dict.fromkeys([*old.evidence_urls, *new.evidence_urls]))
    # Keep additional sources of evidence, but not superseded values for changed claims.
    changed_claims = set(changes)
    if "deadline_type" in changes or "deadline_verified" in changes:
        changed_claims.add("deadline_at")
    if "registration_verified" in changes:
        changed_claims.add("registration_url")
    keys = {(e.claim, e.value, e.url) for e in merged.claim_evidence}
    for evidence in old.claim_evidence:
        key = (evidence.claim, evidence.value, evidence.url)
        if evidence.claim not in changed_claims and key not in keys:
            merged.claim_evidence.append(evidence.model_copy(deep=True))
            keys.add(key)
    return merged, changes

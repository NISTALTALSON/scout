"""Bounded source checks; a reachable URL is not proof of a promotion."""

import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scout.budgets import BudgetExceeded
from scout.intelligence.extraction import _dates, _eligibility, _pricing
from scout.models import Candidate, ClaimEvidence, Opportunity, Source
from scout.urls import is_official_url, normalize_url


def _publisher(item: Opportunity, source: Source) -> bool:
    return (source.trust == "official" and source.validation_status == "validated"
            and is_official_url(source.url, source.official_domains)
            and is_official_url(item.canonical_url, source.official_domains))


def _normalized(value: str) -> str:
    return " ".join(unescape(value).lower().split())


def _supported(claim: ClaimEvidence, page: str) -> bool:
    """Only actual same-page literal claims; generated labels are never page quotations."""
    excerpt = _normalized(claim.excerpt)
    page = _normalized(page)
    if claim.claim == "title":
        return len(excerpt) > 8 and excerpt in page
    if claim.claim in {"deadline_at", "start_at", "end_at"}:
        # ISO normalized values may differ from on-page local time: require the original quote.
        return bool(excerpt and excerpt in page)
    if claim.claim == "registration_url":
        return False  # Checked against the actual anchor separately.
    return bool(len(excerpt) >= 5 and excerpt in page)


def _same_link(url: str, target: str) -> bool:
    try:
        return normalize_url(url) == normalize_url(target)
    except ValueError:
        return False


def verify(opportunity: Opportunity, source: Source, now: datetime, http=None) -> Opportunity:
    """Check the original page, preserving uncertainty when it cannot be checked."""
    item = opportunity.model_copy(deep=True)
    official = _publisher(item, source)
    page = None
    anchors: list[tuple[str, str]] = []
    status = None
    if http is not None:
        try:
            result = http.get(item.canonical_url)
            status = result.status_code
            # Redirects may be safe to fetch, but may not inherit the original host's authority.
            if official and not is_official_url(result.url, source.official_domains):
                official = False
            if status == 200 and result.content:
                soup = BeautifulSoup(result.content[:2_097_152], "html.parser")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                anchors = [(urljoin(result.url, a.get("href", "")), a.get_text(" ", strip=True))
                           for a in soup.find_all("a", href=True)[:500]]
                body = soup.find("article") or soup.find("main") or soup
                for tag in body.find_all(["nav", "footer", "header"]):
                    tag.decompose()
                page = body.get_text(" ", strip=True)[:200_000]
                # Refresh actual facts from the original page, not just the feed snippet.
                # Only do this when the existing title is present: a redirected home page
                # must not replace an opportunity with unrelated generic promotional copy.
                if _normalized(item.title) in _normalized(page) and item.opportunity_type != "student_benefit":
                    fresh = item.model_copy(deep=True)
                    fresh.claim_evidence = [e for e in fresh.claim_evidence if e.claim in {"title", "organizer"}]
                    fresh.deadline_at = fresh.start_at = fresh.end_at = None
                    fresh.deadline_type = "unknown"
                    fresh.date_notes = []
                    fresh.price_status, fresh.eligibility_status = "unknown", "unknown"
                    fresh.india_eligible = fresh.student_verification_required = None
                    fresh.eligibility = "Eligibility for Indian students could not be confirmed."
                    candidate = Candidate(source_id=source.id, title=item.title,
                                          url=item.canonical_url, text=page[:40000], collected_at=now)
                    _dates(fresh, candidate, now)
                    _pricing(fresh, candidate.text, {}, now)
                    _eligibility(fresh, candidate.text, {}, now)
                    # A structured feed may carry an explicit field absent from rendered
                    # text; keep it as an unverified claim rather than invent a replacement.
                    if fresh.deadline_at is None and item.deadline_at is not None:
                        fresh.deadline_at, fresh.deadline_type = item.deadline_at, item.deadline_type
                        fresh.claim_evidence.extend(e for e in item.claim_evidence if e.claim == "deadline_at")
                    fresh.availability_status = "unknown"
                    if re.search(r"\b(?:offer\s+withdrawn|event\s+cancelled|event\s+canceled)\b", page, re.I):
                        fresh.availability_status = "withdrawn"
                    elif re.search(r"\b(?:fully\s+booked|sold\s+out)\b", page, re.I):
                        fresh.availability_status = "full"
                    elif re.search(r"\b(?:registration\s+closed|applications?\s+closed|offer\s+ended)\b", page, re.I):
                        fresh.availability_status = "closed"
                    elif re.search(r"\b(?:register\s+now|registration\s+open|apply\s+now|applications?\s+open)\b", page, re.I):
                        fresh.availability_status = "open"
                    if item.registration_url:
                        fresh.registration_url = item.registration_url
                        fresh.claim_evidence.extend(e for e in item.claim_evidence if e.claim == "registration_url")
                    item = fresh
                if official and item.registration_url is None:
                    for link, label in anchors:
                        if is_official_url(link, source.official_domains) and re.search(
                            r"\b(?:register|apply|enroll|sign\s*up)\b", label, re.I
                        ):
                            item.registration_url = normalize_url(link)
                            item.claim_evidence.append(ClaimEvidence(
                                claim="registration_url", value=item.registration_url,
                                url=item.canonical_url, excerpt=label[:1000], checked_at=now))
                            break
            # 304 cannot verify content without a persisted prior page.
        except BudgetExceeded:
            raise
        except (OSError, ValueError, RuntimeError, TimeoutError):
            pass
    supported = []
    for claim in item.claim_evidence:
        if claim.url != item.canonical_url:
            continue
        if page is not None:
            reliable = _supported(claim, page)
        elif http is None:
            # A validated official publisher's *own* structured feed is first-party
            # evidence, but is never independent corroboration of linked third-party pages.
            reliable = official and bool(claim.excerpt.strip())
        else:
            reliable = False
        if reliable:
            supported.append(claim.claim)
        claim.authority = "official" if official else source.trust if source.trust != "official" else "unknown"
        claim.reliable = reliable
        claim.checked_at = now
    item.verification_checked_at = now
    if item.price_status == "verified_free" and not (
        official and "price_status" in supported
    ):
        item.price_status = "unknown"
    item.deadline_verified = (item.deadline_at is not None and item.deadline_type not in
                              {"unknown", "event_start"} and "deadline_at" in supported and official)
    item.registration_verified = False
    if page is not None and item.registration_url and official and is_official_url(
        item.registration_url, source.official_domains
    ):
        item.registration_verified = any(
            _same_link(link, item.registration_url)
            and re.search(r"\b(?:register|apply|enroll|sign\s*up|join)\b", label, re.I)
            for link, label in anchors if link.startswith(("https://", "http://"))
        )
        if item.registration_verified:
            for claim in item.claim_evidence:
                if claim.claim == "registration_url" and claim.value == item.registration_url:
                    claim.reliable, claim.authority, claim.checked_at = True, "official", now
    if item.availability_status == "withdrawn":
        item.verification_status = "withdrawn"
    elif item.availability_status == "expired" or (item.deadline_verified and item.deadline_at <= now):
        item.availability_status, item.verification_status = "expired", "expired"
    elif item.availability_status in {"closed", "full"}:
        item.verification_status = "stale"
    elif http is not None and (status in {404, 410} or (status == 200 and not supported)):
        item.verification_status = "stale"
    elif official and supported:
        item.verification_status = "official_verified"
    elif source.trust == "community" and supported:
        item.verification_status = "community_reported"
    else:
        item.verification_status = "unverified"
    return item

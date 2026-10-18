"""Extract only contextual claims from untrusted, bounded source text."""

import hashlib
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from scout.models import Candidate, ClaimEvidence, Opportunity, Source, web_url
from scout.urls import normalize_url

_DATE = r"(?:20\d{2}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]+\s+20\d{2}|[A-Za-z]+\s+\d{1,2},?\s+20\d{2})"
_TIME = r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:\s*[ap]m)?|(?:1[0-2]|[1-9])\s*[ap]m"
_ZONE = r"(?:Z|UTC|GMT|IST|[+-](?:0\d|1[0-4]):?[0-5]\d|[A-Za-z_]+/[A-Za-z_]+)"
_STAMP = re.compile(rf"(?P<date>{_DATE})\s*(?:at|,|T|\s)\s*(?P<time>{_TIME})\s*(?P<zone>{_ZONE})\b", re.I)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$", re.I)
_TYPED = re.compile(
    r"\b(registration\s+(?:deadline|closes?|ends?)|(?:apply|application)\s+(?:by|deadline|closes?)|"
    r"(?:offer|promotion|discount)\s+(?:expires?|ends?|valid\s+(?:until|through))|"
    r"(?:redeem|redemption)\s+(?:by|deadline|ends?)|"
    r"(?:submit|submission)\s+(?:by|deadline|closes?)|"
    r"(?:event|workshop|hackathon)\s+(?:starts?|begins?)|"
    r"(?:starts?|begins?)\s+on)\s*[:\-]?\s*(?:on|at|by|until)?\s*",
    re.I,
)
_ACTION = re.compile(
    r"\b(apply|applications?\s+open|register|registration|sign\s*up|enroll|join\s+(?:the|our)|"
    r"redeem|claim|submit|hackathon|scholarship|internship|fellowship|free\s+(?:tier|course|training|"
    r"certification)|trial|discount|offer|competition|challenge|credits?|workshop|webinar)\b", re.I,
)


def _excerpt(text: str, match: re.Match | None, fallback: str) -> str:
    if match:
        start, end = match.span()
        return " ".join(text[max(0, start - 65):min(len(text), end + 90)].split())[:1000]
    return str(fallback)[:1000]


def _date_value(value: object) -> datetime | None:
    """Accept explicit year, calendar day, wall time AND zone; never infer any of these."""
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo and value.utcoffset() is not None else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if _ISO.fullmatch(value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    match = _STAMP.fullmatch(value)
    if not match:
        return None
    try:
        import dateparser

        zone = match["zone"].upper()
        if zone in {"IST", "UTC", "GMT"}:
            from datetime import timedelta, timezone

            tz = timezone(timedelta(hours=5, minutes=30)) if zone == "IST" else UTC
        elif "/" in match["zone"]:
            tz = ZoneInfo(match["zone"])
        elif zone == "Z":
            tz = UTC
        else:
            compact = zone.replace(":", "")
            hours, minutes = int(compact[1:3]), int(compact[3:5])
            if hours > 14 or minutes > 59 or (hours == 14 and minutes):
                return None
            tz = timezone((1 if compact[0] == "+" else -1) * timedelta(hours=hours, minutes=minutes))
        parsed = dateparser.parse(
            f"{match['date']} {match['time']}",
            settings={"DATE_ORDER": "DMY", "STRICT_PARSING": True, "RETURN_AS_TIMEZONE_AWARE": False},
        )
        return parsed.replace(tzinfo=tz).astimezone(UTC) if parsed else None
    except (ValueError, TypeError, OverflowError):
        return None


def _claim(item: Opportunity, name: str, value: object, excerpt: str, now: datetime) -> None:
    item.claim_evidence.append(ClaimEvidence(
        claim=name, value=str(value), url=item.canonical_url, excerpt=excerpt,
        checked_at=now, authority="unknown", reliable=False,
    ))


def _classified_date(label: str) -> str:
    label = label.lower()
    if "registration" in label:
        return "registration"
    if "application" in label or "apply" in label:
        return "application"
    if "offer" in label or "promotion" in label or "discount" in label:
        return "offer_expiry"
    if "redeem" in label or "redemption" in label:
        return "credit_redemption"
    if "submit" in label or "submission" in label:
        return "submission"
    return "event_start"


def _dates(item: Opportunity, candidate: Candidate, now: datetime) -> None:
    metadata = candidate.metadata
    for key in ("start_at", "end_at", "deadline_at"):
        value = metadata.get(key)
        if value is None:
            continue
        parsed = _date_value(value)
        if not parsed:
            item.date_notes.append(f"{key} lacks an explicit valid date, time, year or timezone")
            continue
        setattr(item, key, parsed)
        _claim(item, key, parsed.isoformat(), str(value), now)
        if key == "deadline_at":
            kind = str(metadata.get("deadline_type", "unknown"))
            if kind in {"registration", "offer_expiry", "application", "credit_redemption", "submission"}:
                item.deadline_type = kind
            else:
                item.date_notes.append("deadline type unconfirmed; no countdown")
    for typed in _TYPED.finditer(candidate.text[:40000]):
        stamp = _STAMP.match(candidate.text, typed.end())
        if not stamp:
            if re.match(r"(?:\d{1,2}\s+[A-Za-z]+|[A-Za-z]+\s+\d{1,2})\b",
                        candidate.text[typed.end():typed.end() + 45], re.I):
                item.date_notes.append("Typed date lacks explicit year, time or timezone")
            continue
        parsed = _date_value(stamp.group())
        if not parsed:
            item.date_notes.append("Invalid typed date or timezone")
            continue
        kind = _classified_date(typed.group())
        field = "start_at" if kind == "event_start" else "deadline_at"
        if field == "start_at":
            item.start_at = parsed
            # Event start is useful context, not an application/registration deadline.
            if item.deadline_at is None:
                item.deadline_type = "event_start"
        elif item.deadline_at is None or item.deadline_type == "unknown":
            item.deadline_at, item.deadline_type = parsed, kind
        _claim(item, field, parsed.isoformat(), _excerpt(candidate.text, typed, stamp.group()), now)
    if candidate.metadata.get("start_at") and candidate.metadata.get("deadline_at") is None:
        item.date_notes.append("Event start does not imply registration deadline")


def _pricing(item: Opportunity, text: str, metadata: dict, now: datetime) -> None:
    offer = str(metadata.get("price", ""))[:2000]
    joined = f"{offer}. {text}" if offer else text
    patterns = (
        ("paid", r"\b(?:purchase\s+required|requires?\s+(?:a\s+)?purchase|paid\s+(?:plan|subscription|course)|"
         r"(?:costs?|price|fee)\s*[:=]?\s*(?:₹|\$|INR|USD)\s*\d+)\b"),
        ("free_trial", r"\b(?:free\s+trial|trial\s+(?:for|of)\s+\d+\s+days?)\b"),
        ("conditional_free", r"\b(?:free\s+(?:with|after|if|for\s+eligible|for\s+verified)|"
         r"(?:purchase|payment|subscription)\s+(?:needed|required)\s+to\s+(?:claim|redeem))\b"),
        ("free_tier", r"\b(?:free\s+tier|(?:free|no.cost)\s+plan|always\s+free)\b"),
        ("verified_free", r"\b(?:free\s+(?:course|training|workshop|webinar|certification|voucher|"
         r"access|credits?|license|scholarship)|(?:course|training|workshop|webinar|certification|"
         r"voucher|registration|entry|access)\s+(?:is\s+)?free|no\s+(?:registration\s+)?fee)\b"),
    )
    hits = [(status, re.search(pattern, joined, re.I)) for status, pattern in patterns]
    hits = [(status, match) for status, match in hits if match]
    if re.fullmatch(r"\s*(?:free|no\s+cost|0|₹\s*0|\$\s*0)\s*", offer, re.I):
        hits.append(("verified_free", re.search(re.escape(offer), joined)))
    if hits:
        priority = {"paid": 5, "free_trial": 4, "conditional_free": 3, "free_tier": 2,
                    "verified_free": 1}
        status, match = max(hits, key=lambda hit: priority[hit[0]])
        item.price_status = status
        _claim(item, "price_status", status, _excerpt(joined, match, offer), now)
    if offer:
        item.benefit_summary = offer[:500] if item.price_status != "unknown" else item.benefit_summary
    elif item.price_status != "unknown":
        item.benefit_summary = _excerpt(joined, hits[0][1], text)[:500]


def _eligibility(item: Opportunity, text: str, metadata: dict, now: datetime) -> None:
    raw = str(metadata.get("eligibility", ""))[:2000]
    joined = f"{raw}. {text}" if raw else text
    exclusion = re.search(r"\b(?:except|excluding|not\s+(?:available|eligible)\s+(?:in|for|to)|"
                          r"unavailable\s+in)\s+[^.;\n]{0,100}\bIndia\b|"
                          r"\bIndia(?:n\s+(?:residents?|students?))?\s+(?:is\s+)?not\s+eligible\b",
                          joined, re.I)
    exclusions = re.search(r"\b(?:worldwide|global)\b[^.;\n]{0,100}\b(?:except|excluding)\s+([^.;\n]+)",
                           joined, re.I)
    countries = (r"(?:Canada|United States|USA|US|United Kingdom|UK|China|Russia|"
                 r"Australia|Germany|France|Brazil|Japan|Singapore|Pakistan|Iran|North Korea)")
    unclear_exclusion = bool(exclusions and not re.fullmatch(
        rf"\s*{countries}(?:\s*(?:,|and|&)\s*{countries})*\s*", exclusions[1], re.I
    ))
    local = re.search(r"\b(?:available|open|eligible|valid)\s+(?:to|in|for)\s+[^.;\n]{0,65}\bIndia\b|"
                      r"\bIndian\s+(?:students?|residents?|developers?)\b|"
                      r"\b(?:available|open|eligible)\s+worldwide\b|\bworldwide\s+(?:access|eligibility)\b",
                      joined, re.I)
    if exclusion:
        item.eligibility_status, item.india_eligible = "ineligible", False
        item.eligibility = _excerpt(joined, exclusion, raw)
    elif (local or raw.strip().lower() in {"india", "indian students", "worldwide"}) and not unclear_exclusion:
        item.india_eligible = True
        item.student_verification_required = bool(re.search(r"\b(?:verified\s+student|student\s+verification|"
                                                          r"student\s+email\s+required)\b", joined, re.I))
        item.eligibility_status = "conditional" if item.student_verification_required else "eligible"
        item.eligibility = _excerpt(joined, local, raw)
    if item.eligibility_status != "unknown":
        _claim(item, "eligibility_status", item.eligibility_status, item.eligibility, now)


def extract(candidate: Candidate, source: Source, now: datetime, taxonomy: dict[str, list[str]]) -> Opportunity | None:
    """Build one evidenced actionable record; absent or ambiguous claims remain unknown."""
    text = " ".join(candidate.text.split())[:40000]
    subject = f"{candidate.title} {text}"
    if not _ACTION.search(subject):
        return None
    categories = [(category, sum(2 if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", candidate.title, re.I)
                                else 1 if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.I)
                                else 0 for term in terms)) for category, terms in taxonomy.items()]
    category, strength = max(categories, key=lambda pair: pair[1], default=("", 0))
    if not category or not strength:
        return None
    canonical = normalize_url(candidate.url)
    item = Opportunity(
        id=hashlib.sha256(canonical.encode()).hexdigest()[:24], title=candidate.title.strip(),
        summary=text[:2000], canonical_url=canonical, source_url=source.url, source_name=source.name,
        source_type=source.type, source_id=source.id, category=category,
        discovery_method=candidate.discovery_method, published_at=candidate.published_at,
        first_seen_at=now, last_seen_at=now, last_changed_at=now,
        evidence_urls=[canonical], organizer=str(candidate.metadata["organizer"])[:200]
        if candidate.metadata.get("organizer") else None,
    )
    _claim(item, "title", item.title, item.title, now)
    if item.organizer:
        _claim(item, "organizer", item.organizer, str(candidate.metadata["organizer"]), now)
    _dates(item, candidate, now)
    _pricing(item, subject, candidate.metadata, now)
    _eligibility(item, subject, candidate.metadata, now)
    registration = candidate.metadata.get("registration_url")
    if isinstance(registration, str):
        try:
            item.registration_url = web_url(registration)
            _claim(item, "registration_url", registration, registration, now)
        except ValueError:
            pass
    if re.search(r"\b(?:withdrawn|cancelled|canceled)\b", text, re.I):
        item.availability_status = "withdrawn"
    elif re.search(r"\b(?:registration\s+closed|applications?\s+closed|fully\s+booked|sold\s+out)\b", text, re.I):
        item.availability_status = "full" if re.search(r"fully\s+booked|sold\s+out", text, re.I) else "closed"
    elif item.deadline_at and item.deadline_at <= now:
        item.availability_status = "expired"
    elif re.search(r"\b(?:registration\s+open|applications?\s+open|apply\s+now|register\s+now)\b", subject, re.I):
        item.availability_status = "open"
    item.content_hash = hashlib.sha256(f"{item.title}|{item.summary}".encode()).hexdigest()
    return item

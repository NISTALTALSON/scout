"""Source onboarding requires a real, budgeted probation fetch."""

import hashlib
from urllib.parse import urlsplit

from scout.budgets import BudgetExceeded
from scout.collectors import parse
from scout.http import FetchError
from scout.models import Source
from scout.urls import is_official_url, normalize_url


def _kind(response):
    content_type = response.headers.get("content-type", "").lower()
    start = response.content[:250].lstrip().lower()
    if "calendar" in content_type or start.startswith(b"begin:vcalendar"):
        return "calendar"
    if "json" in content_type or start.startswith((b"[", b"{")):
        return "api"
    if any(word in content_type for word in ("xml", "rss", "atom")) or start.startswith(b"<?xml"):
        return "rss"
    return "html"


def _relevant(url):
    parts = urlsplit(url)
    path = parts.path.lower()
    relevant = any(word in path for word in ("event", "news", "program", "hackathon",
                                            "challenge", "opportunit", "feed", "rss",
                                            "atom", "calendar", "release"))
    return (relevant or path in {"", "/"}) and not any(word in parts.hostname.lower() for word in
                                ("track", "analytics", "advert", "doubleclick"))


def discover_sources(urls, origin, existing, http, budget, now, auto_enable=True):
    existing = list(existing)
    publisher = origin if isinstance(origin, Source) else next(
        (s for s in existing if s.id == origin), None)
    known = set()
    for source in existing:
        try:
            known.add(normalize_url(source.url))
        except ValueError:
            continue
    found = []
    pending = list(urls)[:20]
    inspected = set()
    capacity = min(3, max(0, budget.max_new_sources - budget.new_sources))
    while pending and len(found) < capacity:
        raw = pending.pop(0)
        try:
            url = normalize_url(raw)
        except (TypeError, ValueError):
            continue
        if url in known or url in inspected or (publisher and not _relevant(url)):
            continue
        inspected.add(url)
        try:
            response = http.get(url)
            if response.status_code != 200:
                continue
            kind = _kind(response)
            # Prefer an actual advertised feed to a generic HTML wrapper.
            if kind == "html":
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(response.content, "html.parser")
                found_feed = False
                for tag in soup.select('link[rel~="alternate"][href]')[:10]:
                    if tag.get("type", "").lower() in {"application/rss+xml", "application/atom+xml"}:
                        from urllib.parse import urljoin

                        try:
                            feed = normalize_url(urljoin(response.url, tag["href"]))
                        except ValueError:
                            continue
                        if feed not in inspected and feed not in known:
                            pending.insert(0, feed)
                            found_feed = True
                if found_feed:
                    # Do not onboard a listing shell before testing its advertised feed.
                    continue
            final_url = normalize_url(response.url)
            owner = next((s for s in existing if s.trust == "official" and
                          s.validation_status == "validated" and
                          is_official_url(final_url, s.official_domains)), None)
            trusted = owner is not None
            source = Source(id="source-" + hashlib.sha256(final_url.encode()).hexdigest()[:20],
                            name=urlsplit(final_url).hostname or "Discovered source",
                            url=final_url, type=kind,
                            categories=list(publisher.categories) if publisher else ["general"],
                            organization=owner.organization if owner else "Unknown publisher",
                            trust="official" if trusted else "unknown",
                            official_domains=list(owner.official_domains) if owner else [],
                            discovery_origin=publisher.id if publisher else str(origin)[:100],
                            discovered_at=now)
            trial = parse(source, response, now)
            useful = bool(trial.candidates)
            source.validation_status = "validated" if trusted and useful else (
                "probation" if trusted else "quarantined")
            source.validation_note = "Useful probation fetch" if useful else "No useful candidates; review required"
            if not trusted:
                source.validation_note = "Unknown publisher domain; manual review required"
            source.probation_successes = int(useful)
            source.last_attempt_at = now
            source.last_success_at = now
            source.etag = trial.metadata.get("etag")
            source.last_modified = trial.metadata.get("last_modified")
            source.content_hash = trial.metadata.get("content_hash")
            source.enabled = bool(auto_enable and trusted and useful)
            source.auto_enabled = source.enabled
            if source.enabled:
                source.validated_at = now
            budget.consume_source()
            found.append(source)
            known.add(final_url)
        except BudgetExceeded:
            raise
        except (FetchError, ValueError, TypeError):
            continue
    return found

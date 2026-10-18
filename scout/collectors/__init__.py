"""Bounded, data-only parsing of configured source responses."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
from bs4 import BeautifulSoup
from icalendar import Calendar
from soupsieve import SelectorSyntaxError

from scout.models import Candidate, CollectionResult
from scout.urls import normalize_url


def _url(base, href):
    try:
        return normalize_url(urljoin(base, href)) if isinstance(href, str) and href else None
    except ValueError:
        return None


def _date(value):
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else None
    return None  # no guessed timezone or date-only countdowns


def _published(value):
    if not isinstance(value, str):
        return None
    try:
        return _date(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        try:
            return _date(parsedate_to_datetime(value))
        except (TypeError, ValueError):
            return None


def _candidate(source, title, url, text, now, metadata=None, published=None, links=None):
    if not title or not url:
        return None
    try:
        return Candidate(source_id=source.id, title=str(title).strip()[:500], url=url,
                         text=str(text or "")[:40000], collected_at=now,
                         published_at=_date(published), metadata=metadata or {},
                         links=(links or [])[:12])
    except ValueError:
        return None


def _limit(source):
    value = source.options.get("limit", 20)
    return min(50, max(1, value)) if type(value) is int else 20


def _feed(source, response, now):
    feed = feedparser.parse(response.content)  # bytes: feedparser does not fetch remote resources
    results = []
    for entry in feed.entries[:_limit(source)]:
        url = _url(response.url, entry.get("link"))
        metadata = {key: entry[key] for key in ("summary", "description") if
                    isinstance(entry.get(key), str)}
        for key in ("start_at", "deadline_at", "price", "eligibility", "organizer"):
            if isinstance(entry.get(key), str):
                metadata[key] = entry[key][:2000]
        raw_date = entry.get("published") or entry.get("updated")
        if isinstance(raw_date, str):
            metadata["date_excerpt"] = raw_date[:500]
        published = _published(raw_date)
        links = [link for item in entry.get("links", [])
                 if (link := _url(response.url, item.get("href")))]
        item = _candidate(source, entry.get("title"), url,
                          BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True),
                          now, metadata, published, links)
        if item:
            results.append(item)
    return results, []


def _html(source, response, now):
    soup = BeautifulSoup(response.content, "html.parser")
    candidates, discovered = [], []
    selectors = source.selectors
    if source.options.get("single_page"):
        heading = soup.find("h1") or soup.find("title")
        body = soup.find("main") or soup.find("article") or soup
        for tag in body.find_all(["script", "style", "nav", "footer", "noscript"]):
            tag.decompose()
        item = _candidate(source, heading.get_text(" ", strip=True) if heading else source.name,
                          response.url, body.get_text(" ", strip=True), now)
        if item:
            candidates.append(item)
    if source.adapter == "student_pack":
        # Benefit headings are the actual publisher's structure, not invented redemption URLs.
        headings = soup.find_all("h3")
        for heading in headings:
            parts = []
            for next_node in heading.next_elements:
                if getattr(next_node, "name", None) == "h3":
                    break
                if isinstance(next_node, str) and next_node.strip():
                    parts.append(next_node.strip())
            text = " ".join(parts)[:4000]
            if "Offer" not in text or "About" not in text:
                continue
            name = heading.get_text(" ", strip=True)
            item = _candidate(source, f"{name} — student benefit", response.url,
                              f"Student Developer Pack offer. {text}", now,
                              {"benefit_name": name, "student_pack": True})
            if item:
                candidates.append(item)
            if len(candidates) >= _limit(source):
                break
    # Only explicitly configured selectors produce ordinary page candidates.
    if selectors.get("item"):
        for node in soup.select(selectors["item"])[:_limit(source)]:
            title_node = node.select_one(selectors.get("title", "h2, h3, h1"))
            link_node = node.select_one(selectors.get("link", "a[href]"))
            text_node = node.select_one(selectors["text"]) if selectors.get("text") else node
            date_node = node.select_one(selectors["date"]) if selectors.get("date") else None
            title = title_node.get_text(" ", strip=True) if title_node else (
                node.get_text(" ", strip=True) if node.name == "a" else None)
            link_node = node if node.name == "a" and node.get("href") else link_node
            url = _url(response.url, link_node.get("href")) if link_node else None
            metadata = {"date_excerpt": date_node.get_text(" ", strip=True)[:500]} if date_node else {}
            for key in ("start_at", "deadline_at", "price", "eligibility", "organizer"):
                if selectors.get(key) and (field := node.select_one(selectors[key])):
                    metadata[key] = field.get_text(" ", strip=True)[:2000]
            item = _candidate(source, title, url,
                              text_node.get_text(" ", strip=True) if text_node else "", now, metadata)
            if item:
                candidates.append(item)
    # JSON-LD is literal structured data, never a command or a publisher assertion.
    for script in soup.select('script[type="application/ld+json"]')[:10]:
        try:
            payload = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        stack = [payload]
        inspected = 0
        while stack and inspected < 100 and len(candidates) < _limit(source):
            inspected += 1
            value = stack.pop()
            if isinstance(value, list):
                stack.extend(value[:50])
            elif isinstance(value, dict):
                for key in ("@graph", "itemListElement"):
                    if key in value:
                        stack.append(value[key])
                if isinstance(value.get("item"), dict):
                    stack.append(value["item"])
                title = value.get("name") or value.get("headline")
                url = _url(response.url, value.get("url"))
                if not isinstance(title, str) or not url:
                    continue
                metadata = {key: value[key] for key in ("startDate", "endDate", "validThrough",
                            "price", "description") if isinstance(value.get(key), (str, int, float))}
                if isinstance(value.get("offers"), dict):
                    metadata["price"] = value["offers"].get("price")
                if isinstance(value.get("organizer"), dict):
                    metadata["organizer"] = value["organizer"].get("name")
                for structured, standard in (("startDate", "start_at"),
                                             ("endDate", "end_at"),
                                             ("validThrough", "deadline_at")):
                    if structured in metadata:
                        metadata[standard] = metadata[structured]
                item = _candidate(source, title, url, value.get("description", ""), now, metadata)
                if item and item.url not in {c.url for c in candidates}:
                    candidates.append(item)
    for node in soup.select('link[rel~="alternate"][href]')[:20]:
        if node.get("type", "").lower() in {"application/rss+xml", "application/atom+xml"}:
            if (url := _url(response.url, node.get("href"))):
                discovered.append(url)
    # Organizer links are selected explicitly, not broad navigation/tracking links.
    if selectors.get("discovery"):
        for node in soup.select(selectors["discovery"])[:20]:
            if (url := _url(response.url, node.get("href"))):
                discovered.append(url)
    return candidates, list(dict.fromkeys(discovered))[:20]


def _records(source, response, now):
    payload = json.loads(response.content)
    if source.type == "github":
        if isinstance(payload, dict) and "items" in payload:
            payload = payload["items"]
        elif isinstance(payload, dict):
            payload = [payload]
        rows = payload if isinstance(payload, list) else []
        results = []
        for row in rows[:_limit(source)]:
            if not isinstance(row, dict):
                continue
            title = row.get("name") or row.get("full_name") or row.get("tag_name")
            url = _url(response.url, row.get("html_url"))
            text = row.get("body") or row.get("description") or ""
            published = row.get("published_at") or row.get("created_at")
            metadata = {"date_excerpt": published} if isinstance(published, str) else {}
            item = _candidate(source, title, url, text, now, metadata)
            if item:
                results.append(item)
        return results, []
    if source.adapter == "ctftime":
        rows = payload if isinstance(payload, list) else []
        results = []
        for row in rows[:_limit(source)]:
            if not isinstance(row, dict):
                continue
            url = _url(response.url, row.get("url") or row.get("ctftime_url"))
            metadata = {key: row[key] for key in ("start", "finish", "onsite", "format",
                        "organizers", "restrictions") if key in row}
            metadata.update({"start_at": row.get("start"), "end_at": row.get("finish"),
                             "external_id": str(row.get("id", ""))})
            item = _candidate(source, row.get("title"), url,
                              "Capture the flag competition. " + str(row.get("description", "")),
                              now, metadata)
            if item:
                results.append(item)
        return results, []
    path = source.options.get("items_path", "items")
    if isinstance(payload, dict) and isinstance(path, str):
        for part in path.split(".")[:5]:
            payload = payload.get(part) if isinstance(payload, dict) else None
    rows = payload if isinstance(payload, list) else []
    fields = {k: source.options.get(k + "_field", k) for k in
              ("title", "url", "text", "date", "start_at", "deadline_at", "price", "eligibility",
               "registration_url", "organizer")}
    results = []
    for row in rows[:_limit(source)]:
        if not isinstance(row, dict):
            continue
        def get(key, record=row):
            return record.get(fields[key]) if isinstance(fields[key], str) else None
        metadata = {key: get(key) for key in ("start_at", "deadline_at", "price", "eligibility",
                    "registration_url", "organizer") if get(key) is not None}
        if get("date") is not None:
            metadata["date_excerpt"] = get("date")
        item = _candidate(source, get("title"), _url(response.url, get("url")), get("text"),
                          now, metadata)
        if item:
            results.append(item)
    return results, []


def _calendar(source, response, now):
    calendar = Calendar.from_ical(response.content)
    results = []
    for event in calendar.walk("VEVENT")[:_limit(source)]:
        start = event.get("dtstart")
        end = event.get("dtend")
        metadata = {}
        if event.get("uid"):
            metadata["external_id"] = str(event.get("uid"))
        if start:
            metadata["start_at"] = str(start.dt)
        if end:
            metadata["end_at"] = str(end.dt)
        url = _url(response.url, str(event.get("url", ""))) or response.url
        item = _candidate(source, event.get("summary"), url, event.get("description", ""),
                          now, metadata)
        if item:
            results.append(item)
    return results, []


def parse(source, response, now) -> CollectionResult:
    headers = {key.lower(): value for key, value in response.headers.items()}
    metadata = {"status_code": response.status_code,
                "etag": headers.get("etag"),
                "last_modified": headers.get("last-modified")}
    if response.status_code == 304:
        return CollectionResult(metadata=metadata, not_modified=True, parse_outcome="not_modified")
    if response.status_code != 200:
        return CollectionResult(metadata=metadata, parse_outcome=f"http_{response.status_code}")
    metadata["content_hash"] = hashlib.sha256(response.content).hexdigest()
    try:
        if source.type == "rss":
            candidates, links = _feed(source, response, now)
        elif source.type == "html":
            candidates, links = _html(source, response, now)
        elif source.type in {"api", "github"}:
            candidates, links = _records(source, response, now)
        else:
            candidates, links = _calendar(source, response, now)
    except (ValueError, TypeError, KeyError, AttributeError, SelectorSyntaxError) as exc:
        return CollectionResult(metadata=metadata, parse_outcome=f"parse_error:{type(exc).__name__}")
    outcome = "ok" if candidates else "empty"
    marker = source.options.get("empty_marker")
    if not candidates and isinstance(marker, str) and marker in response.text:
        outcome = "empty_expected"
    return CollectionResult(candidates=candidates[:_limit(source)], discovered_urls=links[:20],
                            metadata=metadata, parse_outcome=outcome)


def collect(source, http, now) -> CollectionResult:
    headers = {}
    if source.etag:
        headers["If-None-Match"] = source.etag
    if source.last_modified:
        headers["If-Modified-Since"] = source.last_modified
    url = source.url
    if source.adapter == "ctftime":
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        horizon = min(90, max(1, int(source.options.get("horizon_days", 30))))
        query.update(start=str(int(now.timestamp())),
                     finish=str(int((now + timedelta(days=horizon)).timestamp())))
        url = urlunsplit(parts._replace(query=urlencode(query)))
    response = http.get(url, headers=headers, rate_limit_seconds=source.rate_limit_seconds)
    result = parse(source, response, now)
    if result.not_modified:
        result.metadata["etag"] = result.metadata.get("etag") or source.etag
        result.metadata["last_modified"] = result.metadata.get("last_modified") or source.last_modified
        result.metadata["content_hash"] = source.content_hash
    return result

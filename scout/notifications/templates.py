"""Local, static Jinja templates. Collected content is never used as a template."""

import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import FileSystemLoader, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from scout.models import Opportunity, web_url

_DIR = Path(__file__).resolve().parent / "static"
_HTML = SandboxedEnvironment(loader=FileSystemLoader(_DIR), autoescape=True,
                             undefined=StrictUndefined)
_TEXT = SandboxedEnvironment(loader=FileSystemLoader(_DIR), autoescape=False,
                             undefined=StrictUndefined)
_KINDS = {"digest", "urgent", "update", "health", "test"}
_HEADINGS = {
    "digest": "Opportunity digest", "urgent": "Urgent opportunity alert",
    "update": "Opportunity update", "health": "Source health update", "test": "Delivery test",
}


def _clean(value: object, limit: int = 600) -> str:
    """Remove mail header controls (also useful for one-line body fields)."""
    return re.sub(r"\s+", " ", "".join(" " if unicodedata.category(c).startswith("C") else c
                                      for c in str(value))).strip()[:limit]


def _link(value: str | None) -> str | None:
    if not value:
        return None
    try:
        if any(c.isspace() or unicodedata.category(c).startswith("C") for c in value):
            return None
        return web_url(value)
    except (ValueError, TypeError):
        return None


def _time(value: datetime | None, zone: ZoneInfo) -> str:
    if value is None:
        return "Not confirmed"
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("notification times must be timezone-aware")
    utc = value.astimezone(UTC)
    local = value.astimezone(zone)
    return f"{local:%d %b %Y, %I:%M %p} {local.tzname()} / {utc:%d %b %Y, %H:%M} UTC"


def render_message(kind: str, opportunities: list[Opportunity], now: datetime,
                   timezone: str = "Asia/Kolkata", health: dict | None = None
                   ) -> tuple[str, str, str]:
    """Return (safe subject, plain alternative, escaped HTML); digest max 12 entries."""
    if kind not in _KINDS:
        raise ValueError("unknown notification kind")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    zone = ZoneInfo(timezone)
    items = opportunities[:12] if kind == "digest" else opportunities
    entries = []
    for item in items:
        verified = item.verification_status in {"official_verified", "corroborated"}
        entries.append({
            "title": _clean(item.title, 300), "source": _clean(item.source_name, 200),
            "deadline_type": _clean(item.deadline_type.replace("_", " ")),
            "deadline": _time(item.deadline_at, zone),
            "deadline_verified": "Verified" if item.deadline_verified else "Unverified",
            "last_checked": _time(item.verification_checked_at, zone),
            "verification": _clean(item.verification_status.replace("_", " ")),
            "pricing": _clean(item.price_status.replace("_", " ")),
            "eligibility": _clean(item.eligibility, 800),
            "reason": _clean(item.benefit_summary or item.summary, 800),
            "next_action": _clean(item.next_action, 800),
            "canonical": _link(item.canonical_url) if verified else None,
            "registration": _link(item.registration_url)
            if verified and item.registration_verified else None,
        })
    changes = []
    if health:
        for name, change in list(health.items())[:20]:
            changes.append(f"{_clean(name, 120)}: {_clean(change, 300)}")
    heading = _HEADINGS[kind]
    subject = _clean(f"Scout: {heading}" +
                     (f" ({len(entries)})" if entries and kind == "digest" else ""), 180)
    context = {"heading": heading, "items": entries, "changes": changes,
               "generated_at": _time(now, zone)}
    text = _TEXT.get_template(f"{kind}.txt.j2").render(**context).strip() + "\n"
    html = _HTML.get_template(f"{kind}.html.j2").render(**context).strip() + "\n"
    return subject, text, html

"""Alert only against evidenced, typed and live closing times."""

from datetime import datetime

from scout.models import Opportunity


def hours_remaining(opportunity: Opportunity, now: datetime) -> float | None:
    if (not opportunity.deadline_at or not opportunity.deadline_verified
        or opportunity.deadline_type in {"unknown", "event_start"}
        or opportunity.verification_status != "official_verified"
        or opportunity.availability_status in {"closed", "full", "expired", "withdrawn"}
        or opportunity.eligibility_status == "ineligible"):
        return None
    hours = (opportunity.deadline_at - now).total_seconds() / 3600
    return hours if hours > 0 else None


def alert_window(opportunity: Opportunity, now: datetime, windows: list[int]) -> int | None:
    """Choose one smallest crossed window; repeated notifications use history downstream."""
    remaining = hours_remaining(opportunity, now)
    if remaining is None:
        return None
    return min((window for window in set(windows) if window > 0 and remaining <= window), default=None)

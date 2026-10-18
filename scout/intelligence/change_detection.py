"""Changes that warrant a version bump and update notification."""

from scout.models import Opportunity

_MATERIAL = (
    "price_status", "benefit_summary", "deadline_at", "deadline_type", "deadline_verified",
    "eligibility_status", "india_eligible", "eligibility", "verification_status",
    "availability_status", "registration_url", "registration_verified", "start_at", "end_at",
    "student_verification_required",
)


def meaningful_changes(old: Opportunity, new: Opportunity) -> list[str]:
    """Ignore summaries, title cosmetics, scores, sightings and evidence formatting."""
    return [field for field in _MATERIAL if getattr(old, field) != getattr(new, field)]

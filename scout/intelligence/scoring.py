"""Eight independently normalized, configuration-weighted utility dimensions."""

import re
from datetime import datetime

from scout.config import Preferences, Scoring
from scout.intelligence.deadlines import hours_remaining
from scout.models import Opportunity, Source


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def score(opportunity: Opportunity, preferences: Preferences, scoring: Scoring, source: Source,
          now: datetime, feedback_signals=None) -> Opportunity:
    """Return a scored copy; scores are usefulness estimates, not claim verification."""
    item = opportunity.model_copy(deep=True)
    text = f"{item.title} {item.summary}".lower()
    category_weight = preferences.categories.get(item.category, 1.0)
    matches = sum(bool(re.search(rf"(?<!\w){re.escape(word.lower())}(?!\w)", text))
                  for word in preferences.keywords if word.strip())
    relevance = _clamp((55 + min(matches, 4) * 9) * category_weight)
    benefit = 35 if item.benefit_summary == "See source for requirements and benefits." else 65
    if re.search(r"\b(?:scholarship|grant|voucher|credits?)\b", item.benefit_summary, re.I):
        benefit = 80
    cost = {"verified_free": 100, "free_tier": 85, "conditional_free": 65,
            "free_trial": 45, "paid": 5, "unknown": 35}[item.price_status]
    eligibility = {"eligible": 100, "conditional": 65, "unknown": 35,
                   "ineligible": 0}[item.eligibility_status]
    claims = item.claim_evidence
    reliable = [claim for claim in claims if claim.reliable]
    evidence = _clamp(20 + 80 * len(reliable) / max(1, len(claims))) if claims else 0
    remaining = hours_remaining(item, now)
    urgency = (0 if remaining is None else 100 if remaining <= 6 else 85 if remaining <= 24
               else 65 if remaining <= 72 else 35 if remaining <= 168 else 10)
    age = max(0, (now - (item.published_at or item.first_seen_at)).total_seconds() / 86400)
    freshness = _clamp(100 - age * 3)
    trust = {"official": 75, "aggregator": 45, "community": 30, "unknown": 15}[source.trust]
    if source.validation_status != "validated":
        trust = min(trust, 25)
    reliability = (100 if item.verification_status == "official_verified" else
                   65 if item.verification_status == "corroborated" else trust)
    if item.verification_status in {"stale", "expired", "withdrawn"}:
        reliability = 0
    dimensions = {"relevance": relevance, "benefit": benefit, "cost": cost,
                  "eligibility": eligibility, "evidence": evidence, "urgency": urgency,
                  "freshness": freshness, "reliability": reliability}
    item.score_breakdown = {key: round(_clamp(value), 2) for key, value in dimensions.items()}
    item.relevance_score = round(relevance, 2)
    item.urgency_score = round(urgency, 2)
    total = sum(dimensions[key] * weight for key, weight in scoring.weights.items()) / sum(scoring.weights.values())
    if item.price_status == "paid" and not preferences.allow_paid:
        total *= 0.35
    if item.eligibility_status == "ineligible":
        total *= 0.1
    if item.availability_status in {"closed", "full", "expired", "withdrawn"} or item.verification_status in {
        "stale", "expired", "withdrawn"
    }:
        total *= 0.1
    if item.feedback in {"ignore", "irrelevant", "outdated"}:
        total *= 0.2
    elif item.feedback == "save":
        total += 5
    if feedback_signals:
        # Explicit user preference signal, never a claim-confidence boost.
        signal = feedback_signals.get(item.category, 0) if isinstance(feedback_signals, dict) else 0
        if isinstance(signal, (int, float)):
            total += max(-10, min(10, signal))
    item.total_score = round(_clamp(total), 2)
    # Confidence is sourced from evidence/authority and claim consistency alone.
    item.confidence_score = round(_clamp(
        (50 * len(reliable) / max(1, len(claims)) if claims else 0)
        + {"official_verified": 45, "corroborated": 30, "community_reported": 12,
           "unverified": 0, "stale": 0, "expired": 0, "withdrawn": 0}[item.verification_status]
        + (5 if item.deadline_verified else 0)
    ), 2)
    return item

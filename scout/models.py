"""Validated contracts. Web text is data; it can never configure tools or trust."""

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def web_url(value: str) -> str:
    if any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("unsafe URL characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("absolute HTTP(S) URL required")
    if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise ValueError("credentials and non-web ports are prohibited")
    return value


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("*", mode="after")
    @classmethod
    def aware_timestamps(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamp must include an explicit timezone")
            return value.astimezone(UTC)
        return value


class Source(Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,79}$")
    name: str = Field(min_length=1, max_length=200)
    url: str
    type: Literal["rss", "html", "api", "github", "calendar"]
    adapter: str = "generic"
    organization: str = "Unknown publisher"
    categories: list[str] = Field(min_length=1)
    trust: Literal["official", "aggregator", "community", "unknown"] = "unknown"
    official_domains: list[str] = Field(default_factory=list)
    enabled: bool = False
    validation_status: Literal["validated", "probation", "quarantined", "rejected", "suspended"] = (
        "probation"
    )
    validated_at: datetime | None = None
    validation_note: str = "Not validated"
    scan_interval_hours: float = Field(default=6, ge=0.5, le=168)
    min_interval_hours: float = Field(default=3, ge=0.5, le=168)
    max_interval_hours: float = Field(default=168, ge=0.5, le=720)
    rate_limit_seconds: float = Field(default=2, ge=0, le=120)
    selectors: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    expected_coverage: str = "Publisher announcements only"
    discovery_origin: str = "configured"
    discovered_at: datetime = Field(default_factory=utcnow)
    auto_enabled: bool = False
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_meaningful_update: datetime | None = None
    failure_count: int = Field(default=0, ge=0)
    failure_history: list[dict[str, Any]] = Field(default_factory=list)
    last_error: str | None = None
    suspended_until: datetime | None = None
    probation_successes: int = Field(default=0, ge=0)
    usefulness: float = Field(default=0.5, ge=0, le=1)
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None

    _url = field_validator("url")(web_url)

    @model_validator(mode="after")
    def intervals_and_trust(self):
        if not self.min_interval_hours <= self.scan_interval_hours <= self.max_interval_hours:
            raise ValueError("min <= scan <= max interval required")
        if self.trust == "official" and not self.official_domains:
            raise ValueError("official source requires explicitly configured publisher domains")
        return self


class Candidate(Model):
    source_id: str
    title: str = Field(min_length=1, max_length=500)
    url: str
    text: str = Field(default="", max_length=40000)
    published_at: datetime | None = None
    collected_at: datetime = Field(default_factory=utcnow)
    links: list[str] = Field(default_factory=list)
    discovery_method: str = "configured_source"
    metadata: dict[str, Any] = Field(default_factory=dict)

    _url = field_validator("url")(web_url)


class FetchResult(Model):
    url: str
    status_code: int
    headers: dict[str, str] = Field(default_factory=dict)
    content: bytes = b""
    fetched_at: datetime = Field(default_factory=utcnow)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class CollectionResult(Model):
    candidates: list[Candidate] = Field(default_factory=list)
    discovered_urls: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    not_modified: bool = False
    parse_outcome: str = "ok"


class ClaimEvidence(Model):
    claim: str
    value: str
    url: str
    excerpt: str = Field(max_length=1000)
    checked_at: datetime
    authority: Literal["official", "aggregator", "community", "unknown"] = "unknown"
    reliable: bool = False

    _url = field_validator("url")(web_url)


class Opportunity(Model):
    id: str
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=2000)
    canonical_url: str
    source_url: str
    source_name: str
    source_type: str
    source_id: str
    category: str
    tags: list[str] = Field(default_factory=list)
    opportunity_type: str = "announcement"
    discovery_method: str = "configured_source"
    published_at: datetime | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    last_changed_at: datetime = Field(default_factory=utcnow)
    start_at: datetime | None = None
    end_at: datetime | None = None
    deadline_at: datetime | None = None
    deadline_type: Literal[
        "registration", "offer_expiry", "event_start", "application", "credit_redemption",
        "submission", "unknown",
    ] = "unknown"
    deadline_verified: bool = False
    date_notes: list[str] = Field(default_factory=list)
    eligibility: str = "Eligibility for Indian students could not be confirmed."
    eligibility_status: Literal["eligible", "ineligible", "conditional", "unknown"] = "unknown"
    india_eligible: bool | None = None
    student_verification_required: bool | None = None
    price_status: Literal[
        "verified_free", "free_tier", "conditional_free", "free_trial", "paid", "unknown",
    ] = "unknown"
    benefit_summary: str = "See source for requirements and benefits."
    availability_status: Literal[
        "open", "upcoming", "closed", "full", "expired", "withdrawn", "unknown",
    ] = "unknown"
    verification_status: Literal[
        "official_verified", "corroborated", "community_reported", "unverified", "stale",
        "expired", "withdrawn",
    ] = "unverified"
    verification_checked_at: datetime | None = None
    evidence_urls: list[str] = Field(default_factory=list)
    claim_evidence: list[ClaimEvidence] = Field(default_factory=list)
    confidence_score: float = Field(default=0, ge=0, le=100)
    relevance_score: float = Field(default=0, ge=0, le=100)
    urgency_score: float = Field(default=0, ge=0, le=100)
    total_score: float = Field(default=0, ge=0, le=100)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    content_hash: str = ""
    notification_history: list[dict[str, Any]] = Field(default_factory=list)
    record_version: int = Field(default=1, ge=1)
    organizer: str | None = None
    registration_url: str | None = None
    registration_verified: bool = False
    next_action: str = "Read the original announcement and confirm eligibility before applying."
    related_ids: list[str] = Field(default_factory=list)
    baseline: bool = False
    feedback: Literal["save", "ignore", "applied", "irrelevant", "outdated"] | None = None

    _urls = field_validator("canonical_url", "source_url")(web_url)


class Notification(Model):
    id: str
    idempotency_key: str
    kind: Literal["digest", "urgent", "update", "health", "test"]
    opportunity_ids: list[str] = Field(default_factory=list)
    subject: str
    text_body: str
    html_body: str
    status: Literal["pending", "sending", "sent", "retryable_failure", "permanent_failure"] = (
        "pending"
    )
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    next_attempt_at: datetime | None = None
    sent_at: datetime | None = None
    attempts: int = Field(default=0, ge=0)
    last_error: str | None = None
    message_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunMetrics(Model):
    id: str
    started_at: datetime
    finished_at: datetime | None = None
    mode: str = "scan"
    sources_planned: int = 0
    sources_ok: int = 0
    sources_failed: int = 0
    candidates: int = 0
    new_opportunities: int = 0
    updated_opportunities: int = 0
    discovered_sources: int = 0
    http_requests: int = 0
    emails_sent: int = 0
    ai_calls: int = 0
    ai_tokens: int = 0
    stop_reason: str = "completed"
    warnings: list[str] = Field(default_factory=list)

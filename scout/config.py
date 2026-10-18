"""Strict local configuration, kept separate from untrusted collected content."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import Field, field_validator

from scout.models import Model, Source


class Limits(Model):
    max_seconds: float = Field(default=90, gt=0, le=240)
    max_requests: int = Field(default=80, ge=1, le=500)
    max_candidates: int = Field(default=120, ge=1, le=1000)
    max_new_sources: int = Field(default=3, ge=0, le=20)
    max_ai_calls: int = Field(default=0, ge=0, le=0)
    max_ai_tokens: int = Field(default=0, ge=0, le=0)
    max_response_bytes: int = Field(default=2097152, ge=1024, le=8388608)
    max_emails_per_run: int = Field(default=5, ge=1, le=10)
    max_emails_per_day: int = Field(default=10, ge=1, le=50)
    max_opportunities: int = Field(default=1000, ge=10, le=10000)
    retention_days: int = Field(default=180, ge=7, le=730)


class Preferences(Model):
    timezone: str = "Asia/Kolkata"
    country: str = "India"
    region: str = "Kerala"
    student: bool = True
    categories: dict[str, float] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list)
    min_digest_score: float = Field(default=50, ge=0, le=100)
    min_urgent_score: float = Field(default=65, ge=0, le=100)
    min_urgent_confidence: float = Field(default=75, ge=0, le=100)
    alert_windows_hours: list[int] = Field(default_factory=lambda: [24, 6, 1])
    max_digest_items: int = Field(default=12, ge=1, le=25)
    allow_paid: bool = False
    auto_enable_trusted_sources: bool = True
    limits: Limits = Field(default_factory=Limits)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        ZoneInfo(value)
        return value

    @field_validator("categories")
    @classmethod
    def bounded_categories(cls, value):
        if any(not 0.1 <= weight <= 2 for weight in value.values()):
            raise ValueError("category importance must remain between 0.1 and 2")
        return value

    @field_validator("alert_windows_hours")
    @classmethod
    def bounded_windows(cls, value):
        if not value or len(value) > 5 or any(not 1 <= n <= 168 for n in value):
            raise ValueError("one to five alert windows between 1 and 168 hours required")
        return sorted(set(value), reverse=True)


class Scoring(Model):
    weights: dict[str, float] = Field(default_factory=lambda: {
        "relevance": 25, "benefit": 10, "cost": 15, "eligibility": 10,
        "evidence": 15, "urgency": 10, "freshness": 5, "reliability": 10,
    })

    @field_validator("weights")
    @classmethod
    def valid_weights(cls, value):
        keys = {"relevance", "benefit", "cost", "eligibility", "evidence", "urgency",
                "freshness", "reliability"}
        if set(value) != keys or any(w < 0 for w in value.values()) or sum(value.values()) <= 0:
            raise ValueError("all eight non-negative scoring weights are required")
        return value


def read_yaml(path: Path):
    if path.stat().st_size > 524288:
        raise ValueError("configuration exceeds 512 KiB")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class Configuration:
    def __init__(self, directory: str | Path = "config"):
        self.directory = Path(directory)
        data = read_yaml(self.directory / "sources.yaml")
        if not isinstance(data, dict) or set(data) != {"sources"}:
            raise ValueError("sources.yaml must contain only a sources list")
        self.sources = [Source.model_validate(item) for item in data["sources"]]
        if len({s.id for s in self.sources}) != len(self.sources):
            raise ValueError("duplicate source IDs")
        self.preferences = Preferences.model_validate(read_yaml(self.directory / "preferences.yaml"))
        if os.getenv("TIMEZONE"):
            self.preferences.timezone = os.environ["TIMEZONE"]
        self.scoring = Scoring.model_validate(read_yaml(self.directory / "scoring.yaml"))
        taxonomy = read_yaml(self.directory / "categories.yaml")
        if not isinstance(taxonomy, dict) or not taxonomy:
            raise ValueError("non-empty category taxonomy required")
        if any(not isinstance(k, str) or not isinstance(v, list) or
               any(not isinstance(word, str) for word in v) for k, v in taxonomy.items()):
            raise ValueError("taxonomy maps category names to lists of keywords")
        self.taxonomy: dict[str, list[str]] = taxonomy
        for source in self.sources:
            if set(source.categories) - set(taxonomy):
                raise ValueError(f"unknown categories for {source.id}")


def load_local_env(path: Path = Path(".env")) -> None:
    """Simple KEY=value file, no shell expansion/execution; process env wins."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.replace("_", "").isalnum() or not key[0].isalpha():
            raise ValueError("invalid .env entry; use KEY=value")
        os.environ.setdefault(key, value.strip().strip("\"'"))

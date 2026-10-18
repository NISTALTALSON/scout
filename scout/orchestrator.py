"""A bounded agent cycle: plan, collect, evaluate, remember, monitor and notify."""

import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from scout.budgets import Budget, BudgetExceeded
from scout.collectors import collect
from scout.config import Configuration, read_yaml
from scout.discovery.planner import plan_sources
from scout.discovery.query_engine import run_queries
from scout.discovery.source_discovery import discover_sources
from scout.http import FetchError, SafeHttpClient
from scout.intelligence.deadlines import alert_window
from scout.intelligence.deduplication import find_duplicate, merge_opportunities
from scout.intelligence.extraction import extract
from scout.intelligence.scoring import score
from scout.intelligence.verification import verify
from scout.models import RunMetrics, Source, utcnow
from scout.notifications.outbox import Outbox, make_notification
from scout.reporting.health import health_report
from scout.urls import is_official_url


def make_budget(configuration):
    limits = configuration.preferences.limits
    return Budget(**{key: getattr(limits, key) for key in (
        "max_seconds", "max_requests", "max_candidates", "max_new_sources",
        "max_ai_calls", "max_ai_tokens")})


class Scout:
    def __init__(self, store, configuration: Configuration, http=None, budget=None,
                 sender=None, checkpoint=None, now=None):
        self.store, self.configuration = store, configuration
        self.now = now or utcnow()
        self.preferences = configuration.preferences.model_copy(deep=True)
        for category, importance in store.get_meta("category_importance", {}).items():
            if category in self.configuration.taxonomy and 0.1 <= importance <= 2:
                self.preferences.categories = {**self.preferences.categories, category: importance}
        self.budget = budget or make_budget(configuration)
        self._owns_http = http is None
        self.http = http or SafeHttpClient(self.budget,
            max_response_bytes=self.preferences.limits.max_response_bytes)
        self.sender, self.checkpoint = sender, checkpoint
        self._source_changes = []
        self._seed_sources()

    def close(self):
        if self._owns_http:
            self.http.close()

    def _seed_sources(self):
        existing = {s.id: s for s in self.store.list_sources()}
        fields = ("name", "url", "type", "adapter", "organization", "categories", "trust",
                  "official_domains", "enabled", "scan_interval_hours", "min_interval_hours",
                  "max_interval_hours", "rate_limit_seconds", "selectors", "options", "expected_coverage")
        for configured in self.configuration.sources:
            if configured.id not in existing:
                self.store.upsert_source(configured)
                continue
            old = existing[configured.id]
            # Config is authoritative; a source cannot grant itself domains or new tools.
            data = old.model_dump()
            data.update({key: getattr(configured, key) for key in fields})
            if old.url != configured.url:
                data.update(last_success_at=None, last_attempt_at=None, etag=None,
                            last_modified=None, content_hash=None, validation_status="probation")
            if not configured.enabled and configured.validation_status in {"quarantined", "rejected"}:
                data["validation_status"] = configured.validation_status
            self.store.upsert_source(Source.model_validate(data))

    def _authority(self, item, source):
        for publisher in self.store.list_sources():
            if (publisher.trust == "official" and publisher.validation_status == "validated"
                    and is_official_url(item.canonical_url, publisher.official_domains)):
                return publisher
        return source

    def _feedback_signals(self):
        signals = {}
        for item in self.store.list_opportunities():
            amount = 1 if item.feedback == "save" else -1 if item.feedback == "irrelevant" else 0
            signals[item.category] = max(-5, min(5, signals.get(item.category, 0) + amount))
        return signals

    def _score(self, item, source):
        return score(item, self.preferences, self.configuration.scoring, source, self.now,
                     self._feedback_signals())

    def _eligible(self, item, urgent=False):
        threshold = self.preferences.min_urgent_score if urgent else self.preferences.min_digest_score
        return (item.total_score >= threshold and item.feedback not in {"ignore", "irrelevant", "outdated", "applied"}
                and item.eligibility_status != "ineligible"
                and (self.preferences.allow_paid or item.price_status != "paid")
                and item.availability_status not in {"closed", "full", "expired", "withdrawn"}
                and item.verification_status not in {"stale", "expired", "withdrawn"})

    def _queue_update(self, item, changes, baseline):
        major = {"deadline_at", "price_status", "eligibility_status", "india_eligible",
                 "verification_status", "availability_status", "start_at", "end_at"}
        if (baseline or not changes or not (set(changes) & major)
                or item.feedback in {"ignore", "irrelevant", "outdated", "applied"}):
            return
        # Corrections/withdrawals of previously reported items are useful even when
        # the corrected opportunity no longer passes the relevance threshold.
        if not self._eligible(item) and not item.notification_history:
            return
        note = make_notification("update", [item], self.now,
            f"update:{item.id}:{item.record_version}", self.preferences.timezone,
            health={"Changed fields": ", ".join(changes)})
        self.store.enqueue(note)

    def _remember(self, item, source, baseline):
        old = find_duplicate(item, self.store.list_opportunities())
        changes = []
        if old:
            item, changes = merge_opportunities(old, item, self.now)
        else:
            item.baseline = baseline
        item = self._score(item, source)
        self.store.upsert_opportunity(item)
        self._queue_update(item, changes, baseline)
        return item, old is None, changes

    def _fail_source(self, source, error):
        source.last_attempt_at = self.now
        source.failure_count += 1
        source.last_error = str(error) if isinstance(error, FetchError) else type(error).__name__
        source.failure_history = [*source.failure_history[-9:], {
            "at": self.now.isoformat(), "reason": source.last_error}]
        seconds = max(getattr(error, "backoff_seconds", 0),
                      min(168 * 3600, source.scan_interval_hours * 3600 * 2 ** min(6, source.failure_count)))
        source.suspended_until = self.now + timedelta(seconds=seconds)
        if source.failure_count >= 5:
            source.validation_status = "suspended"
        self.store.upsert_source(source)

    def _success(self, source, result, useful):
        source.last_attempt_at = source.last_success_at = self.now
        source.failure_count, source.last_error, source.suspended_until = 0, None, None
        source.last_response_status = result.metadata.get("status_code")
        source.last_parse_outcome = result.parse_outcome
        if source.content_hash != result.metadata.get("content_hash"):
            source.last_meaningful_update = self.now
        source.content_hash = result.metadata.get("content_hash") or source.content_hash
        source.etag, source.last_modified = result.metadata.get("etag"), result.metadata.get("last_modified")
        source.last_opportunity_count = useful
        source.usefulness = round(source.usefulness * 0.8 + min(1, useful / 4) * 0.2, 4)
        if source.validation_status in {"probation", "suspended"} or source.validated_at is None:
            source.validation_status = "validated"
            source.validated_at = self.now
            source.validation_note = f"Scout collector passed: {result.parse_outcome}"
            self._source_changes.append(f"{source.id}: collector validated (not blanket claim verification)")
        self.store.upsert_source(source)

    def refresh(self, metrics, limit=6):
        """Persisted urgency queue, ordered by next deadline and verification age."""
        sources = {s.id: s for s in self.store.list_sources()}
        items = sorted(self.store.list_opportunities(), key=lambda item: (
            item.deadline_at or datetime.max.replace(tzinfo=UTC),
            item.verification_checked_at or datetime.min.replace(tzinfo=UTC)))
        count = 0
        for old in items:
            self.budget.check()
            source = sources.get(old.source_id)
            if (not source or not source.enabled or source.validation_status != "validated"
                    or old.feedback in {"ignore", "irrelevant", "outdated", "applied"}):
                continue
            imminent = bool(old.deadline_at and self.now < old.deadline_at <= self.now + timedelta(days=7))
            interval = 3 if imminent else 24
            if old.verification_checked_at and old.verification_checked_at > self.now - timedelta(hours=interval):
                continue
            if count >= limit:
                break
            checked = verify(old, self._authority(old, source), self.now, self.http)
            _, _, changes = self._remember(checked, source, baseline=False)
            metrics.updated_opportunities += bool(changes)
            count += 1
        return count

    def _discover(self, urls, origin, metrics):
        if not urls or self.budget.new_sources >= self.budget.max_new_sources:
            return []
        found = discover_sources(urls, origin, self.store.list_sources(), self.http,
                                 self.budget, self.now,
                                 auto_enable=self.preferences.auto_enable_trusted_sources)
        for source in found:
            if source.categories == ["general"]:
                source.categories = ["additional"]
            self.store.upsert_source(source)
            self._source_changes.append(f"{source.id}: {source.validation_status}; {source.validation_note}")
        metrics.discovered_sources += len(found)
        return found

    def scan(self, category=None, urgent_only=False, notify_historical=False,
             discover=True, verify_only=False):
        start = time.monotonic()
        metrics = RunMetrics(id=uuid.uuid4().hex, started_at=self.now,
                             mode="verify" if verify_only else "scan")
        try:
            self.refresh(metrics)
            sources = [] if verify_only else plan_sources(self.store.list_sources(), self.now,
                category=category, urgent_only=urgent_only, opportunities=self.store.list_opportunities())
            metrics.sources_planned = len(sources)
            for source in sources:
                self.budget.check()
                baseline = not notify_historical and source.last_success_at is None
                try:
                    result = collect(source, self.http, self.now)
                    if result.parse_outcome not in {"ok", "not_modified", "empty_expected"}:
                        source.last_parse_outcome = result.parse_outcome
                        source.last_response_status = result.metadata.get("status_code")
                        raise FetchError("collector " + result.parse_outcome)
                    # Collector validation precedes first-party evidence use, but does
                    # not assert that any field on an opportunity has been verified.
                    if source.validation_status in {"probation", "suspended"}:
                        source.validation_status = "validated"
                    useful, links = 0, list(result.discovered_urls)
                    for candidate in result.candidates:
                        self.budget.consume_candidate()
                        item = extract(candidate, source, self.now, self.configuration.taxonomy)
                        if item is None:
                            continue
                        checked = verify(item, self._authority(item, source), self.now, self.http)
                        saved, is_new, changes = self._remember(checked, source, baseline)
                        useful += 1
                        metrics.new_opportunities += is_new
                        metrics.updated_opportunities += bool(changes)
                        links.extend(candidate.links)
                        links.append(saved.canonical_url)
                    # Validators advance only after all candidates were processed.
                    # A budget interruption leaves this source due; no lost 304 batch.
                    self._success(source, result, useful)
                    metrics.sources_ok += 1
                    if discover and not urgent_only:
                        self._discover(list(dict.fromkeys(links))[:20], source, metrics)
                except BudgetExceeded:
                    raise
                except (FetchError, ValueError, TypeError, OSError) as error:
                    self._fail_source(source, error)
                    metrics.sources_failed += 1
            if discover and not urgent_only and not verify_only:
                self._search_directories(metrics)
        except BudgetExceeded as error:
            metrics.stop_reason = str(error)
            metrics.warnings.append("Partial coverage saved; unprocessed sources remain due.")
        finally:
            local = self.now.astimezone(ZoneInfo(self.preferences.timezone))
            if self.budget.remaining_seconds > 0:
                if (not verify_only and not urgent_only and local.hour == 9 and local.minute < 30):
                    self.digest(include_historical=notify_historical)
                self.queue_alerts(notify_historical=notify_historical)
            else:
                metrics.warnings.append("Notification queueing deferred because the cycle duration budget was exhausted.")
            metrics.candidates, metrics.http_requests = self.budget.candidates, self.budget.requests
            metrics.finished_at = utcnow()
            metrics.duration_seconds = round(time.monotonic() - start, 3)
            self.store.record_run(metrics)
            if metrics.sources_ok:
                self.store.set_meta("baseline_complete", True)
            existing = self.store.get_meta("source_changes", [])
            self.store.set_meta("source_changes", (existing + self._source_changes)[-50:])
        return metrics

    def _search_directories(self, metrics):
        path = self.configuration.directory / "discovery.yaml"
        if not path.exists():
            return
        options = read_yaml(path)
        if not options.get("enabled", False):
            return
        previous = self.store.get_meta("last_directory_query")
        hours = max(24, min(168, float(options.get("interval_hours", 24))))
        if previous and self.now - datetime.fromisoformat(previous) < timedelta(hours=hours):
            return
        self.store.set_meta("last_directory_query", self.now.isoformat())
        try:
            urls = run_queries(self.http, self.budget, self.now, options.get("queries"))
            self._discover(urls, "GitHub repository search", metrics)
        except (FetchError, ValueError, TypeError, OSError):
            metrics.warnings.append("Directory discovery unavailable; configured-source coverage continues.")

    def queue_alerts(self, notify_historical=False):
        queued = 0
        for item in self.store.list_opportunities():
            if (item.baseline and not notify_historical) or not self._eligible(item, urgent=True):
                continue
            if item.confidence_score < self.preferences.min_urgent_confidence:
                continue
            if (not item.verification_checked_at or
                    self.now - item.verification_checked_at > timedelta(hours=24)):
                continue
            window = alert_window(item, self.now, self.preferences.alert_windows_hours)
            if window is not None:
                key = f"deadline:{item.id}:{item.deadline_at.isoformat()}:{item.deadline_type}:{window}"
                note = make_notification("urgent", [item], self.now, key, self.preferences.timezone,
                    health={"Alert": f"Within the {window}-hour {item.deadline_type} alert window"})
                note.metadata = {**note.metadata, "alert_at": item.deadline_at.isoformat()}
                queued += self.store.enqueue(note)
            elif (item.verification_status == "official_verified" and item.price_status == "verified_free"
                  and item.total_score >= 85 and item.published_at
                  and timedelta(0) <= self.now - item.published_at <= timedelta(hours=12)
                  and re.search(r"\b(?:promotion|temporarily|free access)\b", item.summary, re.I)):
                # No invented countdown: this policy alerts on newly announced access.
                note = make_notification("urgent", [item], self.now, f"new-access:{item.id}",
                    self.preferences.timezone,
                    health={"Alert": "New high-value access announcement; expiry may be unknown"})
                queued += self.store.enqueue(note)
        return queued

    def digest(self, include_historical=False):
        day = self.now.astimezone(ZoneInfo(self.preferences.timezone)).date().isoformat()
        previous = self.store.get_meta("last_digest_queued_at")
        since = datetime.fromisoformat(previous) if previous else datetime.min.replace(tzinfo=UTC)
        items = [item for item in self.store.list_opportunities() if self._eligible(item)
                 and (include_historical or not item.baseline or item.record_version > 1)
                 and (include_historical or max(item.first_seen_at, item.last_changed_at) > since)]
        items.sort(key=lambda item: (-item.total_score, item.deadline_at or datetime.max.replace(tzinfo=UTC)))
        report = health_report(self.store, self.now)
        health = {"Coverage": f"{report['enabled']} enabled sources; {len(report['failing'])} failing",
                  "Local date": day + " " + self.preferences.timezone,
                  "Coverage gaps": "Public configured sources and linked directories, not universal web search"}
        changes = self.store.get_meta("source_changes", [])
        if changes:
            health["Registry changes"] = "; ".join(changes[-5:])
        if report["missed_run_warning"]:
            health["Warning"] = "An expected scan may have been missed; this is a next-run detection."
        note = make_notification("digest", items[:self.preferences.max_digest_items], self.now,
                                 f"digest:{day}", self.preferences.timezone, health)
        if self.store.enqueue(note):
            self.store.set_meta("last_digest_queued_at", self.now.isoformat())
            self.store.set_meta("source_changes", [])
        return note

    def weekly_health(self):
        local = self.now.astimezone(ZoneInfo(self.preferences.timezone))
        if local.weekday() != 0:
            return False
        week = local.strftime("%G-W%V")
        report = health_report(self.store, self.now)
        note = make_notification("health", [], self.now, f"health:{week}", self.preferences.timezone,
            health={"Enabled sources": report["enabled"], "Failing sources": len(report["failing"]),
                    "Overdue coverage": ", ".join(report["overdue_sources"][:10]) or "None",
                    "Pending notifications": report["outbox"].get("pending", 0),
                    "Missed run warning": report["missed_run_warning"]})
        return self.store.enqueue(note)

    def deliver(self, dry_run=False):
        # Superseded deadline/update payloads must not send obsolete claims on retry.
        for note in self.store.list_notifications():
            if note.status not in {"pending", "retryable_failure"} or note.kind not in {"urgent", "update"}:
                continue
            obsolete = False
            for snapshot in note.metadata.get("records", []):
                item = self.store.get_opportunity(snapshot["id"])
                if item is None or item.record_version != snapshot["record_version"]:
                    obsolete = True
                if item and note.kind == "urgent" and not self._eligible(item, urgent=True):
                    obsolete = True
            alert_at = note.metadata.get("alert_at")
            obsolete |= bool(alert_at and datetime.fromisoformat(alert_at) <= self.now)
            if obsolete and not dry_run:
                note.status, note.last_error = "permanent_failure", "Superseded or no longer actionable"
                note.updated_at = self.now
                self.store.update_notification(note)
        if self.sender is None and not dry_run:
            return {"sent": 0, "failed": 0, "pending": sum(
                n.status in {"pending", "sending", "retryable_failure"}
                for n in self.store.list_notifications()), "disabled": True}
        limits = self.preferences.limits
        return Outbox(self.store, self.sender).deliver(self.now,
            limit=limits.max_emails_per_run, daily_limit=limits.max_emails_per_day,
            timezone=self.preferences.timezone, dry_run=dry_run, checkpoint=self.checkpoint)

    def maintain(self):
        limits = self.preferences.limits
        result = self.store.prune(self.now, limits.retention_days, limits.max_opportunities)
        weekly = self.weekly_health()
        result = {**result, "weekly_health_queued": bool(weekly)}
        self.store.set_meta("last_maintenance", {**result, "at": self.now.isoformat()})
        return result


def opportunity_report(store, now, limit=25):
    rows = sorted(store.list_opportunities(), key=lambda item: -item.total_score)[:limit]
    return {"generated_at": now.isoformat(), "opportunities": [item.model_dump(mode="json") for item in rows],
            "health": health_report(store, now)}

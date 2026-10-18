"""Persist a sending intent before SMTP; retries are at-least-once after lease expiry.

An uncertain DATA acceptance or crash while sending waits at least 30 minutes before
retrying. Stable Message-ID helps receivers deduplicate but SMTP cannot guarantee
exactly-once delivery. The checkpoint callback must durably sync external state.
"""

import hashlib
import json
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from scout.models import Notification, Opportunity
from scout.notifications.email_sender import DeliveryError
from scout.notifications.interfaces import NotificationSender, NotificationStore
from scout.notifications.templates import render_message

_LEASE = timedelta(minutes=30)


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def make_notification(kind: str, opportunities: list[Opportunity], now: datetime,
                      key: str, timezone: str = "Asia/Kolkata", health: dict | None = None
                      ) -> Notification:
    """Stable key and Message-ID. Urgent identity includes record/deadline snapshot."""
    now = _aware(now)
    if not key or any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029"
                      for c in key):
        raise ValueError("nonempty, control-free idempotency key required")
    selected = opportunities[:12] if kind == "digest" else opportunities
    records = [{"id": item.id, "record_version": item.record_version,
                "deadline_at": item.deadline_at.isoformat() if item.deadline_at else None,
                "deadline_type": item.deadline_type} for item in selected]
    identity = {"kind": kind, "key": key}
    if kind == "urgent":
        identity["records"] = sorted(records, key=lambda record: record["id"])
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    subject, text, html = render_message(kind, selected, now, timezone, health)
    return Notification(id=digest, idempotency_key=digest, kind=kind,
                        opportunity_ids=[item.id for item in selected],
                        subject=subject, text_body=text, html_body=html,
                        created_at=now, updated_at=now,
                        message_id=f"<scout-{digest}@scout.invalid>",
                        metadata={"records": records, "caller_key": key})


class Outbox:
    def __init__(self, store: NotificationStore, sender: NotificationSender,
                 max_attempts: int = 4):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.store = store
        self.sender = sender
        self.max_attempts = max_attempts

    def deliver(self, now: datetime, limit: int = 5, daily_limit: int = 10,
                timezone: str = "Asia/Kolkata", dry_run: bool = False,
                checkpoint: Callable[[], None] | None = None) -> dict:
        now = _aware(now)
        if limit < 0 or daily_limit < 0:
            raise ValueError("limits must be nonnegative")
        zone = ZoneInfo(timezone)
        all_records = self.store.list_notifications()
        # Include expired sending intents even if a store's due query excludes them.
        due = self.store.due_notifications(now, limit=max(20, limit * 4))
        stale = [record for record in all_records if record.status == "sending"
                 and record.updated_at <= now - _LEASE]
        candidates = {record.idempotency_key: record for record in [*due, *stale]
                      if record.status in {"pending", "retryable_failure", "sending"}
                      and (record.status != "sending" or record.updated_at <= now - _LEASE)
                      and (record.next_attempt_at is None or record.next_attempt_at <= now)}
        ordered = sorted(candidates.values(), key=lambda record:
                         (record.next_attempt_at or record.created_at, record.created_at,
                          record.idempotency_key))
        day = now.astimezone(zone).date()
        attempts_today = sum(
            1 for record in all_records
            for stamp in record.metadata.get("attempt_history", [])
            if datetime.fromisoformat(stamp).astimezone(zone).date() == day
        )
        # Older records may have been sent before attempt history was introduced.
        attempts_today += sum(1 for record in all_records if not record.metadata.get("attempt_history")
                              and record.sent_at and record.sent_at.astimezone(zone).date() == day)
        available = min(limit, max(0, daily_limit - attempts_today))
        selected = ordered[:available]
        if dry_run:
            return {"sent": 0, "failed": 0,
                    "pending": sum(record.status in {"pending", "sending", "retryable_failure"}
                                   for record in all_records), "dry_run": True,
                    "previews": [{"id": record.id, "subject": record.subject,
                                  "text": record.text_body, "html": record.html_body}
                                 for record in selected]}

        sent = failed = 0
        for record in selected:
            if record.attempts >= self.max_attempts:
                record.status = "permanent_failure"
                record.next_attempt_at = None
                record.last_error = "Maximum SMTP attempts reached"
                record.updated_at = now
                self.store.update_notification(record)
                if checkpoint is not None:
                    checkpoint()
                failed += 1
                continue
            record.status = "sending"
            record.updated_at = now
            record.attempts += 1
            record.next_attempt_at = None
            record.metadata = {**record.metadata,
                               "attempt_history": [*record.metadata.get("attempt_history", []),
                                                   now.isoformat()]}
            self.store.update_notification(record)
            if checkpoint is not None:
                checkpoint()  # If this fails, stop: never issue SMTP on unsynced intent.
            try:
                self.sender.send(record)
            except DeliveryError as exc:
                record.last_error = ("SMTP acceptance uncertain; " if exc.uncertain else "") + str(exc)
                record.metadata = {**record.metadata, "acceptance_uncertain": exc.uncertain}
                if exc.retryable and record.attempts < self.max_attempts:
                    record.status = "retryable_failure"
                    delay = timedelta(minutes=min(1440, 5 * 2 ** (record.attempts - 1)))
                    if exc.uncertain:
                        delay = max(delay, _LEASE)
                    record.next_attempt_at = now + delay
                else:
                    record.status = "permanent_failure"
                    record.next_attempt_at = None
                failed += 1
            else:
                record.status = "sent"
                record.sent_at = now
                record.next_attempt_at = None
                record.last_error = None
                record.metadata = {**record.metadata, "acceptance_uncertain": False}
                sent += 1
            record.updated_at = now
            self.store.update_notification(record)
            if record.status == "sent":
                for opportunity_id in record.opportunity_ids:
                    item = self.store.get_opportunity(opportunity_id)
                    if item is not None and not any(
                        entry.get("notification_key") == record.idempotency_key
                        for entry in item.notification_history
                    ):
                        item.notification_history = [*item.notification_history,
                                                     {"notification_key": record.idempotency_key,
                                                      "kind": record.kind,
                                                      "sent_at": now.isoformat()}]
                        self.store.upsert_opportunity(item)
            if checkpoint is not None:
                checkpoint()
        return {"sent": sent, "failed": failed,
                "pending": sum(record.status in {"pending", "sending", "retryable_failure"}
                               for record in self.store.list_notifications()), "dry_run": False}

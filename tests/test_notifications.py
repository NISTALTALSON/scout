"""Offline checks for rendering, TLS transport and restart-safe outbox semantics."""

from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from unittest.mock import patch

import pytest

from scout.models import Opportunity
from scout.notifications.email_sender import DeliveryError, SmtpSender
from scout.notifications.outbox import Outbox, make_notification
from scout.notifications.templates import render_message

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def opportunity(**changes):
    fields = dict(id="alpha", title="Opportunity", canonical_url="https://example.org/?a=1&b=2",
                  source_url="https://example.org", source_name="Example", source_type="rss",
                  source_id="example", category="hackathons", verification_status="official_verified",
                  verification_checked_at=NOW, deadline_at=NOW + timedelta(hours=6),
                  deadline_verified=True, deadline_type="registration", price_status="verified_free",
                  eligibility="Students in India", registration_url="https://example.org/signup",
                  registration_verified=True, benefit_summary="Useful benefit", next_action="Apply now")
    return Opportunity(**(fields | changes))


class FakeStore:
    def __init__(self, *items):
        self.opportunities = {item.id: item.model_copy(deep=True) for item in items}
        self.notifications = {}

    def enqueue(self, record):
        if record.idempotency_key in self.notifications:
            return False
        self.notifications[record.idempotency_key] = record.model_copy(deep=True)
        return True

    def list_notifications(self):
        return [record.model_copy(deep=True) for record in self.notifications.values()]

    def due_notifications(self, now, limit=5):
        return [record for record in self.list_notifications()
                if record.status in {"pending", "retryable_failure"}
                and (record.next_attempt_at is None or record.next_attempt_at <= now)][:limit]

    def update_notification(self, record):
        self.notifications[record.idempotency_key] = record.model_copy(deep=True)

    def get_opportunity(self, id):
        item = self.opportunities.get(id)
        return item.model_copy(deep=True) if item else None

    def upsert_opportunity(self, item):
        self.opportunities[item.id] = item.model_copy(deep=True)


class FakeSender:
    def __init__(self, failures=()):
        self.sent = []
        self.failures = iter(failures)

    def send(self, notification):
        self.sent.append(notification.idempotency_key)
        failure = next(self.failures, None)
        if failure:
            raise failure


def test_static_templates_escape_and_require_verified_links():
    item = opportunity(title='<img src=x onerror="oops">\r\nInjected\u202e',
                       eligibility="a <script>bad</script>",
                       registration_url="javascript:alert(1)")
    subject, text, html = render_message("urgent", [item], NOW)
    assert "\n" not in subject and "\r" not in subject
    assert "\u202e" not in html and "\u202e" not in text
    assert "<img" not in html and "&lt;img" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "javascript:" not in html and "javascript:" not in text
    assert 'href="https://example.org/?a=1&amp;b=2"' in html
    assert "18:00" in text and "UTC" in text and "IST" in text
    assert all(word in text for word in ("Deadline", "Last checked", "Pricing", "Eligibility",
                                        "Verification", "Why it matters", "Next action"))
    _, text, html = render_message("digest", [item.model_copy(update={
        "verification_status": "unverified"})], NOW)
    assert "https://example.org" not in text and "href=" not in html


def test_digest_bounded_and_health_source_changes():
    items = [opportunity(id=f"item-{index}") for index in range(16)]
    _, text, _ = render_message("digest", items, NOW)
    assert "12. Opportunity" in text and "13. Opportunity" not in text
    _, text, html = render_message("health", [], NOW, health={"<Publisher>": "429 <timeout>"})
    assert "<Publisher>: 429 <timeout>" in text
    assert "&lt;Publisher&gt;: 429 &lt;timeout&gt;" in html
    for kind in ("update", "test"):
        assert render_message(kind, [], NOW)[1].strip()


def test_stable_keys_and_urgent_version_deadline_identity():
    item = opportunity()
    first = make_notification("urgent", [item], NOW, "window-6h")
    assert first.idempotency_key == make_notification("urgent", [item], NOW + timedelta(hours=1),
                                                      "window-6h").idempotency_key
    assert first.message_id.startswith("<scout-")
    assert first.metadata["records"][0]["record_version"] == 1
    changed = item.model_copy(update={"record_version": 2})
    assert make_notification("urgent", [changed], NOW, "window-6h").id != first.id
    changed = item.model_copy(update={"deadline_at": NOW + timedelta(hours=8)})
    assert make_notification("urgent", [changed], NOW, "window-6h").id != first.id


def test_retry_backoff_daily_caps_and_history_only_on_sent():
    item = opportunity()
    store = FakeStore(item)
    record = make_notification("digest", [item], NOW, "day-1")
    assert store.enqueue(record) and not store.enqueue(record)
    sender = FakeSender([DeliveryError("temporary", retryable=True)])
    box = Outbox(store, sender)
    assert box.deliver(NOW, checkpoint=lambda: None)["failed"] == 1
    failed = store.list_notifications()[0]
    assert failed.status == "retryable_failure" and failed.attempts == 1
    assert failed.next_attempt_at == NOW + timedelta(minutes=5)
    assert not store.get_opportunity(item.id).notification_history
    assert box.deliver(NOW + timedelta(minutes=4))["pending"] == 1
    assert len(sender.sent) == 1
    assert box.deliver(NOW + timedelta(minutes=5), daily_limit=1)["sent"] == 0
    assert box.deliver(NOW + timedelta(minutes=5), daily_limit=2)["sent"] == 1
    assert len(store.get_opportunity(item.id).notification_history) == 1
    assert box.deliver(NOW + timedelta(minutes=6))["sent"] == 0


def test_checkpoint_failure_blocks_smtp_and_lease_delays_recovery():
    item = opportunity()
    store = FakeStore(item)
    store.enqueue(make_notification("test", [item], NOW, "lease"))
    sender = FakeSender()

    def fail():
        raise RuntimeError("checkpoint failed")

    with pytest.raises(RuntimeError, match="checkpoint failed"):
        Outbox(store, sender).deliver(NOW, checkpoint=fail)
    assert not sender.sent and store.list_notifications()[0].status == "sending"
    assert Outbox(store, sender).deliver(NOW + timedelta(minutes=29))["sent"] == 0
    assert Outbox(store, sender).deliver(NOW + timedelta(minutes=30))["sent"] == 1


def test_uncertain_acceptance_visible_and_no_rapid_resend():
    item = opportunity()
    store = FakeStore(item)
    store.enqueue(make_notification("urgent", [item], NOW, "uncertain"))
    sender = FakeSender([DeliveryError("SMTP connection/transport failure", retryable=True,
                                       uncertain=True)])
    box = Outbox(store, sender)
    assert box.deliver(NOW)["failed"] == 1
    failure = store.list_notifications()[0]
    assert failure.status == "retryable_failure"
    assert failure.next_attempt_at >= NOW + timedelta(minutes=30)
    assert failure.metadata["acceptance_uncertain"]
    assert "acceptance uncertain" in failure.last_error
    assert not store.get_opportunity(item.id).notification_history
    box.deliver(NOW + timedelta(minutes=29))
    assert len(sender.sent) == 1


def test_permanent_failure_and_max_attempts():
    item = opportunity()
    store = FakeStore(item)
    store.enqueue(make_notification("update", [item], NOW, "permanent"))
    sender = FakeSender([DeliveryError("SMTP rejected request (550)", retryable=False)])
    assert Outbox(store, sender).deliver(NOW)["failed"] == 1
    assert store.list_notifications()[0].status == "permanent_failure"
    assert not store.get_opportunity(item.id).notification_history
    store.enqueue(make_notification("update", [item], NOW, "max"))
    another = FakeSender([DeliveryError("temporary", retryable=True)])
    Outbox(store, another, max_attempts=1).deliver(NOW)
    assert any(n.status == "permanent_failure" and n.attempts == 1
               for n in store.list_notifications())


def test_dry_run_no_mutation_and_no_send():
    item = opportunity()
    store = FakeStore(item)
    store.enqueue(make_notification("digest", [item], NOW, "preview"))
    before = store.list_notifications()
    sender = FakeSender()
    result = Outbox(store, sender).deliver(NOW, dry_run=True)
    assert result["dry_run"] and result["pending"] == 1
    assert result["previews"][0]["text"] and result["previews"][0]["html"]
    assert store.list_notifications() == before and not sender.sent


def test_per_run_and_local_day_caps_are_persisted():
    item = opportunity()
    store = FakeStore(item)
    for key in ("first", "second", "third"):
        store.enqueue(make_notification("test", [item], NOW, key))
    sender = FakeSender()
    box = Outbox(store, sender)
    assert box.deliver(NOW, limit=1, daily_limit=1)["sent"] == 1
    assert box.deliver(NOW + timedelta(hours=1), daily_limit=1)["sent"] == 0
    # 12:00 UTC is 17:30 IST; seven hours later is the next local calendar day.
    assert box.deliver(NOW + timedelta(hours=7), daily_limit=1)["sent"] == 1
    assert len(sender.sent) == 2
    assert box.deliver(NOW + timedelta(hours=7), daily_limit=1)["pending"] == 1


def test_outcome_checkpoint_failure_surfaces_without_resend():
    store = FakeStore(opportunity())
    store.enqueue(make_notification("test", [], NOW, "outcome"))
    sender = FakeSender()
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("outcome checkpoint failed")

    with pytest.raises(RuntimeError, match="outcome checkpoint failed"):
        Outbox(store, sender).deliver(NOW, checkpoint=checkpoint)
    assert calls == 2 and len(sender.sent) == 1
    assert store.list_notifications()[0].status == "sent"
    assert Outbox(store, sender).deliver(NOW + timedelta(hours=2))["sent"] == 0


def test_smtp_disabled_and_single_validated_recipient(monkeypatch):
    for name in ("EMAIL_ENABLED", "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
                 "EMAIL_FROM", "EMAIL_TO", "SMTP_SECURITY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="disabled"):
        SmtpSender.from_env()
    values = {"EMAIL_ENABLED": "true", "SMTP_HOST": "mail.example.org", "SMTP_PORT": "587",
              "SMTP_USERNAME": "user", "SMTP_PASSWORD": "private-value",
              "EMAIL_FROM": "sender@example.org", "EMAIL_TO": "user@example.org",
              "SMTP_SECURITY": "starttls"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    assert SmtpSender.from_env().recipient == "user@example.org"
    for invalid in ("a@example.org,b@example.org", "a@example.org\r\nBcc: evil@example.org",
                    "User <a@example.org>"):
        monkeypatch.setenv("EMAIL_TO", invalid)
        with pytest.raises(ValueError, match="one bare email"):
            SmtpSender.from_env()
    monkeypatch.setenv("EMAIL_TO", values["EMAIL_TO"])
    monkeypatch.setenv("SMTP_SECURITY", "none")
    with pytest.raises(ValueError, match="starttls or ssl"):
        SmtpSender.from_env()


def test_smtp_mock_mandatory_tls_auth_envelope_and_plain_alternative():
    record = make_notification("test", [opportunity()], NOW, "tls")
    calls = []

    class SMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def ehlo(self):
            calls.append("ehlo")

        def starttls(self, *, context):
            assert context.verify_mode.name == "CERT_REQUIRED" and context.check_hostname
            calls.append("starttls")

        def login(self, username, password):
            calls.append("login")

        def send_message(self, message, *, from_addr, to_addrs):
            assert calls.index("starttls") < calls.index("login")
            assert message["From"] == from_addr == "sender@example.org"
            assert message["To"] == to_addrs[0] == "recipient@example.org"
            assert message["Message-ID"] == record.message_id
            assert message.is_multipart()
            parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
            assert parsed.get_body(preferencelist=("plain",)).get_content().strip()
            assert parsed.get_body(preferencelist=("html",)).get_content().strip()
            calls.append("sent")
            return {}

        def quit(self):
            calls.append("quit")

    sender = SmtpSender("mail.example.org", 587, "user", "private-value",
                        "sender@example.org", "recipient@example.org", "starttls")
    with patch("scout.notifications.email_sender.smtplib.SMTP", SMTP):
        sender.send(record)
    assert calls.index("starttls") < calls.index("sent")
    assert calls[-1] == "quit"


def test_smtp_tls_failure_does_not_auth_or_send_and_hides_server_response():
    import smtplib

    record = make_notification("test", [], NOW, "tls-error")
    calls = []

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def ehlo(self):
            pass

        def starttls(self, *, context):
            calls.append("tls")
            raise smtplib.SMTPResponseException(454, b"private-value")

        def close(self):
            calls.append("close")

    sender = SmtpSender("mail.example.org", 587, "user", "private-value",
                        "sender@example.org", "recipient@example.org", "starttls")
    with patch("scout.notifications.email_sender.smtplib.SMTP", SMTP):
        with pytest.raises(DeliveryError) as captured:
            sender.send(record)
    assert captured.value.retryable and "private-value" not in str(captured.value)
    assert calls == ["tls", "close"]


def test_smtp_ssl_and_rejected_recipient_are_permanent():
    import smtplib

    record = make_notification("test", [], NOW, "ssl")
    calls = []

    class SMTP:
        def __init__(self, host, port, *, timeout, context):
            assert context.verify_mode.name == "CERT_REQUIRED" and context.check_hostname
            calls.append("implicit_tls")

        def login(self, username, password):
            calls.append("login")

        def send_message(self, *args, **kwargs):
            raise smtplib.SMTPRecipientsRefused({"recipient@example.org": (550, b"private-value")})

        def close(self):
            calls.append("close")

    sender = SmtpSender("mail.example.org", 465, "user", "private-value",
                        "sender@example.org", "recipient@example.org", "ssl")
    with patch("scout.notifications.email_sender.smtplib.SMTP_SSL", SMTP):
        with pytest.raises(DeliveryError) as captured:
            sender.send(record)
    assert not captured.value.retryable and "private-value" not in str(captured.value)
    assert calls == ["implicit_tls", "login", "close"]


def test_real_sqlite_outbox_survives_reopen(tmp_path):
    from scout.models import Source
    from scout.storage.sqlite_store import Store

    item = opportunity()
    source = Source(id="example", name="Example", url="https://example.org", type="rss",
                    categories=["hackathons"])
    db = tmp_path / "outbox.db"
    with Store(db, create=True) as store:
        store.upsert_source(source)
        store.upsert_opportunity(item)
        assert store.enqueue(make_notification("urgent", [item], NOW, "db-key"))
    sender = FakeSender()
    with Store(db) as store:
        assert not store.enqueue(make_notification("urgent", [item], NOW, "db-key"))
        assert Outbox(store, sender).deliver(NOW)["sent"] == 1
    with Store(db) as store:
        assert store.list_notifications()[0].status == "sent"
        assert len(store.get_opportunity(item.id).notification_history) == 1
        assert Outbox(store, sender).deliver(NOW)["sent"] == 0
        assert len(sender.sent) == 1

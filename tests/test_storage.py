import copy
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from scout.models import Notification, Opportunity, RunMetrics, Source
from scout.storage.sqlite_store import StateConflict, StateError, Store, state_checksum

NOW = datetime(2026, 10, 19, tzinfo=UTC)


def source():
    return Source(id="example", name="Example", url="https://example.org/rss",
                  type="rss", categories=["hackathons"])


def opportunity(id="one", **changes):
    return Opportunity(id=id, title="Event", canonical_url="https://example.org/event",
                       source_url="https://example.org/rss", source_name="Example",
                       source_type="rss", source_id="example", category="hackathons",
                       first_seen_at=NOW - timedelta(days=400), last_seen_at=NOW - timedelta(days=200),
                       **changes)


def notification(id="n1", key="key1", **changes):
    return Notification(id=id, idempotency_key=key, kind="digest", opportunity_ids=["one"],
                        subject="Subject", text_body="Text", html_body="<p>Text</p>",
                        message_id="<stable@example.org>", created_at=NOW, updated_at=NOW,
                        **changes)


def sample(db):
    db.upsert_source(source())
    db.upsert_opportunity(opportunity())
    assert db.enqueue(notification())
    db.record_run(RunMetrics(id="run1", started_at=NOW))
    db.set_meta("last_digest", {"at": "2026-10-19"})


def test_missing_corrupt_and_exclusive_process_lock(tmp_path):
    path = tmp_path / "private.db"
    with pytest.raises(StateError, match="missing"):
        Store(path)
    with Store(path, create=True):
        with pytest.raises(StateError, match="lock"):
            Store(path)
    path.write_bytes(b"not sqlite")
    with pytest.raises(StateError):
        Store(path, create=True)


def test_roundtrip_due_feedback_and_atomic_invalid_import(tmp_path):
    path = tmp_path / "private.db"
    with Store(path, create=True) as db:
        sample(db)
        assert db.enqueue(notification()) is False
        due = db.due_notifications(NOW)
        assert [n.id for n in due] == ["n1"]
        retry = due[0]
        retry.status = "retryable_failure"
        retry.next_attempt_at = NOW + timedelta(hours=1)
        db.update_notification(retry)
        assert db.due_notifications(NOW) == []
        assert db.due_notifications(NOW + timedelta(hours=1))[0].id == "n1"
        assert db.due_notifications(NOW, limit=0) == []
        retry.status = "sending"
        retry.updated_at = NOW - timedelta(minutes=29)
        retry.next_attempt_at = None
        db.update_notification(retry)
        assert db.due_notifications(NOW) == []
        retry.updated_at = NOW - timedelta(hours=2)
        db.update_notification(retry)
        assert [n.id for n in db.due_notifications(NOW)] == ["n1"]
        assert db.feedback("one", "save").feedback == "save"
        db.reset_feedback()
        assert db.get_opportunity("one").feedback is None
        state = db.export_state()
        assert state["schema_version"] == 2
        assert len(state["checksum"]) == 64
        invalid = copy.deepcopy(state)
        invalid["opportunities"][0]["source_id"] = "missing"
        invalid["checksum"] = state_checksum({k: v for k, v in invalid.items() if k != "checksum"})
        with pytest.raises(StateError, match="references"):
            db.import_state(invalid, replace=True)
        assert db.export_state() == state
        with pytest.raises(StateConflict):
            db.import_state(state)
    with Store(path) as db:
        assert db.export_state() == state
    with Store(tmp_path / "imported.db", create=True) as db:
        db.import_state(state)
        assert db.export_state() == state


def test_backup_restore_and_prune_keep_unsent_and_history(tmp_path):
    path = tmp_path / "private.db"
    backup = tmp_path / "backup.db"
    with Store(path, create=True) as db:
        sample(db)
        db.upsert_opportunity(opportunity("two"))
        db.upsert_opportunity(opportunity("two").model_copy(update={"first_seen_at": NOW}))
        assert db.get_opportunity("two").first_seen_at == NOW - timedelta(days=400)
        db.backup(backup)
        db.set_meta("changed", True)
        with pytest.raises(StateConflict):
            db.restore(backup)
        db.restore(backup, replace=True)
        assert db.get_meta("changed") is None
        stats = db.prune(NOW, max_opportunities=0)
        assert stats["protected"] == 1
        assert stats["over_limit"]
        assert db.get_opportunity("one") is not None
        assert db.get_opportunity("two") is None
        db.upsert_opportunity(opportunity("two").model_copy(update={"first_seen_at": NOW}))
        assert db.get_opportunity("two").first_seen_at == NOW - timedelta(days=400)
        sent = db.list_notifications()[0]
        sent.status = "sent"
        sent.updated_at = NOW - timedelta(days=200)
        db.update_notification(sent)
        db.prune(NOW, max_opportunities=0)
        assert db.list_notifications() == []
        assert not db.enqueue(notification())  # Compact key prevents repeat delivery.
        assert db.export_state()["tombstones"]
        bad = tmp_path / "bad.db"
        bad.write_bytes(b"bad backup")
        with pytest.raises(StateError):
            db.restore(bad, replace=True)
        assert db.get_meta("last_digest") is not None


def test_v1_migration(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.execute("CREATE TABLE opportunities (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.execute("CREATE TABLE notifications (id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, data TEXT NOT NULL)")
        conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.execute("INSERT INTO sources VALUES (?, ?)", ("example", source().model_dump_json()))
        conn.execute("INSERT INTO opportunities VALUES (?, ?)", ("one", opportunity().model_dump_json()))
        conn.execute("INSERT INTO notifications VALUES (?, ?, ?)",
                     ("n1", "key1", notification().model_dump_json()))
        conn.execute("INSERT INTO runs VALUES (?, ?)",
                     ("run1", RunMetrics(id="run1", started_at=NOW).model_dump_json()))
        conn.execute("PRAGMA user_version=1")
    with Store(path) as db:
        assert db.export_state()["schema_version"] == 2
        assert [n.id for n in db.due_notifications(NOW)] == ["n1"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_bad_v1_migration_rolls_back(tmp_path):
    path = tmp_path / "bad-legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.execute("CREATE TABLE opportunities (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.execute("CREATE TABLE notifications (id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, data TEXT NOT NULL)")
        conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.execute("INSERT INTO opportunities VALUES ('bad', '{broken json')")
        conn.execute("PRAGMA user_version=1")
    with pytest.raises(StateError):
        Store(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert {r[1] for r in conn.execute("PRAGMA table_info(opportunities)")} == {"id", "data"}


def test_corrupt_index_rejected_without_reset(tmp_path):
    path = tmp_path / "private.db"
    with Store(path, create=True) as db:
        sample(db)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE notifications SET status='sent' WHERE id='n1'")
    with pytest.raises(StateError, match="index mismatch"):
        Store(path, create=True)
    assert path.exists()


def test_restore_refuses_extra_sql_objects(tmp_path):
    path = tmp_path / "private.db"
    backup = tmp_path / "untrusted.db"
    with Store(path, create=True) as db:
        sample(db)
        db.backup(backup)
        with sqlite3.connect(backup) as conn:
            conn.execute("CREATE TRIGGER unwanted AFTER INSERT ON meta BEGIN DELETE FROM sources; END")
        with pytest.raises(StateError, match="schema"):
            db.restore(backup, replace=True)
        assert db.get_meta("last_digest") is not None


def test_reject_sql_data_and_cross_references(tmp_path):
    with Store(tmp_path / "state.db", create=True) as db:
        sample(db)
        payload = db.export_state()
        original = json.dumps(payload, sort_keys=True)
        bad = copy.deepcopy(payload)
        bad["meta"]["last_digest"] = "'); DROP TABLE opportunities; --"
        bad["checksum"] = state_checksum({k: v for k, v in bad.items() if k != "checksum"})
        db.import_state(bad, replace=True)
        assert len(db.list_opportunities()) == 1
        payload = json.loads(original)
        bad = copy.deepcopy(payload)
        bad["notifications"][0]["opportunity_ids"] = ["phantom"]
        bad["checksum"] = state_checksum({k: v for k, v in bad.items() if k != "checksum"})
        with pytest.raises(StateError):
            db.import_state(bad, replace=True)
        with pytest.raises(StateError):
            db.set_meta("smtp_password", "nope")

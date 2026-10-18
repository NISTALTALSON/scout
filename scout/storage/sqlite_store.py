"""Locked SQLite private state; exports are validated data, never executable SQL."""

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import portalocker
from pydantic import ValidationError

from scout.models import Notification, Opportunity, RunMetrics, Source

SCHEMA_VERSION = 2
TABLES = {"meta", "sources", "opportunities", "notifications", "runs", "tombstones", "notification_keys"}
INDEXES = {"idx_due", "idx_last_seen", "idx_runs"}
ACTIVE = ("pending", "sending", "retryable_failure")


class StateError(RuntimeError):
    """Missing, corrupt, invalid, or unavailable private state."""


class StateConflict(StateError):
    """A checkpoint was changed concurrently; do not overwrite it."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def state_checksum(value: dict) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def validate_state(payload: Any) -> dict:
    """Normalize/check a versioned snapshot, including IDs and all references."""
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "checksum", "meta", "sources", "opportunities", "notifications",
        "runs", "tombstones", "notification_keys",
    }:
        raise StateError("invalid state format")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise StateError("unsupported state version")
    if not isinstance(payload["checksum"], str) or len(payload["checksum"]) != 64:
        raise StateError("invalid state checksum")
    unsigned = {k: v for k, v in payload.items() if k != "checksum"}
    try:
        if not hmac.compare_digest(payload["checksum"], state_checksum(unsigned)):
            raise StateError("state checksum mismatch")
    except (TypeError, ValueError, OverflowError) as exc:
        raise StateError("invalid state JSON") from exc
    if not isinstance(payload["meta"], dict) or not all(
        isinstance(key, str) for key in payload["meta"]
    ):
        raise StateError("invalid state metadata")
    if any(not key or any(word in key.lower() for word in
           ("password", "secret", "token", "credential", "api_key")) for key in payload["meta"]):
        raise StateError("credentials are not state metadata")
    for name in ("sources", "opportunities", "notifications", "runs", "tombstones", "notification_keys"):
        if not isinstance(payload[name], list):
            raise StateError("invalid state records")
    try:
        sources = [Source.model_validate(item) for item in payload["sources"]]
        items = [Opportunity.model_validate(item) for item in payload["opportunities"]]
        notifications = [Notification.model_validate(item) for item in payload["notifications"]]
        runs = [RunMetrics.model_validate(item) for item in payload["runs"]]
        tombstones = [dict(t) if isinstance(t, dict) else t for t in payload["tombstones"]]
        keys = payload["notification_keys"]
        if any(not isinstance(t, dict) or set(t) != {"id", "first_seen_at"} or
               not isinstance(t["id"], str) for t in tombstones):
            raise ValueError("invalid tombstone")
        for tombstone in tombstones:
            tombstone["first_seen_at"] = _aware_iso(tombstone["first_seen_at"])
        if any(not isinstance(k, str) or not k for k in keys):
            raise ValueError("invalid notification key")
        for records in (sources, items, notifications, runs):
            ids = [record.id for record in records]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate record ID")
        source_ids = {s.id for s in sources}
        item_ids = {i.id for i in items}
        tombstone_ids = {t["id"] for t in tombstones}
        if item_ids & tombstone_ids or len(tombstone_ids) != len(tombstones):
            raise ValueError("duplicate opportunity/tombstone ID")
        if any(i.source_id not in source_ids for i in items):
            raise ValueError("opportunity references missing source")
        if any(set(i.related_ids) - item_ids - tombstone_ids for i in items):
            raise ValueError("opportunity references missing related record")
        if any(set(n.opportunity_ids) - item_ids - tombstone_ids for n in notifications):
            raise ValueError("notification references missing opportunity")
        notification_keys = [n.idempotency_key for n in notifications]
        if len(notification_keys) != len(set(notification_keys)) or len(keys) != len(set(keys)):
            raise ValueError("duplicate notification key")
        if set(notification_keys) & set(keys):
            raise ValueError("notification key already tombstoned")
        if any(n.status in ACTIVE and set(n.opportunity_ids) - item_ids for n in notifications):
            raise ValueError("unsent notification references pruned opportunity")
        normalized = {
            "schema_version": SCHEMA_VERSION, "meta": payload["meta"],
            "sources": [s.model_dump(mode="json") for s in sources],
            "opportunities": [i.model_dump(mode="json") for i in items],
            "notifications": [n.model_dump(mode="json") for n in notifications],
            "runs": [r.model_dump(mode="json") for r in runs],
            "tombstones": tombstones, "notification_keys": keys,
        }
        _json(normalized)
    except (ValidationError, ValueError, TypeError, OverflowError) as exc:
        raise StateError("invalid state records or references") from exc
    normalized["checksum"] = state_checksum(normalized)
    return normalized


def _aware_iso(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("timestamp required")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timezone required")
    return dt.astimezone(UTC).isoformat()


def _schema_objects(db: sqlite3.Connection) -> set[tuple[str, str]]:
    return {(row[0], row[1]) for row in db.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex_%'")}


def _expected_schema(version: int) -> set[tuple[str, str]]:
    tables = TABLES if version == 2 else {"meta", "sources", "opportunities", "notifications", "runs"}
    indexes = INDEXES if version == 2 else set()
    return {("table", name) for name in tables} | {("index", name) for name in indexes}


class Store:
    def __init__(self, path: str | Path, create: bool = False):
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = None
        if not create and not self.path.is_file():
            raise StateError("state database is missing; initialize explicitly")
        if not self.path.parent.is_dir():
            raise StateError("state parent directory is missing")
        try:
            self._lock = portalocker.Lock(str(self.path) + ".lock", timeout=0)
            self._lock.acquire()
            if not create and not self.path.is_file():
                raise StateError("state database is missing; initialize explicitly")
            new = not self.path.exists()
            if new and not create:
                raise StateError("state database is missing")
            self._connect(create=new)
            if new:
                self._create_schema()
                if os.name == "posix":
                    os.chmod(self.path, 0o600)
            else:
                self._check_schema()
        except Exception as exc:
            self.close()
            if isinstance(exc, StateError):
                raise
            raise StateError("cannot open or lock state database") from exc

    def _connect(self, create: bool = False) -> None:
        mode = "rwc" if create else "rw"
        self._conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=" + mode,
                                     uri=True, timeout=5, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def _create_schema(self) -> None:
        with self._transaction() as db:
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE opportunities (id TEXT PRIMARY KEY, data TEXT NOT NULL, last_seen_at TEXT NOT NULL)")
            db.execute("CREATE TABLE notifications (id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, data TEXT NOT NULL, status TEXT NOT NULL, next_attempt_at TEXT)")
            db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, data TEXT NOT NULL, started_at TEXT NOT NULL)")
            db.execute("CREATE TABLE tombstones (id TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL)")
            db.execute("CREATE TABLE notification_keys (key TEXT PRIMARY KEY)")
            db.execute("CREATE INDEX idx_due ON notifications(status, next_attempt_at)")
            db.execute("CREATE INDEX idx_last_seen ON opportunities(last_seen_at)")
            db.execute("CREATE INDEX idx_runs ON runs(started_at)")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _check_schema(self) -> None:
        db = self._db
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise StateError("corrupt state database")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version in (1, SCHEMA_VERSION) and _schema_objects(db) != _expected_schema(version):
            raise StateError("incomplete or unsafe state database schema")
        if version == 1:
            self._migrate_v1()
        elif version != SCHEMA_VERSION:
            raise StateError("unsupported state database schema")
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names != TABLES:
            raise StateError("incomplete state database schema")
        required = {
            "meta": {"key", "value"}, "sources": {"id", "data"},
            "opportunities": {"id", "data", "last_seen_at"},
            "notifications": {"id", "idempotency_key", "data", "status", "next_attempt_at"},
            "runs": {"id", "data", "started_at"},
            "tombstones": {"id", "first_seen_at"}, "notification_keys": {"key"},
        }
        for table, columns in required.items():
            if {r[1] for r in db.execute(f"PRAGMA table_info({table})")} != columns:
                raise StateError("incompatible state database schema")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise StateError("invalid database references")
        validate_state(self._raw_export())

    def _migrate_v1(self) -> None:
        """v1 used JSON rows but did not index scheduling or retain compact tombstones."""
        db = self._db
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names != {"meta", "sources", "opportunities", "notifications", "runs"}:
            raise StateError("incompatible v1 state database")
        with self._transaction() as conn:
            conn.execute("ALTER TABLE opportunities ADD COLUMN last_seen_at TEXT")
            conn.execute("ALTER TABLE notifications ADD COLUMN status TEXT")
            conn.execute("ALTER TABLE notifications ADD COLUMN next_attempt_at TEXT")
            conn.execute("ALTER TABLE runs ADD COLUMN started_at TEXT")
            conn.execute("CREATE TABLE tombstones (id TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE notification_keys (key TEXT PRIMARY KEY)")
            for row in conn.execute("SELECT id, data FROM opportunities").fetchall():
                item = Opportunity.model_validate_json(row["data"])
                if item.id != row["id"]:
                    raise StateError("invalid v1 opportunity ID")
                conn.execute("UPDATE opportunities SET last_seen_at=? WHERE id=?",
                             (item.last_seen_at.isoformat(), item.id))
            for row in conn.execute("SELECT id, data FROM notifications").fetchall():
                item = Notification.model_validate_json(row["data"])
                if item.id != row["id"]:
                    raise StateError("invalid v1 notification ID")
                conn.execute("UPDATE notifications SET status=?, next_attempt_at=? WHERE id=?",
                             (item.status, item.next_attempt_at.isoformat() if item.next_attempt_at else None, item.id))
            for row in conn.execute("SELECT id, data FROM runs").fetchall():
                item = RunMetrics.model_validate_json(row["data"])
                if item.id != row["id"]:
                    raise StateError("invalid v1 run ID")
                conn.execute("UPDATE runs SET started_at=? WHERE id=?", (item.started_at.isoformat(), item.id))
            conn.execute("CREATE INDEX idx_due ON notifications(status, next_attempt_at)")
            conn.execute("CREATE INDEX idx_last_seen ON opportunities(last_seen_at)")
            conn.execute("CREATE INDEX idx_runs ON runs(started_at)")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StateError("state database is closed")
        return self._conn

    @contextmanager
    def _transaction(self):
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key or any(
            word in key.lower() for word in ("password", "secret", "token", "credential", "api_key")
        ):
            raise StateError("invalid metadata key")
        data = _json(value)
        with self._transaction() as db:
            db.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, data))

    def list_sources(self) -> list[Source]:
        return [Source.model_validate_json(row[0]) for row in self._db.execute("SELECT data FROM sources ORDER BY id")]

    def upsert_source(self, source: Source) -> None:
        source = Source.model_validate(source)
        with self._transaction() as db:
            db.execute("INSERT INTO sources VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                       (source.id, source.model_dump_json()))

    def list_opportunities(self) -> list[Opportunity]:
        return [Opportunity.model_validate_json(row[0]) for row in self._db.execute("SELECT data FROM opportunities ORDER BY id")]

    def get_opportunity(self, id: str) -> Opportunity | None:
        row = self._db.execute("SELECT data FROM opportunities WHERE id=?", (id,)).fetchone()
        return Opportunity.model_validate_json(row[0]) if row else None

    def upsert_opportunity(self, item: Opportunity) -> None:
        item = Opportunity.model_validate(item)
        with self._transaction() as db:
            if not db.execute("SELECT 1 FROM sources WHERE id=?", (item.source_id,)).fetchone():
                raise StateError("opportunity references missing source")
            if any(not db.execute("SELECT 1 FROM opportunities WHERE id=? UNION SELECT 1 FROM tombstones WHERE id=?", (ref, ref)).fetchone()
                   for ref in item.related_ids):
                raise StateError("opportunity references missing related record")
            old = db.execute("SELECT data FROM opportunities WHERE id=?", (item.id,)).fetchone()
            if old:
                first = Opportunity.model_validate_json(old[0]).first_seen_at
                item.first_seen_at = min(item.first_seen_at, first)
            old = db.execute("SELECT first_seen_at FROM tombstones WHERE id=?", (item.id,)).fetchone()
            if old:
                item.first_seen_at = min(item.first_seen_at, datetime.fromisoformat(old[0]))
                db.execute("DELETE FROM tombstones WHERE id=?", (item.id,))
            db.execute("INSERT INTO opportunities VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, last_seen_at=excluded.last_seen_at",
                       (item.id, item.model_dump_json(), item.last_seen_at.isoformat()))

    def list_notifications(self) -> list[Notification]:
        return [Notification.model_validate_json(row[0]) for row in self._db.execute("SELECT data FROM notifications ORDER BY id")]

    def enqueue(self, notification: Notification) -> bool:
        item = Notification.model_validate(notification)
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM notifications WHERE id=? OR idempotency_key=?", (item.id, item.idempotency_key)).fetchone() or db.execute(
                "SELECT 1 FROM notification_keys WHERE key=?", (item.idempotency_key,)
            ).fetchone():
                return False
            if any(not db.execute("SELECT 1 FROM opportunities WHERE id=?", (ref,)).fetchone()
                   for ref in item.opportunity_ids):
                raise StateError("notification references missing opportunity")
            db.execute("INSERT INTO notifications VALUES (?, ?, ?, ?, ?)",
                       (item.id, item.idempotency_key, item.model_dump_json(), item.status,
                        item.next_attempt_at.isoformat() if item.next_attempt_at else None))
            return True

    def update_notification(self, notification: Notification) -> None:
        item = Notification.model_validate(notification)
        with self._transaction() as db:
            old = db.execute("SELECT idempotency_key FROM notifications WHERE id=?", (item.id,)).fetchone()
            if old is None or old[0] != item.idempotency_key:
                raise StateError("notification identity cannot change")
            if any(not db.execute("SELECT 1 FROM opportunities WHERE id=? UNION SELECT 1 FROM tombstones WHERE id=?", (ref, ref)).fetchone()
                   for ref in item.opportunity_ids):
                raise StateError("notification references missing opportunity")
            if item.status in ACTIVE and any(not db.execute(
                "SELECT 1 FROM opportunities WHERE id=?", (ref,)).fetchone()
                                             for ref in item.opportunity_ids):
                raise StateError("unsent notification references pruned opportunity")
            db.execute("UPDATE notifications SET data=?, status=?, next_attempt_at=? WHERE id=?",
                       (item.model_dump_json(), item.status,
                        item.next_attempt_at.isoformat() if item.next_attempt_at else None, item.id))

    def due_notifications(self, now: datetime, limit: int = 5) -> list[Notification]:
        when = _instant(now)
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return []
        # Mirror the outbox's 30-minute sending lease; uncertain acceptance is not resent early.
        lease = (when - timedelta(minutes=30)).isoformat()
        rows = self._db.execute(
            "SELECT data FROM notifications WHERE ((status IN ('pending', 'retryable_failure') AND "
            "(next_attempt_at IS NULL OR next_attempt_at <= ?)) OR status='sending') "
            "ORDER BY next_attempt_at, id", (when.isoformat(),),
        )
        due = []
        for row in rows:
            item = Notification.model_validate_json(row[0])
            if item.status != "sending" or item.updated_at.isoformat() <= lease:
                due.append(item)
                if len(due) >= limit:
                    break
        return due

    def record_run(self, metrics: RunMetrics) -> None:
        item = RunMetrics.model_validate(metrics)
        with self._transaction() as db:
            db.execute("INSERT INTO runs VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, started_at=excluded.started_at",
                       (item.id, item.model_dump_json(), item.started_at.isoformat()))

    def list_runs(self, limit: int = 30) -> list[RunMetrics]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        return [RunMetrics.model_validate_json(row[0]) for row in self._db.execute(
            "SELECT data FROM runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,))]

    def feedback(self, id: str, action: str) -> Opportunity:
        if action not in ("save", "ignore", "applied", "irrelevant", "outdated"):
            raise ValueError("invalid feedback action")
        with self._transaction() as db:
            row = db.execute("SELECT data FROM opportunities WHERE id=?", (id,)).fetchone()
            if not row:
                raise StateError("opportunity not found")
            item = Opportunity.model_validate_json(row[0])
            item.feedback = action
            db.execute("UPDATE opportunities SET data=? WHERE id=?", (item.model_dump_json(), id))
            return item

    def reset_feedback(self) -> None:
        with self._transaction() as db:
            for row in db.execute("SELECT id, data FROM opportunities").fetchall():
                item = Opportunity.model_validate_json(row["data"])
                if item.feedback is not None:
                    item.feedback = None
                    db.execute("UPDATE opportunities SET data=? WHERE id=?", (item.model_dump_json(), row["id"]))

    def _raw_export(self) -> dict:
        db = self._db
        raw = {
            "schema_version": SCHEMA_VERSION,
            "meta": {r[0]: json.loads(r[1]) for r in db.execute("SELECT key, value FROM meta ORDER BY key")},
        }
        for name, sql in (
            ("sources", "SELECT id, data FROM sources ORDER BY id"),
            ("opportunities", "SELECT id, data FROM opportunities ORDER BY id"),
            ("notifications", "SELECT id, data FROM notifications ORDER BY id"),
            ("runs", "SELECT id, data FROM runs ORDER BY id"),
        ):
            raw[name] = []
            for row in db.execute(sql):
                value = json.loads(row[1])
                if value.get("id") != row[0]:
                    raise StateError("state row identity mismatch")
                raw[name].append(value)
        for row in db.execute("SELECT id, data, last_seen_at FROM opportunities"):
            item = Opportunity.model_validate_json(row["data"])
            if row["last_seen_at"] != item.last_seen_at.isoformat():
                raise StateError("opportunity index mismatch")
        for row in db.execute("SELECT id, idempotency_key, data, status, next_attempt_at FROM notifications"):
            item = Notification.model_validate_json(row["data"])
            if (row["idempotency_key"] != item.idempotency_key or row["status"] != item.status or
                row["next_attempt_at"] != (item.next_attempt_at.isoformat() if item.next_attempt_at else None)):
                raise StateError("notification index mismatch")
        for row in db.execute("SELECT id, data, started_at FROM runs"):
            item = RunMetrics.model_validate_json(row["data"])
            if row["started_at"] != item.started_at.isoformat():
                raise StateError("run index mismatch")
        raw["tombstones"] = [dict(row) for row in db.execute("SELECT id, first_seen_at FROM tombstones ORDER BY id")]
        raw["notification_keys"] = [row[0] for row in db.execute("SELECT key FROM notification_keys ORDER BY key")]
        raw["checksum"] = state_checksum(raw)
        return raw

    def export_state(self) -> dict:
        db = self._db
        db.execute("BEGIN")
        try:
            state = validate_state(self._raw_export())
            db.execute("COMMIT")
            return state
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def import_state(self, payload: dict, replace: bool = False) -> None:
        state = validate_state(payload)  # Never touch existing state until all records validate.
        with self._transaction() as db:
            if replace:
                for name in ("notifications", "opportunities", "sources", "runs", "meta", "tombstones", "notification_keys"):
                    db.execute(f"DELETE FROM {name}")
            else:
                for name in ("notifications", "opportunities", "sources", "runs", "meta", "tombstones", "notification_keys"):
                    if db.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone():
                        raise StateConflict("state import requires empty database or explicit replacement")
            db.executemany("INSERT INTO meta VALUES (?, ?)",
                           [(k, _json(v)) for k, v in state["meta"].items()])
            for name, model in (("sources", Source), ("opportunities", Opportunity),
                                ("notifications", Notification), ("runs", RunMetrics)):
                for data in state[name]:
                    item = model.model_validate(data)
                    if name == "sources":
                        db.execute("INSERT INTO sources VALUES (?, ?)", (item.id, item.model_dump_json()))
                    elif name == "opportunities":
                        db.execute("INSERT INTO opportunities VALUES (?, ?, ?)",
                                   (item.id, item.model_dump_json(), item.last_seen_at.isoformat()))
                    elif name == "notifications":
                        db.execute("INSERT INTO notifications VALUES (?, ?, ?, ?, ?)",
                                   (item.id, item.idempotency_key, item.model_dump_json(), item.status,
                                    item.next_attempt_at.isoformat() if item.next_attempt_at else None))
                    else:
                        db.execute("INSERT INTO runs VALUES (?, ?, ?)",
                                   (item.id, item.model_dump_json(), item.started_at.isoformat()))
            db.executemany("INSERT INTO tombstones VALUES (?, ?)",
                           [(t["id"], t["first_seen_at"]) for t in state["tombstones"]])
            db.executemany("INSERT INTO notification_keys VALUES (?)",
                           [(key,) for key in state["notification_keys"]])

    def backup(self, path: str | Path) -> None:
        target = Path(path)
        if target.resolve() == self.path.resolve() or not target.parent.is_dir():
            raise StateError("invalid backup destination")
        fd, temp = tempfile.mkstemp(prefix=".scout-backup-", dir=target.parent)
        os.close(fd)
        try:
            with closing(sqlite3.connect(temp)) as dest:
                self._db.backup(dest)
            _validate_external_db(Path(temp))
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def restore(self, path: str | Path, replace: bool = False) -> None:
        if not replace:
            raise StateConflict("restoring over state requires replace=True")
        source = Path(path)
        if source.resolve() == self.path.resolve():
            raise StateError("backup and active state are the same file")
        _validate_external_db(source)
        fd, temp = tempfile.mkstemp(prefix=".scout-restore-", dir=self.path.parent)
        os.close(fd)
        try:
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(temp)) as dest:
                    src.backup(dest)
            _validate_external_db(Path(temp))
            self._db.close()
            self._conn = None
            try:
                os.replace(temp, self.path)
            except OSError:
                self._connect()
                raise
            self._connect()
            self._check_schema()
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def prune(self, now: datetime, retention_days: int = 180, max_opportunities: int = 1000) -> dict:
        now = _instant(now)
        if retention_days < 1 or max_opportunities < 0:
            raise ValueError("invalid retention bounds")
        cutoff = now - timedelta(days=retention_days)
        with self._transaction() as db:
            protected = {id for row in db.execute("SELECT data FROM notifications WHERE status IN ('pending', 'sending', 'retryable_failure')")
                         for id in Notification.model_validate_json(row[0]).opportunity_ids}
            rows = [(Opportunity.model_validate_json(r["data"]), r["last_seen_at"])
                    for r in db.execute("SELECT data, last_seen_at FROM opportunities")]
            recent = sorted((i for i, _ in rows if i.id not in protected),
                            key=lambda i: i.last_seen_at, reverse=True)
            retain = {i.id for i in recent[:max_opportunities] if i.last_seen_at >= cutoff}
            removed = 0
            for item, _ in rows:
                if item.id not in protected | retain:
                    db.execute("INSERT INTO tombstones VALUES (?, ?)",
                               (item.id, item.first_seen_at.isoformat()))
                    db.execute("DELETE FROM opportunities WHERE id=?", (item.id,))
                    removed += 1
            # Complete notifications are compacted, but their keys remain unique forever.
            for row in db.execute("SELECT id, idempotency_key, data FROM notifications WHERE status IN ('sent', 'permanent_failure')").fetchall():
                item = Notification.model_validate_json(row["data"])
                if item.updated_at < cutoff:
                    db.execute("INSERT INTO notification_keys VALUES (?)", (row["idempotency_key"],))
                    db.execute("DELETE FROM notifications WHERE id=?", (row["id"],))
            db.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY started_at DESC LIMIT 1000)")
            count = db.execute("SELECT count(*) FROM opportunities").fetchone()[0]
            return {"removed": removed, "opportunities": count,
                    "protected": len(protected), "over_limit": count > max_opportunities,
                    "tombstones": db.execute("SELECT count(*) FROM tombstones").fetchone()[0],
                    "notification_keys": db.execute("SELECT count(*) FROM notification_keys").fetchone()[0]}

    def stats(self) -> dict:
        return {name: self._db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in TABLES}


def _instant(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(UTC)


def _validate_external_db(path: Path) -> None:
    if not path.is_file():
        raise StateError("backup database is missing")
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute(
                "PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise StateError("backup database is corrupt or incompatible")
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if names != TABLES or _schema_objects(db) != _expected_schema(SCHEMA_VERSION):
                raise StateError("backup database schema incomplete")
            holder = object.__new__(Store)
            holder._conn = db
            validate_state(holder._raw_export())
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise StateError("invalid backup database") from exc

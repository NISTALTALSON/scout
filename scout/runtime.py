"""CLI/runtime assembly; secrets enter only through the process environment."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from scout.config import Configuration, load_local_env
from scout.orchestrator import Scout
from scout.storage.persistence_backend import GitHubStateBackend
from scout.storage.sqlite_store import StateError, Store


def now_utc() -> datetime:
    return datetime.now(UTC)


class Runtime:
    def __init__(self, state_dir: str | Path = "data", config_dir: str | Path = "config",
                 cloud_state: bool = False, bootstrap: bool = False):
        load_local_env()
        self.state_dir, self.config_dir = Path(state_dir), Path(config_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.configuration = Configuration(self.config_dir)
        self.cloud_state = cloud_state
        self.backend = None
        if cloud_state:
            state_values = {
                name: os.getenv(name)
                for name in ("SCOUT_STATE_REPOSITORY", "SCOUT_STATE_TOKEN", "SCOUT_STATE_BRANCH")
            }
            missing = [name for name, value in state_values.items()
                       if not isinstance(value, str) or not value.strip()]
            if missing:
                raise StateError(
                    "cloud state requires SCOUT_STATE_REPOSITORY, SCOUT_STATE_TOKEN, and "
                    "SCOUT_STATE_BRANCH"
                )
            public_repository = os.getenv("GITHUB_REPOSITORY")
            state_repository = state_values["SCOUT_STATE_REPOSITORY"]
            if public_repository and state_repository.casefold() == public_repository.casefold():
                raise StateError("cloud state repository must differ from GITHUB_REPOSITORY")
            self.backend = GitHubStateBackend(
                state_repository, state_values["SCOUT_STATE_TOKEN"], state_values["SCOUT_STATE_BRANCH"]
            )
        self.store = Store(self.state_dir / "scout.sqlite3", create=True)
        if self.backend:
            state = self.backend.load(bootstrap=bootstrap)
            if state is not None:
                self.store.import_state(state, replace=True)
            elif any(value for key, value in self.store.stats().items() if key != "meta"):
                raise StateError("cloud checkpoint is missing but local state is non-empty; explicit recovery required")
        self._checkpoint_count = 0

    def checkpoint(self):
        if not self.backend:
            return
        self.backend.save(self.store.export_state())
        self._checkpoint_count += 1

    def scout(self, sender=None, now=None):
        return Scout(self.store, self.configuration, sender=sender,
                     checkpoint=self.checkpoint if self.backend else None, now=now)

    def close(self):
        try:
            if self.backend:
                self.checkpoint()
        finally:
            if self.backend:
                self.backend.close()
            self.store.close()

    def export_local(self, path: str | Path):
        from scout.storage.persistence_backend import atomic_write_export
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_export(target, self.store.export_state())

    def import_local(self, path: str | Path, replace=False):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.store.import_state(payload, replace=replace)

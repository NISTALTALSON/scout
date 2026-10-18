import base64
import copy
import gzip
import json
from pathlib import Path

import httpx
import pytest

from scout.runtime import Runtime
from scout.storage.persistence_backend import GitHubStateBackend, atomic_write_export
from scout.storage.sqlite_store import StateConflict, StateError, Store, state_checksum

SHA1 = "a" * 40
SHA2 = "b" * 40
BASE = "/repos/owner/private"
FILE = BASE + "/contents/state/scout.json.gz"


class FakeGitHub:
    def __init__(self, payload):
        self.payload = payload
        self.private = True
        self.branch = False
        self.blob = None
        self.sha = None
        self.conflict = False
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == "api.github.com"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        assert request.headers["Authorization"] == "Bearer opaque-token"
        path = request.url.path
        if path == BASE:
            return httpx.Response(200, json={"private": self.private, "default_branch": "main"})
        if path == BASE + "/git/ref/heads/scout-state":
            return httpx.Response(200, json={"object": {"sha": SHA1}}) if self.branch else httpx.Response(404)
        if path == BASE + "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": SHA1}})
        if path == BASE + "/git/refs" and request.method == "POST":
            assert json.loads(request.content)["ref"] == "refs/heads/scout-state"
            self.branch = True
            return httpx.Response(201, json={"object": {"sha": SHA1}})
        if path == FILE and request.method == "GET":
            if not self.branch or self.blob is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"type": "file", "sha": self.sha,
                                             "encoding": "base64", "content": self.blob})
        if path == FILE and request.method == "PUT":
            if self.conflict:
                return httpx.Response(409, json={"message": "private body; do not report"})
            data = json.loads(request.content)
            assert data["branch"] == "scout-state"
            if self.blob is None:
                assert "sha" not in data
            else:
                assert data["sha"] == self.sha
            self.blob = data["content"]
            self.sha = SHA2
            self.branch = True
            return httpx.Response(200, json={"content": {"sha": SHA2}})
        raise AssertionError((request.method, path))


@pytest.fixture
def snapshot(tmp_path):
    with Store(tmp_path / "state.db", create=True) as db:
        db.set_meta("digest", {"last": "2026-10-19"})
        return db.export_state()


def backend(fake):
    client = httpx.Client(transport=httpx.MockTransport(fake), trust_env=False,
                          follow_redirects=False)
    return GitHubStateBackend("owner/private", "opaque-token", client=client)


@pytest.mark.parametrize("missing", ["SCOUT_STATE_REPOSITORY", "SCOUT_STATE_TOKEN", "SCOUT_STATE_BRANCH"])
def test_runtime_requires_independent_cloud_state_configuration(tmp_path, monkeypatch, missing):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/public-source")
    monkeypatch.setenv("GITHUB_TOKEN", "public-workflow-token")
    monkeypatch.setenv("SCOUT_STATE_REPOSITORY", "owner/private-state")
    monkeypatch.setenv("SCOUT_STATE_TOKEN", "private-state-token")
    monkeypatch.setenv("SCOUT_STATE_BRANCH", "scout-state")
    monkeypatch.delenv(missing)

    with pytest.raises(StateError, match=missing):
        Runtime(tmp_path / "data", Path("config"), cloud_state=True)


def test_runtime_rejects_public_workflow_repository_as_state_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "Owner/Public-Source")
    monkeypatch.setenv("SCOUT_STATE_REPOSITORY", "owner/public-source")
    monkeypatch.setenv("SCOUT_STATE_TOKEN", "private-state-token")
    monkeypatch.setenv("SCOUT_STATE_BRANCH", "scout-state")

    with pytest.raises(StateError, match="differ from GITHUB_REPOSITORY"):
        Runtime(tmp_path / "data", Path("config"), cloud_state=True)


def test_runtime_uses_explicit_private_state_backend(tmp_path, monkeypatch, snapshot):
    fake = FakeGitHub(snapshot)
    client = httpx.Client(transport=httpx.MockTransport(fake), trust_env=False,
                          follow_redirects=False)

    class MockedBackend(GitHubStateBackend):
        def __init__(self, repository, token, branch):
            assert repository == "owner/private"
            assert token == "opaque-token"
            assert branch == "scout-state"
            super().__init__(repository, token, branch, client=client)

    monkeypatch.setattr("scout.runtime.GitHubStateBackend", MockedBackend)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/public-source")
    monkeypatch.setenv("GITHUB_TOKEN", "public-workflow-token")
    monkeypatch.setenv("SCOUT_STATE_REPOSITORY", "owner/private")
    monkeypatch.setenv("SCOUT_STATE_TOKEN", "opaque-token")
    monkeypatch.setenv("SCOUT_STATE_BRANCH", "scout-state")

    runtime = Runtime(tmp_path / "data", Path("config"), cloud_state=True, bootstrap=True)
    try:
        runtime.checkpoint()
        assert runtime.backend.repository == "owner/private"
        assert fake.branch
    finally:
        runtime.close()


def test_bootstrap_roundtrip_and_compare_and_swap(snapshot):
    fake = FakeGitHub(snapshot)
    with backend(fake) as service:
        with pytest.raises(StateError, match="missing"):
            service.load()
        assert service.load(bootstrap=True) is None
        assert fake.branch
        service.save(snapshot)
        assert service.load() == snapshot
        fake.sha = SHA1  # Another writer replaced the checkpoint.
        with pytest.raises(StateConflict):
            service.save(snapshot)


def test_private_check_every_operation_and_conflict_body_hidden(snapshot):
    fake = FakeGitHub(snapshot)
    with backend(fake) as service:
        fake.private = False
        with pytest.raises(StateError, match="private"):
            service.load(bootstrap=True)
        assert not fake.branch
        fake.private = True
        service.load(bootstrap=True)
        fake.private = False
        with pytest.raises(StateError, match="private"):
            service.save(snapshot)
        fake.private = True
        fake.conflict = True
        with pytest.raises(StateConflict) as err:
            service.save(snapshot)
        assert "private body" not in str(err.value)


def test_missing_corrupt_checksum_and_compression_bomb(snapshot):
    fake = FakeGitHub(snapshot)
    fake.branch = True
    fake.sha = SHA1
    with backend(fake) as service:
        fake.blob = "not-base64!"
        with pytest.raises(StateError):
            service.load()
        bad = copy.deepcopy(snapshot)
        bad["meta"]["digest"] = "tampered"
        fake.blob = base64.b64encode(gzip.compress(json.dumps(bad).encode())).decode()
        with pytest.raises(StateError, match="checksum"):
            service.load()
        huge = b"x" * (16 * 1024 * 1024 + 1)
        fake.blob = base64.b64encode(gzip.compress(huge)).decode()
        with pytest.raises(StateError, match="oversized"):
            service.load()
        bad["checksum"] = state_checksum({k: v for k, v in bad.items() if k != "checksum"})
        fake.blob = base64.b64encode(gzip.compress(json.dumps(bad).encode())).decode()
        assert service.load() == bad


def test_invalid_repository_redirect_and_unauthorized(snapshot):
    for name in ("owner", "../private", "owner/../private", "owner/private/extra"):
        with pytest.raises(ValueError):
            GitHubStateBackend(name, "opaque-token")
    def redirect(request):
        if request.url.path == BASE:
            return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
        raise AssertionError("redirect followed")
    with backend(redirect) as service:
        with pytest.raises(StateError, match="302"):
            service.load()


def test_reject_payload_before_remote_write(snapshot):
    fake = FakeGitHub(snapshot)
    with backend(fake) as service:
        service.load(bootstrap=True)
        bad = dict(snapshot, checksum="0" * 64)
        count = len(fake.requests)
        with pytest.raises(StateError):
            service.save(bad)
        assert len(fake.requests) == count


def test_requested_local_export_is_validated_and_replaces_atomically(tmp_path, snapshot):
    path = tmp_path / "snapshot.json"
    path.write_text("previous", encoding="utf-8")
    bad = dict(snapshot, checksum="0" * 64)
    with pytest.raises(StateError):
        atomic_write_export(path, bad)
    assert path.read_text(encoding="utf-8") == "previous"
    atomic_write_export(path, snapshot)
    assert json.loads(path.read_text(encoding="utf-8")) == snapshot

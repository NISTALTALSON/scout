"""Private GitHub Contents API checkpoint with optimistic blob-SHA concurrency."""

import base64
import binascii
import gzip
import json
import re
import zlib
from pathlib import Path
from urllib.parse import quote

import httpx

from .sqlite_store import StateConflict, StateError, validate_state

API = "https://api.github.com"
MAX_COMPRESSED = 900_000
MAX_UNCOMPRESSED = 16 * 1024 * 1024
SHA = re.compile(r"[a-fA-F0-9]{40}\Z")


class GitHubStateBackend:
    def __init__(self, repository: str, token: str, branch: str = "scout-state",
                 path: str = "state/scout.json.gz",
                 client: httpx.Client | httpx.BaseTransport | None = None):
        parts = repository.split("/") if isinstance(repository, str) else []
        if len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", parts[0]) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}", parts[1]
        ) or parts[1] in (".", ".."):
            raise ValueError("repository must be OWNER/REPO")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("a GitHub API token is required")
        if not isinstance(branch, str) or not branch or branch.startswith("/") or ".." in branch or re.search(
            r"[\x00-\x20~^:?*\[\\]", branch
        ) or branch.endswith(("/", ".", ".lock")):
            raise ValueError("invalid checkpoint branch")
        if not isinstance(path, str) or not path or path.startswith("/") or any(
            p in ("", ".", "..") for p in path.split("/")
        ) or "\\" in path or any(ord(c) < 33 or ord(c) == 127 for c in path):
            raise ValueError("invalid checkpoint path")
        self.repository = repository
        self.branch = branch
        self.path = path
        self._token = token
        # Wrap an injected client's transport rather than inheriting its environment proxies,
        # redirects, base URL, or default Authorization header.
        if isinstance(client, httpx.Client):
            transport = client._transport
        elif client is None or isinstance(client, httpx.BaseTransport):
            transport = client
        else:
            raise TypeError("client must be an httpx.Client or synchronous transport")
        self._client = httpx.Client(transport=transport, trust_env=False,
                                    follow_redirects=False, timeout=15.0)
        self._sha: str | None = None
        self._loaded = False

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def _repo_path(self) -> str:
        return "/repos/" + self.repository

    @property
    def _contents_path(self) -> str:
        return self._repo_path + "/contents/" + quote(self.path, safe="/")

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None, conflict: bool = False) -> dict | None:
        # Never accept a URL supplied by API responses or follow a redirect carrying Authorization.
        if not path.startswith(self._repo_path + "/") and path != self._repo_path:
            raise StateError("invalid GitHub API endpoint")
        try:
            response = self._client.request(
                method, API + path, params=params, json=body,
                headers={"Accept": "application/vnd.github+json",
                         "Authorization": "Bearer " + self._token,
                         "X-GitHub-Api-Version": "2026-03-10"},
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise StateError("GitHub checkpoint request failed") from None
        if conflict and response.status_code in (409, 412, 422):
            raise StateConflict("GitHub checkpoint changed concurrently")
        if response.status_code == 404:
            return None
        if response.status_code < 200 or response.status_code >= 300:
            # Error bodies may contain private data, URLs or echoed headers.
            raise StateError(f"GitHub checkpoint API returned HTTP {response.status_code}")
        try:
            result = response.json()
        except (ValueError, TypeError):
            raise StateError("invalid GitHub checkpoint API response") from None
        if not isinstance(result, dict):
            raise StateError("invalid GitHub checkpoint API response")
        return result

    def _private_repo(self) -> dict:
        repo = self._request("GET", self._repo_path)
        if repo is None:
            raise StateError("GitHub checkpoint repository unavailable")
        if repo.get("private") is not True:
            raise StateError("GitHub checkpoint repository must be private")
        return repo

    def _contents(self) -> dict | None:
        result = self._request("GET", self._contents_path, params={"ref": self.branch})
        if result is not None and (result.get("type") != "file" or not isinstance(result.get("sha"), str)
                                    or not SHA.fullmatch(result["sha"])):
            raise StateError("invalid GitHub checkpoint blob")
        return result

    def _bootstrap(self, repo: dict) -> None:
        ref_path = self._repo_path + "/git/ref/heads/" + quote(self.branch, safe="/")
        if self._request("GET", ref_path) is not None:
            return
        default = repo.get("default_branch")
        if not isinstance(default, str) or not default or default == self.branch:
            raise StateError("GitHub repository has no usable default branch")
        ref = self._request("GET", self._repo_path + "/git/ref/heads/" + quote(default, safe="/"))
        sha = ref.get("object", {}).get("sha") if isinstance(ref, dict) else None
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise StateError("GitHub default branch reference unavailable")
        created = self._request("POST", self._repo_path + "/git/refs",
                                body={"ref": "refs/heads/" + self.branch, "sha": sha}, conflict=True)
        if created is None:
            raise StateError("GitHub checkpoint branch creation failed")

    def load(self, bootstrap: bool = False) -> dict | None:
        repo = self._private_repo()
        content = self._contents()
        if content is None:
            if not bootstrap:
                raise StateError("GitHub checkpoint missing; bootstrap explicitly")
            self._bootstrap(repo)
            self._sha = None
            self._loaded = True
            return None
        if content.get("encoding") != "base64" or not isinstance(content.get("content"), str):
            raise StateError("GitHub checkpoint content is not inline base64")
        try:
            if len(content["content"]) > ((MAX_COMPRESSED + 2) // 3) * 4 + 10000:
                raise ValueError("encoded checkpoint exceeds limit")
            compressed = base64.b64decode(content["content"], validate=False)
            # Validate the same alphabet after removing only API-provided line breaks.
            if not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", content["content"].replace("\n", "")):
                raise ValueError("invalid base64")
            if len(compressed) > MAX_COMPRESSED:
                raise ValueError("compressed checkpoint exceeds limit")
            unzip = zlib.decompressobj(16 + zlib.MAX_WBITS)
            raw = unzip.decompress(compressed, MAX_UNCOMPRESSED + 1)
            if len(raw) > MAX_UNCOMPRESSED or unzip.unconsumed_tail or not unzip.eof or unzip.unused_data:
                raise ValueError("oversize, truncated, or trailing gzip payload")
            raw += unzip.flush(MAX_UNCOMPRESSED - len(raw) + 1)
            if len(raw) > MAX_UNCOMPRESSED:
                raise ValueError("uncompressed checkpoint exceeds limit")
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, binascii.Error, zlib.error, OverflowError):
            raise StateError("invalid or oversized GitHub checkpoint") from None
        validated = validate_state(payload)
        self._sha = content["sha"]
        self._loaded = True
        return validated

    def save(self, payload: dict) -> None:
        state = validate_state(payload)
        if not self._loaded:
            raise StateError("load the GitHub checkpoint before saving")
        raw = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_UNCOMPRESSED:
            raise StateError("checkpoint exceeds uncompressed limit")
        compressed = gzip.compress(raw, mtime=0)
        if len(compressed) > MAX_COMPRESSED:
            raise StateError("checkpoint exceeds compressed limit")
        self._private_repo()
        latest = self._contents()
        current = latest["sha"] if latest else None
        if current != self._sha:
            raise StateConflict("GitHub checkpoint changed concurrently")
        body = {"message": "Checkpoint scout private state", "branch": self.branch,
                "content": base64.b64encode(compressed).decode("ascii")}
        if self._sha is not None:
            body["sha"] = self._sha
        response = self._request("PUT", self._contents_path, body=body, conflict=True)
        sha = response.get("content", {}).get("sha") if isinstance(response, dict) else None
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise StateError("GitHub checkpoint write could not be verified")
        self._sha = sha


def atomic_write_export(path: str | Path, payload: dict) -> None:
    """Write an explicitly requested local JSON export without partial-file exposure."""
    import os
    import tempfile

    state = validate_state(payload)
    target = Path(path)
    if not target.parent.is_dir():
        raise StateError("export directory is missing")
    fd, temporary = tempfile.mkstemp(prefix=".scout-export-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

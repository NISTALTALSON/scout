"""Sequential, budgeted HTTP; network backend pins public DNS answers to TCP sockets."""

import ipaddress
import socket
import threading
import time
import zlib
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpcore
import httpx
from httpcore._backends.sync import SyncBackend

from scout.budgets import BudgetExceeded
from scout.models import FetchResult
from scout.urls import normalize_url


class FetchError(RuntimeError):
    """Safe, URL-free failure suitable for storing in source failure history."""

    def __init__(self, reason: str, status_code: int | None = None, backoff_seconds: int = 0):
        super().__init__(reason)
        self.status_code = status_code
        self.backoff_seconds = backoff_seconds


def _public(ip: str) -> str:
    try:
        address = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError as exc:
        raise FetchError("DNS returned an invalid address") from exc
    if not address.is_global or getattr(address, "ipv4_mapped", None):
        raise FetchError("DNS returned a non-public address")
    return address.compressed


def _resolve(host: str, port: int) -> list[str]:
    return [entry[4][0] for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


class _PinnedBackend(SyncBackend):
    def __init__(self, resolver):
        self.resolver = resolver

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # httpcore retains the original host for Host and TLS server_hostname;
        # only the TCP destination is replaced. Revalidate on EVERY connection.
        try:
            addresses = self.resolver(host, port)
            addresses = [_public(item[4][0] if isinstance(item, tuple) else item)
                         for item in addresses]
        except (OSError, ValueError) as exc:
            raise FetchError("DNS resolution failed") from exc
        if not addresses:
            raise FetchError("DNS returned no addresses")
        return super().connect_tcp(addresses[0], port, timeout, local_address, socket_options)


class _PinnedTransport(httpx.HTTPTransport):
    def __init__(self, resolver):
        super().__init__(trust_env=False, retries=0)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(network_backend=_PinnedBackend(resolver), retries=0,
                                             max_connections=1, max_keepalive_connections=0)


def _retry_after(header: str | None) -> int:
    if not header:
        return 3600
    try:
        return min(86400, max(60, int(header)))
    except ValueError:
        try:
            return min(86400, max(60, int((parsedate_to_datetime(header) - datetime.now(UTC)).total_seconds())))
        except (TypeError, ValueError, OverflowError):
            return 3600


class SafeHttpClient:
    def __init__(self, budget, max_response_bytes=2097152, transport=None, resolver=None):
        self.budget = budget
        self.max_response_bytes = max_response_bytes
        self._client = httpx.Client(transport=transport or _PinnedTransport(resolver or _resolve),
                                    trust_env=False, follow_redirects=False)
        self._robots: dict[str, RobotFileParser | bool] = {}
        self._robots_errors: dict[str, FetchError] = {}
        self._last_request: dict[str, float] = {}
        self._lock = threading.Lock()

    def close(self):
        self._client.close()

    def _request(self, url, headers, rate_limit_seconds):
        host = urlsplit(url).hostname
        self.budget.check()
        with self._lock:  # no simultaneous requests, even from independent callers
            delay = max(0, self._last_request.get(host, 0) + rate_limit_seconds - time.monotonic())
            if delay:
                if delay >= self.budget.remaining_seconds:
                    raise BudgetExceeded("duration_budget")
                time.sleep(delay)
            self.budget.consume_http()
            self._last_request[host] = time.monotonic()
            try:
                request_headers = dict(headers)
                request_headers["Accept-Encoding"] = "identity"
                with self._client.stream("GET", url, headers=request_headers,
                                         timeout=min(10, self.budget.remaining_seconds)) as response:
                    if response.status_code in (403, 429):
                        raise FetchError("HTTP access denied or rate limited", response.status_code,
                                         _retry_after(response.headers.get("retry-after")))
                    if response.status_code >= 500:
                        raise FetchError("HTTP server unavailable", response.status_code, 3600)
                    coding = response.headers.get("content-encoding", "identity").lower().strip()
                    if coding not in {"identity", "", "gzip", "deflate"}:
                        raise FetchError("unsupported response encoding")
                    decoder = (zlib.decompressobj(16 + zlib.MAX_WBITS if coding == "gzip" else
                               zlib.MAX_WBITS) if coding in {"gzip", "deflate"} else None)
                    content = bytearray()
                    encoded = 0
                    # Offline injected transports may return an already-buffered Response.
                    raw_chunks = ((response.content,) if response.is_stream_consumed else
                                  response.iter_raw(chunk_size=16384))
                    for chunk in raw_chunks:
                        encoded += len(chunk)
                        if encoded > self.max_response_bytes:
                            raise FetchError("response body exceeds byte limit")
                        try:
                            decoded = decoder.decompress(chunk, self.max_response_bytes - len(content) + 1) if decoder else chunk
                        except zlib.error as exc:
                            raise FetchError("invalid compressed response") from exc
                        if (len(content) + len(decoded) > self.max_response_bytes or
                                decoder and (decoder.unconsumed_tail or decoder.unused_data)):
                            raise FetchError("response body exceeds byte limit")
                        content.extend(decoded)
                        self.budget.check()
                    if decoder:
                        tail = decoder.flush(self.max_response_bytes - len(content) + 1)
                        if not decoder.eof or len(content) + len(tail) > self.max_response_bytes:
                            raise FetchError("invalid or oversized compressed response")
                        content.extend(tail)
                    return FetchResult(url=url, status_code=response.status_code,
                                       headers=dict(response.headers), content=bytes(content))
            except httpx.HTTPError as exc:
                raise FetchError("HTTP transport failed", backoff_seconds=3600) from exc

    def _follow(self, url, headers, rate_limit_seconds):
        seen = set()
        for _ in range(6):  # initial request plus at most five redirects
            url = normalize_url(url)
            if url in seen:
                raise FetchError("redirect cycle")
            seen.add(url)
            result = self._request(url, headers, rate_limit_seconds)
            if result.status_code not in (301, 302, 303, 307, 308):
                return result
            location = result.headers.get("location")
            if not location:
                raise FetchError("redirect missing location")
            url = normalize_url(urljoin(url, location))
            # Conditional validators belong to the original resource only.
            headers = {k: v for k, v in headers.items() if k.lower() not in
                       {"if-none-match", "if-modified-since"}}
        raise FetchError("redirect limit exceeded")

    def _allowed(self, url, rate_limit_seconds):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            try:
                robots = self._follow(origin + "/robots.txt", {}, rate_limit_seconds)
                if robots.status_code == 404:
                    self._robots[origin] = True
                elif robots.status_code != 200:
                    self._robots[origin] = False
                else:
                    parser = RobotFileParser()
                    parser.parse(robots.text.splitlines())
                    self._robots[origin] = parser
            except FetchError as exc:
                self._robots_errors[origin] = FetchError("robots retrieval failed", exc.status_code,
                                                         exc.backoff_seconds or 3600)
                self._robots[origin] = False
            except ValueError:
                self._robots[origin] = False
                self._robots_errors[origin] = FetchError("robots redirect unsafe", backoff_seconds=3600)
        if origin in self._robots_errors:
            raise self._robots_errors[origin]
        policy = self._robots[origin]
        return policy is True or (policy is not False and policy.can_fetch("ScoutOpportunityAgent", url))

    def get(self, url, headers=None, rate_limit_seconds=2, respect_robots=True) -> FetchResult:
        url = normalize_url(url)
        if respect_robots and not self._allowed(url, rate_limit_seconds):
            raise FetchError("robots policy unavailable or disallows URL", backoff_seconds=3600)
        # Every redirected destination has its own robots policy checked before connecting.
        if not respect_robots:
            return self._follow(url, dict(headers or {}), rate_limit_seconds)
        seen = set()
        request_headers = dict(headers or {})
        for _ in range(6):
            url = normalize_url(url)
            if url in seen:
                raise FetchError("redirect cycle")
            seen.add(url)
            if not self._allowed(url, rate_limit_seconds):
                raise FetchError("robots policy unavailable or disallows redirect", backoff_seconds=3600)
            result = self._request(url, request_headers, rate_limit_seconds)
            if result.status_code not in (301, 302, 303, 307, 308):
                return result
            if not result.headers.get("location"):
                raise FetchError("redirect missing location")
            previous_origin = urlsplit(url).netloc
            url = normalize_url(urljoin(url, result.headers["location"]))
            if urlsplit(url).netloc != previous_origin:
                request_headers = {}
            request_headers = {k: v for k, v in request_headers.items() if k.lower() not in
                               {"if-none-match", "if-modified-since"}}
        raise FetchError("redirect limit exceeded")

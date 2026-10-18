"""Normalize externally supplied HTTP URLs without granting publisher trust."""

import ipaddress
from urllib.parse import urlsplit, urlunsplit


def normalize_url(url: str) -> str:
    if not isinstance(url, str) or not url or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValueError("unsafe URL characters")
    if "\\" in url:
        raise ValueError("backslash in URL")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        if parts.scheme.lower() not in {"http", "https"} or not host:
            raise ValueError("absolute HTTP(S) URL required")
        if "@" in parts.netloc or port not in {None, 80, 443}:
            raise ValueError("credentials or non-web port")
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or host.startswith(".") or ".." in host or host == "localhost" or host.endswith(".localhost"):
            raise ValueError("invalid host")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Reject non-canonical numeric host aliases (inet_aton octal/hex/single-integer).
            if host.replace(".", "").isdigit() or host.startswith("0x"):
                raise ValueError("ambiguous numeric host") from None
        else:
            if not address.is_global or getattr(address, "ipv4_mapped", None):
                raise ValueError("non-public IP address")
            host = f"[{address.compressed}]" if address.version == 6 else str(address)
        authority = host + (f":{port}" if port is not None else "")
        return urlunsplit((parts.scheme.lower(), authority, parts.path or "/", parts.query, ""))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("invalid or unsafe URL") from exc


def is_official_url(url: str, domains: list[str]) -> bool:
    try:
        host = urlsplit(normalize_url(url)).hostname
        return host in {domain.lower().rstrip(".") for domain in domains}
    except ValueError:
        return False

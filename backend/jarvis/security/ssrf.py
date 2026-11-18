"""SSRF-safe HTTP fetching for website verification probes.

* http/https only, ports 80/443
* DNS resolved by us; every resolved address must be public (no private, loopback, link-local,
  CGNAT, multicast, reserved, IPv6 ULA, or metadata addresses)
* we connect to the vetted IP (Host header + SNI = original hostname) -> no DNS rebinding window
* every redirect hop is re-validated; max 5 hops
* response size and time limits
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

from jarvis.core.errors import ValidationFailed

USER_AGENT = "JarvisSiteCheck/1.0 (+website availability check)"
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata", "instance-data"}
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class UnsafeDestination(ValidationFailed):
    code = "UNSAFE_DESTINATION"


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_reserved
                or addr.is_unspecified or (isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT)
                or (isinstance(addr, ipaddress.IPv6Address) and addr.is_site_local))


def validate_url_shape(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeDestination(f"scheme not allowed: {parts.scheme!r}")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or host in BLOCKED_HOSTS or host.endswith(".internal") or host.endswith(".local"):
        raise UnsafeDestination(f"host not allowed: {host!r}")
    if parts.username or parts.password:
        raise UnsafeDestination("credentials in URL not allowed")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in (80, 443):
        raise UnsafeDestination(f"port not allowed: {port}")
    try:
        if not is_public_ip(host):
            raise UnsafeDestination(f"non-public IP literal: {host}")
    except ValueError:
        pass  # not an IP literal
    return parts.scheme, host, port


async def resolve_public(host: str, port: int) -> str:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DnsFailure(host, str(exc)) from exc
    ips = sorted({i[4][0] for i in infos})
    if not ips:
        raise DnsFailure(host, "no addresses")
    bad = [ip for ip in ips if not is_public_ip(ip)]
    if bad:
        raise UnsafeDestination(f"{host} resolves to non-public address(es): {bad}")
    v4 = [ip for ip in ips if ":" not in ip]
    return (v4 or ips)[0]


class DnsFailure(Exception):
    def __init__(self, host: str, detail: str) -> None:
        super().__init__(f"DNS failure for {host}: {detail}")
        self.host, self.detail = host, detail


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    headers: dict[str, str]
    body: str
    redirects: list[str] = field(default_factory=list)
    error: str | None = None          # nxdomain | connect_error | tls_error | timeout | too_many_redirects | unsafe
    truncated: bool = False


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """Connects to a pre-vetted IP while keeping hostname-based TLS verification and Host header."""

    def __init__(self, ip: str, **kw) -> None:
        super().__init__(**kw)
        self._ip = ip

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        request.extensions = {**request.extensions, "sni_hostname": host}
        request.url = request.url.copy_with(host=self._ip)
        request.headers["Host"] = host if request.url.port in (None, 80, 443) else f"{host}:{request.url.port}"
        return await super().handle_async_request(request)


async def safe_fetch(url: str, *, method: str = "GET", timeout_s: float = 10.0,
                     resolver=resolve_public) -> FetchResult:
    current, hops = url, []
    for _ in range(MAX_REDIRECTS + 1):
        try:
            _scheme, host, port = validate_url_shape(current)
            ip = await resolver(host, port)
        except UnsafeDestination as exc:
            return FetchResult(url, current, None, {}, "", hops, error=f"unsafe: {exc.message}")
        except DnsFailure:
            return FetchResult(url, current, None, {}, "", hops, error="nxdomain")
        transport = _PinnedTransport(ip, retries=0, verify=ssl.create_default_context())
        try:
            async with httpx.AsyncClient(transport=transport, timeout=timeout_s, follow_redirects=False,
                                         headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}) as client:
                async with client.stream(method, current) as resp:
                    chunks, size, truncated = [], 0, False
                    if method != "HEAD":
                        async for chunk in resp.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_BYTES:
                                truncated = True
                                break
                            chunks.append(chunk)
                    body = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    if resp.status_code in (301, 302, 303, 307, 308) and "location" in headers:
                        hops.append(current)
                        current = urljoin(current, headers["location"])
                        continue
                    return FetchResult(url, current, resp.status_code, headers, body, hops, truncated=truncated)
        except httpx.ConnectError as exc:
            err = "tls_error" if "SSL" in str(exc) or "certificate" in str(exc).lower() else "connect_error"
            return FetchResult(url, current, None, {}, "", hops, error=err)
        except httpx.TimeoutException:
            return FetchResult(url, current, None, {}, "", hops, error="timeout")
        except (httpx.HTTPError, ssl.SSLError) as exc:
            return FetchResult(url, current, None, {}, "", hops, error=f"connect_error: {type(exc).__name__}")
    return FetchResult(url, current, None, {}, "", hops, error="too_many_redirects")

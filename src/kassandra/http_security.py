"""Bounded, SSRF-resistant HTTP retrieval for analyst-configured sources."""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

import httpx

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_URL_LENGTH = 8192
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SENSITIVE_REDIRECT_HEADERS = {"authorization", "proxy-authorization", "cookie"}


class URLSafetyError(ValueError):
    """Raised when a URL could reach a non-public network endpoint."""


class ResponseTooLargeError(ValueError):
    """Raised before an oversized response is retained in memory."""


Resolver = Callable[..., list[tuple]]
AddressMap = dict[tuple[str, int], tuple[str, ...]]

_sync_pins: contextvars.ContextVar[AddressMap | None] = contextvars.ContextVar(
    "kassandra_sync_http_pins", default=None
)
_async_pins: contextvars.ContextVar[AddressMap | None] = contextvars.ContextVar(
    "kassandra_async_http_pins", default=None
)


def _normalise_host(host: str) -> str:
    host = host.rstrip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _url_origin(url: str) -> tuple[str, str, int] | None:
    """Return normalized origin tuple, or None when URL authority is invalid."""

    try:
        parsed = urlsplit(url)
        if not parsed.hostname:
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return parsed.scheme, _normalise_host(parsed.hostname), port


def _headers_for_redirect(
    current: str, target: str, headers: dict[str, str] | None
) -> dict[str, str] | None:
    """Do not forward credential-bearing headers across origins or downgrades."""

    if headers is None:
        return None
    try:
        current_parts = urlsplit(current)
        target_parts = urlsplit(target)
    except ValueError as exc:
        raise URLSafetyError("malformed redirect URL") from exc
    if current_parts.scheme == "https" and target_parts.scheme == "http":
        raise URLSafetyError("HTTPS redirect downgrade is forbidden")
    if _url_origin(current) == _url_origin(target):
        return headers
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in SENSITIVE_REDIRECT_HEADERS
    }


class _PinnedSyncBackend:
    """Resolve once in the safety gate, then dial only those addresses.

    httpx/httpcore normally resolves a hostname again while opening a socket.
    Keeping this small backend wrapper at the connection-pool boundary closes
    that resolution gap for standard ``httpx.HTTPTransport`` clients. Custom
    transports (for example ``MockTransport``) continue to work normally.
    """

    def __init__(self, delegate: object):
        self._delegate = delegate

    def connect_tcp(self, host: str, port: int, **kwargs):
        pins = _sync_pins.get()
        addresses = pins.get((_normalise_host(host), port)) if pins else None
        if not addresses:
            return self._delegate.connect_tcp(host, port, **kwargs)

        last_error: Exception | None = None
        for address in addresses:
            try:
                return self._delegate.connect_tcp(address, port, **kwargs)
            except Exception as exc:  # try next public address
                last_error = exc
        assert last_error is not None
        raise last_error

    def connect_unix_socket(self, path: str, **kwargs):
        return self._delegate.connect_unix_socket(path, **kwargs)

    def sleep(self, seconds: float) -> None:
        return self._delegate.sleep(seconds)


class _PinnedAsyncBackend:
    """Async counterpart to :class:`_PinnedSyncBackend`."""

    def __init__(self, delegate: object):
        self._delegate = delegate

    async def connect_tcp(self, host: str, port: int, **kwargs):
        pins = _async_pins.get()
        addresses = pins.get((_normalise_host(host), port)) if pins else None
        if not addresses:
            return await self._delegate.connect_tcp(host, port, **kwargs)

        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._delegate.connect_tcp(address, port, **kwargs)
            except Exception as exc:  # try next public address
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(self, path: str, **kwargs):
        return await self._delegate.connect_unix_socket(path, **kwargs)

    async def sleep(self, seconds: float) -> None:
        return await self._delegate.sleep(seconds)


def _pin_standard_transport(client: object, *, asynchronous: bool) -> None:
    """Install a per-client pool wrapper when client uses standard HTTPX transport."""

    transport = getattr(client, "_transport", None)
    pool = getattr(transport, "_pool", None)
    backend = getattr(pool, "_network_backend", None)
    if backend is None:
        return
    wrapper_type = _PinnedAsyncBackend if asynchronous else _PinnedSyncBackend
    if not isinstance(backend, wrapper_type):
        pool._network_backend = wrapper_type(backend)


def _resolve_public_addresses(url: str, *, resolver: Resolver | None = None) -> tuple[str, ...]:
    """Validate URL and return deduplicated public addresses used for dialing."""

    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        raise URLSafetyError("URL is missing or exceeds the maximum length")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise URLSafetyError("malformed URL") from exc
    if parsed.scheme not in {"http", "https"}:
        raise URLSafetyError("only http and https URLs are allowed")
    if not hostname:
        raise URLSafetyError("URL must contain a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise URLSafetyError("credentials in source URLs are forbidden")
    literal_host = hostname.rstrip(".")
    try:
        literal_ip = ipaddress.ip_address(literal_host)
    except ValueError:
        pass
    else:
        if not literal_ip.is_global:
            raise URLSafetyError(f"non-public address is forbidden: {literal_ip}")

    try:
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise URLSafetyError("invalid URL port") from exc
    if not 1 <= port <= 65535:
        raise URLSafetyError("invalid URL port")

    resolve = resolver or socket.getaddrinfo
    try:
        addresses = resolve(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        raise URLSafetyError(f"hostname resolution failed: {hostname}") from exc
    if not addresses:
        raise URLSafetyError(f"hostname has no addresses: {hostname}")

    public: list[str] = []
    for address in addresses:
        try:
            raw_ip = address[4][0]
            ip = ipaddress.ip_address(raw_ip)
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise URLSafetyError("hostname resolution returned an invalid address") from exc
        if not ip.is_global:
            raise URLSafetyError(f"non-public address is forbidden: {ip}")
        text = str(ip)
        if text not in public:
            public.append(text)
    return tuple(public)


def validate_public_http_url(url: str, *, resolver: Resolver | None = None) -> str:
    """Validate scheme, authority, and every currently resolved address.

    Validation is repeated after every redirect. Standard HTTPX transports are
    pinned to addresses returned by this gate; custom transports and network
    paths outside HTTPX still require an egress firewall.
    """

    _resolve_public_addresses(url, resolver=resolver)
    return url


def _declared_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ResponseTooLargeError("invalid Content-Length header") from exc
    if value < 0:
        raise ResponseTooLargeError("invalid negative Content-Length header")
    return value


def _bounded_response(response: httpx.Response, chunks: list[bytes]) -> httpx.Response:
    content = b"".join(chunks)
    return httpx.Response(
        status_code=response.status_code,
        headers=response.headers,
        content=content,
        request=response.request,
        extensions=response.extensions,
    )


def safe_get(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 20,
    max_bytes: int = MAX_RESPONSE_BYTES,
    resolver: Resolver | None = None,
) -> httpx.Response:
    """GET a public URL with validated redirects and a hard body-size cap."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    _pin_standard_transport(client, asynchronous=False)
    current = url
    request_headers = dict(headers) if headers is not None else None
    for redirect_count in range(MAX_REDIRECTS + 1):
        addresses = _resolve_public_addresses(current, resolver=resolver)
        parsed = urlsplit(current)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        token = _sync_pins.set({(_normalise_host(parsed.hostname), port): addresses})
        try:
            response_context = client.stream(
                "GET",
                current,
                headers=request_headers,
                follow_redirects=False,
                timeout=timeout,
            )
            with response_context as response:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        return _bounded_response(response, [])
                    if redirect_count == MAX_REDIRECTS:
                        raise URLSafetyError("redirect limit exceeded")
                    next_url = urljoin(current, location)
                    request_headers = _headers_for_redirect(current, next_url, request_headers)
                    current = next_url
                    continue

                declared = _declared_length(response)
                if declared is not None and declared > max_bytes:
                    raise ResponseTooLargeError(f"response exceeds {max_bytes} bytes")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ResponseTooLargeError(f"response exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                return _bounded_response(response, chunks)
        finally:
            _sync_pins.reset(token)
    raise AssertionError("unreachable")


async def safe_get_async(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 20,
    max_bytes: int = MAX_RESPONSE_BYTES,
    resolver: Resolver | None = None,
) -> httpx.Response:
    """Async counterpart to :func:`safe_get`."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    _pin_standard_transport(client, asynchronous=True)
    current = url
    request_headers = dict(headers) if headers is not None else None
    for redirect_count in range(MAX_REDIRECTS + 1):
        addresses = await asyncio.to_thread(_resolve_public_addresses, current, resolver=resolver)
        parsed = urlsplit(current)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        token = _async_pins.set({(_normalise_host(parsed.hostname), port): addresses})
        try:
            async with client.stream(
                "GET",
                current,
                headers=request_headers,
                follow_redirects=False,
                timeout=timeout,
            ) as response:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        return _bounded_response(response, [])
                    if redirect_count == MAX_REDIRECTS:
                        raise URLSafetyError("redirect limit exceeded")
                    next_url = urljoin(current, location)
                    request_headers = _headers_for_redirect(current, next_url, request_headers)
                    current = next_url
                    continue

                declared = _declared_length(response)
                if declared is not None and declared > max_bytes:
                    raise ResponseTooLargeError(f"response exceeds {max_bytes} bytes")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ResponseTooLargeError(f"response exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                return _bounded_response(response, chunks)
        finally:
            _async_pins.reset(token)
    raise AssertionError("unreachable")

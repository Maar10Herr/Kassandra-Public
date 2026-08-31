"""Security-boundary tests for analyst-configured HTTP sources."""

from __future__ import annotations

import socket

import httpx
import pytest

from kassandra.http_security import (
    MAX_URL_LENGTH,
    ResponseTooLargeError,
    URLSafetyError,
    _normalise_host,
    _PinnedSyncBackend,
    _sync_pins,
    safe_get,
    validate_public_http_url,
)


def resolver_for(address: str):
    def resolve(host: str, port: int, *, type: int):
        assert host
        assert port > 0
        assert type == socket.SOCK_STREAM
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]

    return resolve


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"],
)
def test_private_or_metadata_addresses_are_rejected(address: str) -> None:
    with pytest.raises(URLSafetyError, match="non-public"):
        validate_public_http_url("https://source.example/report", resolver=resolver_for(address))


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/feed",
        "https://user:secret@example.com/",
        "http://127.0.0.1/",
    ],
)
def test_unsafe_url_forms_are_rejected(url: str) -> None:
    with pytest.raises(URLSafetyError):
        validate_public_http_url(url, resolver=resolver_for("93.184.216.34"))


def test_public_address_is_accepted() -> None:
    assert (
        validate_public_http_url("https://example.com/feed", resolver=resolver_for("93.184.216.34"))
        == "https://example.com/feed"
    )


def test_redirect_to_loopback_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(URLSafetyError, match="non-public"):
            safe_get(
                client,
                "https://example.com/start",
                resolver=(
                    lambda host, port, **kwargs: resolver_for(
                        "127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"
                    )(host, port, **kwargs)
                ),
            )


def test_safe_get_disables_client_redirect_following() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(URLSafetyError, match="non-public"):
            safe_get(
                client,
                "https://example.com/start",
                resolver=(
                    lambda host, port, **kwargs: resolver_for(
                        "127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"
                    )(host, port, **kwargs)
                ),
            )


def test_redirect_does_not_forward_credentials_to_another_origin() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "https://cdn.example/report"})
        return httpx.Response(200, content=b"ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = safe_get(
            client,
            "https://source.example/report",
            headers={"Authorization": "Bearer secret"},
            resolver=resolver_for("93.184.216.34"),
        )

    assert response.text == "ok"
    assert seen == [
        ("https://source.example/report", "Bearer secret"),
        ("https://cdn.example/report", None),
    ]


def test_https_redirect_downgrade_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://source.example/report"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(URLSafetyError, match="downgrade"):
            safe_get(
                client,
                "https://source.example/report",
                headers={"Authorization": "Bearer secret"},
                resolver=resolver_for("93.184.216.34"),
            )


def test_declared_oversize_response_is_rejected_before_retention() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "1000"}, content=b"x")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ResponseTooLargeError, match="exceeds"):
            safe_get(
                client,
                "https://example.com/report",
                max_bytes=10,
                resolver=resolver_for("93.184.216.34"),
            )


def test_streamed_oversize_response_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"01234567890")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ResponseTooLargeError, match="exceeds"):
            safe_get(
                client,
                "https://example.com/report",
                max_bytes=10,
                resolver=resolver_for("93.184.216.34"),
            )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:" + "0",
        "https://example.com:" + "65536",
        "https://example.com/" + "x" * MAX_URL_LENGTH,
    ],
)
def test_url_bounds_are_rejected(url: str) -> None:
    with pytest.raises(URLSafetyError):
        validate_public_http_url(url, resolver=resolver_for("93.184.216.34"))


def test_standard_transport_dials_only_resolver_pinned_address() -> None:
    calls: list[str] = []

    class Delegate:
        def connect_tcp(self, host: str, port: int, **kwargs):
            calls.append(host)
            return host, port

    backend = _PinnedSyncBackend(Delegate())
    token = _sync_pins.set({("source.example", 443): ("93.184.216.34",)})
    try:
        assert backend.connect_tcp("source.example", 443) == ("93.184.216.34", 443)
    finally:
        _sync_pins.reset(token)
    assert calls == ["93.184.216.34"]


def test_host_pin_normalization_matches_httpx_idna_form() -> None:
    assert _normalise_host("BÜCHER.Example.") == "xn--bcher-kva.example"

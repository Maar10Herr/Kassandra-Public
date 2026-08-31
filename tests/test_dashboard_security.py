"""Regression tests for the local dashboard's browser boundary."""

from __future__ import annotations

from kassandra.dashboard import DashboardHandler


def test_dashboard_markup_uses_nonce_and_no_javascript_urls() -> None:
    handler = object.__new__(DashboardHandler)
    markup = handler._index_html("fixed-nonce")
    assert markup.count('nonce="fixed-nonce"') == 2
    assert "javascript:" not in markup
    assert "onclick=" not in markup
    assert "v0.3.0-dev" not in markup


def test_dashboard_escaper_covers_attribute_delimiters() -> None:
    handler = object.__new__(DashboardHandler)
    markup = handler._index_html("fixed-nonce")
    assert ".replace(/\"/g,'&quot;')" in markup
    assert ".replace(/'/g,'&#39;')" in markup


def test_dashboard_query_ids_are_strict_positive_integers() -> None:
    assert DashboardHandler._query_int({"id": ["42"]}, "id") == 42
    for params in (
        {},
        {"id": []},
        {"id": [""]},
        {"id": ["0"]},
        {"id": ["-1"]},
        {"id": ["1_0"]},
        {"id": ["1", "2"]},
        {"id": ["9" * 20]},
    ):
        assert DashboardHandler._query_int(params, "id") is None


def test_dashboard_security_headers_isolate_browser_context() -> None:
    handler = object.__new__(DashboardHandler)
    headers: dict[str, str] = {}
    handler.send_header = headers.__setitem__
    handler._common_security_headers()
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert headers["X-Frame-Options"] == "DENY"

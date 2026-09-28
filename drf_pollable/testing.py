"""Helpers for testing polling endpoints in *your* project.

Polling is awkward to test by hand: state carries between requests, and the
interesting assertions are about headers rather than bodies. These helpers keep
that out of your test suite.

::

    from drf_pollable.testing import clear_poll_state, poll_until, assert_poll_headers

    def test_order_reaches_delivered(client, order):
        clear_poll_state()
        response = client.get(f"/poll/order-status/{order.pk}/")
        assert_poll_headers(response)
        assert response.json()["value"] == "pending"
"""

from __future__ import annotations

from typing import Any, Callable

from django.core.cache import cache


def clear_poll_state() -> None:
    """Forget every client's backoff / last-poll state.

    Call this between tests: leftover state from a previous test would
    otherwise change the advertised interval or trigger ``enforce_interval``.
    """
    cache.clear()


def poll_once(client, url: str, *, etag: str | None = None, **extra):
    """GET ``url``, optionally sending ``If-None-Match``."""
    headers = dict(extra)
    if etag:
        headers["HTTP_IF_NONE_MATCH"] = etag
    return client.get(url, **headers)


def poll_until(
    client,
    url: str,
    predicate: Callable[[Any], bool],
    *,
    max_polls: int = 10,
    advance: Callable[[int], None] | None = None,
):
    """Poll ``url`` until ``predicate(body)`` is true.

    Returns the response that satisfied the predicate; raises ``AssertionError``
    after ``max_polls``. ``advance`` is called with the poll number between
    requests -- use it to move your fixtures forward (or to advance a clock).
    """
    response = None
    for attempt in range(1, max_polls + 1):
        response = client.get(url)
        if response.status_code == 200 and predicate(_body(response)):
            return response
        if advance is not None:
            advance(attempt)
    raise AssertionError(
        f"Polled {url} {max_polls} times without satisfying the predicate; "
        f"last status {response.status_code if response else 'n/a'}."
    )


def assert_poll_headers(response, *, expect_etag: bool = True) -> None:
    """Assert a response carries the headers a polling client relies on."""
    assert "Retry-After" in response, "polling responses must advertise Retry-After"
    retry_after = response["Retry-After"]
    assert retry_after.isdigit() and int(retry_after) >= 1, f"bad Retry-After: {retry_after!r}"
    if expect_etag:
        assert "ETag" in response, "polling responses must send an ETag for 304 support"


def assert_not_modified(response) -> None:
    """Assert a conditional poll was answered with a bodyless ``304``."""
    assert response.status_code == 304, f"expected 304, got {response.status_code}"
    assert not response.content, "a 304 must not carry a body"


def _body(response) -> Any:
    return response.json() if hasattr(response, "json") else response.data

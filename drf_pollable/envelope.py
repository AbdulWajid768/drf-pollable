"""The JSON body a polling endpoint returns.

By default responses are *enveloped* so the client gets the polling metadata it
needs to pace itself alongside the value::

    {
      "value": "shipped",
      "version": "9f2c...",
      "done": false,
      "next_poll_in": 5,
      "polled_at": "2026-09-28T10:12:03.114Z"
    }

Set ``envelope=False`` to return the bare value instead; the same metadata is
still available in the ``ETag`` and ``Retry-After`` headers.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone

#: Envelope keys the package owns. A source's ``extra`` may not overwrite these.
RESERVED_KEYS = frozenset({"value", "version", "done", "next_poll_in", "polled_at"})


def build_envelope(result, *, version: str, next_poll_in: int, config: dict[str, Any]) -> Any:
    """Build the response body for ``result``."""
    if not config["envelope"]:
        return result.value

    body: dict[str, Any] = {
        "value": result.value,
        "version": version,
        "done": bool(result.done),
        "next_poll_in": next_poll_in,
        "polled_at": timezone.now().isoformat(),
    }

    if result.progress is not None:
        body["progress"] = result.progress
    if result.eta_seconds is not None:
        body["eta_seconds"] = result.eta_seconds

    # Source-supplied extras (display label, count, cursor, ...). Reserved keys
    # are dropped rather than silently shadowing the envelope contract.
    for key, value in (result.extra or {}).items():
        if key not in RESERVED_KEYS:
            body[key] = value

    return body

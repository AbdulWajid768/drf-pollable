"""HTTP conditional-request helpers.

Polling is only cheap if an unchanged poll is cheap. These helpers implement the
``ETag`` / ``If-None-Match`` and ``Last-Modified`` / ``If-Modified-Since``
handshake that lets an unchanged poll return a bodyless ``304``.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime, timezone
from typing import Any

from django.utils.http import http_date, parse_etags, parse_http_date_safe, quote_etag


def make_version(payload: Any) -> str:
    """Derive a short, stable version token by hashing ``payload``.

    Deliberately hashes the *value* rather than trusting a timestamp field. An
    ``auto_now`` column does not fire on ``QuerySet.update()``, so a
    timestamp-derived token would miss real changes and hand the client a stale
    ``304``. Hashing costs almost nothing here -- the value is already loaded.
    """
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.blake2b(blob.encode("utf-8"), digest_size=10).hexdigest()


def etag_for(version: str) -> str:
    """Return a correctly quoted strong ETag for ``version``."""
    return quote_etag(str(version))


def if_none_match_matches(request, etag: str) -> bool:
    """True when the client's ``If-None-Match`` already holds ``etag``."""
    header = request.META.get("HTTP_IF_NONE_MATCH")
    if not header:
        return False
    header = header.strip()
    if header == "*":
        return True
    try:
        client_etags = parse_etags(header)
    except (ValueError, TypeError):
        return False
    # Compare weak-insensitively: W/"x" and "x" refer to the same version here.
    target = _strip_weak(etag)
    return any(_strip_weak(candidate) == target for candidate in client_etags)


def if_modified_since_satisfied(
    request, last_modified: datetime | None, now: float | None = None
) -> bool:
    """True when the resource provably has not changed since the client's copy.

    ``Last-Modified`` is a *weak* validator: HTTP dates carry only one-second
    resolution, so a value that changes in the same second the client fetched
    it is indistinguishable from one that did not. Requiring the modification
    to sit in a strictly earlier second than this request closes that window --
    the cost is a single full response for a value that just changed, instead
    of a ``304`` that strands the client on a stale copy.
    """
    if last_modified is None:
        return False
    header = request.META.get("HTTP_IF_MODIFIED_SINCE")
    if not header:
        return False
    since = parse_http_date_safe(header)
    if since is None:
        return False
    modified_at = int(last_modified.timestamp())
    if modified_at > since:
        return False
    # Within the current second the timestamp cannot be trusted yet.
    return modified_at < int(time.time() if now is None else now)


def format_last_modified(value: datetime) -> str:
    return http_date(value.timestamp())


def format_sunset(value: date | datetime | str) -> str:
    """Format a ``Sunset`` header value (RFC 8594 -- an HTTP date)."""
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return http_date(moment.timestamp())


def _strip_weak(etag: str) -> str:
    etag = etag.strip()
    if etag.startswith(("W/", "w/")):
        etag = etag[2:]
    return etag.strip('"')

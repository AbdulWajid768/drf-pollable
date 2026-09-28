"""Interval negotiation between client and server.

The rule, in one line: **the client may always ask to poll more slowly, never
faster than the server allows.**

* ``min_poll_interval`` is the server's hard floor (self-protection).
* ``suggested_poll_interval`` is what we advertise via ``Retry-After``.
* Exponential backoff grows the suggestion while the value keeps coming back
  unchanged, so idle clients quiet down on their own.
* Jitter spreads synchronised clients so they do not re-poll in lockstep.
"""

from __future__ import annotations

import random
from typing import Any


def parse_requested_interval(request, config: dict[str, Any]) -> float | None:
    """Read the client's preferred cadence from the query string.

    Garbage input is ignored rather than raising -- a malformed ``?interval=``
    should not break a status poll.
    """
    param = config.get("client_interval_param")
    if not param:
        return None
    raw = request.query_params.get(param) if hasattr(request, "query_params") else None
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def compute_next_interval(
    config: dict[str, Any],
    *,
    unchanged_streak: int = 0,
    requested: float | None = None,
    apply_jitter: bool = True,
) -> int:
    """Return the interval (seconds) to advertise to the client.

    ``unchanged_streak`` is how many consecutive polls have returned the same
    version; it drives exponential backoff.
    """
    base = float(config["suggested_poll_interval"])

    if config["backoff"] == "exponential" and unchanged_streak > 0:
        factor = float(config["backoff_factor"]) ** unchanged_streak
        base = min(base * factor, float(config["backoff_max"]))

    # The client may request a *slower* cadence; we take the larger of the two
    # so a client request can never undercut the server's current suggestion.
    if requested is not None:
        base = max(base, float(requested))

    floor = float(config["min_poll_interval"])
    ceiling = float(config["max_poll_interval"])
    base = max(floor, min(base, ceiling))

    if apply_jitter:
        base = _jitter(base, float(config.get("jitter") or 0.0), floor=floor)

    return max(1, int(round(base)))


def _jitter(value: float, fraction: float, *, floor: float) -> float:
    """Spread ``value`` by +/- ``fraction`` without dropping below ``floor``."""
    if fraction <= 0:
        return value
    spread = value * fraction
    return max(floor, value + random.uniform(-spread, spread))

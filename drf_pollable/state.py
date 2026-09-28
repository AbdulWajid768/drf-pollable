"""Per-client polling state, kept in the Django cache.

Two things need a little memory between polls:

* **backoff** -- how many consecutive polls returned the same version, so the
  suggested interval can grow while nothing is happening;
* **interval enforcement** -- when this client last polled this object, so a
  too-early poll can be rejected with ``429``.

State is best-effort. If the cache is cleared or unavailable we simply behave as
if the client is polling for the first time, which is always safe.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

from django.core.cache import caches

#: State is short-lived; a client that stops polling should not linger forever.
STATE_TTL = 3600


@dataclass
class ClientPollState:
    """What we remember about one client polling one object."""

    version: str | None = None
    #: Consecutive polls that returned the *same* version -- drives backoff.
    streak: int = 0
    #: Consecutive polls that returned a *different* version -- drives the
    #: "this data changes fast enough to deserve a websocket" hint.
    change_streak: int = 0
    last_poll: float | None = None

    def seconds_since_last_poll(self, now: float | None = None) -> float | None:
        if self.last_poll is None:
            return None
        return (now if now is not None else time.time()) - self.last_poll


def client_identifier(request) -> str:
    """Stable id for the caller: user pk when authenticated, else client IP.

    Falls back to ``"anon"`` so a missing ``REMOTE_ADDR`` (some test clients,
    some proxies) never raises.
    """
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return f"u{user.pk}"

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        # Left-most entry is the original client.
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.META.get("REMOTE_ADDR")
    return f"ip{ip}" if ip else "anon"


def state_key(endpoint_name: str, request, object_id: Any = None) -> str:
    scope = object_id if object_id is not None else "-"
    raw = f"{endpoint_name}|{client_identifier(request)}|{scope}"
    digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()
    return f"drf_pollable:state:{digest}"


class PollStateStore:
    """Thin wrapper over a Django cache alias."""

    def __init__(self, cache_alias: str = "default"):
        self.cache_alias = cache_alias

    @property
    def cache(self):
        return caches[self.cache_alias]

    def get(self, key: str) -> ClientPollState:
        try:
            raw = self.cache.get(key)
        except Exception:  # pragma: no cover - a broken cache must not 500 a poll
            return ClientPollState()
        if not isinstance(raw, dict):
            return ClientPollState()
        return ClientPollState(
            version=raw.get("version"),
            streak=int(raw.get("streak") or 0),
            change_streak=int(raw.get("change_streak") or 0),
            last_poll=raw.get("last_poll"),
        )

    def record(self, key: str, *, version: str, previous: ClientPollState) -> ClientPollState:
        """Persist this poll, growing the unchanged streak when appropriate."""
        unchanged = previous.version == version
        # A first-ever poll (no stored version) counts as neither a repeat nor
        # a change, so both streaks start at zero.
        first_poll = previous.version is None
        new_state = ClientPollState(
            version=version,
            streak=previous.streak + 1 if unchanged else 0,
            change_streak=0 if (unchanged or first_poll) else previous.change_streak + 1,
            last_poll=time.time(),
        )
        self._write(key, new_state)
        return new_state

    def touch(self, key: str) -> None:
        """Record a poll attempt without changing either streak."""
        state = self.get(key)
        state.last_poll = time.time()
        self._write(key, state)

    def _write(self, key: str, state: ClientPollState) -> None:
        try:
            self.cache.set(
                key,
                {
                    "version": state.version,
                    "streak": state.streak,
                    "change_streak": state.change_streak,
                    "last_poll": state.last_poll,
                },
                STATE_TTL,
            )
        except Exception:  # pragma: no cover - best effort only
            pass


#: Module-level default store, used unless an endpoint overrides the alias.
default_store = PollStateStore()

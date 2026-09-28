"""The generic polling view.

One read-only view serves every registered endpoint. It handles, in order:

1. the kill switch,
2. authentication / permissions / throttling (plain DRF),
3. resolving the source (optionally from cache),
4. object-level permission checks,
5. version + interval negotiation,
6. the conditional-request handshake (``304`` when nothing changed),
7. headers: ``ETag``, ``Last-Modified``, ``Retry-After``, ``Sunset``.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Any

from django.core.cache import cache as default_cache
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .envelope import build_envelope
from .headers import (
    etag_for,
    format_last_modified,
    format_sunset,
    if_modified_since_satisfied,
    if_none_match_matches,
    make_version,
)
from .intervals import compute_next_interval, parse_requested_interval
from .sources import PollResult
from .state import client_identifier, default_store, state_key


class PollResponseMixin:
    """The polling request/response cycle, independent of how it is routed.

    Both the generated :class:`PollView` and the user-facing ``PollingMixin``
    inherit this, so a hand-written ViewSet gets byte-identical behaviour to a
    generated endpoint. Implementors must provide ``registration`` and
    ``poll_config``.
    """

    #: Set on the generated subclass (or by PollingMixin).
    registration = None
    poll_config: dict[str, Any] = {}

    def poll_response(self, request, **kwargs):
        config = self.poll_config

        if not config["enabled"]:
            return self._disabled_response(config)

        result, version = self._resolve(request, kwargs, config)

        # Interval negotiation. Record first so the streak reflects this poll.
        key = state_key(self.registration.name, request, self._object_id(kwargs))
        previous = default_store.get(key)
        state = default_store.record(key, version=version, previous=previous)
        next_poll_in = compute_next_interval(
            config,
            unchanged_streak=state.streak,
            requested=parse_requested_interval(request, config),
        )

        etag = etag_for(version) if config["send_etag"] else None
        last_modified = result.last_modified if config["send_last_modified"] else None

        if self._is_not_modified(request, etag, last_modified, config):
            response = Response(status=status.HTTP_304_NOT_MODIFIED)
        else:
            body = build_envelope(result, version=version, next_poll_in=next_poll_in, config=config)
            response = Response(body, status=status.HTTP_200_OK)

        return self._apply_headers(
            response,
            etag=etag,
            last_modified=last_modified,
            next_poll_in=next_poll_in,
            config=config,
            change_streak=state.change_streak,
        )

    # -- resolution -------------------------------------------------------
    def _resolve(self, request, kwargs, config) -> tuple[PollResult, str]:
        """Produce the current value + version, using the cache when enabled."""
        cache_key = self._value_cache_key(request, kwargs, config)

        if cache_key:
            cached = default_cache.get(cache_key)
            if cached is not None:
                return self._result_from_cache(cached), cached["version"]

        result = self.registration.source.get_result(request, **kwargs)

        # Object-level permissions run against the real instance, so an endpoint
        # can reuse the project's existing IsOwner-style permission classes.
        if result.instance is not None:
            self.check_object_permissions(request, result.instance)

        version = make_version(result.value)

        if cache_key:
            default_cache.set(
                cache_key, self._result_to_cache(result, version), config["cache_timeout"]
            )

        return result, version

    def _value_cache_key(self, request, kwargs, config) -> str | None:
        if not config["cache_timeout"]:
            return None
        parts = [
            "drf_pollable:value",
            self.registration.name,
            ":".join(f"{k}={v}" for k, v in sorted(kwargs.items())),
        ]
        if config["cache_vary_on_user"]:
            parts.append(client_identifier(request))
        # A cursor changes the payload, so it must change the key.
        since = request.query_params.get("since")
        if since:
            parts.append(f"since={since}")
        return "|".join(parts)

    @staticmethod
    def _result_to_cache(result: PollResult, version: str) -> dict[str, Any]:
        return {
            "value": result.value,
            "version": version,
            "done": result.done,
            "progress": result.progress,
            "eta_seconds": result.eta_seconds,
            "extra": result.extra,
            "last_modified": result.last_modified.timestamp() if result.last_modified else None,
        }

    @staticmethod
    def _result_from_cache(payload: dict[str, Any]) -> PollResult:
        last_modified = payload.get("last_modified")
        return PollResult(
            value=payload["value"],
            last_modified=(
                datetime.fromtimestamp(last_modified, tz=dt_timezone.utc) if last_modified else None
            ),
            done=payload.get("done", False),
            progress=payload.get("progress"),
            eta_seconds=payload.get("eta_seconds"),
            extra=payload.get("extra") or {},
        )

    # -- conditional requests ---------------------------------------------
    @staticmethod
    def _is_not_modified(request, etag, last_modified, config) -> bool:
        """RFC 9110: ``If-None-Match`` wins outright when present."""
        if etag and request.META.get("HTTP_IF_NONE_MATCH"):
            return if_none_match_matches(request, etag)
        if config["send_last_modified"]:
            return if_modified_since_satisfied(request, last_modified)
        return False

    # -- headers ----------------------------------------------------------
    def _apply_headers(self, response, *, etag, last_modified, next_poll_in, config, change_streak):
        if etag:
            response["ETag"] = etag
        if last_modified is not None:
            response["Last-Modified"] = format_last_modified(last_modified)

        response["Retry-After"] = str(next_poll_in)
        # Let caches revalidate (so the 304 handshake works) but never serve a
        # stale status without asking.
        response["Cache-Control"] = "no-cache, private"

        if config["sunset"]:
            response["Sunset"] = format_sunset(config["sunset"])
        if config["deprecation_message"]:
            message = config["deprecation_message"]
            response["Deprecation"] = "true"
            response["Link"] = f'<about:blank>; rel="deprecation"; title="{message}"'

        threshold = config["upgrade_hint_after"]
        if threshold and change_streak >= threshold:
            response["X-Poll-Upgrade-Hint"] = (
                "This value changes on nearly every poll; a websocket or SSE stream "
                "may suit it better than polling."
            )
        return response

    def _disabled_response(self, config):
        retry_after = max(int(config["max_poll_interval"] or 60), 1)
        response = Response(
            {"detail": "This polling endpoint is temporarily disabled."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        response["Retry-After"] = str(retry_after)
        return response

    def _object_id(self, kwargs) -> Any:
        source = self.registration.source
        if not source.requires_object:
            return None
        return kwargs.get(getattr(source, "lookup_url_kwarg", "pk"))


class PollView(PollResponseMixin, APIView):
    """Read-only endpoint for one registered poll source.

    Subclasses are generated by ``registry.build_poll_view``; you rarely
    instantiate this directly.
    """

    http_method_names = ["get", "head", "options"]

    def get(self, request, *args, **kwargs):
        return self.poll_response(request, **kwargs)

    def get_view_name(self) -> str:  # pragma: no cover - cosmetic
        return f"Poll: {self.registration.name}"

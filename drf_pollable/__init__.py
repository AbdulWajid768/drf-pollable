"""drf-pollable -- auto-generate polling (status) APIs for Django REST Framework.

Point it at a model field and you get a ready-to-poll endpoint::

    from drf_pollable import register_poll

    register_poll(
        "order-status",
        source="shop.Order.status",
        min_poll_interval=5,
        done_when={"delivered", "cancelled"},
    )

    # GET /poll/order-status/42/
    # 200  ETag: "a1b2"  Retry-After: 5
    # {"value": "shipped", "done": false, "next_poll_in": 5, ...}

See the README for the other declaration styles, the interval-negotiation rules,
and the rate-limiting layers.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "CallableSource",
    "FieldSource",
    "InstanceSource",
    "PollEndpoint",
    "PollRegistration",
    "PollResult",
    "PollRouter",
    "PollSource",
    "PollView",
    "PollableConfigurationError",
    "PollingMixin",
    "QuerySetSource",
    "poll_urls",
    "register_poll",
    "registry",
]

#: Import lazily so that merely importing this package (which Django does while
#: populating INSTALLED_APPS) never pulls in DRF before settings are ready.
_LAZY: dict[str, tuple[str, str]] = {
    "CallableSource": (".sources", "CallableSource"),
    "FieldSource": (".sources", "FieldSource"),
    "InstanceSource": (".sources", "InstanceSource"),
    "PollResult": (".sources", "PollResult"),
    "PollSource": (".sources", "PollSource"),
    "QuerySetSource": (".sources", "QuerySetSource"),
    "PollEndpoint": (".registration", "PollEndpoint"),
    "PollRegistration": (".registration", "PollRegistration"),
    "register_poll": (".registration", "register_poll"),
    "registry": (".registration", "registry"),
    "PollRouter": (".routers", "PollRouter"),
    "poll_urls": (".routers", "poll_urls"),
    "PollingMixin": (".mixins", "PollingMixin"),
    "PollView": (".views", "PollView"),
    "PollableConfigurationError": (".exceptions", "PollableConfigurationError"),
}


def __getattr__(name: str) -> Any:
    try:
        module_path, attribute = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

    from importlib import import_module

    value = getattr(import_module(module_path, __name__), attribute)
    globals()[name] = value  # cache so later lookups skip this path
    return value


def __dir__() -> list[str]:  # pragma: no cover - REPL nicety
    return sorted(__all__)

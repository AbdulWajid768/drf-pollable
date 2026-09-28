"""Polling endpoints for the test suite -- one per behaviour under test.

This file is auto-discovered by ``DrfPollableConfig.ready()``, exactly as an
``polls.py`` in a real project would be.
"""

from drf_pollable import PollEndpoint, PollResult, register_poll

# --- declaration style 1: plain function call ------------------------------
register_poll(
    "order-status",
    source="testapp.Order.status",
    min_poll_interval=5,
    done_when={"delivered", "cancelled"},
)

# Same field, but the floor is policed rather than merely advertised.
register_poll(
    "order-enforced",
    source="testapp.Order.status",
    min_poll_interval=30,
    enforce_interval=True,
)

# Exponential backoff while the value stays put.
register_poll(
    "order-backoff",
    source="testapp.Order.status",
    min_poll_interval=2,
    suggested_poll_interval=2,
    backoff="exponential",
    backoff_factor=2.0,
    max_poll_interval=60,
)

# Instance source with an explicit field allow-list.
register_poll(
    "order-detail",
    source="testapp.Order",
    fields=["status", "progress"],
    min_poll_interval=3,
    progress_field="progress",
    eta_field="eta_seconds",
)

# Bare (un-enveloped) response.
register_poll("order-bare", source="testapp.Order.status", envelope=False)

# Kill switch.
register_poll("order-disabled", source="testapp.Order.status", enabled=False)

# Deprecation / sunset advertising.
register_poll(
    "order-legacy",
    source="testapp.Order.status",
    sunset="2027-01-01",
    deprecation_message="Use order-status instead.",
)

# Scoped so only the owner can poll their own orders.
register_poll(
    "order-scoped",
    source="testapp.Order.status",
    scope=lambda queryset, request: queryset.filter(owner=request.user),
    permission_classes=["rest_framework.permissions.IsAuthenticated"],
)

# Rate-limited via a DRF throttle scope.
register_poll("order-throttled", source="testapp.Order.status", throttle_scope="tight")

# Cached value.
register_poll("order-cached", source="testapp.Order.status", cache_timeout=60)

# Emits X-Poll-Upgrade-Hint once the value has changed on 2 consecutive polls.
register_poll("order-chatty", source="testapp.Order.status", upgrade_hint_after=2)

# Jitter spreads the advertised interval to de-synchronise clients.
register_poll(
    "order-jittered",
    source="testapp.Order.status",
    min_poll_interval=10,
    max_poll_interval=100,
    jitter=0.3,
)

# Client may request its own cadence.
register_poll(
    "order-interval",
    source="testapp.Order.status",
    min_poll_interval=5,
    max_poll_interval=120,
)

# --- queryset / aggregate / delta sources ----------------------------------
register_poll(
    "unread-count",
    source="testapp.Notification",
    as_list=True,
    aggregate="count",
    scope=lambda queryset, request: queryset.filter(read=False),
)

register_poll(
    "notification-feed",
    source="testapp.Notification",
    as_list=True,
    fields=["id", "message"],
    since_field="created_at",
    order_by=["created_at"],
)


# --- callable source -------------------------------------------------------
def _pipeline_status(request, **kwargs):
    return PollResult(value="running", progress=42.0, eta_seconds=8.0)


register_poll("pipeline", source=_pipeline_status, min_poll_interval=2)


def _always_done(request, **kwargs):
    return "finished"


register_poll("pipeline-done", source=_always_done, done_when="finished")


# --- declaration style 2: class-based --------------------------------------
class OrderStatusEndpoint(PollEndpoint):
    name = "order-class"
    source = "testapp.Order.status"
    min_poll_interval = 8
    done_when = {"delivered"}

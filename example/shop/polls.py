"""Polling endpoints for the example project.

Auto-discovered at startup, exactly like Django's ``admin.py``.
"""

from drf_pollable import register_poll

# The headline case: poll one field of one row.
#   GET /api/poll/order-status/1/
register_poll(
    "order-status",
    source="shop.Order.status",
    min_poll_interval=3,
    done_when={"delivered"},
)

# A few fields at once, with a progress bar.
#   GET /api/poll/order-detail/1/
register_poll(
    "order-detail",
    source="shop.Order",
    fields=["reference", "status", "progress"],
    progress_field="progress",
    min_poll_interval=2,
    throttle_scope="orders",
)

# Back off automatically while nothing is happening: 2s, 4s, 8s ... 60s.
#   GET /api/poll/order-quiet/1/
register_poll(
    "order-quiet",
    source="shop.Order.status",
    suggested_poll_interval=2,
    backoff="exponential",
    backoff_factor=2.0,
    max_poll_interval=60,
)

# A scalar aggregate -- no object id in the URL.
#   GET /api/poll/unread-count/
register_poll(
    "unread-count",
    source="shop.Notification",
    as_list=True,
    aggregate="count",
    scope=lambda queryset, request: queryset.filter(read=False),
)

# Delta feed: pass ?since=<cursor> to get only what is new.
#   GET /api/poll/notifications/
register_poll(
    "notifications",
    source="shop.Notification",
    as_list=True,
    fields=["id", "message", "read"],
    since_field="created_at",
    order_by=["created_at"],
)

# drf-pollable

**Stop hand-writing status endpoints.** Point it at a model field and get a
ready-to-poll REST API — with conditional requests, interval negotiation, and
rate limiting already wired in.

```python
from drf_pollable import register_poll

register_poll("order-status", source="shop.Order.status", done_when={"delivered"})
```

```console
$ curl -i /api/poll/order-status/1/
HTTP/1.1 200 OK
ETag: "a844bf4f973cc84b8110"
Retry-After: 5
Cache-Control: no-cache, private

{"value": "pending", "version": "a844bf4f973cc84b8110", "done": false,
 "next_poll_in": 5, "polled_at": "...", "display": "Pending"}
```

That is the whole feature: the view, the serializer, the URL, the `ETag`/`304`
handshake, the `Retry-After` pacing, and the throttles — from one line.

For when to poll, when to choose a socket, webhook, or push instead, and the
client contract that makes the headers matter, see **[GUIDE.md](https://github.com/AbdulWajid768/drf-pollable/blob/main/GUIDE.md)**.

---

## Why polling at all? (and why not WebSockets)

"Real-time" gets treated as a synonym for WebSockets, but for the most common
need — *"what's the status of this thing? check every few seconds"* — polling is
usually the **right** tool, not a fallback:

| Approach | Why it often doesn't fit a status check |
|---|---|
| **WebSockets** | Needs ASGI + Channels + a Redis channel layer. Stateful: every client holds a socket, so scaling needs sticky sessions or pub/sub fan-out. Proxies, CDNs, and corporate firewalls break upgrades. You build reconnection, heartbeats, auth-on-upgrade yourself. Enormous overhead for a value that changes twice a day. |
| **SSE** | Still a long-lived connection pinning a worker (bad on sync WSGI). Browsers cap ~6 HTTP/1.1 connections per domain; many proxies buffer the stream. |
| **Long-polling** | Ties up a worker per waiting client — the classic exhaustion footgun. Still polling, just more expensive. |
| **Webhooks** | Requires the *client* to be a publicly reachable server. Useless for a browser or mobile app. |
| **Push (FCM/APNs)** | Heavy setup, delivery not guaranteed or ordered. Wrong for "what is the value right now". |

Polling wins here because it is **plain HTTP**: it works through every proxy,
CDN, and load balancer; it is **stateless** so any worker serves any request; it
**reuses your existing DRF auth, permissions, and throttling**; and an unchanged
poll is nearly free thanks to `304`.

Polling's real weaknesses are wasted requests and up-to-one-interval latency —
and this package attacks exactly those: server-driven `Retry-After` with
exponential backoff, `304 Not Modified` for unchanged values, and a `done` flag
that tells the client to stop entirely.

**When you genuinely need sub-second bidirectional updates — chat, collaborative
cursors, trading tickers — use WebSockets.** This package does not try to
replace that, and will even hint when your data looks like it wants one.
[GUIDE.md](https://github.com/AbdulWajid768/drf-pollable/blob/main/GUIDE.md) is the full comparison and the usage rules.

---

## Install

```bash
pip install drf-pollable
```

```python
# settings.py
INSTALLED_APPS = [..., "rest_framework", "drf_pollable"]
```

```python
# urls.py
from django.urls import path

from drf_pollable import poll_urls

urlpatterns = [path("api/", poll_urls())]
```

Declare endpoints in a `polls.py` in any app — it is auto-discovered, like
`admin.py`.

---

## Five ways to declare an endpoint

Different developers want different things; all five produce identical
behaviour because they all reduce to the same internal source.

**1. Function call** — the common case.

```python
# shop/polls.py
register_poll("order-status", source="shop.Order.status", min_poll_interval=5)
```

**2. Class-based** — when you like declarations to look like classes.

```python
from drf_pollable import PollEndpoint

class OrderStatus(PollEndpoint):
    name = "order-status"
    source = "shop.Order.status"
    min_poll_interval = 5
    done_when = {"delivered", "cancelled"}
```

**3. On the model** — declared right next to the data.

```python
class Order(models.Model):
    status = models.CharField(...)

    class PollingMeta:
        fields = {"status": {"min_poll_interval": 5, "done_when": "delivered"}}
        instance = {"name": "order-detail", "fields": ["status", "progress"]}
```

**4. Central settings** — good for an overview, or ops overrides.

```python
DRF_POLLABLE = {
    "DEFAULTS": {"min_poll_interval": 5},
    "ENDPOINTS": {
        "order-status": {"source": "shop.Order.status", "done_when": "delivered"},
    },
}
```

**5. On a ViewSet you already own** — adds `GET /orders/{pk}/poll/`.

```python
from drf_pollable import PollingMixin

class OrderViewSet(PollingMixin, viewsets.ModelViewSet):
    queryset = Order.objects.all()
    polling = {"source": "status", "min_poll_interval": 5}
```

---

## Four things you can poll

```python
# 1. a single field                 -> GET /poll/order-status/1/
register_poll("order-status", source="shop.Order.status")

# 2. several fields of one instance -> GET /poll/order-detail/1/
register_poll("order-detail", source="shop.Order", fields=["status", "progress"])

# 3. a list, count, or delta feed   -> GET /poll/unread-count/
register_poll("unread-count", source="shop.Notification", as_list=True,
              aggregate="count",
              scope=lambda qs, request: qs.filter(owner=request.user, read=False))

# 4. any function                   -> GET /poll/pipeline/
def pipeline_status(request, **kwargs):
    return PollResult(value="running", progress=42, eta_seconds=8)

register_poll("pipeline", source=pipeline_status)
```

`fields`/`serializer` is **required** for instance and list sources — dumping
every column by default would quietly leak fields as your model grows.

### Delta polling

Give a list source a `since_field` and it returns a cursor; send it back as
`?since=` and you get only what changed:

```python
register_poll("notifications", source="shop.Notification", as_list=True,
              fields=["id", "message"], since_field="created_at")
```

---

## Who decides the interval? Both.

This is the question everyone asks, and the honest answer is that it is shared:

- **The frontend owns the *actual* cadence.** Only the client knows how fresh
  the data must feel, whether the tab is focused, or what the battery is doing.
- **The backend owns the *floor* and a *suggestion*.** Only the server can
  protect itself. If the client chose freely, one buggy loop polling every 100ms
  would hammer your database and you could do nothing about it.

**The client can always ask to go slower, never faster than the server allows.**

```python
register_poll(
    "order-status",
    source="shop.Order.status",
    min_poll_interval=5,        # hard floor
    suggested_poll_interval=5,  # advertised via Retry-After
    max_poll_interval=300,      # ceiling for client requests / backoff
    enforce_interval=False,     # True -> a too-early poll gets 429
    backoff="exponential",      # suggestion grows while nothing changes
    backoff_factor=2.0,
    jitter=0.1,                 # de-synchronise clients (anti-thundering-herd)
    done_when={"delivered"},    # tells the client to stop entirely
)
```

- `GET /poll/order-status/1/?interval=30` → `Retry-After: 30` (slower: allowed)
- `GET /poll/order-status/1/?interval=1` → `Retry-After: 5` (clamped to the floor)
- While the value is unchanged and backoff is on: `5 → 10 → 20 → … → max`
- `jitter` spreads the advertised value so a thousand clients that started
  together don't all come back in the same second.

Defaults are lenient — advertised, not enforced — so simple projects just work.

---

## Rate limiting

Layered on top of DRF's own throttling, so you keep `DEFAULT_THROTTLE_RATES`
and your existing conventions. Opt into as much as you need.

**1. Per-endpoint budget** — the simplest:

```python
register_poll("order-status", source="shop.Order.status", throttle_scope="orders")
REST_FRAMEWORK = {"DEFAULT_THROTTLE_RATES": {"orders": "30/min"}}
```

**2. Per-object cadence floor** — the one a plain rate limit misses. A client
can be well inside its budget and still hammer *one* record:

```python
register_poll(..., min_poll_interval=5, enforce_interval=True)  # too soon -> 429
```

**3. Burst + sustained** — allow spikes, cap the long run:

```python
from drf_pollable.throttling import PollBurstThrottle, PollSustainedThrottle

register_poll(..., throttle_classes=[PollBurstThrottle, PollSustainedThrottle])
REST_FRAMEWORK = {"DEFAULT_THROTTLE_RATES": {
    "poll_burst": "10/sec", "poll_sustained": "600/hour",
}}
```

**4. Global endpoint cap** — protect the DB regardless of who is calling:

```python
from drf_pollable.throttling import GlobalPollThrottle
# DEFAULT_THROTTLE_RATES = {"poll_global": "5000/min"}
```

**5. Per-tenant isolation** — one customer cannot starve the others:

```python
DRF_POLLABLE = {"TENANT_RESOLVER": "myapp.tenancy.current_tenant_id"}
# DEFAULT_THROTTLE_RATES = {"poll_tenant": "1000/hour"}
```

**6. Bring your own** — `throttle_classes` is overridable per endpoint, and this
plays fine with `django-ratelimit`, an API gateway, or nginx `limit_req` at the
edge with these as a backstop.

Every rejection returns a correct `429` with `Retry-After`.

### Permissions and scoping

Endpoints reuse your DRF auth. `scope` filters the queryset so users only poll
their own rows:

```python
register_poll(
    "order-status",
    source="shop.Order.status",
    permission_classes=[IsAuthenticated],
    scope=lambda queryset, request: queryset.filter(owner=request.user),
)
```

Object-level permissions run against the real instance, so existing
`IsOwner`-style classes work unchanged.

---

## Bandwidth: the 304 handshake

Every response carries an `ETag` derived from the **value itself** (not a
timestamp — `auto_now` doesn't fire on `QuerySet.update()`, which would hand
clients a stale `304`). An unchanged poll costs a header exchange:

```console
$ curl -i -H 'If-None-Match: "a844bf4f973cc84b8110"' /api/poll/order-status/1/
HTTP/1.1 304 Not Modified
ETag: "a844bf4f973cc84b8110"
Retry-After: 5
```

`Last-Modified` / `If-Modified-Since` work too. For hot endpoints,
`cache_timeout=N` caches the computed value so a burst of pollers doesn't each
hit the database (per-user by default; set `cache_vary_on_user=False` only when
the data is identical for everyone).

---

## Configuration reference

Global defaults go in `DRF_POLLABLE["DEFAULTS"]`; any endpoint overrides them.
Resolution is **per-endpoint → global defaults → package built-in**. Unknown
options raise immediately rather than silently doing nothing.

| Option | Default | What it does |
|---|---|---|
| `enabled` | `True` | Kill switch — `503 + Retry-After` when off |
| `min_poll_interval` | `5` | Hard floor (seconds) |
| `suggested_poll_interval` | = floor | What `Retry-After` advertises |
| `max_poll_interval` | `3600` | Ceiling for clamping and backoff |
| `enforce_interval` | `False` | `429` a too-early poll instead of just hinting |
| `client_interval_param` | `"interval"` | Query param for client-requested cadence |
| `backoff` | `"fixed"` | or `"exponential"` |
| `backoff_factor` / `backoff_max` | `2.0` / = max | Growth rate and cap |
| `jitter` | `0.0` | Random spread as a fraction (`0.1` = ±10%) |
| `envelope` | `True` | `False` returns the bare value |
| `send_etag` / `send_last_modified` | `True` | Conditional-request headers |
| `cache_timeout` / `cache_vary_on_user` | `0` / `True` | Value caching |
| `throttle_scope` / `throttle_classes` | `None` | Rate limiting |
| `permission_classes` / `authentication_classes` | `None` | Inherit DRF defaults |
| `sunset` / `deprecation_message` | `None` | Advertise retirement (RFC 8594) |
| `upgrade_hint_after` | `0` | Hint that this data wants a websocket |
| `url_prefix` / `lookup_field` / `lookup_url_kwarg` | `"poll"` / `"pk"` | Routing |

Source options (not behaviour): `source`, `fields`, `serializer`, `done_when`,
`scope`, `aggregate`, `since_field`, `order_by`, `limit`, `version_field`,
`progress_field`, `eta_field`, `select_related`, `as_list`.

---

## Introspection

```console
$ python manage.py list_polls
order-status
  source   field shop.Order.status
  path     /poll/order-status/<str:pk>/
  declared code
  min_poll_interval  5
  ...
```

---

## Testing your endpoints

```python
from drf_pollable.testing import clear_poll_state, poll_until, assert_poll_headers

def test_order_reaches_delivered(client, order):
    clear_poll_state()                       # forget backoff/last-poll state
    response = client.get(f"/poll/order-status/{order.pk}/")
    assert_poll_headers(response)
    assert response.json()["value"] == "pending"
```

`poll_until(client, url, predicate, advance=...)` drives a polling loop to a
terminal state; `assert_not_modified(response)` checks the `304` path.

---

## Development

```bash
python runtests.py          # 300 tests, needs only Django + DRF
cd example && python manage.py migrate && python manage.py seed && python manage.py runserver
curl -i http://localhost:8000/api/poll/order-status/1/
```

---

## Roadmap

Shipped in 0.1: everything above. Planned:

- `drf-spectacular` schema extension documenting the envelope, `Retry-After`, and `304`
- Admin dashboard: poll volume, `304` ratio, throttle rate, bandwidth saved
- Batch endpoint — poll several sources in one request
- Signals/webhooks on value change
- Backpressure: longer `Retry-After` when the backend is unhealthy
- Opt-in long-poll mode (ASGI), deliberately last — see the worker-exhaustion note above

## License

MIT

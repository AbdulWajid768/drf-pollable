# drf-pollable usage guide

This is the decision and usage guide for [drf-pollable](README.md). The README
is the short reference (install, options, examples). This file answers three
questions:

1. Should this status check be a poll at all?
2. If yes, how do you declare it so it stays safe as the model and the traffic grow?
3. What must the client do for the server-side machinery to actually help?

drf-pollable generates a **read-only HTTP endpoint** for "what is the current
value of this thing?". It is not a messaging system, a job queue, or a
replacement for a ViewSet you already use for create/update/delete.

---

## 1. Use it when

Use a polling endpoint when all of the following are true:

- A person or a service needs the **current value** of one field, a few fields, a count, or a short list.
- Freshness of **a few seconds** is enough. The data changes on a human or job timescale: an order moves from `pending` to `shipped`, a report finishes, a badge count ticks up.
- The caller is a browser, a mobile app, or another service that can make ordinary HTTP requests. It does not have to be reachable from the public internet.
- You already run Django REST Framework and want the poll to use the **same authentication, permissions, and throttles** as the rest of the API.
- The deployment is WSGI, or you do not want a long-lived connection per client (ASGI, a channel layer, sticky sessions, heartbeats).

Typical fits:

| Situation | What you poll | Why a poll is enough |
|---|---|---|
| Order, shipment, payout, or ticket status | One choice field, with `done_when` set to the terminal values | The value changes a handful of times, then the client stops |
| Import, export, encoding, or payment progress | A field plus `progress_field` / `eta_field`, or a `PollResult` | The UI needs a bar, not a stream of events |
| Unread badge | `aggregate="count"` on a scoped queryset | One integer; a list would be wasted work |
| Notification inbox | A list with `since_field` | The client fetches the delta since its cursor |
| "Is the pipeline still running?" | A function that returns a `PollResult` | The status is computed, not a single column |

A concrete declaration for the headline case:

```python
# shop/polls.py  — auto-discovered, like admin.py
from drf_pollable import register_poll

register_poll(
    "order-status",
    source="shop.Order.status",
    min_poll_interval=5,
    done_when={"delivered", "cancelled"},
    permission_classes=["rest_framework.permissions.IsAuthenticated"],
    scope=lambda queryset, request: queryset.filter(owner=request.user),
)
```

`GET /poll/order-status/42/` then returns the value, whether the client should
stop, and when to come back. Unchanged polls answer `304` with an empty body.

---

## 2. Use something else when

Pick another tool when the constraint below is the real requirement. drf-pollable
will not meet it, and wrapping it to try will cost more than using the right
mechanism.

| You need | Use this instead | Why a status poll is the wrong shape |
|---|---|---|
| Sub-second updates, or the server must speak first | WebSockets (Django Channels, or a hosted realtime service) | A poll can only be as fresh as its interval, and the client has to ask |
| Two-way interaction: chat, cursors, presence, collaborative editing, live trading | WebSockets | Each side sends on its own clock; a poll is a question, not a conversation |
| The app is backgrounded and must wake up | APNs / FCM / web push | A poll only runs while the client is awake and asking |
| Server-to-server delivery to an endpoint you do not control | Webhooks | The other system is the server. It cannot poll you, and you cannot rely on it polling itself |
| A worker should block until a job exists | A queue (or long-polling, knowingly) | Polling a Django view from a tight loop still occupies your scheduler; a queue is the waiting primitive |
| A full resource API: list, create, update, delete | A DRF `ViewSet` | A poll is a status lens on data you already expose. Add `PollingMixin` only for the status action |
| One request must read many unrelated objects | Your own aggregate endpoint | A batch poll is not shipped. N polls means N requests |
| The value is a large document that changes continuously | A stream, or a delta protocol you design | Conditional requests save an unchanged body; they do not make a firehose cheap |
| OpenAPI must describe the envelope and the `304` today | Hand-written schema, for now | A drf-spectacular extension is planned, not included |
| You need the client to learn about a change with no request at all | Push or a socket | Polling always spends a request to discover that nothing changed |

If a poll's value changes on almost every request, the server can set
`upgrade_hint_after`. After that many consecutive *changes* it sends
`X-Poll-Upgrade-Hint`. Treat that header as an operational signal to move that
endpoint to a socket or SSE, not as something the client has to implement.

---

## 3. How it compares

Polling's weaknesses are well known: the client waits up to one interval to
notice a change, and idle clients still send requests. drf-pollable spends its
complexity on those two costs (`Retry-After` with backoff, `304`, `done`) and
leaves the transport as plain HTTP. The comparison below is about a **status
check**, not about every realtime problem.

### Against a hand-written DRF action

A `GET /orders/{id}/status/` that you write yourself is the closest alternative,
and it is the right alternative when the response is not a status poll (a
one-shot report, a signed download, a command).

For an actual poll, the hand-written action tends to grow the same machinery
one endpoint at a time, and each copy drifts:

- The `ETag` is derived from `updated_at`. `QuerySet.update()` does not touch
  `auto_now` columns, so clients receive a stale `304` and keep showing the old
  status. drf-pollable hashes the **value**.
- There is no `Retry-After`, so every client invents an interval. One buggy
  loop polls at 100ms and you have no floor.
- "Stop when delivered" is a convention in the frontend repo, not a field in
  the response.
- Object permissions, owner scoping, and a per-object rate limit are easy to
  forget on the second endpoint.

drf-pollable gives you one declaration and the same contract on every endpoint:
envelope, `ETag`, `Retry-After`, `done`, throttles, and the permission check.

### Against WebSockets

| | WebSockets | drf-pollable |
|---|---|---|
| Freshness | As fast as the network, once connected | Bounded by the interval you advertise (seconds, by design) |
| Direction | Both sides send | Client asks, server answers |
| Connection | One long-lived socket per client | One short request, then the worker is free |
| Infrastructure | ASGI, a channel layer, reconnect, heartbeats, auth on upgrade | The WSGI/DRF stack you already run |
| Proxies and firewalls | Upgrade requests are commonly blocked or buffered | Ordinary HTTPS |
| Idle cost | The socket and the pub/sub subscription stay up | `304` plus a growing `Retry-After` while the value is stable |
| Auth | A second scheme, unless you carefully reuse the session on upgrade | The same DRF authentication and permission classes |

Choose sockets when the product is the stream. Choose a poll when the product
is a value that is usually the same as last time.

### Against Server-Sent Events

SSE is still one long-lived response per client. On a synchronous WSGI worker
that pins a process for the life of the stream. Browsers also cap HTTP/1.1
connections per host (commonly six), so a few open streams starve the rest of
the origin. Many proxies buffer `text/event-stream` until the response ends,
which defeats the stream.

A poll uses a normal response, ends, and releases the worker. The cost is the
interval of latency and the request rate, both of which you cap.

### Against long-polling

Long-polling holds the request open until the value changes or a timeout fires.
That is still a poll, with a worker occupied for the whole wait. Under load
this is how sync worker pools get exhausted. drf-pollable answers immediately
and tells the client when to come back. An opt-in long-poll mode is
deliberately not included.

### Against webhooks

A webhook is the right call when **you** are notifying **someone else's
server**: payment captured, shipment handed to the carrier, a partner's
endpoint. The receiver must be publicly reachable, and you must handle retries,
signatures, and "they were down, so they missed it".

A poll is the right call when the consumer is a browser or a phone. The client
asks for the current value, so a missed interval heals on the next request.
There is no unsigned POST to verify and no hole while the tab was closed: the
next poll returns the value as it is now.

They compose. A webhook can update your model; the user's browser polls that
model. drf-pollable is the second half.

### Against mobile and web push

Push is how you get attention when the app is not running. Delivery is not
ordered, not guaranteed, and the payload is often a hint ("order updated")
rather than the authoritative value. After the user opens the app you still
need a request that returns the current status. That request can be a
drf-pollable endpoint. Push does not replace it.

### Against "just poll the existing list endpoint"

`GET /api/orders/42/` on a timer works until it does not:

- The body is the whole resource, so an unchanged poll is not cheap.
- Nothing tells the client the minimum safe interval, or that the status is terminal.
- Your list throttles are shaped for humans clicking, not for a loop.

Point a poll at the field you actually care about. Keep the ViewSet for
everything else. `PollingMixin` adds `GET /orders/{pk}/poll/` on that ViewSet
when you want the poll next to the resource URL.

---

## 4. Declare the endpoint

Any installed app may contain a `polls.py`. `DrfPollableConfig.ready()` loads
them, then model `PollingMeta`, then `DRF_POLLABLE["ENDPOINTS"]`. Later sources
win when the name collides, so settings can override a declaration shipped in
code. Mount the routes from the **root** URLconf, after apps are ready:

```python
# urls.py
from django.urls import path
from drf_pollable import poll_urls

urlpatterns = [
    path("api/", poll_urls()),
]
```

That serves `GET /api/poll/<name>/<pk>/` for a single object and
`GET /api/poll/<name>/` for a list or aggregate. Only `GET`, `HEAD`, and
`OPTIONS` are accepted.

Five declaration styles produce the same runtime behaviour. Pick by where you
want the declaration to live:

| Style | Write it when |
|---|---|
| `register_poll(...)` in `polls.py` | This is the default. The endpoint is configuration, not a class. |
| `@register_poll` or a `PollEndpoint` subclass | The endpoint has enough options that a class body is easier to read, or several endpoints share an `abstract = True` base. |
| `PollingMeta` on the model | The poll is part of the model's contract and you want it beside the field. |
| `DRF_POLLABLE["ENDPOINTS"]` | Ops or a central settings module must override an endpoint without editing the app. A `scope` or other callable has to be the callable itself; a dotted path is not imported for it. Thresholds and source strings are the settings that belong here. |
| `PollingMixin` on a ViewSet | You already have the route and the permissions, and you only want `GET /{pk}/poll/`. These endpoints are **not** registered globally, so `list_polls` does not show them. |

Inspect what the process actually registered:

```console
python manage.py list_polls
python manage.py list_polls --name order-status --verbose
```

---

## 5. Choose the source

Every style reduces to one of four sources. Pick the smallest one that answers
the question the screen is asking.

### One field

```python
register_poll("order-status", source="shop.Order.status", done_when={"delivered"})
```

Use this for a choice, a boolean, or a progress integer. Choice fields include
`display` (the human label) so the client does not copy `choices` into the
frontend. A property on the model is allowed; a typo in the field name fails at
first use with `PollableConfigurationError`.

`done_when` accepts a single value, a collection of terminal values, or a
callable. A string is compared for equality. It is not treated as a set of
characters: `done_when="delivered"` does not match `"d"`.

### Several fields of one row

```python
register_poll(
    "order-detail",
    source="shop.Order",
    fields=["status", "progress"],
    progress_field="progress",
    eta_field="eta_seconds",
)
```

`fields` or `serializer` is required. There is no "all columns" default,
because a column added next month would otherwise become part of a public poll.
`progress` and `eta_seconds` are copied onto the envelope when those fields
hold numbers, including `0`.

### A list, a count, or a delta

```python
register_poll(
    "unread-count",
    source="shop.Notification",
    as_list=True,
    aggregate="count",
    scope=lambda queryset, request: queryset.filter(owner=request.user, read=False),
)

register_poll(
    "notifications",
    source="shop.Notification",
    as_list=True,
    fields=["id", "message"],
    since_field="created_at",
    order_by=["created_at"],
    limit=50,
)
```

- `aggregate` is `"count"`, `"exists"`, or a callable. The URL has **no** object id. Use this when the screen wants a scalar.
- A list also requires `fields` or `serializer`. `count` is included in the envelope.
- `since_field` adds a `cursor`. The client sends it back as `?since=<iso8601>` and receives rows strictly newer than that timestamp. A cursor that does not parse is ignored, and the full window is returned.
- `scope` is how you keep one user's rows out of another user's poll. It runs on every read, including a cache hit.
- `limit` caps the page. Combine it with `order_by` so the page is stable.

### A function

```python
from drf_pollable import PollResult, register_poll


def pipeline_status(request, **kwargs):
    job = find_job(kwargs["pk"])
    return PollResult(
        value=job.state,
        progress=job.percent,
        eta_seconds=job.eta,
        done=job.state in {"succeeded", "failed"},
    )


register_poll("pipeline", source=pipeline_status, requires_object=True, min_poll_interval=2)
```

Use a callable when the value is not a column: a third-party API, a cache key,
a state machine spread across tables. Return a plain value, or a `PollResult`
when you already know `done`, `progress`, or `eta_seconds`. Set
`requires_object=True` if the URL should carry the lookup (default is a URL
with no object id).

---

## 6. The contract the client follows

The server can only suggest. The client has to honour the suggestion, or the
interval floor, the `304`, and `done` do nothing.

### Response

`200` body, when `envelope` is left on (the default):

```json
{
  "value": "shipped",
  "display": "Shipped",
  "version": "a844bf4f973c",
  "done": false,
  "next_poll_in": 5,
  "polled_at": "2026-10-11T16:02:03.114+00:00"
}
```

`display`, `progress`, `eta_seconds`, `count`, and `cursor` appear only when
the source has them. `envelope=False` returns the bare value; the metadata
stays in the headers.

Headers on `200` and `304`:

| Header | Meaning |
|---|---|
| `ETag` | Strong tag for this **value**. Send it back as `If-None-Match`. |
| `Retry-After` | Seconds to wait before the next poll. Same number as `next_poll_in`. |
| `Cache-Control` | `no-cache, private`. A cache may revalidate; it may not reuse a stale body. |
| `Last-Modified` | Present when the model has a timestamp field such as `updated_at`. Secondary to the ETag. |

`304` has an empty body. Keep the previous `value` and still wait `Retry-After`.
If both `If-None-Match` and `If-Modified-Since` are sent, `If-None-Match` wins,
including when it does **not** match.

### Status codes

| Code | What the client does |
|---|---|
| `200` | Replace the stored value. Stop if `done` is true. |
| `304` | Keep the stored value. Wait `Retry-After`. |
| `401` / `403` | Stop. The caller is not allowed to see this row. |
| `404` | Stop, or treat the object as gone. Scoped querysets use `404` when the row is not in the caller's set. |
| `429` | Wait `Retry-After`, then try again. The client polled faster than the floor or the rate limit. |
| `503` | The endpoint is disabled (`enabled=False`). Wait `Retry-After`. |
| `405` | The client used a method other than `GET` or `HEAD`. |

### Loop

```javascript
async function watchStatus(url, { signal, onUpdate } = {}) {
  let etag = null;
  let waitSeconds = 5;

  while (!signal?.aborted) {
    const headers = {};
    if (etag) headers["If-None-Match"] = etag;

    const response = await fetch(url, { headers, signal });
    const retry = Number(response.headers.get("Retry-After"));
    if (Number.isFinite(retry) && retry >= 1) waitSeconds = retry;
    etag = response.headers.get("ETag") || etag;

    if (response.status === 200) {
      const body = await response.json();
      onUpdate(body);
      if (body.done) return body;
    } else if (response.status === 304 || response.status === 429 || response.status === 503) {
      // 304 keeps the last body; 429 and 503 just delay the next attempt.
    } else {
      throw new Error(`poll failed: ${response.status}`);
    }

    await delay(waitSeconds * 1000, signal);
  }
}
```

Rules worth enforcing in review:

- Wait the server's interval. Do not hard-code `1000` and ignore `Retry-After`.
- Send the last `ETag` on every poll after the first.
- Stop on `done: true`. A terminal order that is still polled is a bug in the client.
- The client may slow itself down (`?interval=30` asks for a 30s cadence, and the server accepts it when 30 is between the floor and the ceiling). The server will not let the client go faster than `min_poll_interval`.
- Hide the tab, lose focus, or drop battery: raise `?interval=`. That is the client's job. The server does not know the tab is in the background.
- `HEAD` returns the same headers as `GET` when the client only needs to revalidate.

---

## 7. Set the interval on purpose

The client owns the real cadence. The server owns the floor and the
suggestion. Defaults are deliberately lenient: the floor is advertised through
`Retry-After` and is **not** enforced until you set `enforce_interval=True`.

| Knob | Set it |
|---|---|
| `min_poll_interval` | The fastest poll you are willing to serve for this data. A status that changes a few times an hour does not need a 1s floor. |
| `suggested_poll_interval` | What you want a well-behaved client to do at the start. Falls back to the floor. |
| `max_poll_interval` | The slowest you will agree to, and the cap for backoff. |
| `backoff="exponential"` | While the value is unchanged. Idle clients should get quieter: 5s, 10s, 20s, up to the cap. A change resets the streak. |
| `jitter` | When many clients start together (a page load, a push that opens the app). `0.1` spreads `Retry-After` by ±10% so they do not reconverge on the same second. |
| `enforce_interval=True` | On any endpoint a broken client could hammer. Too-soon polls return `429` with `Retry-After`. Enforcement is per caller **and** per object: hammering order 1 does not block order 2, and it does not block a different user. |
| `done_when` | Whenever the value has a real terminal state. This is the only way the request rate goes to zero. |

Backoff and the last-poll timestamp live in the Django cache, per client. A
cache flush makes the next poll look like the first one, which is safe: the
client is allowed through, and the streak starts over.

`X-Forwarded-For` is treated as the client address for anonymous callers, using
the left-most hop. Put the app behind a proxy that overwrites that header, or
anonymous users can share (or spoof) a bucket. Authenticated users are keyed by
user id, which is what you want.

---

## 8. Permissions, scope, and cache

### Who can see the row

Reuse DRF. Two different mechanisms, used together:

- `permission_classes` decides whether this caller may use the endpoint, and
  object-permission classes (`has_object_permission`) run against the real
  model instance.
- `scope` filters the queryset. A row outside the filter is `404`, which avoids
  confirming that the object exists.

```python
register_poll(
    "order-status",
    source="shop.Order.status",
    permission_classes=["rest_framework.permissions.IsAuthenticated"],
    scope=lambda queryset, request: queryset.filter(owner=request.user),
)
```

On a ViewSet, put object permissions on the viewset itself. `PollingMixin`
uses those classes; it does not invent a second permission stack.

### Caching the value

`cache_timeout=0` (the default) hits the database on every poll. Set a timeout
only when the same value is requested often inside that window and you can
tolerate the value being stale for that long.

- `cache_vary_on_user=True` (the default) keeps a separate entry per caller.
  Leave it on whenever two callers can see different values.
- `cache_vary_on_user=False` is for data that is identical for every caller
  (a global feature flag, a public incident banner). One user's poll must not
  be servable to another user.
- When the endpoint has a `scope` or any permission class other than
  `AllowAny`, a cache hit still re-reads the row and re-checks access. Losing
  ownership takes effect on the next poll even though the cached **value** is
  reused. The timeout does not extend a permission.

---

## 9. Rate limits

Interval enforcement and DRF rate limits answer different questions. Use both
when the endpoint is public or shared by tenants.

| Layer | Stops |
|---|---|
| `enforce_interval=True` | One caller polling **one object** faster than the floor |
| `throttle_scope` | One caller spending the endpoint's budget (`"30/min"`) across objects |
| `PollBurstThrottle` + `PollSustainedThrottle` | A short spike and a long-run ceiling. Set `poll_burst` and `poll_sustained` in `DEFAULT_THROTTLE_RATES` |
| `GlobalPollThrottle` | Every caller combined, to protect the database. Set `poll_global` |
| `TenantRateThrottle` | One tenant starving the others. Set `DRF_POLLABLE["TENANT_RESOLVER"]` to a dotted path, and set `poll_tenant` |

A missing rate for `poll_burst`, `poll_sustained`, `poll_global`, or
`poll_tenant` raises `ImproperlyConfigured` when that throttle class is
constructed. Set the rate, or set it to `None` to leave the class installed
and inactive. Every `429` includes `Retry-After`.

These sit on top of DRF's throttle classes. An API gateway or nginx
`limit_req` in front is compatible; keep one of the layers above as the
backstop that understands per-object cadence.

---

## 10. Before you ship an endpoint

- [ ] The source is the smallest one that answers the screen: field, then instance, then aggregate, then list, then callable.
- [ ] Instance and list polls pass `fields` or `serializer`. No unbounded column dump.
- [ ] `scope` and object permissions match the ViewSet that already exposes this model.
- [ ] `done_when` is set if a human would stop refreshing.
- [ ] `min_poll_interval` is a number you can afford at the peak client count, not a copy of the default.
- [ ] Public or multi-tenant endpoints set `enforce_interval=True` and a scope rate.
- [ ] `cache_vary_on_user` stays `True` unless you have written down why every caller sees the same value.
- [ ] The client sends `If-None-Match`, waits `Retry-After`, and stops on `done`.
- [ ] `python manage.py list_polls --verbose` shows the floor, backoff, and throttle you think you configured.
- [ ] A test calls `clear_poll_state()` so a previous test's backoff does not leak. `poll_until` and `assert_poll_headers` cover the loop and the headers.

```python
from drf_pollable.testing import assert_poll_headers, clear_poll_state, poll_until


def test_order_reaches_delivered(client, order):
    clear_poll_state()
    url = f"/poll/order-status/{order.pk}/"
    assert_poll_headers(client.get(url))

    def deliver(attempt):
        if attempt == 1:
            order.__class__.objects.filter(pk=order.pk).update(status="delivered")

    response = poll_until(client, url, lambda body: body["done"], advance=deliver)
    assert response.json()["value"] == "delivered"
```

The option table, throttle snippets, and the five declaration examples live in
the [README](README.md).

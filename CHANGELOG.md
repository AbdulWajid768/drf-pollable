# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-10-17

First release. Generates read-only polling ("status") endpoints for Django REST
Framework from a model field, instance, queryset, or callable.

### Added

**Sources** — four kinds, all behind one `PollSource` abstraction:
- `FieldSource` — one field of one instance, with the choice `display` label included
- `InstanceSource` — several fields, gated behind a required `fields`/`serializer` allow-list
- `QuerySetSource` — lists, `count`/`exists` aggregates, and `?since=` delta polling with a cursor
- `CallableSource` — any function, returning a value or a `PollResult`

**Declaration styles** — five, all producing identical behaviour:
- `register_poll()` function call or decorator
- `PollEndpoint` subclass (self-registering)
- `PollingMeta` inner class on a model
- `DRF_POLLABLE["ENDPOINTS"]` in settings
- `PollingMixin` on an existing ViewSet

**Interval negotiation** — the client may always ask to poll slower, never faster:
- `min_poll_interval` floor, `suggested_poll_interval` advertised via `Retry-After`
- client-requested cadence via `?interval=`, clamped to `[min, max]`
- fixed or exponential `backoff` while the value is unchanged
- `jitter` to de-synchronise clients
- `done_when` sets `done: true` so the client stops polling

**Rate limiting**, layered on DRF throttling:
- `PollIntervalThrottle` — per-object cadence floor (`enforce_interval` → `429`)
- `PollScopedRateThrottle`, `PollBurstThrottle`, `PollSustainedThrottle`
- `GlobalPollThrottle` — per-endpoint ceiling across all callers
- `TenantRateThrottle` — per-tenant isolation via `TENANT_RESOLVER`

**Bandwidth**:
- `ETag` / `If-None-Match` → bodyless `304`
- `Last-Modified` / `If-Modified-Since`
- optional short-TTL value cache (`cache_timeout`, `cache_vary_on_user`)

**Operations**:
- two-layer configuration (per-endpoint → global `DEFAULTS` → built-ins), with
  unknown options rejected loudly
- `enabled` kill switch → `503 + Retry-After`
- `Sunset` / `Deprecation` headers
- `X-Poll-Upgrade-Hint` when data changes on nearly every poll
- `manage.py list_polls` for introspection
- `drf_pollable.testing` helpers: `clear_poll_state`, `poll_until`,
  `assert_poll_headers`, `assert_not_modified`

### Notes

- Version tokens hash the **value**, not a timestamp field. An `auto_now` column
  does not update on `QuerySet.update()`, so a timestamp-derived `ETag` would
  miss real changes and serve a stale `304`.
- Instance and list sources require an explicit field allow-list so that fields
  added to a model later are never exposed by accident.

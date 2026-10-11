"""Unit tests for the pure helpers: config resolution, intervals, headers."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils.dateparse import parse_datetime

from drf_pollable.conf import (
    BUILTIN_DEFAULTS,
    ImproperlyConfiguredDefaults,
    global_defaults,
    resolve_config,
    settings_endpoints,
)
from drf_pollable.envelope import build_envelope
from drf_pollable.headers import (
    etag_for,
    format_last_modified,
    format_sunset,
    if_modified_since_satisfied,
    if_none_match_matches,
    make_version,
)
from drf_pollable.intervals import compute_next_interval, parse_requested_interval
from drf_pollable.sources import PollResult, resolve_done
from drf_pollable.state import (
    ClientPollState,
    PollStateStore,
    client_identifier,
    state_key,
)


class ConfigResolutionTests(SimpleTestCase):
    def test_global_defaults_override_builtins(self):
        # tests/settings.py sets min_poll_interval = 7; the built-in is 5.
        self.assertEqual(BUILTIN_DEFAULTS["min_poll_interval"], 5)
        self.assertEqual(resolve_config()["min_poll_interval"], 7)

    def test_per_endpoint_overrides_global(self):
        self.assertEqual(resolve_config({"min_poll_interval": 42})["min_poll_interval"], 42)

    def test_suggested_interval_falls_back_to_floor(self):
        config = resolve_config({"min_poll_interval": 12})
        self.assertEqual(config["suggested_poll_interval"], 12)

    def test_backoff_max_falls_back_to_max_interval(self):
        config = resolve_config({"max_poll_interval": 99})
        self.assertEqual(config["backoff_max"], 99)

    def test_unknown_option_is_rejected(self):
        # A typo must fail loudly rather than silently doing nothing.
        with self.assertRaises(ImproperlyConfiguredDefaults):
            resolve_config({"min_poll_intervall": 5})

    def test_source_options_are_not_treated_as_config(self):
        config = resolve_config({"source": "a.B.c", "done_when": {"x"}, "fields": ["a"]})
        self.assertNotIn("source", config)
        self.assertNotIn("done_when", config)

    @override_settings(DRF_POLLABLE={"DEFAULTS": {"jitter": 0.5}})
    def test_settings_changes_are_picked_up(self):
        self.assertEqual(resolve_config()["jitter"], 0.5)

    def test_explicit_suggestion_and_backoff_cap_are_kept(self):
        config = resolve_config(
            {"suggested_poll_interval": 9, "backoff_max": 40, "max_poll_interval": 100}
        )
        self.assertEqual(config["suggested_poll_interval"], 9)
        self.assertEqual(config["backoff_max"], 40)

    @override_settings(DRF_POLLABLE=None)
    def test_missing_settings_fall_back_to_builtins(self):
        self.assertEqual(
            resolve_config()["min_poll_interval"],
            BUILTIN_DEFAULTS["min_poll_interval"],
        )

    @override_settings(DRF_POLLABLE={"DEFAULTS": {"not_an_option": 1}})
    def test_unknown_global_default_is_rejected(self):
        with self.assertRaises(ImproperlyConfiguredDefaults) as caught:
            global_defaults()
        self.assertIn("not_an_option", str(caught.exception))
        self.assertIn("min_poll_interval", str(caught.exception))

    def test_settings_endpoints_are_a_copy(self):
        endpoints = settings_endpoints()
        self.assertIn("ticket-from-settings", endpoints)
        endpoints["ticket-from-settings"] = {"source": "changed"}
        self.assertNotEqual(settings_endpoints()["ticket-from-settings"]["source"], "changed")

    def test_empty_overrides_still_resolve(self):
        self.assertEqual(resolve_config(None)["enabled"], True)


class IntervalTests(SimpleTestCase):
    def config(self, **overrides):
        return resolve_config({"jitter": 0, **overrides})

    def test_fixed_backoff_stays_flat(self):
        config = self.config(min_poll_interval=5, backoff="fixed")
        for streak in range(5):
            self.assertEqual(compute_next_interval(config, unchanged_streak=streak), 5)

    def test_exponential_backoff_grows_then_caps(self):
        config = self.config(
            min_poll_interval=2,
            suggested_poll_interval=2,
            backoff="exponential",
            backoff_factor=2.0,
            max_poll_interval=16,
        )
        seen = [compute_next_interval(config, unchanged_streak=s) for s in range(5)]
        self.assertEqual(seen, [2, 4, 8, 16, 16])

    def test_client_may_request_a_slower_cadence(self):
        config = self.config(min_poll_interval=5, max_poll_interval=120)
        self.assertEqual(compute_next_interval(config, requested=30), 30)

    def test_client_cannot_undercut_the_floor(self):
        config = self.config(min_poll_interval=5)
        self.assertEqual(compute_next_interval(config, requested=1), 5)

    def test_client_request_cannot_beat_active_backoff(self):
        # The server has backed off to 8s; a client asking for 2s still gets 8.
        config = self.config(
            min_poll_interval=1,
            suggested_poll_interval=2,
            backoff="exponential",
            backoff_factor=2.0,
        )
        self.assertEqual(compute_next_interval(config, unchanged_streak=2, requested=2), 8)

    def test_requested_interval_is_capped_at_max(self):
        config = self.config(min_poll_interval=5, max_poll_interval=60)
        self.assertEqual(compute_next_interval(config, requested=9999), 60)

    def test_jitter_stays_within_bounds(self):
        config = resolve_config(
            {"min_poll_interval": 10, "max_poll_interval": 100, "jitter": 0.2}
        )
        values = {compute_next_interval(config) for _ in range(200)}
        self.assertTrue(all(10 <= value <= 13 for value in values), values)
        # With jitter on, we should not always land on the same number.
        self.assertGreater(len(values), 1)

    def test_jitter_never_drops_below_the_floor(self):
        config = resolve_config(
            {
                "min_poll_interval": 10,
                "suggested_poll_interval": 10,
                "max_poll_interval": 100,
                "jitter": 0.9,
            }
        )
        values = [compute_next_interval(config) for _ in range(300)]
        self.assertTrue(all(value >= 10 for value in values), values)
        self.assertGreater(len(set(values)), 1)

    def test_jitter_can_be_suppressed(self):
        config = resolve_config(
            {"min_poll_interval": 10, "max_poll_interval": 100, "jitter": 0.5}
        )
        self.assertEqual(compute_next_interval(config, apply_jitter=False), 10)

    def test_non_exponential_backoff_ignores_the_streak(self):
        config = self.config(min_poll_interval=5, backoff="fixed")
        self.assertEqual(compute_next_interval(config, unchanged_streak=6), 5)

    def test_backoff_factor_of_one_does_not_grow(self):
        config = self.config(
            min_poll_interval=4,
            suggested_poll_interval=4,
            backoff="exponential",
            backoff_factor=1.0,
            max_poll_interval=100,
        )
        seen = [compute_next_interval(config, unchanged_streak=s) for s in range(4)]
        self.assertEqual(seen, [4, 4, 4, 4])

    def test_fractional_interval_rounds_to_the_nearest_second(self):
        config = self.config(min_poll_interval=1, suggested_poll_interval=2.6, max_poll_interval=10)
        self.assertEqual(compute_next_interval(config), 3)

    def test_sub_second_result_is_at_least_one(self):
        # Retry-After is an integer number of seconds; 0 would tell clients to loop.
        config = self.config(
            min_poll_interval=0.2, suggested_poll_interval=0.2, max_poll_interval=10
        )
        self.assertEqual(compute_next_interval(config), 1)


class RequestedIntervalTests(SimpleTestCase):
    def config(self, **overrides):
        return resolve_config(overrides)

    def request(self, params):
        return SimpleNamespace(query_params=params)

    def test_reads_a_positive_number(self):
        config = self.config()
        self.assertEqual(parse_requested_interval(self.request({"interval": "4.5"}), config), 4.5)

    def test_blank_zero_and_negative_are_ignored(self):
        config = self.config()
        for raw in ("", "0", "0.0", "-1", "-0.2"):
            self.assertIsNone(parse_requested_interval(self.request({"interval": raw}), config))

    def test_garbage_is_ignored(self):
        config = self.config()
        self.assertIsNone(parse_requested_interval(self.request({"interval": "soon"}), config))

    def test_missing_param_is_ignored(self):
        self.assertIsNone(parse_requested_interval(self.request({}), self.config()))

    def test_disabled_param_name_ignores_the_query_string(self):
        config = self.config(client_interval_param=None)
        self.assertIsNone(parse_requested_interval(self.request({"interval": "30"}), config))

    def test_custom_param_name(self):
        config = self.config(client_interval_param="every")
        request = self.request({"every": "12", "interval": "99"})
        self.assertEqual(parse_requested_interval(request, config), 12.0)

    def test_request_without_query_params_is_ignored(self):
        self.assertIsNone(parse_requested_interval(SimpleNamespace(), self.config()))


class DoneWhenTests(SimpleTestCase):
    def test_none_is_never_done(self):
        self.assertFalse(resolve_done(None, "anything"))

    def test_collection_membership(self):
        self.assertTrue(resolve_done({"delivered", "cancelled"}, "delivered"))
        self.assertFalse(resolve_done({"delivered"}, "pending"))

    def test_scalar_equality(self):
        self.assertTrue(resolve_done("closed", "closed"))

    def test_callable(self):
        self.assertTrue(resolve_done(lambda value: value > 10, 11))
        self.assertFalse(resolve_done(lambda value: value > 10, 10))

    def test_list_tuple_and_frozenset(self):
        self.assertTrue(resolve_done(["delivered", "cancelled"], "cancelled"))
        self.assertTrue(resolve_done(("delivered",), "delivered"))
        self.assertTrue(resolve_done(frozenset({"closed"}), "closed"))
        self.assertFalse(resolve_done(("open",), "closed"))

    def test_string_is_equality_not_character_membership(self):
        # A string is iterable, but done_when="delivered" must not match "d".
        self.assertFalse(resolve_done("delivered", "d"))
        self.assertTrue(resolve_done("delivered", "delivered"))

    def test_unhashable_subject_against_collection(self):
        # A list value must not blow up a membership test.
        self.assertFalse(resolve_done({"a"}, ["a"]))


class VersionTests(SimpleTestCase):
    def test_same_payload_yields_same_version(self):
        self.assertEqual(make_version({"a": 1}), make_version({"a": 1}))

    def test_different_payload_yields_different_version(self):
        self.assertNotEqual(make_version({"a": 1}), make_version({"a": 2}))

    def test_key_order_does_not_matter(self):
        self.assertEqual(make_version({"a": 1, "b": 2}), make_version({"b": 2, "a": 1}))

    def test_etag_is_quoted(self):
        self.assertTrue(etag_for("abc").startswith('"'))
        self.assertTrue(etag_for("abc").endswith('"'))

    def test_list_order_changes_the_version(self):
        self.assertNotEqual(make_version([1, 2]), make_version([2, 1]))

    def test_nested_key_order_does_not_matter(self):
        self.assertEqual(
            make_version([{"b": 1, "a": 2}]),
            make_version([{"a": 2, "b": 1}]),
        )

    def test_non_json_values_are_stringified(self):
        moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.assertEqual(make_version({"at": moment}), make_version({"at": moment}))
        self.assertNotEqual(make_version({"at": moment}), make_version({"at": "other"}))


class ConditionalHeaderTests(SimpleTestCase):
    def request(self, **meta):
        return SimpleNamespace(META=meta)

    def test_absent_if_none_match_does_not_match(self):
        self.assertFalse(if_none_match_matches(self.request(), etag_for("abc")))

    def test_weak_etag_matches_the_strong_one(self):
        etag = etag_for("abc")
        request = self.request(HTTP_IF_NONE_MATCH=f"W/{etag}")
        self.assertTrue(if_none_match_matches(request, etag))

    def test_one_of_several_etags_can_match(self):
        etag = etag_for("abc")
        header = f'"stale", {etag}'
        self.assertTrue(if_none_match_matches(self.request(HTTP_IF_NONE_MATCH=header), etag))

    def test_malformed_etag_does_not_match(self):
        request = self.request(HTTP_IF_NONE_MATCH='"unterminated')
        self.assertFalse(if_none_match_matches(request, etag_for("abc")))

    def test_blank_if_none_match_does_not_match(self):
        request = self.request(HTTP_IF_NONE_MATCH="   ")
        self.assertFalse(if_none_match_matches(request, etag_for("a")))

    def test_if_modified_since_needs_a_timestamp(self):
        request = self.request(HTTP_IF_MODIFIED_SINCE="Thu, 01 Jan 2026 00:00:00 GMT")
        self.assertFalse(if_modified_since_satisfied(request, None))

    def test_if_modified_since_needs_a_header(self):
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertFalse(if_modified_since_satisfied(self.request(), moment))

    def test_garbage_if_modified_since_does_not_match(self):
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        request = self.request(HTTP_IF_MODIFIED_SINCE="not-a-date")
        self.assertFalse(if_modified_since_satisfied(request, moment))

    def test_older_copy_is_modified(self):
        moment = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        earlier = format_last_modified(datetime(2026, 1, 1, tzinfo=timezone.utc))
        request = self.request(HTTP_IF_MODIFIED_SINCE=earlier)
        self.assertFalse(if_modified_since_satisfied(request, moment))

    def test_same_http_date_is_not_modified(self):
        moment = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        request = self.request(HTTP_IF_MODIFIED_SINCE=format_last_modified(moment))
        self.assertTrue(if_modified_since_satisfied(request, moment))

    def test_sunset_string_is_passed_through(self):
        header = "Wed, 01 Jan 2027 00:00:00 GMT"
        self.assertEqual(format_sunset(header), header)

    def test_sunset_date_and_datetime_become_http_dates(self):
        for value in (
            date(2027, 6, 15),
            datetime(2027, 6, 15, 8, 30, tzinfo=timezone.utc),
            datetime(2027, 6, 15, 8, 30),
        ):
            formatted = format_sunset(value)
            self.assertIn("GMT", formatted)
            self.assertNotIn("2027-06-15", formatted)


class EnvelopeTests(SimpleTestCase):
    def result(self, **kwargs):
        defaults = {"value": "pending"}
        defaults.update(kwargs)
        return PollResult(**defaults)

    def test_envelope_carries_metadata(self):
        body = build_envelope(
            self.result(done=1),
            version="abc",
            next_poll_in=5,
            config={"envelope": True},
        )
        self.assertEqual(body["value"], "pending")
        self.assertEqual(body["version"], "abc")
        self.assertIs(body["done"], True)
        self.assertEqual(body["next_poll_in"], 5)
        stamp = parse_datetime(body["polled_at"])
        self.assertIsNotNone(stamp)

    def test_zero_progress_and_eta_are_included(self):
        body = build_envelope(
            self.result(progress=0.0, eta_seconds=0.0),
            version="abc",
            next_poll_in=5,
            config={"envelope": True},
        )
        self.assertEqual(body["progress"], 0.0)
        self.assertEqual(body["eta_seconds"], 0.0)

    def test_missing_progress_is_omitted(self):
        body = build_envelope(
            self.result(),
            version="abc",
            next_poll_in=5,
            config={"envelope": True},
        )
        self.assertNotIn("progress", body)
        self.assertNotIn("eta_seconds", body)

    def test_reserved_extra_keys_cannot_shadow_the_envelope(self):
        body = build_envelope(
            self.result(extra={"value": "nope", "version": "nope", "display": "Pending"}),
            version="abc",
            next_poll_in=5,
            config={"envelope": True},
        )
        self.assertEqual(body["value"], "pending")
        self.assertEqual(body["version"], "abc")
        self.assertEqual(body["display"], "Pending")

    def test_null_extra_is_ignored(self):
        body = build_envelope(
            self.result(extra=None),
            version="abc",
            next_poll_in=5,
            config={"envelope": True},
        )
        self.assertEqual(set(body), {"value", "version", "done", "next_poll_in", "polled_at"})

    def test_bare_mode_returns_the_value_only(self):
        body = build_envelope(
            self.result(progress=10, extra={"display": "Pending"}),
            version="abc",
            next_poll_in=5,
            config={"envelope": False},
        )
        self.assertEqual(body, "pending")


class ClientStateTests(TestCase):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache

        cache.clear()
        self.store = PollStateStore()

    def request(self, *, user=None, meta=None):
        return SimpleNamespace(user=user, META=meta or {})

    def test_authenticated_user_wins_over_forwarded_ip(self):
        user = SimpleNamespace(is_authenticated=True, pk=7)
        request = self.request(user=user, meta={"HTTP_X_FORWARDED_FOR": "1.2.3.4"})
        self.assertEqual(client_identifier(request), "u7")

    def test_leftmost_forwarded_address_is_the_client(self):
        request = self.request(meta={"HTTP_X_FORWARDED_FOR": "1.2.3.4, 5.6.7.8"})
        self.assertEqual(client_identifier(request), "ip1.2.3.4")

    def test_remote_addr_is_used_when_there_is_no_forwarded_header(self):
        request = self.request(meta={"REMOTE_ADDR": "10.0.0.8"})
        self.assertEqual(client_identifier(request), "ip10.0.0.8")

    def test_missing_address_falls_back_to_anon(self):
        self.assertEqual(client_identifier(self.request()), "anon")

    def test_state_key_depends_on_endpoint_client_and_object(self):
        request = self.request(meta={"REMOTE_ADDR": "10.0.0.8"})
        other = self.request(meta={"REMOTE_ADDR": "10.0.0.9"})
        base = state_key("order-status", request, 1)
        self.assertEqual(base, state_key("order-status", request, 1))
        self.assertNotEqual(base, state_key("order-status", request, 2))
        self.assertNotEqual(base, state_key("order-status", other, 1))
        self.assertNotEqual(base, state_key("other", request, 1))
        self.assertTrue(base.startswith("drf_pollable:state:"))

    def test_first_poll_starts_both_streaks_at_zero(self):
        state = self.store.record("k", version="a", previous=ClientPollState())
        self.assertEqual(state.streak, 0)
        self.assertEqual(state.change_streak, 0)
        self.assertEqual(state.version, "a")
        self.assertIsNotNone(state.last_poll)

    def test_unchanged_version_grows_only_the_backoff_streak(self):
        first = self.store.record("k", version="a", previous=ClientPollState())
        second = self.store.record("k", version="a", previous=first)
        self.assertEqual(second.streak, 1)
        self.assertEqual(second.change_streak, 0)

    def test_changed_version_grows_only_the_change_streak(self):
        first = self.store.record("k", version="a", previous=ClientPollState())
        second = self.store.record("k", version="b", previous=first)
        third = self.store.record("k", version="c", previous=second)
        self.assertEqual(third.streak, 0)
        self.assertEqual(third.change_streak, 2)

    def test_an_unchanged_poll_clears_the_change_streak(self):
        first = self.store.record("k", version="a", previous=ClientPollState())
        changed = self.store.record("k", version="b", previous=first)
        settled = self.store.record("k", version="b", previous=changed)
        self.assertEqual(settled.change_streak, 0)
        self.assertEqual(settled.streak, 1)

    def test_stored_state_round_trips(self):
        saved = self.store.record("k", version="a", previous=ClientPollState())
        loaded = self.store.get("k")
        self.assertEqual(loaded.version, "a")
        self.assertEqual(loaded.streak, saved.streak)
        self.assertEqual(loaded.change_streak, saved.change_streak)
        self.assertEqual(loaded.last_poll, saved.last_poll)

    def test_missing_or_garbage_cache_entries_look_like_a_first_poll(self):
        self.assertEqual(self.store.get("missing"), ClientPollState())
        self.store.cache.set("k", "not-a-dict")
        self.assertEqual(self.store.get("k"), ClientPollState())

    def test_touch_updates_last_poll_without_moving_streaks(self):
        self.store.record("k", version="a", previous=ClientPollState(version="a", streak=4))
        before = self.store.get("k")
        self.store.touch("k")
        after = self.store.get("k")
        self.assertEqual(after.version, "a")
        self.assertEqual(after.streak, before.streak)
        self.assertGreaterEqual(after.last_poll, before.last_poll)

    def test_seconds_since_last_poll(self):
        state = ClientPollState()
        self.assertIsNone(state.seconds_since_last_poll())
        state.last_poll = 1_000.0
        self.assertEqual(state.seconds_since_last_poll(now=1_030.0), 30.0)

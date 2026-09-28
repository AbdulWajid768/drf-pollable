"""Unit tests for the pure helpers: config resolution, intervals, headers."""

from django.test import SimpleTestCase, override_settings

from drf_pollable.conf import BUILTIN_DEFAULTS, ImproperlyConfiguredDefaults, resolve_config
from drf_pollable.headers import etag_for, make_version
from drf_pollable.intervals import compute_next_interval
from drf_pollable.sources import resolve_done


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

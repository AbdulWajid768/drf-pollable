"""Core polling behaviour: value, envelope, headers, 304, done_when."""

import time
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone as dt_timezone
from unittest import mock

from drf_pollable.headers import format_last_modified

from .base import PollTestCase
from .testapp.models import Order


def _now_plus(seconds: int) -> datetime:
    return datetime.fromtimestamp(int(time.time()) + seconds, tz=dt_timezone.utc)


@contextmanager
def _one_second_later():
    """Run the block as if a second of wall-clock had passed.

    Last-Modified has one-second resolution, so a test that re-polls instantly
    cannot exercise the 304 path at all; sleeping for real would just make the
    suite slower.
    """
    later = time.time() + 1
    with (
        mock.patch("drf_pollable.headers.time.time", return_value=later),
        mock.patch("drf_pollable.state.time.time", return_value=later),
    ):
        yield


class FieldPollingTests(PollTestCase):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(status="pending")
        self.url = f"/poll/order-status/{self.order.pk}/"

    def test_returns_current_field_value(self):
        body = self.poll(self.url).json()
        self.assertEqual(body["value"], "pending")

    def test_reflects_a_change(self):
        Order.objects.filter(pk=self.order.pk).update(status="shipped")
        self.assertEqual(self.poll(self.url).json()["value"], "shipped")

    def test_envelope_shape(self):
        body = self.poll(self.url).json()
        self.assertEqual(
            set(body) & {"value", "version", "done", "next_poll_in", "polled_at"},
            {"value", "version", "done", "next_poll_in", "polled_at"},
        )

    def test_choice_display_label_is_included(self):
        # The frontend would otherwise have to duplicate the choices mapping.
        self.assertEqual(self.poll(self.url).json()["display"], "Pending")

    def test_retry_after_matches_configured_interval(self):
        response = self.poll(self.url)
        self.assertEqual(response["Retry-After"], "5")
        self.assertEqual(response.json()["next_poll_in"], 5)

    def test_cache_control_allows_revalidation_but_not_stale_reuse(self):
        self.assertEqual(self.poll(self.url)["Cache-Control"], "no-cache, private")

    def test_missing_object_is_404(self):
        self.assertEqual(self.poll("/poll/order-status/999999/").status_code, 404)

    def test_non_numeric_lookup_is_404_not_500(self):
        self.assertEqual(self.poll("/poll/order-status/not-an-id/").status_code, 404)

    def test_only_get_is_allowed(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)
        for method in ("put", "patch", "delete"):
            self.assertEqual(getattr(self.client, method)(self.url).status_code, 405)

    def test_head_returns_the_polling_headers(self):
        response = self.client.head(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("ETag", response)
        self.assertIn("Retry-After", response)

    def test_options_advertises_get(self):
        response = self.client.options(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("GET", response["Allow"])

    def test_etag_header_quotes_the_body_version(self):
        response = self.poll(self.url)
        self.assertEqual(response["ETag"], f'"{response.json()["version"]}"')


class DoneWhenTests(PollTestCase):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(status="pending")
        self.url = f"/poll/order-status/{self.order.pk}/"

    def test_not_done_while_in_progress(self):
        self.assertFalse(self.poll(self.url).json()["done"])

    def test_done_at_a_terminal_value(self):
        Order.objects.filter(pk=self.order.pk).update(status="delivered")
        self.assertTrue(self.poll(self.url).json()["done"])

    def test_done_for_every_terminal_value(self):
        Order.objects.filter(pk=self.order.pk).update(status="cancelled")
        self.assertTrue(self.poll(self.url).json()["done"])


class ConditionalRequestTests(PollTestCase):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(status="pending")
        self.url = f"/poll/order-status/{self.order.pk}/"

    def test_etag_is_sent(self):
        self.assertIn("ETag", self.poll(self.url))

    def test_unchanged_value_returns_304_without_a_body(self):
        etag = self.poll(self.url)["ETag"]
        response = self.poll(self.url, etag=etag)
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")

    def test_304_still_advertises_retry_after(self):
        # The client must still know when to come back.
        etag = self.poll(self.url)["ETag"]
        self.assertIn("Retry-After", self.poll(self.url, etag=etag))

    def test_changed_value_returns_200_with_new_etag(self):
        first = self.poll(self.url)
        Order.objects.filter(pk=self.order.pk).update(status="shipped")
        second = self.poll(self.url, etag=first["ETag"])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["value"], "shipped")
        self.assertNotEqual(second["ETag"], first["ETag"])

    def test_stale_etag_does_not_short_circuit(self):
        response = self.poll(self.url, etag='"definitely-not-current"')
        self.assertEqual(response.status_code, 200)

    def test_wildcard_if_none_match_returns_304(self):
        self.assertEqual(self.poll(self.url, etag="*").status_code, 304)

    def test_last_modified_is_sent(self):
        # Order has updated_at, which is auto-detected as the version field.
        self.assertIn("Last-Modified", self.poll(self.url))

    def test_if_modified_since_returns_304(self):
        last_modified = self.poll(self.url)["Last-Modified"]
        # Poll again a second later, as any real client would: Last-Modified is
        # a weak validator and says nothing within the second it was issued.
        with _one_second_later():
            response = self.client.get(self.url, HTTP_IF_MODIFIED_SINCE=last_modified)
        self.assertEqual(response.status_code, 304)

    def test_stale_if_modified_since_returns_200(self):
        response = self.client.get(self.url, HTTP_IF_MODIFIED_SINCE="Thu, 01 Jan 1970 00:00:00 GMT")
        self.assertEqual(response.status_code, 200)

    def test_garbage_if_modified_since_returns_200(self):
        response = self.client.get(self.url, HTTP_IF_MODIFIED_SINCE="not-a-date")
        self.assertEqual(response.status_code, 200)

    def test_if_modified_since_sees_a_change_auto_now_missed(self):
        # QuerySet.update() does not fire auto_now, so updated_at still holds
        # the original time while the value has genuinely changed. Answering
        # If-Modified-Since from that column alone would strand a client on a
        # 304 forever -- the same stale-304 trap make_version avoids for ETag.
        first = self.poll(self.url)
        last_modified = first["Last-Modified"]
        before = Order.objects.values_list("updated_at", flat=True).get(pk=self.order.pk)

        Order.objects.filter(pk=self.order.pk).update(status="shipped")

        after = Order.objects.values_list("updated_at", flat=True).get(pk=self.order.pk)
        self.assertEqual(before, after, "precondition: auto_now must not have fired")

        response = self.client.get(self.url, HTTP_IF_MODIFIED_SINCE=last_modified)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["value"], "shipped")

    def test_last_modified_advances_when_the_value_changes(self):
        self.poll(self.url)
        Order.objects.filter(pk=self.order.pk).update(status="shipped")
        with _one_second_later():
            second = self.poll(self.url)["Last-Modified"]
        # The new value was observed a second after the old one, so the
        # timestamp must have moved with it rather than tracking updated_at.
        self.assertEqual(second, format_last_modified(_now_plus(1)))

    def test_unchanged_value_keeps_a_stable_last_modified(self):
        # The timestamp must not creep forward on every poll, or a client
        # using If-Modified-Since would never get a 304.
        first = self.poll(self.url)["Last-Modified"]
        self.assertEqual(self.poll(self.url)["Last-Modified"], first)

    def test_if_none_match_wins_over_if_modified_since(self):
        # RFC 9110: a present If-None-Match is decisive, even when it misses
        # and If-Modified-Since would have produced a 304.
        last_modified = self.poll(self.url)["Last-Modified"]
        response = self.client.get(
            self.url,
            HTTP_IF_NONE_MATCH='"not-the-current-etag"',
            HTTP_IF_MODIFIED_SINCE=last_modified,
        )
        self.assertEqual(response.status_code, 200)

    def test_weak_etag_matches(self):
        etag = self.poll(self.url)["ETag"]
        response = self.client.get(self.url, HTTP_IF_NONE_MATCH=f"W/{etag}")
        self.assertEqual(response.status_code, 304)

    def test_one_matching_etag_among_several_returns_304(self):
        etag = self.poll(self.url)["ETag"]
        response = self.client.get(self.url, HTTP_IF_NONE_MATCH=f'"stale", {etag}')
        self.assertEqual(response.status_code, 304)

    def test_malformed_etag_does_not_short_circuit(self):
        response = self.client.get(self.url, HTTP_IF_NONE_MATCH='"unterminated')
        self.assertEqual(response.status_code, 200)

    def test_304_still_carries_etag_and_cache_control(self):
        etag = self.poll(self.url)["ETag"]
        response = self.poll(self.url, etag=etag)
        self.assertEqual(response["ETag"], etag)
        self.assertEqual(response["Cache-Control"], "no-cache, private")


class EnvelopeTests(PollTestCase):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(status="pending")

    def test_bare_mode_returns_the_value_itself(self):
        response = self.poll(f"/poll/order-bare/{self.order.pk}/")
        self.assertEqual(response.json(), "pending")
        # Metadata is still available in headers.
        self.assertIn("Retry-After", response)
        self.assertIn("ETag", response)

    def test_bare_mode_still_supports_304(self):
        url = f"/poll/order-bare/{self.order.pk}/"
        etag = self.poll(url)["ETag"]
        response = self.poll(url, etag=etag)
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")

    def test_instance_source_returns_only_allow_listed_fields(self):
        self.order.progress = 40
        self.order.eta_seconds = 12
        self.order.save()
        body = self.poll(f"/poll/order-detail/{self.order.pk}/").json()
        self.assertEqual(body["value"], {"status": "pending", "progress": 40})
        self.assertEqual(body["progress"], 40.0)
        self.assertEqual(body["eta_seconds"], 12.0)


class LifecycleHeaderTests(PollTestCase):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(status="pending")

    def test_disabled_endpoint_returns_503_with_retry_after(self):
        response = self.poll(f"/poll/order-disabled/{self.order.pk}/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "3600")

    def test_disabled_endpoint_does_not_look_up_the_object(self):
        # The kill switch runs before the source, so a missing row is still 503.
        response = self.poll("/poll/order-disabled/999999/")
        self.assertEqual(response.status_code, 503)

    def test_deprecated_endpoint_advertises_sunset(self):
        response = self.poll(f"/poll/order-legacy/{self.order.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Sunset"], "2027-01-01")
        self.assertEqual(response["Deprecation"], "true")
        self.assertIn("Use order-status instead.", response["Link"])


class ValueCacheTests(PollTestCase):
    def test_cached_endpoint_serves_the_cached_value(self):
        order = Order.objects.create(status="pending")
        url = f"/poll/order-cached/{order.pk}/"
        self.assertEqual(self.poll(url).json()["value"], "pending")

        Order.objects.filter(pk=order.pk).update(status="shipped")
        # Still cached, so the database change is not visible yet.
        self.assertEqual(self.poll(url).json()["value"], "pending")

    def test_uncached_endpoint_sees_the_change_immediately(self):
        order = Order.objects.create(status="pending")
        url = f"/poll/order-status/{order.pk}/"
        self.poll(url)
        Order.objects.filter(pk=order.pk).update(status="shipped")
        self.assertEqual(self.poll(url).json()["value"], "shipped")

    def test_cache_is_not_shared_across_users(self):
        from django.contrib.auth.models import User

        order = Order.objects.create(status="pending")
        url = f"/poll/order-cached/{order.pk}/"
        alice = User.objects.create_user("cache-alice")
        bob = User.objects.create_user("cache-bob")

        self.client.force_login(alice)
        self.assertEqual(self.poll(url).json()["value"], "pending")
        Order.objects.filter(pk=order.pk).update(status="shipped")

        self.client.force_login(bob)
        self.assertEqual(self.poll(url).json()["value"], "shipped")

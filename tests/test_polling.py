"""Core polling behaviour: value, envelope, headers, 304, done_when."""

from .base import PollTestCase
from .testapp.models import Order


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
        response = self.client.get(self.url, HTTP_IF_MODIFIED_SINCE=last_modified)
        self.assertEqual(response.status_code, 304)


class EnvelopeTests(PollTestCase):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(status="pending")

    def test_bare_mode_returns_the_value_itself(self):
        response = self.poll(f"/poll/order-bare/{self.order.pk}/")
        self.assertEqual(response.json(), "pending")
        # Metadata is still available in headers.
        self.assertIn("Retry-After", response)

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
        self.assertIn("Retry-After", response)

    def test_deprecated_endpoint_advertises_sunset(self):
        response = self.poll(f"/poll/order-legacy/{self.order.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sunset", response)
        self.assertEqual(response["Deprecation"], "true")


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

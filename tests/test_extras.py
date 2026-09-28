"""Upgrade hints, jitter, and the public testing helpers."""

from drf_pollable.testing import (
    assert_not_modified,
    assert_poll_headers,
    clear_poll_state,
    poll_once,
    poll_until,
)

from .base import PollTestCase
from .testapp.models import Order


class UpgradeHintTests(PollTestCase):
    def test_hint_appears_when_the_value_changes_every_poll(self):
        order = Order.objects.create(status="pending")
        url = f"/poll/order-chatty/{order.pk}/"

        self.assertNotIn("X-Poll-Upgrade-Hint", self.poll(url))

        Order.objects.filter(pk=order.pk).update(status="shipped")
        self.assertNotIn("X-Poll-Upgrade-Hint", self.poll(url))

        Order.objects.filter(pk=order.pk).update(status="delivered")
        self.assertIn("X-Poll-Upgrade-Hint", self.poll(url))

    def test_hint_is_absent_for_a_settled_value(self):
        order = Order.objects.create(status="pending")
        url = f"/poll/order-chatty/{order.pk}/"
        for _ in range(4):
            response = self.poll(url)
        self.assertNotIn("X-Poll-Upgrade-Hint", response)

    def test_hint_is_off_by_default(self):
        order = Order.objects.create(status="pending")
        url = f"/poll/order-status/{order.pk}/"
        for status_value in ("shipped", "delivered", "cancelled"):
            Order.objects.filter(pk=order.pk).update(status=status_value)
            response = self.poll(url)
        self.assertNotIn("X-Poll-Upgrade-Hint", response)


class JitterTests(PollTestCase):
    def test_advertised_interval_varies_but_stays_in_range(self):
        order = Order.objects.create(status="pending")
        url = f"/poll/order-jittered/{order.pk}/"
        seen = {int(self.poll(url)["Retry-After"]) for _ in range(40)}
        self.assertTrue(all(10 <= value <= 13 for value in seen), seen)
        self.assertGreater(len(seen), 1, "jitter should de-synchronise clients")


class TestingHelperTests(PollTestCase):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(status="pending")
        self.url = f"/poll/order-status/{self.order.pk}/"

    def test_assert_poll_headers_passes_for_a_real_response(self):
        assert_poll_headers(self.client.get(self.url))

    def test_assert_poll_headers_fails_without_retry_after(self):
        with self.assertRaises(AssertionError):
            assert_poll_headers(_HeaderlessResponse())

    def test_poll_once_sends_if_none_match(self):
        etag = self.client.get(self.url)["ETag"]
        assert_not_modified(poll_once(self.client, self.url, etag=etag))

    def test_poll_until_returns_the_matching_response(self):
        def advance(attempt):
            if attempt == 2:
                Order.objects.filter(pk=self.order.pk).update(status="delivered")

        response = poll_until(
            self.client, self.url, lambda body: body["done"], max_polls=5, advance=advance
        )
        self.assertEqual(response.json()["value"], "delivered")

    def test_poll_until_raises_when_it_never_matches(self):
        with self.assertRaises(AssertionError):
            poll_until(self.client, self.url, lambda body: body["done"], max_polls=2)

    def test_clear_poll_state_resets_backoff(self):
        url = f"/poll/order-backoff/{self.order.pk}/"
        self.client.get(url)
        self.client.get(url)
        self.assertEqual(self.client.get(url)["Retry-After"], "8")

        clear_poll_state()
        self.assertEqual(self.client.get(url)["Retry-After"], "2")


class _HeaderlessResponse:
    def __contains__(self, key):
        return False

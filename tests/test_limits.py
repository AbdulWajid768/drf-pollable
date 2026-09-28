"""Limits: interval negotiation, enforcement, throttling, and scoping."""

from django.contrib.auth.models import User

from .base import PollTestCase
from .testapp.models import Order


class IntervalNegotiationTests(PollTestCase):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(status="pending")

    def test_client_may_ask_to_poll_more_slowly(self):
        response = self.poll(f"/poll/order-interval/{self.order.pk}/", interval=30)
        self.assertEqual(response["Retry-After"], "30")
        self.assertEqual(response.json()["next_poll_in"], 30)

    def test_client_cannot_ask_to_poll_faster_than_the_floor(self):
        response = self.poll(f"/poll/order-interval/{self.order.pk}/", interval=1)
        self.assertEqual(response["Retry-After"], "5")

    def test_client_request_is_capped_at_the_maximum(self):
        response = self.poll(f"/poll/order-interval/{self.order.pk}/", interval=99999)
        self.assertEqual(response["Retry-After"], "120")

    def test_garbage_interval_is_ignored(self):
        response = self.poll(f"/poll/order-interval/{self.order.pk}/", interval="soon")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Retry-After"], "5")


class BackoffTests(PollTestCase):
    def test_interval_grows_while_the_value_is_unchanged(self):
        order = Order.objects.create(status="pending")
        url = f"/poll/order-backoff/{order.pk}/"
        intervals = [int(self.poll(url)["Retry-After"]) for _ in range(4)]
        self.assertEqual(intervals, [2, 4, 8, 16])

    def test_backoff_resets_when_the_value_changes(self):
        order = Order.objects.create(status="pending")
        url = f"/poll/order-backoff/{order.pk}/"
        self.poll(url)
        self.poll(url)
        self.assertEqual(int(self.poll(url)["Retry-After"]), 8)

        Order.objects.filter(pk=order.pk).update(status="shipped")
        self.assertEqual(int(self.poll(url)["Retry-After"]), 2)

    def test_backoff_is_capped(self):
        order = Order.objects.create(status="pending")
        url = f"/poll/order-backoff/{order.pk}/"
        for _ in range(10):
            last = int(self.poll(url)["Retry-After"])
        self.assertEqual(last, 60)


class IntervalEnforcementTests(PollTestCase):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(status="pending")
        self.url = f"/poll/order-enforced/{self.order.pk}/"

    def test_first_poll_is_allowed(self):
        self.assertEqual(self.poll(self.url).status_code, 200)

    def test_polling_too_soon_is_rejected(self):
        self.assertEqual(self.poll(self.url).status_code, 200)
        response = self.poll(self.url)
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)

    def test_enforcement_is_per_object(self):
        # Being early on one order must not lock the client out of another.
        other = Order.objects.create(status="pending")
        self.assertEqual(self.poll(self.url).status_code, 200)
        self.assertEqual(self.poll(f"/poll/order-enforced/{other.pk}/").status_code, 200)

    def test_endpoints_without_enforcement_allow_rapid_polling(self):
        url = f"/poll/order-status/{self.order.pk}/"
        self.assertEqual(self.poll(url).status_code, 200)
        self.assertEqual(self.poll(url).status_code, 200)


class ThrottleScopeTests(PollTestCase):
    def test_scoped_rate_limit_applies(self):
        order = Order.objects.create(status="pending")
        url = f"/poll/order-throttled/{order.pk}/"
        # settings: DEFAULT_THROTTLE_RATES = {"tight": "2/min"}
        self.assertEqual(self.poll(url).status_code, 200)
        self.assertEqual(self.poll(url).status_code, 200)
        response = self.poll(url)
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)

    def test_unscoped_endpoints_are_not_rate_limited(self):
        order = Order.objects.create(status="pending")
        url = f"/poll/order-status/{order.pk}/"
        for _ in range(5):
            self.assertEqual(self.poll(url).status_code, 200)


class ScopingAndPermissionTests(PollTestCase):
    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user("alice", password="pw")
        self.bob = User.objects.create_user("bob", password="pw")
        self.alice_order = Order.objects.create(status="pending", owner=self.alice)

    def url(self, order):
        return f"/poll/order-scoped/{order.pk}/"

    def test_anonymous_access_is_denied(self):
        self.assertIn(self.poll(self.url(self.alice_order)).status_code, (401, 403))

    def test_owner_can_poll_their_own_record(self):
        self.client.force_login(self.alice)
        response = self.poll(self.url(self.alice_order))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["value"], "pending")

    def test_other_users_cannot_poll_someone_elses_record(self):
        # The scope filters the queryset, so the record is simply not there.
        self.client.force_login(self.bob)
        self.assertEqual(self.poll(self.url(self.alice_order)).status_code, 404)

"""Throttle classes beyond the per-endpoint scope already covered in test_limits."""

from contextlib import contextmanager
from types import SimpleNamespace

from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from rest_framework.throttling import BaseThrottle, SimpleRateThrottle

from drf_pollable import registry
from drf_pollable.state import default_store, state_key
from drf_pollable.throttling import (
    GlobalPollThrottle,
    PollBurstThrottle,
    PollIntervalThrottle,
    PollScopedRateThrottle,
    PollSustainedThrottle,
    TenantRateThrottle,
)

from .base import PollTestCase, invoke_registration, json_body
from .testapp.models import Order


@contextmanager
def throttle_rates(**overrides):
    """DRF copies throttle rates onto the class at import, so patch them here."""
    previous = SimpleRateThrottle.THROTTLE_RATES
    SimpleRateThrottle.THROTTLE_RATES = {**previous, **overrides}
    try:
        yield
    finally:
        SimpleRateThrottle.THROTTLE_RATES = previous


def _tenant(request):
    return request.META.get("HTTP_X_TENANT") or None


class IntervalThrottleUnitTests(PollTestCase):
    def view_for(self, registration, pk="1"):
        return SimpleNamespace(
            poll_config=registration.config,
            registration=registration,
            kwargs={"pk": pk},
        )

    def test_disabled_enforcement_always_allows(self):
        view = self.view_for(registry.get("order-status"))
        request = SimpleNamespace(user=None, META={"REMOTE_ADDR": "10.1.1.1"})
        self.assertTrue(PollIntervalThrottle().allow_request(request, view))

    def test_missing_config_or_registration_allows(self):
        request = SimpleNamespace(user=None, META={})
        self.assertTrue(PollIntervalThrottle().allow_request(request, SimpleNamespace()))
        view = SimpleNamespace(poll_config={"enforce_interval": True}, registration=None)
        self.assertTrue(PollIntervalThrottle().allow_request(request, view))

    def test_recent_poll_is_rejected_and_wait_is_reported(self):
        registration = registry.get("order-enforced")
        request = SimpleNamespace(user=None, META={"REMOTE_ADDR": "10.2.2.2"})
        key = state_key(registration.name, request, "1")
        default_store.touch(key)
        throttle = PollIntervalThrottle()
        self.assertFalse(throttle.allow_request(request, self.view_for(registration)))
        self.assertGreaterEqual(throttle.wait(), 1)
        self.assertLessEqual(throttle.wait(), 30)

    def test_a_different_object_is_still_allowed(self):
        registration = registry.get("order-enforced")
        request = SimpleNamespace(user=None, META={"REMOTE_ADDR": "10.3.3.3"})
        default_store.touch(state_key(registration.name, request, "1"))
        throttle = PollIntervalThrottle()
        self.assertTrue(throttle.allow_request(request, self.view_for(registration, pk="2")))

    def test_wait_is_empty_until_a_request_is_rejected(self):
        self.assertIsNone(PollIntervalThrottle().wait())


class NoRateThrottleTests(PollTestCase):
    def test_explicit_null_rate_is_a_no_op(self):
        # DRF treats a rate of None (its own user/anon default) as "not configured".
        request = SimpleNamespace(user=None, META={"REMOTE_ADDR": "10.4.4.4"})
        view = SimpleNamespace()
        with throttle_rates(poll_burst=None, poll_sustained=None, poll_global=None):
            self.assertTrue(PollBurstThrottle().allow_request(request, view))
            self.assertTrue(PollSustainedThrottle().allow_request(request, view))
            self.assertTrue(GlobalPollThrottle().allow_request(request, view))

    def test_a_missing_rate_is_a_configuration_error(self):
        previous = SimpleRateThrottle.THROTTLE_RATES
        SimpleRateThrottle.THROTTLE_RATES = {
            key: value for key, value in previous.items() if key != "poll_burst"
        }
        self.addCleanup(setattr, SimpleRateThrottle, "THROTTLE_RATES", previous)
        with self.assertRaises(ImproperlyConfigured):
            PollBurstThrottle()

    def test_scoped_throttle_is_a_no_op_without_a_scope(self):
        request = SimpleNamespace(user=None, META={})
        view = SimpleNamespace(throttle_scope=None)
        self.assertTrue(PollScopedRateThrottle().allow_request(request, view))

    def test_tenant_throttle_without_a_resolver_allows_the_request(self):
        # A rate is configured, but nobody to bucket means the request is allowed.
        request = SimpleNamespace(user=None, META={})
        view = SimpleNamespace()
        with throttle_rates(poll_tenant="2/min"):
            self.assertTrue(TenantRateThrottle().allow_request(request, view))


class EndpointThrottleTests(PollTestCase):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(status="pending")

    def _register(self, name, **options):
        registration = registry.register(
            name,
            "testapp.Order.status",
            replace=True,
            min_poll_interval=1,
            **options,
        )
        self.addCleanup(registry.unregister, name)
        return registration

    def test_burst_and_sustained_share_a_client_budget(self):
        registration = self._register(
            "order-burst",
            throttle_classes=[PollBurstThrottle, PollSustainedThrottle],
        )
        with throttle_rates(poll_burst="10/min", poll_sustained="2/hour"):
            statuses = [
                invoke_registration(registration, pk=self.order.pk).status_code for _ in range(3)
            ]
        self.assertEqual(statuses, [200, 200, 429])

    def test_global_throttle_is_shared_by_every_caller(self):
        from django.contrib.auth.models import User

        registration = self._register("order-global", throttle_classes=[GlobalPollThrottle])
        other = self._register("order-global-other", throttle_classes=[GlobalPollThrottle])
        alice = User.objects.create_user("global-alice")
        bob = User.objects.create_user("global-bob")

        with throttle_rates(poll_global="2/min"):
            first = invoke_registration(registration, pk=self.order.pk, user=alice)
            second = invoke_registration(registration, pk=self.order.pk, user=alice)
            denied = invoke_registration(registration, pk=self.order.pk, user=bob)
            other_response = invoke_registration(other, pk=self.order.pk, user=bob)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(denied.status_code, 429)
        self.assertIn("Retry-After", denied)
        # A different endpoint has its own ceiling.
        self.assertEqual(other_response.status_code, 200)

    @override_settings(DRF_POLLABLE={"TENANT_RESOLVER": _tenant})
    def test_tenant_throttle_isolates_customers(self):
        registration = self._register("order-tenant", throttle_classes=[TenantRateThrottle])

        def poll(tenant):
            return invoke_registration(registration, pk=self.order.pk, HTTP_X_TENANT=tenant)

        with throttle_rates(poll_tenant="2/min"):
            self.assertEqual(poll("acme").status_code, 200)
            self.assertEqual(poll("acme").status_code, 200)
            self.assertEqual(poll("acme").status_code, 429)
            self.assertEqual(poll("other").status_code, 200)

    @override_settings(DRF_POLLABLE={"TENANT_RESOLVER": _tenant})
    def test_missing_tenant_is_not_throttled(self):
        registration = self._register("order-tenant-open", throttle_classes=[TenantRateThrottle])
        with throttle_rates(poll_tenant="2/min"):
            for _ in range(4):
                response = invoke_registration(registration, pk=self.order.pk)
                self.assertEqual(response.status_code, 200)

    def test_broken_tenant_resolver_is_a_configuration_error(self):
        request = SimpleNamespace(user=None, META={})
        with override_settings(DRF_POLLABLE={"TENANT_RESOLVER": "no.such.Resolver"}):
            with throttle_rates(poll_tenant="2/min"):
                throttle = TenantRateThrottle()
                with self.assertRaises(ImproperlyConfigured):
                    throttle.allow_request(request, SimpleNamespace())

    def test_bring_your_own_throttle_is_honored(self):
        class AlwaysDeny(BaseThrottle):
            def allow_request(self, request, view):
                return False

            def wait(self):
                return 12

        registration = self._register("order-custom-throttle", throttle_classes=[AlwaysDeny])
        response = invoke_registration(registration, pk=self.order.pk)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "12")
        self.assertIn("detail", json_body(response))

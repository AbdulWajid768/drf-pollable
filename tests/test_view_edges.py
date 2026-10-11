"""View behaviour that needs an endpoint registered for the test itself."""

from datetime import date

from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.viewsets import GenericViewSet

from drf_pollable import PollingMixin, registry
from drf_pollable.headers import format_last_modified

from .base import PollTestCase, invoke_registration, json_body
from .testapp.models import Notification, Order


class IsOwner(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        return obj.owner_id == request.user.id


class HeaderSwitchTests(PollTestCase):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(status="pending")

    def _register(self, name, **options):
        registration = registry.register(name, "testapp.Order.status", replace=True, **options)
        self.addCleanup(registry.unregister, name)
        return registration

    def test_etag_can_be_turned_off(self):
        registration = self._register(
            "order-no-etag", send_etag=False, send_last_modified=False, min_poll_interval=1
        )
        response = invoke_registration(registration, pk=self.order.pk, HTTP_IF_NONE_MATCH="*")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("ETag", response)
        self.assertNotIn("Last-Modified", response)
        self.assertEqual(json_body(response)["value"], "pending")

    def test_last_modified_can_be_turned_off(self):
        registration = self._register(
            "order-no-lm", send_etag=False, send_last_modified=False, min_poll_interval=1
        )
        response = invoke_registration(
            registration,
            pk=self.order.pk,
            HTTP_IF_MODIFIED_SINCE=format_last_modified(timezone.now()),
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Last-Modified", response)

    def test_custom_interval_parameter(self):
        registration = self._register(
            "order-every",
            client_interval_param="every",
            min_poll_interval=5,
            max_poll_interval=100,
        )
        slowed = invoke_registration(registration, pk=self.order.pk, params={"every": "40"})
        self.assertEqual(slowed["Retry-After"], "40")
        ignored = invoke_registration(registration, pk=self.order.pk, params={"interval": "40"})
        self.assertEqual(ignored["Retry-After"], "5")

    def test_interval_parameter_can_be_disabled(self):
        registration = self._register(
            "order-no-interval",
            client_interval_param=None,
            min_poll_interval=5,
            max_poll_interval=100,
        )
        response = invoke_registration(registration, pk=self.order.pk, params={"interval": "40"})
        self.assertEqual(response["Retry-After"], "5")

    def test_sunset_date_is_an_http_date(self):
        registration = self._register("order-sunset-date", sunset=date(2027, 6, 15))
        response = invoke_registration(registration, pk=self.order.pk)
        self.assertIn("GMT", response["Sunset"])

    def test_disabled_endpoint_uses_a_fallback_when_the_max_interval_is_zero(self):
        registration = self._register("order-off", enabled=False, max_poll_interval=0)
        response = invoke_registration(registration, pk=self.order.pk)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "60")
        self.assertIn("disabled", json_body(response)["detail"])


class CacheEdgeTests(PollTestCase):
    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user("edge-alice")
        self.bob = User.objects.create_user("edge-bob")
        self.order = Order.objects.create(status="pending", owner=self.alice)

    def _register(self, name, **options):
        registration = registry.register(
            name,
            "testapp.Order.status",
            replace=True,
            cache_timeout=60,
            min_poll_interval=1,
            **options,
        )
        self.addCleanup(registry.unregister, name)
        return registration

    def test_shared_cache_serves_the_same_value_to_every_caller(self):
        registration = self._register("order-shared-cache", cache_vary_on_user=False)
        first = invoke_registration(registration, pk=self.order.pk, user=self.alice)
        self.assertEqual(json_body(first)["value"], "pending")

        Order.objects.filter(pk=self.order.pk).update(status="shipped")
        second = invoke_registration(registration, pk=self.order.pk, user=self.bob)
        self.assertEqual(json_body(second)["value"], "pending")

    def test_object_permission_still_applies_on_a_cache_hit(self):
        registration = self._register("order-owned-cache", permission_classes=[IsOwner])
        allowed = invoke_registration(registration, pk=self.order.pk, user=self.alice)
        self.assertEqual(allowed.status_code, 200)

        self.order.owner = self.bob
        self.order.save()

        denied = invoke_registration(registration, pk=self.order.pk, user=self.alice)
        self.assertEqual(denied.status_code, 403)
        fresh = invoke_registration(registration, pk=self.order.pk, user=self.bob)
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual(json_body(fresh)["value"], "pending")

    def test_shared_cache_does_not_bypass_object_permissions(self):
        registration = self._register(
            "order-owned-shared",
            permission_classes=[IsOwner],
            cache_vary_on_user=False,
        )
        self.assertEqual(
            invoke_registration(registration, pk=self.order.pk, user=self.alice).status_code,
            200,
        )
        denied = invoke_registration(registration, pk=self.order.pk, user=self.bob)
        self.assertEqual(denied.status_code, 403)

    def test_anonymous_caller_is_rejected_before_the_cache(self):
        registration = self._register("order-owned-anon", permission_classes=[IsOwner])
        response = invoke_registration(registration, pk=self.order.pk)
        self.assertIn(response.status_code, (401, 403))

    def test_scope_is_rechecked_on_a_cache_hit(self):
        registration = self._register(
            "order-scoped-cache",
            scope=lambda queryset, request: queryset.filter(owner=request.user),
            permission_classes=[IsAuthenticated],
        )
        self.assertEqual(
            invoke_registration(registration, pk=self.order.pk, user=self.alice).status_code,
            200,
        )
        self.order.owner = self.bob
        self.order.save()
        self.assertEqual(
            invoke_registration(registration, pk=self.order.pk, user=self.alice).status_code,
            404,
        )

    def test_owner_still_receives_the_cached_value(self):
        registration = self._register("order-owned-stale", permission_classes=[IsOwner])
        first = invoke_registration(registration, pk=self.order.pk, user=self.alice)
        self.assertEqual(json_body(first)["value"], "pending")
        Order.objects.filter(pk=self.order.pk).update(status="shipped")
        second = invoke_registration(registration, pk=self.order.pk, user=self.alice)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(json_body(second)["value"], "pending")

    def test_since_is_part_of_the_cache_key(self):
        registration = registry.register(
            "notes-cached",
            "testapp.Notification",
            as_list=True,
            fields=["message"],
            since_field="created_at",
            cache_timeout=60,
            replace=True,
        )
        self.addCleanup(registry.unregister, "notes-cached")
        first = Notification.objects.create(message="first")
        cached = json_body(invoke_registration(registration))
        self.assertEqual(cached["count"], 1)

        Notification.objects.create(message="second")
        still_cached = json_body(invoke_registration(registration))
        self.assertEqual(still_cached["count"], 1)

        fresh = json_body(
            invoke_registration(registration, params={"since": first.created_at.isoformat()})
        )
        self.assertEqual(fresh["count"], 1)
        self.assertEqual(fresh["value"][0]["message"], "second")


class MixinPermissionCacheTests(PollTestCase):
    def test_viewset_object_permissions_apply_on_a_cache_hit(self):
        class OwnedOrders(PollingMixin, GenericViewSet):
            queryset = Order.objects.all()
            permission_classes = [IsOwner]
            polling = {"source": "status", "cache_timeout": 60, "min_poll_interval": 1}

        alice = User.objects.create_user("mixin-alice")
        bob = User.objects.create_user("mixin-bob")
        order = Order.objects.create(status="pending", owner=alice)
        view = OwnedOrders.as_view({"get": "poll"})

        def poll(user):
            request = APIRequestFactory().get("/")
            force_authenticate(request, user=user)
            return view(request, pk=order.pk)

        self.assertEqual(poll(alice).status_code, 200)
        order.owner = bob
        order.save()
        self.assertEqual(poll(alice).status_code, 403)
        self.assertEqual(poll(bob).status_code, 200)

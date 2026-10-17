"""Registry, declaration helpers, routing, and the list_polls command."""

from io import StringIO
from unittest.mock import patch

from django.apps import apps
from django.core.management import call_command
from django.urls import reverse
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import GenericViewSet, ViewSet

from drf_pollable import PollEndpoint, poll_urls, register_poll, registry
from drf_pollable.exceptions import PollableConfigurationError
from drf_pollable.registration import PollRegistry, _throttle_classes, build_poll_view
from drf_pollable.routers import PollRouter
from drf_pollable.throttling import (
    DEFAULT_POLL_THROTTLES,
    PollBurstThrottle,
    PollIntervalThrottle,
)

from .base import PollTestCase
from .testapp.models import Notification, Order


class RegistryBehaviorTests(PollTestCase):
    def test_private_registry_tracks_membership(self):
        local = PollRegistry()
        self.assertEqual(len(local), 0)
        self.assertEqual(list(local), [])
        local.register("alpha", Order, field="status", min_poll_interval=3)
        self.assertIn("alpha", local)
        self.assertEqual(local.names(), ["alpha"])
        self.assertEqual(len(local), 1)
        self.assertEqual(local.get("missing"), None)

    def test_unregister_is_idempotent(self):
        local = PollRegistry()
        local.register("alpha", Order, field="status")
        local.unregister("alpha")
        local.unregister("alpha")
        self.assertNotIn("alpha", local)

    def test_clear_drops_registrations_and_allows_settings_to_load_again(self):
        local = PollRegistry()
        local.register("alpha", Order, field="status")
        local._settings_loaded = True
        local.clear()
        self.assertEqual(len(local), 0)
        self.assertFalse(local._settings_loaded)

    def test_replace_overwrites_config_and_origin(self):
        local = PollRegistry()
        local.register("alpha", "testapp.Order.status", min_poll_interval=3, origin="first")
        local.register(
            "alpha",
            "testapp.Order.progress",
            min_poll_interval=8,
            replace=True,
            origin="second",
        )
        found = local.get("alpha")
        self.assertEqual(found.config["min_poll_interval"], 8)
        self.assertEqual(found.origin, "second")
        self.assertIn("progress", found.source.describe())

    def test_name_must_be_a_non_empty_string(self):
        local = PollRegistry()
        for name in ("", None, 12):
            with self.assertRaises(PollableConfigurationError):
                local.register(name, "testapp.Order.status")

    def test_duplicate_message_names_the_existing_source(self):
        local = PollRegistry()
        local.register("alpha", "testapp.Order.status")
        with self.assertRaises(PollableConfigurationError) as caught:
            local.register("alpha", "testapp.Order.progress")
        self.assertIn("testapp.Order.status", str(caught.exception))

    def test_view_class_is_built_once(self):
        registration = registry.get("order-status")
        self.assertIs(registration.view, registration.view)
        self.assertEqual(registration.view.__name__, "PollOrderStatusView")

    def test_url_path_for_object_and_collection_endpoints(self):
        self.assertEqual(registry.get("order-status").url_path, "order-status/<str:pk>/")
        self.assertEqual(registry.get("unread-count").url_path, "unread-count/")
        self.assertEqual(registry.get("order-status").url_name, "poll-order-status")

    def test_custom_lookup_kwarg_is_part_of_the_path(self):
        local = PollRegistry()
        registration = local.register(
            "by-order",
            Order,
            field="status",
            lookup_url_kwarg="order_id",
        )
        self.assertEqual(registration.url_path, "by-order/<str:order_id>/")

    def test_load_from_settings_is_idempotent(self):
        local = PollRegistry()
        local.load_from_settings()
        local.load_from_settings()
        self.assertEqual(len([item for item in local if item.name == "ticket-from-settings"]), 1)
        self.assertEqual(local.get("ticket-from-settings").origin, "settings")

    def test_load_from_models_picks_up_field_instance_and_list(self):
        local = PollRegistry()
        local.load_from_models()
        self.assertEqual(local.get("ticket-state").origin, "model:testapp.Ticket")
        self.assertIsNotNone(local.get("ticket-detail"))
        self.assertIsNotNone(local.get("ticket-list"))
        self.assertFalse(local.get("ticket-list").source.requires_object)

    def test_permission_and_throttle_options_are_applied_to_the_view(self):
        local = PollRegistry()
        registration = local.register(
            "guarded",
            "testapp.Order.status",
            permission_classes=[IsAuthenticated],
            authentication_classes=["rest_framework.authentication.SessionAuthentication"],
            throttle_scope="tight",
            throttle_classes=["drf_pollable.throttling.PollBurstThrottle"],
        )
        view = registration.view
        self.assertEqual(view.permission_classes, [IsAuthenticated])
        self.assertEqual(view.throttle_scope, "tight")
        self.assertIs(view.throttle_classes[0], PollIntervalThrottle)
        self.assertIn(PollBurstThrottle, view.throttle_classes)

    def test_a_single_permission_class_is_accepted(self):
        local = PollRegistry()
        registration = local.register(
            "one-perm",
            "testapp.Order.status",
            permission_classes=IsAuthenticated,
        )
        self.assertEqual(registration.view.permission_classes, [IsAuthenticated])

    def test_unknown_authentication_path_fails_when_the_view_is_built(self):
        local = PollRegistry()
        registration = local.register(
            "bad-auth",
            "testapp.Order.status",
            authentication_classes=["does.not.Exist"],
        )
        with self.assertRaises(ImportError):
            build_poll_view(registration)

    def test_app_ready_does_not_register_twice(self):
        before = len(registry)
        apps.get_app_config("drf_pollable").ready()
        self.assertEqual(len(registry), before)


class DeclarationHelperTests(PollTestCase):
    def test_decorator_reads_options_from_the_class(self):
        @register_poll("decorated-order")
        class Decorated:
            source = "testapp.Order.status"
            min_poll_interval = 3

        self.addCleanup(registry.unregister, "decorated-order")
        self.assertEqual(registry.get("decorated-order").config["min_poll_interval"], 3)
        self.assertIs(Decorated.source, "testapp.Order.status")

    def test_explicit_decorator_options_win(self):
        @register_poll("decorated-explicit", min_poll_interval=2)
        class Decorated:
            source = "testapp.Order.status"
            min_poll_interval = 50

        self.addCleanup(registry.unregister, "decorated-explicit")
        self.assertEqual(registry.get("decorated-explicit").config["min_poll_interval"], 2)

    def test_calling_with_a_source_returns_the_decorated_object_unchanged(self):
        marker = object()
        decorator = register_poll("immediate-order", source="testapp.Order.status")
        self.addCleanup(registry.unregister, "immediate-order")
        self.assertIs(decorator(marker), marker)
        self.assertIn("immediate-order", registry)

    def test_abstract_base_is_not_registered_and_its_options_are_inherited(self):
        class Base(PollEndpoint):
            abstract = True
            source = "testapp.Order.status"
            min_poll_interval = 15

        self.assertNotIn("Base", registry)

        class Child(Base):
            name = "inherited-order"

        self.addCleanup(registry.unregister, "inherited-order")
        self.assertEqual(registry.get("inherited-order").config["min_poll_interval"], 15)

        class StillAbstract(Base):
            abstract = True
            name = "not-registered"

        self.assertNotIn("not-registered", registry)

    def test_endpoint_without_a_name_is_rejected(self):
        class Base(PollEndpoint):
            abstract = True
            source = "testapp.Order.status"

        with self.assertRaises(PollableConfigurationError):

            class Incomplete(Base):
                pass

    def test_endpoint_without_a_source_is_rejected(self):
        with self.assertRaises(PollableConfigurationError):

            class NoSource(PollEndpoint):
                name = "no-source-endpoint"


class ThrottleClassAssemblyTests(PollTestCase):
    def test_default_list_is_used_when_unset(self):
        self.assertEqual(
            _throttle_classes({"throttle_classes": None}),
            list(DEFAULT_POLL_THROTTLES),
        )

    def test_interval_throttle_is_prepended_to_a_custom_list(self):
        classes = _throttle_classes({"throttle_classes": [PollBurstThrottle]})
        self.assertIs(classes[0], PollIntervalThrottle)
        self.assertIn(PollBurstThrottle, classes)

    def test_interval_throttle_is_not_duplicated(self):
        classes = _throttle_classes({"throttle_classes": [PollIntervalThrottle, PollBurstThrottle]})
        self.assertEqual(classes.count(PollIntervalThrottle), 1)

    def test_a_subclass_counts_as_the_interval_throttle(self):
        class CustomInterval(PollIntervalThrottle):
            pass

        classes = _throttle_classes({"throttle_classes": [CustomInterval]})
        self.assertEqual(classes, [CustomInterval])

    def test_a_single_class_is_wrapped_in_a_list(self):
        classes = _throttle_classes({"throttle_classes": PollBurstThrottle})
        self.assertEqual(classes[0], PollIntervalThrottle)
        self.assertIn(PollBurstThrottle, classes)


class MixinTests(PollTestCase):
    def test_mixin_does_not_enter_the_global_registry(self):
        from drf_pollable import PollingMixin

        before = set(registry.names())

        class TempOrders(PollingMixin, GenericViewSet):
            queryset = Order.objects.all()
            polling = {"source": "status"}

        TempOrders._get_poll_registration()
        self.assertEqual(set(registry.names()), before)

    def test_empty_polling_and_missing_source_are_rejected(self):
        from drf_pollable import PollingMixin

        class Empty(PollingMixin, ViewSet):
            polling = {}

        class Missing(PollingMixin, ViewSet):
            polling = {"min_poll_interval": 1}

        with self.assertRaises(PollableConfigurationError):
            Empty._get_poll_registration()
        with self.assertRaises(PollableConfigurationError):
            Missing._get_poll_registration()

    def test_bare_field_requires_a_queryset(self):
        from drf_pollable import PollingMixin

        class NoQueryset(PollingMixin, ViewSet):
            polling = {"source": "status"}

        with self.assertRaises(PollableConfigurationError):
            NoQueryset._get_poll_registration()

    def test_dotted_source_does_not_need_a_queryset(self):
        from drf_pollable import PollingMixin

        class Dotted(PollingMixin, ViewSet):
            polling = {"source": "testapp.Order.status", "min_poll_interval": 6}

        registration = Dotted._get_poll_registration()
        self.assertIn("status", registration.source.describe())
        self.assertEqual(registration.config["min_poll_interval"], 6)
        self.assertTrue(registration.origin.startswith("mixin:"))

    def test_subclass_does_not_reuse_the_parent_registration(self):
        from drf_pollable import PollingMixin

        class Parent(PollingMixin, GenericViewSet):
            queryset = Order.objects.all()
            polling = {"source": "status", "min_poll_interval": 4}

        class Child(Parent):
            polling = {"source": "status", "min_poll_interval": 9}

        self.assertEqual(Parent._get_poll_registration().config["min_poll_interval"], 4)
        self.assertEqual(Child._get_poll_registration().config["min_poll_interval"], 9)
        self.assertIsNot(Parent._get_poll_registration(), Child._get_poll_registration())

    def test_poll_name_and_lookup_kwarg(self):
        from drf_pollable import PollingMixin

        class ByOrder(PollingMixin, GenericViewSet):
            queryset = Order.objects.all()
            lookup_url_kwarg = "order_id"
            poll_name = "custom-order-poll"
            polling = {"source": "status"}

        registration = ByOrder._get_poll_registration()
        self.assertEqual(registration.name, "custom-order-poll")
        order = Order.objects.create(status="shipped")
        result = registration.source.get_result(_Request(), order_id=order.pk)
        self.assertEqual(result.value, "shipped")

    def test_poll_action_uses_the_viewset_lookup(self):
        from rest_framework.test import APIRequestFactory

        from drf_pollable import PollingMixin

        class ByOrder(PollingMixin, GenericViewSet):
            queryset = Order.objects.all()
            lookup_url_kwarg = "order_id"
            polling = {"source": "status", "min_poll_interval": 1}

        order = Order.objects.create(status="pending")
        view = ByOrder.as_view({"get": "poll"})
        response = view(APIRequestFactory().get("/"), order_id=order.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["value"], "pending")


class RouterTests(PollTestCase):
    def test_live_routes_reverse(self):
        order = Order.objects.create(status="pending")
        url = reverse("poll:poll-order-status", kwargs={"pk": order.pk})
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(reverse("poll:poll-unread-count"), "/poll/unread-count/")

    def test_prefix_override_and_empty_prefix(self):
        local = PollRegistry()
        local.register("alpha", Order, field="status", url_prefix="ignored")
        local.register(
            "beta",
            Notification,
            as_list=True,
            aggregate="count",
            url_prefix="ignored",
        )
        prefixed = [str(pattern.pattern) for pattern in PollRouter(local, prefix="custom")]
        self.assertIn("custom/alpha/<str:pk>/", prefixed)
        self.assertIn("custom/beta/", prefixed)

        bare = [str(pattern.pattern) for pattern in PollRouter(local, prefix="")]
        self.assertIn("alpha/<str:pk>/", bare)
        self.assertIn("beta/", bare)
        self.assertEqual(list(PollRouter(PollRegistry())), [])

    def test_each_endpoint_keeps_its_own_prefix_by_default(self):
        local = PollRegistry()
        local.register("alpha", Order, field="status", url_prefix="hooks")
        routes = [str(pattern.pattern) for pattern in PollRouter(local)]
        self.assertEqual(routes, ["hooks/alpha/<str:pk>/"])

    def test_poll_urls_is_includeable(self):
        urlconf, app_name, namespace = poll_urls(PollRegistry())
        self.assertEqual(list(urlconf), [])
        self.assertEqual(app_name, "drf_pollable")
        self.assertEqual(namespace, "poll")


class ListPollsEmptyTests(PollTestCase):
    def test_empty_registry_warns(self):
        out = StringIO()
        with patch("drf_pollable.management.commands.list_polls.registry", PollRegistry()):
            call_command("list_polls", stdout=out)
        self.assertIn("No polling endpoints", out.getvalue())


class _Request:
    query_params: dict = {}
    META: dict = {}
    user = None

"""All five declaration styles must produce equivalent endpoints."""

from io import StringIO

from django.core.management import call_command

from drf_pollable import registry

from .base import PollTestCase
from .testapp.models import Order, Ticket


class DeclarationStyleTests(PollTestCase):
    """Each style registers, routes, and behaves the same way."""

    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(status="pending")
        self.ticket = Ticket.objects.create(subject="Printer on fire", state="open")

    # -- style 1: function call (tests/testapp/polls.py) -------------------
    def test_function_style(self):
        response = self.poll(f"/poll/order-status/{self.order.pk}/")
        self.assertEqual(response.json()["value"], "pending")
        self.assertEqual(response["Retry-After"], "5")

    # -- style 2: class-based ---------------------------------------------
    def test_class_style(self):
        response = self.poll(f"/poll/order-class/{self.order.pk}/")
        self.assertEqual(response.json()["value"], "pending")
        self.assertEqual(response["Retry-After"], "8")

    # -- style 3: model PollingMeta ---------------------------------------
    def test_model_meta_field_style(self):
        response = self.poll(f"/poll/ticket-state/{self.ticket.pk}/")
        self.assertEqual(response.json()["value"], "open")
        self.assertEqual(response["Retry-After"], "9")

    def test_model_meta_instance_style(self):
        response = self.poll(f"/poll/ticket-detail/{self.ticket.pk}/")
        self.assertEqual(
            response.json()["value"], {"subject": "Printer on fire", "state": "open"}
        )
        self.assertEqual(response["Retry-After"], "4")

    def test_model_meta_done_when(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(state="closed")
        self.assertTrue(self.poll(f"/poll/ticket-state/{self.ticket.pk}/").json()["done"])

    def test_model_meta_list_style(self):
        # limit=1 and order_by=-id keep only the newest row, with no object id in the URL.
        Ticket.objects.create(subject="newer", state="open")
        body = self.poll("/poll/ticket-list/").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["value"], [{"subject": "newer", "state": "open"}])
        self.assertEqual(self.poll("/poll/ticket-list/")["Retry-After"], "3")

    # -- style 4: settings ENDPOINTS --------------------------------------
    def test_settings_style(self):
        response = self.poll(f"/poll/ticket-from-settings/{self.ticket.pk}/")
        self.assertEqual(response.json()["value"], "open")
        self.assertEqual(response["Retry-After"], "11")

    # -- style 5: PollingMixin on a ViewSet --------------------------------
    def test_mixin_style(self):
        response = self.poll(f"/api/orders/{self.order.pk}/poll/")
        self.assertEqual(response.json()["value"], "pending")
        self.assertEqual(response["Retry-After"], "6")

    def test_mixin_supports_conditional_requests_too(self):
        url = f"/api/orders/{self.order.pk}/poll/"
        etag = self.poll(url)["ETag"]
        self.assertEqual(self.poll(url, etag=etag).status_code, 304)

    def test_mixin_does_not_disturb_the_viewsets_own_routes(self):
        self.assertEqual(self.client.get(f"/api/orders/{self.order.pk}/").status_code, 200)

    # -- parity ------------------------------------------------------------
    def test_all_styles_agree_on_the_same_field(self):
        Order.objects.filter(pk=self.order.pk).update(status="shipped")
        urls = [
            f"/poll/order-status/{self.order.pk}/",
            f"/poll/order-class/{self.order.pk}/",
            f"/api/orders/{self.order.pk}/poll/",
        ]
        values = {self.poll(url).json()["value"] for url in urls}
        self.assertEqual(values, {"shipped"})

    def test_global_default_applies_when_an_endpoint_does_not_override(self):
        # tests/settings.py sets DEFAULTS.min_poll_interval = 7.
        self.assertEqual(self.poll(f"/poll/order-bare/{self.order.pk}/")["Retry-After"], "7")


class RegistryTests(PollTestCase):
    def test_every_style_is_present_in_the_registry(self):
        for name in [
            "order-status",       # function
            "order-class",        # class
            "ticket-state",       # model PollingMeta (field)
            "ticket-detail",      # model PollingMeta (instance)
            "ticket-list",        # model PollingMeta (list)
            "ticket-from-settings",  # settings
        ]:
            self.assertIn(name, registry, f"{name} was not registered")

    def test_origins_are_tracked(self):
        self.assertEqual(registry.get("ticket-from-settings").origin, "settings")
        self.assertTrue(registry.get("ticket-state").origin.startswith("model:"))

    def test_duplicate_registration_is_rejected(self):
        from drf_pollable.exceptions import PollableConfigurationError

        with self.assertRaises(PollableConfigurationError):
            registry.register("order-status", "testapp.Order.status")

    def test_registration_requires_a_source(self):
        from drf_pollable.exceptions import PollableConfigurationError

        with self.assertRaises(PollableConfigurationError):
            registry.register("no-source")


class ManagementCommandTests(PollTestCase):
    def test_list_polls_reports_endpoints(self):
        out = StringIO()
        call_command("list_polls", stdout=out)
        output = out.getvalue()
        self.assertIn("order-status", output)
        self.assertIn("min_poll_interval", output)
        self.assertIn("/poll/order-status/", output)

    def test_list_polls_can_filter_by_name(self):
        out = StringIO()
        call_command("list_polls", name="order-status", stdout=out)
        output = out.getvalue()
        self.assertIn("order-status", output)
        self.assertNotIn("ticket-detail", output)
        self.assertNotIn("authentication_classes", output)

    def test_list_polls_verbose_includes_the_full_config(self):
        out = StringIO()
        call_command("list_polls", name="order-status", verbose=True, stdout=out)
        self.assertIn("authentication_classes", out.getvalue())

    def test_list_polls_unknown_name_warns(self):
        out = StringIO()
        call_command("list_polls", name="does-not-exist", stdout=out)
        self.assertIn("No polling endpoints", out.getvalue())

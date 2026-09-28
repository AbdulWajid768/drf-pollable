"""The four source types beyond a plain field."""

from drf_pollable.exceptions import PollableConfigurationError
from drf_pollable.sources import build_source

from .base import PollTestCase
from .testapp.models import Notification, Order


class QuerySetSourceTests(PollTestCase):
    def test_aggregate_count(self):
        Notification.objects.create(message="a", read=False)
        Notification.objects.create(message="b", read=False)
        Notification.objects.create(message="c", read=True)
        body = self.poll("/poll/unread-count/").json()
        self.assertEqual(body["value"], 2)

    def test_aggregate_endpoint_needs_no_object_id(self):
        self.assertEqual(self.poll("/poll/unread-count/").status_code, 200)

    def test_list_source_returns_allow_listed_fields(self):
        Notification.objects.create(message="hello")
        body = self.poll("/poll/notification-feed/").json()
        self.assertEqual(len(body["value"]), 1)
        self.assertEqual(set(body["value"][0]), {"id", "message"})
        self.assertEqual(body["count"], 1)

    def test_list_source_exposes_a_cursor(self):
        Notification.objects.create(message="hello")
        self.assertIn("cursor", self.poll("/poll/notification-feed/").json())

    def test_since_cursor_returns_only_newer_rows(self):
        Notification.objects.create(message="first")
        cursor = self.poll("/poll/notification-feed/").json()["cursor"]

        # Nothing new yet.
        self.assertEqual(self.poll("/poll/notification-feed/", since=cursor).json()["count"], 0)

        Notification.objects.create(message="second")
        body = self.poll("/poll/notification-feed/", since=cursor).json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["value"][0]["message"], "second")


class CallableSourceTests(PollTestCase):
    def test_poll_result_fields_are_surfaced(self):
        body = self.poll("/poll/pipeline/").json()
        self.assertEqual(body["value"], "running")
        self.assertEqual(body["progress"], 42.0)
        self.assertEqual(body["eta_seconds"], 8.0)

    def test_plain_return_value_is_wrapped(self):
        body = self.poll("/poll/pipeline-done/").json()
        self.assertEqual(body["value"], "finished")
        self.assertTrue(body["done"])

    def test_callable_endpoint_still_advertises_an_interval(self):
        self.assertEqual(self.poll("/poll/pipeline/")["Retry-After"], "2")


class SourceBuildingTests(PollTestCase):
    """build_source() dispatch and its guard rails."""

    def test_three_part_string_builds_a_field_source(self):
        source = build_source("testapp.Order.status")
        self.assertEqual(source.describe(), "field testapp.Order.status")
        self.assertTrue(source.requires_object)

    def test_two_part_string_builds_an_instance_source(self):
        source = build_source("testapp.Order", fields=["status"])
        self.assertEqual(source.describe(), "instance testapp.Order")

    def test_as_list_builds_a_queryset_source(self):
        source = build_source("testapp.Notification", as_list=True, aggregate="count")
        self.assertFalse(source.requires_object)

    def test_model_class_is_accepted(self):
        self.assertTrue(build_source(Order, field="status").requires_object)

    def test_queryset_is_accepted(self):
        source = build_source(Notification.objects.all(), fields=["id"])
        self.assertFalse(source.requires_object)

    def test_callable_is_accepted(self):
        source = build_source(lambda request, **kwargs: "x")
        self.assertTrue(source.describe().startswith("callable"))

    def test_instance_source_requires_an_allow_list(self):
        # Refusing to dump every column is a deliberate safety default.
        with self.assertRaises(PollableConfigurationError):
            build_source("testapp.Order")

    def test_unparseable_source_is_rejected(self):
        with self.assertRaises(PollableConfigurationError):
            build_source("nonsense")

    def test_unsupported_source_type_is_rejected(self):
        with self.assertRaises(PollableConfigurationError):
            build_source(12345)

    def test_unknown_model_is_reported_clearly(self):
        source = build_source("testapp.DoesNotExist.field")
        with self.assertRaises(PollableConfigurationError):
            _ = source.model

    def test_unknown_field_is_reported_clearly(self):
        order = Order.objects.create(status="pending")
        source = build_source("testapp.Order.no_such_field")
        with self.assertRaises(PollableConfigurationError):
            source.get_result(_FakeRequest(), pk=order.pk)


class _FakeRequest:
    query_params: dict = {}
    META: dict = {}
    user = None

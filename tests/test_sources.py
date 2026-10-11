"""The four source types beyond a plain field."""

from django.http import Http404
from rest_framework import serializers

from drf_pollable.exceptions import PollableConfigurationError
from drf_pollable.sources import (
    CallableSource,
    FieldSource,
    PollResult,
    PollSource,
    build_source,
    normalize_value,
)

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

    def test_empty_and_single_segment_strings_are_rejected(self):
        for spec in ("", "Order", "app.Model.field.extra"):
            with self.assertRaises(PollableConfigurationError):
                build_source(spec)

    def test_an_existing_source_is_returned_unchanged(self):
        source = build_source("testapp.Order.status")
        self.assertIs(build_source(source), source)

    def test_explicit_field_on_a_model_string_builds_a_field_source(self):
        source = build_source("testapp.Order", field="status")
        self.assertIsInstance(source, FieldSource)
        self.assertEqual(source.field, "status")

    def test_callable_source_ignores_model_only_options(self):
        # Registrations pass one flat bag; model options must not TypeError a function.
        source = build_source(
            lambda request, **kwargs: "ok",
            scope=lambda queryset, request: queryset,
            version_field="updated_at",
            fields=["status"],
        )
        self.assertIsInstance(source, CallableSource)

    def test_queryset_source_requires_an_allow_list(self):
        with self.assertRaises(PollableConfigurationError):
            build_source("testapp.Notification", as_list=True)

    def test_non_callable_is_rejected_by_callable_source(self):
        with self.assertRaises(PollableConfigurationError):
            CallableSource("not-a-function")


class NormalizeValueTests(PollTestCase):
    def test_model_instance_collapses_to_its_primary_key(self):
        order = Order.objects.create(status="pending")
        self.assertEqual(normalize_value(order), order.pk)

    def test_queryset_and_manager_collapse_to_primary_keys(self):
        first = Order.objects.create(status="pending")
        second = Order.objects.create(status="shipped")
        self.assertEqual(normalize_value(Order.objects.filter(pk=first.pk)), [first.pk])
        self.assertCountEqual(normalize_value(Order.objects), [first.pk, second.pk])

    def test_plain_values_pass_through(self):
        self.assertEqual(normalize_value("pending"), "pending")
        self.assertIsNone(normalize_value(None))


class FieldSourceBehaviorTests(PollTestCase):
    def test_property_is_allowed_when_it_is_not_a_concrete_field(self):
        Order.status_code = property(lambda self: self.status.upper())
        self.addCleanup(delattr, Order, "status_code")
        order = Order.objects.create(status="pending")
        source = build_source(Order, field="status_code")
        self.assertEqual(source.get_result(_FakeRequest(), pk=order.pk).value, "PENDING")

    def test_include_display_can_be_turned_off(self):
        order = Order.objects.create(status="pending")
        source = build_source(Order, field="status", include_display=False)
        self.assertNotIn("display", source.get_result(_FakeRequest(), pk=order.pk).extra)

    def test_non_choice_field_has_no_display_label(self):
        order = Order.objects.create(status="pending", progress=3)
        source = build_source(Order, field="progress")
        result = source.get_result(_FakeRequest(), pk=order.pk)
        self.assertEqual(result.value, 3)
        self.assertNotIn("display", result.extra)

    def test_progress_and_eta_are_read_from_sibling_fields(self):
        order = Order.objects.create(status="pending", progress=40, eta_seconds=12)
        source = build_source(
            Order, field="status", progress_field="progress", eta_field="eta_seconds"
        )
        result = source.get_result(_FakeRequest(), pk=order.pk)
        self.assertEqual(result.progress, 40.0)
        self.assertEqual(result.eta_seconds, 12.0)

    def test_non_numeric_progress_and_eta_are_omitted(self):
        order = Order.objects.create(status="pending")
        source = build_source(Order, field="status", progress_field="status", eta_field="status")
        result = source.get_result(_FakeRequest(), pk=order.pk)
        self.assertIsNone(result.progress)
        self.assertIsNone(result.eta_seconds)

    def test_missing_lookup_kwarg_is_a_configuration_error(self):
        source = build_source(Order, field="status")
        with self.assertRaises(PollableConfigurationError):
            source.get_result(_FakeRequest())

    def test_callable_queryset_filters_the_lookup(self):
        pending = Order.objects.create(status="pending")
        shipped = Order.objects.create(status="shipped")

        def only_pending(request):
            return Order.objects.filter(status="pending")

        source = build_source(Order, field="status", queryset=only_pending)
        self.assertEqual(source.get_result(_FakeRequest(), pk=pending.pk).value, "pending")
        with self.assertRaises(Http404):
            source.get_result(_FakeRequest(), pk=shipped.pk)

    def test_select_related_is_applied(self):
        order = Order.objects.create(status="pending")
        source = build_source(Order, field="status", select_related=["owner"])
        self.assertEqual(source.get_result(_FakeRequest(), pk=order.pk).value, "pending")

    def test_version_field_auto_detects_updated_at(self):
        source = build_source(Order, field="status")
        self.assertEqual(source.version_field, "updated_at")

    def test_explicit_version_field_wins_over_auto_detection(self):
        source = build_source(Order, field="status", version_field="status")
        self.assertEqual(source.version_field, "status")

    def test_non_datetime_version_field_does_not_invent_a_timestamp(self):
        order = Order.objects.create(status="pending")
        source = build_source(Order, field="status", version_field="status")
        self.assertIsNone(source.get_result(_FakeRequest(), pk=order.pk).last_modified)

    def test_model_without_a_known_timestamp_has_no_version_field(self):
        source = build_source(Notification, as_list=True, aggregate="count")
        self.assertIsNone(source.version_field)

    def test_done_when_callable_sees_the_raw_field_value(self):
        order = Order.objects.create(status="pending")
        source = build_source(Order, field="status", done_when=lambda value: value == "pending")
        self.assertTrue(source.get_result(_FakeRequest(), pk=order.pk).done)

    def test_custom_lookup_kwarg_and_field(self):
        order = Order.objects.create(status="shipped")
        source = build_source(
            Order, field="status", lookup_field="pk", lookup_url_kwarg="order_id"
        )
        result = source.get_result(_FakeRequest(), order_id=order.pk)
        self.assertEqual(result.value, "shipped")


class InstanceAndQuerySetBehaviorTests(PollTestCase):
    def test_foreign_key_collapses_to_a_primary_key(self):
        from django.contrib.auth.models import User

        owner = User.objects.create_user("owner")
        order = Order.objects.create(status="pending", owner=owner)
        source = build_source(Order, fields=["owner", "status"])
        value = source.get_result(_FakeRequest(), pk=order.pk).value
        self.assertEqual(value["owner"], owner.pk)
        self.assertEqual(value["status"], "pending")

    def test_serializer_is_used_for_an_instance(self):
        order = Order.objects.create(status="shipped", progress=2)

        class OrderStatusSerializer(serializers.ModelSerializer):
            class Meta:
                model = Order
                fields = ["status"]

        source = build_source(Order, serializer=OrderStatusSerializer)
        value = source.get_result(_FakeRequest(), pk=order.pk).value
        self.assertEqual(value["status"], "shipped")
        self.assertNotIn("progress", value)

    def test_done_when_on_an_instance_receives_the_object(self):
        order = Order.objects.create(status="delivered")
        source = build_source(
            Order,
            fields=["status"],
            done_when=lambda obj: obj.status == "delivered",
        )
        self.assertTrue(source.get_result(_FakeRequest(), pk=order.pk).done)

    def test_filtered_queryset_spec_keeps_its_filters(self):
        Order.objects.create(status="pending")
        shipped = Order.objects.create(status="shipped")
        source = build_source(Order.objects.filter(status="shipped"), fields=["status"])
        rows = source.get_result(_FakeRequest()).value
        self.assertEqual(rows, [{"status": "shipped"}])
        self.assertEqual(len(Order.objects.all()), 2)
        self.assertEqual(rows[0]["status"], shipped.status)

    def test_exists_aggregate(self):
        source = build_source(Notification, as_list=True, aggregate="exists")
        self.assertIs(source.get_result(_FakeRequest()).value, False)
        Notification.objects.create(message="hi")
        self.assertIs(source.get_result(_FakeRequest()).value, True)

    def test_callable_aggregate(self):
        Notification.objects.create(message="a")
        Notification.objects.create(message="b")
        source = build_source(
            Notification, as_list=True, aggregate=lambda queryset: queryset.count() * 10
        )
        self.assertEqual(source.get_result(_FakeRequest()).value, 20)

    def test_unknown_aggregate_is_rejected(self):
        source = build_source(Notification, as_list=True, aggregate="sum")
        with self.assertRaises(PollableConfigurationError):
            source.get_result(_FakeRequest())

    def test_aggregate_done_when_sees_the_scalar(self):
        source = build_source(Notification, as_list=True, aggregate="count", done_when=0)
        self.assertTrue(source.get_result(_FakeRequest()).done)
        Notification.objects.create(message="a")
        self.assertFalse(source.get_result(_FakeRequest()).done)

    def test_since_filters_an_aggregate(self):
        first = Notification.objects.create(message="first")
        source = build_source(
            Notification, as_list=True, aggregate="count", since_field="created_at"
        )
        cursor = first.created_at.isoformat()
        self.assertEqual(
            source.get_result(_FakeRequest({"since": cursor})).value,
            0,
        )
        Notification.objects.create(message="second")
        self.assertEqual(source.get_result(_FakeRequest({"since": cursor})).value, 1)

    def test_invalid_since_does_not_filter(self):
        Notification.objects.create(message="first")
        source = build_source(
            Notification,
            as_list=True,
            fields=["message"],
            since_field="created_at",
        )
        result = source.get_result(_FakeRequest({"since": "yesterday"}))
        self.assertEqual(result.extra["count"], 1)

    def test_limit_and_order(self):
        Notification.objects.create(message="first")
        Notification.objects.create(message="second")
        Notification.objects.create(message="third")
        source = build_source(
            Notification,
            as_list=True,
            fields=["message"],
            order_by=["-id"],
            limit=2,
        )
        messages = [row["message"] for row in source.get_result(_FakeRequest()).value]
        self.assertEqual(messages, ["third", "second"])

    def test_serializer_is_used_for_a_list(self):
        Notification.objects.create(message="hello")

        class NoteSerializer(serializers.ModelSerializer):
            class Meta:
                model = Notification
                fields = ["message"]

        source = build_source(Notification, as_list=True, serializer=NoteSerializer)
        row = source.get_result(_FakeRequest()).value[0]
        self.assertEqual(set(row), {"message"})

    def test_cursor_is_omitted_when_the_since_field_is_not_a_timestamp(self):
        Notification.objects.create(message="hello")
        source = build_source(
            Notification, as_list=True, fields=["message"], since_field="message"
        )
        self.assertNotIn("cursor", source.get_result(_FakeRequest()).extra)

    def test_scope_is_applied_to_the_queryset(self):
        Notification.objects.create(message="keep", read=False)
        Notification.objects.create(message="drop", read=True)
        source = build_source(
            Notification,
            as_list=True,
            fields=["message"],
            scope=lambda queryset, request: queryset.filter(read=False),
        )
        rows = source.get_result(_FakeRequest()).value
        self.assertEqual(rows, [{"message": "keep"}])


class CallableSourceBehaviorTests(PollTestCase):
    def test_requires_object_exposes_a_lookup_and_passes_kwargs(self):
        def status(request, **kwargs):
            return kwargs["item_id"]

        source = build_source(
            status, requires_object=True, lookup_url_kwarg="item_id", done_when=5
        )
        self.assertTrue(source.requires_object)
        self.assertEqual(source.lookup_url_kwarg, "item_id")
        result = source.get_result(_FakeRequest(), item_id=5)
        self.assertEqual(result.value, 5)
        self.assertTrue(result.done)

    def test_poll_result_done_flag_is_kept(self):
        def status(request, **kwargs):
            return PollResult(value="running", done=True)

        source = build_source(status, done_when="finished")
        self.assertTrue(source.get_result(_FakeRequest()).done)

    def test_poll_result_done_flag_can_be_filled_in(self):
        def status(request, **kwargs):
            return PollResult(value="finished", done=False)

        source = build_source(status, done_when="finished")
        self.assertTrue(source.get_result(_FakeRequest()).done)

    def test_done_when_sees_the_raw_return_value(self):
        order = Order.objects.create(status="pending")

        def status(request, **kwargs):
            return order

        source = build_source(status, done_when=lambda value: value.pk == order.pk)
        result = source.get_result(_FakeRequest())
        self.assertEqual(result.value, order.pk)
        self.assertTrue(result.done)

    def test_describe_uses_the_function_name(self):
        def pipeline(request, **kwargs):
            return "ok"

        self.assertIn("pipeline", build_source(pipeline).describe())


class PollSourceBaseTests(PollTestCase):
    def test_base_get_result_is_abstract(self):
        with self.assertRaises(NotImplementedError):
            PollSource().get_result(_FakeRequest())

    def test_base_describe_is_the_class_name(self):
        self.assertEqual(PollSource().describe(), "PollSource")


class _FakeRequest:
    def __init__(self, params=None, meta=None, user=None):
        self.query_params = params or {}
        self.META = meta or {}
        self.user = user

"""A hand-written ViewSet using declaration style 5 (PollingMixin)."""

from rest_framework import serializers, viewsets

from drf_pollable import PollingMixin

from .models import Order


class OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = ["id", "status", "progress"]


class OrderViewSet(PollingMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Order.objects.all()
    serializer_class = OrderSerializer

    polling = {
        "source": "status",
        "min_poll_interval": 6,
        "done_when": {"delivered"},
    }

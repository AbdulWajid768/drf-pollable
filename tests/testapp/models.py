"""Models used by the test suite."""

from django.conf import settings
from django.db import models


class Order(models.Model):
    """Exercises the field, instance, and mixin styles."""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("shipped", "Shipped"),
        ("delivered", "Delivered"),
        ("cancelled", "Cancelled"),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    progress = models.IntegerField(default=0)
    eta_seconds = models.IntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class Ticket(models.Model):
    """Exercises declaration style 3: PollingMeta on the model itself."""

    subject = models.CharField(max_length=100, default="")
    state = models.CharField(max_length=20, default="open")
    updated_at = models.DateTimeField(auto_now=True)

    class PollingMeta:
        fields = {
            "state": {
                "min_poll_interval": 9,
                "done_when": "closed",
            }
        }
        instance = {
            "name": "ticket-detail",
            "fields": ["subject", "state"],
            "min_poll_interval": 4,
        }


class Notification(models.Model):
    """Exercises queryset / aggregate / delta sources."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE
    )
    message = models.CharField(max_length=200, default="")
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

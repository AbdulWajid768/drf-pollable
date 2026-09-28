"""Create a little data to poll against."""

from django.core.management.base import BaseCommand

from shop.models import Notification, Order


class Command(BaseCommand):
    help = "Seed the example project with an order and some notifications."

    def handle(self, *args, **options):
        order, _ = Order.objects.get_or_create(
            reference="ORD-1", defaults={"status": "pending", "progress": 10}
        )
        Notification.objects.get_or_create(message="Order received", defaults={"read": False})
        Notification.objects.get_or_create(message="Payment captured", defaults={"read": False})

        self.stdout.write(self.style.SUCCESS(f"Seeded order {order.pk} ({order.reference})."))
        self.stdout.write("Try:")
        self.stdout.write(f"  curl -i http://localhost:8000/api/poll/order-status/{order.pk}/")
        self.stdout.write("  curl -i http://localhost:8000/api/poll/unread-count/")

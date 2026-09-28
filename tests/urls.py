"""URLconf for the test suite."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from drf_pollable import poll_urls

from .testapp.views import OrderViewSet

router = DefaultRouter()
router.register("orders", OrderViewSet, basename="order")

urlpatterns = [
    path("", poll_urls()),
    path("api/", include(router.urls)),
]

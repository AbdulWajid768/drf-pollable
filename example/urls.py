from django.urls import path

from drf_pollable import poll_urls

urlpatterns = [
    path("api/", poll_urls()),
]

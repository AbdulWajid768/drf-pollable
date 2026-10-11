"""Shared test base."""

import json

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate


class PollTestCase(TestCase):
    """Clears polling state between tests.

    Backoff streaks and last-poll timestamps live in the cache; leftovers from a
    previous test would change the advertised interval or trip enforcement.
    """

    def setUp(self):
        super().setUp()
        cache.clear()

    def poll(self, url, etag=None, **params):
        headers = {"HTTP_IF_NONE_MATCH": etag} if etag else {}
        return self.client.get(url, params, **headers)


def invoke_registration(registration, *, pk=None, user=None, params=None, **extra):
    """Call a registration's view without going through the URLconf.

    Endpoints registered during a test are not in the URLconf snapshot taken
    at startup, so they have to be invoked directly.
    """
    request = APIRequestFactory().get("/", params or {}, **extra)
    if user is not None:
        force_authenticate(request, user=user)
    kwargs = {}
    if pk is not None:
        lookup = getattr(registration.source, "lookup_url_kwarg", "pk")
        kwargs[lookup] = pk
    return registration.view.as_view()(request, **kwargs)


def json_body(response):
    """Read a DRF response body whether or not it has been rendered yet."""
    if hasattr(response, "data"):
        return response.data
    return json.loads(response.content.decode())

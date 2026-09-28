"""Shared test base."""

from django.core.cache import cache
from django.test import TestCase


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

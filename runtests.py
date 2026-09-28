#!/usr/bin/env python
"""Run the drf-pollable test suite.

Uses Django's own test runner so the suite needs nothing beyond Django and
DRF::

    python runtests.py                 # everything
    python runtests.py tests.test_field_source
"""

import sys

import django
from django.conf import settings
from django.test.utils import get_runner


def main(argv):
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
    django.setup()

    runner_class = get_runner(settings)
    runner = runner_class(verbosity=2, interactive=False)
    labels = argv[1:] or ["tests"]
    failures = runner.run_tests(labels)
    sys.exit(bool(failures))


if __name__ == "__main__":
    main(sys.argv)

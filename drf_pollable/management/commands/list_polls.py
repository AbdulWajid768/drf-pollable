"""``python manage.py list_polls`` -- show every registered polling endpoint.

Answers "what is pollable, at what URL, and with which limits?" without
grepping the codebase -- useful because endpoints can be declared five
different ways.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from ...registration import registry

#: Options worth showing by default. ``--verbose`` prints the full config.
HIGHLIGHT_KEYS = (
    "min_poll_interval",
    "suggested_poll_interval",
    "max_poll_interval",
    "enforce_interval",
    "backoff",
    "jitter",
    "throttle_scope",
    "cache_timeout",
    "enabled",
)


class Command(BaseCommand):
    help = "List every registered drf-pollable endpoint and its effective configuration."

    def add_arguments(self, parser):
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show every configuration option, not just the notable ones.",
        )
        parser.add_argument(
            "--name",
            help="Show only the endpoint with this name.",
        )

    def handle(self, *args, **options):
        registrations = sorted(registry, key=lambda item: item.name)
        if options.get("name"):
            registrations = [r for r in registrations if r.name == options["name"]]

        if not registrations:
            self.stdout.write(self.style.WARNING("No polling endpoints are registered."))
            return

        for registration in registrations:
            self._print(registration, verbose=options.get("verbose", False))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"{len(registrations)} polling endpoint(s)."))

    def _print(self, registration, *, verbose: bool):
        config = registration.config
        prefix = (config["url_prefix"] or "").strip("/")
        url = f"/{prefix}/{registration.url_path}" if prefix else f"/{registration.url_path}"

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(registration.name))
        self.stdout.write(f"  source   {registration.source.describe()}")
        # Relative to wherever poll_urls() is mounted in the root URLconf.
        self.stdout.write(f"  path     {url}")
        self.stdout.write(f"  declared {registration.origin}")

        keys = sorted(config) if verbose else HIGHLIGHT_KEYS
        shown = [(key, config[key]) for key in keys if key in config]
        width = max((len(key) for key, _ in shown), default=0)
        for key, value in shown:
            self.stdout.write(f"  {key.ljust(width)}  {value!r}")

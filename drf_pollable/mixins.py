"""Declaration style 5: add polling to a ViewSet you already own.

For developers who would rather keep their own view than have one generated::

    class OrderViewSet(PollingMixin, viewsets.ModelViewSet):
        queryset = Order.objects.all()
        serializer_class = OrderSerializer
        polling = {
            "source": "status",          # a field on this viewset's model
            "min_poll_interval": 5,
            "done_when": {"delivered", "cancelled"},
        }

This adds ``GET /orders/{pk}/poll/`` with exactly the behaviour a generated
endpoint would have -- same headers, same 304 handshake, same interval logic.
"""

from __future__ import annotations

from typing import Any

from rest_framework.decorators import action

from .conf import resolve_config
from .exceptions import PollableConfigurationError
from .registration import PollRegistration
from .sources import build_source
from .views import PollResponseMixin


class PollingMixin(PollResponseMixin):
    """Adds a ``poll`` detail route to a ViewSet.

    ``polling`` accepts the same options as ``register_poll``. ``source`` may be
    a bare field name, in which case it is resolved against the ViewSet's own
    queryset model.
    """

    polling: dict[str, Any] = {}

    #: Cached per-class so the source is built once, not per request.
    _poll_registration: PollRegistration | None = None

    @action(detail=True, methods=["get"], url_path="poll")
    def poll(self, request, *args, **kwargs):
        registration = self._get_poll_registration()
        self.registration = registration
        self.poll_config = registration.config
        return self.poll_response(request, **kwargs)

    # -- internals --------------------------------------------------------
    @classmethod
    def _get_poll_registration(cls) -> PollRegistration:
        # Check this class's own __dict__: a subclass must not inherit its
        # parent's cached registration.
        cached = cls.__dict__.get("_poll_registration")
        if cached is not None:
            return cached

        options = dict(cls.polling or {})
        if not options:
            raise PollableConfigurationError(
                f"{cls.__name__} uses PollingMixin but defines no `polling = {{...}}`."
            )

        source_spec = options.pop("source", None)
        if source_spec is None:
            raise PollableConfigurationError(f"{cls.__name__}.polling needs a 'source'.")

        # A bare field name is resolved against the ViewSet's model.
        if isinstance(source_spec, str) and "." not in source_spec:
            model = cls._poll_model()
            options.setdefault("field", source_spec)
            source_spec = model

        # The ViewSet already routes /{pk}/poll/, so align the lookup with it.
        # Do this before resolving config so both the source and the resolved
        # config agree on which URL kwarg carries the object id.
        lookup = getattr(cls, "lookup_url_kwarg", None) or getattr(cls, "lookup_field", "pk")
        options.setdefault("lookup_url_kwarg", lookup)
        options.setdefault("lookup_field", getattr(cls, "lookup_field", "pk"))

        config = resolve_config(options)
        registration = PollRegistration(
            name=getattr(cls, "poll_name", None) or cls.__name__.lower(),
            source=build_source(source_spec, **options),
            config=config,
            origin=f"mixin:{cls.__name__}",
        )
        cls._poll_registration = registration
        return registration

    @classmethod
    def _poll_model(cls):
        queryset = getattr(cls, "queryset", None)
        if queryset is not None:
            return queryset.model
        raise PollableConfigurationError(
            f"{cls.__name__} needs a `queryset` (or a fully-qualified "
            "'app.Model.field' source) so PollingMixin can find the model."
        )

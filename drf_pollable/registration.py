"""The registry every declaration style funnels into.

Five ways to declare the same endpoint, one representation underneath:

1. ``register_poll("order-status", source="shop.Order.status")``     -- function/decorator
2. ``class OrderStatus(PollEndpoint): ...``                          -- class-based
3. ``class PollingMeta:`` inside a model                             -- model-driven
4. ``DRF_POLLABLE["ENDPOINTS"]``                                     -- central settings
5. ``PollingMixin`` on an existing ViewSet (see ``mixins.py``)       -- bring your own view
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from django.utils.module_loading import import_string

from .conf import resolve_config, settings_endpoints
from .exceptions import PollableConfigurationError
from .sources import PollSource, build_source
from .throttling import DEFAULT_POLL_THROTTLES, PollIntervalThrottle
from .views import PollView


class PollRegistration:
    """One registered polling endpoint."""

    def __init__(self, name: str, source: PollSource, config: dict[str, Any], origin: str = "code"):
        self.name = name
        self.source = source
        self.config = config
        #: Where this registration came from, for the introspection command.
        self.origin = origin
        self._view = None

    @property
    def view(self):
        """The generated DRF view class (built once, lazily)."""
        if self._view is None:
            self._view = build_poll_view(self)
        return self._view

    @property
    def url_path(self) -> str:
        """URL path for this endpoint, relative to the router prefix."""
        if not self.source.requires_object:
            return f"{self.name}/"
        kwarg = getattr(self.source, "lookup_url_kwarg", "pk")
        return f"{self.name}/<str:{kwarg}>/"

    @property
    def url_name(self) -> str:
        return f"poll-{self.name}"

    def describe(self) -> str:  # pragma: no cover - introspection helper
        return self.source.describe()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PollRegistration {self.name!r} -> {self.source.describe()}>"


def build_poll_view(registration: PollRegistration) -> type[PollView]:
    """Generate the DRF view class for ``registration``."""
    config = registration.config

    attrs: dict[str, Any] = {
        "registration": registration,
        "poll_config": config,
        "throttle_classes": _throttle_classes(config),
        "__doc__": f"Polling endpoint for {registration.source.describe()}.",
    }

    if config["throttle_scope"]:
        attrs["throttle_scope"] = config["throttle_scope"]
    if config["permission_classes"] is not None:
        attrs["permission_classes"] = _load_all(config["permission_classes"])
    if config["authentication_classes"] is not None:
        attrs["authentication_classes"] = _load_all(config["authentication_classes"])

    return type(f"Poll{_camel(registration.name)}View", (PollView,), attrs)


def _throttle_classes(config) -> list:
    configured = config["throttle_classes"]
    if configured is None:
        return list(DEFAULT_POLL_THROTTLES)
    classes = _load_all(configured)
    # The per-object cadence guard is how `enforce_interval` is implemented, so
    # keep it even when a project supplies its own throttle list.
    if not any(issubclass(klass, PollIntervalThrottle) for klass in classes):
        classes = [PollIntervalThrottle, *classes]
    return classes


class PollRegistry:
    """Holds every registered endpoint. One module-level instance is used."""

    def __init__(self):
        self._registrations: dict[str, PollRegistration] = {}
        self._settings_loaded = False

    def register(
        self,
        name: str,
        source: Any = None,
        *,
        replace: bool = False,
        origin: str = "code",
        **options: Any,
    ) -> PollRegistration:
        if not name or not isinstance(name, str):
            raise PollableConfigurationError(
                f"A polling endpoint needs a string name, got {name!r}."
            )
        if source is None:
            raise PollableConfigurationError(f"Endpoint {name!r} needs a `source=`.")
        if name in self._registrations and not replace:
            existing = self._registrations[name]
            raise PollableConfigurationError(
                f"A polling endpoint named {name!r} is already registered "
                f"({existing.describe()}). Pass replace=True to override it."
            )

        config = resolve_config(options)
        built = build_source(source, **options)
        registration = PollRegistration(name, built, config, origin=origin)
        self._registrations[name] = registration
        return registration

    def unregister(self, name: str) -> None:
        self._registrations.pop(name, None)

    def clear(self) -> None:
        """Drop every registration. Mainly for tests."""
        self._registrations.clear()
        self._settings_loaded = False

    def get(self, name: str) -> PollRegistration | None:
        return self._registrations.get(name)

    def names(self) -> list[str]:
        return sorted(self._registrations)

    def __iter__(self) -> Iterator[PollRegistration]:
        return iter(self._registrations.values())

    def __len__(self) -> int:
        return len(self._registrations)

    def __contains__(self, name: object) -> bool:
        return name in self._registrations

    # -- declaration style 4: central settings ----------------------------
    def load_from_settings(self) -> None:
        """Register everything declared in ``DRF_POLLABLE["ENDPOINTS"]``."""
        if self._settings_loaded:
            return
        for name, spec in settings_endpoints().items():
            options = dict(spec)
            source = options.pop("source", None)
            # Settings can only hold strings, so permission/throttle classes
            # arrive as dotted paths; _load_all resolves them at view build.
            self.register(name, source, replace=True, origin="settings", **options)
        self._settings_loaded = True

    # -- declaration style 3: model PollingMeta ---------------------------
    def load_from_models(self) -> None:
        """Scan installed models for a ``PollingMeta`` inner class."""
        from django.apps import apps

        for model in apps.get_models():
            meta = getattr(model, "PollingMeta", None)
            if meta is None:
                continue
            self._register_model_meta(model, meta)

    def _register_model_meta(self, model, meta) -> None:
        label = model._meta.model_name
        prefix = getattr(meta, "name", None) or label

        for field_name, options in (getattr(meta, "fields", None) or {}).items():
            options = dict(options or {})
            name = options.pop("name", f"{prefix}-{field_name}")
            self.register(
                name,
                model,
                field=field_name,
                replace=True,
                origin=f"model:{model._meta.label}",
                **options,
            )

        instance_options = getattr(meta, "instance", None)
        if instance_options is not None:
            options = dict(instance_options)
            name = options.pop("name", prefix)
            self.register(name, model, replace=True, origin=f"model:{model._meta.label}", **options)

        list_options = getattr(meta, "list", None)
        if list_options is not None:
            options = dict(list_options)
            name = options.pop("name", f"{prefix}-list")
            options["as_list"] = True
            self.register(name, model, replace=True, origin=f"model:{model._meta.label}", **options)


#: The registry used by ``register_poll``, the router, and the admin.
registry = PollRegistry()


# ---------------------------------------------------------------------------
# Declaration style 1: function / decorator
# ---------------------------------------------------------------------------


def register_poll(name: str, source: Any = None, **options: Any):
    """Register a polling endpoint.

    Works as a plain call::

        register_poll("order-status", source="shop.Order.status", min_poll_interval=5)

    or as a decorator, in which case class attributes fill in anything not
    passed explicitly::

        @register_poll("order-status")
        class OrderStatus:
            source = "shop.Order.status"
            min_poll_interval = 5
    """
    if source is not None:
        registry.register(name, source, **options)

        def identity(klass):
            return klass

        return identity

    def decorator(klass):
        merged = {**_class_options(klass), **options}
        klass_source = merged.pop("source", None)
        registry.register(name, klass_source, **merged)
        return klass

    return decorator


# ---------------------------------------------------------------------------
# Declaration style 2: class-based
# ---------------------------------------------------------------------------


class PollEndpoint:
    """Declare an endpoint by subclassing.

    ::

        class OrderStatus(PollEndpoint):
            name = "order-status"
            source = "shop.Order.status"
            min_poll_interval = 5
            done_when = {"delivered", "cancelled"}

    Subclasses register themselves. Set ``abstract = True`` to declare a shared
    base without registering it.
    """

    name: str | None = None
    source: Any = None
    abstract: bool = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("abstract"):
            return
        if not cls.name or cls.source is None:
            raise PollableConfigurationError(
                f"{cls.__name__} needs both `name` and `source` (or `abstract = True`)."
            )
        options = _class_options(cls)
        options.pop("source", None)
        options.pop("name", None)
        registry.register(cls.name, cls.source, replace=True, **options)


def _class_options(klass) -> dict[str, Any]:
    """Collect declaration options from a class body.

    Walks the MRO so a shared abstract base's defaults are inherited.
    """
    from .conf import BUILTIN_DEFAULTS
    from .sources import SOURCE_ARG_NAMES

    allowed = set(BUILTIN_DEFAULTS) | set(SOURCE_ARG_NAMES) | {"as_list"}
    options: dict[str, Any] = {}
    for klass_in_mro in reversed(klass.__mro__):
        for key, value in vars(klass_in_mro).items():
            if key in allowed:
                options[key] = value
    return options


def _load_all(value) -> list:
    if not isinstance(value, (list, tuple)):
        value = [value]
    return [import_string(item) if isinstance(item, str) else item for item in value]


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.replace("-", "_").split("_") if part)

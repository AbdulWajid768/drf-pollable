"""Poll sources -- the four things you can point a polling endpoint at.

Every declaration style in this package funnels down to one of these, so the
view / header / throttle machinery only has to be written once:

===================  ==========================================================
``FieldSource``      one field of one model instance  (``"shop.Order.status"``)
``InstanceSource``   several fields of one instance   (``"shop.Order"`` + fields)
``QuerySetSource``   a filtered list, count, or delta feed
``CallableSource``   any function -- the escape hatch for computed status
===================  ==========================================================

Model lookups are resolved *lazily*: a registration usually happens at import
time, when Django's app registry may not be populated yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
from typing import Any, Callable, Sequence

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models
from django.http import Http404
from django.shortcuts import get_object_or_404

from .exceptions import PollableConfigurationError

#: Constructor arguments understood by sources. ``conf.resolve_config`` skips
#: these so they are not mistaken for unknown behaviour options.
SOURCE_ARG_NAMES = frozenset(
    {
        "source",
        "model",
        "field",
        "fields",
        "queryset",
        "serializer",
        "done_when",
        "scope",
        "version_field",
        "progress_field",
        "eta_field",
        "since_field",
        "aggregate",
        "select_related",
        "requires_object",
        "include_display",
        "order_by",
        "limit",
    }
)

#: Field names we will auto-detect as a "last modified" signal.
AUTO_VERSION_FIELDS = ("updated_at", "modified_at", "modified", "updated")


@dataclass
class PollResult:
    """What a source hands back to the view."""

    value: Any
    last_modified: datetime | None = None
    done: bool = False
    progress: float | None = None
    eta_seconds: float | None = None
    extra: dict[str, Any] = dataclass_field(default_factory=dict)
    #: The underlying object, when there is one. Used for object-level
    #: permission checks; never serialized.
    instance: Any = None


def resolve_done(done_when: Any, subject: Any) -> bool:
    """Evaluate a ``done_when`` declaration against ``subject``.

    Accepts a callable, a collection of terminal values, or a single value::

        done_when=lambda v: v.startswith("final")
        done_when={"delivered", "cancelled"}
        done_when="complete"
    """
    if done_when is None:
        return False
    if callable(done_when):
        return bool(done_when(subject))
    if isinstance(done_when, (set, frozenset, list, tuple)):
        try:
            return subject in done_when
        except TypeError:
            return False
    return subject == done_when


def normalize_value(value: Any) -> Any:
    """Make a Python value safe to put in a JSON response.

    DRF's encoder handles dates/Decimal/UUID; model instances it does not, so
    collapse those to their primary key.
    """
    if isinstance(value, models.Model):
        return value.pk
    if isinstance(value, models.QuerySet):
        return [normalize_value(item) for item in value]
    if isinstance(value, models.manager.BaseManager):
        return [normalize_value(item) for item in value.all()]
    return value


class PollSource:
    """Base class. Subclasses implement :meth:`get_result`."""

    #: When True the URL carries a lookup segment and the view fetches an object.
    requires_object = False

    def get_result(self, request, **kwargs) -> PollResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover - introspection helper
        return self.__class__.__name__

    # -- shared helpers ---------------------------------------------------
    def _serialize_with(self, serializer_class, instance, request, many=False):
        serializer = serializer_class(instance, many=many, context={"request": request})
        return serializer.data


class BaseModelSource(PollSource):
    """Shared model resolution, scoping, and object lookup."""

    def __init__(
        self,
        model: Any,
        *,
        queryset: models.QuerySet | Callable[..., models.QuerySet] | None = None,
        lookup_field: str = "pk",
        lookup_url_kwarg: str | None = None,
        scope: Callable[[models.QuerySet, Any], models.QuerySet] | None = None,
        done_when: Any = None,
        version_field: str | None = None,
        progress_field: str | None = None,
        eta_field: str | None = None,
        select_related: Sequence[str] | None = None,
    ):
        self._model_spec = model
        self._model: type[models.Model] | None = None
        self._queryset = queryset
        self.lookup_field = lookup_field
        self.lookup_url_kwarg = lookup_url_kwarg or lookup_field
        self.scope = scope
        self.done_when = done_when
        self._version_field = version_field
        self._version_field_resolved = False
        self.progress_field = progress_field
        self.eta_field = eta_field
        self.select_related = list(select_related or [])

    @property
    def model(self) -> type[models.Model]:
        if self._model is None:
            self._model = _resolve_model(self._model_spec)
        return self._model

    @property
    def version_field(self) -> str | None:
        """Explicit ``version_field``, or an auto-detected timestamp field."""
        if not self._version_field_resolved:
            if self._version_field is None:
                self._version_field = _auto_version_field(self.model)
            self._version_field_resolved = True
        return self._version_field

    def get_queryset(self, request) -> models.QuerySet:
        if callable(self._queryset):
            queryset = self._queryset(request)
        elif self._queryset is not None:
            queryset = self._queryset.all()
        else:
            queryset = self.model._default_manager.all()

        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        if self.scope is not None:
            queryset = self.scope(queryset, request)
        return queryset

    def get_object(self, request, **kwargs):
        lookup_value = kwargs.get(self.lookup_url_kwarg)
        if lookup_value is None:
            raise PollableConfigurationError(
                f"URL kwarg {self.lookup_url_kwarg!r} missing for {self.describe()}."
            )
        try:
            return get_object_or_404(
                self.get_queryset(request), **{self.lookup_field: lookup_value}
            )
        except (ValueError, TypeError, ValidationError):
            # e.g. a non-numeric segment against an integer pk. That is a
            # missing object, not a server error.
            raise Http404(f"No {self.model._meta.object_name} matches {lookup_value!r}.") from None

    def _last_modified(self, obj) -> datetime | None:
        if not self.version_field:
            return None
        value = getattr(obj, self.version_field, None)
        return value if isinstance(value, datetime) else None

    def _progress(self, obj) -> float | None:
        if not self.progress_field:
            return None
        value = getattr(obj, self.progress_field, None)
        return float(value) if isinstance(value, (int, float)) else None

    def _eta(self, obj) -> float | None:
        if not self.eta_field:
            return None
        value = getattr(obj, self.eta_field, None)
        return float(value) if isinstance(value, (int, float)) else None


class FieldSource(BaseModelSource):
    """Poll a single field of a single instance -- the headline use case."""

    requires_object = True

    def __init__(self, model: Any, field: str, *, include_display: bool = True, **kwargs):
        super().__init__(model, **kwargs)
        self.field = field
        self.include_display = include_display
        self._validated = False

    def _validate(self):
        if self._validated:
            return
        try:
            self.model._meta.get_field(self.field)
        except FieldDoesNotExist:
            # Allow properties / annotations, but fail fast on a plain typo.
            if not hasattr(self.model, self.field):
                raise PollableConfigurationError(
                    f"{self.model._meta.label} has no field or attribute {self.field!r}."
                ) from None
        self._validated = True

    def get_result(self, request, **kwargs) -> PollResult:
        self._validate()
        obj = self.get_object(request, **kwargs)
        raw = getattr(obj, self.field)
        extra: dict[str, Any] = {}

        # Surface the human-readable label for choice fields -- the frontend
        # almost always needs it and would otherwise re-implement the mapping.
        if self.include_display:
            display_getter = getattr(obj, f"get_{self.field}_display", None)
            if callable(display_getter):
                label = display_getter()
                if label != raw:
                    extra["display"] = label

        return PollResult(
            value=normalize_value(raw),
            last_modified=self._last_modified(obj),
            done=resolve_done(self.done_when, raw),
            progress=self._progress(obj),
            eta_seconds=self._eta(obj),
            extra=extra,
            instance=obj,
        )

    def describe(self) -> str:
        return f"field {self._model_spec}.{self.field}"


class InstanceSource(BaseModelSource):
    """Poll several fields of one instance.

    Either ``fields`` or ``serializer`` is required: dumping every field by
    default would quietly leak columns as the model grows.
    """

    requires_object = True

    def __init__(
        self,
        model: Any,
        *,
        fields: Sequence[str] | None = None,
        serializer: Any = None,
        **kwargs,
    ):
        super().__init__(model, **kwargs)
        if not fields and serializer is None:
            raise PollableConfigurationError(
                "An instance source needs `fields=[...]` or `serializer=...` so it "
                "cannot leak fields added to the model later."
            )
        self.fields = list(fields or [])
        self.serializer = serializer

    def get_result(self, request, **kwargs) -> PollResult:
        obj = self.get_object(request, **kwargs)
        if self.serializer is not None:
            value = self._serialize_with(self.serializer, obj, request)
        else:
            value = {name: normalize_value(getattr(obj, name, None)) for name in self.fields}

        return PollResult(
            value=value,
            last_modified=self._last_modified(obj),
            done=resolve_done(self.done_when, obj),
            progress=self._progress(obj),
            eta_seconds=self._eta(obj),
            instance=obj,
        )

    def describe(self) -> str:
        return f"instance {self._model_spec}"


class QuerySetSource(BaseModelSource):
    """Poll a list, a count, or a delta feed.

    ``aggregate`` turns the endpoint into a cheap scalar poll ("how many unread
    notifications?") without serializing rows. ``since_field`` enables cursor
    polling: ``?since=<iso8601>`` returns only rows changed after that moment.
    """

    requires_object = False

    def __init__(
        self,
        model: Any,
        *,
        fields: Sequence[str] | None = None,
        serializer: Any = None,
        aggregate: str | Callable[[models.QuerySet], Any] | None = None,
        since_field: str | None = None,
        order_by: Sequence[str] | None = None,
        limit: int | None = None,
        **kwargs,
    ):
        super().__init__(model, **kwargs)
        if aggregate is None and not fields and serializer is None:
            raise PollableConfigurationError(
                "A queryset source needs `fields=[...]`, `serializer=...`, or "
                "`aggregate=...` so it cannot leak fields added to the model later."
            )
        self.fields = list(fields or [])
        self.serializer = serializer
        self.aggregate = aggregate
        self.since_field = since_field
        self.order_by = list(order_by or [])
        self.limit = limit

    def get_result(self, request, **kwargs) -> PollResult:
        queryset = self.get_queryset(request)

        since = self._parse_since(request)
        if since is not None and self.since_field:
            queryset = queryset.filter(**{f"{self.since_field}__gt": since})

        if self.order_by:
            queryset = queryset.order_by(*self.order_by)

        if self.aggregate is not None:
            value = self._apply_aggregate(queryset)
            return PollResult(value=value, done=resolve_done(self.done_when, value))

        if self.limit:
            queryset = queryset[: self.limit]

        if self.serializer is not None:
            value = self._serialize_with(self.serializer, queryset, request, many=True)
        else:
            value = [
                {name: normalize_value(getattr(obj, name, None)) for name in self.fields}
                for obj in queryset
            ]

        extra: dict[str, Any] = {"count": len(value)}
        # Hand back a cursor the client can send as ?since= on the next poll.
        if self.since_field:
            cursor = self._latest_cursor(queryset)
            if cursor is not None:
                extra["cursor"] = cursor.isoformat()

        return PollResult(value=value, done=resolve_done(self.done_when, value), extra=extra)

    def _apply_aggregate(self, queryset):
        if callable(self.aggregate):
            return self.aggregate(queryset)
        if self.aggregate == "count":
            return queryset.count()
        if self.aggregate == "exists":
            return queryset.exists()
        raise PollableConfigurationError(
            f"Unknown aggregate {self.aggregate!r}; use 'count', 'exists', or a callable."
        )

    def _parse_since(self, request):
        if not self.since_field:
            return None
        raw = request.query_params.get("since")
        if not raw:
            return None
        from django.utils.dateparse import parse_datetime

        return parse_datetime(raw)

    def _latest_cursor(self, queryset):
        latest = None
        for obj in queryset:
            value = getattr(obj, self.since_field, None)
            if isinstance(value, datetime) and (latest is None or value > latest):
                latest = value
        return latest

    def describe(self) -> str:
        what = self.aggregate if isinstance(self.aggregate, str) else "rows"
        return f"queryset {self._model_spec} ({what})"


class CallableSource(PollSource):
    """Poll the return value of any function -- the escape hatch.

    The callable receives ``(request, **url_kwargs)`` and may return either a
    plain value or a fully-populated :class:`PollResult`.
    """

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        done_when: Any = None,
        requires_object: bool = False,
        lookup_field: str = "pk",
        lookup_url_kwarg: str | None = None,
    ):
        if not callable(func):
            raise PollableConfigurationError(f"{func!r} is not callable.")
        self.func = func
        self.done_when = done_when
        self.requires_object = requires_object
        self.lookup_field = lookup_field
        self.lookup_url_kwarg = lookup_url_kwarg or lookup_field

    def get_result(self, request, **kwargs) -> PollResult:
        outcome = self.func(request, **kwargs)
        if isinstance(outcome, PollResult):
            if not outcome.done:
                outcome.done = resolve_done(self.done_when, outcome.value)
            return outcome
        return PollResult(
            value=normalize_value(outcome),
            done=resolve_done(self.done_when, outcome),
        )

    def describe(self) -> str:
        name = getattr(self.func, "__qualname__", repr(self.func))
        return f"callable {name}"


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


def build_source(spec: Any, **kwargs) -> PollSource:
    """Turn a ``source=`` declaration into a concrete :class:`PollSource`.

    Accepts:

    * ``"app.Model.field"``  -> :class:`FieldSource`
    * ``"app.Model"``        -> :class:`InstanceSource` / :class:`QuerySetSource`
    * a model class          -> same as above
    * a ``QuerySet``         -> :class:`QuerySetSource`
    * any callable           -> :class:`CallableSource`
    * a ``PollSource``       -> returned unchanged
    """
    if isinstance(spec, PollSource):
        return spec

    explicit_field = kwargs.pop("field", None)
    as_list = kwargs.pop("as_list", False)

    if isinstance(spec, models.QuerySet):
        model = spec.model
        return QuerySetSource(model, queryset=spec, **_for(QuerySetSource, kwargs))

    if isinstance(spec, str):
        parts = spec.split(".")
        if len(parts) == 3:
            model_spec, field_name = f"{parts[0]}.{parts[1]}", parts[2]
            return FieldSource(
                model_spec, explicit_field or field_name, **_for(FieldSource, kwargs)
            )
        if len(parts) == 2:
            return _model_source(spec, explicit_field, as_list, kwargs)
        raise PollableConfigurationError(
            f"Could not parse source {spec!r}. Use 'app_label.Model' or 'app_label.Model.field'."
        )

    if isinstance(spec, type) and issubclass(spec, models.Model):
        return _model_source(spec, explicit_field, as_list, kwargs)

    if callable(spec):
        return CallableSource(spec, **_for(CallableSource, kwargs))

    raise PollableConfigurationError(
        f"Unsupported source {spec!r}. Expected a model, queryset, 'app.Model.field' "
        "string, or callable."
    )


def _model_source(model_spec, explicit_field, as_list, kwargs):
    if explicit_field:
        return FieldSource(model_spec, explicit_field, **_for(FieldSource, kwargs))
    if as_list or kwargs.get("aggregate") is not None:
        return QuerySetSource(model_spec, **_for(QuerySetSource, kwargs))
    return InstanceSource(model_spec, **_for(InstanceSource, kwargs))


def _for(source_class, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop kwargs the target source does not accept.

    Registrations pass one flat bag of options; each source picks out its own.
    Walking the real MRO matters: ``CallableSource`` must not be handed the
    model-only options (``scope``, ``version_field``, ...) that
    ``BaseModelSource`` accepts.
    """
    import inspect

    accepted: set[str] = set()
    for klass in source_class.__mro__:
        init = klass.__dict__.get("__init__")
        if init is None:
            continue
        try:
            accepted |= set(inspect.signature(init).parameters)
        except (TypeError, ValueError):  # pragma: no cover
            continue
    accepted -= {"self", "args", "kwargs", "model", "func"}
    return {key: value for key, value in kwargs.items() if key in accepted}


def _resolve_model(spec: Any) -> type[models.Model]:
    if isinstance(spec, type) and issubclass(spec, models.Model):
        return spec
    if isinstance(spec, str):
        try:
            return apps.get_model(spec)
        except (LookupError, ValueError) as exc:
            raise PollableConfigurationError(f"Could not resolve model {spec!r}: {exc}") from exc
    raise PollableConfigurationError(f"Expected a model class or 'app.Model' string, got {spec!r}.")


def _auto_version_field(model: type[models.Model]) -> str | None:
    """Find a timestamp field to report as ``Last-Modified``.

    Only used for the header. The ``ETag`` hashes the value itself, because an
    ``auto_now`` column does not update on ``QuerySet.update()``.
    """
    names = {f.name for f in model._meta.get_fields() if isinstance(f, models.DateTimeField)}
    for candidate in AUTO_VERSION_FIELDS:
        if candidate in names:
            return candidate
    return None

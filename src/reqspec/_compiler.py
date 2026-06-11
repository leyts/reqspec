"""Compiles endpoint stubs into frozen request plans at class creation.

All signature and annotation introspection happens here, once per
endpoint. The per-call hot path only touches the resulting RequestPlan.
"""

import string
from annotationlib import Format, ForwardRef, get_annotations
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum, auto
from inspect import Parameter, signature
from typing import (
    TYPE_CHECKING,
    Annotated,
    NamedTuple,
    get_args,
    get_origin,
    get_type_hints,
)

from niquests import Response
from pydantic import BaseModel, TypeAdapter

from reqspec._markers import Body, Header, Marker, Path, Query

if TYPE_CHECKING:
    from reqspec.decorators import EndpointSpec
    from reqspec.exceptions import APIError

type _Fn = Callable[..., object]

_MISSING = object()
_adapters: dict[object, TypeAdapter[object]] = {}


class _ReturnKind(StrEnum):
    RAW = auto()
    NONE = auto()
    BYTES = auto()
    TEXT = auto()
    JSON = auto()
    MODEL = auto()


class ReturnSpec:
    """How to turn a Response into the endpoint's return value."""

    __slots__ = ("_adapter", "_fn", "kind")

    def __init__(
        self,
        kind: _ReturnKind,
        adapter: TypeAdapter[object] | None = None,
        fn: _Fn | None = None,
    ) -> None:
        self.kind = kind
        self._adapter = adapter
        self._fn = fn

    def load(self, response: Response) -> object:
        """Parse a successful response per the return annotation."""
        match self.kind:
            case _ReturnKind.MODEL:
                adapter = self._adapter
                if adapter is None:
                    adapter = self._build_deferred()
                return adapter.validate_json(response.content or b"")
            case _ReturnKind.RAW:
                return response
            case _ReturnKind.NONE:
                return None
            case _ReturnKind.BYTES:
                return response.content
            case _ReturnKind.TEXT:
                return response.text
            case _:
                return response.json()

    def _build_deferred(self) -> TypeAdapter[object]:
        if self._fn is None:  # pragma: no cover - defensive
            msg = "deferred return type without source function"
            raise TypeError(msg)
        hints = get_type_hints(self._fn)
        self._adapter = _adapter_for(hints["return"])
        return self._adapter


@dataclass(frozen=True, slots=True)
class Slot:
    """A compiled binding from a Python parameter to a wire name."""

    pyname: str
    wire: str


@dataclass(frozen=True, slots=True)
class RequestPlan:
    """Everything needed to issue one endpoint's request."""

    method: str
    url_parts: tuple[str, ...]
    path_names: tuple[str, ...]
    query_slots: tuple[Slot, ...]
    header_slots: tuple[Slot, ...]
    body_name: str | None
    static_headers: tuple[tuple[str, str], ...]
    raises_map: dict[int, type[APIError]]
    arg_names: tuple[str, ...]
    required: tuple[str, ...]
    defaults: tuple[tuple[str, object], ...]
    returns: ReturnSpec


def split_template(
    template: str, where: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a URL template into static parts and placeholder names."""
    parts: list[str] = []
    names: list[str] = []
    pending = ""
    for literal, name, spec, conversion in string.Formatter().parse(template):
        pending += literal
        if name is None:
            continue
        if not name.isidentifier() or spec or conversion:
            msg = (
                f"{where}: invalid placeholder {{{name}}} in URL"
                f" template {template!r}"
            )
            raise TypeError(msg)
        parts.append(pending)
        names.append(name)
        pending = ""
    parts.append(pending)
    return tuple(parts), tuple(names)


def _marker_of(annotation: object, where: str) -> tuple[object, Marker | None]:
    """Extract (base type, reqspec marker) from a parameter annotation."""
    if get_origin(annotation) is not Annotated:
        return annotation, None
    base, *extras = get_args(annotation)
    found = [e for e in extras if isinstance(e, Path | Query | Header | Body)]
    if len(found) > 1:
        kinds = ", ".join(type(m).__name__ for m in found)
        msg = f"{where}: conflicting markers ({kinds})"
        raise TypeError(msg)
    return base, found[0] if found else None


def _is_model(base: object) -> bool:
    return isinstance(base, type) and issubclass(base, BaseModel)


def _adapter_for(annotation: object) -> TypeAdapter[object]:
    try:
        cached = _adapters.get(annotation)
    except TypeError:
        return TypeAdapter(annotation)
    if cached is None:
        cached = TypeAdapter(annotation)
        _adapters[annotation] = cached
    return cached


def _is_unresolved(annotation: object) -> bool:
    """Whether an annotation still contains unevaluated forward refs."""
    if isinstance(annotation, str | ForwardRef):
        return True
    return any(_is_unresolved(arg) for arg in get_args(annotation))


_SENTINEL_KINDS: dict[object, _ReturnKind] = {
    Response: _ReturnKind.RAW,
    None: _ReturnKind.NONE,
    type(None): _ReturnKind.NONE,
    bytes: _ReturnKind.BYTES,
    str: _ReturnKind.TEXT,
    dict: _ReturnKind.JSON,
}


def _return_spec(fn: _Fn) -> ReturnSpec:
    annotation = get_annotations(fn, format=Format.FORWARDREF).get(
        "return", _MISSING
    )
    if annotation is _MISSING:
        return ReturnSpec(_ReturnKind.RAW)
    try:
        kind = _SENTINEL_KINDS.get(annotation)
    except TypeError:
        kind = None
    if kind is not None:
        return ReturnSpec(kind)
    if _is_unresolved(annotation):
        return ReturnSpec(_ReturnKind.MODEL, fn=fn)
    return ReturnSpec(_ReturnKind.MODEL, adapter=_adapter_for(annotation))


class _Classified(NamedTuple):
    path_map: dict[str, str]
    query_slots: list[Slot]
    header_slots: list[Slot]
    body_names: list[str]
    inferred_models: list[str]


def _classify_params(
    params: list[Parameter],
    placeholders: tuple[str, ...],
    where: str,
) -> _Classified:
    """Sort parameters into path/query/header/body bindings."""
    out = _Classified({}, [], [], [], [])
    for param in params:
        name = param.name
        base, marker = _marker_of(param.annotation, f"{where}({name})")
        match marker:
            case Path(name=wire):
                out.path_map[wire or name] = name
            case Query(name=wire):
                out.query_slots.append(Slot(name, wire or name))
            case Header() as header:
                out.header_slots.append(Slot(name, header.wire_name(name)))
            case Body():
                out.body_names.append(name)
            case None if name in placeholders:
                out.path_map[name] = name
            case None if _is_model(base):
                out.inferred_models.append(name)
            case None:
                out.query_slots.append(Slot(name, name))
    return out


def _resolve_body(classified: _Classified, where: str) -> str | None:
    body_names = classified.body_names
    inferred = classified.inferred_models
    if not body_names and len(inferred) == 1:
        body_names = inferred
    elif inferred:
        listed = ", ".join(body_names + inferred)
        msg = (
            f"{where}: ambiguous body — multiple model parameters"
            f" ({listed}); mark one with Body()"
        )
        raise TypeError(msg)
    if len(body_names) > 1:
        msg = f"{where}: multiple Body() parameters ({', '.join(body_names)})"
        raise TypeError(msg)
    return body_names[0] if body_names else None


def _check_placeholders(
    path_map: dict[str, str],
    placeholders: tuple[str, ...],
    where: str,
) -> None:
    unknown = [p for p in path_map if p not in placeholders]
    if unknown:
        listed = ", ".join(f"{{{p}}}" for p in unknown)
        msg = f"{where}: Path() targets unknown placeholders: {listed}"
        raise TypeError(msg)
    unbound = [p for p in placeholders if p not in path_map]
    if unbound:
        listed = ", ".join(f"{{{p}}}" for p in unbound)
        msg = f"{where}: URL placeholders without parameters: {listed}"
        raise TypeError(msg)


def compile_endpoint(
    fn: _Fn,
    spec: EndpointSpec,
    *,
    class_headers: dict[str, str],
    class_raises: dict[int, type[APIError]],
) -> RequestPlan:
    """Compile one decorated stub into a frozen RequestPlan."""
    where = getattr(fn, "__qualname__", repr(fn))
    if spec.method is None or spec.template is None:
        msg = f"{where}: missing @get/@post/@put/@patch/@delete decorator"
        raise TypeError(msg)
    url_parts, placeholders = split_template(spec.template, where)

    sig = signature(fn, annotation_format=Format.FORWARDREF)
    params = list(sig.parameters.values())[1:]  # drop self
    for param in params:
        if param.kind in (Parameter.VAR_POSITIONAL, Parameter.VAR_KEYWORD):
            msg = f"{where}: *args/**kwargs are not allowed on endpoints"
            raise TypeError(msg)

    classified = _classify_params(params, placeholders, where)
    _check_placeholders(classified.path_map, placeholders, where)

    return RequestPlan(
        method=spec.method,
        url_parts=url_parts,
        path_names=tuple(classified.path_map[p] for p in placeholders),
        query_slots=tuple(classified.query_slots),
        header_slots=tuple(classified.header_slots),
        body_name=_resolve_body(classified, where),
        static_headers=tuple({**class_headers, **spec.headers}.items()),
        raises_map={**class_raises, **spec.raises},
        arg_names=tuple(p.name for p in params),
        required=tuple(p.name for p in params if p.default is Parameter.empty),
        defaults=tuple(
            (p.name, p.default)
            for p in params
            if p.default is not Parameter.empty
        ),
        returns=_return_spec(fn),
    )

"""Compiles endpoint stubs into frozen request plans at class creation.

All signature and annotation introspection happens here, once per
endpoint. The per-call hot path only touches the resulting RequestPlan.
"""

import string
from annotationlib import Format, ForwardRef, get_annotations
from dataclasses import dataclass
from enum import StrEnum, auto
from inspect import Parameter, signature
from typing import (
    TYPE_CHECKING,
    Annotated,
    NamedTuple,
    get_args,
    get_origin,
)

from niquests import Response
from pydantic import BaseModel, TypeAdapter

from reqspec._markers import Body, Header, Marker, Path, Query

if TYPE_CHECKING:
    from reqspec._decorators import EndpointSpec, Fn
    from reqspec._exceptions import APIError


MISSING = object()
adapters: dict[object, TypeAdapter[object]] = {}


class ReturnKind(StrEnum):
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
        kind: ReturnKind,
        adapter: TypeAdapter[object] | None = None,
        fn: Fn | None = None,
    ) -> None:
        self.kind = kind
        self._adapter = adapter
        self._fn = fn

    def load(self, response: Response) -> object:
        """Parse a successful response per the return annotation."""
        match self.kind:
            case ReturnKind.MODEL:
                adapter = self._adapter
                if adapter is None:
                    adapter = self._build_deferred()
                return adapter.validate_json(response.content or b"")
            case ReturnKind.RAW:
                return response
            case ReturnKind.NONE:
                return None
            case ReturnKind.BYTES:
                return response.content
            case ReturnKind.TEXT:
                return response.text
            case _:
                return response.json()

    def _build_deferred(self) -> TypeAdapter[object]:
        if self._fn is None:  # pragma: no cover - defensive
            msg = "deferred return type without source function"
            raise TypeError(msg)
        hints = get_annotations(self._fn, format=Format.VALUE)
        self._adapter = adapter_for(hints["return"])
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


def marker_of(annotation: object, where: str) -> tuple[object, Marker | None]:
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


def is_model(base: object) -> bool:
    return isinstance(base, type) and issubclass(base, BaseModel)


def adapter_for(annotation: object) -> TypeAdapter[object]:
    try:
        cached = adapters.get(annotation)
    except TypeError:
        return TypeAdapter(annotation)
    if cached is None:
        cached = TypeAdapter(annotation)
        adapters[annotation] = cached
    return cached


def is_unresolved(annotation: object) -> bool:
    """Whether an annotation still contains unevaluated forward refs."""
    if isinstance(annotation, str | ForwardRef):
        return True
    return any(is_unresolved(arg) for arg in get_args(annotation))


SENTINEL_KINDS: dict[object, ReturnKind] = {
    Response: ReturnKind.RAW,
    None: ReturnKind.NONE,
    type(None): ReturnKind.NONE,
    bytes: ReturnKind.BYTES,
    str: ReturnKind.TEXT,
    dict: ReturnKind.JSON,
}


def return_spec(fn: Fn) -> ReturnSpec:
    annotation = get_annotations(fn, format=Format.FORWARDREF).get(
        "return", MISSING
    )
    if annotation is MISSING:
        return ReturnSpec(ReturnKind.RAW)
    try:
        kind = SENTINEL_KINDS.get(annotation)
    except TypeError:
        kind = None
    if kind is not None:
        return ReturnSpec(kind)
    if is_unresolved(annotation):
        return ReturnSpec(ReturnKind.MODEL, fn=fn)
    return ReturnSpec(ReturnKind.MODEL, adapter=adapter_for(annotation))


class Classified(NamedTuple):
    path_map: dict[str, str]
    query_slots: list[Slot]
    header_slots: list[Slot]
    body_names: list[str]
    inferred_models: list[str]


def classify_params(
    params: list[Parameter],
    placeholders: tuple[str, ...],
    where: str,
) -> Classified:
    """Sort parameters into path/query/header/body bindings."""
    out = Classified({}, [], [], [], [])
    for param in params:
        name = param.name
        base, marker = marker_of(param.annotation, f"{where}({name})")
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
            case None if is_model(base):
                out.inferred_models.append(name)
            case None:
                out.query_slots.append(Slot(name, name))
    return out


def resolve_body(classified: Classified, where: str) -> str | None:
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


def check_placeholders(
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
    fn: Fn,
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

    classified = classify_params(params, placeholders, where)
    check_placeholders(classified.path_map, placeholders, where)

    return RequestPlan(
        method=spec.method,
        url_parts=url_parts,
        path_names=tuple(classified.path_map[p] for p in placeholders),
        query_slots=tuple(classified.query_slots),
        header_slots=tuple(classified.header_slots),
        body_name=resolve_body(classified, where),
        static_headers=tuple({**class_headers, **spec.headers}.items()),
        raises_map={**class_raises, **spec.raises},
        arg_names=tuple(p.name for p in params),
        required=tuple(p.name for p in params if p.default is Parameter.empty),
        defaults=tuple(
            (p.name, p.default)
            for p in params
            if p.default is not Parameter.empty
        ),
        returns=return_spec(fn),
    )

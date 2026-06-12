"""Compiles endpoint stubs into frozen request plans at class creation.

All signature and annotation introspection happens here, once per
endpoint. The per-call hot path only touches the resulting RequestPlan.
"""

import string
from annotationlib import Format, ForwardRef, get_annotations
from collections.abc import Callable
from dataclasses import dataclass, field
from inspect import Parameter, Signature, signature
from typing import (
    TYPE_CHECKING,
    Annotated,
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


type ReturnLoader = Callable[[Response], object]
"""Turns a successful Response into the endpoint's return value."""


def model_loader(adapter: TypeAdapter[object]) -> ReturnLoader:
    return lambda response: adapter.validate_json(response.content or b"")


def deferred_loader(fn: Fn) -> ReturnLoader:
    """Resolve the return annotation on first call, then parse with it."""
    adapter: TypeAdapter[object] | None = None

    def load(response: Response) -> object:
        nonlocal adapter
        if adapter is None:
            hints = get_annotations(fn, format=Format.VALUE)
            adapter = adapter_for(hints["return"])
        return adapter.validate_json(response.content or b"")

    return load


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
    signature: Signature
    returns: ReturnLoader


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
    for extra in extras:
        if isinstance(extra, type) and issubclass(extra, Marker):
            msg = f"{where}: marker {extra.__name__} must be instantiated."
            raise TypeError(msg)
    found = [e for e in extras if isinstance(e, Marker)]
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


SENTINEL_LOADERS: dict[object, ReturnLoader] = {
    Response: lambda response: response,
    None: lambda _: None,
    type(None): lambda _: None,
    bytes: lambda response: response.content,
    str: lambda response: response.text,
    dict: lambda response: response.json(),
}


def return_loader(fn: Fn) -> ReturnLoader:
    """Choose how to parse responses from the return annotation."""
    annotation = get_annotations(fn, format=Format.FORWARDREF).get(
        "return", MISSING
    )
    if annotation is MISSING:
        return SENTINEL_LOADERS[Response]
    try:
        loader = SENTINEL_LOADERS.get(annotation)
    except TypeError:
        loader = None
    if loader is not None:
        return loader
    if is_unresolved(annotation):
        return deferred_loader(fn)
    return model_loader(adapter_for(annotation))


@dataclass(slots=True)
class Classified:
    path_map: dict[str, str] = field(default_factory=dict)
    query_slots: list[Slot] = field(default_factory=list)
    header_slots: list[Slot] = field(default_factory=list)
    body_names: list[str] = field(default_factory=list)
    inferred_models: list[str] = field(default_factory=list)


def classify_params(
    params: list[Parameter],
    placeholders: tuple[str, ...],
    where: str,
) -> Classified:
    """Sort parameters into path/query/header/body bindings."""
    out = Classified()
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
        signature=sig.replace(parameters=params),
        returns=return_loader(fn),
    )

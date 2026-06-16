"""Stash-only decorators for declaring endpoints.

Every decorator here only records metadata on the decorated function
(or class) — nothing is wrapped, so decorator order never matters.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from niquests.structures import CaseInsensitiveDict

from reqspec._exceptions import APIError

if TYPE_CHECKING:
    from niquests.typing import HeadersType

STASH_ATTR = "__reqspec__"

type Fn = Callable[..., object]

type HeaderMap = CaseInsensitiveDict[str | bytes, str | bytes]
"""Normalized header store covering niquests' str-or-bytes surface."""


@dataclass(slots=True)
class EndpointSpec:
    """Mutable metadata accumulated by decorators on an endpoint stub."""

    method: str | None = None
    template: str | None = None
    headers: HeaderMap = field(default_factory=CaseInsensitiveDict)
    raises: dict[int, type[APIError]] = field(default_factory=dict)


def spec_of(fn: Fn) -> EndpointSpec:
    """Get or create the EndpointSpec stashed on a function."""
    spec = getattr(fn, STASH_ATTR, None)
    if spec is None:
        spec = EndpointSpec()
        setattr(fn, STASH_ATTR, spec)
    return spec


def http_method[F: Fn](method: str) -> Callable[[str], Callable[[F], F]]:
    def with_template(template: str) -> Callable[[F], F]:
        def apply(fn: F) -> F:
            spec = spec_of(fn)
            if spec.method is not None:
                name = getattr(fn, "__qualname__", repr(fn))
                msg = (
                    f"{name} already declared as"
                    f" {spec.method} {spec.template!r}"
                )
                raise TypeError(msg)
            spec.method = method
            spec.template = template
            return fn

        return apply

    return with_template


get = http_method("GET")
post = http_method("POST")
put = http_method("PUT")
patch = http_method("PATCH")
delete = http_method("DELETE")


def headers[T: type | Fn](mapping: HeadersType) -> Callable[[T], T]:
    """Attach static headers to an endpoint or a whole client class."""
    static: HeaderMap = CaseInsensitiveDict(mapping)

    def apply(target: T) -> T:
        apply_config(target, headers=static)
        return target

    return apply


def raises[T: type | Fn](mapping: Mapping[int, object]) -> Callable[[T], T]:
    """Map response status codes to typed APIError subclasses."""
    mapped: dict[int, type[APIError]] = {}

    for status, exc_type in mapping.items():
        if not (isinstance(exc_type, type) and issubclass(exc_type, APIError)):
            msg = (
                f"@raises mapping for status {status} must be an APIError"
                f" subclass; got {exc_type!r}"
            )
            raise TypeError(msg)

        mapped[status] = exc_type

    def apply(target: T) -> T:
        apply_config(target, raises=mapped)
        return target

    return apply


def apply_config(
    target: object,
    *,
    headers: HeaderMap | None = None,
    raises: dict[int, type[APIError]] | None = None,
) -> None:
    if isinstance(target, type):
        configure = getattr(target, "_reqspec_configure", None)
        if configure is None:
            msg = (
                f"@headers/@raises can only be applied to reqspec Client"
                f" subclasses; got {target.__name__!r}"
            )
            raise TypeError(msg)
        configure(headers=headers, raises=raises)
        return
    spec = spec_of(cast("Fn", target))
    if headers is not None:
        spec.headers.update(headers)
    if raises is not None:
        spec.raises.update(raises)

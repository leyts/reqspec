"""Annotated markers that override parameter binding inference."""

from dataclasses import dataclass


class Marker:
    """Base class for parameter binding markers."""

    __slots__ = ()


def to_header_case(name: str) -> str:
    """Convert a snake_case name to header case."""
    return "-".join(part.capitalize() for part in name.split("_"))


@dataclass(frozen=True, slots=True, kw_only=True)
class Path(Marker):
    """Bind a parameter to a URL template placeholder."""

    name: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Query(Marker):
    """Bind a parameter to a query string key."""

    name: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Header(Marker):
    """Bind a parameter to a request header."""

    name: str | None = None
    convert_underscores: bool = True

    def wire_name(self, pyname: str) -> str:
        """Resolve the request header name."""
        if self.name is not None:
            return self.name
        if self.convert_underscores:
            return to_header_case(pyname)
        return pyname


@dataclass(frozen=True, slots=True, kw_only=True)
class Body(Marker):
    """Bind a parameter to the JSON request body."""

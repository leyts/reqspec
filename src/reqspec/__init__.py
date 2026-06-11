"""reqspec: a declarative HTTP client."""

from reqspec._client import Client
from reqspec._decorators import (
    delete,
    get,
    headers,
    patch,
    post,
    put,
    raises,
)
from reqspec._exceptions import APIError
from reqspec._markers import Body, Header, Path, Query

__all__ = [
    "APIError",
    "Body",
    "Client",
    "Header",
    "Path",
    "Query",
    "delete",
    "get",
    "headers",
    "patch",
    "post",
    "put",
    "raises",
]

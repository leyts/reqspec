"""Client base class: endpoint compilation and the per-call hot path."""

import functools
from typing import TYPE_CHECKING, ClassVar, cast
from urllib.parse import quote

import niquests
from pydantic import BaseModel

from reqspec._compiler import RequestPlan, adapter_for, compile_endpoint
from reqspec._decorators import STASH_ATTR, EndpointSpec, Fn
from reqspec._exceptions import APIError, raise_for_response

if TYPE_CHECKING:
    from collections.abc import Mapping

    from niquests.typing import (
        HttpAuthenticationType,
        QueryParameterType,
        TimeoutType,
    )


SUCCESS = range(200, 300)


class Client:
    """Base class for declarative API clients.

    Subclass with endpoint stubs and a base URL::

        class GitHub(Client, base_url="https://api.github.com"):
            @get("/users/{user}/repos")
            def repos(self, user: str) -> list[Repo]: ...
    """

    _reqspec_base_url: ClassVar[str | None] = None
    _reqspec_endpoints: ClassVar[dict[str, Fn]] = {}
    _reqspec_headers: ClassVar[dict[str, str]] = {}
    _reqspec_raises: ClassVar[dict[int, type[APIError]]] = {}

    def __init_subclass__(
        cls,
        *,
        base_url: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if base_url is not None:
            cls._reqspec_base_url = base_url
        cls._reqspec_endpoints = dict(cls._reqspec_endpoints) | {
            name: member
            for name, member in vars(cls).items()
            if callable(member) and hasattr(member, STASH_ATTR)
        }
        cls._reqspec_headers = dict(cls._reqspec_headers)
        cls._reqspec_raises = dict(cls._reqspec_raises)
        compile_all(cls)

    def __init__(
        self,
        *,
        session: niquests.Session | None = None,
        base_url: str | None = None,
        headers: Mapping[str, str] | None = None,
        auth: HttpAuthenticationType | None = None,
        timeout: TimeoutType | None = None,
    ) -> None:
        base = base_url or self._reqspec_base_url
        if base is None:
            msg = (
                f"{type(self).__name__} has no base URL: pass"
                " base_url here or as a class keyword argument"
            )
            raise TypeError(msg)
        self._base = base.rstrip("/")
        self._session = session if session is not None else niquests.Session()
        if headers:
            self._session.headers.update(headers.items())
        self._auth = auth
        self._timeout = timeout


def update_class_config(
    cls: type[Client],
    *,
    headers: dict[str, str] | None = None,
    raises: dict[int, type[APIError]] | None = None,
) -> None:
    """Apply config from class-level decorators and recompile."""
    if headers:
        cls._reqspec_headers.update(headers)
    if raises:
        cls._reqspec_raises.update(raises)
    compile_all(cls)


def compile_all(cls: type[Client]) -> None:
    for name, fn in cls._reqspec_endpoints.items():
        spec: EndpointSpec = getattr(fn, STASH_ATTR)
        plan = compile_endpoint(
            fn,
            spec,
            class_headers=cls._reqspec_headers,
            class_raises=cls._reqspec_raises,
        )
        endpoint = make_endpoint(plan)
        functools.update_wrapper(endpoint, fn)
        setattr(cls, name, endpoint)


def bind_arguments(
    plan: RequestPlan,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> dict[str, object]:
    names = plan.arg_names

    if len(args) > len(names):
        msg = f"expected at most {len(names)} arguments, got {len(args)}"
        raise TypeError(msg)

    values = dict(plan.defaults)
    for name, value in zip(names, args, strict=False):
        if name in kwargs:
            msg = f"got multiple values for argument {name!r}"
            raise TypeError(msg)
        values[name] = value

    for name in kwargs:
        if name not in names:
            msg = f"got an unexpected keyword argument {name!r}"
            raise TypeError(msg)

    values |= kwargs

    missing = [n for n in plan.required if n not in values]

    if missing:
        msg = f"missing required arguments: {', '.join(missing)}"
        raise TypeError(msg)
    return values


def make_endpoint(plan: RequestPlan) -> Fn:
    def endpoint(self: Client, *args: object, **kwargs: object) -> object:
        values = bind_arguments(plan, args, kwargs)

        pieces = [self._base]
        for i, pyname in enumerate(plan.path_names):
            pieces.append(plan.url_parts[i])
            pieces.append(quote(str(values[pyname]), safe=""))
        pieces.append(plan.url_parts[-1])
        url = "".join(pieces)

        # niquests stringifies values itself; its stub type is narrower
        params = cast(
            "QueryParameterType",
            {
                slot.wire: value
                for slot in plan.query_slots
                if (value := values[slot.pyname]) is not None
            },
        )
        headers = dict(plan.static_headers)
        for slot in plan.header_slots:
            value = values[slot.pyname]
            if value is not None:
                headers[slot.wire] = str(value)

        data: bytes | None = None
        json: object = None
        if plan.body_name is not None:
            payload = values[plan.body_name]
            if isinstance(payload, BaseModel):
                data = adapter_for(type(payload)).dump_json(payload)
                headers.setdefault("Content-Type", "application/json")
            else:
                json = payload

        response = self._session.request(
            plan.method,
            url,
            params=params or None,
            data=data,
            json=json,
            headers=headers or None,
            auth=self._auth,
            timeout=self._timeout,
        )
        status = response.status_code or 0  # None on unresolved lazy responses
        if status not in SUCCESS:
            raise_for_response(response, plan.raises_map)
        return plan.returns.load(response)

    return endpoint

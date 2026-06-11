"""API error types raised for non-2xx responses."""

from typing import TYPE_CHECKING, ClassVar, NoReturn

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from niquests import Response


def raise_for_response(
    response: Response,
    raises_map: dict[int, type[APIError]],
) -> NoReturn:
    """Raise the mapped (or generic) APIError for an error response."""
    status = response.status_code or 0
    exc_type = raises_map.get(status, APIError)
    message = (
        f"{response.request.method if response.request else '?'}"
        f" {response.url} returned {status}"
    )
    parsed: BaseModel | None = None
    model = exc_type.body_model
    if model is not None:
        try:
            parsed = model.model_validate_json(response.content or b"")
        except ValidationError:
            message += " (error body failed validation)"
    error = exc_type(message, response=response)
    error.body = parsed
    raise error


class APIError(Exception):
    """Raised when a response has a non-2xx status code.

    Subclasses may declare a Pydantic model for the error body::

        class GitHubError(APIError, body=ErrorBody): ...

    The parsed body is then available as ``.body`` on the raised
    exception.
    """

    body_model: ClassVar[type[BaseModel] | None] = None

    def __init_subclass__(
        cls,
        *,
        body: type[BaseModel] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if body is not None:
            cls.body_model = body

    def __init__(self, message: str, *, response: Response) -> None:
        super().__init__(message)
        self.response = response
        self.status_code = response.status_code
        self.body: BaseModel | None = None

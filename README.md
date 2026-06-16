# reqspec

> [!WARNING]
> This package is under development. The API is subject to breaking changes.

A declarative HTTP client for Python, built on
[Niquests](https://niquests.readthedocs.io/en/latest) and [Pydantic](https://docs.pydantic.dev/).

## Usage

Describe an API as a class of typed method stubs. reqspec compiles each stub
into a real request at class creation; type hints drive both the request and
the response.

```python
from pydantic import BaseModel

from reqspec import Client, get


class Repo(BaseModel):
    full_name: str
    stargazers_count: int


class GitHub(Client, base_url="https://api.github.com"):
    @get("/users/{user}/repos")
    def repos(self, user: str, per_page: int = 30) -> list[Repo]: ...


gh = GitHub()
repos = gh.repos("astral-sh")  # list[Repo]
```

A parameter matching a `{placeholder}` becomes a path parameter; everything
else becomes a query parameter. The return annotation drives parsing.

## Licence

[MIT](LICENCE)

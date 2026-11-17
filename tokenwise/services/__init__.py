"""What the CLI, the local web page and the hosted server do, without their I/O.

Each interface (cli.py, web.py, server/) only parses its input, calls a service and shows the
result. A service never prints and never knows about HTTP; it raises ServiceError with a
status the web layer turns into a response."""

from __future__ import annotations


class ServiceError(Exception):
    """A request that cannot be done as asked. `status` follows HTTP: 400 bad input, 404 not
    found, 409 conflict (budget, stale file), 502 the model's reply was unusable."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

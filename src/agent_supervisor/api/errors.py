"""API error responses: always `{"error": {"code": ..., "message": ...}}`."""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from agent_supervisor import db
from agent_supervisor.runner import BudgetExhaustedError, NoActiveRunError, UnknownAgentError

NOT_FOUND, CONFLICT, INVALID, INTERNAL = 404, 409, 422, 500


# Codes for errors the framework returns itself (unknown path, wrong method); any other
# status is `http_error`.
HTTP_ERROR_CODES = {404: "not_found", 405: "method_not_allowed"}
Handler = Callable[[Request, Exception], Awaitable[JSONResponse]]


class InvalidRequestError(ValueError):
    """The request content is not acceptable (e.g. the path is not a git repo)."""


class ProjectExistsError(RuntimeError):
    """This repo has already been added."""


# Exception type -> (HTTP status, machine-readable code). The message is the exception's text.
ERROR_RESPONSES: dict[type[Exception], tuple[int, str]] = {
    db.NotFoundError: (NOT_FOUND, "not_found"),
    db.RunAlreadyActiveError: (CONFLICT, "run_already_active"),
    db.TaskNotRunnableError: (CONFLICT, "task_not_runnable"),
    BudgetExhaustedError: (CONFLICT, "budget_exhausted"),
    NoActiveRunError: (CONFLICT, "no_active_run"),
    ProjectExistsError: (CONFLICT, "project_exists"),
    UnknownAgentError: (INVALID, "invalid_request"),
    InvalidRequestError: (INVALID, "invalid_request"),
    # Unexpected errors (e.g. git failing during archive) also get the same response shape.
    Exception: (INTERNAL, "internal_error"),
}


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Build an error response."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


def install_error_handlers(app: FastAPI) -> None:
    """Make known exceptions and field validation failures all respond with the same shape."""

    def make_handler(status_code: int, code: str) -> Handler:
        async def handle(request: Request, error: Exception) -> JSONResponse:
            return error_response(status_code, code, str(error))

        return handle

    for error_type, (status_code, code) in ERROR_RESPONSES.items():
        app.add_exception_handler(error_type, make_handler(status_code, code))

    async def handle_validation(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, RequestValidationError)
        problems = [
            f"{'.'.join(str(part) for part in problem['loc'][1:])}: {problem['msg']}"
            for problem in error.errors()
        ]
        return error_response(INVALID, "invalid_request", "; ".join(problems))

    app.add_exception_handler(RequestValidationError, handle_validation)

    async def handle_http_error(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, HTTPException)
        code = HTTP_ERROR_CODES.get(error.status_code, "http_error")
        return error_response(error.status_code, code, str(error.detail))

    app.add_exception_handler(HTTPException, handle_http_error)

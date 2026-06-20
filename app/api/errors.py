"""HTTP error contract — produces exactly what transport.ts expects.

The frontend transport derives the ApiErrorCode from the HTTP STATUS code
(adapters/transport.ts STATUS_TO_CODE), reading only ``message`` and
``retryAfterSeconds`` from the JSON body. So the backend's job is:
  - return the correct HTTP status (the frontend maps it), and
  - return a body shaped ``{"message": str, "retryAfterSeconds"?: int}``.

STATUS_TO_CODE (frozen, transport.ts):
  401 -> unauthorized, 402 -> payment_required, 403 -> unauthorized,
  404 -> not_found, 409 -> conflict, 422 -> validation_error,
  429 -> rate_limited, (anything else) -> server_error.

FLAGGED (BUILD_SPEC §4): there is no status that maps to ``verification_required``
or ``at_cap``. Unverified gating is expressed as documented: ``me()`` returns 200
with state='unverified' + nextRoute='verify_wall', and gated writes return 403
(-> unauthorized, which routes to login). ``at_cap`` is surfaced as billing data.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Raised by handlers; rendered to the frozen wire error shape."""

    def __init__(self, status_code: int, message: str, retry_after_seconds: int | None = None):
        self.status_code = status_code
        self.message = message
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


# Convenience constructors keyed to the frozen status map.
def unauthorized(message: str = "Session invalid or expired.") -> ApiError:
    return ApiError(401, message)


def forbidden(message: str = "Not permitted.") -> ApiError:
    # 403 also maps to 'unauthorized' on the frontend (transport.ts).
    return ApiError(403, message)


def payment_required(message: str = "Payment method required.") -> ApiError:
    return ApiError(402, message)


def not_found(message: str = "Not found.") -> ApiError:
    return ApiError(404, message)


def conflict(message: str = "Conflict.") -> ApiError:
    return ApiError(409, message)


def validation_error(message: str = "Validation failed.") -> ApiError:
    return ApiError(422, message)


def rate_limited(message: str, retry_after_seconds: int | None = None) -> ApiError:
    return ApiError(429, message, retry_after_seconds)


def _body(err: ApiError) -> dict[str, object]:
    body: dict[str, object] = {"message": err.message}
    if err.retry_after_seconds is not None:
        body["retryAfterSeconds"] = err.retry_after_seconds
    return body


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_body(exc))

    # Axis10-F02: FastAPI's native RequestValidationError emits a 422 with a
    # ``{"detail": [...]}`` body, which does NOT match the wire error contract
    # (the frontend transport reads ``{message, retryAfterSeconds?}`` and maps the
    # STATUS — 422 → validation_error). Convert it to the contract body shape so the
    # frontend shows a real message instead of falling back to statusText.
    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errs = exc.errors()
        if errs:
            first = errs[0]
            loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
            msg = first.get("msg", "Validation failed.")
            message = f"{loc}: {msg}" if loc else msg
        else:
            message = "Validation failed."
        return JSONResponse(status_code=422, content={"message": message})

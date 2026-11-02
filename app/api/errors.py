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

TYPED REFUSALS (round 7 WP-5 / owner decision D5): a refusal the frontend must tell
apart from its status-mates carries two OPTIONAL extra keys — ``code`` (a stable
machine word) and ``detail`` (code-shaped values only: instants, counts, state names).
The one user today is ``account_closed``: a 403 whose ``detail.reopenUntil`` lets the
frontend render the reduced closed-account page instead of bouncing to login. A
client that reads only ``message`` sees exactly what it saw before.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Raised by handlers; rendered to the frozen wire error shape."""

    def __init__(
        self,
        status_code: int,
        message: str,
        retry_after_seconds: int | None = None,
        *,
        code: str | None = None,
        detail: dict[str, object] | None = None,
    ):
        self.status_code = status_code
        self.message = message
        self.retry_after_seconds = retry_after_seconds
        self.code = code
        self.detail = detail
        super().__init__(message)


# Convenience constructors keyed to the frozen status map.
def unauthorized(message: str = "Session invalid or expired.") -> ApiError:
    return ApiError(401, message)


def forbidden(message: str = "Not permitted.") -> ApiError:
    # 403 also maps to 'unauthorized' on the frontend (transport.ts).
    return ApiError(403, message)


#: The typed-refusal word for a closed account (D5). The frontend keys on it.
ACCOUNT_CLOSED = "account_closed"


def account_closed(*, closed_at: str | None, reopen_until: str | None) -> ApiError:
    """403 for every route a closed account may not reach (owner decision D5).

    ``detail.reopenUntil`` is the instant the 30-day reopen window ends (ISO-8601,
    UTC) — in the future while the owner can still reopen or re-request the export,
    in the past once the window has closed, ``null`` when the closure instant is not
    on record. Still a 403, so a client that knows nothing of ``code`` behaves as
    before."""
    return ApiError(
        403,
        "Account closed.",
        code=ACCOUNT_CLOSED,
        detail={"closedAt": closed_at, "reopenUntil": reopen_until},
    )


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


def service_unavailable(message: str = "Temporarily unavailable.") -> ApiError:
    """A dependency this response NEEDS did not answer, and the data is not lost.

    Reserved for the case where returning the successful shape would be a lie about
    content — an archived transcript cold storage did not hand back reads as a
    conversation with no messages unless we say otherwise (§ honest error states).
    """
    return ApiError(503, message)


def _body(err: ApiError) -> dict[str, object]:
    body: dict[str, object] = {"message": err.message}
    if err.retry_after_seconds is not None:
        body["retryAfterSeconds"] = err.retry_after_seconds
    if err.code is not None:
        body["code"] = err.code
    if err.detail is not None:
        body["detail"] = err.detail
    return body


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        headers = (
            # A 429 carries its wait on the wire too (RFC 9110), not only in the body.
            {"Retry-After": str(exc.retry_after_seconds)}
            if exc.retry_after_seconds is not None
            else None
        )
        return JSONResponse(status_code=exc.status_code, content=_body(exc), headers=headers)

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

    # Per-admin LLM concurrency cap (Architecture §9 C9 / §4.6). When a tenant is over
    # the 50/min cap the router raises ``LLMCapExceeded`` from inside a turn. Surface it
    # as a 429 (→ rate_limited via the frozen STATUS_TO_CODE map) so the conversation is
    # gracefully rate-limited rather than 500-dropped. This only TRANSPORTS the existing
    # cap to the wire — it does not change when the cap fires. Imported lazily to avoid
    # an import cycle (errors is imported very early, llm_cap pulls in config).
    from app.runtime.llm_cap import LLMCapExceeded  # noqa: PLC0415

    @app.exception_handler(LLMCapExceeded)
    async def _handle_llm_cap(_: Request, exc: LLMCapExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={
                "message": "We're handling a lot of requests right now — please retry in a moment.",
                "retryAfterSeconds": 5,
            },
        )

    # The embedding provider failed while INDEXING (paste/upload/CSV/replace/sync).
    # 2026-09-06 E2E walk (E2E-12): with OpenAI answering 429 this escaped as a bare
    # 500 — no CORS headers on that path — and the dashboard told the owner
    # "Network request failed." The write rolls back with the tenant session
    # (nothing half-indexed), the owner reads what actually happened, and the
    # operator is paged through the same metric the retrieval alarm watches.
    from app.knowledge.embeddings import (  # noqa: PLC0415
        EMBEDDING_DOWN,
        EMBEDDING_RETRY_AFTER_SECONDS,
        EmbeddingError,
    )
    from app.observability.metrics import emit_metric  # noqa: PLC0415

    @app.exception_handler(EmbeddingError)
    async def _handle_embedding_error(_: Request, exc: EmbeddingError) -> JSONResponse:
        emit_metric("KnowledgeRetrievalDegraded", dimensions={"Reason": f"ingest:{exc}"[:120]})
        return JSONResponse(
            status_code=503,
            content={
                "message": EMBEDDING_DOWN,
                "retryAfterSeconds": EMBEDDING_RETRY_AFTER_SECONDS,
            },
        )

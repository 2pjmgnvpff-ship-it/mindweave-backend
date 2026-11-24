"""Domain/application error hierarchy and RFC 7807 Problem Details handlers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base structured error with HTTP status and RFC 7807 type slug (zero PII!)."""

    status_code: int = 500
    error_code: str = "internal_error"
    title: str = "Internal Server Error"

    def __init__(
        self,
        detail: str,
        *,
        error_code: str | None = None,
        status_code: int | None = None,
        title: str | None = None,
        extensions: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        if error_code is not None:
            self.error_code = error_code
        if status_code is not None:
            self.status_code = status_code
        if title is not None:
            self.title = title
        self.extensions: dict[str, Any] = extensions or {}
        self.headers: dict[str, str] | None = dict(headers) if headers else None


class ValidationFailedError(AppError):
    status_code = 422
    error_code = "validation_failed"
    title = "Validation Failed"


class BadRequestError(AppError):
    status_code = 400
    error_code = "bad_request"
    title = "Bad Request"


class UnauthorizedError(AppError):
    status_code = 401
    error_code = "unauthorized"
    title = "Unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    error_code = "forbidden"
    title = "Forbidden"


class ReauthRequiredError(AppError):
    status_code = 403
    error_code = "reauth_required"
    title = "Step-Up Re-Authentication Required"


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"
    title = "Resource Not Found"


class ConflictError(AppError):
    status_code = 409
    error_code = "conflict"
    title = "Conflict"


class VersionMismatchError(AppError):
    status_code = 412
    error_code = "version_mismatch"
    title = "Precondition Failed (Optimistic Lock Version Mismatch)"


class IdempotencyConflictError(AppError):
    status_code = 409
    error_code = "idempotency_conflict"
    title = "Idempotency Key Reused With Different Payload"


class PayloadTooLargeError(AppError):
    status_code = 413
    error_code = "payload_too_large"
    title = "Payload Too Large"


class RateLimitedError(AppError):
    status_code = 429
    error_code = "rate_limited"
    title = "Too Many Requests"


class QuotaExceededError(AppError):
    status_code = 429
    error_code = "quota_exceeded"
    title = "Daily Quota Exceeded"


class UpstreamAIError(AppError):
    status_code = 502
    error_code = "ai_upstream_error"
    title = "AI Provider Unavailable"


class EmailDeliveryError(AppError):
    status_code = 502
    error_code = "email_delivery_failed"
    title = "Email Delivery Failed"


def problem_response(
    *,
    status_code: int,
    error_code: str,
    title: str,
    detail: str,
    instance: str,
    request_id: str | None = None,
    extensions: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "type": f"https://mindweave.local/problems/{error_code}",
        "title": title,
        "status": status_code,
        "code": error_code,
        "detail": detail,
        "instance": instance,
    }
    if request_id:
        payload["requestId"] = request_id
    if extensions:
        payload.update(extensions)
    return JSONResponse(
        status_code=status_code,
        content=payload,
        media_type="application/problem+json",
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        req_id = getattr(request.state, "request_id", None)
        return problem_response(
            status_code=exc.status_code,
            error_code=exc.error_code,
            title=exc.title,
            detail=exc.detail,
            instance=str(request.url.path),
            request_id=req_id,
            extensions=exc.extensions,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        req_id = getattr(request.state, "request_id", None)
        # Never echo raw user input values in validation errors (Invariant I10)!
        sanitized_errors = [
            {
                "loc": [str(part) for part in err.get("loc", ())],
                "msg": str(err.get("msg", "Invalid value")),
                "type": str(err.get("type", "value_error")),
            }
            for err in exc.errors()
        ]
        return problem_response(
            status_code=422,
            error_code="request_validation_failed",
            title="Request Validation Failed",
            detail="One or more request parameters failed validation.",
            instance=str(request.url.path),
            request_id=req_id,
            extensions={"errors": sanitized_errors},
        )

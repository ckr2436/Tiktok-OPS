# app/core/errors.py
from __future__ import annotations
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse

class APIError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = data

def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error_handler(_: Request, exc: APIError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "data": exc.data}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError):
        for error in exc.errors():
            if error.get("type") == "policy_invalid_mode":
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "code": "POLICY_INVALID_MODE",
                            "message": "Invalid policy mode. Allowed values: WHITELIST, BLACKLIST.",
                            "data": error.get("ctx"),
                        }
                    },
                )
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "Internal server error."}},
        )


# app/core/errors.py
from __future__ import annotations
from fastapi import FastAPI, Request
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

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "Internal server error."}},
        )


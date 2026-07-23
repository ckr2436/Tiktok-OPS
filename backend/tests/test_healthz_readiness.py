from __future__ import annotations

from contextlib import nullcontext
from importlib import import_module

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError


healthz_router = import_module("app.features.healthz.router")


class _Connection:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.statements: list[str] = []

    def execute(self, statement):
        self.statements.append(str(statement))
        if self.error:
            raise self.error


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def connect(self):
        return nullcontext(self.connection)


def test_healthz_checks_required_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(healthz_router, "engine", _Engine(connection))

    assert healthz_router.healthz() == {"ok": True}
    assert connection.statements == [
        "SELECT 1",
        "SELECT 1 FROM `users` LIMIT 1",
        "SELECT 1 FROM `workspaces` LIMIT 1",
        "SELECT 1 FROM `oauth_accounts_ttb` LIMIT 1",
    ]


def test_healthz_returns_503_when_database_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = OperationalError("SELECT 1", {}, RuntimeError("database unavailable"))
    monkeypatch.setattr(healthz_router, "engine", _Engine(_Connection(error)))

    with pytest.raises(HTTPException) as caught:
        healthz_router.healthz()

    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "SERVICE_NOT_READY"

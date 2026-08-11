from __future__ import annotations

"""Celery task package.

Task registration is centralized in :mod:`app.celery_app`. Importing a task
submodule must not eagerly import its siblings because that creates circular
imports for workers, management commands, and isolated tests.
"""

"""TikTok Business provider stub implementation."""

from __future__ import annotations

from typing import Mapping

from app.services.providers.base import Provider, registry


class TiktokBusinessProvider(Provider):
    _KEY = "tiktok-business"

    def key(self) -> str:
        return self._KEY

    def display_name(self) -> str:
        return "TikTok Business"

    def capabilities(self) -> Mapping[str, object]:
        return {"supports_policies": True}


registry.register(TiktokBusinessProvider())


__all__ = ["TiktokBusinessProvider"]

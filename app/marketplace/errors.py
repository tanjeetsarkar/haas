"""Marketplace error types shared by the store and the API layer."""

from __future__ import annotations

from typing import Any


class MarketplaceError(Exception):
    status_code = 400

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_detail(self) -> Any:
        if self.details is None:
            return self.message
        return {"message": self.message, **({"details": self.details})}


class MarketplaceInvalid(MarketplaceError):
    status_code = 422


class MarketplaceForbidden(MarketplaceError):
    status_code = 403


class MarketplaceNotFound(MarketplaceError):
    status_code = 404


class MarketplaceConflict(MarketplaceError):
    status_code = 409

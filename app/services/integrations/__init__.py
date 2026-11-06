"""Provider-neutral integration contracts; provider transport stays disabled until verified."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import NoReturn, Protocol, Sequence

from ...ui_i18n import ui


class IntegrationNotReady(RuntimeError):
    """Raised when a provider contract or its server-side credentials are unavailable."""


class SecretResolver(Protocol):
    """Resolves opaque references from a server-side secret manager."""

    def resolve(self, reference: str) -> str:
        """Return the secret without persisting or exposing it to clients."""


@dataclass(frozen=True)
class ExternalReservation:
    external_booking_id: str
    external_location_id: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class AvailabilityUpdate:
    external_location_id: str
    starts_at: datetime
    ends_at: datetime
    available: bool


@dataclass(frozen=True)
class PricingUpdate:
    external_location_id: str
    starts_at: datetime
    ends_at: datetime
    amount: Decimal
    currency: str


class ProviderAdapter(Protocol):
    """Shared import/export contract implemented by each external platform adapter."""

    provider: str

    def import_reservations(self, credential: str) -> Sequence[ExternalReservation]:
        """Fetch provider reservations for later idempotent import."""
        ...

    def sync_availability(self, credential: str, updates: Sequence[AvailabilityUpdate]) -> None:
        """Publish availability updates to a provider."""
        ...

    def sync_pricing(self, credential: str, updates: Sequence[PricingUpdate]) -> None:
        """Publish pricing updates to a provider."""
        ...


class _PendingProviderAdapter:
    """Explicitly disabled adapter; no endpoints or authentication are guessed."""

    def __init__(self, provider: str):
        self.provider = provider

    def _pending(self) -> NoReturn:
        raise IntegrationNotReady(
            ui(
                "%(provider)s synchronization is disabled until its official API contract and access are verified.",
                provider=self.provider,
            )
        )

    def import_reservations(self, credential: str) -> Sequence[ExternalReservation]:
        self._pending()

    def sync_availability(self, credential: str, updates: Sequence[AvailabilityUpdate]) -> None:
        self._pending()

    def sync_pricing(self, credential: str, updates: Sequence[PricingUpdate]) -> None:
        self._pending()


class ParkosAdapter(_PendingProviderAdapter):
    def __init__(self):
        super().__init__("Parkos")


class ParkenlistAdapter(_PendingProviderAdapter):
    def __init__(self):
        super().__init__("Parkenlist")


def adapter_for(provider: str) -> ProviderAdapter:
    """Return a known adapter without implying that network synchronization is configured."""
    adapters = {
        "parkos": ParkosAdapter,
        "parkenlist": ParkenlistAdapter,
    }
    try:
        return adapters[provider]()
    except KeyError as error:
        raise ValueError(ui("Unsupported external integration provider: %(provider)s", provider=provider)) from error


def resolve_secret(reference: str, resolver: SecretResolver) -> str:
    """Resolve credentials only at the server boundary; callers must not log or return the result."""
    if not reference:
        raise IntegrationNotReady(ui("No server-side credential reference is configured."))
    secret = resolver.resolve(reference)
    if not secret:
        raise IntegrationNotReady(ui("The server-side credential reference could not be resolved."))
    return secret

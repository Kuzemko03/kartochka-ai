"""
Payment provider abstraction.

A real PSP (ЮKassa / Robokassa / CloudPayments) is plugged in by implementing
``PaymentProvider`` and registering it — the rest of the system (service.py,
router.py, checkout.html) talks only to this interface, so swapping the mock for
a live provider is a one-file change plus credentials.

For now the active provider is ``MockProvider`` (sandbox): it issues a
confirmation URL that points back to our own hosted "test payment page", and the
purchase is settled when the frontend hits the confirm endpoint. The DB-side flow
(pending → paid, idempotent token credit, receipt record) is identical to what a
real webhook-driven flow will use, so nothing downstream changes later.

54-ФЗ note: a real provider builds and sends the fiscal receipt (чек) from the
``receipt`` payload we hand it. We persist the receipt email + line items +
amount; the mock simply marks ``receipt_sent`` so the UX is complete.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class PaymentProvider(ABC):
    name: str = "abstract"
    #: True if settlement happens synchronously via our confirm endpoint (mock),
    #: False if it arrives asynchronously via a provider webhook (real PSPs).
    is_mock: bool = False

    @abstractmethod
    def create_payment(
        self,
        *,
        purchase_id: str,
        amount_kopecks: int,
        description: str,
        receipt_email: str,
        return_url: str,
        receipt_items: list[dict],
    ) -> dict:
        """Create a payment on the provider side.

        Returns ``{"provider_payment_id": str, "confirmation_url": str}``.
        ``confirmation_url`` is where the browser is redirected to pay.
        """
        raise NotImplementedError


class MockProvider(PaymentProvider):
    """Sandbox provider: no network, settled via our own confirm endpoint."""

    name = "mock"
    is_mock = True

    def create_payment(self, *, purchase_id, amount_kopecks, description,
                       receipt_email, return_url, receipt_items) -> dict:
        # The "hosted payment page" is our own /pay route which renders a
        # realistic test checkout and calls the confirm endpoint on success.
        return {
            "provider_payment_id": f"mock_{purchase_id}",
            "confirmation_url": f"/pay?purchase_id={purchase_id}",
        }


# ── Registry ─────────────────────────────────────────────────────────────────
_PROVIDERS: dict[str, PaymentProvider] = {}
_ACTIVE: Optional[str] = None


def register_provider(provider: PaymentProvider, *, active: bool = False) -> None:
    global _ACTIVE
    _PROVIDERS[provider.name] = provider
    if active or _ACTIVE is None:
        _ACTIVE = provider.name


def get_provider(name: Optional[str] = None) -> PaymentProvider:
    if name:
        return _PROVIDERS[name]
    assert _ACTIVE is not None
    return _PROVIDERS[_ACTIVE]


# Default: mock provider is active until real credentials are wired in.
register_provider(MockProvider(), active=True)

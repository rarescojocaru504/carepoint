"""Payment provider interface and the MVP's mock implementation (ADR-015).

Lifecycle of the money for one reservation:

    checkout  -> authorize  (funds reserved, nothing taken)
    dispensed -> capture    (funds taken)
    expired / cancelled -> void (reservation released)

A real provider (e.g. Stripe PaymentIntents with manual capture) slots in
behind `PaymentProvider` without touching checkout or settlement logic.
"""

import uuid
from dataclasses import dataclass
from typing import Protocol


class PaymentError(Exception):
    """Base class for provider failures."""


class PaymentDeclinedError(PaymentError):
    """The customer's payment method was refused. Safe to show; retryable with another."""


class PaymentProviderUnavailableError(PaymentError):
    """The provider could not be reached or failed. Retry later."""


@dataclass(frozen=True)
class Authorization:
    reference: str


class PaymentProvider(Protocol):
    async def authorize(
        self, *, amount_cents: int, currency: str, payment_method: str, idempotency_key: str
    ) -> Authorization:
        """Reserve funds. Same idempotency_key -> same authorization."""
        ...

    async def capture(self, reference: str) -> None:
        """Take the authorized funds. Must be idempotent."""
        ...

    async def void(self, reference: str) -> None:
        """Release the authorized funds. Must be idempotent."""
        ...


# Test payment-method tokens, in the style of real providers' test cards.
MOCK_OK = "pm_card_ok"
MOCK_DECLINED = "pm_card_declined"
MOCK_PROVIDER_ERROR = "pm_card_provider_error"


class MockPaymentProvider:
    """Deterministic stand-in for a real provider. No money moves.

    Behaviour is chosen by the payment-method token (see MOCK_*), so demos and
    tests can show a decline or an outage on purpose. References are derived
    from the idempotency key, so retried authorizations are idempotent, as with
    a real provider. `fail_next_settlements` lets tests simulate a provider
    outage during capture/void.
    """

    def __init__(self) -> None:
        self.fail_next_settlements = 0
        self.captured: set[str] = set()
        self.voided: set[str] = set()

    async def authorize(
        self, *, amount_cents: int, currency: str, payment_method: str, idempotency_key: str
    ) -> Authorization:
        if payment_method == MOCK_DECLINED:
            raise PaymentDeclinedError("card declined")
        if payment_method == MOCK_PROVIDER_ERROR:
            raise PaymentProviderUnavailableError("payment provider unavailable")
        if payment_method != MOCK_OK:
            raise PaymentDeclinedError("unsupported payment method")
        return Authorization(f"mock_{uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key).hex}")

    async def capture(self, reference: str) -> None:
        self._maybe_fail()
        if reference in self.voided:
            raise PaymentError(f"{reference} was voided")
        self.captured.add(reference)

    async def void(self, reference: str) -> None:
        self._maybe_fail()
        if reference in self.captured:
            raise PaymentError(f"{reference} was captured")
        self.voided.add(reference)

    def _maybe_fail(self) -> None:
        if self.fail_next_settlements > 0:
            self.fail_next_settlements -= 1
            raise PaymentProviderUnavailableError("payment provider unavailable")


_provider: PaymentProvider = MockPaymentProvider()


def get_payment_provider() -> PaymentProvider:
    """FastAPI dependency and service-level accessor (tests override it)."""
    return _provider

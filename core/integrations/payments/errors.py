"""Failure modes every payment provider reports the same way.

Messages name the call, the status and the provider's error codes — never the
URL, the request, or the provider's description, which can quote the payer's
CPF/CNPJ. Connectors raise these `from None`, so the httpx error (which carries
the URL and headers) never reaches a traceback.
"""
from __future__ import annotations


class PaymentsError(RuntimeError):
    def __init__(self, message: str = "", status: int | None = None,
                 code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class PaymentsNotConfigured(PaymentsError):
    """No key: payments are off."""


class PaymentsAuthError(PaymentsError):
    """The provider refused the key (wrong, revoked, other environment)."""


class PaymentsRejected(PaymentsError):
    """The provider refused the request itself (an invalid CPF, a past date)."""

    def __init__(self, message: str = "", status: int | None = None,
                 codes: list[str] | None = None):
        codes = codes or []
        super().__init__(message, status, codes[0] if codes else None)
        self.codes = codes


class PaymentsTransient(PaymentsError):
    """Worth trying again later: the network, a timeout, the provider's 5xx or 429."""

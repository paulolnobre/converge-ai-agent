"""Failure modes every workspace connector reports the same way.

Callers decide what to do about a missing integration without knowing whether
they are talking to Google or to Microsoft Graph.
"""


class WorkspaceError(RuntimeError):
    """Any failure talking to Google Workspace or Microsoft 365.

    The message names the method, the host and the status — never the path or
    the query, which carry e-mail addresses and calendar ids — and the
    connectors raise it `from None`, so the original httpx error (which does
    carry the URL) never reaches a traceback in the logs.
    """

    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status


class WorkspaceNotConfigured(WorkspaceError):
    """The credentials for the selected provider are absent or incomplete.

    Raised at call time rather than at import: a deployment that only uses the
    calendar must not fail to start because no mailbox was configured.
    """


class WorkspaceAuthError(WorkspaceError):
    """The provider refused the credentials (expired consent, revoked token)."""


class WorkspaceTransient(WorkspaceError):
    """A failure worth trying again later: the network, the provider's own
    trouble (5xx) or a quota. Never a reason to believe something is gone."""


class WorkspaceRateLimited(WorkspaceTransient):
    """The provider asked us to back off (HTTP 429 / 503, or Google's 403
    rateLimitExceeded / userRateLimitExceeded)."""

    def __init__(self, message: str, retry_after: float | None = None,
                 status: int | None = None):
        super().__init__(message, status=status)
        self.retry_after = retry_after


def audit_detail(exc: Exception) -> str:
    """What an audit row records about a failure: the kind and the status,
    never the message (an exception's text can hold a file name or a URL)."""
    status = getattr(exc, "status", None)
    return f"{type(exc).__name__} HTTP {status}" if status else type(exc).__name__

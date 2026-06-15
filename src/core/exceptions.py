"""
Custom exception hierarchy for TRON-X.
"""


class TronXError(Exception):
    """Base exception for all TRON-X errors."""
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AllProvidersExhaustedError(TronXError):
    """Raised when every model in the failover chain has failed."""


class ProviderError(TronXError):
    """Raised when a specific provider returns an error."""
    def __init__(self, provider: str, model: str, message: str, status_code: int = 500):
        super().__init__(message, {"provider": provider, "model": model})
        self.provider = provider
        self.model = model
        self.status_code = status_code


class RateLimitError(ProviderError):
    """Raised when a provider rate limit is hit."""


class ConfigurationError(TronXError):
    """Raised when required configuration is missing or invalid."""


class MemoryError(TronXError):
    """Raised when the RAG / vector DB pipeline encounters an error."""


class IntentClassificationError(TronXError):
    """Raised when intent classification fails."""


class SessionNotFoundError(TronXError):
    """Raised when a requested chat session doesn't exist."""

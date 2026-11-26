"""Domain errors safe to display to the caller."""


class ConfigurationError(Exception):
    """Configuration is invalid or unavailable."""


class CredentialError(Exception):
    """Secure credential storage is unavailable or the operation failed."""


class InputValidationError(Exception):
    """Message fields or input files cannot be used to build a message."""


class TransportError(Exception):
    """A Bridge protocol operation did not complete successfully."""


class BridgeConnectionError(TransportError):
    """The Bridge connection could not be opened or maintained."""


class BridgeTLSError(TransportError):
    """A verified TLS session with Bridge could not be established."""


class BridgeAuthenticationError(TransportError):
    """Bridge authentication failed or was unavailable."""


class BridgeRejectionError(TransportError):
    """Bridge explicitly rejected a command, sender, recipient, or message."""


class UncertainOutcomeError(TransportError):
    """The connection failed after submission began; acceptance is unknown."""

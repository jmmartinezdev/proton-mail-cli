"""TLS trust shared by the Bridge protocol adapters."""

import ssl
from pathlib import Path

from .errors import BridgeTLSError


def bridge_tls_context(certificate: Path) -> ssl.SSLContext:
    """Trust the configured Bridge certificate and verify the peer hostname."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        context.load_verify_locations(cafile=certificate)
    except (OSError, ValueError):
        raise BridgeTLSError("Could not load the trusted Bridge certificate.") from None
    return context

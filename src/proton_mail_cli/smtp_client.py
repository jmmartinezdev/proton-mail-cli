"""Submit a built message to Proton Mail Bridge over verified SMTP."""

import smtplib
import ssl
from contextlib import suppress
from email.message import EmailMessage

from .addresses import is_mailbox
from .errors import (
    BridgeAuthenticationError,
    BridgeConnectionError,
    BridgeRejectionError,
    BridgeTLSError,
    InputValidationError,
    UncertainOutcomeError,
)
from .models import BridgeConfig, OperationResult
from .tls import bridge_tls_context


def _envelope_address(message: EmailMessage, field: str) -> str:
    headers = message.get_all(field, [])
    if len(headers) == 1:
        addresses = getattr(headers[0], "addresses", ())
        if len(addresses) == 1 and is_mailbox(addresses[0].addr_spec):
            return addresses[0].addr_spec
    raise InputValidationError(f"SMTP submission requires one {field} mailbox.")


def _require_ehlo(client: smtplib.SMTP) -> None:
    if client.ehlo()[0] != 250:
        raise BridgeRejectionError("Bridge rejected SMTP EHLO.")


def _start_tls(client: smtplib.SMTP, context: ssl.SSLContext) -> None:
    _require_ehlo(client)
    try:
        code, _ = client.starttls(context=context)
    except (smtplib.SMTPNotSupportedError, smtplib.SMTPResponseException):
        raise BridgeTLSError("Bridge did not accept SMTP STARTTLS.") from None
    if code != 220:
        raise BridgeTLSError("Bridge did not accept SMTP STARTTLS.")


def _authenticate(client: smtplib.SMTP, username: str, password: str) -> None:
    try:
        client.login(username, password)
    except smtplib.SMTPServerDisconnected:
        raise
    except (smtplib.SMTPException, UnicodeError):
        raise BridgeAuthenticationError(
            "SMTP authentication failed. Check the Bridge-issued credentials and settings."
        ) from None


def _submit(
    client: smtplib.SMTP, message: EmailMessage, sender: str, recipient: str
) -> None:
    try:
        refused = client.send_message(message, from_addr=sender, to_addrs=[recipient])
    except smtplib.SMTPRecipientsRefused:
        raise BridgeRejectionError("Bridge refused the SMTP recipient.") from None
    except smtplib.SMTPServerDisconnected:
        # SMTPException inherits from OSError; handle this before other SMTP errors.
        raise _uncertain_outcome() from None
    except smtplib.SMTPException:
        raise BridgeRejectionError("Bridge rejected the SMTP submission.") from None
    except OSError:
        raise _uncertain_outcome() from None
    if refused:
        raise BridgeRejectionError("Bridge refused the SMTP recipient.")


def _uncertain_outcome() -> UncertainOutcomeError:
    return UncertainOutcomeError(
        "SMTP submission outcome is uncertain. Check Proton Mail Sent and verify "
        "delivery before retrying."
    )


def send_message(
    message: EmailMessage, config: BridgeConfig, password: str
) -> OperationResult:
    """Submit once and report Bridge acceptance, not delivery."""
    sender = _envelope_address(message, "From")
    recipient = _envelope_address(message, "To")
    if config.security not in ("starttls", "tls"):
        raise BridgeTLSError("SMTP requires STARTTLS or implicit TLS.")
    context = bridge_tls_context(config.certificate)
    client: smtplib.SMTP | None = None
    try:
        # Connect separately so a failure during the greeting still gets cleanup.
        if config.security == "tls":
            client = smtplib.SMTP_SSL(timeout=config.timeout, context=context)
        else:
            client = smtplib.SMTP(timeout=config.timeout)
        if client.connect(config.host, config.smtp_port)[0] != 220:
            raise BridgeConnectionError("Bridge did not accept the SMTP connection.")
        if config.security == "starttls":
            _start_tls(client, context)
        _require_ehlo(client)
        _authenticate(client, config.username, password)
        _submit(client, message, sender, recipient)
    except ssl.SSLError:
        raise BridgeTLSError(
            "Could not establish verified SMTP TLS with Bridge."
        ) from None
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError):
        raise BridgeConnectionError(
            "Could not connect to or stay connected to Bridge SMTP."
        ) from None
    except smtplib.SMTPException:
        raise BridgeRejectionError("Bridge rejected an SMTP command.") from None
    except OSError:
        raise BridgeConnectionError(
            "Could not connect to or stay connected to Bridge SMTP."
        ) from None
    finally:
        if client is not None:
            with suppress(OSError):
                client.close()
    return OperationResult(operation="send", summary="Bridge accepted the message.")

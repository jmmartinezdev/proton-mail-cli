"""Append a built message to Proton Mail Bridge as an editable draft."""

import imaplib
import re
import ssl
from contextlib import suppress
from email.message import EmailMessage
from email.policy import SMTP

from .errors import (
    BridgeAuthenticationError,
    BridgeConnectionError,
    BridgeRejectionError,
    BridgeTLSError,
    UncertainOutcomeError,
)
from .models import BridgeConfig, OperationResult
from .tls import bridge_tls_context

# Plain LIST responses, without LIST-EXTENDED return data.
_LIST_ENTRY = re.compile(
    rb'\(([^()]*)\) (?:NIL|"(?:[^"\\]|\\["\\])*") (.+)', re.IGNORECASE
)
_QUOTED = re.compile(rb'"((?:[^"\\]|\\["\\])*)"')
_ASTRING = re.compile(rb'[^(){ %*"\\\x00-\x1f\x7f]+')
_LITERAL = re.compile(rb"\{([0-9]+)\}")


def _quote(value: bytes) -> bytes:
    return b'"' + value.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def _parse_list_entry(entry: bytes | tuple[bytes, bytes]) -> tuple[list[bytes], bytes]:
    line = entry[0] if isinstance(entry, tuple) else entry
    match = _LIST_ENTRY.fullmatch(line)
    if match:
        flags, name = match.groups()
        if isinstance(entry, tuple):
            marker = _LITERAL.fullmatch(name)
            name = entry[1]
            # imaplib supplies the literal bytes separately from its length marker.
            if marker and (marker[1].lstrip(b"0") or b"0") == str(len(name)).encode():
                return flags.lower().split(), name
        elif quoted := _QUOTED.fullmatch(name):
            return flags.lower().split(), re.sub(rb'\\(["\\])', rb"\1", quoted[1])
        elif _ASTRING.fullmatch(name):
            return flags.lower().split(), name
    raise BridgeRejectionError("Could not interpret the Bridge IMAP LIST response.")


def _find_drafts(client: imaplib.IMAP4, override: str | None) -> bytes:
    status, entries = client.list()
    if status != "OK":
        raise BridgeRejectionError("Bridge rejected IMAP LIST.")

    selectable = set()
    drafts = set()
    for entry in entries:
        # imaplib can return None for no matches and an empty trailer after a literal.
        if entry in (None, b""):
            continue
        flags, name = _parse_list_entry(entry)
        if rb"\noselect" in flags or rb"\nonexistent" in flags:
            continue
        selectable.add(name)
        if rb"\drafts" in flags:
            drafts.add(name)

    if override is not None:
        try:
            name = override.encode("utf-8")
        except UnicodeEncodeError:
            raise BridgeRejectionError(
                "The configured Drafts mailbox name is not valid Unicode."
            ) from None
        if name not in selectable:
            raise BridgeRejectionError(
                "The configured Drafts mailbox was not found as a selectable IMAP mailbox."
            )
    elif not drafts:
        raise BridgeRejectionError(
            "No selectable IMAP mailbox is tagged as Drafts. Verify the Bridge Drafts mailbox."
        )
    elif len(drafts) > 1:
        raise BridgeRejectionError(
            "Multiple selectable IMAP mailboxes are tagged as Drafts. Configure an exact override."
        )
    else:
        (name,) = drafts

    if not name or any(char < 32 or char == 127 for char in name):
        raise BridgeRejectionError("The Drafts mailbox name cannot be safely quoted.")
    return _quote(name)


def _start_tls(client: imaplib.IMAP4, context: ssl.SSLContext) -> None:
    if "STARTTLS" not in client.capabilities:
        raise BridgeTLSError("Bridge does not advertise IMAP STARTTLS.")
    try:
        status, _ = client.starttls(ssl_context=context)
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error:
        raise BridgeTLSError("Bridge did not accept IMAP STARTTLS.") from None
    if status != "OK":
        raise BridgeTLSError("Bridge did not accept IMAP STARTTLS.")


def _authenticate(client: imaplib.IMAP4, username: str, password: str) -> None:
    # Match the Bridge compatibility probe and avoid unsafe LOGIN arguments.
    if not all(
        value.isascii() and value.isprintable() for value in (username, password)
    ):
        raise BridgeAuthenticationError(
            "IMAP requires printable ASCII Bridge credentials."
        )
    try:
        status, _ = client.login(_quote(username.encode("ascii")), password)
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error:
        status = "NO"
    if status != "OK":
        raise BridgeAuthenticationError(
            "IMAP authentication failed. Check the Bridge-issued credentials and settings."
        ) from None


def _append(client: imaplib.IMAP4, mailbox: bytes, payload: bytes) -> None:
    try:
        status, _ = client.append(mailbox, r"(\Draft)", None, payload)
    except (imaplib.IMAP4.abort, OSError):
        raise UncertainOutcomeError(
            "IMAP APPEND outcome is uncertain. Check Proton Mail web Drafts before retrying."
        ) from None
    except imaplib.IMAP4.error:
        raise BridgeRejectionError("Bridge rejected IMAP APPEND.") from None
    if status != "OK":
        raise BridgeRejectionError("Bridge rejected IMAP APPEND.")


def _cleanup(client: imaplib.IMAP4, *, usable: bool) -> None:
    if usable:
        try:
            if client.logout()[0] in ("BYE", "OK"):
                return
        except (imaplib.IMAP4.error, OSError, ValueError):
            pass
    # After losing the command stream, close without sending another command:
    # Bridge may still be waiting for the rest of an APPEND literal.
    try:
        client.shutdown()
    except (OSError, ValueError):
        with suppress(OSError, ValueError):
            client.socket().close()


def save_draft(
    message: EmailMessage, config: BridgeConfig, password: str
) -> OperationResult:
    """Append one draft and report Bridge acceptance, not cloud synchronization."""
    if config.security not in ("starttls", "tls"):
        raise BridgeTLSError("IMAP requires STARTTLS or implicit TLS.")
    context = bridge_tls_context(config.certificate)
    payload = message.as_bytes(policy=SMTP)

    client: imaplib.IMAP4 | None = None
    usable = True
    try:
        if config.security == "tls":
            client = imaplib.IMAP4_SSL(
                config.host,
                config.imap_port,
                ssl_context=context,
                timeout=config.timeout,
            )
        else:
            client = imaplib.IMAP4(
                config.host, config.imap_port, timeout=config.timeout
            )
            _start_tls(client, context)
        _authenticate(client, config.username, password)
        mailbox = _find_drafts(client, config.drafts_mailbox)
        _append(client, mailbox, payload)
    except UncertainOutcomeError:
        usable = False
        raise
    except ssl.SSLError:
        usable = False
        raise BridgeTLSError(
            "Could not establish verified IMAP TLS with Bridge."
        ) from None
    except (imaplib.IMAP4.abort, OSError):
        usable = False
        raise BridgeConnectionError(
            "Could not connect to or stay connected to Bridge IMAP."
        ) from None
    except imaplib.IMAP4.error:
        if client is None:
            raise BridgeConnectionError(
                "Bridge did not accept the IMAP connection."
            ) from None
        raise BridgeRejectionError("Bridge rejected an IMAP command.") from None
    finally:
        if client is not None:
            _cleanup(client, usable=usable)
    return OperationResult(operation="draft", summary="Bridge accepted the draft.")

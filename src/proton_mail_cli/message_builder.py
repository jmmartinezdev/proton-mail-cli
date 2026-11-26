"""Build MIME messages from validated local inputs without using Bridge."""

import mimetypes
import stat
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from pathlib import Path

from .addresses import is_mailbox
from .errors import InputValidationError
from .models import MessageRequest


def _validate_header(value: str, field: str) -> None:
    if (value and value.splitlines() != [value]) or "\0" in value:
        raise InputValidationError(f"{field} must be a single line.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise InputValidationError(f"{field} must be valid Unicode.") from None


def _file_size(path: Path, role: str) -> int:
    try:
        info = path.stat()
    except FileNotFoundError:
        raise InputValidationError(f"{role} file does not exist.") from None
    except (OSError, ValueError):
        raise InputValidationError(
            f"Could not inspect the {role.lower()} file."
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise InputValidationError(f"{role} must be a regular file.")
    return info.st_size


def _read_file(path: Path, role: str, byte_limit: int) -> bytes:
    try:
        with path.open("rb") as file:
            data = file.read(byte_limit + 1)
    except (OSError, ValueError):
        raise InputValidationError(f"Could not read the {role.lower()} file.") from None
    if len(data) > byte_limit:
        raise InputValidationError(f"{role} file grew after the size check.")
    return data


def _read_body(path: Path, role: str, size: int) -> str:
    try:
        return _read_file(path, role, size).decode("utf-8")
    except UnicodeDecodeError:
        raise InputValidationError(f"{role} file must be valid UTF-8.") from None


def _base64_wire_size(size: int) -> int:
    characters = 4 * ((size + 2) // 3)
    lines = (characters + 75) // 76
    return characters + 2 * lines  # SMTP uses CRLF after each base64 line.


def _check_size(size: int, limit: int) -> None:
    if size > limit:
        raise InputValidationError(f"Message exceeds the {limit}-byte size limit.")


def build_message(request: MessageRequest) -> EmailMessage:
    """Validate a request and construct a message within its SMTP MIME size limit."""
    message = EmailMessage(policy=SMTP)
    for field, address in (("From", request.sender), ("To", request.recipient)):
        if not is_mailbox(address):
            raise InputValidationError(f"{field} must be a single bare ASCII mailbox.")
        message[field] = address
        # Do not accept an address that the header parser rewrites (e.g. an
        # encoded word in the local part).
        parsed = message[field].addresses
        if len(parsed) != 1 or parsed[0].addr_spec != address:
            raise InputValidationError(f"{field} must be a single bare ASCII mailbox.")
    _validate_header(request.subject, "Subject")
    if not any(
        request.sender.casefold() == sender.casefold()
        for sender in request.allowed_senders
    ):
        raise InputValidationError("From is not a configured sender.")

    limit = request.max_message_bytes
    if type(limit) is not int or limit <= 0:
        raise InputValidationError("Message size limit must be a positive byte count.")
    extension = request.body.suffix.lower()
    if extension not in (".txt", ".html"):
        raise InputValidationError("Body file must have a .txt or .html extension.")
    plain_body = request.plain_body
    if plain_body is not None:
        if extension != ".html":
            raise InputValidationError(
                "A plain-text alternative requires an HTML body."
            )
        if plain_body.suffix.lower() != ".txt":
            raise InputValidationError(
                "Plain-text alternative must have a .txt extension."
            )

    body_size = _file_size(request.body, "Body")
    plain_size = (
        _file_size(plain_body, "Plain-text alternative")
        if plain_body is not None
        else 0
    )
    attachments = [
        (path, _file_size(path, f"Attachment {index}"))
        for index, path in enumerate(request.attachments, start=1)
    ]
    encoded_size = sum(_base64_wire_size(size) for _, size in attachments)
    _check_size(body_size + plain_size + encoded_size, limit)

    message["Subject"] = request.subject
    message["Date"] = formatdate(usegmt=True)
    # Supplying a domain prevents make_msgid from looking up the local hostname.
    message["Message-ID"] = make_msgid(domain=request.sender.rsplit("@", 1)[1])

    body = _read_body(request.body, "Body", body_size)
    if plain_body is not None:
        message.set_content(
            _read_body(plain_body, "Plain-text alternative", plain_size)
        )
        message.add_alternative(body, subtype="html")
    else:
        message.set_content(body, subtype="html" if extension == ".html" else "plain")

    # Empty parts account for the actual MIME headers, filenames, and boundaries
    # before any attachment content is read.
    for index, (path, _) in enumerate(attachments, start=1):
        _validate_header(path.name, f"Attachment {index} filename")
        mime_type, _ = mimetypes.guess_type(path.name)
        # Container MIME types cannot be represented as opaque base64 parts.
        if mime_type is None or mime_type.startswith(("message/", "multipart/")):
            mime_type = "application/octet-stream"
        main, sub = mime_type.split("/", 1)
        message.add_attachment(b"", main, sub, filename=path.name, cte="base64")
    _check_size(len(message.as_bytes()) + encoded_size, limit)

    for index, ((path, size), part) in enumerate(
        zip(attachments, message.iter_attachments(), strict=True), start=1
    ):
        main, sub = part.get_content_type().split("/", 1)
        data = _read_file(path, f"Attachment {index}", size)
        part.set_content(data, main, sub, filename=path.name, cte="base64")
    _check_size(len(message.as_bytes()), limit)
    return message

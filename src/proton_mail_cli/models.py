"""Data shared by the configuration, message, and transport layers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DEFAULT_MAX_MESSAGE_BYTES = 20 * 1024 * 1024
MailOperation = Literal["send", "draft"]


@dataclass(frozen=True)
class BridgeConfig:
    host: str
    smtp_port: int
    imap_port: int
    username: str
    security: Literal["starttls", "tls"]
    timeout: float
    certificate: Path
    allowed_senders: tuple[str, ...]
    drafts_mailbox: str | None = None


@dataclass(frozen=True)
class MessageRequest:
    sender: str
    recipient: str
    subject: str
    body: Path
    allowed_senders: tuple[str, ...]
    attachments: tuple[Path, ...] = ()
    plain_body: Path | None = None
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES


@dataclass(frozen=True)
class OperationResult:
    """A mail operation accepted by Bridge."""

    operation: MailOperation
    summary: str


@dataclass(frozen=True)
class DryRunResult:
    """Metadata about a built message that was not submitted."""

    operation: MailOperation
    message_size: int
    attachment_sizes: tuple[int, ...]

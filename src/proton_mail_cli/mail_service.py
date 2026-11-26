"""Build and optionally submit mail without depending on the CLI."""

from collections.abc import Callable
from email.policy import SMTP

from .imap_client import save_draft
from .message_builder import build_message
from .models import (
    BridgeConfig,
    DryRunResult,
    MailOperation,
    MessageRequest,
    OperationResult,
)
from .smtp_client import send_message


def process_message(
    operation: MailOperation,
    request: MessageRequest,
    config: BridgeConfig,
    *,
    dry_run: bool,
    password_provider: Callable[[str], str],
) -> OperationResult | DryRunResult:
    """Validate and build before accessing credentials or connecting to Bridge."""
    if operation == "send":
        submit = send_message
    elif operation == "draft":
        submit = save_draft
    else:
        raise ValueError("Unknown mail operation.")

    message = build_message(request)
    if dry_run:
        return DryRunResult(
            operation=operation,
            message_size=len(message.as_bytes(policy=SMTP)),
            attachment_sizes=tuple(
                len(part.get_payload(decode=True))
                for part in message.iter_attachments()
            ),
        )

    password = password_provider(config.username)
    return submit(message, config, password)

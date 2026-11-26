"""Command-line entry point for sending and drafting through Proton Mail Bridge."""

import argparse
import getpass
import sys
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from .config import config_path, load_config, staged_config, validate_config
from .credentials import get_password, store_password
from .errors import (
    BridgeAuthenticationError,
    BridgeConnectionError,
    BridgeRejectionError,
    BridgeTLSError,
    ConfigurationError,
    CredentialError,
    InputValidationError,
    UncertainOutcomeError,
)
from .mail_service import process_message
from .models import DryRunResult, MessageRequest

_EXIT_CODES = (
    (InputValidationError, 2),
    ((ConfigurationError, CredentialError), 3),
    ((BridgeConnectionError, BridgeTLSError), 4),
    (BridgeAuthenticationError, 5),
    (BridgeRejectionError, 6),
    (UncertainOutcomeError, 7),
)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's diagnostics can echo raw argument values (such as a subject).
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments; see --help.\n")


def _add_config_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="override the configuration file path",
    )


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="proton-mail", description="Send and draft email via Proton Mail Bridge."
    )
    parser.add_argument(
        "--config", type=Path, help="override the configuration file path"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="save local Bridge settings")
    _add_config_option(configure)
    for command, help_text in (
        ("send", "submit an email to Bridge"),
        ("draft", "save a draft without sending it"),
    ):
        mail = commands.add_parser(command, help=help_text)
        _add_config_option(mail)
        mail.add_argument(
            "--from", dest="sender", required=True, help="configured sender"
        )
        mail.add_argument("--to", dest="recipient", required=True, help="recipient")
        mail.add_argument("--subject", required=True, help="message subject")
        mail.add_argument(
            "--body", type=Path, required=True, help="UTF-8 .txt or .html file"
        )
        mail.add_argument(
            "--plain-body", type=Path, help="UTF-8 .txt alternative for HTML"
        )
        mail.add_argument(
            "--attach", type=Path, action="append", help="attach a file (repeatable)"
        )
        mail.add_argument(
            "--dry-run",
            action="store_true",
            help="validate and build without accessing credentials or submitting",
        )
    return parser


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{label}{suffix}: ").strip() or default


def _configure(path: Path) -> None:
    if path.is_dir():
        raise ConfigurationError("The configuration path points to a directory.")
    if (
        path.exists()
        and _prompt("Replace the existing configuration? (y/N)").lower() != "y"
    ):
        raise ConfigurationError("Configuration not changed.")

    host = _prompt("Bridge host", "127.0.0.1")
    try:
        smtp_port = int(_prompt("Bridge SMTP port", "1025"))
        imap_port = int(_prompt("Bridge IMAP port", "1143"))
    except ValueError:
        raise ConfigurationError("Bridge ports must be integers.") from None
    username = _prompt("Bridge username")
    security = _prompt("Bridge security (starttls/tls)", "starttls")
    try:
        timeout = float(_prompt("Timeout in seconds", "30"))
    except ValueError:
        raise ConfigurationError("Bridge timeout must be a number.") from None
    certificate = _prompt("Path to exported Bridge PEM certificate")
    mailbox = _prompt("Drafts mailbox override (leave blank for automatic discovery)")
    senders = _prompt("Allowed sender addresses (comma-separated)")
    bridge = {
        "host": host,
        "smtp_port": smtp_port,
        "imap_port": imap_port,
        "username": username,
        "security": security,
        "timeout": timeout,
        "certificate": certificate,
    }
    if mailbox:
        bridge["drafts_mailbox"] = mailbox
    config = validate_config(
        {
            "bridge": bridge,
            "mail": {"allowed_senders": [item.strip() for item in senders.split(",")]},
        }
    )

    # Stop rather than falling back to getpass input that may echo the password.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            password = getpass.getpass("Bridge password: ")
        except getpass.GetPassWarning:
            raise CredentialError(
                "Cannot read the password safely; use an interactive terminal."
            ) from None
    with staged_config(path, config):
        store_password(config.username, password)


def _configure_command(override: Path | None) -> int:
    try:
        _configure(config_path(override))
    except (EOFError, KeyboardInterrupt):
        print("\nConfiguration cancelled.", file=sys.stderr)
        return 3
    except (OSError, ValueError):
        raise ConfigurationError(
            "Could not access the configuration path or terminal."
        ) from None

    print("Bridge configuration saved. No mail was sent.")
    return 0


def _mail_command(args: argparse.Namespace) -> str:
    config = load_config(args.config)
    request = MessageRequest(
        sender=args.sender,
        recipient=args.recipient,
        subject=args.subject,
        body=args.body,
        allowed_senders=config.allowed_senders,
        plain_body=args.plain_body,
        attachments=tuple(args.attach or ()),
    )
    result = process_message(
        args.command,
        request,
        config,
        dry_run=args.dry_run,
        password_provider=get_password,
    )
    if isinstance(result, DryRunResult):
        sizes = ", ".join(f"{size} bytes" for size in result.attachment_sizes)
        attachments = f" ({sizes})" if sizes else ""
        return (
            f"Dry run ({result.operation}): {result.message_size} bytes serialized; "
            f"{len(result.attachment_sizes)} attachment(s){attachments}. Nothing submitted."
        )
    return result.summary


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "configure":
            return _configure_command(args.config)
        print(_mail_command(args))
    except KeyboardInterrupt:
        print(
            "Operation interrupted. If submission had started, verify before retrying.",
            file=sys.stderr,
        )
        return 1
    except Exception as error:  # noqa: BLE001 — CLI boundary must not expose internals.
        for error_type, code in _EXIT_CODES:
            if isinstance(error, error_type):
                print(f"Error: {error}", file=sys.stderr)
                return code
        print("Error: Unexpected internal failure.", file=sys.stderr)
        return 1
    return 0

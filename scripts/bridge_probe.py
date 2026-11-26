"""Phase 0 only: submit one test message or append one test draft to Bridge."""

import argparse
import getpass
import imaplib
import re
import smtplib
import ssl
import sys
import warnings
from contextlib import suppress
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from ipaddress import ip_address
from pathlib import Path
from uuid import uuid4

TIMEOUT = 15
# Plain IMAP LIST, without extended return data.
LIST_ENTRY = re.compile(
    rb'\(([^()]*)\) (?:NIL|"(?:[^"\\]|\\["\\])*") (.+)', re.IGNORECASE
)
QUOTED = re.compile(rb'"((?:[^"\\]|\\["\\])*)"')


class ProbeError(Exception):
    """A diagnostic safe to display without dumping protocol responses."""


def bridge_host(value: str) -> str:
    if value.lower() == "localhost":
        return value
    with suppress(ValueError):
        if ip_address(value).is_loopback:
            return value
    raise argparse.ArgumentTypeError("Use localhost or a loopback IP address.")


def port_number(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535.")
    return port


def mailbox_address(value: str) -> str:
    if value.isascii() and value.isprintable():
        with suppress(ValueError, HeaderParseError):
            address = Address(addr_spec=value)
            if address.username and address.domain and address.addr_spec == value:
                return value
    raise argparse.ArgumentTypeError("Use one bare ASCII email address.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    probes = parser.add_subparsers(dest="probe", required=True)
    for name, help_text in (
        ("smtp", "Send one real test message."),
        ("imap", "Append one draft without sending it."),
    ):
        probe = probes.add_parser(name, help=help_text, description=help_text)
        probe.add_argument("--host", required=True, type=bridge_host)
        probe.add_argument("--port", required=True, type=port_number)
        probe.add_argument("--security", required=True, choices=("starttls", "tls"))
        probe.add_argument(
            "--cert", required=True, type=Path, help="Bridge public PEM certificate"
        )
        probe.add_argument("--from", dest="sender", required=True, type=mailbox_address)
        probe.add_argument("--to", required=True, type=mailbox_address)
        probe.add_argument(
            "--pdf", required=True, type=Path, help="Non-sensitive test PDF"
        )
        probe.add_argument("--format", choices=("plain", "html"), default="plain")
        if name == "imap":
            probe.add_argument(
                "--drafts-mailbox", help="Exact selectable name from IMAP LIST"
            )
    return parser.parse_args()


def make_message(args: argparse.Namespace) -> EmailMessage:
    if args.pdf.suffix.lower() != ".pdf" or not args.pdf.is_file():
        raise ProbeError("--pdf must point to an existing PDF file.")
    try:
        pdf = args.pdf.read_bytes()
    except OSError:
        raise ProbeError("Could not read the file passed to --pdf.") from None
    if not pdf.startswith(b"%PDF-"):
        raise ProbeError("--pdf does not start with a PDF header.")

    message = EmailMessage(policy=SMTP)
    message["From"] = args.sender
    message["To"] = args.to
    message["Subject"] = (
        f"Bridge Phase 0 {args.probe.upper()} {args.format} {uuid4().hex}"
    )
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=args.sender.rsplit("@", 1)[1])
    message.set_content(
        "Proton Mail Bridge compatibility probe — café. See attached PDF."
    )
    if args.format == "html":
        message.add_alternative(
            "<p>Proton Mail Bridge compatibility probe — <strong>café</strong>."
            " See attached PDF.</p>",
            subtype="html",
        )
    message.add_attachment(
        pdf, maintype="application", subtype="pdf", filename=args.pdf.name
    )
    return message


def credentials() -> tuple[str, str]:
    username = input("Bridge username: ").strip()
    # Refuse getpass's fallback to echoing the password when no TTY is available.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = getpass.getpass("Bridge password: ")
    if not all(
        value.isascii() and value.isprintable() for value in (username, password)
    ):
        raise ProbeError(
            "Enter the Bridge-issued username and password (printable ASCII)."
        )
    if not username or not password:
        raise ProbeError("Both Bridge username and password are required.")
    return username, password


def smtp_probe(
    args: argparse.Namespace,
    context: ssl.SSLContext,
    username: str,
    password: str,
    message: EmailMessage,
) -> None:
    if args.security == "tls":
        client = smtplib.SMTP_SSL(
            args.host, args.port, timeout=TIMEOUT, context=context
        )
    else:
        client = smtplib.SMTP(args.host, args.port, timeout=TIMEOUT)
    try:
        if args.security == "starttls":
            if client.ehlo()[0] != 250:
                raise ProbeError("SMTP EHLO was rejected before STARTTLS.")
            client.starttls(context=context)
        if client.ehlo()[0] != 250:
            raise ProbeError("SMTP EHLO was rejected over TLS.")
        client.login(username, password)
        try:
            refused = client.send_message(
                message, from_addr=args.sender, to_addrs=[args.to]
            )
        except (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused):
            raise  # Bridge explicitly rejected a command or the message.
        except OSError:
            raise ProbeError(
                "SMTP outcome unknown. Check Sent and delivery before retrying."
            ) from None
        if refused:
            raise ProbeError("Bridge refused the SMTP recipient.")
    finally:
        with suppress(OSError):
            client.close()


def imap_quote(value: bytes) -> bytes:
    return b'"' + value.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def list_drafts(client: imaplib.IMAP4, override: str | None) -> bytes:
    status, entries = client.list()
    if status != "OK":
        raise ProbeError("Bridge did not accept IMAP LIST.")
    selectable = set()
    drafts = set()
    for entry in entries:
        if entry in (None, b""):
            continue
        line = entry[0] if isinstance(entry, tuple) else entry
        match = LIST_ENTRY.fullmatch(line)
        if not match:
            raise ProbeError("Unexpected IMAP LIST response format; no draft appended.")
        raw_flags, name = match.groups()
        flags = raw_flags.lower().split()
        if isinstance(entry, tuple):
            name = entry[1]
        elif quoted := QUOTED.fullmatch(name):
            name = re.sub(rb'\\(["\\])', rb"\1", quoted[1])
        if rb"\noselect" in flags or rb"\nonexistent" in flags:
            continue
        selectable.add(name)
        if rb"\drafts" in flags:
            drafts.add(name)

    requested = override.encode("utf-8") if override is not None else None
    if requested is None and len(drafts) == 1:
        name = next(iter(drafts))
    elif requested is not None and requested in selectable:
        name = requested
    else:
        names = ", ".join(
            repr(item.decode("ascii", errors="backslashreplace"))
            for item in sorted(selectable)
        )
        raise ProbeError(
            "No unique selectable Drafts mailbox (or override not found). "
            f"Selectable IMAP LIST names: {names or '(none)'}. "
            "Identify the real Drafts mailbox before using --drafts-mailbox."
        )
    if not name or any(byte in name for byte in (b"\x00", b"\r", b"\n")):
        raise ProbeError("Drafts mailbox cannot be safely quoted for IMAP APPEND.")
    return imap_quote(name)


def imap_probe(
    args: argparse.Namespace,
    context: ssl.SSLContext,
    username: str,
    password: str,
    message: EmailMessage,
) -> None:
    if args.security == "tls":
        client = imaplib.IMAP4_SSL(
            args.host, args.port, ssl_context=context, timeout=TIMEOUT
        )
    else:
        client = imaplib.IMAP4(args.host, args.port, timeout=TIMEOUT)
    step = "STARTTLS"
    try:
        if (
            args.security == "starttls"
            and client.starttls(ssl_context=context)[0] != "OK"
        ):
            raise ProbeError("Bridge did not accept IMAP STARTTLS.")
        step = "login"
        client.login(imap_quote(username.encode("ascii")), password)
        step = "LIST"
        mailbox = list_drafts(client, args.drafts_mailbox)
        step = "APPEND"
        try:
            status, _ = client.append(
                mailbox, r"(\Draft)", None, message.as_bytes(policy=SMTP)
            )
        except (imaplib.IMAP4.abort, OSError):
            raise ProbeError(
                "IMAP outcome unknown. Check web Drafts before retrying."
            ) from None
        if status != "OK":
            raise ProbeError("Bridge did not accept IMAP APPEND.")
    except imaplib.IMAP4.error:
        raise ProbeError(f"IMAP {step} failed.") from None
    finally:
        with suppress(OSError):
            client.shutdown()


def main() -> int:
    args = parse_args()
    try:
        message = make_message(args)
        # Trust only the exported Bridge certificate, with hostname verification on.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            context.load_verify_locations(cafile=str(args.cert))
        except ssl.SSLError:
            raise ProbeError(
                "--cert must contain a valid public PEM certificate."
            ) from None
        except OSError:
            raise ProbeError(
                "Could not read --cert. Check the certificate path and permissions."
            ) from None
        username, password = credentials()
        print(f"Test subject: {message['Subject']}", flush=True)
        if args.probe == "smtp":
            smtp_probe(args, context, username, password, message)
            print(
                "Bridge accepted SMTP submission. Check delivery and Sent in Proton Mail."
            )
        else:
            imap_probe(args, context, username, password, message)
            print(
                "Bridge accepted IMAP APPEND. Verify the editable draft in Proton Mail web."
            )
    except ProbeError as error:
        print(f"Probe failed: {error}", file=sys.stderr)
        return 1
    except ssl.SSLCertVerificationError as error:
        print(
            f"TLS certificate verification failed: {error.verify_message}",
            file=sys.stderr,
        )
        return 1
    except ssl.SSLError:
        print(
            "TLS setup failed. Check the public PEM certificate and Bridge security mode.",
            file=sys.stderr,
        )
        return 1
    except smtplib.SMTPAuthenticationError:
        print(
            "SMTP authentication failed. Check the Bridge-issued credentials.",
            file=sys.stderr,
        )
        return 1
    except smtplib.SMTPException:
        print(
            "SMTP failed. Check Bridge settings and sender/recipient acceptance.",
            file=sys.stderr,
        )
        return 1
    except (OSError, imaplib.IMAP4.error):
        print(
            "Could not communicate with Bridge. Check that it is running and confirm the host and port.",
            file=sys.stderr,
        )
        return 1
    except (EOFError, getpass.GetPassWarning, KeyboardInterrupt):
        print(
            "Probe interrupted or no secure interactive terminal available. "
            "If submission started, check Proton Mail before retrying.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

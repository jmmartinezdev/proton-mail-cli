import imaplib
import ssl
from dataclasses import replace
from email.policy import SMTP
from unittest.mock import Mock, call

import pytest

from proton_mail_cli.errors import (
    BridgeAuthenticationError,
    BridgeConnectionError,
    BridgeRejectionError,
    BridgeTLSError,
    UncertainOutcomeError,
)
from proton_mail_cli.imap_client import save_draft
from proton_mail_cli.message_builder import build_message
from proton_mail_cli.models import BridgeConfig, MessageRequest, OperationResult

PASSWORD = 'private "Bridge" password'
SERVER_DETAIL = b"private IMAP response"
DRAFTS = rb'(\Drafts) "/" Drafts'
IMAP_ERROR = imaplib.IMAP4.error
IMAP_ABORT = imaplib.IMAP4.abort


@pytest.fixture
def bridge_config(tmp_path):
    return BridgeConfig(
        host="localhost",
        smtp_port=1025,
        imap_port=1143,
        username='bridge "user"',
        security="starttls",
        timeout=3.5,
        certificate=tmp_path / "bridge.pem",
        allowed_senders=("billing@example.com",),
    )


@pytest.fixture
def message(bridge_config, tmp_path):
    body = tmp_path / "body.txt"
    body.write_text("Private message body — café", encoding="utf-8")
    return build_message(
        MessageRequest(
            sender="billing@example.com",
            recipient="customer@example.net",
            subject="Invoice",
            body=body,
            allowed_senders=bridge_config.allowed_senders,
        )
    )


@pytest.fixture
def imap(monkeypatch):
    client = Mock(spec=imaplib.IMAP4)
    client.capabilities = ("IMAP4REV1", "STARTTLS")
    client.starttls.return_value = ("OK", [b"TLS ready"])
    client.login.return_value = ("OK", [b"authenticated"])
    client.list.return_value = ("OK", [DRAFTS])
    client.append.return_value = ("OK", [b"accepted"])
    client.logout.return_value = ("BYE", [b"closed"])
    starttls = Mock(return_value=client, error=IMAP_ERROR, abort=IMAP_ABORT)
    implicit_tls = Mock(return_value=client)
    load_certificate = Mock()
    monkeypatch.setattr(imaplib, "IMAP4", starttls)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", implicit_tls)
    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", load_certificate)
    return client, starttls, implicit_tls, load_certificate


def _assert_verified(context):
    assert context.protocol == ssl.PROTOCOL_TLS_CLIENT
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_starttls_order_and_exact_append(imap, message, bridge_config):
    client, starttls, implicit_tls, load_certificate = imap
    client.list.return_value = (
        "OK",
        [rb'(\Sent) "/" Sent', rb'(\dRaFtS) "/" "My Drafts"'],
    )

    result = save_draft(message, bridge_config, PASSWORD)

    assert result == OperationResult("draft", "Bridge accepted the draft.")
    context = client.starttls.call_args.kwargs["ssl_context"]
    _assert_verified(context)
    load_certificate.assert_called_once_with(cafile=bridge_config.certificate)
    starttls.assert_called_once_with(
        bridge_config.host, bridge_config.imap_port, timeout=bridge_config.timeout
    )
    implicit_tls.assert_not_called()
    assert client.mock_calls == [
        call.starttls(ssl_context=context),
        call.login(rb'"bridge \"user\""', PASSWORD),
        call.list(),
        call.append(b'"My Drafts"', r"(\Draft)", None, message.as_bytes(policy=SMTP)),
        call.logout(),
    ]


def test_implicit_tls_verifies_bridge_and_does_not_upgrade(
    imap, message, bridge_config
):
    client, starttls, implicit_tls, load_certificate = imap
    config = replace(bridge_config, security="tls")

    assert save_draft(message, config, PASSWORD).operation == "draft"

    context = implicit_tls.call_args.kwargs["ssl_context"]
    _assert_verified(context)
    implicit_tls.assert_called_once_with(
        config.host, config.imap_port, ssl_context=context, timeout=config.timeout
    )
    load_certificate.assert_called_once_with(cafile=config.certificate)
    starttls.assert_not_called()
    assert [step[0] for step in client.mock_calls] == [
        "login",
        "list",
        "append",
        "logout",
    ]


def test_bad_certificate_never_connects(imap, message, bridge_config):
    _, starttls, implicit_tls, load_certificate = imap
    load_certificate.side_effect = ssl.SSLError("private certificate details")

    with pytest.raises(BridgeTLSError, match="trusted Bridge certificate") as exc:
        save_draft(message, bridge_config, PASSWORD)

    assert "private certificate details" not in str(exc.value)
    starttls.assert_not_called()
    implicit_tls.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        ("NO", [SERVER_DETAIL]),
        IMAP_ERROR(SERVER_DETAIL),
        ssl.SSLCertVerificationError(SERVER_DETAIL),
    ],
)
def test_failed_upgrade_never_logs_in(imap, message, bridge_config, failure):
    client, _, implicit_tls, _ = imap
    if isinstance(failure, Exception):
        client.starttls.side_effect = failure
    else:
        client.starttls.return_value = failure
    with pytest.raises(BridgeTLSError) as exc:
        save_draft(message, bridge_config, PASSWORD)
    assert SERVER_DETAIL.decode() not in str(exc.value)
    client.login.assert_not_called()
    implicit_tls.assert_not_called()
    if isinstance(failure, ssl.SSLError):
        client.shutdown.assert_called_once_with()
    else:
        client.logout.assert_called_once_with()


def test_missing_starttls_capability_fails_closed(imap, message, bridge_config):
    client, *_ = imap
    client.capabilities = ("IMAP4REV1",)
    with pytest.raises(BridgeTLSError, match="does not advertise"):
        save_draft(message, bridge_config, PASSWORD)
    client.starttls.assert_not_called()
    client.login.assert_not_called()


def test_implicit_tls_failure_does_not_fall_back(imap, message, bridge_config):
    client, starttls, implicit_tls, _ = imap
    implicit_tls.side_effect = ssl.SSLCertVerificationError(SERVER_DETAIL)
    with pytest.raises(BridgeTLSError, match="verified IMAP TLS"):
        save_draft(message, replace(bridge_config, security="tls"), PASSWORD)
    starttls.assert_not_called()
    client.login.assert_not_called()


def test_login_rejection_is_sanitized(imap, message, bridge_config):
    client, *_ = imap
    client.login.side_effect = IMAP_ERROR(PASSWORD)
    client.logout.side_effect = OSError("private logout failure")
    with pytest.raises(BridgeAuthenticationError, match="authentication failed") as exc:
        save_draft(message, bridge_config, PASSWORD)
    assert PASSWORD not in str(exc.value)
    client.list.assert_not_called()
    client.append.assert_not_called()
    client.logout.assert_called_once_with()
    client.shutdown.assert_called_once_with()


def test_login_rejects_unsafe_credentials(imap, message, bridge_config):
    client, *_ = imap
    with pytest.raises(BridgeAuthenticationError, match="printable ASCII"):
        save_draft(message, bridge_config, PASSWORD + "\r\n")
    client.login.assert_not_called()


def test_override_requires_exact_selectable_name_and_quotes_it(
    imap, message, bridge_config
):
    client, *_ = imap
    quoted = rb'"Local \"Drafts\" \\ Archive"'
    client.list.return_value = ("OK", [DRAFTS, rb'(\HasNoChildren) "/" ' + quoted])
    config = replace(bridge_config, drafts_mailbox='Local "Drafts" \\ Archive')
    assert save_draft(message, config, PASSWORD).operation == "draft"
    client.append.assert_called_once_with(
        quoted, r"(\Draft)", None, message.as_bytes(policy=SMTP)
    )


def test_literal_list_name_is_quoted_for_append(imap, message, bridge_config):
    client, *_ = imap
    client.list.return_value = ("OK", [(rb"(\Drafts) NIL {9}", b"My Drafts"), b""])
    save_draft(message, bridge_config, PASSWORD)
    assert client.append.call_args.args[0] == b'"My Drafts"'


@pytest.mark.parametrize(
    ("listing", "override", "error"),
    [
        (("OK", [None]), None, "No selectable"),
        (("OK", [rb'() "/" Drafts']), None, "No selectable"),
        (("OK", [rb'(\Drafts \Noselect) "/" Drafts']), None, "No selectable"),
        (
            ("OK", [DRAFTS, rb'(\Drafts) "/" "Other Drafts"']),
            None,
            "Multiple selectable",
        ),
        (("OK", [DRAFTS]), "drafts", "was not found"),
        (
            ("OK", [DRAFTS, rb'(\NonExistent) "/" Deleted']),
            "Deleted",
            "was not found",
        ),
        (("NO", [SERVER_DETAIL]), None, "rejected IMAP LIST"),
        (("OK", [SERVER_DETAIL]), None, "interpret"),
        (("OK", [rb'(\Drafts) "/" "unclosed']), None, "interpret"),
        (("OK", [(rb"(\Drafts) NIL {3}", b"x\0x"), b""]), None, "safely quoted"),
    ],
)
def test_invalid_drafts_discovery_never_appends(
    imap, message, bridge_config, listing, override, error
):
    client, *_ = imap
    client.list.return_value = listing
    with pytest.raises(BridgeRejectionError, match=error) as exc:
        save_draft(message, replace(bridge_config, drafts_mailbox=override), PASSWORD)
    assert SERVER_DETAIL.decode() not in str(exc.value)
    client.append.assert_not_called()
    client.logout.assert_called_once_with()


@pytest.mark.parametrize(
    "failure", [("NO", [SERVER_DETAIL]), IMAP_ERROR(SERVER_DETAIL)]
)
def test_explicit_append_rejection_is_not_uncertain(
    imap, message, bridge_config, failure
):
    client, *_ = imap
    if isinstance(failure, Exception):
        client.append.side_effect = failure
    else:
        client.append.return_value = failure
    with pytest.raises(BridgeRejectionError, match="rejected IMAP APPEND") as exc:
        save_draft(message, bridge_config, PASSWORD)
    assert SERVER_DETAIL.decode() not in str(exc.value)
    client.append.assert_called_once()
    client.logout.assert_called_once_with()


@pytest.mark.parametrize(
    "failure", [IMAP_ABORT(SERVER_DETAIL), TimeoutError(SERVER_DETAIL)]
)
@pytest.mark.parametrize("stage", ["list", "append"])
def test_loss_of_connection_depends_on_whether_append_started(
    imap, message, bridge_config, failure, stage
):
    client, starttls, implicit_tls, _ = imap
    getattr(client, stage).side_effect = failure
    if stage == "append":
        with pytest.raises(UncertainOutcomeError, match="web Drafts before retrying"):
            save_draft(message, bridge_config, PASSWORD)
        client.append.assert_called_once()
    else:
        with pytest.raises(BridgeConnectionError, match="stay connected"):
            save_draft(message, bridge_config, PASSWORD)
        client.append.assert_not_called()
    starttls.assert_called_once()
    implicit_tls.assert_not_called()
    client.logout.assert_not_called()
    client.shutdown.assert_called_once_with()


def test_cleanup_failure_does_not_undo_acceptance(imap, message, bridge_config):
    client, *_ = imap
    client.logout.side_effect = IMAP_ABORT(SERVER_DETAIL)
    client.shutdown.side_effect = OSError("private cleanup failure")
    assert save_draft(message, bridge_config, PASSWORD).operation == "draft"
    client.logout.assert_called_once_with()
    client.shutdown.assert_called_once_with()
    client.socket.return_value.close.assert_called_once_with()

import smtplib
import ssl
from dataclasses import replace
from unittest.mock import Mock, call

import pytest

from proton_mail_cli.errors import (
    BridgeAuthenticationError,
    BridgeConnectionError,
    BridgeRejectionError,
    BridgeTLSError,
    InputValidationError,
    UncertainOutcomeError,
)
from proton_mail_cli.message_builder import build_message
from proton_mail_cli.models import BridgeConfig, MessageRequest, OperationResult
from proton_mail_cli.smtp_client import send_message

PASSWORD = "private Bridge password"
SERVER_DETAIL = b"private SMTP response"


@pytest.fixture
def bridge_config(tmp_path):
    return BridgeConfig(
        host="localhost",
        smtp_port=1025,
        imap_port=1143,
        username="bridge-user",
        security="starttls",
        timeout=3.5,
        certificate=tmp_path / "bridge.pem",
        allowed_senders=("billing@example.com",),
    )


@pytest.fixture
def message(bridge_config, tmp_path):
    body = tmp_path / "body.txt"
    body.write_text("Private message body", encoding="utf-8")
    return build_message(
        MessageRequest(
            sender="Billing@example.com",
            recipient="customer@example.net",
            subject="Invoice",
            body=body,
            allowed_senders=bridge_config.allowed_senders,
        )
    )


@pytest.fixture
def smtp(monkeypatch):
    client = Mock(spec=smtplib.SMTP)
    client.connect.return_value = (220, b"ready")
    client.ehlo.return_value = (250, b"hello")
    client.starttls.return_value = (220, b"ready for TLS")
    client.login.return_value = (235, b"authenticated")
    client.send_message.return_value = {}
    plaintext = Mock(return_value=client)
    implicit_tls = Mock(return_value=client)
    load_certificate = Mock()
    monkeypatch.setattr(smtplib, "SMTP", plaintext)
    monkeypatch.setattr(smtplib, "SMTP_SSL", implicit_tls)
    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", load_certificate)
    return client, plaintext, implicit_tls, load_certificate


def _assert_verified(context):
    assert context.protocol == ssl.PROTOCOL_TLS_CLIENT
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_starttls_order_and_explicit_envelope(smtp, message, bridge_config):
    client, plaintext, implicit_tls, load_certificate = smtp

    result = send_message(message, bridge_config, PASSWORD)

    assert result == OperationResult("send", "Bridge accepted the message.")
    context = client.starttls.call_args.kwargs["context"]
    _assert_verified(context)
    load_certificate.assert_called_once_with(cafile=bridge_config.certificate)
    plaintext.assert_called_once_with(timeout=bridge_config.timeout)
    implicit_tls.assert_not_called()
    assert client.mock_calls == [
        call.connect(bridge_config.host, bridge_config.smtp_port),
        call.ehlo(),
        call.starttls(context=context),
        call.ehlo(),
        call.login(bridge_config.username, PASSWORD),
        call.send_message(
            message, from_addr="Billing@example.com", to_addrs=["customer@example.net"]
        ),
        call.close(),
    ]


def test_implicit_tls_verifies_bridge_and_never_upgrades(smtp, message, bridge_config):
    client, plaintext, implicit_tls, load_certificate = smtp
    config = replace(bridge_config, security="tls")

    assert send_message(message, config, PASSWORD).operation == "send"

    context = implicit_tls.call_args.kwargs["context"]
    _assert_verified(context)
    implicit_tls.assert_called_once_with(timeout=config.timeout, context=context)
    load_certificate.assert_called_once_with(cafile=config.certificate)
    plaintext.assert_not_called()
    client.starttls.assert_not_called()
    assert [step[0] for step in client.mock_calls] == [
        "connect",
        "ehlo",
        "login",
        "send_message",
        "close",
    ]


def test_bad_certificate_never_connects(smtp, message, bridge_config):
    _, plaintext, implicit_tls, load_certificate = smtp
    load_certificate.side_effect = ssl.SSLError("private certificate details")

    with pytest.raises(BridgeTLSError, match="trusted Bridge certificate") as exc:
        send_message(message, bridge_config, PASSWORD)
    assert "private certificate details" not in str(exc.value)
    plaintext.assert_not_called()
    implicit_tls.assert_not_called()


@pytest.mark.parametrize(
    ("command", "failure", "error"),
    [
        ("ehlo", (550, SERVER_DETAIL), BridgeRejectionError),
        ("starttls", (454, SERVER_DETAIL), BridgeTLSError),
        ("starttls", smtplib.SMTPNotSupportedError(SERVER_DETAIL), BridgeTLSError),
        ("starttls", ssl.SSLCertVerificationError(SERVER_DETAIL), BridgeTLSError),
    ],
)
def test_negotiation_failure_never_authenticates(
    smtp, message, bridge_config, command, failure, error
):
    client, _, implicit_tls, _ = smtp
    step = getattr(client, command)
    if isinstance(failure, Exception):
        step.side_effect = failure
    else:
        step.return_value = failure

    with pytest.raises(error) as exc:
        send_message(message, bridge_config, PASSWORD)

    assert SERVER_DETAIL.decode() not in str(exc.value)
    implicit_tls.assert_not_called()
    client.login.assert_not_called()
    client.send_message.assert_not_called()
    client.close.assert_called_once_with()


def test_post_upgrade_ehlo_must_succeed(smtp, message, bridge_config):
    client, *_ = smtp
    client.ehlo.side_effect = [(250, b"hello"), (550, SERVER_DETAIL)]
    with pytest.raises(BridgeRejectionError, match="EHLO"):
        send_message(message, bridge_config, PASSWORD)
    client.starttls.assert_called_once()
    client.login.assert_not_called()
    client.close.assert_called_once_with()


def test_implicit_tls_does_not_fall_back_after_verification_failure(
    smtp, message, bridge_config
):
    client, plaintext, _, _ = smtp
    client.connect.side_effect = ssl.SSLCertVerificationError(SERVER_DETAIL)

    with pytest.raises(BridgeTLSError, match="verified SMTP TLS"):
        send_message(message, replace(bridge_config, security="tls"), PASSWORD)

    plaintext.assert_not_called()
    client.login.assert_not_called()
    client.close.assert_called_once_with()


def test_authentication_failure_is_sanitized_even_if_close_fails(
    smtp, message, bridge_config
):
    client, *_ = smtp
    client.login.side_effect = smtplib.SMTPAuthenticationError(535, PASSWORD.encode())
    client.close.side_effect = OSError("private cleanup details")

    with pytest.raises(BridgeAuthenticationError, match="authentication failed") as exc:
        send_message(message, bridge_config, PASSWORD)

    assert PASSWORD not in str(exc.value)
    assert exc.value.__suppress_context__
    client.send_message.assert_not_called()
    client.close.assert_called_once_with()


@pytest.mark.parametrize(
    ("command", "failure"),
    [
        ("connect", ConnectionRefusedError("private socket details")),
        ("ehlo", TimeoutError("private timeout details")),
        ("login", smtplib.SMTPServerDisconnected("private disconnect details")),
    ],
)
def test_pre_submission_connection_failure(
    smtp, message, bridge_config, command, failure
):
    client, *_ = smtp
    getattr(client, command).side_effect = failure
    with pytest.raises(BridgeConnectionError) as exc:
        send_message(message, bridge_config, PASSWORD)
    assert str(failure) not in str(exc.value)
    client.send_message.assert_not_called()
    client.close.assert_called_once_with()


def test_rejected_greeting_closes_connection(smtp, message, bridge_config):
    client, *_ = smtp
    client.connect.return_value = (421, SERVER_DETAIL)
    with pytest.raises(BridgeConnectionError, match="did not accept"):
        send_message(message, bridge_config, PASSWORD)
    client.ehlo.assert_not_called()
    client.close.assert_called_once_with()


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ({"customer@example.net": (550, SERVER_DETAIL)}, "refused.*recipient"),
        (
            smtplib.SMTPRecipientsRefused(
                {"customer@example.net": (550, SERVER_DETAIL)}
            ),
            "refused.*recipient",
        ),
        (
            smtplib.SMTPSenderRefused(550, SERVER_DETAIL, "Billing@example.com"),
            "rejected",
        ),
        (smtplib.SMTPDataError(554, SERVER_DETAIL), "rejected"),
    ],
)
def test_explicit_refusal_is_not_an_uncertain_outcome(
    smtp, message, bridge_config, failure, error
):
    client, *_ = smtp
    if isinstance(failure, Exception):
        client.send_message.side_effect = failure
    else:
        client.send_message.return_value = failure

    with pytest.raises(BridgeRejectionError, match=error) as exc:
        send_message(message, bridge_config, PASSWORD)
    assert SERVER_DETAIL.decode() not in str(exc.value)
    assert "customer@example.net" not in str(exc.value)
    client.send_message.assert_called_once()
    client.close.assert_called_once_with()


@pytest.mark.parametrize(
    "failure",
    [
        smtplib.SMTPServerDisconnected("private disconnect details"),
        TimeoutError("private timeout details"),
        ConnectionResetError("private reset details"),
        ssl.SSLEOFError("private TLS details"),
    ],
)
def test_connection_lost_during_submission_is_uncertain_and_never_retried(
    smtp, message, bridge_config, failure
):
    client, plaintext, implicit_tls, _ = smtp
    client.send_message.side_effect = failure

    with pytest.raises(UncertainOutcomeError, match="verify delivery before retrying"):
        send_message(message, bridge_config, PASSWORD)
    plaintext.assert_called_once()
    implicit_tls.assert_not_called()
    client.send_message.assert_called_once()
    client.close.assert_called_once_with()


def test_cleanup_failure_does_not_undo_acceptance(smtp, message, bridge_config):
    client, *_ = smtp
    client.close.side_effect = OSError("private cleanup details")
    assert send_message(message, bridge_config, PASSWORD).operation == "send"
    client.close.assert_called_once_with()


def test_missing_envelope_recipient_fails_before_connecting(
    smtp, message, bridge_config
):
    _, plaintext, implicit_tls, _ = smtp
    del message["To"]
    with pytest.raises(InputValidationError, match="one To mailbox"):
        send_message(message, bridge_config, PASSWORD)
    plaintext.assert_not_called()
    implicit_tls.assert_not_called()
